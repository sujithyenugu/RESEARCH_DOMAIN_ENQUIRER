# 🏗️ Infrastructure — Research Domain Enquirer

> Covers: AWS CDK stacks · IAM roles · Networking/VPC · S3 buckets · Cost estimates · Deployment runbook

---

## CDK Stack Architecture

All infrastructure is defined as **AWS CDK (Python)** stacks, deployed in dependency order.

```
cdk/
├── app.py                        ← CDK App entry point
├── cdk.json                      ← CDK config
├── requirements.txt              ← CDK + construct deps
└── stacks/
    ├── storage_stack.py          ← S3, DynamoDB, OpenSearch, Neptune, SageMaker
    ├── ingestion_stack.py        ← EventBridge, SQS (Paper/Entity/Embedding), Lambda (Fetcher + Processor)
    ├── embedding_stack.py        ← SQS, Lambda (Embedder), Bedrock config
    ├── graph_stack.py            ← SQS, Lambda (Graph Builder), Neptune config
    ├── retrieval_stack.py        ← Lambda (Query Handler, Reranker, Context Builder), API Gateway
    ├── generation_stack.py       ← Lambda (Answer Gen, Hallucination Detector), Bedrock config
    ├── evaluation_stack.py       ← EventBridge cron, Lambda (Evaluator), S3 eval bucket
    ├── api_stack.py              ← API Gateway (REST + WebSocket), Lambda integrations
    ├── frontend_stack.py         ← S3 (SPA), CloudFront, WAF, ACM certificate
    └── monitoring_stack.py       ← CloudWatch dashboards, alarms, X-Ray, SNS topics
```

### Stack Dependency Order

```
StorageStack
    ├─── IngestionStack (depends on: S3, DynamoDB, SQS)
    │       └─── EmbeddingStack (depends on: SQS from Ingestion)
    │       └─── GraphStack (depends on: SQS from Ingestion, Neptune)
    ├─── RetrievalStack (depends on: OpenSearch, Neptune, SageMaker endpoint)
    │       └─── GenerationStack (depends on: Retrieval Lambdas)
    │               └─── ApiStack (depends on: Generation Lambdas)
    │                       └─── FrontendStack (depends on: API Gateway)
    ├─── EvaluationStack (depends on: RetrievalStack, GenerationStack, S3)
    └─── MonitoringStack (depends on: all stacks — deployed last)
```

---

## VPC Design

```
VPC CIDR: 10.0.0.0/16
Region: us-east-1 (primary)

Availability Zones: us-east-1a, us-east-1b, us-east-1c

┌──────────────────────────────────────────────────────────────────┐
│  VPC: research-rag-vpc (10.0.0.0/16)                             │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Private Subnets (no internet, data tier)                   │ │
│  │  ├─ 10.0.10.0/24 (us-east-1a) — Neptune writer             │ │
│  │  ├─ 10.0.11.0/24 (us-east-1b) — Neptune reader             │ │
│  │  ├─ 10.0.12.0/24 (us-east-1c) — Neptune replica            │ │
│  │  ├─ 10.0.20.0/24 (us-east-1a) — OpenSearch node 1          │ │
│  │  ├─ 10.0.21.0/24 (us-east-1b) — OpenSearch node 2          │ │
│  │  └─ 10.0.22.0/24 (us-east-1c) — OpenSearch node 3          │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Lambda Subnets (VPC-attached Lambdas)                      │ │
│  │  ├─ 10.0.30.0/24 (us-east-1a)                              │ │
│  │  ├─ 10.0.31.0/24 (us-east-1b)                              │ │
│  │  └─ 10.0.32.0/24 (us-east-1c)                              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  Interface VPC Endpoints (private connectivity):                  │
│  ├─ com.amazonaws.us-east-1.bedrock-runtime                      │
│  ├─ com.amazonaws.us-east-1.sagemaker.runtime                    │
│  ├─ com.amazonaws.us-east-1.secretsmanager                       │
│  ├─ com.amazonaws.us-east-1.sqs                                  │
│  ├─ com.amazonaws.us-east-1.dynamodb (Gateway endpoint)          │
│  └─ com.amazonaws.us-east-1.s3 (Gateway endpoint)                │
│                                                                   │
│  Security Groups:                                                 │
│  ├─ sg-neptune: allow 8182 from sg-lambda                        │
│  ├─ sg-opensearch: allow 443 from sg-lambda                      │
│  └─ sg-lambda: allow all outbound (to VPC endpoints)             │
└──────────────────────────────────────────────────────────────────┘
```

---

## IAM Roles — Least Privilege

Each Lambda function has its own dedicated IAM role.

### Lambda: Paper Fetcher Role

