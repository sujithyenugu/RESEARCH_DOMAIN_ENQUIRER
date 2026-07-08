"""
lambdas/evaluator/metrics.py — Retrieval & Generation Metric Computations

Pure functions — no AWS SDK calls, fully unit-testable.

Metrics implemented:
  Retrieval:
    - compute_recall_at_k(retrieved, relevant, k)
    - compute_mrr(retrieved, relevant)
    - compute_hit_rate(retrieved, relevant)
    - compute_ndcg_at_k(retrieved, relevant, k)
  Generation:
    - compute_faithfulness(answer, context_chunks)       — uses hallucination verdicts
    - compute_groundedness(answer, context_chunks)       — strict version
    - compute_citation_accuracy(answer, context_chunks)  — regex citation validation
    - compute_answer_relevance(question, answer, bedrock_client, model_id)
    - compute_context_utilization(answer, context_chunks)
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RETRIEVAL METRICS
# ---------------------------------------------------------------------------

def compute_recall_at_k(
    retrieved: list[str],
    relevant: list[str],
    k: int | None = None,
) -> float:
    """
    Recall@K = |Relevant ∩ Retrieved_top_K| / |Relevant|

    Args:
        retrieved: ordered list of retrieved chunk IDs
        relevant:  ground-truth relevant chunk IDs
        k:         cutoff rank (None = use len(retrieved))

    Returns:
        float in [0.0, 1.0]
    """
    if not relevant:
        return 1.0  # Nothing to recall — vacuously perfect

    top_k = set(retrieved[:k] if k else retrieved)
    rel_set = set(relevant)

    return round(len(top_k & rel_set) / len(rel_set), 4)


def compute_mrr(retrieved: list[str], relevant: list[str]) -> float:
    """
    Mean Reciprocal Rank — reciprocal rank of the first relevant result.

    For a single query this returns 1/rank_of_first_relevant or 0.0.

    Args:
        retrieved: ordered list of retrieved chunk IDs
        relevant:  ground-truth relevant chunk IDs

    Returns:
        float in (0.0, 1.0]
    """
    rel_set = set(relevant)
    for rank, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in rel_set:
            return round(1.0 / rank, 4)
    return 0.0


def compute_hit_rate(retrieved: list[str], relevant: list[str]) -> float:
    """
    Hit Rate@K — 1.0 if at least one relevant chunk appears in retrieved, else 0.0.

    Args:
        retrieved: top-K retrieved chunk IDs
        relevant:  ground-truth relevant chunk IDs

    Returns:
        1.0 or 0.0
    """
    if not relevant:
        return 1.0
    rel_set = set(relevant)
    return 1.0 if any(c in rel_set for c in retrieved) else 0.0


def compute_ndcg_at_k(
    retrieved: list[str],
    relevant: list[str],
    k: int = 10,
    relevance_scores: dict[str, int] | None = None,
) -> float:
    """
    Normalised Discounted Cumulative Gain @ K.

    Relevance grades (default binary):
        3 = highly relevant (direct answer)
        2 = relevant (supporting context)
        1 = marginally relevant
        0 = not relevant

    If `relevance_scores` is None, binary relevance is used
    (relevant → grade 3, not relevant → grade 0).

    Args:
        retrieved:        ordered list of retrieved chunk IDs (top-K)
        relevant:         ground-truth relevant chunk IDs
        k:                rank cutoff
        relevance_scores: optional dict of {chunk_id: grade}

    Returns:
        float in [0.0, 1.0]
    """
    if not relevant:
        return 1.0

    if relevance_scores is None:
        rel_set = set(relevant)
        # Binary: relevant chunks get grade 3
        scores = {c: 3 for c in rel_set}
    else:
        scores = relevance_scores

    def dcg(ranked: list[str], n: int) -> float:
        gain = 0.0
        for i, chunk_id in enumerate(ranked[:n], start=1):
            grade = scores.get(chunk_id, 0)
            gain += grade / math.log2(i + 1)
        return gain

    actual_dcg = dcg(retrieved, k)

    # Ideal DCG: all relevant chunks ranked at top, sorted by grade descending
    ideal_ranked = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)
    ideal_dcg = dcg(ideal_ranked, k)

    if ideal_dcg == 0.0:
        return 0.0

    return round(min(actual_dcg / ideal_dcg, 1.0), 4)


# ---------------------------------------------------------------------------
# GENERATION METRICS
# ---------------------------------------------------------------------------

_CITATION_PATTERN = re.compile(r"\[(\d{4}\.\d{4,5})\]")


def _extract_cited_ids(answer: str) -> list[str]:
    """Extract all [YYYY.NNNNN] arXiv paper IDs cited in the answer."""
    return _CITATION_PATTERN.findall(answer)


def compute_citation_accuracy(answer: str, context_chunks: list[dict]) -> float:
    """
    Citation Accuracy = fraction of [paper_id] citations in the answer
    that correspond to valid paper IDs in the context.

    Args:
        answer:         generated answer text
        context_chunks: list of chunk dicts, each with a "paper_id" field

    Returns:
        float in [0.0, 1.0] — 1.0 if no citations (neutral)
    """
    cited_ids = _extract_cited_ids(answer)
    if not cited_ids:
        return 1.0  # No citations: treat as neutral

    valid_ids = {c.get("paper_id", "") for c in context_chunks}
    valid_count = sum(1 for cid in cited_ids if cid in valid_ids)
    return round(valid_count / len(cited_ids), 4)


def compute_context_utilization(answer: str, context_chunks: list[dict]) -> float:
    """
    Context Utilization = fraction of unique paper IDs in context
    that were actually cited in the answer.

    Args:
        answer:         generated answer text
        context_chunks: list of chunk dicts

    Returns:
        float in [0.0, 1.0]
    """
    all_paper_ids = {c.get("paper_id", "") for c in context_chunks if c.get("paper_id")}
    if not all_paper_ids:
        return 0.0

    cited_ids = set(_extract_cited_ids(answer))
    return round(len(cited_ids & all_paper_ids) / len(all_paper_ids), 4)


def compute_faithfulness(answer: str, context_chunks: list[dict]) -> float:
    """
    Faithfulness = supported_claims / total_claims.

    Uses the verdicts embedded in the context_chunks (populated by the
    Hallucination Detector Lambda) when available. Falls back to a
    heuristic citation-based estimate.

    SUPPORTED + PARTIALLY_SUPPORTED → faithful.

    Args:
        answer:         generated answer text
        context_chunks: list of chunk dicts; may contain "verdict" fields

    Returns:
        float in [0.0, 1.0]
    """
    # If chunks carry claim verdicts (from HallucinationDetector), use them
    verdicts = _collect_verdicts(context_chunks)
    if verdicts:
        supported = sum(
            1 for v in verdicts
            if v in ("SUPPORTED", "PARTIALLY_SUPPORTED")
        )
        return round(supported / len(verdicts), 4)

    # Fallback: citation-based proxy
    return compute_citation_accuracy(answer, context_chunks)


def compute_groundedness(answer: str, context_chunks: list[dict]) -> float:
    """
    Groundedness = fully_supported_claims / total_claims (stricter than faithfulness).

    PARTIALLY_SUPPORTED does NOT count.

    Args:
        answer:         generated answer text
        context_chunks: list of chunk dicts with optional "verdict" fields

    Returns:
        float in [0.0, 1.0]
    """
    verdicts = _collect_verdicts(context_chunks)
    if verdicts:
        fully_supported = sum(1 for v in verdicts if v == "SUPPORTED")
        return round(fully_supported / len(verdicts), 4)

    # Fallback to citation accuracy as conservative proxy
    return compute_citation_accuracy(answer, context_chunks)


def _collect_verdicts(context_chunks: list[dict]) -> list[str]:
    """
    Extract claim verdicts from context chunks if present.
    The Hallucination Detector may attach a `claims` list to each chunk.
    """
    verdicts: list[str] = []
    for chunk in context_chunks:
        for claim in chunk.get("claims", []):
            verdict = claim.get("verdict", "")
            if verdict:
                verdicts.append(verdict)
    return verdicts


def compute_answer_relevance(
    question: str,
    answer: str,
    bedrock_client: Any,
    model_id: str,
) -> float:
    """
    Answer Relevance — asks Claude Haiku to score 1–5, then normalises to [0, 1].

    Prompt:
        "Does the following answer directly address the question?
         Score 1-5: 5 = Directly answers · 1 = Completely irrelevant
         Question: {question}
         Answer: {answer}
         Output JSON: {\"score\": X}"

    Args:
        question:        original user query
        answer:          generated answer text
        bedrock_client:  boto3 bedrock-runtime client
        model_id:        Bedrock model ID (Claude Haiku)

    Returns:
        float in [0.0, 1.0]
    """
    if not answer or not question:
        return 0.0

    # Truncate to avoid token limits
    truncated_answer   = answer[:2000]
    truncated_question = question[:500]

    prompt = (
        "You are a research QA evaluator. "
        "Does the following answer directly address the question?\n\n"
        "Score 1-5:\n"
        "  5 = Directly and completely answers the question\n"
        "  4 = Mostly answers, minor gaps\n"
        "  3 = Partially answers, significant gaps\n"
        "  2 = Tangentially related but does not answer\n"
        "  1 = Completely irrelevant\n\n"
        f"Question: {truncated_question}\n\n"
        f"Answer: {truncated_answer}\n\n"
        'Output ONLY valid JSON in this format: {"score": <integer 1-5>}'
    )

    try:
        response = bedrock_client.invoke_model(
            modelId=model_id,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": prompt}],
                }
            ),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(response["body"].read())
        text = body["content"][0]["text"].strip()

        # Parse JSON output
        parsed = json.loads(text)
        raw_score = int(parsed.get("score", 3))
        raw_score = max(1, min(5, raw_score))

        # Normalise 1–5 → 0.0–1.0
        return round((raw_score - 1) / 4.0, 4)

    except Exception as exc:
        logger.warning("compute_answer_relevance failed: %s", exc)
        return 0.5  # neutral fallback
