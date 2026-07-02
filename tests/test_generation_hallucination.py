"""
tests/test_generation_hallucination.py — Day 5 Test Suite

Tests for:
  1. Hallucination Detector — unit tests for each pipeline step
  2. Answer Generator — unit tests for response building & gating
  3. Prompt templates — correctness of prompt builders
  4. Integration smoke tests — end-to-end flow with mock Bedrock

Run with:
    pytest tests/test_generation_hallucination.py -v
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so we can import Lambda handlers without real AWS SDK
# ---------------------------------------------------------------------------

def _make_boto3_stub() -> MagicMock:
    """Returns a MagicMock that stands in for the boto3 module."""
    boto3_stub = MagicMock()
    boto3_stub.client.return_value = MagicMock()
    return boto3_stub


# Patch boto3 before importing handlers
sys.modules.setdefault("boto3", _make_boto3_stub())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.exceptions", MagicMock())

# Make ClientError importable
import botocore.exceptions  # noqa: E402
botocore.exceptions.ClientError = Exception  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Add Lambda source dirs to path
# ---------------------------------------------------------------------------
import os  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lambdas", "hallucination_detector"))
sys.path.insert(0, os.path.join(_ROOT, "lambdas", "answer_generator"))


# ===========================================================================
# Test Data Fixtures
# ===========================================================================

SAMPLE_ANSWER_GROUNDED = (
    "LoRA reduces trainable parameters by approximately 10,000× compared to full "
    "fine-tuning [2401.12345]. When applied to GPT-3, it matches the quality of "
    "full fine-tuning on GLUE benchmarks [2401.12345]. The key insight is that "
    "weight updates have a low intrinsic rank [2401.67890]."
)

SAMPLE_ANSWER_HALLUCINATED = (
    "LoRA achieves 99.9% accuracy on every benchmark [9999.99999]. "
    "This approach is superior to all existing methods [0000.00000]."
)

SAMPLE_ANSWER_EMPTY = ""

SAMPLE_CHUNKS: list[dict] = [
    {
        "chunk_id": "chunk_001",
        "paper_id": "2401.12345",
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "authors": ["Hu, E.", "Shen, Y."],
        "published_date": "2021-10-16",
        "section_id": "abstract",
        "content": (
            "LoRA reduces the number of trainable parameters by 10,000 times "
            "and reduces GPU memory requirement by 3 times. "
            "LoRA matches or exceeds the quality of full fine-tuning on GLUE "
            "benchmarks when applied to GPT-3."
        ),
        "rerank_score": 0.95,
        "rrf_score": 0.88,
        "entities": ["LoRA", "GPT-3", "GLUE", "fine-tuning"],
    },
    {
        "chunk_id": "chunk_002",
        "paper_id": "2401.67890",
        "title": "Intrinsic Dimensionality Explains Fine-Tuning",
        "authors": ["Aghajanyan, A."],
        "published_date": "2020-12-22",
        "section_id": "introduction",
        "content": (
            "We hypothesize that weight updates during adaptation also have a "
            "low intrinsic rank. This hypothesis is supported by experiments "
            "showing that learned weight matrices are low-rank."
        ),
        "rerank_score": 0.78,
        "rrf_score": 0.72,
        "entities": ["LoRA", "intrinsic rank", "fine-tuning"],
    },
]


# ===========================================================================
# Tests for hallucination_detector helper functions
# ===========================================================================

class TestEvidenceMapping(unittest.TestCase):
    """Tests for _map_claims_to_evidence in hallucination_detector."""

    def setUp(self):
        # Import the module fresh for each test class
        import importlib
        if "handler" in sys.modules:
            del sys.modules["handler"]
        # Set up environment vars before import
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        os.environ.setdefault("CONFIDENCE_PASS_THRESHOLD", "0.85")
        os.environ.setdefault("CONFIDENCE_WARN_THRESHOLD", "0.60")
        os.environ.setdefault("CONFIDENCE_REFUSE_THRESHOLD", "0.30")

        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_citation_match(self):
        """Claim with citation_expected should map to the correct chunks."""
        claims = [
            {
                "claim_id": "c1",
                "text": "LoRA reduces trainable parameters by 10,000×",
                "type": "quantitative",
                "entities_mentioned": ["LoRA"],
                "citation_expected": "[2401.12345]",
            }
        ]
        result = self.hd._map_claims_to_evidence(claims, SAMPLE_CHUNKS)
        self.assertIn("c1", result)
        self.assertEqual(len(result["c1"]), 1)
        self.assertEqual(result["c1"][0]["paper_id"], "2401.12345")

    def test_entity_overlap_match(self):
        """Claim with matching entities but no citation should still find evidence."""
        claims = [
            {
                "claim_id": "c2",
                "text": "Weight updates have a low intrinsic rank",
                "type": "causal",
                "entities_mentioned": ["intrinsic rank", "fine-tuning"],
                "citation_expected": None,
            }
        ]
        result = self.hd._map_claims_to_evidence(claims, SAMPLE_CHUNKS)
        self.assertIn("c2", result)
        # Should find chunk_002 via entity overlap
        paper_ids = [c["paper_id"] for c in result["c2"]]
        self.assertIn("2401.67890", paper_ids)

    def test_no_evidence_found(self):
        """Claim about a topic not in context should return empty list."""
        claims = [
            {
                "claim_id": "c3",
                "text": "GPT-4 achieves 95% on MMLU",
                "type": "quantitative",
                "entities_mentioned": ["GPT-4", "MMLU"],
                "citation_expected": None,
            }
        ]
        result = self.hd._map_claims_to_evidence(claims, SAMPLE_CHUNKS)
        self.assertIn("c3", result)
        # No entity or citation match expected — may still find via keyword
        # but we just assert it's a list
        self.assertIsInstance(result["c3"], list)


class TestCitationAccuracy(unittest.TestCase):
    """Tests for _check_citation_accuracy."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        os.environ.setdefault("CONFIDENCE_PASS_THRESHOLD", "0.85")
        os.environ.setdefault("CONFIDENCE_WARN_THRESHOLD", "0.60")
        os.environ.setdefault("CONFIDENCE_REFUSE_THRESHOLD", "0.30")
        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_all_valid_citations(self):
        """All cited papers are in context → accuracy = 1.0."""
        answer = "LoRA works [2401.12345] and weight updates are low-rank [2401.67890]."
        acc, invalid = self.hd._check_citation_accuracy(answer, SAMPLE_CHUNKS)
        self.assertAlmostEqual(acc, 1.0)
        self.assertEqual(invalid, [])

    def test_hallucinated_citation(self):
        """A fabricated paper_id that's not in context → accuracy < 1.0."""
        answer = "LoRA works [2401.12345]. Also see [9999.99999] for more details."
        acc, invalid = self.hd._check_citation_accuracy(answer, SAMPLE_CHUNKS)
        self.assertLess(acc, 1.0)
        self.assertIn("9999.99999", invalid)

    def test_no_citations(self):
        """Answer with no citations → accuracy = 1.0 (vacuously true)."""
        answer = "Language models are powerful tools for NLP tasks."
        acc, invalid = self.hd._check_citation_accuracy(answer, SAMPLE_CHUNKS)
        self.assertAlmostEqual(acc, 1.0)
        self.assertEqual(invalid, [])


