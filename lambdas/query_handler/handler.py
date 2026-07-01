"""
handler.py — query_handler Lambda

Entry point for the Query Handler that receives POST /query requests from
API Gateway, orchestrates the full retrieval pipeline, and returns structured
results via the Reranker → Context Builder chain.

Execution flow:
  1. Parse and validate the incoming API Gateway request body.
  2. Stage 1 — Query Understanding:
       a. Classify query intent + extract entities (Bedrock Claude 3 Haiku).
       b. Optionally generate a Hypothetical Document Embedding (HyDE) snippet.
       c. Embed the HyDE/query text via Bedrock Titan Embeddings V2.
       d. Expand BM25 query string with entity synonyms.
  3. Stage 2 — Parallel Retrieval (asyncio.gather):
       a. Dense KNN search on OpenSearch `paper_chunks` index (k=30).
       b. BM25 multi_match search on OpenSearch (k=30).
       c. Neptune Gremlin graph expansion on extracted entities (k=20).
  4. Stage 3 — Reciprocal Rank Fusion (RRF) of all three result lists.
  5. Stage 4 — Invoke Reranker Lambda (synchronous) with top-50 RRF candidates.
  6. Return the Reranker response (which contains context-assembled prompt +
     citations + retrieval metadata) directly to API Gateway.

Triggered by: API Gateway POST /query
Timeout:      30 s
Memory:       512 MB
VPC:          yes (OpenSearch + Neptune are inside the VPC)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
import requests
from requests_aws4auth import AWS4Auth
from gremlin_python.driver import client as gremlin_client, serializer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# Environment variables (all injected by CDK)
# ---------------------------------------------------------------------------
_AWS_REGION         = os.environ.get("AWS_REGION_NAME", "us-east-1")
OPENSEARCH_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"]    # https://vpc-xxx...es.amazonaws.com
OPENSEARCH_INDEX    = os.environ.get("OPENSEARCH_INDEX",   "paper_chunks")
NEPTUNE_ENDPOINT    = os.environ["NEPTUNE_ENDPOINT"]        # xxx.cluster.us-east-1.neptune.amazonaws.com
NEPTUNE_PORT        = int(os.environ.get("NEPTUNE_PORT",   "8182"))
EMBEDDING_MODEL_ID  = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
HYDE_MODEL_ID       = os.environ.get("HYDE_MODEL_ID",      "anthropic.claude-3-haiku-20240307-v1:0")
DENSE_K             = int(os.environ.get("DENSE_K",         "30"))
BM25_K              = int(os.environ.get("BM25_K",          "30"))
GRAPH_K             = int(os.environ.get("GRAPH_K",          "20"))
RERANK_TOP_K        = int(os.environ.get("RERANK_TOP_K",    "50"))
HYDE_ENABLED        = os.environ.get("HYDE_ENABLED", "True").lower() == "true"
RERANKER_FN         = os.environ["RERANKER_FN"]             # lambda function name
CW_NAMESPACE        = os.environ.get("CW_NAMESPACE",        "ResearchRAG")

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
bedrock          = boto3.client("bedrock-runtime", region_name=_AWS_REGION)
lambda_client    = boto3.client("lambda",          region_name=_AWS_REGION)
cw               = boto3.client("cloudwatch",      region_name=_AWS_REGION)

# OpenSearch SigV4 auth
_credentials = boto3.Session().get_credentials().get_frozen_credentials()
_OS_AUTH = AWS4Auth(
    _credentials.access_key,
    _credentials.secret_key,
    _AWS_REGION,
    "es",
    session_token=_credentials.token,
)
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({"Content-Type": "application/json"})

# RRF constant (standard default is 60)
_RRF_K = 60

# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point — triggered by API Gateway POST /query.

    Parameters
    ----------
    event : dict
        API Gateway proxy event.  The body must be a JSON string with at minimum::

            {
              "question": "How does LoRA compare to full fine-tuning?",
              "filters": {          # optional
                "date_from": "2022-01-01",
                "categories": ["cs.LG", "cs.CL"],
                "top_k": 10
              },
              "options": {          # optional
                "stream": false,
                "include_graph": true,
                "include_evaluation": false
              }
            }

    context : LambdaContext
        Standard Lambda context object.

    Returns
    -------
    dict
        API Gateway proxy response with statusCode and JSON body.
        On success the body mirrors the Reranker / Context Builder output::

            {
              "answer_prompt": "...",      # assembled prompt string
              "chunks": [...],             # top-K chunk dicts
              "citations": [...],
              "retrieval_metadata": {...}
            }
    """
    t_start = time.perf_counter()
    run_id  = f"qh_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    logger.info("Query handler run %s | event keys=%s", run_id, list(event.keys()))

    # ------------------------------------------------------------------
    # Parse request
    # ------------------------------------------------------------------
    try:
        body = json.loads(event.get("body") or "{}")
        question: str = body["question"].strip()
        if not question:
            raise ValueError("question field is empty")
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Bad request: %s", exc)
        return _api_error(400, f"Invalid request body: {exc}")

    filters: dict[str, Any] = body.get("filters", {})
    options: dict[str, Any] = body.get("options", {})

    logger.info("run=%s | question=%r | filters=%s | options=%s",
                run_id, question[:120], filters, options)

    # ------------------------------------------------------------------
    # Stage 1 — Query Understanding
    # ------------------------------------------------------------------
    try:
        query_ctx = _query_understanding(question, filters)
    except Exception as exc:
        logger.error("Query understanding failed: %s", exc, exc_info=True)
        return _api_error(500, "Query understanding failed")

    logger.info(
        "run=%s | hyde_snippet_len=%d | entities=%s | bm25_query=%r",
        run_id,
        len(query_ctx.get("hyde_text", "")),
        query_ctx.get("entities", []),
        query_ctx.get("bm25_query", "")[:100],
    )

    # ------------------------------------------------------------------
    # Stage 2 — Parallel Retrieval
    # ------------------------------------------------------------------
    try:
        dense_chunks, bm25_chunks, graph_chunks = asyncio.get_event_loop().run_until_complete(
            _retrieve_parallel(query_ctx, filters, options)
        )
    except Exception as exc:
        logger.error("Parallel retrieval failed: %s", exc, exc_info=True)
        return _api_error(500, "Retrieval failed")

    logger.info(
        "run=%s | dense=%d bm25=%d graph=%d",
        run_id, len(dense_chunks), len(bm25_chunks), len(graph_chunks),
    )

    # ------------------------------------------------------------------
    # Stage 3 — Reciprocal Rank Fusion
    # ------------------------------------------------------------------
    dense_ids = [c["chunk_id"] for c in dense_chunks]
    bm25_ids  = [c["chunk_id"] for c in bm25_chunks]
    graph_ids = [c["chunk_id"] for c in graph_chunks]

    rrf_scores = _reciprocal_rank_fusion([dense_ids, bm25_ids, graph_ids])

    # Build lookup by chunk_id from all three sources (dense takes precedence)
    all_chunks_by_id: dict[str, dict] = {}
    for chunk in graph_chunks:
        chunk["source"] = "graph"
        all_chunks_by_id[chunk["chunk_id"]] = chunk
    for chunk in bm25_chunks:
        chunk.setdefault("source", "bm25")
        all_chunks_by_id[chunk["chunk_id"]] = chunk
    for chunk in dense_chunks:
        chunk["source"] = "dense"
        all_chunks_by_id[chunk["chunk_id"]] = chunk

    # Order by RRF score, keep top RERANK_TOP_K
    ranked_ids   = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
    top_candidates = [
        {**all_chunks_by_id[cid], "rrf_score": rrf_scores[cid]}
        for cid in ranked_ids[:RERANK_TOP_K]
        if cid in all_chunks_by_id
    ]

    logger.info("run=%s | rrf_candidates=%d", run_id, len(top_candidates))

    # ------------------------------------------------------------------
    # Stage 4 — Invoke Reranker Lambda (synchronous)
    # ------------------------------------------------------------------
    try:
        reranker_payload = {
            "query": question,
            "candidates": top_candidates,
            "options": options,
        }
        reranker_response = _invoke_lambda(RERANKER_FN, reranker_payload)
    except Exception as exc:
        logger.error("Reranker invocation failed: %s", exc, exc_info=True)
        return _api_error(500, "Reranker failed")

    # ------------------------------------------------------------------
    # Emit metrics
    # ------------------------------------------------------------------
    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    _emit_metrics(cw, CW_NAMESPACE, {
        "query_handler_duration_ms":   elapsed_ms,
        "dense_candidates_returned":   len(dense_chunks),
        "bm25_candidates_returned":    len(bm25_chunks),
        "graph_candidates_returned":   len(graph_chunks),
        "rrf_candidates_total":        len(top_candidates),
    })

    logger.info("run=%s | completed | elapsed_ms=%d", run_id, elapsed_ms)

    # Attach retrieval metadata so downstream components can log it
    reranker_response.setdefault("retrieval_metadata", {})
    reranker_response["retrieval_metadata"].update({
        "dense_candidates":  len(dense_chunks),
        "bm25_candidates":   len(bm25_chunks),
        "graph_candidates":  len(graph_chunks),
        "reranked_from":     len(top_candidates),
        "query_handler_ms":  elapsed_ms,
    })

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(reranker_response),
    }


