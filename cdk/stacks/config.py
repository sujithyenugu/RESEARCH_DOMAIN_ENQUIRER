"""
config.py — Central configuration constants for all CDK stacks.

All resource names, CIDR blocks, instance types, retention periods, and
threshold values live here. Change a value once and it propagates to every
stack that imports this module.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Project identity
# ---------------------------------------------------------------------------
PROJECT_NAME    = "research-rag"
PROJECT_PREFIX  = "research"   # Used as prefix for all resource names

# ---------------------------------------------------------------------------
# AWS Region & Account (overridable via CDK context)
# ---------------------------------------------------------------------------
DEFAULT_REGION  = "us-east-1"

# ---------------------------------------------------------------------------
# VPC Configuration  (matches INFRASTRUCTURE.md §VPC Design)
# ---------------------------------------------------------------------------
VPC_CIDR = "10.0.0.0/16"

# Availability zones used in us-east-1
AVAILABILITY_ZONES = [
    "us-east-1a",
    "us-east-1b",
    "us-east-1c",
]

# Private subnets — Neptune nodes
SUBNET_NEPTUNE_A = "10.0.10.0/24"
SUBNET_NEPTUNE_B = "10.0.11.0/24"
SUBNET_NEPTUNE_C = "10.0.12.0/24"

# Private subnets — OpenSearch nodes
SUBNET_OPENSEARCH_A = "10.0.20.0/24"
SUBNET_OPENSEARCH_B = "10.0.21.0/24"
SUBNET_OPENSEARCH_C = "10.0.22.0/24"

# Lambda subnets (VPC-attached Lambda functions)
SUBNET_LAMBDA_A = "10.0.30.0/24"
SUBNET_LAMBDA_B = "10.0.31.0/24"
SUBNET_LAMBDA_C = "10.0.32.0/24"

# Security group ports
NEPTUNE_PORT    = 8182    # Gremlin WebSocket
OPENSEARCH_PORT = 443     # HTTPS

# ---------------------------------------------------------------------------
# S3 Bucket Names  (matches INFRASTRUCTURE.md §S3 Buckets)
# ---------------------------------------------------------------------------
S3_RAW_PAPERS          = f"{PROJECT_PREFIX}-raw-papers"
S3_PARSED_PAPERS       = f"{PROJECT_PREFIX}-parsed-papers"
S3_CLEANED_PAPERS      = f"{PROJECT_PREFIX}-cleaned-papers"
S3_EMBEDDINGS_CACHE    = f"{PROJECT_PREFIX}-embeddings-cache"
S3_NEPTUNE_BULK        = f"{PROJECT_PREFIX}-neptune-bulk"
S3_EVALUATION          = f"{PROJECT_PREFIX}-evaluation"
S3_FRONTEND            = f"{PROJECT_PREFIX}-frontend"
S3_LOGS                = f"{PROJECT_PREFIX}-logs"
S3_PROCESSING_LOGS     = f"{PROJECT_PREFIX}-processing-logs"

# S3 lifecycle transition periods (in days)
S3_INTELLIGENT_TIERING_DAYS = 30
S3_GLACIER_DAYS             = 365
S3_EMBEDDINGS_CACHE_EXPIRY  = 90
S3_NEPTUNE_BULK_EXPIRY      = 7
S3_LOGS_EXPIRY              = 90
S3_PROCESSING_LOGS_EXPIRY   = 30

# ---------------------------------------------------------------------------
# DynamoDB Table Names  (matches INGESTION_PIPELINE.md §Stage 5)
# ---------------------------------------------------------------------------
DYNAMO_PAPER_METADATA_TABLE = "ResearchPaperMetadata"
DYNAMO_EVAL_HISTORY_TABLE   = "EvalHistory"

# DynamoDB GSI names
GSI_CATEGORY_PUBLISHED  = "category-published-index"
GSI_STATUS_CREATED      = "status-index"

# ---------------------------------------------------------------------------
# Amazon OpenSearch  (matches ARCHITECTURE.md §6 + VECTOR_PIPELINE.md)
# ---------------------------------------------------------------------------
OPENSEARCH_DOMAIN_NAME   = f"{PROJECT_PREFIX}-opensearch"
OPENSEARCH_VERSION       = "OpenSearch_2.11"

# Instance types
OPENSEARCH_MASTER_TYPE   = "m6g.large.search"
OPENSEARCH_MASTER_COUNT  = 3
OPENSEARCH_DATA_TYPE     = "r6g.large.search"
OPENSEARCH_DATA_COUNT    = 3

# Storage
OPENSEARCH_VOLUME_SIZE_GB = 500          # per data node (gp3 EBS)
OPENSEARCH_VOLUME_TYPE    = "gp3"

# Index settings
OPENSEARCH_INDEX_NAME       = "paper_chunks"
OPENSEARCH_KNN_DIMENSIONS   = 1536       # Titan Embeddings V2
OPENSEARCH_KNN_EF_SEARCH    = 512
OPENSEARCH_KNN_EF_CONSTRUCT = 512
OPENSEARCH_KNN_M            = 16
OPENSEARCH_PRIMARY_SHARDS   = 6
OPENSEARCH_REPLICA_SHARDS   = 1
OPENSEARCH_REFRESH_INTERVAL = "30s"

# ---------------------------------------------------------------------------
# Amazon Neptune  (matches ARCHITECTURE.md §7 + GRAPH_PIPELINE.md)
# ---------------------------------------------------------------------------
NEPTUNE_CLUSTER_ID       = f"{PROJECT_PREFIX}-neptune"
NEPTUNE_INSTANCE_TYPE    = "db.r6g.large"
NEPTUNE_ENGINE_VERSION   = "1.3.2.1"     # Neptune 1.3
NEPTUNE_PORT             = 8182
NEPTUNE_BACKUP_RETENTION = 7             # days
NEPTUNE_BACKUP_WINDOW    = "02:00-03:00" # UTC

# ---------------------------------------------------------------------------
# Amazon Bedrock Model IDs  (matches ARCHITECTURE.md §8)
# ---------------------------------------------------------------------------
BEDROCK_EMBEDDING_MODEL   = "amazon.titan-embed-text-v2:0"
BEDROCK_ENTITY_MODEL      = "anthropic.claude-3-haiku-20240307-v1:0"
BEDROCK_GENERATION_MODEL  = "anthropic.claude-3-5-sonnet-20241022-v2:0"
BEDROCK_VERIFY_MODEL      = "anthropic.claude-3-haiku-20240307-v1:0"

# ---------------------------------------------------------------------------
# SageMaker Cross-Encoder  (matches ARCHITECTURE.md §9)
# ---------------------------------------------------------------------------
SAGEMAKER_RERANKER_ENDPOINT = "cross-encoder-reranker-prod"
SAGEMAKER_INSTANCE_TYPE     = "ml.g4dn.xlarge"
SAGEMAKER_MIN_INSTANCES     = 1
SAGEMAKER_MAX_INSTANCES     = 3

# ---------------------------------------------------------------------------
# Lambda configuration  (matches INFRASTRUCTURE.md §Lambda Runtime)
# ---------------------------------------------------------------------------
LAMBDA_RUNTIME = "python3.11"

LAMBDA_CONFIGS: dict[str, dict] = {
    "paper_fetcher": {
        "memory_mb": 128,
        "timeout_seconds": 30,
        "concurrency": 1,
        "vpc": False,
    },
    "paper_processor": {
        "memory_mb": 512,
        "timeout_seconds": 900,
        "concurrency": 5,
        "vpc": True,
    },
    "docling_parser": {
        "memory_mb": 3008,
        "timeout_seconds": 600,
        "concurrency": 5,
        "vpc": True,
        "container": True,   # Container image Lambda
    },
    "embedding_worker": {
        "memory_mb": 1024,
        "timeout_seconds": 300,
        "concurrency": 20,
        "vpc": True,
    },
    "graph_builder": {
        "memory_mb": 512,
        "timeout_seconds": 300,
        "concurrency": 10,
        "vpc": True,
    },
    "entity_extractor": {
        "memory_mb": 512,
        "timeout_seconds": 300,
        "concurrency": 10,
        "vpc": True,
    },
    "query_handler": {
        "memory_mb": 512,
        "timeout_seconds": 30,
        "concurrency": None,  # unreserved — scales with traffic
        "vpc": True,
    },
    "reranker": {
        "memory_mb": 256,
        "timeout_seconds": 30,
        "concurrency": None,
        "vpc": False,
    },
    "context_builder": {
        "memory_mb": 256,
        "timeout_seconds": 30,
        "concurrency": None,
        "vpc": False,
    },
    "answer_generator": {
        "memory_mb": 512,
        "timeout_seconds": 60,
        "concurrency": None,
        "vpc": False,
    },
    "hallucination_detector": {
        "memory_mb": 512,
        "timeout_seconds": 30,
        "concurrency": None,
        "vpc": False,
    },
    "online_evaluator": {
        "memory_mb": 256,
        "timeout_seconds": 30,
        "concurrency": None,
        "vpc": False,
    },
    "offline_evaluator": {
        "memory_mb": 1024,
        "timeout_seconds": 900,
        "concurrency": 1,
        "vpc": False,
    },
    "response_api": {
        "memory_mb": 256,
        "timeout_seconds": 30,
        "concurrency": None,
        "vpc": False,
    },
}

# ---------------------------------------------------------------------------
# SQS Queues  (matches ARCHITECTURE.md + DATA_FLOW.md)
# ---------------------------------------------------------------------------
SQS_PAPER_QUEUE_NAME       = f"{PROJECT_PREFIX}-paper-queue.fifo"
SQS_EMBEDDING_QUEUE_NAME   = f"{PROJECT_PREFIX}-embedding-queue"
SQS_ENTITY_QUEUE_NAME      = f"{PROJECT_PREFIX}-entity-queue"

SQS_PAPER_DLQ_NAME         = f"{PROJECT_PREFIX}-paper-dlq.fifo"
SQS_EMBEDDING_DLQ_NAME     = f"{PROJECT_PREFIX}-embedding-dlq"
SQS_ENTITY_DLQ_NAME        = f"{PROJECT_PREFIX}-entity-dlq"

SQS_PAPER_VISIBILITY_TIMEOUT = 900   # seconds — must match max Lambda timeout
SQS_DLQ_MAX_RECEIVE_COUNT    = 3     # retries before DLQ
SQS_MESSAGE_RETENTION_DAYS   = 4

# ---------------------------------------------------------------------------
# EventBridge schedules
# ---------------------------------------------------------------------------
FETCH_SCHEDULE_EXPRESSION = "rate(6 hours)"
EVAL_SCHEDULE_EXPRESSION  = "cron(0 2 * * ? *)"   # daily at 02:00 UTC

# arXiv categories to monitor
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.NE", "cs.IR", "stat.ML"]
ARXIV_LOOKBACK_HOURS = 7
ARXIV_MAX_RESULTS    = 100

# ---------------------------------------------------------------------------
# Retrieval configuration
# ---------------------------------------------------------------------------
DENSE_RETRIEVAL_K    = 30
BM25_RETRIEVAL_K     = 30
GRAPH_EXPANSION_K    = 20
RERANK_TOP_K         = 50
FINAL_TOP_K          = 10
CONTEXT_TOKEN_BUDGET = 8000

# HyDE (Hypothetical Document Embedding)
HYDE_ENABLED = True

# ---------------------------------------------------------------------------
# Hallucination detection thresholds
# ---------------------------------------------------------------------------
CONFIDENCE_PASS_THRESHOLD   = 0.85
CONFIDENCE_WARN_THRESHOLD   = 0.60
CONFIDENCE_REFUSE_THRESHOLD = 0.30

# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
GOLDEN_DATASET_VERSION = "v2"
GOLDEN_DATASET_COUNT   = 100

EVAL_REGRESSION_THRESHOLDS: dict[str, float] = {
    "recall_at_10":       0.05,
    "faithfulness":       0.05,
    "groundedness":       0.07,
    "citation_accuracy":  0.03,
    "e2e_latency_p95_ms": 1000.0,
}

# CloudWatch alarm targets
ALARM_RECALL_MIN          = 0.70
ALARM_FAITHFULNESS_MIN    = 0.75
ALARM_E2E_P95_MAX_MS      = 8000
ALARM_CITATION_ACC_MIN    = 0.90
ALARM_CONFIDENCE_AVG_MIN  = 0.65
ALARM_REFUSAL_RATE_MAX    = 0.15

# ---------------------------------------------------------------------------
# API Gateway
# ---------------------------------------------------------------------------
API_STAGE_NAME = "prod"

# ---------------------------------------------------------------------------
# CloudFront / WAF
# ---------------------------------------------------------------------------
WAF_RATE_LIMIT_PER_5MIN = 1000   # requests per IP per 5 minutes

# ---------------------------------------------------------------------------
# Secrets Manager paths  (matches INFRASTRUCTURE.md §Secrets Manager)
# ---------------------------------------------------------------------------
SECRET_ARXIV_API_KEY        = f"{PROJECT_PREFIX}-rag/arxiv-api-key"
SECRET_NEPTUNE_ENDPOINT     = f"{PROJECT_PREFIX}-rag/neptune-endpoint"
SECRET_OPENSEARCH_ENDPOINT  = f"{PROJECT_PREFIX}-rag/opensearch-endpoint"
SECRET_SLACK_WEBHOOK        = f"{PROJECT_PREFIX}-rag/slack-webhook"
SECRET_SAGEMAKER_ENDPOINT   = f"{PROJECT_PREFIX}-rag/sagemaker-endpoint"

# ---------------------------------------------------------------------------
# CloudWatch namespace
# ---------------------------------------------------------------------------
CW_NAMESPACE = "ResearchRAG"
