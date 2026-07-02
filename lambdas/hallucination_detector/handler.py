"""
handler.py — hallucination_detector Lambda

Verifies every factual claim in an LLM-generated answer against the
retrieved evidence chunks.  Returns a gated response with a confidence
score and per-claim verdicts.

Five-step pipeline (see HALLUCINATION_DETECTION.md):
  Step 1 — Atomic claim extraction        (Bedrock Claude 3 Haiku)
  Step 2 — Evidence mapping               (citation + entity + semantic match)
  Step 3 — Claim verification             (Bedrock Claude 3 Haiku, per-claim)
  Step 4 — Evidence coverage analysis     (fraction of context cited)
  Step 5 — Confidence score computation   (weighted composite)

Response gate:
  ≥ 0.85  → PASS              (return answer as-is)
  0.60–0.85 → PASS_WITH_DISCLAIMER
  0.30–0.60 → WARN
  < 0.30  → REFUSE

Triggered by: Answer Generator Lambda (synchronous invoke)
Timeout:      30 s
Memory:       512 MB
VPC:          no
"""

from __future__ import annotations

import json
import logging
import os
import re
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
BEDROCK_VERIFY_MODEL = os.environ.get(
    "BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0"
)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
CW_NAMESPACE   = os.environ.get("CW_NAMESPACE", "ResearchRAG")

CONFIDENCE_PASS_THRESHOLD   = float(os.environ.get("CONFIDENCE_PASS_THRESHOLD",   "0.85"))
CONFIDENCE_WARN_THRESHOLD   = float(os.environ.get("CONFIDENCE_WARN_THRESHOLD",   "0.60"))
CONFIDENCE_REFUSE_THRESHOLD = float(os.environ.get("CONFIDENCE_REFUSE_THRESHOLD", "0.30"))

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
_bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
_cloudwatch      = boto3.client("cloudwatch")

# ---------------------------------------------------------------------------
# Verdict score mapping
# ---------------------------------------------------------------------------
VERDICT_SCORES: dict[str, float] = {
    "SUPPORTED":           1.0,
    "PARTIALLY_SUPPORTED": 0.5,
    "UNSUPPORTED":         0.0,
    "CONTRADICTED":       -1.0,
}


# ===========================================================================
# Step 1 — Atomic Claim Extraction
# ===========================================================================

_CLAIM_EXTRACTION_SYSTEM = (
    "You are a fact-checking assistant. Extract all factual claims from "
    "the following AI research answer. Break compound claims into atomic statements. "
    "Focus on quantitative claims, method comparisons, and dataset results.\n\n"
    'Output ONLY valid JSON in this exact format:\n'
    '{\n'
    '  "claims": [\n'
    '    {\n'
    '      "claim_id": "c1",\n'
    '      "text": "<atomic claim text>",\n'
    '      "type": "<quantitative|comparative|causal|definitional|existence>",\n'
    '      "entities_mentioned": ["<entity1>", "<entity2>"],\n'
    '      "citation_expected": "<[paper_id] or null>"\n'
    '    }\n'
    '  ]\n'
    '}'
)