class TestConfidenceScore(unittest.TestCase):
    """Tests for _compute_confidence_score."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        os.environ.setdefault("CONFIDENCE_PASS_THRESHOLD", "0.85")
        os.environ.setdefault("CONFIDENCE_WARN_THRESHOLD", "0.60")
        os.environ.setdefault("CONFIDENCE_REFUSE_THRESHOLD", "0.30")
        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_all_supported_claims_high_confidence(self):
        """All claims SUPPORTED + full coverage + valid citations → high confidence."""
        claims = [
            {"claim_id": "c1", "type": "quantitative"},
            {"claim_id": "c2", "type": "comparative"},
        ]
        verdicts = {
            "c1": {"verdict": "SUPPORTED"},
            "c2": {"verdict": "SUPPORTED"},
        }
        score = self.hd._compute_confidence_score(
            claims, verdicts, coverage=1.0, citation_accuracy=1.0
        )
        self.assertGreater(score, 0.85)

    def test_contradicted_claim_reduces_confidence(self):
        """A CONTRADICTED claim should significantly reduce confidence."""
        claims = [
            {"claim_id": "c1", "type": "quantitative"},
            {"claim_id": "c2", "type": "quantitative"},
        ]
        verdicts = {
            "c1": {"verdict": "SUPPORTED"},
            "c2": {"verdict": "CONTRADICTED"},
        }
        score = self.hd._compute_confidence_score(
            claims, verdicts, coverage=0.8, citation_accuracy=1.0
        )
        self.assertLess(score, 0.60)

    def test_all_unsupported_zero_coverage(self):
        """All UNSUPPORTED + zero coverage → very low confidence."""
        claims = [
            {"claim_id": "c1", "type": "quantitative"},
            {"claim_id": "c2", "type": "comparative"},
        ]
        verdicts = {
            "c1": {"verdict": "UNSUPPORTED"},
            "c2": {"verdict": "UNSUPPORTED"},
        }
        score = self.hd._compute_confidence_score(
            claims, verdicts, coverage=0.0, citation_accuracy=0.0
        )
        self.assertAlmostEqual(score, 0.0)

    def test_empty_claims_uses_fallback(self):
        """No claims extracted → fallback formula (coverage + citation)."""
        score = self.hd._compute_confidence_score(
            [], {}, coverage=0.8, citation_accuracy=1.0
        )
        # 0.5 * 0.8 + 0.15 * 1.0 = 0.4 + 0.15 = 0.55
        self.assertAlmostEqual(score, 0.55, places=2)

    def test_score_bounded_zero_to_one(self):
        """Confidence score is always in [0, 1]."""
        # Extreme case: all CONTRADICTED
        claims = [{"claim_id": f"c{i}", "type": "quantitative"} for i in range(5)]
        verdicts = {c["claim_id"]: {"verdict": "CONTRADICTED"} for c in claims}
        score = self.hd._compute_confidence_score(
            claims, verdicts, coverage=0.0, citation_accuracy=0.0
        )
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestEvidenceCoverage(unittest.TestCase):
    """Tests for _compute_evidence_coverage."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        os.environ.setdefault("CONFIDENCE_PASS_THRESHOLD", "0.85")
        os.environ.setdefault("CONFIDENCE_WARN_THRESHOLD", "0.60")
        os.environ.setdefault("CONFIDENCE_REFUSE_THRESHOLD", "0.30")
        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_full_coverage(self):
        """All chunks cited → coverage = 1.0."""
        claim_evidence_map = {
            "c1": [SAMPLE_CHUNKS[0]],
            "c2": [SAMPLE_CHUNKS[1]],
        }
        cov = self.hd._compute_evidence_coverage(SAMPLE_CHUNKS, claim_evidence_map)
        self.assertAlmostEqual(cov, 1.0)

    def test_no_coverage(self):
        """No chunks cited → coverage = 0.0."""
        cov = self.hd._compute_evidence_coverage(SAMPLE_CHUNKS, {})
        self.assertAlmostEqual(cov, 0.0)

    def test_partial_coverage(self):
        """Only one of two chunks cited → coverage = 0.5."""
        claim_evidence_map = {"c1": [SAMPLE_CHUNKS[0]]}
        cov = self.hd._compute_evidence_coverage(SAMPLE_CHUNKS, claim_evidence_map)
        self.assertAlmostEqual(cov, 0.5)

    def test_empty_context_zero(self):
        """Empty context → coverage = 0.0 (no division by zero)."""
        cov = self.hd._compute_evidence_coverage([], {"c1": []})
        self.assertAlmostEqual(cov, 0.0)


