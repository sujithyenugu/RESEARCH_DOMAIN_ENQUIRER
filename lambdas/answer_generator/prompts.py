"""
prompts.py — Prompt templates for Answer Generation & Hallucination Detection

All prompt strings live here so they can be imported by both Lambda handlers
and unit tests without circular dependencies.

Exported constants:
  ANSWER_GENERATION_SYSTEM     — system prompt for Claude 3.5 Sonnet
  CLAIM_EXTRACTION_SYSTEM      — system prompt for claim extraction (Claude Haiku)
  CLAIM_VERIFICATION_SYSTEM    — system prompt for claim verification (Claude Haiku)

Exported functions:
  build_generation_user_prompt(query, context_blocks)  → str
  build_claim_extraction_prompt(answer)                → str
  build_claim_verification_prompt(claim, evidence)     → str
"""

from __future__ import annotations

# ===========================================================================
# Answer Generation
# ===========================================================================

ANSWER_GENERATION_SYSTEM = """\
You are a rigorous AI research assistant. Your task is to answer questions \
about AI research using ONLY the provided research paper excerpts.

Rules you MUST follow:
1. Every factual claim MUST be followed immediately by a citation in [paper_id] format \
(e.g., [2401.12345]).
2. Only use information explicitly present in the provided context chunks.
3. Do NOT speculate, infer beyond what is written, or introduce external knowledge.
4. If multiple papers support a claim, cite all of them: [paper_id_1][paper_id_2].
5. If the context does not contain enough information to answer, say so clearly.
6. Structure your answer with clear paragraphs. Use bullet points for comparisons or lists.
7. Quantitative results (accuracy %, parameter counts, latency) must be cited \
exactly as stated in the source.

Citation format: [arxiv_id] — e.g., [2401.12345] or [2106.09685]

Answer in a professional, academic tone suitable for a researcher audience.\
"""


def build_generation_user_prompt(
    query: str,
    context_blocks: list[dict],
) -> str:
    """
    Assembles the user-turn message for Claude 3.5 Sonnet.

    Args:
        query:          The original research question.
        context_blocks: List of chunk dicts with keys:
                          paper_id, title, authors, published_date,
                          section_id, content, rerank_score.

    Returns:
        Full user prompt string.
    """
    lines: list[str] = []

    # --- Context section ---
    lines.append("## Retrieved Research Paper Excerpts\n")
    for i, chunk in enumerate(context_blocks, 1):
        paper_id    = chunk.get("paper_id", "unknown")
        title       = chunk.get("title", "Untitled")
        section     = chunk.get("section_id", "unknown")
        content     = chunk.get("content", "").strip()
        score       = chunk.get("rerank_score", chunk.get("rrf_score", 0.0))

        lines.append(
            f"### [{paper_id}] {title}\n"
            f"**Section:** {section}  |  **Relevance score:** {score:.3f}\n\n"
            f"{content}\n"
        )

    # --- Question section ---
    lines.append("---\n")
    lines.append("## Research Question\n")
    lines.append(query)
    lines.append("\n---\n")
    lines.append(
        "## Your Answer\n"
        "Answer the research question using only the excerpts above. "
        "Include inline citations [paper_id] for every claim.\n"
    )

    return "\n".join(lines)


# ===========================================================================
# Claim Extraction
# ===========================================================================

CLAIM_EXTRACTION_SYSTEM = """\
You are a fact-checking assistant. Extract all factual claims from \
the following AI research answer. Break compound claims into atomic statements. \
Focus on quantitative claims, method comparisons, and dataset results.

Claim types:
  quantitative   — involves numbers, percentages, parameter counts, latency, etc.
  comparative    — compares two or more methods/models/datasets
  causal         — states a cause-effect relationship
  definitional   — defines what something is
  existence      — asserts the existence of a property or phenomenon

Output ONLY valid JSON in this exact format (no markdown, no prose):
{
  "claims": [
    {
      "claim_id": "c1",
      "text": "<atomic claim text>",
      "type": "<quantitative|comparative|causal|definitional|existence>",
      "entities_mentioned": ["<entity1>", "<entity2>"],
      "citation_expected": "<[paper_id] or null>"
    }
  ]
}\
"""


def build_claim_extraction_prompt(answer: str) -> str:
    """Returns the user prompt for claim extraction."""
    return f"Answer to analyze:\n\n{answer}"


# ===========================================================================
# Claim Verification
# ===========================================================================

CLAIM_VERIFICATION_SYSTEM = """\
You are a rigorous fact-checker for AI research. Determine whether \
the given claim is supported by the provided evidence chunks.

Be strict: only mark SUPPORTED if the evidence explicitly states or directly \
implies the claim. Do not infer beyond what is written.

Verdict definitions:
  SUPPORTED           — evidence explicitly supports the claim
  PARTIALLY_SUPPORTED — evidence weakly or indirectly supports
  UNSUPPORTED         — no evidence found for the claim
  CONTRADICTED        — evidence contradicts the claim

Output ONLY valid JSON (no markdown, no prose):
{
  "verdict": "SUPPORTED|PARTIALLY_SUPPORTED|UNSUPPORTED|CONTRADICTED",
  "confidence": <0.0-1.0>,
  "explanation": "<brief reason, 1-2 sentences>",
  "supporting_quotes": ["<exact quote from evidence that supports the claim>"]
}\
"""


def build_claim_verification_prompt(
    claim_text: str,
    evidence_chunks: list[dict],
) -> str:
    """
    Returns the user prompt for a single claim verification.

    Args:
        claim_text:      The atomic claim to verify.
        evidence_chunks: Relevant context chunks (max 3 used).
    """
    lines: list[str] = [f"Claim: {claim_text}\n", "Evidence:"]
    for i, chunk in enumerate(evidence_chunks[:3], 1):
        pid     = chunk.get("paper_id", "unknown")
        section = chunk.get("section_id", "unknown")
        content = chunk.get("content", "")[:800]
        lines.append(f"\n[Chunk {i} from paper {pid} — {section}]\n{content}")
    return "\n".join(lines)
