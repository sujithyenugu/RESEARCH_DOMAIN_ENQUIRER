# graph_stack.py — Day 3: Graph Pipeline
"""
GraphStack — AWS CDK stack for the knowledge graph building pipeline.

Responsibilities:
  - Lambda: Graph Builder (SQS-triggered, VPC-attached)
  - SQS Event Source Mapping from IngestionStack's entity_queue
  - IAM grants: Bedrock InvokeModel (Claude Haiku), Neptune read/write, CloudWatch
  - CloudWatch Log Group (30-day retention)

Dependency order:
  StorageStack → IngestionStack → GraphStack

Deploy:
  cdk deploy GraphStack

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
    BEDROCK_ENTITY_MODEL,
    CW_NAMESPACE,
    LAMBDA_CONFIGS,
    NEPTUNE_PORT,
    PROJECT_PREFIX,
)

# Path from CDK app root to the lambda source directory
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambdas", "graph_builder"
)


class GraphStack(Stack):
    """
    CDK Stack: Graph Pipeline (Day 3).

    Deploys the Lambda function that reads paper entity data from SQS,
    calls Bedrock Claude 3 Haiku to extract entities and relationships,
    and upserts vertices + edges into the Amazon Neptune knowledge graph
    using idempotent Gremlin queries.

    Parameters
    ----------
    scope : Construct
        CDK parent construct.
    construct_id : str
        Logical ID for this stack.
    storage_stack : StorageStack
        Must expose: .vpc, .lambda_security_group, .neptune_cluster
    ingestion_stack : IngestionStack
        Must expose: .entity_queue (SQS Queue)
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

        cfg = LAMBDA_CONFIGS["graph_builder"]
        region = cdk.Aws.REGION
        account = cdk.Aws.ACCOUNT_ID

        # ------------------------------------------------------------------
        # CloudWatch Log Group
        # ------------------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "GraphBuilderLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-graph-builder",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Lambda: Graph Builder
        # ------------------------------------------------------------------
        self.graph_builder_fn = lambda_.Function(
            self,
            "GraphBuilderFn",
            function_name=f"{PROJECT_PREFIX}-graph-builder",
            description=(
                "Reads entity extraction requests from SQS, calls Claude Haiku "
                "to extract entities and relationships, upserts into Neptune."
            ),
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
                "NEPTUNE_ENDPOINT": storage_stack.neptune_cluster.cluster_endpoint.hostname,
                "NEPTUNE_PORT":     str(NEPTUNE_PORT),
                "ENTITY_MODEL_ID":  BEDROCK_ENTITY_MODEL,
                "AWS_REGION":       region,
                "LOG_LEVEL":        "INFO",
                "CW_NAMESPACE":     CW_NAMESPACE,
            },
            log_group=log_group,
        )

        # ------------------------------------------------------------------
        # SQS Event Source — triggered by IngestionStack's entity_queue
        # Batch size 5, 60s batching window.
        # Entity extraction is more expensive (LLM call per paper),
        # so a larger batching window reduces Lambda cold starts.
        # ------------------------------------------------------------------
        self.graph_builder_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                ingestion_stack.entity_queue,
                batch_size=5,
                max_batching_window=Duration.seconds(60),
                report_batch_item_failures=True,
            )
        )

        # ------------------------------------------------------------------
        # IAM: Bedrock — InvokeModel on Claude 3 Haiku (entity extraction)
        # ------------------------------------------------------------------
        bedrock_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{BEDROCK_ENTITY_MODEL}"
        )
        self.graph_builder_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowBedrockEntityExtraction",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[bedrock_model_arn],
            )
        )

        # ------------------------------------------------------------------
        # IAM: Neptune — connect, read, and write via Gremlin/IAM auth
        # Neptune uses IAM database authentication; the Lambda role needs
        # neptune-db:* permissions on the cluster resource ARN.
        # ------------------------------------------------------------------
        neptune_resource_arn = (
            f"arn:aws:neptune-db:{region}:{account}:"
            f"{storage_stack.neptune_cluster.cluster_resource_identifier}/*"
        )
        self.graph_builder_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowNeptuneReadWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:connect",
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:WriteDataViaQuery",
                    "neptune-db:DeleteDataViaQuery",
                    "neptune-db:GetEngineStatus",
                ],
                resources=[neptune_resource_arn],
            )
        )

        # ------------------------------------------------------------------
        # IAM: CloudWatch — publish custom metrics
        # ------------------------------------------------------------------
        self.graph_builder_fn.add_to_role_policy(
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
            "GraphBuilderFunctionName",
            value=self.graph_builder_fn.function_name,
            description="Name of the Graph Builder Lambda function",
            export_name=f"{PROJECT_PREFIX}-graph-builder-fn-name",
        )
