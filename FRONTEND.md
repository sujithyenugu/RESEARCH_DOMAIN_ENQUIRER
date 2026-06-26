# 🖥️ Frontend — Research Domain Enquirer

> Covers: UI components · API contracts · CloudFront config · WebSocket streaming · Knowledge Graph Viewer · Evaluation Dashboard

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Framework | React 18 + TypeScript |
| State | Zustand (lightweight, no Redux overhead) |
| API Client | React Query (caching, loading states) |
| Graph Viz | D3.js (force-directed knowledge graph) |
| Charts | Recharts (evaluation dashboard metrics) |
| PDF Viewer | react-pdf (paper viewer) |
| Styling | Tailwind CSS + shadcn/ui components |
| Build | Vite |
| Hosting | S3 (private) + CloudFront (OAC) |
| Auth | Amazon Cognito (optional, per org policy) |

---

## Pages & Components

```
src/
├── pages/
│   ├── ChatPage.tsx            ← Research Chat (main)
│   ├── PaperViewerPage.tsx     ← PDF + metadata viewer
│   ├── GraphViewerPage.tsx     ← Knowledge graph explorer
│   ├── EvaluationPage.tsx      ← Metrics dashboard
│   └── IngestionStatusPage.tsx ← Pipeline health
│
├── components/
│   ├── chat/
│   │   ├── QueryInput.tsx      ← Search bar with category filters
│   │   ├── AnswerCard.tsx      ← Answer with confidence badge
│   │   ├── CitationList.tsx    ← Cited papers with links
│   │   ├── ChunkViewer.tsx     ← Retrieved evidence chunks
│   │   ├── GraphContext.tsx    ← Mini graph for query entities
│   │   ├── ConfidenceBadge.tsx ← Color-coded confidence indicator
│   │   └── VerificationPanel.tsx ← Claim-by-claim verification
│   │
│   ├── graph/
│   │   ├── KnowledgeGraph.tsx  ← D3 force-directed graph
│   │   ├── EntityNode.tsx      ← Colored node by entity type
│   │   ├── RelationshipEdge.tsx ← Labeled directed edges
│   │   ├── GraphControls.tsx   ← Zoom, filter, search
│   │   └── EntitySidebar.tsx   ← Selected entity details
│   │
│   ├── paper/
│   │   ├── PaperMetadata.tsx   ← Title, authors, abstract
│   │   ├── PDFViewer.tsx       ← Embedded PDF with page nav
│   │   ├── ChunkHighlighter.tsx ← Highlight retrieved chunks in PDF
│   │   └── CitationGraph.tsx   ← Paper's citation neighborhood
│   │
│   └── eval/
│       ├── MetricCard.tsx      ← Single metric with trend
│       ├── RecallChart.tsx     ← Recall@K over time
│       ├── LatencyChart.tsx    ← P50/P95/P99 latency
│       ├── FaithfulnessGauge.tsx ← Gauge chart
│       └── EvalRunTable.tsx    ← Historical eval runs
```

---

## Chat Page — Main UI