```json
{
  "RoleName": "research-rag-paper-fetcher-role",
  "Policies": [
    {
      "PolicyName": "PaperFetcherPolicy",
      "Statements": [
        {
          "Effect": "Allow",
          "Actions": ["sqs:SendMessage"],
          "Resources": ["arn:aws:sqs:*:ACCOUNT:research-paper-queue.fifo"]
        },
        {
          "Effect": "Allow",
          "Actions": ["dynamodb:GetItem"],
          "Resources": ["arn:aws:dynamodb:*:ACCOUNT:table/ResearchPaperMetadata"]
        },
        {
          "Effect": "Allow",
          "Actions": ["cloudwatch:PutMetricData"],
          "Resources": ["*"]
        },
        {
          "Effect": "Allow",
          "Actions": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
          "Resources": ["arn:aws:logs:*:ACCOUNT:log-group:/aws/lambda/research-paper-fetcher*"]
        }
      ]
    }
  ]
}
```

### Lambda: Paper Processor Role

```json
{
  "RoleName": "research-rag-paper-processor-role",
  "Policies": [
    {
      "PolicyName": "PaperProcessorPolicy",
      "Statements": [
        { "Effect": "Allow", "Actions": ["s3:PutObject", "s3:GetObject"],
          "Resources": ["arn:aws:s3:::research-raw-papers/*", "arn:aws:s3:::research-parsed-papers/*"] },
        { "Effect": "Allow", "Actions": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
          "Resources": ["arn:aws:dynamodb:*:ACCOUNT:table/ResearchPaperMetadata"] },
        { "Effect": "Allow", "Actions": ["sqs:SendMessage"],
          "Resources": ["arn:aws:sqs:*:ACCOUNT:research-embedding-queue",
                        "arn:aws:sqs:*:ACCOUNT:research-entity-queue"] },
        { "Effect": "Allow", "Actions": ["sqs:ReceiveMessage", "sqs:DeleteMessage",
                                         "sqs:GetQueueAttributes"],
          "Resources": ["arn:aws:sqs:*:ACCOUNT:research-paper-queue.fifo"] },
        { "Effect": "Allow", "Actions": ["textract:StartDocumentAnalysis",
                                         "textract:GetDocumentAnalysis"],
          "Resources": ["*"] },
        { "Effect": "Allow", "Actions": ["lambda:InvokeFunction"],
          "Resources": ["arn:aws:lambda:*:ACCOUNT:function:research-docling-parser"] },
        { "Effect": "Allow", "Actions": ["secretsmanager:GetSecretValue"],
          "Resources": ["arn:aws:secretsmanager:*:ACCOUNT:secret:research-rag/*"] }
      ]
    }
  ]
}
```

### Lambda: Query Handler Role

```json
{
  "RoleName": "research-rag-query-handler-role",
  "Policies": [
    { "PolicyName": "QueryHandlerPolicy", "Statements": [
        { "Effect": "Allow", "Actions": ["bedrock:InvokeModel"],
          "Resources": ["arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
                        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku*"] },
        { "Effect": "Allow", "Actions": ["es:ESHttpPost", "es:ESHttpGet"],
          "Resources": ["arn:aws:es:*:ACCOUNT:domain/research-opensearch/*"] },
        { "Effect": "Allow", "Actions": ["neptune-db:connect", "neptune-db:ReadDataViaQuery"],
          "Resources": ["arn:aws:neptune-db:*:ACCOUNT:*/*"] },
        { "Effect": "Allow", "Actions": ["lambda:InvokeFunction"],
          "Resources": ["arn:aws:lambda:*:ACCOUNT:function:research-reranker",
                        "arn:aws:lambda:*:ACCOUNT:function:research-context-builder"] }
    ]}
  ]
}
```

---

## S3 Buckets

| Bucket Name | Purpose | Versioning | Lifecycle |
|-------------|---------|-----------|-----------|
| `research-raw-papers` | Original PDFs from arXiv | Enabled | Intelligent-Tiering after 30d, Glacier after 1y |
| `research-parsed-papers` | Docling/Textract JSON output | Enabled | Standard, no expiry |
| `research-cleaned-papers` | Final cleaned text JSON | Enabled | Standard, no expiry |
| `research-embeddings-cache` | Pre-computed embeddings (cache) | Disabled | Expire after 90d |
| `research-neptune-bulk` | CSV files for Neptune bulk load | Disabled | Expire after 7d |
| `research-evaluation` | Golden dataset, eval results | Enabled | No expiry |
| `research-frontend` | React SPA assets (private, CloudFront origin) | Enabled | No expiry |
| `research-logs` | Access logs from all S3 buckets | Disabled | Expire after 90d |
| `research-processing-logs` | Lambda processing audit logs | Disabled | Expire after 30d |

