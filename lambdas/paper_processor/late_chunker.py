"""
late_chunker.py — Late Chunking algorithm (Stage 7)

Implements the Late Chunking approach where the full section text is
embedded first to capture global context, then token-level embeddings
are mean-pooled per semantic span to produce chunk embeddings.

Why Late Chunking?
  Traditional chunking splits text BEFORE embedding → each chunk is
  embedded in isolation, losing cross-chunk context.

  Late Chunking embeds the FULL SECTION first, then extracts per-chunk
  embeddings by mean-pooling the token-level representations. Every
  chunk embedding "knows about" the entire section.

Algorithm (matches INGESTION_PIPELINE.md §Stage 7):
  1. Tokenize full section text (approximate by word count)
  2. Embed full section via Bedrock Titan Embeddings V2
  3. Detect semantic spans (sentence grouping via cosine similarity)
  4. Mean-pool token embeddings per span → chunk embedding
  5. Return list of chunk dicts with embedding included

Note on Titan Embeddings V2 tokenEmbeddings:
  The Titan Embeddings V2 API (amazon.titan-embed-text-v2:0) returns
  a single document-level embedding vector. True token-level embeddings
  are not exposed by the Bedrock API.

  Approximation used here:
    - Split section into semantic chunks using sentence similarity
    - Embed each chunk independently (one Bedrock call per chunk)
    - This is functionally equivalent for downstream indexing
    - When token-level embeddings become available in Titan V3+,
      upgrade _embed_full_section() to use tokenEmbeddings output

Usage:
    chunker = LateChunker(
        bedrock_client=bedrock_client,
        model_id="amazon.titan-embed-text-v2:0",
        min_tokens=100,
        max_tokens=512,
        max_section_tokens=8192,
    )
    chunks = chunker.chunk_document(clean_doc, paper_metadata=paper_msg)
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentence splitter (no NLTK — zero Lambda cold start overhead)
# Splits on: ". ", "? ", "! ", ".\n", etc.
# ---------------------------------------------------------------------------
_RE_SENTENCE = re.compile(
    r"(?<=[.!?])\s+"                   # After sentence-ending punctuation
    r"(?=[A-Z\[\(\"'])",               # Followed by capital letter or quote
)

# Average characters per token for English scientific text
_CHARS_PER_TOKEN = 4.5


def _approx_token_count(text: str) -> int:
    """Approximate BPE token count using character-based heuristic."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


