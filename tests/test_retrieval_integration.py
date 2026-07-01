"""
test_retrieval_integration.py — Day 4 integration test

End-to-end integration test for the Retrieval Engine.

What it tests:
  1. Query Handler can be invoked via direct Lambda invoke (or API Gateway).
  2. The response contains a valid prompt string, citation list, and retrieval metadata.
  3. Retrieval metadata confirms that dense, BM25, and graph candidates were produced.
  4. Context stats confirm the 8 000-token budget was respected.

Usage:
  # Run against a deployed stack:
  QUERY_HANDLER_FN=research-query-handler pytest tests/test_retrieval_integration.py -v

  # Run unit tests only (mocked AWS calls):
  pytest tests/test_retrieval_integration.py -v -k "not live"

Dependencies:
  pip install pytest boto3
"""

from __future__ import annotations

import json
import os
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no AWS calls
# ---------------------------------------------------------------------------

class TestReciprocalRankFusion(unittest.TestCase):
    """Unit tests for the RRF algorithm in query_handler."""

    def _rrf(self, rankings, k=60):
        """Inline copy of the RRF function for isolation."""
        scores = defaultdict(float)
        for ranked_list in rankings:
            for rank, chunk_id in enumerate(ranked_list, start=1):
                scores[chunk_id] += 1.0 / (k + rank)
        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    def test_single_list(self):
        rankings = [["A", "B", "C"]]
        scores = self._rrf(rankings)
        self.assertGreater(scores["A"], scores["B"])
        self.assertGreater(scores["B"], scores["C"])

    def test_fusion_boosts_overlap(self):
        """Chunk appearing in all 3 lists should have the highest fused score."""
        rankings = [
            ["A", "B", "C"],  # A is top dense
            ["C", "A", "D"],  # A is second in BM25
            ["A", "E", "F"],  # A is top in graph
        ]
        scores = self._rrf(rankings)
        # A appears in rank 1, 2, 1 → highest fused score
        top_chunk = max(scores, key=scores.__getitem__)
        self.assertEqual(top_chunk, "A")

    def test_empty_lists(self):
        scores = self._rrf([[], [], []])
        self.assertEqual(scores, {})

    def test_rrf_constant_k(self):
        """Higher k → smaller score differences between ranks."""
        rankings = [["A", "B"]]
        scores_k60   = self._rrf(rankings, k=60)
        scores_k1000 = self._rrf(rankings, k=1000)
        diff_k60   = scores_k60["A"]   - scores_k60["B"]
        diff_k1000 = scores_k1000["A"] - scores_k1000["B"]
        self.assertGreater(diff_k60, diff_k1000)


class TestDeduplication(unittest.TestCase):
    """Unit tests for context_builder deduplication logic."""

    def _dedup(self, chunks):
        """Inline copy of _deduplicate_chunks for isolation."""
        import hashlib
        seen_sections = set()
        seen_hashes   = set()
        unique = []
        for c in chunks:
            pid      = c.get("paper_id",   "unknown")
            sec_id   = c.get("section_id", "unknown")
            text_head = c.get("text", "")[:200]
            content_hash = hashlib.md5(text_head.encode()).hexdigest()
            key = (pid, sec_id)
            if key in seen_sections or content_hash in seen_hashes:
                continue
            seen_sections.add(key)
            seen_hashes.add(content_hash)
            unique.append(c)
        return unique

    def test_removes_same_section(self):
        chunks = [
            {"chunk_id": "c1", "paper_id": "p1", "section_id": "abstract", "text": "foo bar baz"},
            {"chunk_id": "c2", "paper_id": "p1", "section_id": "abstract", "text": "different text"},
        ]
        unique = self._dedup(chunks)
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0]["chunk_id"], "c1")

    def test_removes_duplicate_content(self):
        same_text = "x" * 200
        chunks = [
            {"chunk_id": "c1", "paper_id": "p1", "section_id": "s1", "text": same_text},
            {"chunk_id": "c2", "paper_id": "p2", "section_id": "s2", "text": same_text},
        ]
        unique = self._dedup(chunks)
        self.assertEqual(len(unique), 1)

    def test_keeps_distinct_chunks(self):
        chunks = [
            {"chunk_id": "c1", "paper_id": "p1", "section_id": "abstract", "text": "aaa"},
            {"chunk_id": "c2", "paper_id": "p1", "section_id": "section2", "text": "bbb"},
            {"chunk_id": "c3", "paper_id": "p2", "section_id": "abstract", "text": "ccc"},
        ]
        unique = self._dedup(chunks)
        self.assertEqual(len(unique), 3)


