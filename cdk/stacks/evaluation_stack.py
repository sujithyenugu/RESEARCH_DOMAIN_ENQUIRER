# evaluation_stack.py — Day 8: Evaluation Pipeline
"""
EvaluationStack — AWS CDK stack for the evaluation pipeline.

Responsibilities:
  - Lambda: Online Evaluator   (invoked per-query: latency, confidence, citation accuracy)
  - Lambda: Offline Evaluator  (nightly batch: Recall@K, MRR, Hit Rate, NDCG, Faithfulness)
  - EventBridge cron rule      (cron(0 2 * * ? *) → daily 02:00 UTC)
  - DynamoDB Table             (EvalHistory — trend tracking)
  - SNS Topic                  (evaluation regression alerts)
  - CloudWatch Dashboard       (ResearchRAG-Evaluation)
  - CloudWatch Alarms          (Recall@10, Faithfulness, P95 latency, Citation Accuracy)

Dependency order:
  StorageStack → IngestionStack → EmbeddingStack → GraphStack
  → RetrievalStack → GenerationStack → ApiStack → FrontendStack → EvaluationStack

Deploy:
  cdk deploy EvaluationStack

This file defines CDK constructs only — no AWS API calls are made here.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

from .config import (
    ALARM_CITATION_ACC_MIN,
    ALARM_CONFIDENCE_AVG_MIN,
    ALARM_E2E_P95_MAX_MS,
    ALARM_FAITHFULNESS_MIN,
    ALARM_RECALL_MIN,
    ALARM_REFUSAL_RATE_MAX,
    CW_NAMESPACE,
    DYNAMO_EVAL_HISTORY_TABLE,
    EVAL_SCHEDULE_EXPRESSION,
    GOLDEN_DATASET_COUNT,
    GOLDEN_DATASET_VERSION,
    LAMBDA_CONFIGS,
    PROJECT_PREFIX,
    S3_EVALUATION,
    SECRET_SLACK_WEBHOOK,
)

# ---------------------------------------------------------------------------
# Paths from CDK app root to each Lambda source directory
# ---------------------------------------------------------------------------
_LAMBDA_BASE       = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
_EVALUATOR_DIR     = os.path.join(_LAMBDA_BASE, "evaluator")


def _pip_bundling(runtime: lambda_.Runtime) -> cdk.BundlingOptions:
    """Standard pip-install bundling options for Python Lambda functions."""
    return cdk.BundlingOptions(
        image=runtime.bundling_image,
        command=[
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
        ],
    )


class EvaluationStack(Stack):
    """
    CDK Stack — Evaluation Pipeline.

    Constructs created:
      - aws_dynamodb.Table: EvalHistory
      - aws_sns.Topic: EvalAlertsTopic
      - aws_lambda.Function: online_evaluator
      - aws_lambda.Function: offline_evaluator
      - aws_events.Rule: nightly cron → Offline Evaluator
      - aws_cloudwatch.Dashboard: ResearchRAG-Evaluation
      - aws_cloudwatch.Alarm × 6 (retrieval, generation, latency alarms)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        storage_stack=None,      # type: ignore[type-arg]
        frontend_stack=None,     # type: ignore[type-arg]  # dependency wiring
        env: cdk.Environment,
        alert_email: str = "",   # optional: SNS email subscription
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        runtime = lambda_.Runtime.PYTHON_3_11

        # ------------------------------------------------------------------
        # DynamoDB: EvalHistory — trend tracking table
        # ------------------------------------------------------------------
        self.eval_history_table = dynamodb.Table(
            self,
            "EvalHistoryTable",
            table_name=DYNAMO_EVAL_HISTORY_TABLE,
            partition_key=dynamodb.Attribute(
                name="eval_date",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="eval_run_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # GSI: date-index — for trend queries sorted by eval_date
        self.eval_history_table.add_global_secondary_index(
            index_name="date-index",
            partition_key=dynamodb.Attribute(
                name="eval_date",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="created_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------
        # SNS: Alert topic — evaluation regressions + alarm notifications
        # ------------------------------------------------------------------
        self.alerts_topic = sns.Topic(
            self,
            "EvalAlertsTopic",
            topic_name=f"{PROJECT_PREFIX}-eval-alerts",
            display_name="Research RAG — Evaluation Alerts",
        )

        # Optionally subscribe an email address at deploy time
        if alert_email:
            self.alerts_topic.add_subscription(
                subs.EmailSubscription(alert_email)
            )

        # ------------------------------------------------------------------
        # IAM — shared policy building blocks
        # ------------------------------------------------------------------
        cloudwatch_policy = iam.PolicyStatement(
            sid="CloudWatchPutMetricData",
            effect=iam.Effect.ALLOW,
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
        )

        bedrock_haiku_policy = iam.PolicyStatement(
            sid="BedrockInvokeHaiku",
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/"
                "anthropic.claude-3-haiku-20240307-v1:0"
            ],
        )

        sns_publish_policy = iam.PolicyStatement(
            sid="SNSPublishAlerts",
            effect=iam.Effect.ALLOW,
            actions=["sns:Publish"],
            resources=[self.alerts_topic.topic_arn],
        )

        # ------------------------------------------------------------------
        # Lambda: Online Evaluator
        # Invoked per query — lightweight metrics only (latency, confidence,
        # citation accuracy, chunk count). Does NOT call Bedrock.
        # ------------------------------------------------------------------
        oe_cfg = LAMBDA_CONFIGS["online_evaluator"]

        oe_role = iam.Role(
            self,
            "OnlineEvaluatorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"{PROJECT_PREFIX} — Online Evaluator Lambda execution role",
        )
        oe_role.add_to_policy(cloudwatch_policy)
        oe_role.add_to_policy(sns_publish_policy)

        oe_log_group = logs.LogGroup(
            self,
            "OnlineEvaluatorLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-online-evaluator",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.online_evaluator = lambda_.Function(
            self,
            "OnlineEvaluator",
            function_name=f"{PROJECT_PREFIX}-online-evaluator",
            description=(
                "Per-query evaluation: latency, confidence score, "
                "citation accuracy, chunk count → CloudWatch metrics"
            ),
            runtime=runtime,
            handler="handler.online_handler",
            code=lambda_.Code.from_asset(
                _EVALUATOR_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=oe_role,
            memory_size=oe_cfg["memory_mb"],
            timeout=Duration.seconds(oe_cfg["timeout_seconds"]),
            environment={
                "CW_NAMESPACE":           CW_NAMESPACE,
                "SNS_ALERTS_TOPIC_ARN":   self.alerts_topic.topic_arn,
                "ALARM_CONFIDENCE_MIN":   str(ALARM_CONFIDENCE_AVG_MIN),
                "ALARM_CITATION_ACC_MIN": str(ALARM_CITATION_ACC_MIN),
                "LOG_LEVEL":              "INFO",
            },
            log_group=oe_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # Lambda: Offline Evaluator
        # Nightly batch — runs full golden dataset (100 Q&A pairs),
        # computes all retrieval + generation metrics, stores to S3 + DynamoDB.
        # ------------------------------------------------------------------
        off_cfg = LAMBDA_CONFIGS["offline_evaluator"]

        offline_role = iam.Role(
            self,
            "OfflineEvaluatorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"{PROJECT_PREFIX} — Offline Evaluator Lambda execution role",
        )
        offline_role.add_to_policy(cloudwatch_policy)
        offline_role.add_to_policy(bedrock_haiku_policy)
        offline_role.add_to_policy(sns_publish_policy)

        # Read golden dataset + write results to research-evaluation S3 bucket
        offline_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3EvalBucketReadWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket",
                ],
                resources=[
                    f"arn:aws:s3:::{S3_EVALUATION}",
                    f"arn:aws:s3:::{S3_EVALUATION}/*",
                ],
            )
        )

        # Read + write EvalHistory DynamoDB table
        self.eval_history_table.grant_read_write_data(offline_role)

        # Invoke query-handler Lambda to run the full RAG pipeline
        offline_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeQueryHandler",
                effect=iam.Effect.ALLOW,
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:"
                    f"{PROJECT_PREFIX}-query-handler",
                ],
            )
        )

        # Read Secrets Manager (Slack webhook for regression alerts)
        offline_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerRead",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:"
                    f"{SECRET_SLACK_WEBHOOK}*",
                ],
            )
        )

        offline_log_group = logs.LogGroup(
            self,
            "OfflineEvaluatorLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-offline-evaluator",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.offline_evaluator = lambda_.Function(
            self,
            "OfflineEvaluator",
            function_name=f"{PROJECT_PREFIX}-offline-evaluator",
            description=(
                "Nightly batch evaluation over golden dataset: "
                "Recall@K, MRR, Hit Rate, nDCG, Faithfulness, Groundedness"
            ),
            runtime=runtime,
            handler="handler.offline_handler",
            code=lambda_.Code.from_asset(
                _EVALUATOR_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=offline_role,
            memory_size=off_cfg["memory_mb"],
            timeout=Duration.seconds(off_cfg["timeout_seconds"]),
            environment={
                "CW_NAMESPACE":             CW_NAMESPACE,
                "SNS_ALERTS_TOPIC_ARN":     self.alerts_topic.topic_arn,
                "EVAL_S3_BUCKET":           S3_EVALUATION,
                "EVAL_HISTORY_TABLE":       DYNAMO_EVAL_HISTORY_TABLE,
                "QUERY_HANDLER_FUNCTION":   f"{PROJECT_PREFIX}-query-handler",
                "GOLDEN_DATASET_VERSION":   GOLDEN_DATASET_VERSION,
                "GOLDEN_DATASET_COUNT":     str(GOLDEN_DATASET_COUNT),
                "BEDROCK_VERIFY_MODEL":     "anthropic.claude-3-haiku-20240307-v1:0",
                "BEDROCK_REGION":           self.region,
                "ALARM_RECALL_MIN":         str(ALARM_RECALL_MIN),
                "ALARM_FAITHFULNESS_MIN":   str(ALARM_FAITHFULNESS_MIN),
                "ALARM_CITATION_ACC_MIN":   str(ALARM_CITATION_ACC_MIN),
                "ALARM_CONFIDENCE_MIN":     str(ALARM_CONFIDENCE_AVG_MIN),
                "ALARM_E2E_P95_MAX_MS":     str(ALARM_E2E_P95_MAX_MS),
                "LOG_LEVEL":                "INFO",
            },
            log_group=offline_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # EventBridge: nightly cron → Offline Evaluator (02:00 UTC daily)
        # ------------------------------------------------------------------
        self.eval_cron_rule = events.Rule(
            self,
            "NightlyEvalCron",
            rule_name=f"{PROJECT_PREFIX}-nightly-eval",
            description="Triggers the Offline Evaluator Lambda daily at 02:00 UTC",
            schedule=events.Schedule.expression(EVAL_SCHEDULE_EXPRESSION),
            enabled=True,
        )
        self.eval_cron_rule.add_target(
            targets.LambdaFunction(
                self.offline_evaluator,
                event=events.RuleTargetInput.from_object(
                    {
                        "dataset_version": GOLDEN_DATASET_VERSION,
                        "max_questions": GOLDEN_DATASET_COUNT,
                        "triggered_by": "EventBridge",
                    }
                ),
                retry_attempts=2,
            )
        )

        # ------------------------------------------------------------------
        # CloudWatch Alarms — evaluation regression gates
        # ------------------------------------------------------------------
        def _make_metric(
            metric_name: str,
            stat: str = "Average",
            period_minutes: int = 60,
        ) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace=CW_NAMESPACE,
                metric_name=metric_name,
                statistic=stat,
                period=Duration.minutes(period_minutes),
            )

        # Alarm 1: Recall@10 drops below 0.70 for 2 evaluation runs
        recall_alarm = cloudwatch.Alarm(
            self,
            "RecallAt10Alarm",
            alarm_name=f"{PROJECT_PREFIX}-recall-at-10-low",
            alarm_description=(
                "Recall@10 dropped below threshold — retrieval quality degraded"
            ),
            metric=_make_metric("EvalRecallAt10", period_minutes=1440),  # daily
            threshold=ALARM_RECALL_MIN,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        recall_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # Alarm 2: Faithfulness below 0.75 for daily eval
        faithfulness_alarm = cloudwatch.Alarm(
            self,
            "FaithfulnessAlarm",
            alarm_name=f"{PROJECT_PREFIX}-faithfulness-low",
            alarm_description="Answer faithfulness below minimum threshold",
            metric=_make_metric("EvalFaithfulness", period_minutes=1440),
            threshold=ALARM_FAITHFULNESS_MIN,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        faithfulness_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # Alarm 3: E2E P95 latency > 8s sustained for 5 minutes
        latency_alarm = cloudwatch.Alarm(
            self,
            "E2ELatencyP95Alarm",
            alarm_name=f"{PROJECT_PREFIX}-e2e-p95-latency-high",
            alarm_description="E2E P95 latency exceeded 8 seconds — SLO breach",
            metric=_make_metric("E2ELatencyMs", stat="p95", period_minutes=5),
            threshold=ALARM_E2E_P95_MAX_MS,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        latency_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # Alarm 4: Citation accuracy < 0.90 for daily eval
        citation_alarm = cloudwatch.Alarm(
            self,
            "CitationAccuracyAlarm",
            alarm_name=f"{PROJECT_PREFIX}-citation-accuracy-low",
            alarm_description="Citation accuracy dropped below 0.90 threshold",
            metric=_make_metric("EvalCitationAccuracy", period_minutes=1440),
            threshold=ALARM_CITATION_ACC_MIN,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        citation_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # Alarm 5: Confidence score average < 0.65 over rolling 1h
        confidence_alarm = cloudwatch.Alarm(
            self,
            "ConfidenceScoreAlarm",
            alarm_name=f"{PROJECT_PREFIX}-confidence-score-low",
            alarm_description="Rolling 1h average confidence score below 0.65",
            metric=_make_metric("ConfidenceScore", period_minutes=60),
            threshold=ALARM_CONFIDENCE_AVG_MIN,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        confidence_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # Alarm 6: Refusal rate > 15% over rolling 1h
        refusal_alarm = cloudwatch.Alarm(
            self,
            "RefusalRateAlarm",
            alarm_name=f"{PROJECT_PREFIX}-refusal-rate-high",
            alarm_description="Refusal rate exceeded 15% over rolling 1 hour",
            metric=_make_metric("RefusalRate", period_minutes=60),
            threshold=ALARM_REFUSAL_RATE_MAX,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        refusal_alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))

        # ------------------------------------------------------------------
        # CloudWatch Dashboard: ResearchRAG-Evaluation
        # ------------------------------------------------------------------
        self.dashboard = cloudwatch.Dashboard(
            self,
            "EvalDashboard",
            dashboard_name="ResearchRAG-Evaluation",
        )

        # Row 1 — Retrieval metrics (7-day trend)
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Recall@K (7-day trend)",
                left=[
                    _make_metric("EvalRecallAt5",  period_minutes=1440),
                    _make_metric("EvalRecallAt10", period_minutes=1440),
                ],
                width=8,
                period=Duration.days(7),
            ),
            cloudwatch.GraphWidget(
                title="MRR + Hit Rate@10 (7-day trend)",
                left=[
                    _make_metric("EvalMRR",         period_minutes=1440),
                    _make_metric("EvalHitRateAt10", period_minutes=1440),
                    _make_metric("EvalNDCGAt10",    period_minutes=1440),
                ],
                width=8,
                period=Duration.days(7),
            ),
            cloudwatch.AlarmStatusWidget(
                title="Retrieval Alarm Status",
                alarms=[recall_alarm, citation_alarm],
                width=8,
            ),
        )

        # Row 2 — Generation metrics (7-day trend)
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Faithfulness + Groundedness (7-day trend)",
                left=[
                    _make_metric("EvalFaithfulness",  period_minutes=1440),
                    _make_metric("EvalGroundedness",  period_minutes=1440),
                ],
                width=8,
                period=Duration.days(7),
            ),
            cloudwatch.GraphWidget(
                title="Citation Accuracy + Answer Relevance",
                left=[
                    _make_metric("EvalCitationAccuracy", period_minutes=1440),
                    _make_metric("EvalAnswerRelevance",  period_minutes=1440),
                ],
                width=8,
                period=Duration.days(7),
            ),
            cloudwatch.AlarmStatusWidget(
                title="Generation Alarm Status",
                alarms=[faithfulness_alarm, confidence_alarm, refusal_alarm],
                width=8,
            ),
        )

        # Row 3 — Latency metrics (real-time)
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="E2E Latency Percentiles (real-time)",
                left=[
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="E2ELatencyMs",
                        statistic="p50",
                        period=Duration.minutes(5),
                        label="P50",
                    ),
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="E2ELatencyMs",
                        statistic="p95",
                        period=Duration.minutes(5),
                        label="P95",
                    ),
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="E2ELatencyMs",
                        statistic="p99",
                        period=Duration.minutes(5),
                        label="P99",
                    ),
                ],
                width=12,
            ),
            cloudwatch.AlarmStatusWidget(
                title="Latency Alarm Status",
                alarms=[latency_alarm],
                width=6,
            ),
            cloudwatch.SingleValueWidget(
                title="Confidence Score (1h avg)",
                metrics=[_make_metric("ConfidenceScore", period_minutes=60)],
                width=6,
            ),
        )

        # Row 4 — Ingestion pipeline status
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Papers Ingested (daily)",
                left=[_make_metric("PapersIngested", period_minutes=1440)],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Query Volume + Refusal Rate",
                left=[
                    _make_metric("QueryCount",  period_minutes=60),
                    _make_metric("RefusalRate", period_minutes=60),
                ],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Hallucination Rate (rolling 1h)",
                left=[_make_metric("HallucinationRate", period_minutes=60)],
                width=8,
            ),
        )

        # ------------------------------------------------------------------
        # CloudFormation Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "OnlineEvaluatorFunctionName",
            value=self.online_evaluator.function_name,
            description="Online Evaluator Lambda function name",
            export_name=f"{PROJECT_PREFIX}-online-evaluator-name",
        )
        cdk.CfnOutput(
            self,
            "OfflineEvaluatorFunctionName",
            value=self.offline_evaluator.function_name,
            description="Offline Evaluator Lambda function name",
            export_name=f"{PROJECT_PREFIX}-offline-evaluator-name",
        )
        cdk.CfnOutput(
            self,
            "EvalHistoryTableName",
            value=self.eval_history_table.table_name,
            description="DynamoDB EvalHistory table name",
            export_name=f"{PROJECT_PREFIX}-eval-history-table",
        )
        cdk.CfnOutput(
            self,
            "EvalAlertsTopicArn",
            value=self.alerts_topic.topic_arn,
            description="SNS topic ARN for evaluation regression alerts",
            export_name=f"{PROJECT_PREFIX}-eval-alerts-arn",
        )
        cdk.CfnOutput(
            self,
            "EvalDashboardName",
            value=self.dashboard.dashboard_name,
            description="CloudWatch evaluation dashboard name",
            export_name=f"{PROJECT_PREFIX}-eval-dashboard",
        )