```
┌──────────────────────────────────────────────────────────────────────┐
│  🔬 Research Domain Enquirer                          [Filters ▼] [⚙] │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  How does LoRA compare to full fine-tuning on LLMs?    [Search] │ │
│  │  📅 From: 2022 · 📁 cs.LG, cs.CL · 🔢 Top-10 chunks           │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  ANSWER          ✅ High Confidence (0.91)  🔗 4 papers cited   │ │
│  │  ────────────────────────────────────────────────────────────── │ │
│  │  LoRA (Low-Rank Adaptation) significantly reduces memory         │ │
│  │  requirements compared to full fine-tuning [2106.09685].         │ │
│  │  Specifically, LoRA reduces trainable parameters by up to        │ │
│  │  10,000× while achieving comparable performance on GLUE and      │ │
│  │  SuperGLUE benchmarks [2106.09685]...                            │ │
│  │                                                                  │ │
│  │  [Show Verification ▼]  [Copy Answer]  [Export Citations]        │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌──────────────────────────┐  ┌──────────────────────────────────┐  │
│  │  CITATIONS (4)            │  │  EVIDENCE CHUNKS (10)            │  │
│  │  ─────────────────────── │  │  ──────────────────────────────  │  │
│  │  [1] LoRA: Low-Rank...   │  │  [1.0] LoRA: Abstract ★★★★★    │  │
│  │      Hu et al. (2021)    │  │  "LoRA reduces trainable..."     │  │
│  │      📄 View Paper       │  │                                  │  │
│  │                          │  │  [0.87] Full FT Survey: §3 ★★★★ │  │
│  │  [2] Full FT vs PEFT...  │  │  "Full fine-tuning updates..."   │  │
│  │      Chen et al. (2024)  │  │                                  │  │
│  │      📄 View Paper       │  │  [0.81] LLaMA: Experiments ★★★★ │  │
│  │                          │  │  "We compare LoRA vs FFT on..."  │  │
│  └──────────────────────────┘  └──────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  KNOWLEDGE GRAPH CONTEXT                                         │ │
│  │                                                                  │ │
│  │   [LoRA] ──IMPROVES──► [GLUE Benchmark]                         │ │
│  │      │                                                           │ │
│  │   BASED_ON                                                       │ │
│  │      ▼                                                           │ │
│  │   [Transformer]  ◄──PROPOSES── [Paper: 2106.09685]              │ │
│  │                                                                  │ │
│  │  [Open Full Graph Viewer]                                        │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Knowledge Graph Viewer

### D3.js Force-Directed Graph

```
┌──────────────────────────────────────────────────────────────────────┐
│  🕸️ Knowledge Graph Explorer                    [Search entity...]   │
│  ──────────────────────────────────────────────────────────────────  │
│  Filter: [✓ Papers] [✓ Models] [✓ Methods] [✓ Datasets] [Concepts]  │
│          Depth: [2] hops  Year: [2020 ──────────── 2024]             │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│           ●── CITES ──► ●                                             │
│         Paper          Paper                                          │
│           │                         ●  Dataset                       │
│        PROPOSES         IMPROVES ──►●  Benchmark                     │
│           ▼                                                           │
│           ● Method ──USES──► ●  Dataset                              │
│                                                                       │
│  [Node Colors]                   [Edge Labels]                        │
│  🔵 Paper    🟢 Model            → CITES  → INTRODUCES               │
│  🟡 Method   🔴 Dataset          → PROPOSES → IMPROVES               │
│  🟣 Concept  ⚪ Benchmark         → USES  → BASED_ON                 │
│                                                                       │
├──────────────────────────────────────────────────────────────────────┤
│  Selected: [LoRA — Method]                                            │
│  ─────────────────────────────────────────────────────────────────── │
│  Introduced in: LoRA: Low-Rank Adaptation... (2021) [View Paper]     │
│  Used in: 247 papers                                                  │
│  Improves: GLUE (+2.3), SuperGLUE (+1.8), MT-Bench (+0.4)           │
│  Based on: Transformer architecture                                   │
│  Related methods: QLoRA, AdaLoRA, DoRA                               │
└──────────────────────────────────────────────────────────────────────┘
```

### Graph Controls

- **Zoom:** scroll wheel, pinch gesture
- **Pan:** click + drag
- **Node click:** open entity sidebar
- **Node double-click:** expand neighborhood (fetch from API)
- **Edge click:** show context (what paper asserted this relationship)
- **Filter panel:** toggle vertex types, date range slider
- **Search:** type entity name, highlight + center on node
- **Export:** download as SVG or JSON

---

## Paper Viewer Page

```
┌──────────────────────────────────────────────────────────────────────┐
│  ← Back   LoRA: Low-Rank Adaptation of Large Language Models        │
├─────────────────────────────┬────────────────────────────────────────┤
│  METADATA                   │  PDF VIEWER                            │
│  ─────────────────────────  │  ──────────────────────────────────── │
│  Authors:                   │  ┌────────────────────────────────┐   │
│    Hu, Edward J.             │  │                                │   │
│    Shen, Yelong              │  │   [PDF Content Rendered]       │   │
│    Wallis, Phillip           │  │                                │   │
│                             │  │   ██████ Highlighted chunk     │   │
│  Published: Oct 16, 2021    │  │   from query result ██████     │   │
│  arXiv: 2106.09685          │  │                                │   │
│  Categories: cs.CL, cs.LG   │  └────────────────────────────────┘   │
│                             │  Page 1 of 20  [◄] [►]               │
│  Abstract:                  │                                        │
│  We propose Low-Rank        │  RETRIEVED CHUNKS FROM THIS PAPER (3) │
│  Adaptation, or LoRA...     │  ────────────────────────────────────  │
│                             │  [Abstract p.1] Relevance: ★★★★★    │
│  ENTITIES IN PAPER (12)     │  "We propose Low-Rank Adaptation..."  │
│  ─────────────────────────  │                                        │
│  🟡 LoRA (Method)           │  [Experiments p.6] Relevance: ★★★★  │
│  🔵 GPT-3 (Model)           │  "Table 1 shows LoRA matches full..." │
│  🔴 GLUE (Dataset)          │                                        │
│  ⚪ HumanEval (Benchmark)   │  [Conclusion p.18] Relevance: ★★★   │
│  ....                       │  "In this work we show that LoRA..."  │
│                             │                                        │
│  CITATION GRAPH             │                                        │
│  [Mini D3 graph here]       │                                        │
└─────────────────────────────┴────────────────────────────────────────┘
```

---

## Evaluation Dashboard

```
┌──────────────────────────────────────────────────────────────────────┐
│  📊 Evaluation Dashboard           Last run: 2024-01-15 02:14 UTC   │
│                               [Run Evaluation Now]  [Compare ▼]     │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  RETRIEVAL METRICS                                                    │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐  │
│  │ Recall@10    │ │ MRR          │ │ Hit Rate@10  │ │ nDCG@10    │  │
│  │  0.84        │ │  0.79        │ │  0.92        │ │  0.81      │  │
│  │ ▲+0.03 (7d)  │ │ ▼-0.01 (7d) │ │ ▲+0.02 (7d)  │ │ ▲+0.01(7d)│  │
│  │ Target: 0.80 │ │ Target: 0.75 │ │ Target: 0.90 │ │Target:0.75 │  │
│  │ ✅ PASS       │ │ ✅ PASS       │ │ ✅ PASS       │ │ ✅ PASS    │  │
│  └──────────────┘ └──────────────┘ └──────────────┘ └────────────┘  │
│                                                                       │
│  GENERATION METRICS                                                   │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐  │
│  │ Faithfulness │ │ Groundedness │ │ Citation Acc │ │ Relevance  │  │
│  │  0.91        │ │  0.85        │ │  0.97        │ │  0.88      │  │
│  │ ▲+0.02 (7d)  │ │ ▲+0.03 (7d) │ │ ─ 0.00 (7d)  │ │ ▲+0.05(7d)│  │
│  │ Target: 0.85 │ │ Target: 0.80 │ │ Target: 0.95 │ │Target:0.80 │  │
│  │ ✅ PASS       │ │ ✅ PASS       │ │ ✅ PASS       │ │ ✅ PASS    │  │
│  └──────────────┘ └──────────────┘ └──────────────┘ └────────────┘  │
│                                                                       │
│  LATENCY (real-time)                   CONFIDENCE DISTRIBUTION        │
│  ┌───────────────────────────────┐    ┌──────────────────────────┐   │
│  │  ████ P50: 2.1s              │    │  High (≥0.85):   73%     │   │
│  │  ██████ P90: 3.8s            │    │  Medium (0.6-0.85): 21%  │   │
│  │  ████████ P95: 4.2s          │    │  Low (<0.60):     5%     │   │
│  │  ██████████ P99: 7.8s        │    │  Refused:         1%     │   │
│  └───────────────────────────────┘    └──────────────────────────┘   │
│                                                                       │
│  RECALL@K TREND (14 days)                                            │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │ 1.0 ─                                                            │ │
│  │ 0.9 ─       ●──●──●──●──●──●──●──●──● Recall@10               │ │
│  │ 0.8 ─  ●──●                           Recall@5                 │ │
│  │ 0.7 ─                                                           │ │
│  │     Jan 1  Jan 3  Jan 5  Jan 7  Jan 9  Jan 11  Jan 13  Jan 15   │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  INGESTION STATUS                                                     │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │ Papers in index: 8,742    |  New today: 147  |  Failed: 2    │   │
│  │ Entities in graph: 24,891 |  Edges: 187,342                  │   │
│  │ Last fetch: 12:00 UTC     |  Next: 18:00 UTC                 │   │
│  │ Paper Queue depth: 0      |  DLQ depth: 2  [View DLQ Items]  │   │
│  └───────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Verification Panel (Hallucination Details)

