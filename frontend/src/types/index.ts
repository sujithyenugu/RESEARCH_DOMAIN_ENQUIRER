// ============================================================
// Research Domain Enquirer — TypeScript Types
// ============================================================

// --- Chat / Query -------------------------------------------

export type ConfidenceLevel = 'high' | 'medium' | 'low' | 'refused';
export type ActionType = 'PASS' | 'PASS_WITH_DISCLAIMER' | 'WARN' | 'REFUSE';

export interface QueryRequest {
  query: string;
  topK: number;
  dateFrom?: string;
  dateTo?: string;
  categories?: string[];
  sessionId?: string;
}

export interface Citation {
  paperId: string;
  title: string;
  authors: string[];
  year: number;
  url: string;
  relevanceScore: number;
}

export interface EvidenceChunk {
  chunkId: string;
  paperId: string;
  paperTitle: string;
  section: string;
  text: string;
  relevanceScore: number;
  pageNumber?: number;
}

export type ClaimStatus = 'SUPPORTED' | 'PARTIALLY_SUPPORTED' | 'UNSUPPORTED' | 'CONTRADICTED';

export interface Claim {
  id: string;
  text: string;
  status: ClaimStatus;
  confidence: number;
  evidence?: string;
  sourceRef?: string;
}

export interface VerificationReport {
  overallConfidence: number;
  action: ActionType;
  claimsExtracted: number;
  supported: number;
  partiallySupported: number;
  unsupported: number;
  contradicted: number;
  evidenceCoverage: number;
  citationAccuracy: number;
  claims: Claim[];
}

export interface QueryResponse {
  queryId: string;
  query: string;
  answer: string;
  confidence: number;
  confidenceLevel: ConfidenceLevel;
  action: ActionType;
  citations: Citation[];
  chunks: EvidenceChunk[];
  verification: VerificationReport;
  latencyMs: number;
  timestamp: string;
}

// --- WebSocket frames ----------------------------------------

export interface WsTokenFrame {
  type: 'token';
  token: string;
  queryId: string;
}

export interface WsCompleteFrame {
  type: 'complete';
  queryId: string;
  response: QueryResponse;
}

export interface WsErrorFrame {
  type: 'error';
  message: string;
}

export type WsFrame = WsTokenFrame | WsCompleteFrame | WsErrorFrame;

// --- Knowledge Graph ----------------------------------------

export type NodeType = 'Paper' | 'Model' | 'Method' | 'Dataset' | 'Concept' | 'Benchmark';

export type EdgeType =
  | 'CITES'
  | 'INTRODUCES'
  | 'PROPOSES'
  | 'EVALUATES_ON'
  | 'AUTHORED_BY'
  | 'IMPROVES'
  | 'USES'
  | 'BASED_ON'
  | 'RELATED_TO'
  | 'PART_OF'
  | 'EXTENDS';

export interface GraphNode {
  id: string;
  label: string;
  type: NodeType;
  year?: number;
  properties?: Record<string, string | number | boolean>;
  // D3 simulation fields (added at runtime)
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
}

export interface GraphEdge {
  id: string;
  source: string | GraphNode;
  target: string | GraphNode;
  type: EdgeType;
  weight?: number;
  properties?: Record<string, string | number | boolean>;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface EntityDetails {
  id: string;
  name: string;
  type: NodeType;
  description?: string;
  year?: number;
  paperCount?: number;
  relatedEntities: { id: string; label: string; type: NodeType; relation: EdgeType }[];
  properties?: Record<string, string | number | boolean>;
}

// --- Papers -------------------------------------------------

export type EntityType = NodeType;

export interface PaperEntity {
  id: string;
  name: string;
  type: EntityType;
  role?: string;
}

export interface PaperChunk {
  chunkId: string;
  section: string;
  text: string;
  pageNumber?: number;
  relevanceScore: number;
}

export interface Paper {
  paperId: string;
  arxivId: string;
  title: string;
  authors: string[];
  publishedDate: string;
  categories: string[];
  abstract: string;
  pdfUrl?: string;
  entities: PaperEntity[];
  citationCount?: number;
  totalChunks?: number;
}

// --- Evaluation Dashboard -----------------------------------

export interface MetricValue {
  name: string;
  value: number;
  target: number;
  delta7d: number;
  pass: boolean;
}

export interface LatencyMetrics {
  p50: number;
  p90: number;
  p95: number;
  p99: number;
  timestamp: string;
}

export interface ConfidenceDistribution {
  high: number;
  medium: number;
  low: number;
  refused: number;
}

export interface RecallPoint {
  date: string;
  recall5: number;
  recall10: number;
}

export interface EvaluationMetrics {
  runId: string;
  timestamp: string;
  retrieval: {
    recall10: MetricValue;
    mrr: MetricValue;
    hitRate10: MetricValue;
    ndcg10: MetricValue;
  };
  generation: {
    faithfulness: MetricValue;
    groundedness: MetricValue;
    citationAccuracy: MetricValue;
    relevance: MetricValue;
  };
  latency: LatencyMetrics;
  confidenceDistribution: ConfidenceDistribution;
  recallTrend: RecallPoint[];
}

export interface IngestionStatus {
  papersIndexed: number;
  newToday: number;
  failedToday: number;
  entitiesInGraph: number;
  edgesInGraph: number;
  lastFetchUtc: string;
  nextFetchUtc: string;
  paperQueueDepth: number;
  dlqDepth: number;
  status: 'healthy' | 'degraded' | 'down';
}

// --- Chat History -------------------------------------------

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  response?: QueryResponse;
}

export interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: string;
  updatedAt: string;
}

// --- Filter state -------------------------------------------

export interface QueryFilters {
  topK: number;
  dateFrom: string;
  dateTo: string;
  categories: string[];
}
