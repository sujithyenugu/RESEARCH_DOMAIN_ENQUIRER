# 🔬 Research Domain Enquirer
### Production AI Research RAG — AWS-Native Architecture
#### GraphRAG · Late Chunking · Hallucination Detection · Knowledge Graph

---

> **Mission:** An event-driven, fully serverless AWS system that continuously ingests AI research papers from arXiv, builds a living knowledge graph, and answers research questions with hallucination-verified, citation-grounded responses.

---

## 📐 Architecture at a Glance

```
arXiv API
    │
    ▼
[EventBridge Scheduler]  ──►  [Lambda: Paper Fetcher]  ──►  [SQS: Paper Queue]
                                                                     │
                              ┌──────────────────────────────────────┘
                              ▼
                    [Lambda: Paper Processor]
                    ├── Download PDF  ──────────────────────►  [S3: Raw PDFs]
                    ├── Parse (Textract / Docling Lambda)
                    ├── Clean & Normalize
                    ├── Extract Metadata  ────────────────►  [DynamoDB: Paper Metadata]
                    ├── Extract Entities  ────────────────►  [SQS: Entity Queue]
                    └── Chunk (Late Chunking)  ────────────►  [SQS: Embedding Queue]
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
  [Lambda: Graph Builder]          [Lambda: Embedding Worker]
        │                                    │
        ▼                                    ▼
  [Neptune: Knowledge Graph]      [OpenSearch: Vector + BM25]
  ├── Entities                    ├── Dense Embeddings (Titan)
  ├── Relationships               ├── BM25 Full-text Index
  ├── Citation Graph              └── Chunk Metadata
  └── Topic Graph
              │                               │
              └───────────────┬───────────────┘
                              ▼
              ════════════════════════════════
                    HYBRID RETRIEVAL ENGINE
              ════════════════════════════════
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        Dense Search      BM25 Search   Graph Expansion
        (OpenSearch)      (OpenSearch)  (Neptune Gremlin)
              └───────────────┼───────────────┘
                              ▼
                   [Lambda: Reranker]  (SageMaker Cross-Encoder)
                              │
                              ▼
                   [Lambda: Context Builder]
                   ├── Deduplication
                   ├── Citation Ordering
                   └── Prompt Assembly
                              │
                              ▼
                   [Amazon Bedrock]
                   Claude 3.5 / Titan
                   Answer Generation
                              │
                              ▼
                   [Lambda: Hallucination Detector]
                   ├── Evidence Coverage Check
                   ├── Citation Grounding
                   ├── Unsupported Claim Detection
                   └── Confidence Scoring
                              │
                              ▼
                   [API Gateway + Lambda: Response API]
                              │
                              ▼
                   [CloudFront + S3: Frontend]
                   ├── Research Chat UI
                   ├── Knowledge Graph Viewer
                   ├── Paper Viewer
                   └── Evaluation Dashboard
```

---

## 📁 Document Index

| File | Description |
|------|-------------|
| [`README.md`](./README.md) | This file — system overview & mission |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Full AWS service mapping & component design |
| [`INGESTION_PIPELINE.md`](./INGESTION_PIPELINE.md) | arXiv fetching, parsing, chunking, entity extraction |
| [`GRAPH_PIPELINE.md`](./GRAPH_PIPELINE.md) | Neptune knowledge graph, entities, relationships, citation graph |
| [`VECTOR_PIPELINE.md`](./VECTOR_PIPELINE.md) | OpenSearch embeddings, BM25, late chunking strategy |
| [`RETRIEVAL_ENGINE.md`](./RETRIEVAL_ENGINE.md) | Hybrid retrieval, reranking, context construction |
| [`HALLUCINATION_DETECTION.md`](./HALLUCINATION_DETECTION.md) | Evidence verification, citation grounding, confidence scoring |
| [`EVALUATION_PIPELINE.md`](./EVALUATION_PIPELINE.md) | Retrieval & generation metrics, latency tracking |
| [`INFRASTRUCTURE.md`](./INFRASTRUCTURE.md) | AWS CDK stacks, IAM, networking, cost estimates |
| [`DATA_FLOW.md`](./DATA_FLOW.md) | End-to-end data flow, event contracts, SQS message schemas |
| [`FRONTEND.md`](./FRONTEND.md) | Frontend components, API contracts, CloudFront config |

---

## 🧱 Core AWS Services Used