# ---------------------------------------------------------------------------
# Stage 1: Query Understanding
# ---------------------------------------------------------------------------

def _query_understanding(question: str, filters: dict) -> dict[str, Any]:
    """
    Classify the query, extract entities, generate a HyDE snippet, embed it
    via Bedrock Titan V2, and build an expanded BM25 query string.

    Returns a context dict with keys:
        hyde_text      : str   — hypothetical document snippet
        query_embedding: list  — 1536-dim float vector
        entities       : list  — extracted entity strings
        bm25_query     : str   — expanded query for OpenSearch multi_match
        intent         : str   — classification label
    """
    # Step A — classify + extract entities via Claude 3 Haiku
    classification = _classify_query(question)
    entities: list[str]  = classification.get("entities", [])
    intent:   str        = classification.get("intent",   "general")
    hyde_text: str       = classification.get("hyde_snippet", question)

    # Step B — HyDE: embed the hypothetical document snippet
    embed_text = hyde_text if (HYDE_ENABLED and hyde_text) else question
    query_embedding = _embed_text(embed_text)

    # Step C — BM25 query expansion
    expanded_terms = [question] + entities
    bm25_query = " ".join(expanded_terms)

    return {
        "hyde_text":       hyde_text,
        "query_embedding": query_embedding,
        "entities":        entities,
        "bm25_query":      bm25_query,
        "intent":          intent,
    }


