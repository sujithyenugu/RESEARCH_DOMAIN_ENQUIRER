"""
handler.py — answer_generator Lambda

Entry point for the Answer Generator that receives the assembled context +
query prompt from Context Builder, calls Bedrock Claude 3.5 Sonnet to produce
a cited, grounded answer, then invokes the Hallucination Detector before
returning the final response payload.

Execution flow:
  1. Receive payload from Context Builder:
       { query, assembled_prompt, context_chunks, options }
  2. Call Bedrock Claude 3.5 Sonnet with the assembled prompt (streaming).
  3. Collect full streamed response, extract answer text.
  4. Invoke Hallucination Detector Lambda (synchronous) with:
       { answer, query, context_chunks }
  5. Return verified + gated response payload.

Triggered by: Reranker Lambda → Context Builder → Answer Generator (synchronous invoke chain)
Timeout:      60 s
Memory:       512 MB
VPC:          no (Bedrock + Lambda VPC endpoint via NAT or PrivateLink)
"""

from __future__ import annotations

import json
import logging
import os
import time
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
# Environment variables (injected by CDK GenerationStack)
# ---------------------------------------------------------------------------
BEDROCK_GENERATION_MODEL   = os.environ.get(
    "BEDROCK_GENERATION_MODEL", "anthropic.claude-3-5-sonnet-20241022-v2:0"
)
BEDROCK_REGION             = os.environ.get("BEDROCK_REGION", "us-east-1")
HALLUCINATION_DETECTOR_FN  = os.environ.get("HALLUCINATION_DETECTOR_FUNCTION_NAME", "")
CW_NAMESPACE               = os.environ.get("CW_NAMESPACE", "ResearchRAG")
LOG_LEVEL_ENV              = os.environ.get("LOG_LEVEL", "INFO")

# Generation parameters
MAX_TOKENS    = int(os.environ.get("MAX_TOKENS", "2048"))
TEMPERATURE   = float(os.environ.get("TEMPERATURE", "0.1"))
TOP_P         = float(os.environ.get("TOP_P", "0.9"))

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
_bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
_lambda_client   = boto3.client("lambda")
_cloudwatch      = boto3.client("cloudwatch")


# ---------------------------------------------------------------------------
# System prompt for answer generation
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a rigorous AI research assistant. Your task is to answer questions about AI research using ONLY the provided research paper excerpts.

Rules you MUST follow:
1. Every factual claim MUST be followed immediately by a citation in [paper_id] format (e.g., [2401.12345]).
2. Only use information explicitly present in the provided context chunks.
3. Do NOT speculate, infer beyond what is written, or introduce external knowledge.
4. If multiple papers support a claim, cite all of them: [paper_id_1][paper_id_2].
5. If the context does not contain enough information to answer, say so clearly.
6. Structure your answer with clear paragraphs. Use bullet points for comparisons or lists.
7. Quantitative results (accuracy %, parameter counts, latency) must be cited exactly as stated in the source.

Citation format: [arxiv_id] — e.g., [2401.12345] or [2106.09685]