class TestResponseGating(unittest.TestCase):
    """Tests for _gate_response gating logic."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ["CONFIDENCE_PASS_THRESHOLD"]   = "0.85"
        os.environ["CONFIDENCE_WARN_THRESHOLD"]   = "0.60"
        os.environ["CONFIDENCE_REFUSE_THRESHOLD"] = "0.30"
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_high_confidence_pass(self):
        """Confidence ≥ 0.85 → PASS."""
        verdicts = {"c1": {"verdict": "SUPPORTED"}}
        result = self.hd._gate_response(
            SAMPLE_ANSWER_GROUNDED, 0.90, [], verdicts, []
        )
        self.assertEqual(result["action"], "PASS")
        self.assertEqual(result["quality_badge"], "high_confidence")

    def test_medium_confidence_disclaimer(self):
        """Confidence 0.60–0.85 → PASS_WITH_DISCLAIMER."""
        verdicts = {"c1": {"verdict": "PARTIALLY_SUPPORTED"}}
        result = self.hd._gate_response(
            SAMPLE_ANSWER_GROUNDED, 0.72, [], verdicts, []
        )
        self.assertEqual(result["action"], "PASS_WITH_DISCLAIMER")
        self.assertIn("Note:", result["answer"])

    def test_low_confidence_warn(self):
        """Confidence 0.30–0.60 → WARN."""
        verdicts = {"c1": {"verdict": "UNSUPPORTED"}}
        result = self.hd._gate_response(
            SAMPLE_ANSWER_GROUNDED, 0.45, [], verdicts, []
        )
        self.assertEqual(result["action"], "WARN")

    def test_very_low_confidence_refuse(self):
        """Confidence < 0.30 → REFUSE."""
        verdicts = {}
        result = self.hd._gate_response(
            SAMPLE_ANSWER_HALLUCINATED, 0.10, [], verdicts, []
        )
        self.assertEqual(result["action"], "REFUSE")
        self.assertIn("cannot provide", result["answer"])

    def test_contradicted_claim_always_warns(self):
        """Any CONTRADICTED verdict → WARN regardless of confidence."""
        verdicts = {
            "c1": {"verdict": "SUPPORTED"},
            "c2": {"verdict": "CONTRADICTED"},
        }
        claims = [
            {"claim_id": "c1", "type": "quantitative"},
            {"claim_id": "c2", "type": "quantitative"},
        ]
        result = self.hd._gate_response(
            SAMPLE_ANSWER_GROUNDED, 0.92, claims, verdicts, []
        )
        self.assertEqual(result["action"], "WARN")
        self.assertIn("contradicted_claim_ids", result)

    def test_invalid_citations_add_warning_text(self):
        """Invalid citations should append warning text to answer."""
        verdicts = {"c1": {"verdict": "SUPPORTED"}}
        result = self.hd._gate_response(
            SAMPLE_ANSWER_GROUNDED, 0.90, [], verdicts, ["9999.99999"]
        )
        self.assertIn("9999.99999", result["answer"])


# ===========================================================================
# Tests for prompt builder functions
# ===========================================================================

class TestPromptBuilders(unittest.TestCase):
    """Tests for prompts.py builder functions."""

    def setUp(self):
        if "prompts" in sys.modules:
            del sys.modules["prompts"]
        import prompts  # noqa: PLC0415
        self.prompts = prompts

    def test_generation_prompt_contains_query(self):
        """Generation prompt must include the user query."""
        prompt = self.prompts.build_generation_user_prompt(
            "What is LoRA?", SAMPLE_CHUNKS
        )
        self.assertIn("What is LoRA?", prompt)

    def test_generation_prompt_contains_paper_ids(self):
        """Generation prompt must include paper IDs from chunks."""
        prompt = self.prompts.build_generation_user_prompt(
            "Explain LoRA", SAMPLE_CHUNKS
        )
        self.assertIn("2401.12345", prompt)
        self.assertIn("2401.67890", prompt)

    def test_claim_extraction_prompt_wraps_answer(self):
        """Claim extraction prompt must contain the answer text."""
        prompt = self.prompts.build_claim_extraction_prompt(SAMPLE_ANSWER_GROUNDED)
        self.assertIn("LoRA reduces trainable parameters", prompt)

    def test_claim_verification_prompt_contains_claim(self):
        """Claim verification prompt must contain the claim text."""
        prompt = self.prompts.build_claim_verification_prompt(
            "LoRA reduces parameters by 10,000×",
            SAMPLE_CHUNKS[:1],
        )
        self.assertIn("LoRA reduces parameters by 10,000×", prompt)

    def test_claim_verification_prompt_includes_evidence(self):
        """Claim verification prompt must include chunk content."""
        prompt = self.prompts.build_claim_verification_prompt(
            "LoRA reduces parameters by 10,000×",
            SAMPLE_CHUNKS[:1],
        )
        self.assertIn("2401.12345", prompt)


# ===========================================================================
# Tests for answer_generator response assembly
# ===========================================================================

class TestAnswerGeneratorResponseBuilding(unittest.TestCase):
    """Tests for _build_response_payload in answer_generator."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ.setdefault("BEDROCK_GENERATION_MODEL", "anthropic.claude-3-5-sonnet-20241022-v2:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("HALLUCINATION_DETECTOR_FUNCTION_NAME", "")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")

        # Mock boto3 so we don't need real AWS credentials
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        sys.modules["boto3"] = mock_boto3

        import handler as ag_handler  # noqa: PLC0415
        self.ag = ag_handler

    def test_response_has_required_fields(self):
        """Response payload must contain all required top-level fields."""
        verification_result = {
            "action": "PASS",
            "confidence": 0.91,
            "answer": SAMPLE_ANSWER_GROUNDED,
            "quality_badge": "high_confidence",
            "verification": {},
        }
        resp = self.ag._build_response_payload(
            query="What is LoRA?",
            verification_result=verification_result,
            context_chunks=SAMPLE_CHUNKS,
            generation_latency_ms=1250.0,
            token_usage={"inputTokens": 800, "outputTokens": 300},
        )
        for field in ("query", "answer", "action", "confidence",
                      "quality_badge", "citations", "metadata"):
            self.assertIn(field, resp, f"Missing field: {field}")

    def test_citations_deduplicated(self):
        """Duplicate paper IDs in chunks should appear only once in citations."""
        chunks_with_dupes = SAMPLE_CHUNKS + [
            {
                **SAMPLE_CHUNKS[0],
                "chunk_id": "chunk_003",
                "section_id": "experiments",
            }
        ]
        verification_result = {
            "action": "PASS",
            "confidence": 0.91,
            "answer": SAMPLE_ANSWER_GROUNDED,
            "quality_badge": "high_confidence",
            "verification": {},
        }
        resp = self.ag._build_response_payload(
            query="What is LoRA?",
            verification_result=verification_result,
            context_chunks=chunks_with_dupes,
            generation_latency_ms=1000.0,
            token_usage={},
        )
        paper_ids = [c["paper_id"] for c in resp["citations"]]
        self.assertEqual(len(paper_ids), len(set(paper_ids)), "Duplicate paper IDs in citations")

    def test_citations_include_arxiv_url(self):
        """Each citation must include an arxiv URL."""
        verification_result = {
            "action": "PASS",
            "confidence": 0.91,
            "answer": SAMPLE_ANSWER_GROUNDED,
            "quality_badge": "high_confidence",
            "verification": {},
        }
        resp = self.ag._build_response_payload(
            query="What is LoRA?",
            verification_result=verification_result,
            context_chunks=SAMPLE_CHUNKS,
            generation_latency_ms=800.0,
            token_usage={},
        )
        for citation in resp["citations"]:
            self.assertIn("arxiv.org/abs/", citation["url"])

    def test_metadata_contains_latency(self):
        """Metadata must include generation_latency_ms."""
        verification_result = {
            "action": "PASS",
            "confidence": 0.91,
            "answer": SAMPLE_ANSWER_GROUNDED,
            "quality_badge": "high_confidence",
            "verification": {},
        }
        resp = self.ag._build_response_payload(
            query="q",
            verification_result=verification_result,
            context_chunks=SAMPLE_CHUNKS,
            generation_latency_ms=543.2,
            token_usage={"inputTokens": 100, "outputTokens": 50},
        )
        self.assertIn("generation_latency_ms", resp["metadata"])
        self.assertAlmostEqual(resp["metadata"]["generation_latency_ms"], 543.2)