def _classify_query(question: str) -> dict[str, Any]:
    """
    Call Claude 3 Haiku to classify the query intent, extract entities, and
    generate a short hypothetical document snippet (HyDE).

    Returns a dict with 'intent', 'entities', and 'hyde_snippet'.
    Falls back gracefully on any Bedrock error.
    """
    system_prompt = (
        "You are a research assistant AI. Given a research question, respond "
        "with a JSON object with these keys:\n"
        "  intent: one of [comparison, survey, method_explanation, benchmark, general]\n"
        "  entities: list of key technical terms, model names, datasets, concepts\n"
        "  hyde_snippet: a 3-5 sentence hypothetical research paper excerpt that "
        "would perfectly answer this question. Be specific with numbers and citations style.\n"
        "Respond ONLY with the JSON object, no markdown."
    )
    user_msg = f"Research question: {question}"

    try:
        resp = bedrock.invoke_model(
            modelId=HYDE_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 600,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_msg}],
            }),
        )
        result_text = json.loads(resp["body"].read())["content"][0]["text"]
        # Strip possible markdown code fences
        result_text = result_text.strip().strip("```json").strip("```").strip()
        return json.loads(result_text)
    except Exception as exc:
        logger.warning("Query classification failed (using fallback): %s", exc)
        return {
            "intent":       "general",
            "entities":     [],
            "hyde_snippet": question,
        }


def _embed_text(text: str) -> list[float]:
    """
    Embed a text string via Bedrock Titan Embeddings V2.
    Returns a 1536-dim float vector.
    Falls back to zero vector on error.
    """
    try:
        resp = bedrock.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text[:8000]}),  # Titan V2 max input
        )
        return json.loads(resp["body"].read())["embedding"]
    except Exception as exc:
        logger.error("Bedrock embedding failed: %s", exc)
        return [0.0] * 1536


# ---------------------------------------------------------------------------
# Stage 2: Parallel Retrieval
# ---------------------------------------------------------------------------