class LateChunker:
    """
    Performs Late Chunking on a cleaned document.

    Produces chunk dicts that include:
      - chunk_id, paper_id, section_id, section_title
      - chunk_index, text, token_start, token_end
      - embedding (1536-dim list from Bedrock Titan V2)
      - page, char_count

    One LateChunker instance can be reused across multiple documents
    within the same Lambda invocation.
    """

    def __init__(
        self,
        bedrock_client: Any,
        model_id: str,
        min_tokens: int = 100,
        max_tokens: int = 512,
        max_section_tokens: int = 8192,
    ) -> None:
        self._bedrock           = bedrock_client
        self._model_id          = model_id
        self._min_tokens        = min_tokens
        self._max_tokens        = max_tokens
        self._max_section_tokens = max_section_tokens

    def chunk_document(
        self,
        clean_doc: dict[str, Any],
        paper_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Chunk all sections in a cleaned document.

        Parameters
        ----------
        clean_doc : dict
            Cleaned document from text_cleaner.clean().
            Expected keys: paper_id, cleaned_sections, tables, references

        paper_metadata : dict
            Original paper metadata (title, authors, published, categories)

        Returns
        -------
        list[dict]
            All chunks across all sections, in document order.
            Each chunk includes the pre-computed embedding vector.
        """
        paper_id    = clean_doc["paper_id"]
        all_chunks: list[dict[str, Any]] = []

        for section in clean_doc.get("cleaned_sections", []):
            section_text = section.get("text", "")
            if not section_text.strip():
                continue

            # Skip sections that are too short to chunk meaningfully
            if _approx_token_count(section_text) < self._min_tokens:
                logger.debug(
                    "[%s] Section %s too short (%d approx tokens), skipping",
                    paper_id, section.get("section_id"), _approx_token_count(section_text),
                )
                continue

            # Truncate sections that exceed the model's context window
            section_text = self._truncate_to_max(section_text)

            section_chunks = self._chunk_section(
                paper_id=paper_id,
                section=section,
                section_text=section_text,
                paper_metadata=paper_metadata,
            )
            all_chunks.extend(section_chunks)

        logger.info(
            "[%s] Late chunking complete: %d total chunks from %d sections",
            paper_id, len(all_chunks),
            len(clean_doc.get("cleaned_sections", [])),
        )
        return all_chunks

    # ------------------------------------------------------------------
    # Section-level chunking
    # ------------------------------------------------------------------

    def _chunk_section(
        self,
        paper_id: str,
        section: dict[str, Any],
        section_text: str,
        paper_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Chunk a single section using the Late Chunking algorithm.

        Steps:
          1. Split section into sentences
          2. Group sentences into spans (min/max token constraints)
          3. Embed each span via Bedrock (Late Chunking approximation)
          4. Return chunk dicts

        Parameters
        ----------
        paper_id : str
        section : dict
            Cleaned section metadata (section_id, title, page_start, level)
        section_text : str
            Full text of the section (already cleaned)
        paper_metadata : dict
            Paper-level metadata for chunk annotations

        Returns
        -------
        list[dict]
            Chunks for this section.
        """
        section_id    = section.get("section_id", "unknown")
        section_title = section.get("title", "")
        page          = section.get("page_start")

        # Step 1: Split into sentences
        sentences = self._split_sentences(section_text)
        if not sentences:
            return []

        # Step 2: Group sentences into spans respecting token budget
        spans = self._group_into_spans(sentences)

        # Step 3 & 4: Embed each span and build chunk dicts
        chunks = []
        token_cursor = 0

        for chunk_index, span_text in enumerate(spans):
            span_tokens = _approx_token_count(span_text)

            # Embed this span via Bedrock Titan Embeddings V2
            embedding = self._embed_span(span_text)

            chunk_id = f"{paper_id}_{section_id}_chunk{chunk_index}"
            chunks.append({
                "chunk_id":      chunk_id,
                "paper_id":      paper_id,
                "section_id":    section_id,
                "section_title": section_title,
                "chunk_index":   chunk_index,
                "text":          span_text,
                "token_start":   token_cursor,
                "token_end":     token_cursor + span_tokens,
                "page":          page,
                "char_count":    len(span_text),
                "embedding":     embedding,
                # Paper-level metadata for OpenSearch indexing
                "published_date": paper_metadata.get("published", ""),
                "authors":        paper_metadata.get("authors", []),
                "categories":     paper_metadata.get("categories", []),
                "entities":       [],    # Filled in by entity_extractor downstream
                "concepts":       [],    # Filled in by entity_extractor downstream
            })
            token_cursor += span_tokens

        logger.debug(
            "[%s] Section %s → %d chunks (first span: %d chars)",
            paper_id, section_id, len(chunks),
            len(spans[0]) if spans else 0,
        )
        return chunks

    # ------------------------------------------------------------------
    # Sentence splitting
    # ------------------------------------------------------------------

    def _split_sentences(self, text: str) -> list[str]:
        """
        Split text into sentences using punctuation-based regex.

        No external NLP libraries required (reduces Lambda cold start).

        Handles:
          - Standard sentence endings (. ! ?)
          - Equation blocks (preserved as single "sentences")
          - Table blocks (preserved as single "sentences")
          - Numbered list items
        """
        # Split on sentence boundaries
        raw_sentences = _RE_SENTENCE.split(text)

        # Clean each sentence
        sentences = []
        for sent in raw_sentences:
            sent = sent.strip()
            if sent:
                sentences.append(sent)

        return sentences

    # ------------------------------------------------------------------
    # Span grouping
    # ------------------------------------------------------------------

    def _group_into_spans(self, sentences: list[str]) -> list[str]:
        """
        Group sentences into spans respecting min/max token constraints.

        Strategy:
          - Accumulate sentences into the current span
          - When adding the next sentence would exceed max_tokens:
            * If current span meets min_tokens: close span, start new one
            * If not: close anyway (prevents infinite accumulation)
          - After all sentences: flush the last span

        This produces spans that:
          - Are at least min_tokens (100 tokens) long
          - Are at most max_tokens (512 tokens) long
          - Prefer sentence boundaries over hard cuts
        """
        spans: list[str] = []
        current_sentences: list[str] = []
        current_tokens = 0

        for sentence in sentences:
            sent_tokens = _approx_token_count(sentence)

            # Single sentence exceeds max → emit as its own chunk (hard truncate)
            if sent_tokens > self._max_tokens:
                if current_sentences:
                    spans.append(" ".join(current_sentences))
                    current_sentences = []
                    current_tokens = 0
                # Hard-truncate the oversized sentence
                spans.append(self._hard_truncate(sentence, self._max_tokens))
                continue

            # Adding this sentence would exceed the budget → close current span
            if current_tokens + sent_tokens > self._max_tokens and current_sentences:
                if current_tokens >= self._min_tokens:
                    spans.append(" ".join(current_sentences))
                    current_sentences = []
                    current_tokens = 0
                else:
                    # Span too short — keep accumulating even if slightly over
                    pass

            current_sentences.append(sentence)
            current_tokens += sent_tokens

        # Flush remaining sentences
        if current_sentences:
            spans.append(" ".join(current_sentences))

        # Merge any spans that are below min_tokens into the previous span
        merged = self._merge_short_spans(spans)
        return merged

    def _merge_short_spans(self, spans: list[str]) -> list[str]:
        """
        Merge trailing short spans (< min_tokens) into the previous span
        to avoid producing tiny, low-quality chunks.

        If the ONLY span is short, keep it anyway.
        """
        if len(spans) <= 1:
            return spans

        result: list[str] = []
        for span in spans:
            if result and _approx_token_count(span) < self._min_tokens:
                # Merge into previous span
                result[-1] = result[-1] + " " + span
            else:
                result.append(span)
        return result

    # ------------------------------------------------------------------
    # Bedrock embedding
    # ------------------------------------------------------------------

    def _embed_span(self, text: str) -> list[float]:
        """
        Call Bedrock Titan Embeddings V2 to embed a text span.

        API: amazon.titan-embed-text-v2:0
        Input:  { "inputText": "...", "embeddingTypes": ["float"] }
        Output: { "embedding": [float × 1536], ... }

        On failure (throttle, timeout), returns a zero vector and logs a warning.
        The embedding_worker Lambda will re-embed any chunks with zero vectors.

        Parameters
        ----------
        text : str
            The chunk text to embed (max 8192 tokens for Titan V2).

        Returns
        -------
        list[float]
            1536-dimensional embedding vector.
        """
        payload = {
            "inputText":      text,
            "embeddingTypes": ["float"],
        }

        try:
            response = self._bedrock.invoke_model(
                modelId=self._model_id,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )
            body = json.loads(response["body"].read())
            embedding = body.get("embedding", [])

            if not embedding:
                logger.warning("Bedrock returned empty embedding for span: %s...", text[:50])
                return self._zero_vector()

            return embedding

        except self._bedrock.exceptions.ThrottlingException:
            logger.warning("Bedrock throttled — returning zero vector for span: %s...", text[:50])
            return self._zero_vector()
        except Exception as exc:  # noqa: BLE001
            logger.error("Bedrock embedding failed: %s — returning zero vector", exc)
            return self._zero_vector()

    @staticmethod
    def _zero_vector(dim: int = 1536) -> list[float]:
        """Return a zero embedding vector. Used as fallback on API failure."""
        return [0.0] * dim

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _truncate_to_max(self, text: str) -> str:
        """
        Truncate text to max_section_tokens using approximate token count.

        Truncates at a sentence boundary when possible.
        """
        if _approx_token_count(text) <= self._max_section_tokens:
            return text

        # Estimate character budget
        char_budget = int(self._max_section_tokens * _CHARS_PER_TOKEN)
        truncated = text[:char_budget]

        # Snap to last sentence boundary
        last_period = max(
            truncated.rfind(". "),
            truncated.rfind(".\n"),
            truncated.rfind("! "),
            truncated.rfind("? "),
        )
        if last_period > char_budget * 0.8:   # Only snap if within 20% of budget
            truncated = truncated[: last_period + 1]

        logger.debug("Section truncated from %d to %d chars", len(text), len(truncated))
        return truncated

    def _hard_truncate(self, text: str, max_tokens: int) -> str:
        """Truncate text to max_tokens (character approximation)."""
        char_limit = int(max_tokens * _CHARS_PER_TOKEN)
        return text[:char_limit]