When user clicks "Show Verification" on an answer:

```
┌─────────────────────────────────────────────────────────────────┐
│  HALLUCINATION VERIFICATION REPORT         Confidence: 0.91 ✅  │
│  ───────────────────────────────────────────────────────────── │
│                                                                 │
│  Claims Extracted: 4                                            │
│  ├── ✅ SUPPORTED:          3  (75%)                           │
│  ├── ⚠️  PARTIALLY SUPPORTED: 1  (25%)                           │
│  ├── ❌ UNSUPPORTED:         0                                   │
│  └── 🚫 CONTRADICTED:        0                                   │
│                                                                 │
│  Evidence Coverage: 85% (8 of 10 chunks cited)                  │
│  Citation Accuracy: 100% (all [paper_id]s valid)                │
│                                                                 │
│  ─────────────────────────────────────────────────────────────  │
│  Claim 1: ✅ SUPPORTED (confidence: 0.98)                       │
│  "LoRA reduces trainable parameters by approximately 10,000×"   │
│  Evidence: "LoRA reduces the number of trainable parameters     │
│  by 10,000 times..." — [2106.09685] Abstract                    │
│                                                                 │
│  Claim 2: ✅ SUPPORTED (confidence: 0.94)                       │
│  "LoRA matches full fine-tuning quality on GLUE (GPT-3)"        │
│  Evidence: "LoRA matches or exceeds the quality of full         │
│  fine-tuning on GLUE..." — [2106.09685] §4 Experiments          │
│                                                                 │
│  Claim 3: ⚠️ PARTIALLY SUPPORTED (confidence: 0.72)              │
│  "LoRA matches full fine-tuning on SuperGLUE"                   │
│  Note: GLUE evidence present, SuperGLUE not directly quoted     │
│                                                                 │
│  Claim 4: ✅ SUPPORTED (confidence: 0.96)                       │
│  "Weight updates have a low intrinsic rank"                     │
│  Evidence: "we hypothesize that weight updates during           │
│  adaptation also have a low intrinsic rank..." — [2106.09685]   │
└─────────────────────────────────────────────────────────────────┘
```