Answer in a professional, academic tone suitable for a researcher audience."""


# ---------------------------------------------------------------------------
# Helper: emit a CloudWatch metric
# ---------------------------------------------------------------------------
def _put_metric(metric_name: str, value: float, unit: str = "Count") -> None:
    """Emit a single CW metric. Best-effort — does not raise on failure."""
    try:
        _cloudwatch.put_metric_data(
            Namespace=CW_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit metric %s: %s", metric_name, exc)


# ---------------------------------------------------------------------------
# Helper: call Bedrock Converse API (streaming)
# ---------------------------------------------------------------------------
def _generate_answer_streaming(assembled_prompt: str) -> tuple[str, dict]:
    """
    Calls Bedrock Claude 3.5 Sonnet using the Converse stream API.

    Returns:
        answer_text  — the full generated answer string
        usage        — token usage dict { input_tokens, output_tokens }
    """
    logger.info(
        "Calling Bedrock model=%s max_tokens=%d", BEDROCK_GENERATION_MODEL, MAX_TOKENS
    )

    try:
        response = _bedrock_runtime.converse_stream(
            modelId=BEDROCK_GENERATION_MODEL,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": assembled_prompt}]}],
            inferenceConfig={
                "maxTokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "topP": TOP_P,
            },
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        logger.error("Bedrock converse_stream error %s: %s", error_code, exc)
        raise

    # Collect streamed chunks
    answer_parts: list[str] = []
    usage: dict[str, int] = {}

    stream = response.get("stream")
    if stream:
        for event in stream:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    answer_parts.append(delta["text"])
            elif "metadata" in event:
                meta = event["metadata"]
                if "usage" in meta:
                    usage = meta["usage"]

    answer_text = "".join(answer_parts).strip()
    logger.info(
        "Generation complete. input_tokens=%s output_tokens=%s answer_chars=%d",
        usage.get("inputTokens", "?"),
        usage.get("outputTokens", "?"),
        len(answer_text),
    )
    return answer_text, usage


# ---------------------------------------------------------------------------
# Helper: invoke Hallucination Detector Lambda
# ---------------------------------------------------------------------------
def _invoke_hallucination_detector(
    answer: str,
    query: str,
    context_chunks: list[dict],
) -> dict:
    """
    Synchronously invokes the Hallucination Detector Lambda.

    Returns the full verification result dict on success, or a fallback
    PASS result if the detector Lambda is not configured / fails.
    """
    if not HALLUCINATION_DETECTOR_FN:
        logger.warning(
            "HALLUCINATION_DETECTOR_FUNCTION_NAME not set — skipping detection"
        )
        return _fallback_verification(answer, confidence=0.75)

    payload = {
        "answer": answer,
        "query": query,
        "context_chunks": context_chunks,
    }

    try:
        resp = _lambda_client.invoke(
            FunctionName=HALLUCINATION_DETECTOR_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        raw = resp["Payload"].read()
        result = json.loads(raw)

        # Lambda may wrap errors
        if "errorMessage" in result:
            logger.error(
                "Hallucination Detector returned error: %s", result["errorMessage"]
            )
            return _fallback_verification(answer, confidence=0.75)

        logger.info(
            "Hallucination Detector: action=%s confidence=%s",
            result.get("action"),
            result.get("confidence"),
        )
        return result

    except ClientError as exc:
        logger.error("Failed to invoke Hallucination Detector: %s", exc)
        return _fallback_verification(answer, confidence=0.75)


def _fallback_verification(answer: str, confidence: float) -> dict:
    """Returns a minimal verification payload used when detection is skipped."""
    return {
        "action": "PASS_WITH_DISCLAIMER",
        "confidence": confidence,
        "answer": answer
        + "\n\n*Note: Hallucination detection was unavailable. Please verify citations.*",
        "quality_badge": "unverified",
        "verification": {
            "total_claims": 0,
            "supported_claims": 0,
            "partially_supported_claims": 0,
            "unsupported_claims": 0,
            "contradicted_claims": 0,
            "evidence_coverage": None,
            "citation_accuracy": None,
            "verdicts": {},
        },
    }


# ---------------------------------------------------------------------------
# Helper: build the final response payload
# ---------------------------------------------------------------------------
def _build_response_payload(
    query: str,
    verification_result: dict,
    context_chunks: list[dict],
    generation_latency_ms: float,
    token_usage: dict,
) -> dict:
    """Assembles the complete response payload returned to the API caller."""

    # Collect unique cited papers from context
    citations: list[dict] = []
    seen_ids: set[str] = set()
    for chunk in context_chunks:
        pid = chunk.get("paper_id", "")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            citations.append(
                {
                    "paper_id": pid,
                    "title": chunk.get("title", ""),
                    "authors": chunk.get("authors", []),
                    "published": chunk.get("published_date", ""),
                    "url": f"https://arxiv.org/abs/{pid}",
                    "chunks_used": sum(
                        1 for c in context_chunks if c.get("paper_id") == pid
                    ),
                }
            )

    return {
        "query": query,
        "answer": verification_result.get("answer", ""),
        "action": verification_result.get("action", "PASS"),
        "confidence": verification_result.get("confidence", 0.0),
        "quality_badge": verification_result.get("quality_badge", "unknown"),
        "warning": verification_result.get("warning"),
        "verification": verification_result.get("verification", {}),
        "citations": citations,
        "metadata": {
            "model": BEDROCK_GENERATION_MODEL,
            "generation_latency_ms": round(generation_latency_ms, 1),
            "input_tokens": token_usage.get("inputTokens", 0),
            "output_tokens": token_usage.get("outputTokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point.

    Expected event schema (from Context Builder or direct invoke):
    {
        "query":            str,           # original user question
        "assembled_prompt": str,           # full prompt string from Context Builder
        "context_chunks":   list[dict],    # top-K reranked chunks with metadata
        "options": {                       # optional overrides
            "max_tokens":  int,
            "temperature": float
        }
    }

    Returns:
    {
        "query":         str,
        "answer":        str,
        "action":        "PASS" | "PASS_WITH_DISCLAIMER" | "WARN" | "REFUSE",
        "confidence":    float,
        "quality_badge": str,
        "warning":       str | None,
        "verification":  dict,
        "citations":     list[dict],
        "metadata":      dict
    }
    """
    start_time = time.monotonic()
    logger.info("answer_generator invoked. query=%r", event.get("query", "")[:120])

    # ------------------------------------------------------------------
    # 1. Parse and validate event
    # ------------------------------------------------------------------
    query = event.get("query", "").strip()
    assembled_prompt = event.get("assembled_prompt", "").strip()
    context_chunks: list[dict] = event.get("context_chunks", [])
    options: dict = event.get("options", {})

    if not query:
        logger.error("Missing 'query' in event")
        return {"error": "Missing required field: query", "statusCode": 400}

    if not assembled_prompt:
        logger.warning("assembled_prompt is empty — using raw query as prompt")
        assembled_prompt = query

    # Optional overrides
    if "max_tokens" in options:
        global MAX_TOKENS  # noqa: PLW0603
        MAX_TOKENS = int(options["max_tokens"])
    if "temperature" in options:
        global TEMPERATURE  # noqa: PLW0603
        TEMPERATURE = float(options["temperature"])

    # ------------------------------------------------------------------
    # 2. Generate answer via Bedrock Claude 3.5 Sonnet
    # ------------------------------------------------------------------
    try:
        gen_start = time.monotonic()
        answer_text, token_usage = _generate_answer_streaming(assembled_prompt)
        generation_latency_ms = (time.monotonic() - gen_start) * 1000
    except Exception as exc:  # noqa: BLE001
        logger.error("Bedrock generation failed: %s", exc, exc_info=True)
        _put_metric("GenerationErrors", 1)
        return {
            "error": f"Answer generation failed: {exc!s}",
            "statusCode": 500,
        }

    if not answer_text:
        logger.warning("Bedrock returned empty answer")
        _put_metric("EmptyAnswers", 1)
        return {
            "query": query,
            "answer": "I was unable to generate an answer. Please try rephrasing your question.",
            "action": "REFUSE",
            "confidence": 0.0,
            "quality_badge": "generation_failed",
            "citations": [],
            "metadata": {"model": BEDROCK_GENERATION_MODEL},
        }

    logger.info(
        "Answer generated in %.0f ms, length=%d chars",
        generation_latency_ms,
        len(answer_text),
    )
    _put_metric("GenerationLatencyMs", generation_latency_ms, "Milliseconds")
    _put_metric("OutputTokens", token_usage.get("outputTokens", 0))

    # ------------------------------------------------------------------
    # 3. Hallucination Detection
    # ------------------------------------------------------------------
    logger.info("Invoking Hallucination Detector…")
    verification_result = _invoke_hallucination_detector(
        answer=answer_text,
        query=query,
        context_chunks=context_chunks,
    )

    action = verification_result.get("action", "PASS")
    confidence = verification_result.get("confidence", 0.0)
    logger.info(
        "Verification complete: action=%s confidence=%.3f", action, confidence
    )

    # Emit confidence metric
    _put_metric("ConfidenceScore", confidence)
    if action == "REFUSE":
        _put_metric("RefusedResponses", 1)
    elif action == "WARN":
        _put_metric("WarnedResponses", 1)

    # ------------------------------------------------------------------
    # 4. Assemble final response
    # ------------------------------------------------------------------
    total_latency_ms = (time.monotonic() - start_time) * 1000
    response = _build_response_payload(
        query=query,
        verification_result=verification_result,
        context_chunks=context_chunks,
        generation_latency_ms=generation_latency_ms,
        token_usage=token_usage,
    )
    response["metadata"]["total_latency_ms"] = round(total_latency_ms, 1)

    logger.info(
        "answer_generator complete. total_latency=%.0f ms action=%s confidence=%.3f",
        total_latency_ms,
        action,
        confidence,
    )
    return response
