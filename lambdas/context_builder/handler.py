"""
handler.py — context_builder Lambda

Entry point for the Context Builder that receives the top-10 reranked chunks,
deduplicates them, orders them by citation relevance tiers, compresses the
context to fit within an 8 000-token budget, and assembles the final
system + context + query prompt string for the Answer Generator (Day 5).

Execution flow:
  1. Receive payload from Reranker: { query, chunks: [...], options }
  2. Deduplicate chunks — remove identical (paper_id, section_id) pairs and
     near-duplicate content (MD5 of first 200 chars).
  3. Order by citation relevance tiers:
       Tier 1: rerank_score > 0.8  (most relevant, keep whole)
       Tier 2: 0.5 ≤ rerank_score ≤ 0.8 (supporting evidence)
       Tier 3: source == "graph"   (graph-expanded context papers)
     Within each tier, sort by published_date descending.
  4. Context compression — if total token count > CONTEXT_TOKEN_BUDGET:
       - Keep Tier 1 chunks whole.
       - Truncate Tier 2+ chunks to first 400 tokens per chunk.
       - If still over budget, drop lowest-scoring Tier 3 chunks.
  5. Format citations in [paper_id] notation.
  6. Assemble final prompt string (system + context blocks + query).

Triggered by: Reranker Lambda (synchronous invoke)
Timeout:      30 s
Memory:       256 MB
VPC:          no (pure in-memory logic)
"""

from __future__ import annotations

import hashlib
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
_AWS_REGION          = os.environ.get("AWS_REGION_NAME",    "us-east-1")
CONTEXT_TOKEN_BUDGET = int(os.environ.get("CONTEXT_TOKEN_BUDGET", "8000"))
FINAL_TOP_K          = int(os.environ.get("FINAL_TOP_K",          "10"))
CW_NAMESPACE         = os.environ.get("CW_NAMESPACE",             "ResearchRAG")

# Rough token estimation: 1 token ≈ 4 characters (conservative)
_CHARS_PER_TOKEN = 4
_TIER2_TRUNCATE_TOKENS = 400  # max tokens per Tier 2+ chunk after compression

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
cw = boto3.client("cloudwatch", region_name=_AWS_REGION)

