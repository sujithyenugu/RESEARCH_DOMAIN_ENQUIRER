# 📅 BUILD PLAN — Research Domain Enquirer
### Complete Day-by-Day Execution Roadmap

> **Project:** Production AI Research RAG on AWS  
> **Stack:** AWS CDK (Python) · Lambda · OpenSearch · Neptune · Bedrock · SageMaker  
> **Total Build Duration:** ~10 Days  
> **Strategy:** Deploy stacks in dependency order — each day builds on the previous.

---

## ✅ Status Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Complete — built, committed, pushed |
| 🔜 | Next up |
| ⏳ | Pending |
| 🔗 | Depends on a prior day |

---

## ✅ DAY 1 — Storage Layer (Foundation)
**CDK Stack:** `StorageStack`  
**Commit:** `Day 1 updated commit`  
**File:** `cdk/stacks/storage_stack.py`

### What was built:
- [x] **S3 Buckets** (9 buckets):
  - `research-raw-papers` — original PDFs from arXiv
  - `research-parsed-papers` — Docling/Textract output
  - `research-cleaned-papers` — final cleaned text JSON
  - `research-embeddings-cache` — pre-computed embeddings
  - `research-neptune-bulk` — CSV for Neptune bulk load
  - `research-evaluation` — golden dataset + eval results
  - `research-frontend` — React SPA assets (private)
  - `research-logs` — S3 access logs
  - `research-processing-logs` — Lambda audit logs
- [x] **DynamoDB Table** — `ResearchPaperMetadata` (on-demand, TTL)
- [x] **Amazon OpenSearch** — 3-node cluster, KNN + BM25, 1536-dim vectors
- [x] **Amazon Neptune** — writer + reader, Gremlin query language
- [x] **VPC** — private subnets, security groups, VPC endpoints
- [x] **Secrets Manager** — secret paths for endpoints, API keys
- [x] **KMS encryption** on all storage resources
- [x] **CDK config** — `cdk/app.py`, `cdk/cdk.json`, `config.py`

---

## ✅ DAY 2 — Ingestion Pipeline
**CDK Stack:** `IngestionStack`  
**Commit:** `Day 2 updated commit`  
**Files:** `cdk/stacks/ingestion_stack.py`, `lambdas/paper_fetcher/`, `lambdas/paper_processor/`

### What was built:
- [x] **EventBridge Scheduler** — cron every 6h, targets Paper Fetcher Lambda
- [x] **SQS: Paper Queue (FIFO)** — visibility=15min, DLQ after 3 failures
- [x] **SQS: Embedding Queue** — standard, batch=10
- [x] **SQS: Entity Queue** — standard, batch=5
- [x] **Lambda: Paper Fetcher** — queries arXiv API, deduplicates via DynamoDB, fans out to SQS
- [x] **Lambda: Paper Processor** — downloads PDF → S3, invokes Textract + Docling, chunks text, sends to Embedding + Entity queues
- [x] **Lambda: Docling Parser** (stub) — structured section parsing (container image, 3GB)
- [x] IAM roles for Fetcher + Processor (least-privilege)
- [x] CloudWatch Log Groups for all Lambdas
- [x] All environment variables wired via CDK

---

## ✅ DAY 3 — Embedding & Graph Pipeline
**CDK Stacks:** `EmbeddingStack`, `GraphStack`  
**Files to create:** `cdk/stacks/embedding_stack.py`, `cdk/stacks/graph_stack.py`, `lambdas/embedding_worker/`, `lambdas/graph_builder/`

### What to build:
- [ ] **Lambda: Embedding Worker**
  - Triggered by SQS Embedding Queue (batch=10)
  - Calls Bedrock Titan Embeddings V2 (`amazon.titan-embed-text-v2:0`) per chunk
  - Late Chunking: embeds full document context + chunk window
  - Indexes to OpenSearch (`paper_chunks` index) — KNN vector + BM25 text + metadata
  - Memory: 1024 MB, timeout: 5min, VPC-attached
- [ ] **Lambda: Graph Builder**
  - Triggered by SQS Entity Queue (batch=5)
  - Calls Bedrock Claude 3 Haiku for entity + relationship extraction
  - Upserts vertices (Paper, Author, Model, Dataset, Method, Benchmark, Concept, Topic) into Neptune via Gremlin
  - Upserts edges (CITES, INTRODUCES, PROPOSES, EVALUATES_ON, AUTHORED_BY, etc.)
  - Memory: 512 MB, timeout: 5min, VPC-attached