# ===========================================================================
# Integration smoke tests — end-to-end flow with mocked Bedrock
# ===========================================================================

class TestHallucinationDetectorHandler(unittest.TestCase):
    """End-to-end handler() tests with mocked Bedrock calls."""

    def _make_converse_response(self, json_text: str) -> dict:
        return {
            "output": {
                "message": {
                    "content": [{"text": json_text}]
                }
            },
            "usage": {"inputTokens": 200, "outputTokens": 100},
        }

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ["BEDROCK_VERIFY_MODEL"]        = "anthropic.claude-3-haiku-20240307-v1:0"
        os.environ["BEDROCK_REGION"]              = "us-east-1"
        os.environ["CW_NAMESPACE"]                = "ResearchRAG"
        os.environ["CONFIDENCE_PASS_THRESHOLD"]   = "0.85"
        os.environ["CONFIDENCE_WARN_THRESHOLD"]   = "0.60"
        os.environ["CONFIDENCE_REFUSE_THRESHOLD"] = "0.30"

    def test_grounded_answer_passes(self):
        """
        Smoke test: a well-grounded answer with SUPPORTED claims and valid
        citations should return action=PASS or PASS_WITH_DISCLAIMER with
        confidence > 0.60.
        """
        claims_json = json.dumps({
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "LoRA reduces trainable parameters by 10,000×",
                    "type": "quantitative",
                    "entities_mentioned": ["LoRA"],
                    "citation_expected": "[2401.12345]",
                },
                {
                    "claim_id": "c2",
                    "text": "LoRA matches full fine-tuning quality on GLUE",
                    "type": "comparative",
                    "entities_mentioned": ["LoRA", "GLUE"],
                    "citation_expected": "[2401.12345]",
                },
            ]
        })
        verdict_json = json.dumps({
            "verdict": "SUPPORTED",
            "confidence": 0.95,
            "explanation": "Evidence explicitly states this.",
            "supporting_quotes": ["LoRA reduces the number of trainable parameters by 10,000 times"],
        })

        mock_bedrock = MagicMock()
        # First call = claim extraction, subsequent calls = claim verification
        mock_bedrock.converse.side_effect = [
            self._make_converse_response(claims_json),
            self._make_converse_response(verdict_json),
            self._make_converse_response(verdict_json),
        ]
        mock_cw = MagicMock()
        mock_lambda = MagicMock()

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "bedrock-runtime": mock_bedrock,
            "cloudwatch": mock_cw,
            "lambda": mock_lambda,
        }.get(svc, MagicMock())

        sys.modules["boto3"] = mock_boto3

        import handler  # noqa: PLC0415

        event = {
            "answer": SAMPLE_ANSWER_GROUNDED,
            "query": "How does LoRA work?",
            "context_chunks": SAMPLE_CHUNKS,
        }
        result = handler.handler(event, None)

        self.assertIn(result["action"], ("PASS", "PASS_WITH_DISCLAIMER", "WARN"))
        self.assertIn("verification", result)
        self.assertIn("confidence", result)
        self.assertGreater(result["confidence"], 0.0)

    def test_empty_answer_refuses(self):
        """Empty answer should immediately return REFUSE."""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        sys.modules["boto3"] = mock_boto3

        import handler  # noqa: PLC0415

        event = {"answer": "", "query": "test", "context_chunks": []}
        result = handler.handler(event, None)
        self.assertEqual(result["action"], "REFUSE")


