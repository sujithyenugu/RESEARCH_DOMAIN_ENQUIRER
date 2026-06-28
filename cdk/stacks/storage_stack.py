"""
storage_stack.py — Day 1: Storage Layer

Provisions ALL persistent infrastructure:
  ✅ VPC  — 3-AZ private subnets for Neptune, OpenSearch, Lambda + VPC endpoints
  ✅ S3   — 9 buckets with lifecycle rules, versioning, SSE-KMS, access logging
  ✅ DynamoDB — ResearchPaperMetadata table with 2 GSIs + EvalHistory table
  ✅ Amazon OpenSearch Service — 3-node KNN + BM25 cluster (private VPC)
  ✅ Amazon Neptune — Writer + Reader cluster (private VPC, Gremlin)
  ✅ AWS Secrets Manager — placeholder secrets for all credentials
  ✅ KMS Keys — per-service encryption keys

This stack must be deployed FIRST. All other stacks depend on its outputs.
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
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_neptune_alpha as neptune
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from stacks import config


class StorageStack(Stack):
    """
    Storage Layer — VPC, S3, DynamoDB, OpenSearch, Neptune.

    Exported properties (used by downstream stacks):
      self.vpc
      self.sg_lambda          — security group for all VPC-attached Lambdas
      self.sg_neptune         — security group for Neptune cluster
      self.sg_opensearch      — security group for OpenSearch domain
      self.raw_papers_bucket
      self.parsed_papers_bucket
      self.cleaned_papers_bucket
      self.embeddings_cache_bucket
      self.neptune_bulk_bucket
      self.evaluation_bucket
      self.frontend_bucket
      self.paper_metadata_table
      self.eval_history_table
      self.opensearch_domain
      self.neptune_cluster
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Global tags applied to every resource in this stack
        Tags.of(self).add("Project",     config.PROJECT_NAME)
        Tags.of(self).add("Stack",       "StorageStack")
        Tags.of(self).add("Environment", "production")
        Tags.of(self).add("ManagedBy",   "aws-cdk")

        # ---------------------------------------------------------------
        # 1. KMS Encryption Keys
        # ---------------------------------------------------------------
        self._s3_key        = self._create_kms_key("s3-key",        "S3 buckets encryption key")
        self._dynamo_key    = self._create_kms_key("dynamo-key",     "DynamoDB tables encryption key")
        self._neptune_key   = self._create_kms_key("neptune-key",    "Neptune cluster encryption key")
        self._opensearch_key = self._create_kms_key("opensearch-key","OpenSearch domain encryption key")
        self._secrets_key   = self._create_kms_key("secrets-key",    "Secrets Manager encryption key")

        # ---------------------------------------------------------------
        # 2. VPC
        # ---------------------------------------------------------------
        self.vpc = self._create_vpc()

        # ---------------------------------------------------------------
        # 3. Security Groups
        # ---------------------------------------------------------------
        self.sg_lambda      = self._create_sg_lambda()
        self.sg_neptune     = self._create_sg_neptune()
        self.sg_opensearch  = self._create_sg_opensearch()

        # ---------------------------------------------------------------
        # 4. VPC Endpoints (keep all traffic inside AWS network)
        # ---------------------------------------------------------------
        self._create_vpc_endpoints()

        # ---------------------------------------------------------------
        # 5. S3 Buckets
        # ---------------------------------------------------------------
        (
            self.raw_papers_bucket,
            self.parsed_papers_bucket,
            self.cleaned_papers_bucket,
            self.embeddings_cache_bucket,
            self.neptune_bulk_bucket,
            self.evaluation_bucket,
            self.frontend_bucket,
            self.logs_bucket,
            self.processing_logs_bucket,
        ) = self._create_s3_buckets()

        # ---------------------------------------------------------------
        # 6. DynamoDB Tables
        # ---------------------------------------------------------------
        self.paper_metadata_table = self._create_paper_metadata_table()
        self.eval_history_table   = self._create_eval_history_table()

        # ---------------------------------------------------------------
        # 7. Amazon OpenSearch Service
        # ---------------------------------------------------------------
        self.opensearch_domain = self._create_opensearch_domain()

        # ---------------------------------------------------------------
        # 8. Amazon Neptune
        # ---------------------------------------------------------------
        self.neptune_cluster = self._create_neptune_cluster()

        # ---------------------------------------------------------------
        # 9. Secrets Manager — placeholder secrets for all services
        # ---------------------------------------------------------------
        self._create_secrets()

        # ---------------------------------------------------------------
        # 10. CloudFormation Outputs
        # ---------------------------------------------------------------
        self._create_outputs()

    # ===================================================================
    # KMS
    # ===================================================================

    def _create_kms_key(self, key_id: str, description: str) -> kms.Key:
        """Create a customer-managed KMS key with 7-day pending deletion."""
        key = kms.Key(
            self,
            key_id,
            description=description,
            enable_key_rotation=True,           # Rotate annually (AWS best practice)
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.RETAIN,  # Never auto-delete KMS keys
        )
        cdk.Tags.of(key).add("Name", f"{config.PROJECT_PREFIX}/{key_id}")
        return key

    # ===================================================================
    # VPC
    # ===================================================================

    def _create_vpc(self) -> ec2.Vpc:
        """
        Create the main VPC with:
          - 3 private isolated subnets for Neptune (data tier)
          - 3 private isolated subnets for OpenSearch (data tier)
          - 3 private subnets with NAT for Lambda functions
          - NO public subnets — data stores have no internet access

        Matches INFRASTRUCTURE.md §VPC Design.
        """
        vpc = ec2.Vpc(
            self,
            "ResearchRagVpc",
            vpc_name=f"{config.PROJECT_PREFIX}-rag-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDR),
            max_azs=3,
            nat_gateways=1,             # One NAT for Lambda outbound calls (Bedrock etc.)
            enable_dns_hostnames=True,
            enable_dns_support=True,
            subnet_configuration=[
                # Lambda functions need outbound internet for non-VPC services
                ec2.SubnetConfiguration(
                    name="LambdaSubnet",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                # Neptune — no internet access
                ec2.SubnetConfiguration(
                    name="NeptuneSubnet",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
                # OpenSearch — no internet access
                ec2.SubnetConfiguration(
                    name="OpenSearchSubnet",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
                # Public subnet required for NAT Gateway only
                ec2.SubnetConfiguration(
                    name="PublicSubnet",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        Tags.of(vpc).add("Name", f"{config.PROJECT_PREFIX}-rag-vpc")
        return vpc

    # ===================================================================
    # Security Groups
    # ===================================================================

    def _create_sg_lambda(self) -> ec2.SecurityGroup:
        """
        Security group for all VPC-attached Lambda functions.
        Allows all outbound traffic (to VPC endpoints and NAT).
        """
        sg = ec2.SecurityGroup(
            self,
            "SgLambda",
            vpc=self.vpc,
            security_group_name=f"{config.PROJECT_PREFIX}-sg-lambda",
            description="Lambda functions — all outbound to VPC endpoints and NAT",
            allow_all_outbound=True,
        )
        Tags.of(sg).add("Name", f"{config.PROJECT_PREFIX}-sg-lambda")
        return sg

    def _create_sg_neptune(self) -> ec2.SecurityGroup:
        """
        Security group for Neptune cluster.
        Allows inbound on port 8182 from Lambda SG only.
        """
        sg = ec2.SecurityGroup(
            self,
            "SgNeptune",
            vpc=self.vpc,
            security_group_name=f"{config.PROJECT_PREFIX}-sg-neptune",
            description="Neptune cluster — Gremlin port 8182 from Lambda SG only",
            allow_all_outbound=False,
        )
        # Allow Gremlin WebSocket from Lambda security group
        sg.add_ingress_rule(
            peer=self.sg_lambda,
            connection=ec2.Port.tcp(config.NEPTUNE_PORT),
            description="Allow Gremlin (8182) from Lambda functions",
        )
        Tags.of(sg).add("Name", f"{config.PROJECT_PREFIX}-sg-neptune")
        return sg

    def _create_sg_opensearch(self) -> ec2.SecurityGroup:
        """
        Security group for OpenSearch domain.
        Allows inbound HTTPS (443) from Lambda SG only.
        """
        sg = ec2.SecurityGroup(
            self,
            "SgOpenSearch",
            vpc=self.vpc,
            security_group_name=f"{config.PROJECT_PREFIX}-sg-opensearch",
            description="OpenSearch domain — HTTPS 443 from Lambda SG only",
            allow_all_outbound=False,
        )
        # Allow HTTPS from Lambda security group
        sg.add_ingress_rule(
            peer=self.sg_lambda,
            connection=ec2.Port.tcp(config.OPENSEARCH_PORT),
            description="Allow HTTPS (443) from Lambda functions",
        )
        Tags.of(sg).add("Name", f"{config.PROJECT_PREFIX}-sg-opensearch")
        return sg

    # ===================================================================
    # VPC Endpoints
    # ===================================================================

    def _create_vpc_endpoints(self) -> None:
        """
        Create VPC endpoints so all AWS service calls stay within the
        AWS network — no data leaves the VPC.

        Gateway endpoints (free): S3, DynamoDB
        Interface endpoints (charged per AZ): Bedrock, SageMaker, Secrets Manager, SQS

        Matches INFRASTRUCTURE.md §VPC Design > Interface VPC Endpoints.
        """
        isolated_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        )
        lambda_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        )

        # ------ Gateway Endpoints (no cost, no SG required) ------

        # S3 Gateway endpoint
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # DynamoDB Gateway endpoint
        self.vpc.add_gateway_endpoint(
            "DynamoDBEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        # ------ Interface Endpoints (charged per AZ-hour) ------

        endpoint_sg = ec2.SecurityGroup(
            self,
            "SgVpcEndpoints",
            vpc=self.vpc,
            security_group_name=f"{config.PROJECT_PREFIX}-sg-endpoints",
            description="VPC interface endpoints — HTTPS from Lambda SG",
            allow_all_outbound=False,
        )
        endpoint_sg.add_ingress_rule(
            peer=self.sg_lambda,
            connection=ec2.Port.tcp(443),
            description="Allow HTTPS from Lambda SG to VPC endpoints",
        )

        # Bedrock Runtime — for InvokeModel API calls
        self.vpc.add_interface_endpoint(
            "BedrockRuntimeEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            subnets=ec2.SubnetSelection(subnets=lambda_subnets.subnets),
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # SageMaker Runtime — for cross-encoder InvokeEndpoint calls
        self.vpc.add_interface_endpoint(
            "SageMakerRuntimeEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
            subnets=ec2.SubnetSelection(subnets=lambda_subnets.subnets),
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # Secrets Manager — for credential retrieval at Lambda cold start
        self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets=ec2.SubnetSelection(subnets=lambda_subnets.subnets),
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # SQS — for Lambda triggers and SendMessage calls
        self.vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            subnets=ec2.SubnetSelection(subnets=lambda_subnets.subnets),
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

    # ===================================================================
    # S3 Buckets
    # ===================================================================

    def _create_s3_buckets(
        self,
    ) -> tuple[
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
        s3.Bucket,
    ]:
        """
        Create all 9 S3 buckets defined in INFRASTRUCTURE.md §S3 Buckets.

        All buckets share these security controls:
          - Block all public access
          - Server-side encryption (SSE-KMS with customer-managed key)
          - Enforce TLS-only access via bucket policy
          - Access logging → research-logs bucket (except the logs bucket itself)
          - CORS disabled (not a public-facing API)
        """

        # ---- Logging bucket (created first; other buckets log here) ----
        logs_bucket = self._make_bucket(
            bucket_id="LogsBucket",
            bucket_name=config.S3_LOGS,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireLogsAfter90Days",
                    enabled=True,
                    expiration=Duration.days(config.S3_LOGS_EXPIRY),
                )
            ],
            server_access_logs_bucket=None,  # Can't log-to-self
        )

        # ---- Raw PDFs from arXiv ----
        raw_papers_bucket = self._make_bucket(
            bucket_id="RawPapersBucket",
            bucket_name=config.S3_RAW_PAPERS,
            versioned=True,
            lifecycle_rules=[
                # Transition to Intelligent-Tiering after 30 days
                s3.LifecycleRule(
                    id="IntelligentTiering30d",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(config.S3_INTELLIGENT_TIERING_DAYS),
                        )
                    ],
                ),
                # Archive to Glacier after 365 days
                s3.LifecycleRule(
                    id="GlacierAfter365d",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(config.S3_GLACIER_DAYS),
                        )
                    ],
                ),
            ],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Docling/Textract parsed JSON ----
        parsed_papers_bucket = self._make_bucket(
            bucket_id="ParsedPapersBucket",
            bucket_name=config.S3_PARSED_PAPERS,
            versioned=True,
            lifecycle_rules=[],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Final cleaned text JSON ----
        cleaned_papers_bucket = self._make_bucket(
            bucket_id="CleanedPapersBucket",
            bucket_name=config.S3_CLEANED_PAPERS,
            versioned=True,
            lifecycle_rules=[],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Pre-computed embedding cache (short-lived) ----
        embeddings_cache_bucket = self._make_bucket(
            bucket_id="EmbeddingsCacheBucket",
            bucket_name=config.S3_EMBEDDINGS_CACHE,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireCacheAfter90Days",
                    enabled=True,
                    expiration=Duration.days(config.S3_EMBEDDINGS_CACHE_EXPIRY),
                )
            ],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Neptune bulk-load CSV files (short-lived) ----
        neptune_bulk_bucket = self._make_bucket(
            bucket_id="NeptuneBulkBucket",
            bucket_name=config.S3_NEPTUNE_BULK,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireBulkAfter7Days",
                    enabled=True,
                    expiration=Duration.days(config.S3_NEPTUNE_BULK_EXPIRY),
                )
            ],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Golden dataset + evaluation results ----
        evaluation_bucket = self._make_bucket(
            bucket_id="EvaluationBucket",
            bucket_name=config.S3_EVALUATION,
            versioned=True,
            lifecycle_rules=[],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- React SPA assets (private, CloudFront origin) ----
        frontend_bucket = self._make_bucket(
            bucket_id="FrontendBucket",
            bucket_name=config.S3_FRONTEND,
            versioned=True,
            lifecycle_rules=[],
            server_access_logs_bucket=logs_bucket,
        )

        # ---- Lambda processing audit logs ----
        processing_logs_bucket = self._make_bucket(
            bucket_id="ProcessingLogsBucket",
            bucket_name=config.S3_PROCESSING_LOGS,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireProcessingLogsAfter30Days",
                    enabled=True,
                    expiration=Duration.days(config.S3_PROCESSING_LOGS_EXPIRY),
                )
            ],
            server_access_logs_bucket=logs_bucket,
        )

        return (
            raw_papers_bucket,
            parsed_papers_bucket,
            cleaned_papers_bucket,
            embeddings_cache_bucket,
            neptune_bulk_bucket,
            evaluation_bucket,
            frontend_bucket,
            logs_bucket,
            processing_logs_bucket,
        )

    def _make_bucket(
        self,
        bucket_id: str,
        bucket_name: str,
        versioned: bool,
        lifecycle_rules: list[s3.LifecycleRule],
        server_access_logs_bucket: s3.Bucket | None,
    ) -> s3.Bucket:
        """
        Helper that creates an S3 bucket with the standard security baseline:
          - Block all public access (enforced)
          - SSE-KMS encryption with our shared S3 KMS key
          - Enforce SSL/TLS (deny HTTP requests)
          - Access logging (when server_access_logs_bucket is provided)
          - Versioning (when versioned=True)
          - RETAIN on destroy so production data is never auto-deleted
        """
        kwargs: dict = dict(
            bucket_name=bucket_name,
            versioned=versioned,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self._s3_key,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=lifecycle_rules or [],
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        )
        if server_access_logs_bucket is not None:
            kwargs["server_access_logs_bucket"] = server_access_logs_bucket
            kwargs["server_access_logs_prefix"] = f"{bucket_name}/"

        bucket = s3.Bucket(self, bucket_id, **kwargs)
        Tags.of(bucket).add("Name", bucket_name)
        return bucket

    # ===================================================================
    # DynamoDB
    # ===================================================================

    def _create_paper_metadata_table(self) -> dynamodb.Table:
        """
        ResearchPaperMetadata — primary metadata store for all ingested papers.

        Schema (matches INGESTION_PIPELINE.md §Stage 5):
          PK: paper_id (String)

          GSI-1  category-published-index
                 PK: category (String), SK: published (String)
                 Purpose: Query by category + date range

          GSI-2  status-index
                 PK: processing_status (String), SK: created_at (String)
                 Purpose: Monitor stuck/failed papers

        Billing: On-demand (PAX) — no provisioned capacity needed for variable load.
        TTL: None — papers are permanent records.
        """
        table = dynamodb.Table(
            self,
            "PaperMetadataTable",
            table_name=config.DYNAMO_PAPER_METADATA_TABLE,
            partition_key=dynamodb.Attribute(
                name="paper_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self._dynamo_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,  # Never auto-delete paper records
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,  # For EventBridge pipe
        )

        # GSI-1: category-published-index
        table.add_global_secondary_index(
            index_name=config.GSI_CATEGORY_PUBLISHED,
            partition_key=dynamodb.Attribute(
                name="category",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="published",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-2: status-index
        table.add_global_secondary_index(
            index_name=config.GSI_STATUS_CREATED,
            partition_key=dynamodb.Attribute(
                name="processing_status",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="created_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        Tags.of(table).add("Name", config.DYNAMO_PAPER_METADATA_TABLE)
        return table

    def _create_eval_history_table(self) -> dynamodb.Table:
        """
        EvalHistory — stores daily evaluation run results for trend analysis.

        Schema (matches EVALUATION_PIPELINE.md §DynamoDB Eval History):
          PK: eval_date (String)   e.g. "2024-01-15"
          SK: eval_run_id (String) e.g. "eval_2024-01-15-020000"

          GSI: date-index — sort by eval_date for trend queries
        """
        table = dynamodb.Table(
            self,
            "EvalHistoryTable",
            table_name=config.DYNAMO_EVAL_HISTORY_TABLE,
            partition_key=dynamodb.Attribute(
                name="eval_date",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="eval_run_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self._dynamo_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        Tags.of(table).add("Name", config.DYNAMO_EVAL_HISTORY_TABLE)
        return table

    # ===================================================================
    # Amazon OpenSearch Service
    # ===================================================================

    def _create_opensearch_domain(self) -> opensearch.Domain:
        """
        Amazon OpenSearch Service domain — dual-purpose:
          1. KNN vector index (1536-dim Titan Embeddings, HNSW)
          2. BM25 full-text index (english analyzer)

        Cluster specification (matches VECTOR_PIPELINE.md §Cluster Specs):
          - 3 dedicated master nodes: m6g.large.search
          - 3 data nodes: r6g.large.search, 500 GB gp3 EBS each → 1.5 TB raw
          - Zone awareness across all 3 AZs
          - Private VPC only (no public endpoint)
          - Encryption at rest (KMS) + in-transit (TLS 1.2+)
          - Fine-grained access control (IAM)
        """
        opensearch_subnets = self.vpc.select_subnets(
            subnet_group_name="OpenSearchSubnet"
        )

        domain = opensearch.Domain(
            self,
            "OpenSearchDomain",
            domain_name=config.OPENSEARCH_DOMAIN_NAME,
            version=opensearch.EngineVersion.open_search("2.11"),

            # ---- Cluster topology ----
            capacity=opensearch.CapacityConfig(
                master_nodes=config.OPENSEARCH_MASTER_COUNT,
                master_node_instance_type=config.OPENSEARCH_MASTER_TYPE,
                data_nodes=config.OPENSEARCH_DATA_COUNT,
                data_node_instance_type=config.OPENSEARCH_DATA_TYPE,
            ),

            # ---- EBS storage per data node ----
            ebs=opensearch.EbsOptions(
                enabled=True,
                volume_type=ec2.EbsDeviceVolumeType.GP3,
                volume_size=config.OPENSEARCH_VOLUME_SIZE_GB,
                iops=3000,          # Baseline gp3 IOPS
                throughput=125,     # Baseline gp3 throughput (MB/s)
            ),

            # ---- Zone awareness (3 AZs for HA) ----
            zone_awareness=opensearch.ZoneAwarenessConfig(
                enabled=True,
                availability_zone_count=3,
            ),

            # ---- Networking — private VPC, no public endpoint ----
            vpc=self.vpc,
            vpc_subnets=[
                ec2.SubnetSelection(
                    subnets=opensearch_subnets.subnets[:config.OPENSEARCH_DATA_COUNT]
                )
            ],
            security_groups=[self.sg_opensearch],

            # ---- Encryption ----
            encryption_at_rest=opensearch.EncryptionAtRestOptions(
                enabled=True,
                kms_key=self._opensearch_key,
            ),
            node_to_node_encryption=True,
            enforce_https=True,
            tls_security_policy=opensearch.TLSSecurityPolicy.TLS_1_2,

            # ---- Fine-grained access control ----
            fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                master_user_name="admin",
                # Password is auto-generated and stored in Secrets Manager
            ),

            # ---- Logging ----
            logging=opensearch.LoggingOptions(
                slow_search_log_enabled=True,
                app_log_enabled=True,
                slow_index_log_enabled=True,
            ),

            # ---- Removal policy — RETAIN so data survives CDK destroy ----
            removal_policy=RemovalPolicy.RETAIN,
        )

        Tags.of(domain).add("Name", config.OPENSEARCH_DOMAIN_NAME)
        return domain

    # ===================================================================
    # Amazon Neptune
    # ===================================================================

    def _create_neptune_cluster(self) -> neptune.DatabaseCluster:
        """
        Amazon Neptune cluster — knowledge graph store.

        Configuration (matches GRAPH_PIPELINE.md §Neptune Configuration):
          - Engine: Neptune 1.3
          - Writer: db.r6g.large (2 vCPU, 16 GB RAM)
          - Reader: db.r6g.large × 1
          - Multi-AZ: enabled
          - Backup window: 02:00–03:00 UTC, 7-day retention
          - Encryption: KMS (customer-managed)
          - VPC: private subnets only
          - Port 8182 (Gremlin WebSocket)
          - IAM auth (SigV4)
        """
        neptune_subnets = self.vpc.select_subnets(
            subnet_group_name="NeptuneSubnet"
        )

        # Subnet group spanning all AZs
        subnet_group = neptune.SubnetGroup(
            self,
            "NeptuneSubnetGroup",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=neptune_subnets.subnets),
            description="Neptune private subnets — no internet access",
        )

        # Parameter group for performance tuning
        param_group = neptune.ClusterParameterGroup(
            self,
            "NeptuneClusterParams",
            description="Research RAG Neptune cluster parameter group",
            parameters={
                # Enable audit logging for compliance
                "neptune_enable_audit_log": "1",
                # Increase query timeout for complex graph traversals (ms)
                "neptune_query_timeout": "120000",
            },
        )

        cluster = neptune.DatabaseCluster(
            self,
            "NeptuneCluster",
            vpc=self.vpc,
            instance_type=neptune.InstanceType.of(
                config.NEPTUNE_INSTANCE_TYPE
            ),
            instances=2,                           # 1 writer + 1 reader
            subnet_group=subnet_group,
            security_groups=[self.sg_neptune],
            cluster_parameter_group=param_group,

            # ---- Backup ----
            backup=neptune.BackupProps(
                retention=Duration.days(config.NEPTUNE_BACKUP_RETENTION),
                preferred_window=config.NEPTUNE_BACKUP_WINDOW,
            ),

            # ---- Encryption ----
            storage_encrypted=True,
            kms_key=self._neptune_key,

            # ---- IAM authentication (SigV4) — no password auth ----
            iam_authentication=True,

            # ---- Port ----
            port=config.NEPTUNE_PORT,

            # ---- Deletion protection on production ----
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        Tags.of(cluster).add("Name", config.NEPTUNE_CLUSTER_ID)
        return cluster

    # ===================================================================
    # Secrets Manager
    # ===================================================================

    def _create_secrets(self) -> None:
        """
        Create placeholder secrets in AWS Secrets Manager.
        Lambda functions read these at cold start and cache them.

        All secrets are encrypted with the shared secrets KMS key.
        Actual values must be populated manually (or via CI/CD) after deployment.

        Matches INFRASTRUCTURE.md §Secrets Manager.
        """

        # arXiv API key (if using Semantic Scholar endpoint)
        secretsmanager.Secret(
            self,
            "SecretArxivApiKey",
            secret_name=config.SECRET_ARXIV_API_KEY,
            description="arXiv / Semantic Scholar API key",
            encryption_key=self._secrets_key,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Neptune endpoint (writer + reader URLs)
        secretsmanager.Secret(
            self,
            "SecretNeptuneEndpoint",
            secret_name=config.SECRET_NEPTUNE_ENDPOINT,
            description="Neptune cluster writer and reader endpoints",
            encryption_key=self._secrets_key,
            secret_object_value={
                "writer": cdk.SecretValue.unsafe_plain_text(
                    self.neptune_cluster.cluster_endpoint.hostname
                ),
                "reader": cdk.SecretValue.unsafe_plain_text(
                    self.neptune_cluster.cluster_read_endpoint.hostname
                ),
            },
            removal_policy=RemovalPolicy.RETAIN,
        )

        # OpenSearch endpoint + admin credentials
        secretsmanager.Secret(
            self,
            "SecretOpenSearchEndpoint",
            secret_name=config.SECRET_OPENSEARCH_ENDPOINT,
            description="OpenSearch domain endpoint and admin credentials",
            encryption_key=self._secrets_key,
            removal_policy=RemovalPolicy.RETAIN,
            # Admin password is auto-generated by OpenSearch fine-grained access control
            # — populate this secret with the password after first deploy
        )

        # Slack incoming webhook for CloudWatch alarm notifications
        secretsmanager.Secret(
            self,
            "SecretSlackWebhook",
            secret_name=config.SECRET_SLACK_WEBHOOK,
            description="Slack incoming webhook URL for pipeline alerts",
            encryption_key=self._secrets_key,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # SageMaker cross-encoder endpoint name
        secretsmanager.Secret(
            self,
            "SecretSageMakerEndpoint",
            secret_name=config.SECRET_SAGEMAKER_ENDPOINT,
            description="SageMaker cross-encoder reranker endpoint name",
            encryption_key=self._secrets_key,
            secret_object_value={
                "endpoint_name": cdk.SecretValue.unsafe_plain_text(
                    config.SAGEMAKER_RERANKER_ENDPOINT
                )
            },
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ===================================================================
    # CloudFormation Outputs
    # ===================================================================

    def _create_outputs(self) -> None:
        """
        Export key resource identifiers so downstream stacks can import them
        without hard-coding ARNs.
        """

        # VPC
        CfnOutput(self, "VpcId",
                  value=self.vpc.vpc_id,
                  description="VPC ID",
                  export_name=f"{config.PROJECT_PREFIX}-vpc-id")

        CfnOutput(self, "SgLambdaId",
                  value=self.sg_lambda.security_group_id,
                  description="Lambda security group ID",
                  export_name=f"{config.PROJECT_PREFIX}-sg-lambda-id")

        # S3 Buckets
        CfnOutput(self, "RawPapersBucketName",
                  value=self.raw_papers_bucket.bucket_name,
                  description="S3 bucket — raw PDFs",
                  export_name=f"{config.PROJECT_PREFIX}-raw-papers-bucket")

        CfnOutput(self, "ParsedPapersBucketName",
                  value=self.parsed_papers_bucket.bucket_name,
                  description="S3 bucket — parsed papers",
                  export_name=f"{config.PROJECT_PREFIX}-parsed-papers-bucket")

        CfnOutput(self, "CleanedPapersBucketName",
                  value=self.cleaned_papers_bucket.bucket_name,
                  description="S3 bucket — cleaned papers",
                  export_name=f"{config.PROJECT_PREFIX}-cleaned-papers-bucket")

        CfnOutput(self, "EvaluationBucketName",
                  value=self.evaluation_bucket.bucket_name,
                  description="S3 bucket — evaluation results + golden dataset",
                  export_name=f"{config.PROJECT_PREFIX}-evaluation-bucket")

        CfnOutput(self, "FrontendBucketName",
                  value=self.frontend_bucket.bucket_name,
                  description="S3 bucket — React SPA assets (private)",
                  export_name=f"{config.PROJECT_PREFIX}-frontend-bucket")

        # DynamoDB
        CfnOutput(self, "PaperMetadataTableName",
                  value=self.paper_metadata_table.table_name,
                  description="DynamoDB — ResearchPaperMetadata table",
                  export_name=f"{config.PROJECT_PREFIX}-paper-metadata-table")

        CfnOutput(self, "PaperMetadataTableArn",
                  value=self.paper_metadata_table.table_arn,
                  description="DynamoDB — ResearchPaperMetadata table ARN",
                  export_name=f"{config.PROJECT_PREFIX}-paper-metadata-table-arn")

        CfnOutput(self, "EvalHistoryTableName",
                  value=self.eval_history_table.table_name,
                  description="DynamoDB — EvalHistory table",
                  export_name=f"{config.PROJECT_PREFIX}-eval-history-table")

        # OpenSearch
        CfnOutput(self, "OpenSearchDomainEndpoint",
                  value=self.opensearch_domain.domain_endpoint,
                  description="OpenSearch domain VPC endpoint",
                  export_name=f"{config.PROJECT_PREFIX}-opensearch-endpoint")

        CfnOutput(self, "OpenSearchDomainArn",
                  value=self.opensearch_domain.domain_arn,
                  description="OpenSearch domain ARN",
                  export_name=f"{config.PROJECT_PREFIX}-opensearch-arn")

        # Neptune
        CfnOutput(self, "NeptuneClusterEndpoint",
                  value=self.neptune_cluster.cluster_endpoint.hostname,
                  description="Neptune writer endpoint",
                  export_name=f"{config.PROJECT_PREFIX}-neptune-writer")

        CfnOutput(self, "NeptuneClusterReadEndpoint",
                  value=self.neptune_cluster.cluster_read_endpoint.hostname,
                  description="Neptune reader endpoint",
                  export_name=f"{config.PROJECT_PREFIX}-neptune-reader")

        CfnOutput(self, "NeptuneClusterArn",
                  value=self.neptune_cluster.cluster_resource_identifier,
                  description="Neptune cluster resource identifier",
                  export_name=f"{config.PROJECT_PREFIX}-neptune-cluster-id")