| Category | AWS Service | Purpose |
|----------|------------|---------|
| **Scheduling** | EventBridge Scheduler | Trigger paper fetches on cron (e.g., every 6h) |
| **Compute** | AWS Lambda | All processing steps — stateless, auto-scaling |
| **Queuing** | Amazon SQS | Decouple all pipeline stages, back-pressure control |
| **Storage** | Amazon S3 | Raw PDFs, parsed text, prompt logs, frontend assets |
| **Metadata** | Amazon DynamoDB | Paper metadata, processing state, deduplication |
| **Vector Search** | Amazon OpenSearch | Dense embeddings (KNN) + BM25 full-text search |
| **Knowledge Graph** | Amazon Neptune | Entity graph, citation graph, topic graph |
| **AI / LLM** | Amazon Bedrock | Embeddings (Titan), generation (Claude), reranking |
| **ML Hosting** | Amazon SageMaker | Cross-encoder reranker model endpoint |
| **OCR / Parse** | Amazon Textract | PDF text + table extraction fallback |
| **API** | Amazon API Gateway | REST/WebSocket API for frontend |
| **CDN** | Amazon CloudFront | Frontend delivery, API caching |
| **Secrets** | AWS Secrets Manager | API keys, DB credentials |
| **Monitoring** | Amazon CloudWatch | Logs, metrics, alarms, dashboards |
| **Tracing** | AWS X-Ray | Distributed tracing across Lambda chain |
| **IaC** | AWS CDK (Python) | Infrastructure as Code, all stacks defined in CDK |

---

## 🔄 The "Not All At Once" Design — Event-Driven Ingestion

The system is designed to **never** batch-process all arXiv papers at once.  
Instead it uses a **fan-out, rate-limited, queue-driven** pattern:

```
EventBridge (every 6h)
       │
       ▼
Lambda: Fetcher
  ├── Queries arXiv API (last N hours, specific categories)
  ├── Filters already-processed papers (DynamoDB dedup check)
  └── Sends ONE message per paper to SQS Paper Queue
                    │
         ┌──────────┘  (Lambda concurrency = 5, SQS batch = 1)
         ▼
Lambda: Processor (per paper, isolated, retryable)
  ├── Downloads PDF to S3
  ├── Parses, cleans, chunks
  ├── Sends to Embedding Queue
  └── Sends to Entity Queue
```

**Key properties:**
- **Lambda concurrency limit** on Processor = 5 (respects arXiv rate limits)  
- **SQS visibility timeout** = 15 min (matches max Lambda timeout)  
- **DLQ** on every queue (failed papers don't block the pipeline)  
- **DynamoDB conditional write** = idempotent deduplication  
- **EventBridge → Lambda** can be paused/resumed without code changes  

---

## 🏗️ Infrastructure Stacks (CDK)

```
cdk/
├── stacks/
│   ├── storage_stack.py          # S3, DynamoDB, OpenSearch, Neptune
│   ├── ingestion_stack.py        # EventBridge, SQS, Lambda (Fetcher + Processor)
│   ├── embedding_stack.py        # SQS, Lambda (Embedder), Bedrock Titan
│   ├── graph_stack.py            # SQS, Lambda (Graph Builder), Neptune
│   ├── retrieval_stack.py        # Lambda (Query Handler, Reranker, Context Builder)
│   ├── generation_stack.py       # Bedrock, Lambda (Answer Gen, Hallucination Detector)
│   ├── api_stack.py              # API Gateway, Lambda (Response API)
│   └── frontend_stack.py         # S3, CloudFront
└── app.py
```

---

## 🚀 Getting Started

### Prerequisites
- AWS CLI configured with credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- AWS CDK CLI installed (`npm install -g aws-cdk`)
- Python 3.11+
- Docker (for Lambda container images with heavy deps)

### Deploy Order
```bash
# 1. Bootstrap CDK in your account
cdk bootstrap aws://ACCOUNT_ID/REGION

# 2. Deploy storage layer first (Neptune, OpenSearch, S3, DynamoDB)
cdk deploy StorageStack

# 3. Deploy ingestion pipeline
cdk deploy IngestionStack

# 4. Deploy embedding & graph workers
cdk deploy EmbeddingStack GraphStack

# 5. Deploy retrieval + generation
cdk deploy RetrievalStack GenerationStack

# 6. Deploy API + Frontend
cdk deploy ApiStack FrontendStack
```

---

## 📊 Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Graph DB | Amazon Neptune | Managed, no-ops, native Gremlin/SPARQL |
| Vector DB | Amazon OpenSearch | Managed KNN + BM25 in one service |
| LLM | Amazon Bedrock (Claude 3.5) | No GPU management, pay-per-token |
| Embeddings | Titan Embeddings V2 | Native Bedrock, 1536-dim, cost-effective |
| Chunking | Late Chunking | Better semantic coherence than fixed-size |
| Orchestration | SQS + Lambda (not Step Functions) | Simpler, cheaper for high-volume async work |
| Reranker | SageMaker Endpoint | Dedicated GPU for cross-encoder latency |

---

## 📈 Scalability Properties

- **Ingestion throughput:** ~100–500 papers/day (Lambda concurrency controlled)
- **Query latency target:** < 3s end-to-end (P95)
- **Vector index:** OpenSearch scales to 100M+ vectors
- **Graph size:** Neptune handles 10B+ edges
- **Cost model:** Fully pay-per-use (no idle EC2/RDS costs)

---

*See individual pipeline documents for deep-dives on each component.*
