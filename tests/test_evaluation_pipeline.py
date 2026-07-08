"""
tests/test_evaluation_pipeline.py — Day 8: Evaluation Pipeline Test Suite

Unit tests covering:
  1.  compute_recall_at_k              — basic, empty relevant, k cutoff
  2.  compute_mrr                      — first-rank hit, mid-rank, miss
  3.  compute_hit_rate                 — hit, miss, empty relevant
  4.  compute_ndcg_at_k               — binary relevance, custom grades, empty
  5.  compute_citation_accuracy        — valid citations, invalid, mixed, no citations
  6.  compute_context_utilization      — full, partial, none cited
  7.  compute_faithfulness             — with verdicts, without verdicts (fallback)
  8.  compute_groundedness             — with verdicts, partial not counted
  9.  _percentile                      — p50, p95, p99, empty
  10. _aggregate_metrics               — mean computation, latency percentiles
  11. _check_regression_and_alert      — no regression, warning, critical
  12. online_handler                   — metric payload, CloudWatch call count
  13. offline_handler (smoke)          — invocation with empty golden dataset

Run:
  pytest tests/test_evaluation_pipeline.py -v
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call
from io import BytesIO

# ---------------------------------------------------------------------------
# Stub AWS SDK before any handler import
# ---------------------------------------------------------------------------

def _make_boto3_stub() -> types.ModuleType:
    """Create a minimal boto3 stub so handler.py can be imported without AWS."""
    boto3_mod = types.ModuleType("boto3")

    def client(service, **kwargs):  # noqa: ARG001
        return MagicMock()

    def resource(service, **kwargs):  # noqa: ARG001
        return MagicMock()

    boto3_mod.client   = client
    boto3_mod.resource = resource
    return boto3_mod


if "boto3" not in sys.modules:
    sys.modules["boto3"] = _make_boto3_stub()

# Add lambdas/evaluator to path so we can import metrics directly
import os
_EVALUATOR_PATH = os.path.join(os.path.dirname(__file__), "..", "lambdas", "evaluator")
if _EVALUATOR_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_EVALUATOR_PATH))

import metrics as M  # noqa: E402  (after path manipulation)


# ===========================================================================
# RETRIEVAL METRIC TESTS
# ===========================================================================

class TestRecallAtK(unittest.TestCase):

    def test_perfect_recall(self):
        retrieved = ["A", "B", "C", "D"]
        relevant  = ["A", "B"]
        self.assertEqual(M.compute_recall_at_k(retrieved, relevant), 1.0)

    def test_partial_recall(self):
        retrieved = ["A", "X", "Y"]
        relevant  = ["A", "B", "C"]
        self.assertAlmostEqual(M.compute_recall_at_k(retrieved, relevant), 1/3, places=3)

    def test_zero_recall(self):
        retrieved = ["X", "Y", "Z"]
        relevant  = ["A", "B"]
        self.assertEqual(M.compute_recall_at_k(retrieved, relevant), 0.0)

    def test_empty_relevant(self):
        # Vacuously perfect when there is nothing to recall
        self.assertEqual(M.compute_recall_at_k(["A"], []), 1.0)

    def test_k_cutoff(self):
        # Only first 2 items considered, A is relevant but at rank 3 — miss
        retrieved = ["X", "Y", "A"]
        relevant  = ["A"]
        self.assertEqual(M.compute_recall_at_k(retrieved, relevant, k=2), 0.0)

    def test_k_cutoff_hit(self):
        retrieved = ["A", "X", "Y"]
        relevant  = ["A"]
        self.assertEqual(M.compute_recall_at_k(retrieved, relevant, k=2), 1.0)


class TestMRR(unittest.TestCase):

    def test_first_rank_hit(self):
        self.assertEqual(M.compute_mrr(["A", "B"], ["A"]), 1.0)

    def test_second_rank_hit(self):
        self.assertAlmostEqual(M.compute_mrr(["X", "A", "B"], ["A"]), 0.5, places=3)

    def test_third_rank_hit(self):
        self.assertAlmostEqual(M.compute_mrr(["X", "Y", "A"], ["A"]), 1/3, places=3)

    def test_miss(self):
        self.assertEqual(M.compute_mrr(["X", "Y", "Z"], ["A"]), 0.0)

    def test_multiple_relevant_returns_first(self):
        # Relevant at ranks 2 and 4 — MRR = 1/2 (first relevant rank)
        self.assertAlmostEqual(M.compute_mrr(["X", "A", "Y", "B"], ["A", "B"]), 0.5, places=3)


class TestHitRate(unittest.TestCase):

    def test_hit(self):
        self.assertEqual(M.compute_hit_rate(["A", "B"], ["A"]), 1.0)

    def test_miss(self):
        self.assertEqual(M.compute_hit_rate(["X", "Y"], ["A"]), 0.0)

    def test_empty_relevant(self):
        self.assertEqual(M.compute_hit_rate(["X"], []), 1.0)


class TestNDCGAtK(unittest.TestCase):

    def test_perfect_ndcg(self):
        # All retrieved are relevant, ideal order → nDCG = 1.0
        result = M.compute_ndcg_at_k(["A", "B"], ["A", "B"], k=2)
        self.assertAlmostEqual(result, 1.0, places=3)

    def test_zero_ndcg(self):
        result = M.compute_ndcg_at_k(["X", "Y"], ["A", "B"], k=2)
        self.assertEqual(result, 0.0)

    def test_partial_ndcg(self):
        # Relevant = [A, B], retrieved = [A, X, X, B]
        result = M.compute_ndcg_at_k(["A", "X", "X", "B"], ["A", "B"], k=4)
        self.assertGreater(result, 0.0)
        self.assertLess(result, 1.0)

    def test_empty_relevant(self):
        self.assertEqual(M.compute_ndcg_at_k(["A"], [], k=5), 1.0)

    def test_custom_relevance_grades(self):
        grades = {"A": 3, "B": 2, "C": 1}
        result = M.compute_ndcg_at_k(["A", "B", "C"], ["A", "B", "C"], k=3,
                                     relevance_scores=grades)
        self.assertAlmostEqual(result, 1.0, places=2)


# ===========================================================================
# GENERATION METRIC TESTS
# ===========================================================================

class TestCitationAccuracy(unittest.TestCase):

    def _make_chunks(self, paper_ids):
        return [{"paper_id": pid} for pid in paper_ids]

    def test_all_valid(self):
        answer = "LoRA works [2106.09685] and attention scales [1706.03762]."
        chunks = self._make_chunks(["2106.09685", "1706.03762"])
        self.assertEqual(M.compute_citation_accuracy(answer, chunks), 1.0)

    def test_all_invalid(self):
        answer = "See [9999.99999] for details."
        chunks = self._make_chunks(["2106.09685"])
        self.assertEqual(M.compute_citation_accuracy(answer, chunks), 0.0)

    def test_mixed_citations(self):
        answer = "Valid [2106.09685] and invalid [9999.99999]."
        chunks = self._make_chunks(["2106.09685"])
        self.assertAlmostEqual(M.compute_citation_accuracy(answer, chunks), 0.5, places=3)

    def test_no_citations(self):
        # No [YYYY.NNNNN] patterns → neutral 1.0
        answer = "LoRA reduces parameters significantly."
        self.assertEqual(M.compute_citation_accuracy(answer, []), 1.0)

    def test_empty_answer(self):
        self.assertEqual(M.compute_citation_accuracy("", []), 1.0)


class TestContextUtilization(unittest.TestCase):

    def _make_chunks(self, paper_ids):
        return [{"paper_id": pid} for pid in paper_ids]

    def test_full_utilization(self):
        answer = "See [2106.09685] and [1706.03762]."
        chunks = self._make_chunks(["2106.09685", "1706.03762"])
        self.assertEqual(M.compute_context_utilization(answer, chunks), 1.0)

    def test_partial_utilization(self):
        answer = "See [2106.09685]."
        chunks = self._make_chunks(["2106.09685", "1706.03762"])
        self.assertAlmostEqual(M.compute_context_utilization(answer, chunks), 0.5, places=3)

    def test_zero_utilization(self):
        answer = "No citations here."
        chunks = self._make_chunks(["2106.09685"])
        self.assertEqual(M.compute_context_utilization(answer, chunks), 0.0)


class TestFaithfulness(unittest.TestCase):

    def _make_chunks_with_verdicts(self, verdicts):
        return [
            {"paper_id": "test", "claims": [{"verdict": v} for v in verdicts]}
        ]

    def test_all_supported(self):
        chunks = self._make_chunks_with_verdicts(["SUPPORTED", "SUPPORTED"])
        result = M.compute_faithfulness("test [9999.99999]", chunks)
        self.assertEqual(result, 1.0)

    def test_partially_supported_counts(self):
        chunks = self._make_chunks_with_verdicts(
            ["SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED"]
        )
        result = M.compute_faithfulness("test", chunks)
        self.assertAlmostEqual(result, 2/3, places=3)

    def test_all_unsupported(self):
        chunks = self._make_chunks_with_verdicts(["UNSUPPORTED", "CONTRADICTED"])
        result = M.compute_faithfulness("test", chunks)
        self.assertEqual(result, 0.0)

    def test_fallback_to_citation_accuracy(self):
        # No verdicts → fallback to citation accuracy
        chunks = [{"paper_id": "2106.09685"}]
        answer = "See [2106.09685] for details."
        result = M.compute_faithfulness(answer, chunks)
        self.assertEqual(result, 1.0)


class TestGroundedness(unittest.TestCase):

    def _make_chunks_with_verdicts(self, verdicts):
        return [
            {"paper_id": "test", "claims": [{"verdict": v} for v in verdicts]}
        ]

    def test_all_supported(self):
        chunks = self._make_chunks_with_verdicts(["SUPPORTED", "SUPPORTED"])
        self.assertEqual(M.compute_groundedness("test", chunks), 1.0)

    def test_partial_does_not_count(self):
        # PARTIALLY_SUPPORTED should NOT count in groundedness
        chunks = self._make_chunks_with_verdicts(
            ["SUPPORTED", "PARTIALLY_SUPPORTED"]
        )
        result = M.compute_groundedness("test", chunks)
        self.assertAlmostEqual(result, 0.5, places=3)

    def test_zero_groundedness(self):
        chunks = self._make_chunks_with_verdicts(["PARTIALLY_SUPPORTED", "UNSUPPORTED"])
        self.assertEqual(M.compute_groundedness("test", chunks), 0.0)


# ===========================================================================
# HANDLER INTERNAL HELPER TESTS
# ===========================================================================

class TestPercentile(unittest.TestCase):

    def setUp(self):
        # Import private helper
        import importlib
        import handler as H
        self.H = H

    def test_p50(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = self.H._percentile(values, 50)
        self.assertIn(result, [2.0, 3.0])  # midpoint varies by floor

    def test_p95(self):
        values = list(range(100))
        result = self.H._percentile(values, 95)
        self.assertAlmostEqual(result, 95, delta=2)

    def test_empty_list(self):
        self.assertEqual(self.H._percentile([], 50), 0.0)


class TestAggregateMetrics(unittest.TestCase):

    def setUp(self):
        import importlib
        import handler as H
        self.H = H

    def _make_result(self, score: float, latency: float = 2000.0) -> dict:
        return {
            "eval_id": "test",
            "question": "Q",
            "metrics": {
                "recall_at_5": score,
                "recall_at_10": score,
                "mrr": score,
                "hit_rate_at_10": score,
                "ndcg_at_10": score,
                "faithfulness": score,
                "groundedness": score,
                "citation_accuracy": score,
                "answer_relevance": score,
                "e2e_latency_ms": latency,
                "confidence": score,
            },
        }

    def test_mean_computation(self):
        results = [self._make_result(0.8), self._make_result(0.6)]
        agg = self.H._aggregate_metrics(results)
        self.assertAlmostEqual(agg["recall_at_10"], 0.7, places=3)

    def test_latency_percentiles_present(self):
        results = [self._make_result(0.9, latency=float(i * 100)) for i in range(1, 101)]
        agg = self.H._aggregate_metrics(results)
        self.assertIn("e2e_latency_p50_ms", agg)
        self.assertIn("e2e_latency_p95_ms", agg)
        self.assertIn("e2e_latency_p99_ms", agg)

    def test_empty_results(self):
        agg = self.H._aggregate_metrics([])
        self.assertEqual(agg, {})


class TestRegressionCheck(unittest.TestCase):

    def setUp(self):
        import handler as H
        self.H = H

    def test_no_regression(self):
        current   = {"recall_at_10": 0.85, "faithfulness": 0.90, "groundedness": 0.80,
                     "citation_accuracy": 0.97, "e2e_latency_p95_ms": 4000.0}
        historical = [
            {"recall_at_10": "0.85", "faithfulness": "0.90", "groundedness": "0.80",
             "citation_accuracy": "0.97", "e2e_latency_p95_ms": "4000"}
        ] * 7
        # Should NOT publish — no exception raised
        with patch.object(self.H, "_publish_regression_alert") as mock_alert:
            self.H._check_regression_and_alert(current, historical)
            mock_alert.assert_not_called()

    def test_recall_regression_triggers_alert(self):
        current   = {"recall_at_10": 0.65, "faithfulness": 0.90, "groundedness": 0.80,
                     "citation_accuracy": 0.97, "e2e_latency_p95_ms": 4000.0}
        historical = [
            {"recall_at_10": "0.85", "faithfulness": "0.90", "groundedness": "0.80",
             "citation_accuracy": "0.97", "e2e_latency_p95_ms": "4000"}
        ] * 7
        with patch.object(self.H, "_publish_regression_alert") as mock_alert:
            self.H._check_regression_and_alert(current, historical)
            mock_alert.assert_called_once()
            regressions = mock_alert.call_args[0][0]
            metrics_flagged = [r["metric"] for r in regressions]
            self.assertIn("recall_at_10", metrics_flagged)

    def test_empty_historical_skips_check(self):
        current = {"recall_at_10": 0.5}
        with patch.object(self.H, "_publish_regression_alert") as mock_alert:
            self.H._check_regression_and_alert(current, [])
            mock_alert.assert_not_called()


# ===========================================================================
# ONLINE HANDLER TESTS
# ===========================================================================

class TestOnlineHandler(unittest.TestCase):

    def setUp(self):
        import handler as H
        self.H = H

    def _make_event(self, confidence=0.85, action="PASS", e2e_ms=2000.0):
        return {
            "query":   "What is LoRA?",
            "answer":  "LoRA uses low-rank matrices [2106.09685].",
            "confidence": confidence,
            "action":  action,
            "citations": ["2106.09685"],
            "context_chunks": [{"paper_id": "2106.09685"}],
            "latency_ms": {
                "total_e2e": e2e_ms,
                "query_understanding": 100,
                "dense_retrieval": 150,
                "llm_generation": 1000,
            },
        }

    def test_returns_ok_status(self):
        with patch.object(self.H, "_publish_metrics"):
            result = self.H.online_handler(self._make_event(), None)
        self.assertEqual(result["status"], "ok")

    def test_metrics_keys_present(self):
        with patch.object(self.H, "_publish_metrics"):
            result = self.H.online_handler(self._make_event(), None)
        for key in ("confidence", "citation_accuracy", "chunk_count", "e2e_latency_ms"):
            self.assertIn(key, result["metrics"])

    def test_refusal_rate_metric_for_refuse_action(self):
        """Refusal action should set is_refusal=1.0 in the published metrics."""
        published: list = []

        def capture(metrics):
            published.extend(metrics)

        with patch.object(self.H, "_publish_metrics", side_effect=capture):
            self.H.online_handler(self._make_event(action="REFUSE"), None)

        refusal_metrics = [m for m in published if m["MetricName"] == "RefusalRate"]
        self.assertEqual(len(refusal_metrics), 1)
        self.assertEqual(refusal_metrics[0]["Value"], 1.0)

    def test_pass_action_refusal_rate_zero(self):
        published: list = []

        def capture(metrics):
            published.extend(metrics)

        with patch.object(self.H, "_publish_metrics", side_effect=capture):
            self.H.online_handler(self._make_event(action="PASS"), None)

        refusal_metrics = [m for m in published if m["MetricName"] == "RefusalRate"]
        self.assertEqual(refusal_metrics[0]["Value"], 0.0)

    def test_citation_accuracy_in_returned_metrics(self):
        with patch.object(self.H, "_publish_metrics"):
            result = self.H.online_handler(self._make_event(), None)
        self.assertAlmostEqual(result["metrics"]["citation_accuracy"], 1.0, places=3)

    def test_e2e_latency_propagated(self):
        with patch.object(self.H, "_publish_metrics"):
            result = self.H.online_handler(self._make_event(e2e_ms=3500.0), None)
        self.assertAlmostEqual(result["metrics"]["e2e_latency_ms"], 3500.0, places=1)


# ===========================================================================
# OFFLINE HANDLER SMOKE TEST
# ===========================================================================

class TestOfflineHandlerSmoke(unittest.TestCase):

    def setUp(self):
        import handler as H
        self.H = H

    def test_empty_golden_dataset_returns_ok(self):
        """With an empty golden dataset, offline handler should complete without error."""
        with (
            patch.object(self.H, "_load_golden_dataset", return_value=[]),
            patch.object(self.H, "_store_results_to_s3"),
            patch.object(self.H, "_store_aggregated_to_dynamodb"),
            patch.object(self.H, "_publish_eval_metrics_to_cloudwatch"),
            patch.object(self.H, "_load_historical_metrics", return_value=[]),
            patch.object(self.H, "_check_regression_and_alert"),
        ):
            result = self.H.offline_handler(
                {"eval_run_id": "eval_test", "max_questions": 5}, None
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evaluated"], 0)

    def test_s3_and_dynamo_called_on_completion(self):
        """After a full run (empty dataset), both S3 and DynamoDB persistence are invoked."""
        with (
            patch.object(self.H, "_load_golden_dataset", return_value=[]),
            patch.object(self.H, "_store_results_to_s3") as mock_s3,
            patch.object(self.H, "_store_aggregated_to_dynamodb") as mock_ddb,
            patch.object(self.H, "_publish_eval_metrics_to_cloudwatch"),
            patch.object(self.H, "_load_historical_metrics", return_value=[]),
            patch.object(self.H, "_check_regression_and_alert"),
        ):
            self.H.offline_handler({}, None)

        mock_s3.assert_called_once()
        mock_ddb.assert_called_once()


# ===========================================================================
# PROMPT / RESPONSE PAYLOAD TESTS
# ===========================================================================

class TestPublishMetrics(unittest.TestCase):

    def setUp(self):
        import handler as H
        self.H = H

    def test_batching_over_20(self):
        """Metrics > 20 should be published in multiple batch calls."""
        mock_cw = MagicMock()
        with patch.object(self.H, "_cw", return_value=mock_cw):
            metrics = [{"MetricName": f"M{i}", "Value": float(i), "Unit": "None"}
                       for i in range(45)]
            self.H._publish_metrics(metrics)

        # 45 metrics → ceil(45/20) = 3 calls
        self.assertEqual(mock_cw.put_metric_data.call_count, 3)

    def test_empty_metrics_no_call(self):
        mock_cw = MagicMock()
        with patch.object(self.H, "_cw", return_value=mock_cw):
            self.H._publish_metrics([])
        mock_cw.put_metric_data.assert_not_called()


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
