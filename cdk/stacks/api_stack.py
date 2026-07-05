# api_stack.py — Day 6: API Layer
"""
ApiStack — AWS CDK stack for the API layer.

Responsibilities:
  REST API (API Gateway v1):
    POST /query                 → Query Handler Lambda (full pipeline invoke)
    GET  /papers/{id}           → Response API Lambda (DynamoDB paper lookup)
    GET  /papers/{id}/chunks    → Response API Lambda (OpenSearch chunk lookup)
    GET  /graph/entity/{name}   → Response API Lambda (Neptune Gremlin entity)
    GET  /graph/citation/{id}   → Response API Lambda (Neptune citation subgraph)
    POST /evaluate              → Response API Lambda (invoke Evaluator — stub)

  WebSocket API (API Gateway v2):
    $connect    → WebSocket Handler Lambda
    $disconnect → WebSocket Handler Lambda
    sendmessage → WebSocket Handler Lambda

  Supporting resources:
    DynamoDB table  — WS connection registry (TTL-enabled)
    IAM roles       — least-privilege per Lambda
    CloudWatch log groups (30-day retention)
    CORS            — allow all origins (restrict in production)
    Throttling      — 100 rps burst / 50 rps steady per stage
    Usage Plan + API Key (optional enforcement)
    CFN Outputs     — REST URL, WebSocket URL, API key

Dependency order:
  StorageStack → ... → GenerationStack → ApiStack

Deploy:
    cdk deploy ApiStack

This file defines CDK constructs only — no AWS API calls are made here.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_int,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

from .config import (
    API_STAGE_NAME,
    CW_NAMESPACE,
    DYNAMO_PAPER_METADATA_TABLE,
    LAMBDA_CONFIGS,
    PROJECT_PREFIX,
    SECRET_OPENSEARCH_ENDPOINT,
)

# ---------------------------------------------------------------------------
# Paths from CDK app root to each Lambda source directory
# ---------------------------------------------------------------------------
_LAMBDA_BASE          = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
_RESPONSE_API_DIR     = os.path.join(_LAMBDA_BASE, "response_api")
_WEBSOCKET_HANDLER_DIR = os.path.join(_LAMBDA_BASE, "websocket_handler")

# DynamoDB table name for WebSocket connection registry
_WS_TABLE_NAME = f"{PROJECT_PREFIX}-ws-connections"

# WebSocket route keys
_WS_ROUTE_CONNECT    = "$connect"
_WS_ROUTE_DISCONNECT = "$disconnect"
_WS_ROUTE_SEND       = "sendmessage"


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


class ApiStack(Stack):
    """
    CDK Stack — REST + WebSocket API layer.

    Constructs created:
      aws_dynamodb.Table          — WebSocket connection registry
      aws_lambda.Function         — response_api (REST routes)
      aws_lambda.Function         — websocket_handler
      aws_apigateway.RestApi      — REST API with 6 routes
      aws_apigatewayv2.WebSocketApi — WebSocket API (3 routes)
      aws_apigatewayv2.WebSocketStage
      aws_logs.LogGroup × 2
      aws_iam.Role × 2

    Public attributes:
      self.rest_api                — apigw.RestApi
      self.websocket_api           — apigwv2.WebSocketApi
      self.websocket_stage         — apigwv2.WebSocketStage
      self.response_api_fn         — lambda_.Function
      self.websocket_handler_fn    — lambda_.Function
      self.ws_connections_table    — dynamodb.Table
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        generation_stack=None,   # type: ignore[type-arg]
        storage_stack=None,      # type: ignore[type-arg]
        env: cdk.Environment,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        runtime = lambda_.Runtime.PYTHON_3_11

        # ------------------------------------------------------------------
        # 1. DynamoDB — WebSocket connection registry
        # ------------------------------------------------------------------
        self.ws_connections_table = dynamodb.Table(
            self,
            "WSConnectionsTable",
            table_name=_WS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="connection_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # ------------------------------------------------------------------
        # 2. IAM — CloudWatch shared policy
        # ------------------------------------------------------------------
        cloudwatch_policy = iam.PolicyStatement(
            sid="CloudWatchPutMetricData",
            effect=iam.Effect.ALLOW,
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
        )

        # ------------------------------------------------------------------
        # 3. Lambda: Response API
        # ------------------------------------------------------------------
        ra_cfg = LAMBDA_CONFIGS["response_api"]

        ra_role = iam.Role(
            self,
            "ResponseApiRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"{PROJECT_PREFIX} — Response API Lambda execution role",
        )
        ra_role.add_to_policy(cloudwatch_policy)

        # Allow reading paper metadata from DynamoDB (storage stack table)
        ra_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDBReadPaperMetadata",
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMO_PAPER_METADATA_TABLE}",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMO_PAPER_METADATA_TABLE}/index/*",
                ],
            )
        )

        # Allow reading secrets (OpenSearch endpoint)
        ra_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerReadOpenSearch",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{SECRET_OPENSEARCH_ENDPOINT}*",
                ],
            )
        )

        # Allow ES HTTP calls to OpenSearch (for chunk lookup)
        ra_role.add_to_policy(
            iam.PolicyStatement(
                sid="OpenSearchESHttp",
                effect=iam.Effect.ALLOW,
                actions=["es:ESHttpGet", "es:ESHttpPost"],
                resources=[
                    f"arn:aws:es:{self.region}:{self.account}:domain/{PROJECT_PREFIX}-opensearch/*",
                ],
            )
        )

        ra_log_group = logs.LogGroup(
            self,
            "ResponseApiLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-response-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Resolve the Answer Generator ARN from generation_stack if available
        answer_gen_fn_name = (
            generation_stack.answer_generator.function_name
            if generation_stack is not None
            else f"{PROJECT_PREFIX}-answer-generator"
        )

        self.response_api_fn = lambda_.Function(
            self,
            "ResponseApi",
            function_name=f"{PROJECT_PREFIX}-response-api",
            description=(
                "Formats the final answer + citations + confidence + metadata "
                "into a standardised API Gateway response envelope"
            ),
            runtime=runtime,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                _RESPONSE_API_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=ra_role,
            memory_size=ra_cfg["memory_mb"],
            timeout=Duration.seconds(ra_cfg["timeout_seconds"]),
            environment={
                "API_VERSION":      "1.0",
                "CW_NAMESPACE":     CW_NAMESPACE,
                "SERVICE_NAME":     "response-api",
                "ANSWER_GEN_FN":    answer_gen_fn_name,
                "LOG_LEVEL":        "INFO",
            },
            log_group=ra_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # 4. Lambda: WebSocket Handler
        # ------------------------------------------------------------------
        wsh_role = iam.Role(
            self,
            "WebSocketHandlerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"{PROJECT_PREFIX} — WebSocket Handler Lambda execution role",
        )
        wsh_role.add_to_policy(cloudwatch_policy)

        # DynamoDB access for connection registry
        self.ws_connections_table.grant_read_write_data(wsh_role)

        # API Gateway Management API — post_to_connection
        wsh_role.add_to_policy(
            iam.PolicyStatement(
                sid="ApiGatewayManagementApiPostToConnection",
                effect=iam.Effect.ALLOW,
                actions=["execute-api:ManageConnections"],
                resources=[
                    f"arn:aws:execute-api:{self.region}:{self.account}:*/{API_STAGE_NAME}/*",
                ],
            )
        )

        wsh_log_group = logs.LogGroup(
            self,
            "WebSocketHandlerLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-websocket-handler",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.websocket_handler_fn = lambda_.Function(
            self,
            "WebSocketHandler",
            function_name=f"{PROJECT_PREFIX}-websocket-handler",
            description=(
                "Manages WebSocket lifecycle ($connect/$disconnect) and pushes "
                "streamed answer tokens to connected clients via APIGW Management API"
            ),
            runtime=runtime,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                _WEBSOCKET_HANDLER_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=wsh_role,
            memory_size=256,
            timeout=Duration.seconds(29),   # API Gateway WebSocket max
            environment={
                "WS_CONNECTIONS_TABLE": _WS_TABLE_NAME,
                "CW_NAMESPACE":         CW_NAMESPACE,
                "CONNECTION_TTL_SECS":  "3600",
                "LOG_LEVEL":            "INFO",
            },
            log_group=wsh_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # 5. REST API — API Gateway v1
        # ------------------------------------------------------------------
        rest_api_log_group = logs.LogGroup(
            self,
            "RestApiAccessLogGroup",
            log_group_name=f"/aws/apigateway/{PROJECT_PREFIX}-rest-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.rest_api = apigw.RestApi(
            self,
            "ResearchRestApi",
            rest_api_name=f"{PROJECT_PREFIX}-api",
            description=(
                "Research Domain Enquirer — REST API: "
                "query, paper lookup, chunk retrieval, graph exploration"
            ),
            deploy_options=apigw.StageOptions(
                stage_name=API_STAGE_NAME,
                throttling_burst_limit=100,
                throttling_rate_limit=50,
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
                metrics_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(rest_api_log_group),
                access_log_format=apigw.AccessLogFormat.clf(),
                tracing_enabled=True,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "X-Amz-Date", "Authorization", "X-Api-Key"],
            ),
        )

        # Shared Lambda integration for the Response API
        response_api_integration = apigw.LambdaIntegration(
            self.response_api_fn,
            proxy=True,
            allow_test_invoke=True,
        )

        # Wire in the Query Handler Lambda if available from generation_stack
        if generation_stack is not None:
            query_handler_fn = getattr(generation_stack, "query_handler_fn", None)
        else:
            query_handler_fn = None

        # ------------------------------------------------------------------
        # Route: POST /query
        # ------------------------------------------------------------------
        query_resource = self.rest_api.root.add_resource("query")
        # If Query Handler Lambda is available, wire directly; otherwise use Response API
        query_integration = (
            apigw.LambdaIntegration(query_handler_fn, proxy=True, allow_test_invoke=True)
            if query_handler_fn is not None
            else response_api_integration
        )
        query_resource.add_method(
            "POST",
            query_integration,
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="422"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # ------------------------------------------------------------------
        # Route: GET /papers/{id}
        # ------------------------------------------------------------------
        papers_resource     = self.rest_api.root.add_resource("papers")
        paper_id_resource   = papers_resource.add_resource("{id}")
        paper_id_resource.add_method("GET", response_api_integration)

        # ------------------------------------------------------------------
        # Route: GET /papers/{id}/chunks
        # ------------------------------------------------------------------
        chunks_resource = paper_id_resource.add_resource("chunks")
        chunks_resource.add_method("GET", response_api_integration)

        # ------------------------------------------------------------------
        # Route: GET /graph/entity/{name}  and  GET /graph/citation/{id}
        # ------------------------------------------------------------------
        graph_resource    = self.rest_api.root.add_resource("graph")
        entity_resource   = graph_resource.add_resource("entity")
        entity_name_res   = entity_resource.add_resource("{name}")
        entity_name_res.add_method("GET", response_api_integration)

        citation_resource = graph_resource.add_resource("citation")
        citation_id_res   = citation_resource.add_resource("{id}")
        citation_id_res.add_method("GET", response_api_integration)

        # ------------------------------------------------------------------
        # Route: POST /evaluate
        # ------------------------------------------------------------------
        evaluate_resource = self.rest_api.root.add_resource("evaluate")
        evaluate_resource.add_method("POST", response_api_integration)

        # ------------------------------------------------------------------
        # 6. API Key + Usage Plan (optional enforcement)
        # ------------------------------------------------------------------
        api_key = self.rest_api.add_api_key(
            "ResearchApiKey",
            api_key_name=f"{PROJECT_PREFIX}-api-key",
            description="API key for Research Domain Enquirer REST API",
        )

        usage_plan = self.rest_api.add_usage_plan(
            "ResearchUsagePlan",
            name=f"{PROJECT_PREFIX}-usage-plan",
            throttle=apigw.ThrottleSettings(
                burst_limit=200,
                rate_limit=100,
            ),
            quota=apigw.QuotaSettings(
                limit=10_000,
                period=apigw.Period.DAY,
            ),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(
            api=self.rest_api,
            stage=self.rest_api.deployment_stage,
        )

        # ------------------------------------------------------------------
        # 7. WebSocket API — API Gateway v2
        # ------------------------------------------------------------------
        ws_lambda_integration = apigwv2_int.WebSocketLambdaIntegration(
            "WsLambdaIntegration",
            self.websocket_handler_fn,
        )

        self.websocket_api = apigwv2.WebSocketApi(
            self,
            "ResearchWebSocketApi",
            api_name=f"{PROJECT_PREFIX}-ws-api",
            description=(
                "Research Domain Enquirer — WebSocket API for real-time "
                "streaming answer token delivery"
            ),
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=ws_lambda_integration,
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=ws_lambda_integration,
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=ws_lambda_integration,
            ),
        )

        # Add sendmessage route explicitly
        self.websocket_api.add_route(
            _WS_ROUTE_SEND,
            integration=ws_lambda_integration,
        )

        self.websocket_stage = apigwv2.WebSocketStage(
            self,
            "WebSocketProdStage",
            web_socket_api=self.websocket_api,
            stage_name=API_STAGE_NAME,
            auto_deploy=True,
        )

        # Grant WebSocket stage permission to invoke handler
        self.websocket_api.grant_manage_connections(wsh_role)

        # ------------------------------------------------------------------
        # 8. Grant REST API execution permissions (allow APIGW to invoke Lambda)
        # ------------------------------------------------------------------
        self.response_api_fn.add_permission(
            "AllowApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=self.rest_api.arn_for_execute_api(),
        )

        self.websocket_handler_fn.add_permission(
            "AllowWebSocketApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=(
                f"arn:aws:execute-api:{self.region}:{self.account}:"
                f"{self.websocket_api.api_id}/*"
            ),
        )

        # ------------------------------------------------------------------
        # 9. CloudFormation Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "RestApiUrl",
            value=self.rest_api.url,
            description="Research REST API base URL",
            export_name=f"{PROJECT_PREFIX}-rest-api-url",
        )
        cdk.CfnOutput(
            self,
            "RestApiId",
            value=self.rest_api.rest_api_id,
            description="Research REST API ID",
            export_name=f"{PROJECT_PREFIX}-rest-api-id",
        )
        cdk.CfnOutput(
            self,
            "WebSocketApiUrl",
            value=self.websocket_stage.url,
            description="Research WebSocket API URL (wss://...)",
            export_name=f"{PROJECT_PREFIX}-ws-api-url",
        )
        cdk.CfnOutput(
            self,
            "WebSocketApiId",
            value=self.websocket_api.api_id,
            description="Research WebSocket API ID",
            export_name=f"{PROJECT_PREFIX}-ws-api-id",
        )
        cdk.CfnOutput(
            self,
            "WSConnectionsTableName",
            value=self.ws_connections_table.table_name,
            description="DynamoDB table for WebSocket connection registry",
            export_name=f"{PROJECT_PREFIX}-ws-connections-table",
        )
        cdk.CfnOutput(
            self,
            "ResponseApiFunctionName",
            value=self.response_api_fn.function_name,
            description="Response API Lambda function name",
            export_name=f"{PROJECT_PREFIX}-response-api-name",
        )
        cdk.CfnOutput(
            self,
            "WebSocketHandlerFunctionName",
            value=self.websocket_handler_fn.function_name,
            description="WebSocket Handler Lambda function name",
            export_name=f"{PROJECT_PREFIX}-websocket-handler-name",
        )
