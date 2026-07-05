"""
handler.py — response_api Lambda

Formats the raw output from the Answer Generator into a standardised JSON
response envelope suitable for delivery to API Gateway REST clients.

Execution flow:
  1. Receive payload from API Gateway or direct invocation:
       { query, answer, action, confidence, citations, metadata, ... }
  2. Validate and normalise all fields.
  3. Enrich with server-side metadata (request_id, timestamps, version).
  4. Return a properly shaped HTTP response (status code + JSON body).

Triggered by: API Gateway POST /query (via query_handler invoke chain) OR
              direct synchronous invocation from Answer Generator.
Timeout:      30 s
Memory:       256 MB
VPC:          no
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
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
# Environment variables (injected by CDK ApiStack)
# ---------------------------------------------------------------------------
API_VERSION    = os.environ.get("API_VERSION", "1.0")
CW_NAMESPACE   = os.environ.get("CW_NAMESPACE", "ResearchRAG")
SERVICE_NAME   = os.environ.get("SERVICE_NAME", "response-api")

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
cloudwatch = boto3.client("cloudwatch")

# ---------------------------------------------------------------------------
# Response gating labels → HTTP status codes
# ---------------------------------------------------------------------------
_ACTION_STATUS_MAP: dict[str, int] = {
    "PASS":                 200,
    "PASS_WITH_DISCLAIMER": 200,
    "WARN":                 200,
    "REFUSE":               422,   # Unprocessable — low-confidence refusal
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CURRENT_VERSION = API_VERSION


# ===========================================================================
# Public helpers (also used by tests)
# ===========================================================================

def build_citation_list(citations_raw: list[dict]) -> list[dict]:
    """Normalise citation objects from the upstream payload.

    Each citation is expected to have at minimum:
      paper_id, title, authors, year

    Returns a cleaned list with deterministic field ordering and safe defaults.
    """
    normalised: list[dict] = []
    for idx, c in enumerate(citations_raw or []):
        normalised.append(
            {
                "index":    idx + 1,
                "paper_id": str(c.get("paper_id", "")),
                "title":    str(c.get("title", "Unknown Title")),
                "authors":  c.get("authors", []),
                "year":     int(c.get("year", 0)) if c.get("year") else None,
                "venue":    c.get("venue", None),
                "url":      c.get("url", None),
                "score":    round(float(c.get("score", 0.0)), 4),
            }
        )
    return normalised


def build_confidence_block(
    confidence: float,
    action: str,
    claim_score: float | None = None,
    evidence_coverage: float | None = None,
    citation_accuracy: float | None = None,
) -> dict:
    """Construct the structured confidence sub-object for the response."""
    return {
        "overall":           round(float(confidence), 4),
        "action":            action,
        "claim_score":       round(float(claim_score), 4) if claim_score is not None else None,
        "evidence_coverage": round(float(evidence_coverage), 4) if evidence_coverage is not None else None,
        "citation_accuracy": round(float(citation_accuracy), 4) if citation_accuracy is not None else None,
    }


def build_metadata_block(
    query: str,
    request_id: str,
    pipeline_latency_ms: float | None,
    chunk_count: int,
    model_id: str,
    api_version: str,
) -> dict:
    """Construct the structured metadata sub-object for the response."""
    return {
        "request_id":          request_id,
        "api_version":         api_version,
        "model":               model_id,
        "query_length_chars":  len(query),
        "chunks_retrieved":    chunk_count,
        "pipeline_latency_ms": round(pipeline_latency_ms, 1) if pipeline_latency_ms is not None else None,
        "timestamp_utc":       int(time.time()),
    }


def build_response_envelope(
    *,
    query: str,
    answer: str,
    action: str,
    confidence: float,
    citations: list[dict],
    hallucination_detail: dict | None,
    pipeline_latency_ms: float | None,
    chunk_count: int,
    model_id: str,
    request_id: str,
    disclaimer: str | None = None,
) -> dict:
    """Assemble the full API response envelope.

    Returns the *body* dict (caller wraps in statusCode + headers as needed).
    """
    hd = hallucination_detail or {}

    confidence_block = build_confidence_block(
        confidence=confidence,
        action=action,
        claim_score=hd.get("claim_score"),
        evidence_coverage=hd.get("evidence_coverage"),
        citation_accuracy=hd.get("citation_accuracy"),
    )

    meta_block = build_metadata_block(
        query=query,
        request_id=request_id,
        pipeline_latency_ms=pipeline_latency_ms,
        chunk_count=chunk_count,
        model_id=model_id,
        api_version=_CURRENT_VERSION,
    )

    body: dict[str, Any] = {
        "query":      query,
        "answer":     answer,
        "confidence": confidence_block,
        "citations":  build_citation_list(citations),
        "metadata":   meta_block,
    }

    # Attach disclaimer for PASS_WITH_DISCLAIMER / WARN
    if action in ("PASS_WITH_DISCLAIMER", "WARN") and disclaimer:
        body["disclaimer"] = disclaimer
    elif action in ("PASS_WITH_DISCLAIMER", "WARN"):
        body["disclaimer"] = (
            "This answer is based on retrieved research context. "
            "Some claims may have limited evidentiary support."
        )

    # For REFUSE: replace answer with a safe refusal message
    if action == "REFUSE":
        body["answer"] = (
            "I cannot provide a high-confidence answer to this query based on "
            "the available research context. Please try rephrasing or narrowing "
            "your question."
        )
        body["disclaimer"] = (
            "The response was withheld due to insufficient evidence confidence."
        )

    return body


def format_http_response(body: dict, action: str, request_id: str) -> dict:
    """Wrap body dict in an API Gateway-compatible HTTP response object."""
    status_code = _ACTION_STATUS_MAP.get(action, 200)
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "X-Request-Id":                request_id,
            "X-API-Version":               _CURRENT_VERSION,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key",
            "Access-Control-Allow-Methods": "OPTIONS,POST,GET",
        },
        "body": json.dumps(body),
    }


# ===========================================================================
# CloudWatch metrics
# ===========================================================================

def _emit_metrics(action: str, confidence: float, latency_ms: float | None) -> None:
    """Emit response API metrics to CloudWatch (best-effort, non-blocking)."""
    try:
        metric_data = [
            {
                "MetricName": "ResponseCount",
                "Dimensions": [
                    {"Name": "Service", "Value": SERVICE_NAME},
                    {"Name": "Action",  "Value": action},
                ],
                "Value": 1,
                "Unit":  "Count",
            },
            {
                "MetricName": "ConfidenceScore",
                "Dimensions": [{"Name": "Service", "Value": SERVICE_NAME}],
                "Value": round(confidence, 4),
                "Unit":  "None",
            },
        ]
        if latency_ms is not None:
            metric_data.append(
                {
                    "MetricName": "PipelineLatencyMs",
                    "Dimensions": [{"Name": "Service", "Value": SERVICE_NAME}],
                    "Value": latency_ms,
                    "Unit":  "Milliseconds",
                }
            )
        cloudwatch.put_metric_data(Namespace=CW_NAMESPACE, MetricData=metric_data)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("CloudWatch metric emission failed (non-fatal): %s", exc)


# ===========================================================================
# Lambda handler
# ===========================================================================

def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point for the Response API formatter.

    Accepted event shapes:
      (a) Direct invoke from Answer Generator:
          { query, answer, action, confidence, citations, metadata, hallucination_detail }

      (b) API Gateway proxy event (in case of direct wiring):
          { body: "<json string>", requestContext: { requestId: "..." } }

    Returns:
      API Gateway proxy-compatible response:
      { statusCode, headers, body }
    """
    t_start = time.monotonic()

    # ------------------------------------------------------------------
    # 1. Parse input
    # ------------------------------------------------------------------
    # Handle API Gateway proxy format (body is a JSON string)
    if "body" in event and isinstance(event["body"], str):
        try:
            payload: dict = json.loads(event["body"])
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON body: %s", exc)
            return {
                "statusCode": 400,
                "headers":    {"Content-Type": "application/json"},
                "body":       json.dumps({"error": "Invalid JSON body"}),
            }
    else:
        payload = event

    # Extract request ID
    request_id = (
        event.get("requestContext", {}).get("requestId")
        or payload.get("request_id")
        or str(uuid.uuid4())
    )

    logger.info("ResponseAPI invoked | request_id=%s", request_id)

    # ------------------------------------------------------------------
    # 2. Extract and validate required fields
    # ------------------------------------------------------------------
    query   = str(payload.get("query",   ""))
    answer  = str(payload.get("answer",  ""))
    action  = str(payload.get("action",  "WARN")).upper()
    confidence_raw = payload.get("confidence", 0.0)
    citations      = payload.get("citations",  [])

    # Normalise confidence — accept float or nested dict
    if isinstance(confidence_raw, dict):
        confidence = float(confidence_raw.get("overall", confidence_raw.get("score", 0.0)))
    else:
        confidence = float(confidence_raw)

    if not query:
        logger.warning("Empty query field in payload")

    if action not in _ACTION_STATUS_MAP:
        logger.warning("Unrecognised action '%s' — defaulting to WARN", action)
        action = "WARN"

    # ------------------------------------------------------------------
    # 3. Extract optional enrichment fields
    # ------------------------------------------------------------------
    hallucination_detail = payload.get("hallucination_detail") or {}
    pipeline_meta        = payload.get("metadata") or {}
    pipeline_latency_ms  = float(pipeline_meta.get("pipeline_latency_ms", 0)) or None
    chunk_count          = int(pipeline_meta.get("chunks_retrieved", len(citations)))
    model_id             = str(pipeline_meta.get("model", "anthropic.claude-3-5-sonnet-20241022-v2:0"))
    disclaimer           = payload.get("disclaimer")

    # ------------------------------------------------------------------
    # 4. Build response envelope
    # ------------------------------------------------------------------
    body = build_response_envelope(
        query=query,
        answer=answer,
        action=action,
        confidence=confidence,
        citations=citations if isinstance(citations, list) else [],
        hallucination_detail=hallucination_detail,
        pipeline_latency_ms=pipeline_latency_ms,
        chunk_count=chunk_count,
        model_id=model_id,
        request_id=request_id,
        disclaimer=disclaimer,
    )

    elapsed_ms = (time.monotonic() - t_start) * 1000
    logger.info(
        "ResponseAPI complete | request_id=%s action=%s confidence=%.3f latency_ms=%.1f",
        request_id, action, confidence, elapsed_ms,
    )

    # ------------------------------------------------------------------
    # 5. Emit CloudWatch metrics (best-effort)
    # ------------------------------------------------------------------
    _emit_metrics(action=action, confidence=confidence, latency_ms=pipeline_latency_ms)

    # ------------------------------------------------------------------
    # 6. Return API Gateway proxy-compatible response
    # ------------------------------------------------------------------
    return format_http_response(body=body, action=action, request_id=request_id)