- [ ] **Docling Lambda** — full container image deployment (3GB, EFS mount at `/mnt/tmp`)
- [ ] **EmbeddingStack CDK** — SQS trigger, Lambda config, Bedrock IAM
- [ ] **GraphStack CDK** — SQS trigger, Lambda config, Neptune IAM, Bedrock IAM
- [ ] **OpenSearch index initializer** — creates `paper_chunks` mapping with KNN + BM25
- [ ] **Neptune schema initializer** — creates initial vertex labels + indexes
- [ ] Unit tests for embedding chunking logic

---

## ✅ DAY 4 — Retrieval Engine
**CDK Stack:** `RetrievalStack`  
**Commit:** `Day 4 updated commit`  
**Files:** `cdk/stacks/retrieval_stack.py`, `lambdas/query_handler/`, `lambdas/reranker/`, `lambdas/context_builder/`, `tests/test_retrieval_integration.py`

### What was built:
- [x] **Lambda: Query Handler** (`lambdas/query_handler/handler.py`)
  - Accepts POST `/query` from API Gateway
  - Query Understanding: Claude 3 Haiku classification + HyDE snippet generation
  - Embeds HyDE text via Bedrock Titan Embeddings V2 (1536-dim)
  - Runs 3 parallel searches via `asyncio.gather`: Dense KNN (k=30) + BM25 (k=30) + Neptune graph expansion (k=20)
  - Merges + deduplicates results with Reciprocal Rank Fusion (RRF, k=60)
  - Invokes Reranker Lambda synchronously with top-50 RRF candidates
  - Memory: 512 MB, timeout: 30s, VPC-attached
- [x] **Lambda: Reranker** (`lambdas/reranker/handler.py`)
  - Invokes SageMaker cross-encoder endpoint (`cross-encoder/ms-marco-MiniLM-L-12-v2`)
  - Batched scoring (batch=20) → top-K=10 final chunks by rerank_score
  - Graceful fallback to RRF ordering on SageMaker error
  - Invokes Context Builder Lambda synchronously
  - Memory: 256 MB, timeout: 30s
- [x] **Lambda: Context Builder** (`lambdas/context_builder/handler.py`)
  - Deduplication: (paper_id, section_id) + MD5 content hash
  - Tier-based citation ordering (Tier1 score>0.8 · Tier2 0.5-0.8 · Tier3 graph)
  - Context compression to 8,000-token budget (truncate Tier2, drop Tier3)
  - Formats citations as [paper_id] notation
  - Assembles SYSTEM + CONTEXT + QUESTION + ANSWER prompt string
  - Memory: 256 MB, timeout: 30s
- [x] **SageMaker Endpoint** — `cross-encoder/ms-marco-MiniLM-L-12-v2` on `ml.g4dn.xlarge`, auto-scaling min=1 max=3
- [x] **RetrievalStack CDK** — all Lambda configs, SageMaker Model/EndpointConfig/Endpoint, IAM grants (Bedrock, OpenSearch, Neptune, SageMaker)
- [x] **Integration + Unit tests** (`tests/test_retrieval_integration.py`) — RRF, dedup, ordering, compression, prompt assembly + live Lambda tests
- [x] `cdk/app.py` updated — EmbeddingStack, GraphStack, RetrievalStack wired with correct dependency chain

---

## ⏳ DAY 5 — Generation & Hallucination Detection
**CDK Stack:** `GenerationStack`  
**Files to create:** `cdk/stacks/generation_stack.py`, `lambdas/answer_generator/`, `lambdas/hallucination_detector/`

### What to build:
- [ ] **Lambda: Answer Generator**
  - Calls Bedrock Claude 3.5 Sonnet with assembled context + query
  - Streams answer tokens (for WebSocket delivery)
  - Enforces citation format: every claim must reference `[paper_id]`
  - Calls Hallucination Detector on complete answer
  - Memory: 512 MB, timeout: 60s
- [ ] **Lambda: Hallucination Detector**
  - Evidence Coverage Check — every claim has a supporting chunk
  - Citation Grounding — cited paper actually contains claimed info
  - Unsupported Claim Detection — flags claims with no evidence
  - Confidence Scoring (0.0–1.0):
    - >= 0.85 → PASS, return answer
    - 0.60–0.84 → WARN, add disclaimer
    - < 0.30 → REFUSE, do not return answer
  - Uses Bedrock Claude 3 Haiku for claim verification
  - Memory: 512 MB, timeout: 30s
- [ ] **GenerationStack CDK** — Lambda configs, Bedrock IAM, confidence thresholds as env vars
- [ ] Prompt templates for answer generation + claim verification
- [ ] Test suite: hallucination detection on synthetic grounded vs. hallucinated answers