### S3 Bucket Policies

All buckets have:
- **Block all public access** — enabled
- **Server-side encryption** — SSE-KMS with aws/s3 key
- **Enforce TLS** — deny HTTP requests (`aws:SecureTransport: false`)
- **Access logging** → `research-logs` bucket

---

## Lambda Runtime Configuration

| Lambda | Runtime | Memory | Timeout | Type | VPC |
|--------|---------|--------|---------|------|-----|
| Paper Fetcher | Python 3.11 | 128 MB | 30s | Zip | No |
| Paper Processor | Python 3.11 | 512 MB | 900s | Zip | Yes |
| Docling Parser | Python 3.11 | 3008 MB | 600s | Container | Yes |
| Embedding Worker | Python 3.11 | 1024 MB | 300s | Zip | Yes |
| Graph Builder | Python 3.11 | 512 MB | 300s | Zip | Yes |
| Entity Extractor | Python 3.11 | 512 MB | 300s | Zip | Yes |
| Query Handler | Python 3.11 | 512 MB | 30s | Zip | Yes |
| Reranker | Python 3.11 | 256 MB | 30s | Zip | No |
| Context Builder | Python 3.11 | 256 MB | 30s | Zip | No |
| Answer Generator | Python 3.11 | 512 MB | 60s | Zip | No |
| Hallucination Detector | Python 3.11 | 512 MB | 30s | Zip | No |
| Online Evaluator | Python 3.11 | 256 MB | 30s | Zip | No |
| Offline Evaluator | Python 3.11 | 1024 MB | 900s | Zip | No |
| Response API | Python 3.11 | 256 MB | 30s | Zip | No |

**VPC-attached Lambdas** (those needing Neptune/OpenSearch):  
Paper Processor, Embedding Worker, Graph Builder, Entity Extractor, Query Handler

**Non-VPC Lambdas** call Bedrock/SageMaker via VPC endpoint from within Lambda VPC, or over internet (for non-VPC Lambdas using IAM auth).

---

## Secrets Manager

```
Secret paths:

research-rag/arxiv-api-key
  → arXiv API key (if using Semantic Scholar instead)

research-rag/neptune-endpoint
  → { "writer": "neptune-cluster.xxx.us-east-1.neptune.amazonaws.com",
      "reader": "neptune-cluster-ro.xxx.us-east-1.neptune.amazonaws.com" }

research-rag/opensearch-endpoint
  → { "endpoint": "vpc-research-opensearch-xxxx.us-east-1.es.amazonaws.com",
      "username": "admin",
      "password": "..." }

research-rag/slack-webhook
  → Slack incoming webhook URL for alerts

research-rag/sagemaker-endpoint
  → { "endpoint_name": "cross-encoder-reranker-prod" }
```

Lambda Processor reads secrets **once at cold start**, cached for Lambda lifetime.

---

## Monthly Cost Estimate

> Estimated for ~500 papers/day ingestion, ~5,000 queries/day

| Service | Component | Monthly Cost |
|---------|-----------|-------------|
| **Lambda** | All functions combined (~10M invocations) | ~$20 |
| **Amazon OpenSearch** | 3× r6g.large + 1.5TB EBS | ~$750 |
| **Amazon Neptune** | db.r6g.large writer + 1 reader | ~$500 |
| **Amazon SageMaker** | ml.g4dn.xlarge (1 instance, auto-scale) | ~$400 |
| **Amazon Bedrock** | Titan Embeddings (10M tokens/day) | ~$60 |
| **Amazon Bedrock** | Claude 3 Haiku (entity + verification) | ~$90 |
| **Amazon Bedrock** | Claude 3.5 Sonnet (generation) | ~$150 |
| **Amazon Textract** | ~500 PDFs/day (async detection) | ~$150 |
| **Amazon S3** | 5TB total storage | ~$115 |
| **Amazon DynamoDB** | On-demand (~5M writes/month) | ~$10 |
| **Amazon SQS** | ~50M messages/month | ~$20 |
| **EventBridge** | Schedulers (4× daily) | ~$1 |
| **API Gateway** | REST + WebSocket (5K queries/day) | ~$15 |
| **CloudFront** | ~100GB transfer/month | ~$10 |
| **CloudWatch** | Metrics + Logs + Dashboards | ~$30 |
| **VPC Endpoints** | 5 interface endpoints × 2 AZs | ~$70 |
| **Data Transfer** | Cross-AZ + internet egress | ~$50 |
| **Total** | | **~$2,441/month** |

### Cost Optimization Options

