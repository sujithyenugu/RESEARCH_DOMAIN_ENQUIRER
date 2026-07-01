"""
handler.py — reranker Lambda

Entry point for the Reranker that receives top-50 RRF candidates from the
Query Handler, re-scores each (query, chunk) pair using the SageMaker
cross-encoder endpoint (cross-encoder/ms-marco-MiniLM-L-12-v2), selects
the top-10 highest-scoring chunks, then invokes the Context Builder Lambda
to assemble the final prompt.

Execution flow:
  1. Receive payload from Query Handler: { query, candidates: [...], options }
  2. Build (query, document_text) pairs for every candidate.
  3. Send pairs in batches of 20 to the SageMaker endpoint for scoring.
  4. Sort by cross-encoder score descending, keep top FINAL_TOP_K=10.
  5. Attach rerank_score to each selected chunk.
  6. Invoke Context Builder Lambda synchronously.
  7. Return Context Builder output (prompt + citations + metadata).

Triggered by: Query Handler Lambda (synchronous invoke)
Timeout:      30 s
Memory:       256 MB
VPC:          no (SageMaker + Lambda endpoints are regional, not VPC-bound)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
_AWS_REGION          = os.environ.get("AWS_REGION_NAME",         "us-east-1")
SAGEMAKER_ENDPOINT   = os.environ["SAGEMAKER_ENDPOINT_NAME"]     # cross-encoder-reranker-prod
RERANK_TOP_K         = int(os.environ.get("RERANK_TOP_K",        "50"))
FINAL_TOP_K          = int(os.environ.get("FINAL_TOP_K",         "10"))
CONTEXT_BUILDER_FN   = os.environ["CONTEXT_BUILDER_FN"]
CW_NAMESPACE         = os.environ.get("CW_NAMESPACE",            "ResearchRAG")

# Cross-encoder batch size — keep low to stay within SageMaker payload limits
_RERANKER_BATCH_SIZE = 20

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
sagemaker_rt  = boto3.client("sagemaker-runtime", region_name=_AWS_REGION)
lambda_client = boto3.client("lambda",            region_name=_AWS_REGION)
cw            = boto3.client("cloudwatch",        region_name=_AWS_REGION)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point — invoked synchronously by Query Handler.

    Parameters
    ----------
    event : dict
        Payload from Query Handler::

            {
              "query":      "How does LoRA compare to full fine-tuning?",
              "candidates": [
                {
                  "chunk_id":        "...",
                  "paper_id":        "...",
                  "text":            "...",
                  "section_title":   "...",
                  "published_date":  "2023-01-15",
                  "authors":         [...],
                  "rrf_score":       0.042,
                  "source":          "dense"
                },
                ...   # up to RERANK_TOP_K candidates
              ],
              "options":    {...}   # pass-through from original API request
            }

    context : LambdaContext
        Standard Lambda context.

    Returns
    -------
    dict
        Output of Context Builder Lambda, enriched with reranker metadata.
    """
    t_start = time.perf_counter()
    run_id  = f"rr_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    query:      str              = event.get("query",      "")
    candidates: list[dict]       = event.get("candidates", [])
    options:    dict[str, Any]   = event.get("options",    {})

    logger.info(
        "Reranker run %s | query=%r | candidates=%d",
        run_id, query[:80], len(candidates),
    )

    if not candidates:
        logger.warning("run=%s | no candidates — returning empty context", run_id)
        return _build_empty_response(query)

    # ------------------------------------------------------------------
    # Score each (query, chunk) pair via SageMaker cross-encoder
    # ------------------------------------------------------------------
    try:
        scored = _score_candidates(query, candidates)
    except Exception as exc:
        logger.error("SageMaker scoring failed: %s", exc, exc_info=True)
        # Fallback: rank by RRF score
        scored = [
            {**c, "rerank_score": c.get("rrf_score", 0.0)}
            for c in candidates
        ]

    # ------------------------------------------------------------------
    # Select top FINAL_TOP_K
    # ------------------------------------------------------------------
    top_chunks = sorted(scored, key=lambda c: c["rerank_score"], reverse=True)[:FINAL_TOP_K]

    logger.info(
        "run=%s | reranked %d candidates → top %d | top_score=%.4f bottom_score=%.4f",
        run_id,
        len(scored),
        len(top_chunks),
        top_chunks[0]["rerank_score"] if top_chunks else 0.0,
        top_chunks[-1]["rerank_score"] if top_chunks else 0.0,
    )

    # ------------------------------------------------------------------
    # Invoke Context Builder synchronously
    # ------------------------------------------------------------------
    cb_payload = {
        "query":     query,
        "chunks":    top_chunks,
        "options":   options,
    }
    try:
        cb_response = _invoke_lambda(CONTEXT_BUILDER_FN, cb_payload)
    except Exception as exc:
        logger.error("Context Builder invocation failed: %s", exc, exc_info=True)
        # Return scored chunks without a fully assembled prompt
        cb_response = {
            "answer_prompt":       _fallback_prompt(query, top_chunks),
            "chunks":              top_chunks,
            "citations":           _extract_citations(top_chunks),
            "context_builder_error": str(exc),
        }

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    _emit_metrics(cw, CW_NAMESPACE, {
        "reranker_duration_ms":     elapsed_ms,
        "candidates_scored":        len(scored),
        "final_chunks_selected":    len(top_chunks),
    })

    # Attach reranker metadata
    cb_response.setdefault("retrieval_metadata", {})
    cb_response["retrieval_metadata"].update({
        "reranker_ms":        elapsed_ms,
        "candidates_scored":  len(scored),
        "final_chunks":       len(top_chunks),
    })

    logger.info("run=%s | completed | elapsed_ms=%d", run_id, elapsed_ms)
    return cb_response


