"""
Research Domain Enquirer — CDK App Entry Point

Deploy order:
  1. StorageStack       — S3, DynamoDB, OpenSearch, Neptune, VPC
  2. IngestionStack     — EventBridge, SQS, Lambda (Fetcher + Processor)
  3. EmbeddingStack     — SQS, Lambda (Embedder), Bedrock Titan config
  4. GraphStack         — SQS, Lambda (Graph Builder), Neptune config
  5. RetrievalStack     — Lambda (Query Handler, Reranker, Context Builder)
  6. GenerationStack    — Lambda (Answer Gen, Hallucination Detector)
  7. EvaluationStack    — EventBridge cron, Lambda (Evaluator), S3 eval bucket
  8. ApiStack           — API Gateway REST + WebSocket
  9. FrontendStack      — S3 (SPA), CloudFront, WAF, ACM cert
  10. MonitoringStack   — CloudWatch Dashboards, Alarms, X-Ray, SNS

Usage:
  cdk bootstrap aws://ACCOUNT_ID/REGION
  cdk deploy StorageStack
  cdk deploy IngestionStack
  ...
"""

import aws_cdk as cdk
from stacks.storage_stack import StorageStack

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = cdk.App()

# Pull environment overrides from CDK context or use defaults
account = app.node.try_get_context("account") or cdk.Aws.ACCOUNT_ID
region  = app.node.try_get_context("region")  or "us-east-1"
env     = cdk.Environment(account=account, region=region)

# ---------------------------------------------------------------------------
# Stage 1 — Storage layer (must be deployed FIRST)
# ---------------------------------------------------------------------------
storage = StorageStack(
    app,
    "StorageStack",
    env=env,
    description=(
        "Research Domain Enquirer — Storage layer: "
        "VPC, S3 buckets, DynamoDB, Amazon OpenSearch, Amazon Neptune"
    ),
)

# Future stacks will be added here as they are implemented:
# ingestion = IngestionStack(app, "IngestionStack", storage=storage, env=env)
# embedding  = EmbeddingStack(app, "EmbeddingStack", storage=storage, env=env)
# graph      = GraphStack(app, "GraphStack", storage=storage, env=env)
# retrieval  = RetrievalStack(app, "RetrievalStack", storage=storage, env=env)
# generation = GenerationStack(app, "GenerationStack", retrieval=retrieval, env=env)
# evaluation = EvaluationStack(app, "EvaluationStack", retrieval=retrieval, env=env)
# api        = ApiStack(app, "ApiStack", generation=generation, env=env)
# frontend   = FrontendStack(app, "FrontendStack", api=api, env=env)
# monitoring = MonitoringStack(app, "MonitoringStack", env=env)

# ---------------------------------------------------------------------------
# Synthesise all stacks
# ---------------------------------------------------------------------------
app.synth()
