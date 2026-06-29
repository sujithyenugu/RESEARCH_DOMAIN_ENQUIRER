"""
ingestion_stack.py — Day 2: Ingestion Pipeline

Provisions the full paper ingestion pipeline:
  ✅ EventBridge Scheduler — triggers paper_fetcher every 6 hours
  ✅ SQS FIFO Paper Queue + DLQ — FIFO with dedup, 900s visibility
  ✅ SQS Standard Embedding Queue + DLQ — downstream for embedding_worker
  ✅ SQS Standard Entity Queue + DLQ — downstream for entity_extractor
  ✅ Lambda: paper_fetcher — arXiv API → dedup check → SQS send
  ✅ Lambda: paper_processor — SQS consumer → PDF download → Textract
                               → text_cleaner → late_chunker → S3 write
  ✅ IAM Roles — least-privilege for each Lambda
  ✅ CloudWatch Alarms — DLQ depth > 10 triggers SNS alert

Deploy AFTER StorageStack:
  cdk deploy StorageStack
  cdk deploy IngestionStack
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from stacks import config
from stacks.storage_stack import StorageStack


class IngestionStack(Stack):
    """
    Ingestion Pipeline — EventBridge → paper_fetcher → SQS → paper_processor.

    Exported properties (used by downstream stacks):
      self.paper_queue            — SQS FIFO queue (paper_processor trigger)
      self.embedding_queue        — SQS standard queue (embedding_worker trigger)
      self.entity_queue           — SQS standard queue (entity_extractor trigger)
      self.paper_fetcher_fn       — Lambda: arXiv fetcher
      self.paper_processor_fn     — Lambda: PDF download + parse + clean + chunk
      self.ops_topic              — SNS topic for operational alarms
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        storage: StorageStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._storage = storage

        # Global tags
        Tags.of(self).add("Project",     config.PROJECT_NAME)
        Tags.of(self).add("Stack",       "IngestionStack")
        Tags.of(self).add("Environment", "production")
        Tags.of(self).add("ManagedBy",   "aws-cdk")

        # -----------------------------------------------------------------
        # 1. SNS Topic — operational alarms (DLQ depth, Lambda errors)
        # -----------------------------------------------------------------
        self.ops_topic = self._create_ops_topic()

        # -----------------------------------------------------------------
        # 2. SQS Queues + DLQs
        # -----------------------------------------------------------------
        self.paper_dlq,     self.paper_queue     = self._create_paper_queue()
        self.embedding_dlq, self.embedding_queue = self._create_embedding_queue()
        self.entity_dlq,    self.entity_queue    = self._create_entity_queue()

        # -----------------------------------------------------------------
        # 3. Lambda Functions
        # -----------------------------------------------------------------
        self.paper_fetcher_fn   = self._create_paper_fetcher()
        self.paper_processor_fn = self._create_paper_processor()

        # -----------------------------------------------------------------
        # 4. EventBridge Scheduler — trigger fetcher every 6 hours
        # -----------------------------------------------------------------
        self._create_fetch_schedule()

        # -----------------------------------------------------------------
        # 5. SQS → Lambda event source mappings
        # -----------------------------------------------------------------
        self._wire_event_sources()

        # -----------------------------------------------------------------
        # 6. CloudWatch Alarms on DLQs and Lambda errors
        # -----------------------------------------------------------------
        self._create_alarms()

        # -----------------------------------------------------------------
        # 7. CloudFormation Outputs
        # -----------------------------------------------------------------
        self._create_outputs()

    # =====================================================================
    # SNS
    # =====================================================================

    def _create_ops_topic(self) -> sns.Topic:
        """
        Operational SNS topic — all ingestion alarms publish here.
        Subscribers (e.g. email, Slack) are added manually post-deploy.
        """
        topic = sns.Topic(
            self,
            "OpsAlarmTopic",
            topic_name=f"{config.PROJECT_PREFIX}-ingestion-ops",
            display_name="Research RAG — Ingestion Pipeline Alarms",
        )
        Tags.of(topic).add("Name", f"{config.PROJECT_PREFIX}-ingestion-ops")
        return topic

    # =====================================================================
    # SQS Queues
    # =====================================================================

    def _create_paper_queue(self) -> tuple[sqs.Queue, sqs.Queue]:
        """
        SQS FIFO queue pair for individual paper messages.

        FIFO guarantees:
          - Exactly-once delivery (content-based deduplication)
          - Ordering within a MessageGroupId (one group per arXiv run)

        Visibility timeout == Lambda timeout (900 s) to prevent double-processing
        while a paper is actively being fetched and parsed.

        Matches INFRASTRUCTURE.md §SQS + config.py SQS_* constants.
        """
        dlq = sqs.Queue(
            self,
            "PaperDLQ",
            queue_name=config.SQS_PAPER_DLQ_NAME,   # ends in .fifo
            fifo=True,
            content_based_deduplication=True,
            retention_period=Duration.days(14),      # Keep failed messages 2 weeks
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(dlq).add("Name", config.SQS_PAPER_DLQ_NAME)

        queue = sqs.Queue(
            self,
            "PaperQueue",
            queue_name=config.SQS_PAPER_QUEUE_NAME,  # ends in .fifo
            fifo=True,
            content_based_deduplication=True,
            visibility_timeout=Duration.seconds(config.SQS_PAPER_VISIBILITY_TIMEOUT),
            retention_period=Duration.days(config.SQS_MESSAGE_RETENTION_DAYS),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=config.SQS_DLQ_MAX_RECEIVE_COUNT,
                queue=dlq,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(queue).add("Name", config.SQS_PAPER_QUEUE_NAME)
        return dlq, queue

    def _create_embedding_queue(self) -> tuple[sqs.Queue, sqs.Queue]:
        """
        Standard SQS queue for chunks destined for Bedrock embedding.

        Standard (not FIFO) because:
          - Embedding is idempotent — safe to retry duplicates
          - Higher throughput needed (40–80 chunks × 5 concurrent processors)
          - No ordering requirement across papers
        """
        dlq = sqs.Queue(
            self,
            "EmbeddingDLQ",
            queue_name=config.SQS_EMBEDDING_DLQ_NAME,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(dlq).add("Name", config.SQS_EMBEDDING_DLQ_NAME)

        queue = sqs.Queue(
            self,
            "EmbeddingQueue",
            queue_name=config.SQS_EMBEDDING_QUEUE_NAME,
            visibility_timeout=Duration.seconds(
                config.LAMBDA_CONFIGS["embedding_worker"]["timeout_seconds"]
            ),
            retention_period=Duration.days(config.SQS_MESSAGE_RETENTION_DAYS),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=config.SQS_DLQ_MAX_RECEIVE_COUNT,
                queue=dlq,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(queue).add("Name", config.SQS_EMBEDDING_QUEUE_NAME)
        return dlq, queue

    def _create_entity_queue(self) -> tuple[sqs.Queue, sqs.Queue]:
        """
        Standard SQS queue for cleaned paper sections destined for
        entity extraction (Bedrock Claude Haiku).
        """
        dlq = sqs.Queue(
            self,
            "EntityDLQ",
            queue_name=config.SQS_ENTITY_DLQ_NAME,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(dlq).add("Name", config.SQS_ENTITY_DLQ_NAME)

        queue = sqs.Queue(
            self,
            "EntityQueue",
            queue_name=config.SQS_ENTITY_QUEUE_NAME,
            visibility_timeout=Duration.seconds(
                config.LAMBDA_CONFIGS["entity_extractor"]["timeout_seconds"]
            ),
            retention_period=Duration.days(config.SQS_MESSAGE_RETENTION_DAYS),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=config.SQS_DLQ_MAX_RECEIVE_COUNT,
                queue=dlq,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(queue).add("Name", config.SQS_ENTITY_QUEUE_NAME)
        return dlq, queue

    # =====================================================================
    # Lambda: paper_fetcher
    # =====================================================================

    def _create_paper_fetcher(self) -> lambda_.Function:
        """
        paper_fetcher Lambda — runs every 6 hours.

        Responsibilities:
          1. Query arXiv Atom API for each configured category
          2. Check DynamoDB for existing paper_id (deduplication)
          3. Send new papers to SQS FIFO paper queue

        Runtime: Python 3.11, 128 MB, 30s timeout (arXiv is fast)
        VPC: No — arXiv API is public internet; Lambda uses NAT
        """
        cfg = config.LAMBDA_CONFIGS["paper_fetcher"]

        role = self._create_fetcher_role()

        fn = lambda_.Function(
            self,
            "PaperFetcherFn",
            function_name=f"{config.PROJECT_PREFIX}-paper-fetcher",
            description="Queries arXiv API, deduplicates against DynamoDB, sends to SQS",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/paper_fetcher"),
            role=role,
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            reserved_concurrent_executions=cfg["concurrency"],

            environment={
                # arXiv config
                "ARXIV_CATEGORIES":     ",".join(config.ARXIV_CATEGORIES),
                "ARXIV_LOOKBACK_HOURS": str(config.ARXIV_LOOKBACK_HOURS),
                "ARXIV_MAX_RESULTS":    str(config.ARXIV_MAX_RESULTS),

                # AWS resource names
                "PAPER_QUEUE_URL":          self.paper_queue.queue_url,
                "PAPER_METADATA_TABLE":     config.DYNAMO_PAPER_METADATA_TABLE,

                # Observability
                "CW_NAMESPACE":             config.CW_NAMESPACE,
                "LOG_LEVEL":                "INFO",
            },

            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        Tags.of(fn).add("Name",      f"{config.PROJECT_PREFIX}-paper-fetcher")
        Tags.of(fn).add("Component", "IngestionPipeline")
        return fn

    def _create_fetcher_role(self) -> iam.Role:
        """
        Least-privilege IAM role for paper_fetcher.

        Grants:
          - DynamoDB: GetItem on PaperMetadataTable (dedup check only)
          - SQS: SendMessage on paper_queue (FIFO)
          - CloudWatch: PutMetricData for custom metrics
          - X-Ray: PutTraceSegments (optional tracing)
        """
        role = iam.Role(
            self,
            "PaperFetcherRole",
            role_name=f"{config.PROJECT_PREFIX}-paper-fetcher-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="paper_fetcher Lambda — arXiv → DynamoDB dedup → SQS",
            managed_policies=[
                # Basic Lambda execution (CloudWatch Logs)
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # DynamoDB — read only, only on paper metadata table
        role.add_to_policy(iam.PolicyStatement(
            sid="DynamoReadPaperMetadata",
            effect=iam.Effect.ALLOW,
            actions=["dynamodb:GetItem"],
            resources=[self._storage.paper_metadata_table.table_arn],
        ))

        # SQS — send only, only to paper queue
        role.add_to_policy(iam.PolicyStatement(
            sid="SqsSendPaperQueue",
            effect=iam.Effect.ALLOW,
            actions=["sqs:SendMessage", "sqs:GetQueueAttributes"],
            resources=[self.paper_queue.queue_arn],
        ))

        # CloudWatch — publish custom ingestion metrics
        role.add_to_policy(iam.PolicyStatement(
            sid="CwPutMetrics",
            effect=iam.Effect.ALLOW,
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={
                "StringEquals": {"cloudwatch:namespace": config.CW_NAMESPACE}
            },
        ))

        # X-Ray — active tracing
        role.add_to_policy(iam.PolicyStatement(
            sid="XRayTracing",
            effect=iam.Effect.ALLOW,
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources=["*"],
        ))

        return role

    # =====================================================================
    # Lambda: paper_processor
    # =====================================================================

    def _create_paper_processor(self) -> lambda_.Function:
        """
        paper_processor Lambda — triggered by SQS paper queue.

        Responsibilities:
          1. Download PDF from arXiv URL → stream to S3 raw-papers
          2. Invoke Textract StartDocumentAnalysis for OCR
          3. Call Docling container Lambda for structural parsing
          4. Run text_cleaner (10 cleaning rules) → S3 cleaned-papers
          5. Write metadata record to DynamoDB (conditional put)
          6. Run late_chunker → chunks with pre-computed embeddings
          7. Send chunks to SQS embedding queue
          8. Send sections to SQS entity queue

        Runtime: Python 3.11, 512 MB, 900s timeout
        VPC: Yes — needs private connectivity to OpenSearch/Neptune subnets
              (Textract and Bedrock reached via VPC endpoints)
        Concurrency: 5 (matches INGESTION_PIPELINE.md §Throughput)
        """
        cfg = config.LAMBDA_CONFIGS["paper_processor"]

        role = self._create_processor_role()

        fn = lambda_.Function(
            self,
            "PaperProcessorFn",
            function_name=f"{config.PROJECT_PREFIX}-paper-processor",
            description=(
                "Consumes SQS paper queue: download PDF → Textract → clean → chunk → S3"
            ),
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/paper_processor"),
            role=role,
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            reserved_concurrent_executions=cfg["concurrency"],

            # VPC attachment — same subnets as other data-plane Lambdas
            vpc=self._storage.vpc,
            vpc_subnets=cdk.aws_ec2.SubnetSelection(
                subnet_group_name="LambdaSubnet"
            ),
            security_groups=[self._storage.sg_lambda],

            environment={
                # S3 buckets
                "RAW_PAPERS_BUCKET":     config.S3_RAW_PAPERS,
                "PARSED_PAPERS_BUCKET":  config.S3_PARSED_PAPERS,
                "CLEANED_PAPERS_BUCKET": config.S3_CLEANED_PAPERS,

                # DynamoDB
                "PAPER_METADATA_TABLE":  config.DYNAMO_PAPER_METADATA_TABLE,

                # SQS downstream queues
                "EMBEDDING_QUEUE_URL":   self.embedding_queue.queue_url,
                "ENTITY_QUEUE_URL":      self.entity_queue.queue_url,

                # Docling Lambda (invoked synchronously for parsing)
                "DOCLING_LAMBDA_NAME":   f"{config.PROJECT_PREFIX}-docling-parser",

                # Bedrock — for late chunking embeddings
                "BEDROCK_EMBEDDING_MODEL": config.BEDROCK_EMBEDDING_MODEL,

                # Chunking params (from config constants)
                "CHUNK_MIN_TOKENS":     "100",
                "CHUNK_MAX_TOKENS":     "512",
                "CHUNK_MAX_SECTION_TOKENS": "8192",

                # Observability
                "CW_NAMESPACE":         config.CW_NAMESPACE,
                "LOG_LEVEL":            "INFO",
            },

            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        Tags.of(fn).add("Name",      f"{config.PROJECT_PREFIX}-paper-processor")
        Tags.of(fn).add("Component", "IngestionPipeline")
        return fn

    def _create_processor_role(self) -> iam.Role:
        """
        Least-privilege IAM role for paper_processor.

        Grants:
          - S3: GetObject on raw bucket (PDF read back if needed)
                PutObject/GetObject on raw, parsed, cleaned buckets
          - DynamoDB: PutItem (conditional) on PaperMetadataTable
          - SQS: SendMessage on embedding + entity queues
          - Textract: StartDocumentAnalysis + GetDocumentAnalysis
          - Lambda: InvokeFunction on docling-parser
          - Bedrock: InvokeModel for Titan Embeddings V2
          - CloudWatch: PutMetricData
          - X-Ray: PutTraceSegments
          - VPC: CreateNetworkInterface (auto-added by Lambda VPC integration)
        """
        role = iam.Role(
            self,
            "PaperProcessorRole",
            role_name=f"{config.PROJECT_PREFIX}-paper-processor-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="paper_processor Lambda — full ingestion pipeline role",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                # VPC execution allows ENI creation in private subnets
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
            ],
        )

        # S3 — raw, parsed, cleaned buckets
        s3_bucket_arns = [
            self._storage.raw_papers_bucket.bucket_arn,
            f"{self._storage.raw_papers_bucket.bucket_arn}/*",
            self._storage.parsed_papers_bucket.bucket_arn,
            f"{self._storage.parsed_papers_bucket.bucket_arn}/*",
            self._storage.cleaned_papers_bucket.bucket_arn,
            f"{self._storage.cleaned_papers_bucket.bucket_arn}/*",
        ]
        role.add_to_policy(iam.PolicyStatement(
            sid="S3ReadWritePapers",
            effect=iam.Effect.ALLOW,
            actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                     "s3:GetObjectTagging", "s3:PutObjectTagging"],
            resources=s3_bucket_arns,
        ))

        # DynamoDB — write paper metadata (conditional put prevents races)
        role.add_to_policy(iam.PolicyStatement(
            sid="DynamoWritePaperMetadata",
            effect=iam.Effect.ALLOW,
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[self._storage.paper_metadata_table.table_arn],
        ))

        # SQS — send to embedding and entity queues
        role.add_to_policy(iam.PolicyStatement(
            sid="SqsSendDownstream",
            effect=iam.Effect.ALLOW,
            actions=["sqs:SendMessage", "sqs:SendMessageBatch",
                     "sqs:GetQueueAttributes"],
            resources=[
                self.embedding_queue.queue_arn,
                self.entity_queue.queue_arn,
            ],
        ))

        # Textract — async document analysis
        role.add_to_policy(iam.PolicyStatement(
            sid="TextractDocumentAnalysis",
            effect=iam.Effect.ALLOW,
            actions=[
                "textract:StartDocumentAnalysis",
                "textract:GetDocumentAnalysis",
                "textract:StartDocumentTextDetection",
                "textract:GetDocumentTextDetection",
            ],
            resources=["*"],   # Textract does not support resource-level restrictions
        ))

        # Lambda — invoke Docling parser (synchronous, for parsing)
        role.add_to_policy(iam.PolicyStatement(
            sid="LambdaInvokeDocling",
            effect=iam.Effect.ALLOW,
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{self.region}:{self.account}"
                f":function:{config.PROJECT_PREFIX}-docling-parser"
            ],
        ))

        # Bedrock — Titan Embeddings V2 (for late chunking)
        role.add_to_policy(iam.PolicyStatement(
            sid="BedrockInvokeEmbedding",
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/"
                f"{config.BEDROCK_EMBEDDING_MODEL}"
            ],
        ))

        # CloudWatch custom metrics
        role.add_to_policy(iam.PolicyStatement(
            sid="CwPutMetrics",
            effect=iam.Effect.ALLOW,
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={
                "StringEquals": {"cloudwatch:namespace": config.CW_NAMESPACE}
            },
        ))

        # X-Ray active tracing
        role.add_to_policy(iam.PolicyStatement(
            sid="XRayTracing",
            effect=iam.Effect.ALLOW,
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources=["*"],
        ))

        return role

    # =====================================================================
    # EventBridge Scheduler
    # =====================================================================

    def _create_fetch_schedule(self) -> None:
        """
        EventBridge rule that triggers paper_fetcher every 6 hours.

        Schedule: rate(6 hours)  (from config.FETCH_SCHEDULE_EXPRESSION)

        Uses a dedicated EventBridge IAM role that can only invoke the
        paper_fetcher Lambda — no wildcard resources.
        """
        # IAM role for EventBridge to invoke Lambda
        scheduler_role = iam.Role(
            self,
            "FetchSchedulerRole",
            role_name=f"{config.PROJECT_PREFIX}-fetch-scheduler-role",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            description="EventBridge scheduler → paper_fetcher Lambda",
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeFetcherLambda",
            effect=iam.Effect.ALLOW,
            actions=["lambda:InvokeFunction"],
            resources=[self.paper_fetcher_fn.function_arn],
        ))

        rule = events.Rule(
            self,
            "FetchScheduleRule",
            rule_name=f"{config.PROJECT_PREFIX}-fetch-schedule",
            description="Trigger paper_fetcher every 6 hours to poll arXiv",
            schedule=events.Schedule.expression(config.FETCH_SCHEDULE_EXPRESSION),
            enabled=True,
        )
        rule.add_target(
            targets.LambdaFunction(
                self.paper_fetcher_fn,
                # Pass a fixed payload so the fetcher knows it was scheduled
                event=events.RuleTargetInput.from_object({
                    "source": "scheduled",
                    "categories": config.ARXIV_CATEGORIES,
                    "lookback_hours": config.ARXIV_LOOKBACK_HOURS,
                    "max_results": config.ARXIV_MAX_RESULTS,
                }),
                retry_attempts=2,
            )
        )

        Tags.of(rule).add("Name", f"{config.PROJECT_PREFIX}-fetch-schedule")

    # =====================================================================
    # SQS Event Source Mappings
    # =====================================================================

    def _wire_event_sources(self) -> None:
        """
        Wire SQS → Lambda event sources.

        paper_queue  (FIFO)  → paper_processor
          batch_size=1: Each paper is processed independently so a single
          failure doesn't block others. FIFO doesn't support batch windows.

        Note: embedding_queue and entity_queue are wired in their
        respective stacks (EmbeddingStack, GraphStack) to keep each stack
        self-contained.
        """
        self.paper_processor_fn.add_event_source(
            event_sources.SqsEventSource(
                self.paper_queue,
                batch_size=1,                     # One paper per Lambda invocation
                report_batch_item_failures=True,  # Partial batch failure support
            )
        )

    # =====================================================================
    # CloudWatch Alarms
    # =====================================================================

    def _create_alarms(self) -> None:
        """
        CloudWatch alarms for the ingestion pipeline.

        Alarms:
          1. PaperDLQ depth > 10        → ops SNS topic
          2. EmbeddingDLQ depth > 10    → ops SNS topic
          3. EntityDLQ depth > 10       → ops SNS topic
          4. Fetcher errors > 5/hour    → ops SNS topic
          5. Processor errors > 5/hour  → ops SNS topic

        All alarms use a 5-minute evaluation period.
        """
        sns_action = cw_actions.SnsAction(self.ops_topic)

        def _dlq_alarm(alarm_id: str, queue: sqs.Queue, alarm_name: str) -> None:
            alarm = cloudwatch.Alarm(
                self,
                alarm_id,
                alarm_name=alarm_name,
                alarm_description=f"DLQ {queue.queue_name} has messages — check processor logs",
                metric=queue.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5),
                    statistic="Maximum",
                ),
                threshold=10,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(sns_action)
            alarm.add_ok_action(sns_action)

        _dlq_alarm("PaperDlqAlarm",     self.paper_dlq,     f"{config.PROJECT_PREFIX}-paper-dlq-depth")
        _dlq_alarm("EmbeddingDlqAlarm", self.embedding_dlq, f"{config.PROJECT_PREFIX}-embedding-dlq-depth")
        _dlq_alarm("EntityDlqAlarm",    self.entity_dlq,    f"{config.PROJECT_PREFIX}-entity-dlq-depth")

        def _lambda_error_alarm(alarm_id: str, fn: lambda_.Function, alarm_name: str) -> None:
            alarm = cloudwatch.Alarm(
                self,
                alarm_id,
                alarm_name=alarm_name,
                alarm_description=f"Lambda {fn.function_name} has elevated errors",
                metric=fn.metric_errors(
                    period=Duration.minutes(60),
                    statistic="Sum",
                ),
                threshold=5,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(sns_action)

        _lambda_error_alarm(
            "FetcherErrorAlarm",
            self.paper_fetcher_fn,
            f"{config.PROJECT_PREFIX}-fetcher-errors",
        )
        _lambda_error_alarm(
            "ProcessorErrorAlarm",
            self.paper_processor_fn,
            f"{config.PROJECT_PREFIX}-processor-errors",
        )

    # =====================================================================
    # CloudFormation Outputs
    # =====================================================================

    def _create_outputs(self) -> None:
        """Export key resource identifiers for downstream stacks and ops."""

        outputs = [
            ("PaperQueueUrl",     self.paper_queue.queue_url,       "SQS FIFO paper queue URL"),
            ("PaperQueueArn",     self.paper_queue.queue_arn,       "SQS FIFO paper queue ARN"),
            ("PaperDlqUrl",       self.paper_dlq.queue_url,         "SQS paper DLQ URL"),
            ("EmbeddingQueueUrl", self.embedding_queue.queue_url,   "SQS embedding queue URL"),
            ("EmbeddingQueueArn", self.embedding_queue.queue_arn,   "SQS embedding queue ARN"),
            ("EntityQueueUrl",    self.entity_queue.queue_url,      "SQS entity queue URL"),
            ("EntityQueueArn",    self.entity_queue.queue_arn,      "SQS entity queue ARN"),
            ("FetcherFnArn",      self.paper_fetcher_fn.function_arn,   "paper_fetcher Lambda ARN"),
            ("ProcessorFnArn",    self.paper_processor_fn.function_arn, "paper_processor Lambda ARN"),
            ("OpsTopicArn",       self.ops_topic.topic_arn,         "Ingestion ops SNS topic ARN"),
        ]

        for export_id, value, desc in outputs:
            CfnOutput(
                self,
                export_id,
                value=value,
                description=desc,
                export_name=f"IngestionStack-{export_id}",
            )