async def _retrieve_parallel(
    query_ctx: dict[str, Any],
    filters: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Run dense KNN, BM25, and Neptune graph expansion concurrently via asyncio.

    Returns
    -------
    tuple[list[dict], list[dict], list[dict]]
        (dense_chunks, bm25_chunks, graph_chunks)
    """
    loop = asyncio.get_event_loop()

    dense_fut = loop.run_in_executor(
        None,
        lambda: _dense_search(query_ctx["query_embedding"], filters),
    )
    bm25_fut = loop.run_in_executor(
        None,
        lambda: _bm25_search(query_ctx["bm25_query"], filters),
    )
    # Graph expansion is optional — only run if include_graph is not explicitly False
    include_graph = options.get("include_graph", True)
    graph_fut = loop.run_in_executor(
        None,
        lambda: _graph_expansion(query_ctx["entities"]) if include_graph else [],
    )

    dense_result, bm25_result, graph_result = await asyncio.gather(
        dense_fut, bm25_fut, graph_fut,
        return_exceptions=True,
    )

    def _safe(result: Any, label: str) -> list[dict]:
        if isinstance(result, Exception):
            logger.error("%s retrieval failed: %s", label, result)
            return []
        return result or []

    return (
        _safe(dense_result, "dense"),
        _safe(bm25_result,  "bm25"),
        _safe(graph_result, "graph"),
    )


def _dense_search(
    query_embedding: list[float],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Dense KNN search on OpenSearch paper_chunks index.

    Returns up to DENSE_K chunk dicts, each with chunk_id, paper_id, text,
    section_title, published_date, authors, entities, and dense_score.
    """
    body: dict[str, Any] = {
        "size": DENSE_K,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": DENSE_K,
                }
            }
        },
        "_source": [
            "chunk_id", "paper_id", "text", "section_title",
            "published_date", "authors", "entities", "section_id",
        ],
    }

    # Optionally filter by date / category
    filter_clauses: list[dict] = []
    if "date_from" in filters:
        filter_clauses.append({
            "range": {"published_date": {"gte": filters["date_from"]}}
        })
    if "categories" in filters and filters["categories"]:
        filter_clauses.append({
            "terms": {"categories": filters["categories"]}
        })
    if filter_clauses:
        body["post_filter"] = {"bool": {"must": filter_clauses}}

    return _run_opensearch_query(body, score_field="dense_score")