# System prompt injected at the top of every assembled prompt
_SYSTEM_PROMPT = (
    "You are an expert AI research assistant. Answer the question based ONLY on "
    "the provided research paper excerpts. For every factual claim you make, cite "
    "the specific paper using [paper_id] notation (e.g. [2401.12345]). If the "
    "evidence is insufficient to answer confidently, state so explicitly and "
    "explain what information is missing."
)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point — invoked synchronously by Reranker.

    Parameters
    ----------
    event : dict
        Payload from Reranker::

            {
              "query":   "How does LoRA compare to full fine-tuning?",
              "chunks":  [
                {
                  "chunk_id":       "arxiv:2106.09685:chunk:0",
                  "paper_id":       "2106.09685",
                  "text":           "LoRA proposes freezing pre-trained...",
                  "section_title":  "Abstract",
                  "section_id":     "abstract",
                  "published_date": "2021-10-16",
                  "authors":        ["Hu, E.", "Shen, Y."],
                  "rerank_score":   0.92,
                  "rrf_score":      0.042,
                  "source":         "dense"
                },
                ...   # up to FINAL_TOP_K chunks
              ],
              "options": {...}
            }

    context : LambdaContext
        Standard Lambda context.

    Returns
    -------
    dict
        Ready-to-pass response for the Answer Generator::

            {
              "answer_prompt":  "SYSTEM:\\n...\\n\\nCONTEXT:\\n...\\n\\nQUESTION: ...\\n\\nANSWER:",
              "chunks":         [...],       # deduplicated, ordered, compressed chunks
              "citations":      [...],       # structured citation list
              "context_stats":  {
                "chunks_in":          10,
                "chunks_out":         8,
                "estimated_tokens":   6200,
                "compressed":         true
              }
            }
    """
    t_start = time.perf_counter()
    run_id  = f"cb_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    query:  str             = event.get("query", "")
    chunks: list[dict]      = event.get("chunks", [])
    options: dict[str, Any] = event.get("options", {})

    logger.info(
        "Context builder run %s | query=%r | chunks_in=%d",
        run_id, query[:80], len(chunks),
    )

    chunks_in = len(chunks)

    # ------------------------------------------------------------------
    # Step 1: Deduplication
    # ------------------------------------------------------------------
    unique_chunks = _deduplicate_chunks(chunks)
    logger.info("run=%s | dedup: %d → %d chunks", run_id, chunks_in, len(unique_chunks))

    # ------------------------------------------------------------------
    # Step 2: Citation ordering (tier sort)
    # ------------------------------------------------------------------
    ordered_chunks = _order_chunks(unique_chunks)

    # ------------------------------------------------------------------
    # Step 3: Context compression to fit token budget
    # ------------------------------------------------------------------
    compressed_chunks, compressed, estimated_tokens = _compress_to_budget(
        ordered_chunks, CONTEXT_TOKEN_BUDGET
    )

    logger.info(
        "run=%s | ordered=%d | compressed=%s | tokens≈%d",
        run_id, len(compressed_chunks), compressed, estimated_tokens,
    )

    # ------------------------------------------------------------------
    # Step 4: Build citations list
    # ------------------------------------------------------------------
    citations = _build_citations(compressed_chunks)

    # ------------------------------------------------------------------
    # Step 5: Assemble final prompt
    # ------------------------------------------------------------------
    prompt = _assemble_prompt(query, compressed_chunks)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    _emit_metrics(cw, CW_NAMESPACE, {
        "context_builder_duration_ms": elapsed_ms,
        "chunks_after_dedup":          len(unique_chunks),
        "chunks_in_prompt":            len(compressed_chunks),
        "estimated_prompt_tokens":     estimated_tokens,
    })

    logger.info("run=%s | completed | elapsed_ms=%d", run_id, elapsed_ms)

    return {
        "answer_prompt": prompt,
        "chunks":        compressed_chunks,
        "citations":     citations,
        "context_stats": {
            "chunks_in":         chunks_in,
            "chunks_out":        len(compressed_chunks),
            "estimated_tokens":  estimated_tokens,
            "compressed":        compressed,
        },
    }


# ---------------------------------------------------------------------------
# Step 1: Deduplication
# ---------------------------------------------------------------------------

def _deduplicate_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Remove near-duplicate chunks by checking two keys:

    1. (paper_id, section_id) — same paper section seen twice.
    2. MD5 of the first 200 characters of text — identical content from
       different metadata paths (e.g. graph vs. dense result).

    The chunk list is assumed to already be in priority order (tier 1 first),
    so the first occurrence of any duplicate wins.
    """
    seen_sections:     set[tuple[str, str]] = set()
    seen_content_hashes: set[str]           = set()
    unique: list[dict[str, Any]]            = []

    for chunk in chunks:
        paper_id   = chunk.get("paper_id",   "unknown")
        section_id = chunk.get("section_id", "unknown")
        text_head  = chunk.get("text", "")[:200]
        content_hash = hashlib.md5(text_head.encode()).hexdigest()

        section_key = (paper_id, section_id)
        if section_key in seen_sections or content_hash in seen_content_hashes:
            logger.debug(
                "Dedup: skipping chunk %s (paper=%s section=%s)",
                chunk.get("chunk_id"), paper_id, section_id,
            )
            continue

        seen_sections.add(section_key)
        seen_content_hashes.add(content_hash)
        unique.append(chunk)

    return unique


# ---------------------------------------------------------------------------
# Step 2: Citation ordering
# ---------------------------------------------------------------------------

