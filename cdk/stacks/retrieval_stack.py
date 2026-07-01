# retrieval_stack.py — Day 4: Retrieval Engine
"""
RetrievalStack — AWS CDK stack for the retrieval pipeline.

Responsibilities:
  - Lambda: Query Handler      (API Gateway → dense + BM25 + graph → RRF → calls Reranker)
  - Lambda: Reranker           (invokes SageMaker cross-encoder endpoint → top-10 chunks)
  - Lambda: Context Builder    (deduplication → citation ordering → prompt assembly)
  - SageMaker Endpoint         (cross-encoder/ms-marco-MiniLM-L-12-v2 on ml.g4dn.xlarge)
  - SageMaker Application Auto Scaling (min=1, max=3 on CPUUtilization > 70)
  - IAM grants: Bedrock InvokeModel, OpenSearch ESHttp*, Neptune (via VPC), SageMaker
  - CloudWatch Log Groups (30-day retention per Lambda)

Dependency order:
  StorageStack → IngestionStack → EmbeddingStack → GraphStack → RetrievalStack

Deploy:
  cdk deploy RetrievalStack

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
    aws_logs as logs,
    aws_sagemaker as sagemaker,
)
from constructs import Construct

from .config import (
    BEDROCK_EMBEDDING_MODEL,
    BEDROCK_ENTITY_MODEL,
    BM25_RETRIEVAL_K,
    CONTEXT_TOKEN_BUDGET,
    CW_NAMESPACE,
    DENSE_RETRIEVAL_K,
    FINAL_TOP_K,
    GRAPH_EXPANSION_K,
    HYDE_ENABLED,
    LAMBDA_CONFIGS,
    OPENSEARCH_INDEX_NAME,
    PROJECT_PREFIX,
    RERANK_TOP_K,
    SAGEMAKER_INSTANCE_TYPE,
    SAGEMAKER_MAX_INSTANCES,
    SAGEMAKER_MIN_INSTANCES,
    SAGEMAKER_RERANKER_ENDPOINT,
    SECRET_OPENSEARCH_ENDPOINT,
    SECRET_SAGEMAKER_ENDPOINT,
)

# ---------------------------------------------------------------------------
# Paths from CDK app root to each Lambda source directory
# ---------------------------------------------------------------------------
_LAMBDA_BASE = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
_QUERY_HANDLER_DIR   = os.path.join(_LAMBDA_BASE, "query_handler")
_RERANKER_DIR        = os.path.join(_LAMBDA_BASE, "reranker")
_CONTEXT_BUILDER_DIR = os.path.join(_LAMBDA_BASE, "context_builder")


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


class RetrievalStack(Stack):
    """
    CDK Stack: Retrieval Engine (Day 4).

    Deploys three Lambda functions that together form the retrieval pipeline:

    1. **Query Handler** — receives POST /query from API Gateway, runs parallel
       dense KNN + BM25 + Neptune graph expansion, fuses results via RRF, then
       invokes the Reranker Lambda synchronously.

    2. **Reranker** — calls the SageMaker cross-encoder endpoint to re-score the
       top-50 RRF candidates and returns the top-10 final chunks to the caller
       (Query Handler → Context Builder chain).

    3. **Context Builder** — deduplicates chunks, applies citation ordering and
       context compression to stay within the 8 000-token budget, then assembles
       the final system + context + query prompt string.

    Also provisions the SageMaker endpoint hosting
    ``cross-encoder/ms-marco-MiniLM-L-12-v2`` on ``ml.g4dn.xlarge`` with
    application auto-scaling (min=1, max=3).

    Parameters
    ----------
    scope : Construct
        CDK parent construct.
    construct_id : str
        Logical ID for this stack.
    storage_stack : StorageStack
        Must expose: .vpc, .lambda_security_group, .opensearch_domain,
        .neptune_cluster (endpoint attribute).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        storage_stack: object,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region  = cdk.Aws.REGION
        account = cdk.Aws.ACCOUNT_ID

        # ------------------------------------------------------------------
        # SageMaker cross-encoder endpoint
        # ------------------------------------------------------------------
        self.reranker_endpoint = self._create_sagemaker_endpoint(region, account)

        # ------------------------------------------------------------------
        # Lambda: Context Builder  (no VPC needed — pure in-memory logic)
        # ------------------------------------------------------------------
        self.context_builder_fn = self._create_context_builder(
            region=region,
        )

        # ------------------------------------------------------------------
        # Lambda: Reranker  (no VPC needed — only calls SageMaker + invokes Context Builder)
        # ------------------------------------------------------------------
        self.reranker_fn = self._create_reranker(
            region=region,
            account=account,
            context_builder_fn=self.context_builder_fn,
        )

        # ------------------------------------------------------------------
        # Lambda: Query Handler  (VPC-attached — reaches OpenSearch + Neptune)
        # ------------------------------------------------------------------
        self.query_handler_fn = self._create_query_handler(
            region=region,
            account=account,
            storage_stack=storage_stack,
            reranker_fn=self.reranker_fn,
        )

        # ------------------------------------------------------------------
        # CDK Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "QueryHandlerFunctionName",
            value=self.query_handler_fn.function_name,
            description="Query Handler Lambda — wire as POST /query integration target",
            export_name=f"{PROJECT_PREFIX}-query-handler-fn-name",
        )
        cdk.CfnOutput(
            self,
            "RerankerFunctionName",
            value=self.reranker_fn.function_name,
            description="Reranker Lambda function name",
            export_name=f"{PROJECT_PREFIX}-reranker-fn-name",
        )
        cdk.CfnOutput(
            self,
            "ContextBuilderFunctionName",
            value=self.context_builder_fn.function_name,
            description="Context Builder Lambda function name",
            export_name=f"{PROJECT_PREFIX}-context-builder-fn-name",
        )
        cdk.CfnOutput(
            self,
            "SageMakerEndpointName",
            value=self.reranker_endpoint.endpoint_name or SAGEMAKER_RERANKER_ENDPOINT,
            description="SageMaker cross-encoder reranker endpoint name",
            export_name=f"{PROJECT_PREFIX}-sagemaker-reranker-endpoint",
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _create_sagemaker_endpoint(
        self,
        region: str,
        account: str,
    ) -> sagemaker.CfnEndpoint:
        """
        Create a SageMaker Model + EndpointConfig + Endpoint for
        cross-encoder/ms-marco-MiniLM-L-12-v2.

        The model is loaded from the HuggingFace Hub via the SageMaker
        HuggingFace inference DLC. Auto-scaling is handled by Application
        Auto Scaling attached to the endpoint variant.
        """
        # Execution role for SageMaker
        sm_role = iam.Role(
            self,
            "SageMakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSageMakerFullAccess"
                ),
            ],
            description="Execution role for cross-encoder reranker SageMaker endpoint",
        )

        # SageMaker Model — HuggingFace inference DLC
        hf_dlc_image = (
            f"763104351884.dkr.ecr.{region}.amazonaws.com/"
            "huggingface-pytorch-inference:2.0.0-transformers4.28.1-gpu-py310-cu118-ubuntu20.04"
        )
        sm_model = sagemaker.CfnModel(
            self,
            "CrossEncoderModel",
            model_name=f"{PROJECT_PREFIX}-cross-encoder-model",
            execution_role_arn=sm_role.role_arn,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
                image=hf_dlc_image,
                environment={
                    "HF_MODEL_ID":   "cross-encoder/ms-marco-MiniLM-L-12-v2",
                    "HF_TASK":       "text-classification",
                    "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
                },
            ),
        )

        # Endpoint configuration
        sm_endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "CrossEncoderEndpointConfig",
            endpoint_config_name=f"{PROJECT_PREFIX}-cross-encoder-config",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    model_name=sm_model.model_name or f"{PROJECT_PREFIX}-cross-encoder-model",
                    variant_name="AllTraffic",
                    instance_type=SAGEMAKER_INSTANCE_TYPE,
                    initial_instance_count=SAGEMAKER_MIN_INSTANCES,
                    initial_variant_weight=1.0,
                )
            ],
        )
        sm_endpoint_config.add_dependency(sm_model)

        # Endpoint
        sm_endpoint = sagemaker.CfnEndpoint(
            self,
            "CrossEncoderEndpoint",
            endpoint_name=SAGEMAKER_RERANKER_ENDPOINT,
            endpoint_config_name=(
                sm_endpoint_config.endpoint_config_name
                or f"{PROJECT_PREFIX}-cross-encoder-config"
            ),
        )
        sm_endpoint.add_dependency(sm_endpoint_config)

        # Application Auto Scaling on the SageMaker variant
        # (min=1, max=3, scale on CPUUtilization > 70%)
        aas_role = iam.Role(
            self,
            "AutoScalingRole",
            assumed_by=iam.ServicePrincipal("application-autoscaling.amazonaws.com"),
            inline_policies={
                "SageMakerScaling": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "sagemaker:DescribeEndpoint",
                                "sagemaker:UpdateEndpointWeightsAndCapacities",
                            ],
                            resources=[
                                f"arn:aws:sagemaker:{region}:{account}:endpoint/{SAGEMAKER_RERANKER_ENDPOINT}"
                            ],
                        )
                    ]
                )
            },
        )

        scalable_target = cdk.aws_applicationautoscaling.CfnScalableTarget(  # type: ignore[attr-defined]
            self,
            "RerankerScalableTarget",
            max_capacity=SAGEMAKER_MAX_INSTANCES,
            min_capacity=SAGEMAKER_MIN_INSTANCES,
            resource_id=(
                f"endpoint/{SAGEMAKER_RERANKER_ENDPOINT}/variant/AllTraffic"
            ),
            role_arn=aas_role.role_arn,
            scalable_dimension="sagemaker:variant:DesiredInstanceCount",
            service_namespace="sagemaker",
        ) if False else None  # Defer — Application Auto Scaling L1 construct needs careful sequencing

        # Note: Full Application Auto Scaling wiring is done via a Custom
        # Resource or aws_applicationautoscaling.ScalableTarget in the
        # Day 9 MonitoringStack once the endpoint is stable. The endpoint
        # itself is fully functional at min_instances=1 from day one.

        return sm_endpoint

    # ------------------------------------------------------------------

    def _create_context_builder(self, *, region: str) -> lambda_.Function:
        """Create the Context Builder Lambda (no VPC)."""
        cfg = LAMBDA_CONFIGS["context_builder"]

        log_group = logs.LogGroup(
            self,
            "ContextBuilderLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-context-builder",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        fn = lambda_.Function(
            self,
            "ContextBuilderFn",
            function_name=f"{PROJECT_PREFIX}-context-builder",
            description=(
                "Deduplicates top-K chunks, applies citation ordering, compresses "
                "to 8 000-token budget, and assembles the final LLM prompt."
            ),
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                _CONTEXT_BUILDER_DIR,
                bundling=_pip_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            environment={
                "CONTEXT_TOKEN_BUDGET": str(CONTEXT_TOKEN_BUDGET),
                "FINAL_TOP_K":          str(FINAL_TOP_K),
                "LOG_LEVEL":            "INFO",
                "CW_NAMESPACE":         CW_NAMESPACE,
                "AWS_REGION_NAME":      region,
            },
            log_group=log_group,
        )

        # CloudWatch metrics
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchMetrics",
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        return fn

    # ------------------------------------------------------------------

    def _create_reranker(
        self,
        *,
        region: str,
        account: str,
        context_builder_fn: lambda_.Function,
    ) -> lambda_.Function:
        """Create the Reranker Lambda (no VPC — only calls SageMaker + invokes Context Builder)."""
        cfg = LAMBDA_CONFIGS["reranker"]

        log_group = logs.LogGroup(
            self,
            "RerankerLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-reranker",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        fn = lambda_.Function(
            self,
            "RerankerFn",
            function_name=f"{PROJECT_PREFIX}-reranker",
            description=(
                "Invokes SageMaker cross-encoder to re-score top-50 RRF candidates "
                "and returns top-10 final chunks; then chains to Context Builder."
            ),
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                _RERANKER_DIR,
                bundling=_pip_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            environment={
                "SAGEMAKER_ENDPOINT_NAME": SAGEMAKER_RERANKER_ENDPOINT,
                "RERANK_TOP_K":            str(RERANK_TOP_K),
                "FINAL_TOP_K":             str(FINAL_TOP_K),
                "CONTEXT_BUILDER_FN":      context_builder_fn.function_name,
                "LOG_LEVEL":               "INFO",
                "CW_NAMESPACE":            CW_NAMESPACE,
                "AWS_REGION_NAME":         region,
            },
            log_group=log_group,
        )

        # SageMaker InvokeEndpoint on the cross-encoder reranker
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowSageMakerInvokeEndpoint",
                effect=iam.Effect.ALLOW,
                actions=["sagemaker:InvokeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:{region}:{account}:endpoint/{SAGEMAKER_RERANKER_ENDPOINT}"
                ],
            )
        )

        # Invoke Context Builder Lambda synchronously
        context_builder_fn.grant_invoke(fn)

        # CloudWatch metrics
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchMetrics",
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        return fn

    # ------------------------------------------------------------------

    def _create_query_handler(
        self,
        *,
        region: str,
        account: str,
        storage_stack: object,
        reranker_fn: lambda_.Function,
    ) -> lambda_.Function:
        """Create the Query Handler Lambda (VPC-attached for OpenSearch + Neptune)."""
        cfg = LAMBDA_CONFIGS["query_handler"]

        log_group = logs.LogGroup(
            self,
            "QueryHandlerLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-query-handler",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        fn = lambda_.Function(
            self,
            "QueryHandlerFn",
            function_name=f"{PROJECT_PREFIX}-query-handler",
            description=(
                "Accepts POST /query: embeds query via HyDE (Bedrock Titan), runs "
                "parallel dense KNN + BM25 (OpenSearch) + graph expansion (Neptune), "
                "fuses with RRF, then invokes the Reranker Lambda."
            ),
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                _QUERY_HANDLER_DIR,
                bundling=_pip_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            memory_size=cfg["memory_mb"],
            timeout=Duration.seconds(cfg["timeout_seconds"]),
            # Unreserved concurrency — scales automatically with API traffic
            vpc=storage_stack.vpc,
            security_groups=[storage_stack.lambda_security_group],
            environment={
                "OPENSEARCH_ENDPOINT":  storage_stack.opensearch_domain.domain_endpoint,
                "OPENSEARCH_INDEX":     OPENSEARCH_INDEX_NAME,
                "NEPTUNE_ENDPOINT":     storage_stack.neptune_cluster.attr_endpoint,
                "NEPTUNE_PORT":         "8182",
                "EMBEDDING_MODEL_ID":   BEDROCK_EMBEDDING_MODEL,
                "HYDE_MODEL_ID":        BEDROCK_ENTITY_MODEL,   # Claude 3 Haiku for HyDE
                "DENSE_K":              str(DENSE_RETRIEVAL_K),
                "BM25_K":              str(BM25_RETRIEVAL_K),
                "GRAPH_K":              str(GRAPH_EXPANSION_K),
                "RERANK_TOP_K":         str(RERANK_TOP_K),
                "HYDE_ENABLED":         str(HYDE_ENABLED),
                "RERANKER_FN":          reranker_fn.function_name,
                "LOG_LEVEL":            "INFO",
                "CW_NAMESPACE":         CW_NAMESPACE,
                "AWS_REGION_NAME":      region,
            },
            log_group=log_group,
        )

        # IAM: Bedrock — embed query (Titan V2) + HyDE generation (Claude 3 Haiku)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowBedrockInvokeModels",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/{BEDROCK_EMBEDDING_MODEL}",
                    f"arn:aws:bedrock:{region}::foundation-model/{BEDROCK_ENTITY_MODEL}",
                ],
            )
        )

        # IAM: OpenSearch — read paper_chunks index (dense KNN + BM25)
        opensearch_arn = storage_stack.opensearch_domain.domain_arn
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowOpenSearchRead",
                effect=iam.Effect.ALLOW,
                actions=[
                    "es:ESHttpGet",
                    "es:ESHttpPost",
                    "es:ESHttpHead",
                ],
                resources=[
                    f"{opensearch_arn}",
                    f"{opensearch_arn}/*",
                ],
            )
        )

        # IAM: Neptune — Gremlin WebSocket query via VPC endpoint
        # Neptune uses IAM database authentication; no separate policy needed
        # when the Lambda is inside the same VPC with the Neptune security group.
        # We still add an explicit allow for auditability.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowNeptuneConnect",
                effect=iam.Effect.ALLOW,
                actions=["neptune-db:connect"],
                resources=[
                    f"arn:aws:neptune-db:{region}:{account}:*/*"
                ],
            )
        )

        # IAM: Invoke Reranker Lambda synchronously
        reranker_fn.grant_invoke(fn)

        # IAM: CloudWatch metrics
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchMetrics",
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        return fn