---

## ⏳ DAY 6 — API Layer
**CDK Stack:** `ApiStack`  
**Files to create:** `cdk/stacks/api_stack.py`, `lambdas/response_api/`, `lambdas/websocket_handler/`

### What to build:
- [ ] **API Gateway REST** endpoints:
  - `POST /query` → Query Handler Lambda
  - `GET /papers/{id}` → Paper API Lambda (DynamoDB lookup)
  - `GET /papers/{id}/chunks` → Chunk API Lambda (OpenSearch lookup)
  - `GET /graph/entity/{name}` → Graph API Lambda (Neptune Gremlin)
  - `GET /graph/citation/{id}` → Citation Graph Lambda (Neptune)
  - `POST /evaluate` → Evaluator Lambda
- [ ] **API Gateway WebSocket** — `$connect`, `$disconnect`, `sendmessage` routes for streaming answers
- [ ] **Lambda: Response API** — formats final JSON response with answer + citations + confidence score + metadata
- [ ] **Lambda: WebSocket Handler** — manages connection IDs, pushes streamed tokens to connected clients
- [ ] **Cognito Authorizer** (optional) or API key authentication
- [ ] **ApiStack CDK** — API Gateway, Lambda integrations, CORS config, throttling, usage plans
- [ ] API contract documentation (OpenAPI spec)

---

## ⏳ DAY 7 — Frontend (React SPA)
**CDK Stack:** `FrontendStack`  
**Files to create:** `frontend/` (React app), `cdk/stacks/frontend_stack.py`

### What to build:
- [ ] **React SPA** with 4 main views:
  1. **Research Chat UI** — query input, streaming answer display, citation cards, confidence badge
  2. **Knowledge Graph Viewer** — interactive graph (D3.js or Sigma.js), entity search, citation network
  3. **Paper Viewer** — paper metadata, abstract, chunks, entity highlights
  4. **Evaluation Dashboard** — retrieval metrics, latency charts, hallucination rate over time
- [ ] **API integration** — connect to API Gateway REST + WebSocket endpoints
- [ ] **CloudFront Distribution** — OAC origin, HTTPS, gzip, custom domain (optional)
- [ ] **WAF** on CloudFront — rate limiting, bot protection
- [ ] **FrontendStack CDK** — S3 + CloudFront + WAF + ACM certificate
- [ ] Deploy script: build React → upload to S3 → invalidate CloudFront cache

---

## ⏳ DAY 8 — Evaluation Pipeline
**CDK Stack:** `EvaluationStack`  
**Files to create:** `cdk/stacks/evaluation_stack.py`, `lambdas/evaluator/`, `evaluation/golden_dataset/`

### What to build:
- [ ] **Golden Dataset** — 50 research questions with ground-truth answers + source papers
- [ ] **Lambda: Online Evaluator** — runs on every query, computes:
  - Retrieval precision@K, recall@K, NDCG
  - Answer ROUGE-L, BERTScore
  - Hallucination rate, confidence score distribution
  - End-to-end latency (P50, P95, P99)
- [ ] **Lambda: Offline Evaluator** — nightly batch run over full golden dataset
  - Triggered by EventBridge cron (`0 2 * * *`)
  - Writes results to S3 `research-evaluation/` bucket
- [ ] **CloudWatch Dashboard** — real-time metrics: papers/day, queries/day, hallucination rate, latency
- [ ] **CloudWatch Alarms**:
  - Hallucination rate > 15% → SNS alert
  - P95 latency > 5s → SNS alert
  - Ingestion DLQ depth > 10 → SNS alert
- [ ] **EvaluationStack CDK** — EventBridge cron, Lambda, S3 eval bucket, SNS topic

---

## ⏳ DAY 9 — Monitoring & Observability
**CDK Stack:** `MonitoringStack`  
**Files to create:** `cdk/stacks/monitoring_stack.py`

### What to build:
- [ ] **AWS X-Ray tracing** — end-to-end distributed trace across all Lambdas (query → retrieve → generate)
- [ ] **CloudWatch Log Insights queries** — saved queries for debugging ingestion failures, slow queries
- [ ] **CloudWatch Composite Alarms** — combined system health alarm
- [ ] **SNS Topic + Email/Slack alerts** for critical alarms
- [ ] **Lambda Powertools** integration — structured logging, custom metrics, X-Ray tracing via decorator in all Lambdas
- [ ] **Cost Anomaly Detection** — AWS Cost Anomaly Detector alert if spend spikes > 20%
- [ ] **CloudTrail** — audit log of all API calls (enabled at account level)
- [ ] **MonitoringStack CDK** — all CloudWatch resources, X-Ray groups, SNS

