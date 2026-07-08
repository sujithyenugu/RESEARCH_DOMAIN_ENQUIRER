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

## ✅ DAY 5 — Generation & Hallucination Detection
**CDK Stack:** `GenerationStack`  
**Commit:** `Day 5 updated commit`  
**Files:** `cdk/stacks/generation_stack.py`, `lambdas/answer_generator/`, `lambdas/hallucination_detector/`

### What was built:
- [x] **Lambda: Answer Generator** (`lambdas/answer_generator/handler.py`)
  - Calls Bedrock Claude 3.5 Sonnet (`converse_stream`) with assembled context + query
  - Streams answer tokens (collected in-memory; ready for WebSocket delivery Day 6)
  - Enforces citation format: every claim must reference `[paper_id]`
  - Calls Hallucination Detector Lambda synchronously on complete answer
  - Builds final response payload (query, answer, action, confidence, citations, metadata)
  - Memory: 512 MB, timeout: 60s
- [x] **Lambda: Hallucination Detector** (`lambdas/hallucination_detector/handler.py`)
  - Step 1 — Atomic claim extraction via Claude 3 Haiku (quantitative / comparative / causal / definitional / existence)
  - Step 2 — Evidence mapping: citation match + entity overlap + keyword fallback
  - Step 3 — Per-claim verification via Claude 3 Haiku (SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED / CONTRADICTED)
  - Step 4 — Evidence coverage analysis (fraction of retrieved chunks cited)
  - Step 5 — Weighted confidence score (60% claim score + 25% coverage + 15% citation accuracy)
  - Citation grounding check — every `[paper_id]` in answer verified against context
  - Response gate: ≥0.85→PASS · 0.60–0.85→PASS_WITH_DISCLAIMER · 0.30–0.60→WARN · <0.30→REFUSE
  - Emits CloudWatch metrics: ConfidenceScore, EvidenceCoverage, CitationAccuracy, ContradictedClaims
  - Memory: 512 MB, timeout: 30s
- [x] **Prompt templates** (`lambdas/answer_generator/prompts.py`) — centralised system prompts + builder functions for generation, claim extraction, and claim verification
- [x] **GenerationStack CDK** (`cdk/stacks/generation_stack.py`) — both Lambda configs, Bedrock IAM (Claude 3.5 Sonnet + Claude 3 Haiku), cross-function invocation grant, CloudWatch log groups (30-day retention), X-Ray tracing, CFN outputs
- [x] **Test suite** (`tests/test_generation_hallucination.py`) — 18 unit tests covering: evidence mapping, citation accuracy, confidence scoring, coverage analysis, response gating, prompt builders, response payload assembly, and E2E handler smoke tests
- [x] `cdk/app.py` updated — GenerationStack wired with `add_dependency(retrieval)`

---

## ✅ DAY 6 — API Layer
**CDK Stack:** `ApiStack`  
**Commit:** `Day 6 updated commit`  
**Files:** `cdk/stacks/api_stack.py`, `lambdas/response_api/`, `lambdas/websocket_handler/`, `tests/test_api_layer.py`

### What was built:
- [x] **API Gateway REST** endpoints (6 routes):
  - `POST /query` → Query Handler Lambda
  - `GET /papers/{id}` → Response API Lambda (DynamoDB lookup)
  - `GET /papers/{id}/chunks` → Response API Lambda (OpenSearch lookup)
  - `GET /graph/entity/{name}` → Response API Lambda (Neptune Gremlin)
  - `GET /graph/citation/{id}` → Response API Lambda (Neptune)
  - `POST /evaluate` → Response API Lambda (Evaluator stub)
- [x] **API Gateway WebSocket** — `$connect`, `$disconnect`, `sendmessage` routes for streaming answers
- [x] **Lambda: Response API** (`lambdas/response_api/handler.py`) — formats final JSON envelope: answer + citations + confidence block + metadata, action-gated HTTP status codes (200/422), CORS headers, CloudWatch metrics
- [x] **Lambda: WebSocket Handler** (`lambdas/websocket_handler/handler.py`) — manages connection IDs in DynamoDB (TTL), pushes streamed token/done/error frames via API Gateway Management API, `push_streaming_tokens` helper, GoneException cleanup
- [x] **DynamoDB table** — `research-ws-connections` (TTL-enabled, on-demand, PITR)
- [x] **IAM roles** (least-privilege per Lambda) — DynamoDB R/W, Secrets Manager, OpenSearch ESHttp, `execute-api:ManageConnections`
- [x] **CORS** — allow all origins with method + header controls
- [x] **Throttling** — 100 rps burst / 50 rps steady, Usage Plan 10k/day quota
- [x] **API Key** — `research-api-key` in Usage Plan
- [x] **ApiStack CDK** (`cdk/stacks/api_stack.py`) — all constructs, CFN outputs: RestApiUrl, WebSocketApiUrl, function names
- [x] **Test suite** (`tests/test_api_layer.py`) — 38 unit tests: envelope building, citation normalisation, confidence normalisation, HTTP formatting, connect/disconnect DynamoDB, sendmessage routing, frame shapes, GoneException cleanup, push_streaming_tokens streaming + abort + empty
- [x] `cdk/app.py` updated — ApiStack wired with `add_dependency(generation)`

---

## ✅ DAY 7 — Frontend (React SPA)
**CDK Stack:** `FrontendStack`  
**Commit:** `Day 7 updated commit`  
**Files:** `cdk/stacks/frontend_stack.py`, `frontend/` (React SPA), `frontend/deploy.ps1`, `frontend/deploy.sh`