# ---------------------------------------------------------------------------
# SageMaker Scoring
# ---------------------------------------------------------------------------

def _score_candidates(
    query: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Score all (query, document) pairs using the SageMaker cross-encoder
    endpoint, processing in batches of _RERANKER_BATCH_SIZE to stay within
    the endpoint's payload limits.

    Scores are attached to each candidate dict as ``rerank_score``.
    Returns a flat list of candidate dicts (same order as input, now with scores).
    """
    all_scores: list[float] = []

    for i in range(0, len(candidates), _RERANKER_BATCH_SIZE):
        batch = candidates[i : i + _RERANKER_BATCH_SIZE]
        pairs = [
            {"query": query, "document": c.get("text", "")[:512]}
            for c in batch
        ]
        batch_scores = _call_sagemaker_endpoint(pairs)
        all_scores.extend(batch_scores)

    result: list[dict[str, Any]] = []
    for chunk, score in zip(candidates, all_scores):
        result.append({**chunk, "rerank_score": score})
    return result


def _call_sagemaker_endpoint(pairs: list[dict[str, str]]) -> list[float]:
    """
    Call the SageMaker cross-encoder endpoint with a list of (query, document)
    pairs and return a list of relevance scores (one per pair, range ≈ -10 to 10).

    The HuggingFace cross-encoder/ms-marco-MiniLM-L-12-v2 model is served via
    the text-classification pipeline, which returns logits directly.

    Returns a list of 0.0 scores on any error so the caller can fall back
    gracefully to RRF ordering.
    """
    payload = {"pairs": pairs}
    try:
        resp = sagemaker_rt.invoke_endpoint(
            EndpointName=SAGEMAKER_ENDPOINT,
            ContentType="application/json",
            Accept="application/json",
            Body=json.dumps(payload),
        )
        body = json.loads(resp["Body"].read())

        # The endpoint returns {"scores": [float, ...]} or a list of dicts
        if isinstance(body, dict) and "scores" in body:
            raw_scores = body["scores"]
        elif isinstance(body, list):
            # HF text-classification returns [{label, score}, ...]
            raw_scores = [
                item["score"] if isinstance(item, dict) else float(item)
                for item in body
            ]
        else:
            logger.warning("Unexpected SageMaker response format: %s", type(body))
            raw_scores = [0.0] * len(pairs)

        if len(raw_scores) != len(pairs):
            logger.warning(
                "Score count mismatch: expected %d got %d",
                len(pairs), len(raw_scores),
            )
            # Pad or truncate to match
            raw_scores = (raw_scores + [0.0] * len(pairs))[: len(pairs)]

        return [float(s) for s in raw_scores]

    except (ClientError, json.JSONDecodeError, KeyError) as exc:
        logger.error("SageMaker endpoint call failed: %s", exc)
        return [0.0] * len(pairs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke_lambda(function_name: str, payload: dict) -> dict[str, Any]:
    """Synchronously invoke a Lambda and return its parsed response payload."""
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    if resp.get("FunctionError"):
        raise RuntimeError(
            f"Lambda {function_name} returned FunctionError: "
            f"{resp['FunctionError']} | {resp['Payload'].read().decode()}"
        )
    return json.loads(resp["Payload"].read())


def _extract_citations(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a deduplicated citation list from a set of chunks."""
    seen: set[str] = set()
    citations: list[dict[str, Any]] = []
    for c in chunks:
        pid = c.get("paper_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            citations.append({
                "paper_id":       pid,
                "title":          c.get("title", ""),
                "authors":        c.get("authors", []),
                "published":      c.get("published_date", ""),
                "section":        c.get("section_title", ""),
                "relevance_score": c.get("rerank_score", 0.0),
            })
    return citations


def _fallback_prompt(query: str, chunks: list[dict[str, Any]]) -> str:
    """Minimal prompt assembly fallback when Context Builder is unavailable."""
    context_parts = [
        f"[{c.get('paper_id', '?')}] {c.get('text', '')[:500]}"
        for c in chunks
    ]
    return (
        "Answer the question using ONLY the context below. "
        "Cite papers using [paper_id] notation.\n\n"
        "CONTEXT:\n" + "\n\n".join(context_parts)
        + f"\n\nQUESTION: {query}\n\nANSWER:"
    )


def _build_empty_response(query: str) -> dict[str, Any]:
    """Return an empty/no-results response structure."""
    return {
        "answer_prompt":    f"QUESTION: {query}\n\nANSWER:",
        "chunks":           [],
        "citations":        [],
        "retrieval_metadata": {"candidates_scored": 0, "final_chunks": 0},
    }


def _emit_metrics(cw_client: Any, namespace: str, metrics: dict[str, float]) -> None:
    """Publish custom CloudWatch metrics. Non-fatal on failure."""
    timestamp = datetime.now(tz=timezone.utc)
    metric_data = [
        {"MetricName": name, "Value": float(val), "Unit": "Count", "Timestamp": timestamp}
        for name, val in metrics.items()
    ]
    try:
        cw_client.put_metric_data(Namespace=namespace, MetricData=metric_data)
    except ClientError as exc:
        logger.error("CloudWatch metric emission failed: %s", exc)