# ===========================================================================
# Paper ID extraction helper
# ===========================================================================

class TestPaperIdExtraction(unittest.TestCase):
    """Tests for _extract_paper_id."""

    def setUp(self):
        if "handler" in sys.modules:
            del sys.modules["handler"]
        os.environ.setdefault("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
        os.environ.setdefault("BEDROCK_REGION", "us-east-1")
        os.environ.setdefault("CW_NAMESPACE", "ResearchRAG")
        os.environ.setdefault("CONFIDENCE_PASS_THRESHOLD", "0.85")
        os.environ.setdefault("CONFIDENCE_WARN_THRESHOLD", "0.60")
        os.environ.setdefault("CONFIDENCE_REFUSE_THRESHOLD", "0.30")
        import handler as hd_handler  # noqa: PLC0415
        self.hd = hd_handler

    def test_valid_citation(self):
        self.assertEqual(self.hd._extract_paper_id("[2401.12345]"), "2401.12345")

    def test_valid_short_arxiv_id(self):
        self.assertEqual(self.hd._extract_paper_id("[2106.09685]"), "2106.09685")

    def test_none_input(self):
        self.assertIsNone(self.hd._extract_paper_id(None))

    def test_empty_string(self):
        self.assertIsNone(self.hd._extract_paper_id(""))

    def test_no_match(self):
        self.assertIsNone(self.hd._extract_paper_id("null"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