def _order_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sort chunks into relevance tiers, then chronologically within each tier.

    Tiers:
        1 — rerank_score > 0.8    (highest relevance — lead with these)
        2 — 0.5 ≤ score ≤ 0.8    (supporting evidence)
        3 — source == "graph" or score < 0.5  (graph context / lower-relevance)

    Within each tier, newer papers (higher published_date) appear first so the
    LLM sees the most recent findings before older background work.
    """
    def _sort_key(c: dict) -> str:
        # ISO date string sorts lexicographically — descending handled by reverse flag
        return c.get("published_date", "1970-01-01")

    tier1 = sorted(
        [c for c in chunks if c.get("rerank_score", 0.0) > 0.8],
        key=_sort_key, reverse=True,
    )
    tier2 = sorted(
        [c for c in chunks if 0.5 <= c.get("rerank_score", 0.0) <= 0.8],
        key=_sort_key, reverse=True,
    )
    tier3 = sorted(
        [c for c in chunks if c.get("rerank_score", 0.0) < 0.5 or c.get("source") == "graph"],
        key=_sort_key, reverse=True,
    )
    # Avoid double-counting: tier3 must not include chunks already in tier1/tier2
    tier1_2_ids = {c.get("chunk_id") for c in tier1 + tier2}
    tier3 = [c for c in tier3 if c.get("chunk_id") not in tier1_2_ids]

    return tier1 + tier2 + tier3


# ---------------------------------------------------------------------------
# Step 3: Context compression
# ---------------------------------------------------------------------------

def _compress_to_budget(
    chunks: list[dict[str, Any]],
    token_budget: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """
    Compress the chunk list to fit within ``token_budget`` tokens.

    Strategy:
        1. Estimate total tokens using a 1-token-per-4-chars heuristic.
        2. If within budget → return as-is (no compression).
        3. If over budget:
           a. Keep Tier 1 chunks (rerank_score > 0.8) whole.
           b. Truncate Tier 2 chunks to _TIER2_TRUNCATE_TOKENS tokens.
           c. Drop Tier 3 chunks from the tail if still over budget.

    Returns
    -------
    tuple[list[dict], bool, int]
        (final_chunks, was_compressed, estimated_token_count)
    """
    def _estimate_tokens(c: dict) -> int:
        text = _format_chunk_block(c)
        return max(1, len(text) // _CHARS_PER_TOKEN)

    total_tokens = sum(_estimate_tokens(c) for c in chunks)
    if total_tokens <= token_budget:
        return chunks, False, total_tokens

    compressed = True
    result: list[dict[str, Any]] = []
    used_tokens = 0

    for chunk in chunks:
        rerank_score = chunk.get("rerank_score", 0.0)
        source       = chunk.get("source", "")

        if rerank_score > 0.8:
            # Tier 1 — keep whole
            chunk_copy = dict(chunk)
        elif rerank_score >= 0.5:
            # Tier 2 — truncate text to budget
            max_chars  = _TIER2_TRUNCATE_TOKENS * _CHARS_PER_TOKEN
            chunk_copy = dict(chunk)
            if len(chunk_copy.get("text", "")) > max_chars:
                chunk_copy["text"] = chunk_copy["text"][:max_chars] + " [truncated]"
        else:
            # Tier 3 (graph / low-relevance) — abstract only
            chunk_copy = dict(chunk)
            abstract_text = chunk_copy.get("text", "")[:200]
            chunk_copy["text"] = abstract_text + " [context excerpt]"

        chunk_tokens = max(1, len(_format_chunk_block(chunk_copy)) // _CHARS_PER_TOKEN)
        if used_tokens + chunk_tokens > token_budget and source == "graph":
            # Drop low-priority graph chunks to stay within budget
            logger.debug("Budget exceeded — dropping graph chunk %s", chunk.get("chunk_id"))
            continue

        result.append(chunk_copy)
        used_tokens += chunk_tokens

    return result, compressed, used_tokens


# ---------------------------------------------------------------------------
# Step 4 & 5: Citations + Prompt assembly
# ---------------------------------------------------------------------------

def _build_citations(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build a deduplicated, ordered list of citation objects from the final
    chunk set. One citation per unique paper_id, in order of first appearance.
    """
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
                "relevance_score": round(c.get("rerank_score", 0.0), 4),
            })
    return citations


def _format_chunk_block(chunk: dict[str, Any]) -> str:
    """
    Format a single chunk as a context block string for inclusion in the prompt.

    Example output::

        [Paper 2106.09685] "LoRA: Low-Rank Adaptation of Large Language Models"
        Authors: Hu, E., Shen, Y. (2021-10-16) | Section: Abstract
        ---
        LoRA proposes freezing pre-trained model weights and injecting trainable
        rank decomposition matrices into each Transformer layer...
    """
    paper_id     = chunk.get("paper_id",       "unknown")
    title        = chunk.get("title",          "")
    authors_raw  = chunk.get("authors",        [])
    pub_date     = chunk.get("published_date", "")
    section      = chunk.get("section_title",  "")
    text         = chunk.get("text",           "").strip()

    authors_str = ", ".join(str(a) for a in authors_raw[:4])
    if len(authors_raw) > 4:
        authors_str += " et al."

    header = f"[Paper {paper_id}]"
    if title:
        header += f' "{title}"'
    meta = f"Authors: {authors_str} ({pub_date})"
    if section:
        meta += f" | Section: {section}"

    return f"{header}\n{meta}\n---\n{text}"


def _assemble_prompt(query: str, chunks: list[dict[str, Any]]) -> str:
    """
    Assemble the complete prompt string passed to the Answer Generator.

    Structure::

        SYSTEM:
        <system_prompt>

        CONTEXT:
        <chunk_block_1>

        <chunk_block_2>

        [... more chunks ...]

        QUESTION:
        <query>

        ANSWER:
    """
    if not chunks:
        return (
            f"SYSTEM:\n{_SYSTEM_PROMPT}\n\n"
            "CONTEXT:\n[No relevant research paper excerpts found.]\n\n"
            f"QUESTION:\n{query}\n\nANSWER:"
        )

    context_blocks = "\n\n".join(_format_chunk_block(c) for c in chunks)

    return (
        f"SYSTEM:\n{_SYSTEM_PROMPT}\n\n"
        f"CONTEXT:\n{context_blocks}\n\n"
        f"QUESTION:\n{query}\n\nANSWER:"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