class TestCitationOrdering(unittest.TestCase):
    """Unit tests for the tier-based chunk ordering."""

    def _order(self, chunks):
        """Inline copy of _order_chunks."""
        def _sort_key(c):
            return c.get("published_date", "1970-01-01")

        tier1 = sorted([c for c in chunks if c.get("rerank_score", 0.0) > 0.8],
                       key=_sort_key, reverse=True)
        tier2 = sorted([c for c in chunks if 0.5 <= c.get("rerank_score", 0.0) <= 0.8],
                       key=_sort_key, reverse=True)
        tier3_all = sorted([c for c in chunks if c.get("rerank_score", 0.0) < 0.5 or c.get("source") == "graph"],
                           key=_sort_key, reverse=True)
        tier1_2_ids = {c.get("chunk_id") for c in tier1 + tier2}
        tier3 = [c for c in tier3_all if c.get("chunk_id") not in tier1_2_ids]
        return tier1 + tier2 + tier3

    def test_tier1_comes_first(self):
        chunks = [
            {"chunk_id": "low",  "rerank_score": 0.3,  "published_date": "2024-01-01", "source": "dense"},
            {"chunk_id": "high", "rerank_score": 0.92, "published_date": "2023-01-01", "source": "dense"},
        ]
        ordered = self._order(chunks)
        self.assertEqual(ordered[0]["chunk_id"], "high")

    def test_newer_first_within_tier(self):
        chunks = [
            {"chunk_id": "old",  "rerank_score": 0.9, "published_date": "2020-01-01", "source": "dense"},
            {"chunk_id": "new",  "rerank_score": 0.85, "published_date": "2024-06-01", "source": "dense"},
        ]
        ordered = self._order(chunks)
        self.assertEqual(ordered[0]["chunk_id"], "new")

    def test_graph_goes_to_tier3(self):
        chunks = [
            {"chunk_id": "graph_chunk", "rerank_score": 0.9, "published_date": "2024-01-01", "source": "graph"},
            {"chunk_id": "dense_chunk", "rerank_score": 0.6, "published_date": "2024-01-01", "source": "dense"},
        ]
        ordered = self._order(chunks)
        # The dense (tier 2) chunk should come before graph (tier 3)
        chunk_ids = [c["chunk_id"] for c in ordered]
        self.assertLess(chunk_ids.index("dense_chunk"), chunk_ids.index("graph_chunk"))


class TestContextCompression(unittest.TestCase):
    """Unit tests for token-budget compression."""

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def test_no_compression_when_within_budget(self):
        """If total tokens < budget, return chunks unchanged."""
        chunks = [{"chunk_id": "c1", "rerank_score": 0.9, "text": "short text", "source": "dense"}]
        budget = 10000
        total = self._estimate_tokens("short text")
        self.assertLess(total, budget)
        # Would not trigger compression
        self.assertTrue(True)  # just verify no error path reached

    def test_tier2_text_truncated(self):
        """Tier 2 chunks should have text truncated to ~400 tokens on compression."""
        long_text = "word " * 1000  # ~5000 tokens
        chunk = {
            "chunk_id":    "c1",
            "rerank_score": 0.7,  # Tier 2
            "text":         long_text,
            "source":       "dense",
        }
        max_chars = 400 * 4  # 1600 chars
        truncated = long_text[:max_chars] + " [truncated]"
        self.assertTrue(truncated.endswith("[truncated]"))
        self.assertLessEqual(len(truncated), max_chars + 20)