---

## ⏳ DAY 10 — Integration Testing, Hardening & Final Push
**No new CDK stack — testing + polish day**

### What to build:
- [ ] **End-to-end integration test** — trigger ingestion → wait for paper in OpenSearch → run query → verify answer with citations
- [ ] **Load test** — 100 concurrent queries, verify P95 < 3s
- [ ] **Chaos test** — kill Embedding Worker mid-batch, verify DLQ captures + retries succeed
- [ ] **SageMaker auto-scaling test** — verify reranker scales from 1 → 3 under load
- [ ] **Security review**:
  - All S3 buckets block public access
  - All Lambdas have least-privilege IAM
  - Neptune/OpenSearch in private subnets
  - All secrets in Secrets Manager (no hardcoded creds)
- [ ] **Final README update** — getting started guide, architecture diagram, FAQ
- [ ] **GitHub Actions CI/CD** — on push to `main`:
  - `cdk synth` (validate all stacks)
  - `pytest` (unit tests)
  - `cdk diff` (show what changed)
  - Manual approval gate → `cdk deploy --all`
- [ ] Git tag `v1.0.0` — first complete production release

---

## 🗂️ CDK Stack Deploy Order (Summary)

```
Day 1:   cdk deploy StorageStack
Day 2:   cdk deploy IngestionStack
Day 3:   cdk deploy EmbeddingStack GraphStack
Day 4:   cdk deploy RetrievalStack
Day 5:   cdk deploy GenerationStack
Day 6:   cdk deploy ApiStack
Day 7:   cdk deploy FrontendStack
Day 8:   cdk deploy EvaluationStack
Day 9:   cdk deploy MonitoringStack
Day 10:  Integration testing — no new deploy
```

---

## 📁 Final Folder Structure (After All 10 Days)

```
RESEARCH_DOMAIN_ENQUIRER/
├── BUILD_PLAN.md                   <- this file
├── README.md
├── ARCHITECTURE.md
├── INGESTION_PIPELINE.md
├── VECTOR_PIPELINE.md
├── GRAPH_PIPELINE.md
├── RETRIEVAL_ENGINE.md
├── HALLUCINATION_DETECTION.md
├── EVALUATION_PIPELINE.md
├── INFRASTRUCTURE.md
├── DATA_FLOW.md
├── FRONTEND.md
│
├── cdk/
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       ├── config.py
│       ├── storage_stack.py        (DONE - Day 1)
│       ├── ingestion_stack.py      (DONE - Day 2)
│       ├── embedding_stack.py      (NEXT - Day 3)
│       ├── graph_stack.py          (NEXT - Day 3)
│       ├── retrieval_stack.py      (Pending - Day 4)
│       ├── generation_stack.py     (Pending - Day 5)
│       ├── api_stack.py            (Pending - Day 6)
│       ├── frontend_stack.py       (Pending - Day 7)
│       ├── evaluation_stack.py     (Pending - Day 8)
│       └── monitoring_stack.py     (Pending - Day 9)
│
├── lambdas/
│   ├── paper_fetcher/              (DONE - Day 2)
│   ├── paper_processor/            (DONE - Day 2)
│   ├── docling_parser/             (stub Day 2 -> full container Day 3)
│   ├── embedding_worker/           (NEXT - Day 3)
│   ├── graph_builder/              (NEXT - Day 3)
│   ├── query_handler/              (Pending - Day 4)
│   ├── reranker/                   (Pending - Day 4)
│   ├── context_builder/            (Pending - Day 4)
│   ├── answer_generator/           (Pending - Day 5)
│   ├── hallucination_detector/     (Pending - Day 5)
│   ├── response_api/               (Pending - Day 6)
│   ├── websocket_handler/          (Pending - Day 6)
│   └── evaluator/                  (Pending - Day 8)
│
├── frontend/                       (Pending - Day 7)
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Chat.jsx
│   │   │   ├── GraphViewer.jsx
│   │   │   ├── PaperViewer.jsx
│   │   │   └── Dashboard.jsx
│   │   └── App.jsx
│   └── package.json
│
├── evaluation/                     (Pending - Day 8)
│   └── golden_dataset/
│
└── .github/
    └── workflows/
        └── ci_cd.yml               (Pending - Day 10)
```

---

*Last updated: Day 4 complete. Next: Day 5 — Generation & Hallucination Detection.*