| Option | Savings | Trade-off |
|--------|---------|-----------|
| OpenSearch UltraWarm for old indexes | -$200 | Slower cold queries |
| Neptune Serverless (if low traffic) | -$300 | Higher per-query cost |
| SageMaker scale-to-zero (serverless) | -$300 | Cold start latency |
| Bedrock Provisioned Throughput | -$50 | Upfront commitment |
| S3 Intelligent-Tiering | -$30 | None |

---

## Deployment Runbook

### Prerequisites

```bash
# Install tools
pip install aws-cdk-lib constructs
npm install -g aws-cdk
aws configure  # Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, region

# Verify access
aws sts get-caller-identity
aws bedrock list-foundation-models --region us-east-1  # Check Bedrock access
```

### Bootstrap & Deploy

```bash
# 1. Bootstrap CDK (once per account/region)
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# 2. Deploy in order (each depends on previous)
cdk deploy StorageStack --require-approval never
cdk deploy IngestionStack --require-approval never
cdk deploy EmbeddingStack GraphStack --require-approval never  # parallel OK
cdk deploy RetrievalStack --require-approval never
cdk deploy GenerationStack --require-approval never
cdk deploy EvaluationStack ApiStack --require-approval never  # parallel OK
cdk deploy FrontendStack MonitoringStack --require-approval never

# 3. Initialize OpenSearch index
aws lambda invoke \
  --function-name research-opensearch-initializer \
  --payload '{"action": "create_index"}' \
  response.json

# 4. Load Neptune schema (initial vertices/indexes)
aws lambda invoke \
  --function-name research-neptune-initializer \
  --payload '{"action": "create_schema"}' \
  response.json

# 5. Trigger first ingestion run
aws events put-events \
  --entries '[{
    "Source": "research.manual",
    "DetailType": "ManualFetch",
    "Detail": "{\"categories\": [\"cs.AI\", \"cs.LG\"], \"lookback_hours\": 168}"
  }]'
```

### Teardown

```bash
# Remove all stacks in reverse order
cdk destroy MonitoringStack FrontendStack
cdk destroy ApiStack EvaluationStack
cdk destroy GenerationStack RetrievalStack
cdk destroy GraphStack EmbeddingStack IngestionStack
cdk destroy StorageStack  # Warning: destroys all data!
```

> ⚠️ `StorageStack` destroy will permanently delete S3 objects, DynamoDB data, Neptune graph, and OpenSearch indexes. Enable `RemovalPolicy.RETAIN` on production to prevent accidental deletion.

---

## Environment Variables per Lambda

```bash
# Shared (all Lambdas)
AWS_REGION=us-east-1
POWERTOOLS_SERVICE_NAME=research-rag
POWERTOOLS_LOG_LEVEL=INFO
POWERTOOLS_TRACER_CAPTURE_RESPONSE=true

# Storage
PAPER_METADATA_TABLE=ResearchPaperMetadata
RAW_PAPERS_BUCKET=research-raw-papers
PARSED_PAPERS_BUCKET=research-parsed-papers
CLEANED_PAPERS_BUCKET=research-cleaned-papers

# Queues
PAPER_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT/research-paper-queue.fifo
EMBEDDING_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT/research-embedding-queue
ENTITY_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT/research-entity-queue

# Search
OPENSEARCH_ENDPOINT=vpc-research-opensearch-xxxx.us-east-1.es.amazonaws.com
OPENSEARCH_INDEX=paper_chunks

# Graph
NEPTUNE_ENDPOINT=neptune-cluster.xxx.us-east-1.neptune.amazonaws.com
NEPTUNE_PORT=8182

# Models
EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
ENTITY_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
GENERATION_MODEL_ID=anthropic.claude-3-5-sonnet-20241022-v2:0
VERIFY_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
RERANKER_ENDPOINT=cross-encoder-reranker-prod

# Fetch config
ARXIV_CATEGORIES=cs.AI,cs.LG,cs.CL,cs.CV,cs.NE
ARXIV_LOOKBACK_HOURS=7
ARXIV_MAX_RESULTS_PER_FETCH=100
LAMBDA_CONCURRENCY_PROCESSOR=5

# Retrieval config
DENSE_RETRIEVAL_K=30
BM25_RETRIEVAL_K=30
GRAPH_EXPANSION_K=20
RERANK_TOP_K=50
FINAL_TOP_K=10
CONTEXT_TOKEN_BUDGET=8000

# Confidence thresholds
CONFIDENCE_PASS_THRESHOLD=0.85
CONFIDENCE_WARN_THRESHOLD=0.60
CONFIDENCE_REFUSE_THRESHOLD=0.30
```

---

*See [DATA_FLOW.md](./DATA_FLOW.md) for exact event schemas and message contracts between all components.*