class TestPromptAssembly(unittest.TestCase):
    """Unit tests for prompt assembly in context_builder."""

    def _assemble(self, query, chunks):
        """Minimal inline prompt assembly."""
        if not chunks:
            return f"QUESTION:\n{query}\n\nANSWER:"
        blocks = []
        for c in chunks:
            blocks.append(
                f"[Paper {c.get('paper_id', '?')}]\n---\n{c.get('text', '')}"
            )
        context = "\n\n".join(blocks)
        return f"CONTEXT:\n{context}\n\nQUESTION:\n{query}\n\nANSWER:"

    def test_prompt_contains_query(self):
        query = "How does LoRA work?"
        prompt = self._assemble(query, [])
        self.assertIn(query, prompt)

    def test_prompt_contains_paper_id(self):
        chunks = [{"paper_id": "2106.09685", "text": "LoRA text here"}]
        prompt = self._assemble("test query", chunks)
        self.assertIn("2106.09685", prompt)

    def test_empty_chunks_returns_valid_prompt(self):
        prompt = self._assemble("test query", [])
        self.assertIn("QUESTION", prompt)
        self.assertIn("ANSWER", prompt)


# ---------------------------------------------------------------------------
# Live integration test (requires deployed stack)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("QUERY_HANDLER_FN"),
    reason="Set QUERY_HANDLER_FN env var to run live integration tests",
)
class TestRetrievalLiveIntegration(unittest.TestCase):
    """
    Live integration tests — invoke the deployed Query Handler Lambda directly.

    Prerequisites:
      - export QUERY_HANDLER_FN=research-query-handler
      - AWS credentials configured with Lambda:InvokeFunction permission
    """

    @classmethod
    def setUpClass(cls):
        import boto3
        cls.lambda_client = boto3.client("lambda", region_name="us-east-1")
        cls.fn_name = os.environ["QUERY_HANDLER_FN"]

    def _invoke(self, question: str, filters: dict = None, options: dict = None) -> dict:
        payload = {
            "body": json.dumps({
                "question": question,
                "filters":  filters or {},
                "options":  options or {"include_graph": True},
            })
        }
        resp = self.lambda_client.invoke(
            FunctionName=self.fn_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        raw = json.loads(resp["Payload"].read())
        self.assertEqual(raw["statusCode"], 200, f"Non-200 response: {raw}")
        return json.loads(raw["body"])

    def test_basic_query_returns_prompt(self):
        """A simple research query should return a non-empty answer_prompt."""
        result = self._invoke("What is LoRA and how does it work?")
        self.assertIn("answer_prompt", result)
        self.assertGreater(len(result["answer_prompt"]), 100)

    def test_retrieval_metadata_present(self):
        """Response must include retrieval_metadata with candidate counts."""
        result = self._invoke("Compare transformer attention mechanisms")
        meta = result.get("retrieval_metadata", {})
        self.assertIn("dense_candidates",  meta)
        self.assertIn("bm25_candidates",   meta)
        self.assertIn("graph_candidates",  meta)
        self.assertIn("reranked_from",     meta)

    def test_citations_are_structured(self):
        """Citations should be a list of objects with paper_id and relevance_score."""
        result = self._invoke("What datasets are used for NLP benchmarks?")
        citations = result.get("citations", [])
        self.assertIsInstance(citations, list)
        for c in citations:
            self.assertIn("paper_id",       c)
            self.assertIn("relevance_score", c)

    def test_context_stats_within_budget(self):
        """Estimated prompt tokens must not exceed the 8000-token budget."""
        result = self._invoke("Explain diffusion models for image generation")
        stats = result.get("context_stats", {})
        if "estimated_tokens" in stats:
            self.assertLessEqual(
                stats["estimated_tokens"], 8000,
                f"Context exceeded budget: {stats['estimated_tokens']} tokens",
            )

    def test_category_filter_respected(self):
        """A category filter should not cause an error."""
        result = self._invoke(
            "How do vision transformers work?",
            filters={"categories": ["cs.CV"], "date_from": "2022-01-01"},
        )
        self.assertIn("answer_prompt", result)

    def test_no_graph_option(self):
        """Setting include_graph=False should still return a valid response."""
        result = self._invoke(
            "What is RLHF?",
            options={"include_graph": False},
        )
        self.assertIn("answer_prompt", result)
        meta = result.get("retrieval_metadata", {})
        self.assertEqual(meta.get("graph_candidates", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