def _extract_claims(answer: str) -> list[dict]:
    """
    Step 1: Extracts atomic factual claims from the answer text.
    Returns a list of claim dicts.  Falls back to [] on any error.
    """
    prompt = f"Answer to analyze:\n{answer}"

    try:
        response = _bedrock_runtime.converse(
            modelId=BEDROCK_VERIFY_MODEL,
            system=[{"text": _CLAIM_EXTRACTION_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
        )
        raw_text = (
            response.get("output", {})
            .get("message", {})
            .get("content", [{}])[0]
            .get("text", "")
        )
        # Strip possible markdown code fences
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
        raw_text = re.sub(r"\s*```$", "", raw_text.strip(), flags=re.IGNORECASE)

        data = json.loads(raw_text)
        claims: list[dict] = data.get("claims", [])
        logger.info("Extracted %d atomic claims from answer", len(claims))
        return claims

    except (ClientError, json.JSONDecodeError, KeyError) as exc:
        logger.error("Claim extraction failed: %s", exc)
        return []


# ===========================================================================
# Step 2 — Evidence Mapping
# ===========================================================================

def _extract_paper_id(citation_str: str | None) -> str | None:
    """Returns the raw paper_id from a string like '[2401.12345]' or None."""
    if not citation_str:
        return None
    match = re.search(r"(\d{4}\.\d{4,5})", citation_str)
    return match.group(1) if match else None


def _map_claims_to_evidence(
    claims: list[dict], context_chunks: list[dict]
) -> dict[str, list[dict]]:
    """
    Step 2: For each claim, find supporting context chunks using three methods:
      1. Citation match  — explicit [paper_id] reference in the claim
      2. Entity overlap  — at least one entity name shared
      3. Keyword overlap — fallback substring match on claim text

    Returns: { claim_id → [supporting_chunks] }
    """
    claim_evidence_map: dict[str, list[dict]] = {}

    for claim in claims:
        cid = claim["claim_id"]
        supporting: list[dict] = []
        seen_ids: set[str] = set()

        def _add(chunk: dict) -> None:
            chunk_id = chunk.get("chunk_id", id(chunk))
            if chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                supporting.append(chunk)

        # Method 1 — citation match
        paper_id = _extract_paper_id(claim.get("citation_expected"))
        if paper_id:
            for c in context_chunks:
                if c.get("paper_id") == paper_id:
                    _add(c)

        # Method 2 — entity overlap
        claim_entities = {e.lower() for e in claim.get("entities_mentioned", [])}
        for c in context_chunks:
            chunk_entities = {e.lower() for e in c.get("entities", [])}
            if claim_entities & chunk_entities:
                _add(c)

        # Method 3 — keyword fallback (simple substring of first 5 words)
        if not supporting:
            keywords = claim["text"].lower().split()[:5]
            for c in context_chunks:
                content = c.get("content", "").lower()
                if any(kw in content for kw in keywords if len(kw) > 3):
                    _add(c)
                    if len(supporting) >= 3:
                        break

        claim_evidence_map[cid] = supporting

    return claim_evidence_map


# ===========================================================================
# Step 3 — Claim Verification
# ===========================================================================

_CLAIM_VERIFY_SYSTEM = (
    "You are a rigorous fact-checker for AI research. Determine whether "
    "the given claim is supported by the provided evidence chunks.\n\n"
    "Be strict: only mark SUPPORTED if the evidence explicitly states or directly "
    "implies the claim. Do not infer beyond what is written.\n\n"
    'Output ONLY valid JSON:\n'
    '{\n'
    '  "verdict": "SUPPORTED|PARTIALLY_SUPPORTED|UNSUPPORTED|CONTRADICTED",\n'
    '  "confidence": <0.0-1.0>,\n'
    '  "explanation": "<brief reason>",\n'
    '  "supporting_quotes": ["<exact quote from evidence>"]\n'
    '}'
)


def _verify_claim(claim: dict, evidence_chunks: list[dict]) -> dict:
    """
    Step 3: Verifies one claim against its evidence chunks.
    Returns a verdict dict.  Falls back to UNSUPPORTED on error.
    """
    if not evidence_chunks:
        return {
            "verdict": "UNSUPPORTED",
            "confidence": 0.0,
            "explanation": "No supporting evidence found in retrieved context.",
            "supporting_quotes": [],
        }

    # Build evidence text block (cap at 3 chunks to fit token budget)
    evidence_parts = []
    for i, chunk in enumerate(evidence_chunks[:3], 1):
        pid = chunk.get("paper_id", "unknown")
        section = chunk.get("section_id", "unknown")
        content = chunk.get("content", "")[:600]  # 600 chars per chunk
        evidence_parts.append(f"[Chunk {i} from paper {pid} — {section}]\n{content}")

    evidence_text = "\n\n".join(evidence_parts)
    prompt = f"Claim: {claim['text']}\n\nEvidence:\n{evidence_text}"

    try:
        response = _bedrock_runtime.converse(
            modelId=BEDROCK_VERIFY_MODEL,
            system=[{"text": _CLAIM_VERIFY_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )
        raw_text = (
            response.get("output", {})
            .get("message", {})
            .get("content", [{}])[0]
            .get("text", "")
        )
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
        raw_text = re.sub(r"\s*```$", "", raw_text.strip(), flags=re.IGNORECASE)
        return json.loads(raw_text)

    except (ClientError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Claim verification failed for %s: %s", claim["claim_id"], exc)
        return {
            "verdict": "UNSUPPORTED",
            "confidence": 0.0,
            "explanation": f"Verification error: {exc!s}",
            "supporting_quotes": [],
        }


def _verify_all_claims(
    claims: list[dict],
    claim_evidence_map: dict[str, list[dict]],
) -> dict[str, dict]:
    """
    Step 3 (orchestrator): Verifies all claims sequentially.
    Returns { claim_id → verdict_dict }
    """
    verdicts: dict[str, dict] = {}
    for claim in claims:
        evidence = claim_evidence_map.get(claim["claim_id"], [])
        verdict = _verify_claim(claim, evidence)
        verdicts[claim["claim_id"]] = verdict
        logger.debug(
            "Claim %s → verdict=%s conf=%.2f",
            claim["claim_id"],
            verdict.get("verdict"),
            verdict.get("confidence", 0),
        )
    return verdicts


# ===========================================================================
# Step 4 — Evidence Coverage Analysis
# ===========================================================================

def _compute_evidence_coverage(
    context_chunks: list[dict],
    claim_evidence_map: dict[str, list[dict]],
) -> float:
    """
    Step 4: What fraction of retrieved chunks are cited by at least one claim?
    Low coverage → LLM likely hallucinated beyond retrieved context.
    """
    cited_ids: set = set()
    for chunks in claim_evidence_map.values():
        for chunk in chunks:
            cited_ids.add(chunk.get("chunk_id", id(chunk)))

    total = len(context_chunks)
    if total == 0:
        return 0.0
    return round(len(cited_ids) / total, 3)


# ===========================================================================
# Citation Grounding Check
# ===========================================================================

def _check_citation_accuracy(
    answer: str, context_chunks: list[dict]
) -> tuple[float, list[str]]:
    """
    Verifies that every [paper_id] reference in the answer exists in context.
    Returns (accuracy_score 0–1, list_of_invalid_paper_ids).
    """
    cited_ids = re.findall(r"\[(\d{4}\.\d{4,5})\]", answer)
    valid_ids = {c.get("paper_id", "") for c in context_chunks}

    if not cited_ids:
        return 1.0, []  # no citations → no invalid ones

    invalid = [cid for cid in cited_ids if cid not in valid_ids]
    accuracy = (len(cited_ids) - len(invalid)) / len(cited_ids)
    if invalid:
        logger.warning("Invalid citations detected: %s", invalid)
    return round(accuracy, 3), invalid


# ===========================================================================
# Step 5 — Confidence Score Computation
# ===========================================================================

def _compute_confidence_score(
    claims: list[dict],
    verdicts: dict[str, dict],
    coverage: float,
    citation_accuracy: float,
) -> float:
    """
    Step 5: Weighted composite confidence score.

    Weights:
      60 % — claim verification score (quantitative claims weighted 1.5×)
      25 % — evidence coverage
      15 % — citation accuracy
    """
    if not claims:
        # No claims extracted — limited evidence
        return round(0.5 * coverage + 0.15 * citation_accuracy, 3)

    claim_score = 0.0
    max_claim_score = 0.0

    for claim in claims:
        weight = 1.5 if claim.get("type") == "quantitative" else 1.0
        verdict = verdicts.get(claim["claim_id"], {}).get("verdict", "UNSUPPORTED")
        claim_score += VERDICT_SCORES.get(verdict, 0.0) * weight
        max_claim_score += weight

    normalized_claim_score = (
        max(0.0, claim_score) / max_claim_score if max_claim_score > 0 else 0.0
    )
    coverage_score = min(coverage * 1.25, 1.0)

    confidence = (
        0.60 * normalized_claim_score
        + 0.25 * coverage_score
        + 0.15 * citation_accuracy
    )
    return round(min(max(confidence, 0.0), 1.0), 3)


# ===========================================================================
# Response Gating
# ===========================================================================

def _gate_response(
    answer: str,
    confidence: float,
    claims: list[dict],
    verdicts: dict[str, dict],
    invalid_citations: list[str],
) -> dict:
    """
    Applies gating rules and returns the final response dict.

    Gate priority:
      1. Any CONTRADICTED claim → WARN (always, regardless of confidence)
      2. confidence ≥ 0.85      → PASS
      3. confidence ≥ 0.60      → PASS_WITH_DISCLAIMER
      4. confidence ≥ 0.30      → WARN
      5. confidence < 0.30      → REFUSE
    """
    contradicted = [
        cid
        for cid, v in verdicts.items()
        if v.get("verdict") == "CONTRADICTED"
    ]
    unsupported = [
        cid
        for cid, v in verdicts.items()
        if v.get("verdict") == "UNSUPPORTED"
    ]

    def _add_citation_warning(text: str) -> str:
        if invalid_citations:
            text += (
                f"\n\n⚠️ Warning: {len(invalid_citations)} citation(s) "
                f"({', '.join(f'[{c}]' for c in invalid_citations)}) "
                "could not be verified against retrieved papers."
            )
        return text

    if contradicted:
        return {
            "action": "WARN",
            "confidence": confidence,
            "answer": _add_citation_warning(answer),
            "quality_badge": "contradicted_claims",
            "warning": (
                f"⚠️ {len(contradicted)} claim(s) may be inaccurate "
                "based on the retrieved evidence."
            ),
            "contradicted_claim_ids": contradicted,
        }

    if confidence >= CONFIDENCE_PASS_THRESHOLD:
        return {
            "action": "PASS",
            "confidence": confidence,
            "answer": _add_citation_warning(answer),
            "quality_badge": "high_confidence",
        }

    if confidence >= CONFIDENCE_WARN_THRESHOLD:
        disclaimer = (
            "\n\n*Note: Some claims have limited evidence support. "
            "Please verify citations independently.*"
        )
        return {
            "action": "PASS_WITH_DISCLAIMER",
            "confidence": confidence,
            "answer": _add_citation_warning(answer) + disclaimer,
            "quality_badge": "medium_confidence",
            "unsupported_claim_ids": unsupported,
        }

    if confidence >= CONFIDENCE_REFUSE_THRESHOLD:
        return {
            "action": "WARN",
            "confidence": confidence,
            "answer": _add_citation_warning(answer),
            "quality_badge": "low_confidence",
            "warning": "⚠️ This answer has low evidence coverage. Treat with caution.",
            "unsupported_claim_ids": unsupported,
        }

    # Below refuse threshold
    return {
        "action": "REFUSE",
        "confidence": confidence,
        "answer": (
            "I cannot provide a reliable answer to this question based on the "
            "available research papers. The evidence is insufficient or contradictory."
        ),
        "quality_badge": "insufficient_evidence",
        "reason": "Confidence score below minimum threshold",
    }


# ===========================================================================
# CloudWatch metrics
# ===========================================================================

def _put_metric(metric_name: str, value: float, unit: str = "None") -> None:
    try:
        _cloudwatch.put_metric_data(
            Namespace=CW_NAMESPACE,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit metric %s: %s", metric_name, exc)


# ===========================================================================
# Lambda Handler
# ===========================================================================

def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point for the Hallucination Detector.

    Expected event schema (from Answer Generator):
    {
        "answer":         str,           # LLM-generated answer text
        "query":          str,           # original user question
        "context_chunks": list[dict],   # retrieved chunks with paper_id, content, entities
    }

    Returns:
    {
        "action":        "PASS" | "PASS_WITH_DISCLAIMER" | "WARN" | "REFUSE",
        "confidence":    float,
        "answer":        str,           # possibly modified (disclaimer added)
        "quality_badge": str,
        "warning":       str | None,
        "verification":  {
            "total_claims":              int,
            "supported_claims":          int,
            "partially_supported_claims": int,
            "unsupported_claims":        int,
            "contradicted_claims":       int,
            "evidence_coverage":         float,
            "citation_accuracy":         float,
            "verdicts":                  dict
        }
    }
    """
    start_time = time.monotonic()
    logger.info("hallucination_detector invoked")

    # ------------------------------------------------------------------
    # Parse event
    # ------------------------------------------------------------------
    answer: str              = event.get("answer", "").strip()
    query: str               = event.get("query", "").strip()
    context_chunks: list     = event.get("context_chunks", [])

    if not answer:
        logger.warning("Empty answer received — returning REFUSE")
        return {
            "action": "REFUSE",
            "confidence": 0.0,
            "answer": "No answer was generated.",
            "quality_badge": "no_answer",
            "verification": {},
        }

    # ------------------------------------------------------------------
    # Step 1 — Extract atomic claims
    # ------------------------------------------------------------------
    logger.info("Step 1: Extracting claims…")
    claims = _extract_claims(answer)

    # ------------------------------------------------------------------
    # Step 2 — Map claims to evidence
    # ------------------------------------------------------------------
    logger.info("Step 2: Mapping %d claims to evidence…", len(claims))
    claim_evidence_map = _map_claims_to_evidence(claims, context_chunks)

    # ------------------------------------------------------------------
    # Step 3 — Verify each claim
    # ------------------------------------------------------------------
    logger.info("Step 3: Verifying claims…")
    verdicts = _verify_all_claims(claims, claim_evidence_map)

    # ------------------------------------------------------------------
    # Step 4 — Evidence coverage
    # ------------------------------------------------------------------
    logger.info("Step 4: Computing evidence coverage…")
    coverage = _compute_evidence_coverage(context_chunks, claim_evidence_map)

    # ------------------------------------------------------------------
    # Citation accuracy check
    # ------------------------------------------------------------------
    citation_accuracy, invalid_citations = _check_citation_accuracy(
        answer, context_chunks
    )

    # ------------------------------------------------------------------
    # Step 5 — Confidence score
    # ------------------------------------------------------------------
    logger.info("Step 5: Computing confidence score…")
    confidence = _compute_confidence_score(
        claims, verdicts, coverage, citation_accuracy
    )

    # ------------------------------------------------------------------
    # Tally verdicts
    # ------------------------------------------------------------------
    verdict_counts = {
        "SUPPORTED":           0,
        "PARTIALLY_SUPPORTED": 0,
        "UNSUPPORTED":         0,
        "CONTRADICTED":        0,
    }
    for v in verdicts.values():
        vdict = v.get("verdict", "UNSUPPORTED")
        if vdict in verdict_counts:
            verdict_counts[vdict] += 1

    # ------------------------------------------------------------------
    # Response gate
    # ------------------------------------------------------------------
    gated = _gate_response(answer, confidence, claims, verdicts, invalid_citations)

    # ------------------------------------------------------------------
    # Emit CloudWatch metrics
    # ------------------------------------------------------------------
    elapsed_ms = (time.monotonic() - start_time) * 1000
    _put_metric("ConfidenceScore",      confidence)
    _put_metric("EvidenceCoverage",     coverage)
    _put_metric("CitationAccuracy",     citation_accuracy)
    _put_metric("DetectionLatencyMs",   elapsed_ms, "Milliseconds")
    if verdict_counts["CONTRADICTED"] > 0:
        _put_metric("ContradictedClaims", verdict_counts["CONTRADICTED"])
    if verdict_counts["UNSUPPORTED"] > 0:
        _put_metric("UnsupportedClaims", verdict_counts["UNSUPPORTED"])

    logger.info(
        "hallucination_detector complete: action=%s confidence=%.3f "
        "claims=%d supported=%d partial=%d unsupported=%d contradicted=%d "
        "coverage=%.2f citation_acc=%.2f elapsed=%.0f ms",
        gated.get("action"),
        confidence,
        len(claims),
        verdict_counts["SUPPORTED"],
        verdict_counts["PARTIALLY_SUPPORTED"],
        verdict_counts["UNSUPPORTED"],
        verdict_counts["CONTRADICTED"],
        coverage,
        citation_accuracy,
        elapsed_ms,
    )

    # Merge gate result with full verification detail
    gated["verification"] = {
        "total_claims":               len(claims),
        "supported_claims":           verdict_counts["SUPPORTED"],
        "partially_supported_claims": verdict_counts["PARTIALLY_SUPPORTED"],
        "unsupported_claims":         verdict_counts["UNSUPPORTED"],
        "contradicted_claims":        verdict_counts["CONTRADICTED"],
        "evidence_coverage":          coverage,
        "citation_accuracy":          citation_accuracy,
        "verdicts": {
            cid: {
                "verdict":           v.get("verdict"),
                "confidence":        v.get("confidence", 0.0),
                "explanation":       v.get("explanation", ""),
                "supporting_quotes": v.get("supporting_quotes", []),
            }
            for cid, v in verdicts.items()
        },
    }

    return gated