def _bm25_search(
    bm25_query: str,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    BM25 multi-match full-text search on OpenSearch paper_chunks index.

    Returns up to BM25_K chunk dicts with bm25_score attached.
    """
    must_clauses: list[dict] = [
        {
            "multi_match": {
                "query":                bm25_query,
                "fields":               ["text^2", "title^3", "abstract^1.5", "entities^2.5"],
                "type":                 "best_fields",
                "minimum_should_match": "1",
            }
        }
    ]

    filter_clauses: list[dict] = []
    if "date_from" in filters:
        filter_clauses.append({
            "range": {"published_date": {"gte": filters["date_from"]}}
        })
    if "categories" in filters and filters["categories"]:
        filter_clauses.append({
            "terms": {"categories": filters["categories"]}
        })

    body: dict[str, Any] = {
        "size": BM25_K,
        "query": {
            "bool": {
                "must":   must_clauses,
                "filter": filter_clauses,
            }
        },
        "_source": [
            "chunk_id", "paper_id", "text", "section_title",
            "published_date", "authors", "entities", "section_id",
        ],
    }

    return _run_opensearch_query(body, score_field="bm25_score")


def _run_opensearch_query(
    body: dict[str, Any],
    score_field: str,
) -> list[dict[str, Any]]:
    """
    Execute an OpenSearch query and return a list of chunk dicts with a
    ``score_field`` key containing the raw OpenSearch score.
    """
    url = f"https://{OPENSEARCH_ENDPOINT.rstrip('/')}/{OPENSEARCH_INDEX}/_search"
    try:
        resp = _HTTP_SESSION.post(
            url,
            data=json.dumps(body),
            auth=_OS_AUTH,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenSearch query failed: %s", exc)
        return []

    chunks = []
    for hit in data.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        source["chunk_id"]   = source.get("chunk_id") or hit["_id"]
        source[score_field]  = hit.get("_score", 0.0)
        chunks.append(source)
    return chunks


def _graph_expansion(entities: list[str]) -> list[dict[str, Any]]:
    """
    Neptune Gremlin graph expansion — finds papers related to the query
    entities through graph traversal (depth=2) and fetches their top chunks
    from OpenSearch.

    Returns up to GRAPH_K chunk dicts with source="graph".
    """
    if not entities:
        return []

    neptune_url = f"wss://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
    try:
        gc = gremlin_client.Client(
            neptune_url,
            "g",
            message_serializer=serializer.GraphSONSerializersV2d0(),
        )
    except Exception as exc:
        logger.warning("Could not connect to Neptune: %s", exc)
        return []

    try:
        # Find papers that PROPOSE, INTRODUCE, or EVALUATE_ON these entities
        quoted = [f"'{e}'" for e in entities[:10]]  # cap entity list
        gremlin_query = (
            f"g.V().has('name', within({','.join(quoted)}))"
            f".in('PROPOSES','INTRODUCES','EVALUATES_ON')"
            f".hasLabel('Paper')"
            f".dedup()"
            f".order().by('published_date', decr)"
            f".limit({GRAPH_K})"
            f".valueMap('paper_id','title','published_date')"
        )
        results = gc.submit(gremlin_query).all().result(timeout=8)
    except Exception as exc:
        logger.warning("Gremlin traversal failed: %s", exc)
        return []
    finally:
        try:
            gc.close()
        except Exception:
            pass

    if not results:
        return []

    # Fetch top-3 chunks per related paper from OpenSearch
    paper_ids = [
        r.get("paper_id", [None])[0]
        for r in results
        if r.get("paper_id")
    ]
    paper_ids = [p for p in paper_ids if p]

    if not paper_ids:
        return []

    body = {
        "size": GRAPH_K,
        "query": {
            "terms": {"paper_id": paper_ids[:GRAPH_K]}
        },
        "sort":     [{"_score": "desc"}],
        "_source":  [
            "chunk_id", "paper_id", "text", "section_title",
            "published_date", "authors", "entities", "section_id",
        ],
    }
    chunks = _run_opensearch_query(body, score_field="graph_score")
    for c in chunks:
        c["source"] = "graph"
    return chunks


# ---------------------------------------------------------------------------
# Stage 3: Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def _reciprocal_rank_fusion(
    rankings: list[list[str]],
    k: int = _RRF_K,
) -> dict[str, float]:
    """
    Combine ranked lists of chunk_ids into a single RRF score map.

    Parameters
    ----------
    rankings : list[list[str]]
        Ordered lists of chunk_ids from each retrieval source
        (dense, bm25, graph).
    k : int
        RRF constant (standard default 60).

    Returns
    -------
    dict[str, float]
        Mapping of chunk_id → RRF score, sorted descending.
    """
    scores: dict[str, float] = defaultdict(float)
    for ranked_list in rankings:
        for rank, chunk_id in enumerate(ranked_list, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke_lambda(function_name: str, payload: dict) -> dict[str, Any]:
    """
    Synchronously invoke another Lambda function and return its parsed response.

    Raises RuntimeError on invocation error or non-200 status code from callee.
    """
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    if resp.get("FunctionError"):
        raise RuntimeError(
            f"Lambda {function_name} returned FunctionError: "
            f"{resp['FunctionError']} — {resp['Payload'].read().decode()}"
        )
    return json.loads(resp["Payload"].read())


def _emit_metrics(cw_client: Any, namespace: str, metrics: dict[str, float]) -> None:
    """Publish custom CloudWatch metrics. Failures are logged but not re-raised."""
    timestamp = datetime.now(tz=timezone.utc)
    metric_data = [
        {"MetricName": name, "Value": float(val), "Unit": "Count", "Timestamp": timestamp}
        for name, val in metrics.items()
    ]
    try:
        cw_client.put_metric_data(Namespace=namespace, MetricData=metric_data)
    except ClientError as exc:
        logger.error("CloudWatch metric emission failed: %s", exc)


def _api_error(status_code: int, message: str) -> dict[str, Any]:
    """Return an API Gateway error response dict."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({"error": message}),
    }