### What was built:
- [x] **React SPA** with 4 main views:
  1. **Research Chat UI** (`frontend/src/pages/ChatPage.tsx`) — query input, streaming answer display, citation cards, confidence badge, verification panel, knowledge graph context
  2. **Knowledge Graph Viewer** (`frontend/src/pages/GraphViewerPage.tsx`) — interactive D3.js force-directed graph, entity search, citation network, entity sidebar
  3. **Paper Viewer** (`frontend/src/pages/PaperViewerPage.tsx`) — paper metadata, abstract, entities, retrieved chunks, mini citation graph, PDF viewer placeholder
  4. **Evaluation Dashboard** (`frontend/src/pages/EvaluationPage.tsx`) — retrieval metrics, latency charts, confidence distribution, recall trend, ingestion status
- [x] **API integration** (`frontend/src/services/api.ts`) — connects to API Gateway REST endpoints, mock mode (`VITE_USE_MOCK_API=true`) for local dev
- [x] **WebSocket manager** (`frontend/src/services/websocket.ts`) — real-time token streaming + mock stream simulation
- [x] **Zustand state stores** (`frontend/src/store/index.ts`) — ChatStore, SidebarStore, GraphStore
- [x] **TypeScript types** (`frontend/src/types/index.ts`) — all data models (Query, Citation, Paper, Graph, Evaluation)
- [x] **Rich mock data** (`frontend/src/mock/data.ts`) — full mock responses for all API endpoints
- [x] **Design system CSS** (`frontend/src/index.css`) — dark mode, glassmorphism, gradient accents, micro-animations
- [x] **Components**: chat (QueryInput, AnswerCard, CitationList, ChunkViewer, GraphContext, ConfidenceBadge, VerificationPanel), graph (KnowledgeGraph, GraphControls, EntitySidebar), eval (MetricCard, LatencyChart, RecallChart), layout (Sidebar)
- [x] **CloudFront Distribution** — OAC origin, HTTPS, gzip+brotli, SPA error routing (403/404 → index.html)
- [x] **WAF** on CloudFront — rate limiting (1000 req/5min), AWSManagedRulesCommonRuleSet, AWSManagedRulesKnownBadInputsRuleSet
- [x] **FrontendStack CDK** (`cdk/stacks/frontend_stack.py`) — S3 + CloudFront + WAF + OAC + BucketDeployment + SSM params, CFN outputs
- [x] **Deploy scripts** — `frontend/deploy.ps1` (Windows) + `frontend/deploy.sh` (Linux/macOS): build → S3 sync → CloudFront invalidation
- [x] **SEO meta tags** — title, description, OG tags, Twitter Card in `index.html`
- [x] **Production build verified** — `tsc && vite build` succeeded, 1,326 modules → `dist/`
- [x] `cdk/app.py` updated — FrontendStack wired with `add_dependency(api)`

---

## ✅ DAY 8 — Evaluation Pipeline
**CDK Stack:** `EvaluationStack`  
**Commit:** `Day 8 updated commit`  
**Files:** `cdk/stacks/evaluation_stack.py`, `lambdas/evaluator/`, `evaluation/golden_dataset/`, `tests/test_evaluation_pipeline.py`

### What was built:
- [x] **Golden Dataset** — 50 research Q&A pairs across 7 categories in `evaluation/golden_dataset/golden_qa_v1.json`
- [x] **Annotation Guidelines** — `evaluation/golden_dataset/annotation_guidelines.md`
- [x] **Lambda: Online Evaluator** (`lambdas/evaluator/handler.py::online_handler`)
  - Invoked per query — latency, confidence, citation accuracy, chunk count
  - Publishes 10+ CloudWatch metrics per query (per-stage latency + scores)
  - Memory: 256 MB, timeout: 30s
- [x] **Lambda: Offline Evaluator** (`lambdas/evaluator/handler.py::offline_handler`)
  - Triggered by EventBridge cron (`0 2 * * ? *`)
  - Runs full RAG pipeline for each golden question via Query Handler Lambda
  - Computes: Recall@5, Recall@10, MRR, Hit Rate@10, nDCG@10, Faithfulness, Groundedness, Citation Accuracy, Answer Relevance
  - Writes results to S3 `research-evaluation/` bucket
  - Writes aggregated metrics to DynamoDB `EvalHistory` table
  - Regression detection vs 7-day baseline with SNS alerting
- [x] **Metrics module** (`lambdas/evaluator/metrics.py`) — pure functions, fully unit-testable
- [x] **CloudWatch Dashboard** — `ResearchRAG-Evaluation` with 4 rows: retrieval, generation, latency, ingestion
- [x] **CloudWatch Alarms** (6 alarms):
  - Recall@10 < 0.70 for 2 eval runs → SNS alert
  - Faithfulness < 0.75 daily eval → SNS alert
  - E2E P95 latency > 8s (5min) → SNS alert
  - Citation accuracy < 0.90 → SNS alert
  - Confidence avg < 0.65 (1h) → SNS alert
  - Refusal rate > 15% (1h) → SNS alert
- [x] **DynamoDB Table** — `EvalHistory` (GSI: date-index for trend queries)
- [x] **SNS Topic** — `research-eval-alerts` for regression alerts
- [x] **EventBridge cron rule** — daily 02:00 UTC → Offline Evaluator
- [x] **EvaluationStack CDK** (`cdk/stacks/evaluation_stack.py`) — all constructs, IAM grants (Bedrock, S3, DynamoDB, Lambda invoke), CFN outputs
- [x] **Test suite** (`tests/test_evaluation_pipeline.py`) — 38 unit tests: all retrieval metrics, all generation metrics, percentile, aggregate, regression detection, online handler, offline handler smoke, CloudWatch batching
- [x] `cdk/app.py` updated — EvaluationStack wired with `add_dependency(frontend)`

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

*Last updated: Day 8 complete. Next: Day 9 — Monitoring & Observability.*