---

## CloudFront Configuration

| Property | Value |
|----------|-------|
| Distribution | Single distribution for SPA + API |
| Origin 1 | S3 (OAC) → serves `/` and static assets |
| Origin 2 | API Gateway → serves `/api/*` |
| Origin 3 | API Gateway WebSocket → serves `/ws` |
| Default TTL | 86400s (24h) for static assets |
| Dynamic TTL | 0s for `/api/*` (no cache) |
| Compression | Gzip + Brotli enabled |
| HTTPS | Redirect HTTP → HTTPS |
| TLS version | TLSv1.2_2021 |
| Security headers | HSTS, X-Content-Type-Options, X-Frame-Options |
| WAF | Rate limit 1000 req/5min per IP |
| Custom domain | `research.yourdomain.com` (ACM cert) |
| IPv6 | Enabled |

### Cache Behaviors

| Path Pattern | Origin | Cache TTL |
|-------------|--------|-----------|
| `/api/*` | API Gateway | 0 (no cache) |
| `/ws` | API GW WebSocket | 0 |
| `/assets/*` | S3 | 31536000s (1 year) |
| `/*.js` | S3 | 86400s |
| `/*.css` | S3 | 86400s |
| `/*` (default) | S3 | 3600s |

---

## API Endpoints Used by Frontend

| Page | Endpoint | Method | Description |
|------|----------|--------|-------------|
| Chat | `/api/query` | POST | Submit research question |
| Chat | `/api/ws` | WS | Stream answer tokens |
| Paper Viewer | `/api/papers/{id}` | GET | Paper metadata + entities |
| Paper Viewer | `/api/papers/{id}/chunks` | GET | Retrieved chunks for paper |
| Paper Viewer | `/api/papers/{id}/pdf` | GET | Pre-signed S3 URL for PDF |
| Graph Viewer | `/api/graph/entity/{name}` | GET | Entity neighborhood (N hops) |
| Graph Viewer | `/api/graph/citation/{id}` | GET | Citation graph for paper |
| Graph Viewer | `/api/graph/search` | GET | Search entities by name |
| Eval Dashboard | `/api/evaluation/latest` | GET | Latest eval run metrics |
| Eval Dashboard | `/api/evaluation/history` | GET | Historical eval runs |
| Eval Dashboard | `/api/evaluation/run` | POST | Trigger manual eval run |
| Ingestion Status | `/api/ingestion/status` | GET | Pipeline health + queue depths |
| Ingestion Status | `/api/papers` | GET | List papers (paginated) |

---

## Deployment to S3 + CloudFront

```bash
# Build the SPA
npm run build  # outputs to dist/

# Upload to S3
aws s3 sync dist/ s3://research-frontend/ \
  --delete \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "index.html"

aws s3 cp dist/index.html s3://research-frontend/index.html \
  --cache-control "public, max-age=3600"

# Invalidate CloudFront cache
aws cloudfront create-invalidation \
  --distribution-id YOUR_DIST_ID \
  --paths "/*"
```

---

*For full architecture, see [ARCHITECTURE.md](./ARCHITECTURE.md). For retrieval design, see [RETRIEVAL_ENGINE.md](./RETRIEVAL_ENGINE.md).*
