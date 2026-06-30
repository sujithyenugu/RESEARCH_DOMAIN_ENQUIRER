# embedding_stack.py — Day 3: Embedding Pipeline
"""
EmbeddingStack — AWS CDK stack for the vector embedding pipeline.

Responsibilities:
  - Lambda: Embedding Worker (SQS-triggered, VPC-attached)
  - SQS Event Source Mapping from IngestionStack's embedding_queue
  - IAM grants: Bedrock InvokeModel, OpenSearch ESHttp*, CloudWatch PutMetricData
  - CloudWatch Log Group (30-day retention)

Dependency order:
  StorageStack → IngestionStack → EmbeddingStack

Deploy:
  cdk deploy EmbeddingStack

This file defines CDK constructs only — no AWS API calls are made here.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
)
from constructs import Construct

from .config import (
    BEDROCK_EMBEDDING_MODEL,
    CW_NAMESPACE,
    LAMBDA_CONFIGS,
    OPENSEARCH_INDEX_NAME,
    PROJECT_PREFIX,
)

# Path from CDK app root to the lambda source directory
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambdas", "embedding_worker"
)


class EmbeddingStack(Stack):
    """
    CDK Stack: Embedding Pipeline (Day 3).

    Deploys the Lambda function that reads chunks from SQS, calls Bedrock
    Titan Embeddings V2 to (re-)embed zero-vector chunks, and bulk-indexes
    all chunks into the OpenSearch `paper_chunks` index.

    Parameters
    ----------
    scope : Construct
        CDK parent construct.
    construct_id : str
        Logical ID for this stack.
    storage_stack : StorageStack
        Must expose: .vpc, .lambda_security_group, .opensearch_domain
    ingestion_stack : IngestionStack
        Must expose: .embedding_queue (SQS Queue)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        storage_stack: object,
        ingestion_stack: object,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = LAMBDA_CONFIGS["embedding_worker"]
        region = cdk.Aws.REGION
        account = cdk.Aws.ACCOUNT_ID

        # ------------------------------------------------------------------
        # CloudWatch Log Group
        # ------------------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "EmbeddingWorkerLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-embedding-worker",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Lambda: Embedding Worker
        # ------------------------------------------------------------------
        self.embedding_worker_fn = lambda_.Function(
            self,
            "EmbeddingWorkerFn",
            function_name=f"{PROJECT_PREFIX}-embedding-worker",
            description="Reads chunks from SQS, embeds via Bedrock Titan V2, indexes to OpenSearch.",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                _LAMBDA_DIR,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            reserved_concurrent_executions=cfg["concurrency"],
            vpc=storage_stack.vpc,
            security_groups=[storage_stack.lambda_security_group],
            environment={
                "OPENSEARCH_ENDPOINT": storage_stack.opensearch_domain.domain_endpoint,
                "OPENSEARCH_INDEX":    OPENSEARCH_INDEX_NAME,
                "EMBEDDING_MODEL_ID":  BEDROCK_EMBEDDING_MODEL,
                "AWS_REGION":          region,
                "LOG_LEVEL":           "INFO",
                "CW_NAMESPACE":        CW_NAMESPACE,
            },
            log_group=log_group,
        )

        # ------------------------------------------------------------------
        # SQS Event Source — triggered by IngestionStack's embedding_queue
        # Batch size 10, 30s batching window so Lambda accumulates chunks
        # before invocation (reduces cold starts under bursty load).
        # ------------------------------------------------------------------
        self.embedding_worker_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                ingestion_stack.embedding_queue,
                batch_size=10,
                max_batching_window=Duration.seconds(30),
                report_batch_item_failures=True,  # partial failures → only failed msgs go to DLQ
            )
        )

        # ------------------------------------------------------------------
        # IAM: Bedrock — InvokeModel on Titan Embeddings V2
        # ------------------------------------------------------------------
        bedrock_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{BEDROCK_EMBEDDING_MODEL}"
        )
        self.embedding_worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowBedrockEmbedding",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[bedrock_model_arn],
            )
        )

        # ------------------------------------------------------------------
        # IAM: OpenSearch — read/write to paper_chunks index
        # ------------------------------------------------------------------
        opensearch_arn = storage_stack.opensearch_domain.domain_arn
        self.embedding_worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowOpenSearchReadWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    "es:ESHttpPost",
                    "es:ESHttpPut",
                    "es:ESHttpGet",
                    "es:ESHttpHead",
                ],
                resources=[
                    f"{opensearch_arn}",
                    f"{opensearch_arn}/*",
                ],
            )
        )

        # ------------------------------------------------------------------
        # IAM: CloudWatch — publish custom metrics
        # ------------------------------------------------------------------
        self.embedding_worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchMetrics",
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # ------------------------------------------------------------------
        # CDK Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "EmbeddingWorkerFunctionName",
            value=self.embedding_worker_fn.function_name,
            description="Name of the Embedding Worker Lambda function",
            export_name=f"{PROJECT_PREFIX}-embedding-worker-fn-name",
        )
