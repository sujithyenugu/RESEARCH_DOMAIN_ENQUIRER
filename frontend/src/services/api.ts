// ============================================================
// API Service Layer
// ============================================================

import axios from 'axios';
import type {
  QueryRequest,
  QueryResponse,
  Paper,
  PaperChunk,
  GraphData,
  EntityDetails,
  EvaluationMetrics,
  IngestionStatus,
} from '../types';
import {
  MOCK_QUERY_RESPONSE,
  MOCK_GRAPH_DATA,
  MOCK_PAPER,
  MOCK_PAPER_CHUNKS,
  MOCK_ENTITY_DETAILS,
  MOCK_EVALUATION_METRICS,
  MOCK_INGESTION_STATUS,
} from '../mock/data';

const isMock = import.meta.env.VITE_USE_MOCK_API === 'true';
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api';

const sleep = (ms: number) => new Promise(res => setTimeout(res, ms));

const api = axios.create({
  baseURL: BASE_URL,
  headers: { 'Content-Type': 'application/json' },
});

// --- Interceptors -------------------------------------------

api.interceptors.response.use(
  res => res,
  err => {
    console.error('[API Error]', err.response?.data ?? err.message);
    return Promise.reject(err);
  }
);

// --- Query --------------------------------------------------

export async function submitQuery(req: QueryRequest): Promise<QueryResponse> {
  if (isMock) {
    await sleep(1800);
    return { ...MOCK_QUERY_RESPONSE, query: req.query };
  }
  const { data } = await api.post<QueryResponse>('/query', req);
  return data;
}

// --- Papers -------------------------------------------------

export async function getPaper(paperId: string): Promise<Paper> {
  if (isMock) {
    await sleep(600);
    return { ...MOCK_PAPER, paperId };
  }
  const { data } = await api.get<Paper>(`/papers/${paperId}`);
  return data;
}

export async function getPaperChunks(paperId: string): Promise<PaperChunk[]> {
  if (isMock) {
    await sleep(400);
    return MOCK_PAPER_CHUNKS;
  }
  const { data } = await api.get<PaperChunk[]>(`/papers/${paperId}/chunks`);
  return data;
}

export async function getPaperPdfUrl(paperId: string): Promise<string> {
  if (isMock) {
    return `https://arxiv.org/pdf/${paperId}`;
  }
  const { data } = await api.get<{ url: string }>(`/papers/${paperId}/pdf`);
  return data.url;
}

// --- Graph --------------------------------------------------

export async function getGraphEntity(entityName: string, hops = 2): Promise<GraphData> {
  if (isMock) {
    await sleep(700);
    return MOCK_GRAPH_DATA;
  }
  const { data } = await api.get<GraphData>(`/graph/entity/${encodeURIComponent(entityName)}`, {
    params: { hops },
  });
  return data;
}

export async function getGraphCitation(paperId: string): Promise<GraphData> {
  if (isMock) {
    await sleep(600);
    return MOCK_GRAPH_DATA;
  }
  const { data } = await api.get<GraphData>(`/graph/citation/${paperId}`);
  return data;
}

export async function getGraphSearch(query: string): Promise<{ id: string; label: string; type: string }[]> {
  if (isMock) {
    const q = query.toLowerCase();
    return MOCK_GRAPH_DATA.nodes
      .filter(n => n.label.toLowerCase().includes(q))
      .slice(0, 10)
      .map(n => ({ id: n.id, label: n.label, type: n.type }));
  }
  const { data } = await api.get<{ id: string; label: string; type: string }[]>('/graph/search', {
    params: { q: query },
  });
  return data;
}

export async function getEntityDetails(entityId: string): Promise<EntityDetails> {
  if (isMock) {
    await sleep(400);
    return { ...MOCK_ENTITY_DETAILS, id: entityId };
  }
  const { data } = await api.get<EntityDetails>(`/graph/entity/${entityId}/details`);
  return data;
}

// --- Evaluation ---------------------------------------------

export async function getEvaluationLatest(): Promise<EvaluationMetrics> {
  if (isMock) {
    await sleep(800);
    return MOCK_EVALUATION_METRICS;
  }
  const { data } = await api.get<EvaluationMetrics>('/evaluation/latest');
  return data;
}

export async function getEvaluationHistory(): Promise<EvaluationMetrics[]> {
  if (isMock) {
    await sleep(600);
    return [MOCK_EVALUATION_METRICS];
  }
  const { data } = await api.get<EvaluationMetrics[]>('/evaluation/history');
  return data;
}

export async function triggerEvaluation(): Promise<{ runId: string; status: string }> {
  if (isMock) {
    await sleep(1000);
    return { runId: `eval-${Date.now()}`, status: 'queued' };
  }
  const { data } = await api.post<{ runId: string; status: string }>('/evaluation/run');
  return data;
}

// --- Ingestion ----------------------------------------------

export async function getIngestionStatus(): Promise<IngestionStatus> {
  if (isMock) {
    await sleep(400);
    return MOCK_INGESTION_STATUS;
  }
  const { data } = await api.get<IngestionStatus>('/ingestion/status');
  return data;
}
