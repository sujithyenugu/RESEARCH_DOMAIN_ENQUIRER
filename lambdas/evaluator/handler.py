"""
lambdas/evaluator/handler.py — Day 8: Evaluation Pipeline

Two Lambda handlers:
  1. online_handler  — per-query lightweight evaluation (latency, confidence,
                       citation accuracy, chunk count) → CloudWatch metrics
  2. offline_handler — nightly batch over golden dataset → Recall@K, MRR,
                       Hit Rate, nDCG, Faithfulness, Groundedness →
                       S3 results + DynamoDB EvalHistory + CloudWatch metrics

Environment variables (set by EvaluationStack CDK):
  CW_NAMESPACE              — CloudWatch namespace (ResearchRAG)
  SNS_ALERTS_TOPIC_ARN      — SNS topic for regression alerts
  EVAL_S3_BUCKET            — S3 bucket name for evaluation results
  EVAL_HISTORY_TABLE        — DynamoDB table name (EvalHistory)
  QUERY_HANDLER_FUNCTION    — Lambda function name of the query handler
  GOLDEN_DATASET_VERSION    — v1 | v2
  GOLDEN_DATASET_COUNT      — number of questions in golden dataset
  BEDROCK_VERIFY_MODEL      — Claude Haiku model ID
  BEDROCK_REGION            — AWS region
  ALARM_RECALL_MIN          — minimum acceptable Recall@10
  ALARM_FAITHFULNESS_MIN    — minimum acceptable faithfulness
  ALARM_CITATION_ACC_MIN    — minimum acceptable citation accuracy
  ALARM_CONFIDENCE_MIN      — minimum acceptable confidence score avg
  ALARM_E2E_P95_MAX_MS      — maximum acceptable E2E P95 latency (ms)
  LOG_LEVEL                 — INFO | DEBUG
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from metrics import (
    compute_citation_accuracy,
    compute_faithfulness,
    compute_groundedness,
    compute_hit_rate,
    compute_mrr,
    compute_ndcg_at_k,
    compute_recall_at_k,
    compute_answer_relevance,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS clients (lazy-initialised per execution environment)
# ---------------------------------------------------------------------------
_cw_client: Any = None
_s3_client: Any = None
_dynamodb_resource: Any = None
_lambda_client: Any = None
_bedrock_client: Any = None
_sns_client: Any = None


def _cw() -> Any:
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _s3() -> Any:
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _ddb() -> Any:
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def _lambda() -> Any:
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _bedrock() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
        )
    return _bedrock_client


def _sns() -> Any:
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
CW_NAMESPACE            = os.environ.get("CW_NAMESPACE", "ResearchRAG")
SNS_ALERTS_TOPIC_ARN    = os.environ.get("SNS_ALERTS_TOPIC_ARN", "")
EVAL_S3_BUCKET          = os.environ.get("EVAL_S3_BUCKET", "research-evaluation")
EVAL_HISTORY_TABLE      = os.environ.get("EVAL_HISTORY_TABLE", "EvalHistory")
QUERY_HANDLER_FUNCTION  = os.environ.get("QUERY_HANDLER_FUNCTION", "research-query-handler")
GOLDEN_DATASET_VERSION  = os.environ.get("GOLDEN_DATASET_VERSION", "v2")
GOLDEN_DATASET_COUNT    = int(os.environ.get("GOLDEN_DATASET_COUNT", "100"))
BEDROCK_VERIFY_MODEL    = os.environ.get("BEDROCK_VERIFY_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
ALARM_RECALL_MIN        = float(os.environ.get("ALARM_RECALL_MIN", "0.70"))
ALARM_FAITHFULNESS_MIN  = float(os.environ.get("ALARM_FAITHFULNESS_MIN", "0.75"))
ALARM_CITATION_ACC_MIN  = float(os.environ.get("ALARM_CITATION_ACC_MIN", "0.90"))
ALARM_CONFIDENCE_MIN    = float(os.environ.get("ALARM_CONFIDENCE_MIN", "0.65"))
ALARM_E2E_P95_MAX_MS    = float(os.environ.get("ALARM_E2E_P95_MAX_MS", "8000"))

REGRESSION_THRESHOLDS: dict[str, float] = {
    "recall_at_10":       0.05,
    "faithfulness":       0.05,
    "groundedness":       0.07,
    "citation_accuracy":  0.03,
    "e2e_latency_p95_ms": 1000.0,
}

# ===========================================================================
# HANDLER 1 — Online Evaluator (per-query, lightweight)
# ===========================================================================

def online_handler(event: dict, context: Any) -> dict:
    """
    Invoked by the Response API after every successful query.

    Expected event payload:
    {
        "query":          "What is LoRA?",
        "answer":         "LoRA introduces low-rank decomposition...",
        "confidence":     0.91,
        "action":         "PASS",
        "citations":      ["2106.09685", "2302.00001"],
        "context_chunks": [...],
        "latency_ms": {
            "total_e2e":          2340,
            "query_understanding": 120,
            "dense_retrieval":     180,
            "bm25_retrieval":      160,
            "graph_expansion":     210,
            "reranking":           390,
            "context_construction": 85,
            "llm_generation":      980,
            "hallucination_detection": 215
        }
    }
    """
    logger.info("Online evaluator invoked")

    query           = event.get("query", "")
    answer          = event.get("answer", "")
    confidence      = float(event.get("confidence", 0.0))
    action          = event.get("action", "PASS")
    context_chunks  = event.get("context_chunks", [])
    latency_ms      = event.get("latency_ms", {})
    citations       = event.get("citations", [])

    # ------------------------------------------------------------------
    # Compute lightweight online metrics
    # ------------------------------------------------------------------
    citation_acc = compute_citation_accuracy(answer, context_chunks)
    chunk_count  = len(context_chunks)
    e2e_ms       = float(latency_ms.get("total_e2e", 0))
    is_refusal   = 1.0 if action in ("REFUSE", "WARN") else 0.0

    # ------------------------------------------------------------------
    # Publish to CloudWatch
    # ------------------------------------------------------------------
    metrics = [
        {"MetricName": "ConfidenceScore",       "Value": confidence,   "Unit": "None"},
        {"MetricName": "CitationAccuracy",       "Value": citation_acc, "Unit": "None"},
        {"MetricName": "ChunkCount",             "Value": chunk_count,  "Unit": "Count"},
        {"MetricName": "E2ELatencyMs",           "Value": e2e_ms,       "Unit": "Milliseconds"},
        {"MetricName": "RefusalRate",            "Value": is_refusal,   "Unit": "None"},
        {"MetricName": "QueryCount",             "Value": 1.0,          "Unit": "Count"},
    ]

    # Per-stage latency metrics
    for stage_key, cw_name in [
        ("query_understanding",      "QueryUnderstandingLatencyMs"),
        ("dense_retrieval",          "DenseRetrievalLatencyMs"),
        ("bm25_retrieval",           "BM25RetrievalLatencyMs"),
        ("graph_expansion",          "GraphExpansionLatencyMs"),
        ("reranking",                "RerankingLatencyMs"),
        ("context_construction",     "ContextConstructionLatencyMs"),
        ("llm_generation",           "LLMGenerationLatencyMs"),
        ("hallucination_detection",  "HallucinationDetectionLatencyMs"),
    ]:
        if stage_key in latency_ms:
            metrics.append(
                {
                    "MetricName": cw_name,
                    "Value": float(latency_ms[stage_key]),
                    "Unit": "Milliseconds",
                }
            )

    _publish_metrics(metrics)

    logger.info(
        "Online evaluation complete | confidence=%.3f | citation_acc=%.3f | "
        "e2e_ms=%.0f | action=%s",
        confidence, citation_acc, e2e_ms, action,
    )

    return {
        "status": "ok",
        "metrics": {
            "confidence":      confidence,
            "citation_accuracy": citation_acc,
            "chunk_count":     chunk_count,
            "e2e_latency_ms":  e2e_ms,
        },
    }


# ===========================================================================
# HANDLER 2 — Offline Evaluator (nightly batch)
# ===========================================================================

def offline_handler(event: dict, context: Any) -> dict:
    """
    Triggered by EventBridge cron daily at 02:00 UTC.

    Runs the full RAG pipeline for every golden question,
    computes all retrieval + generation metrics, and stores results.
    """
    run_id = event.get("eval_run_id") or f"eval_{_today()}"
    dataset_version = event.get("dataset_version", GOLDEN_DATASET_VERSION)
    max_questions   = int(event.get("max_questions", GOLDEN_DATASET_COUNT))

    logger.info("Offline evaluator started | run_id=%s | version=%s | max=%d",
                run_id, dataset_version, max_questions)

    start_ts = time.time()

    # ------------------------------------------------------------------
    # Load golden dataset from S3
    # ------------------------------------------------------------------
    golden_dataset = _load_golden_dataset(dataset_version, max_questions)
    logger.info("Loaded %d golden Q&A pairs", len(golden_dataset))

    # ------------------------------------------------------------------
    # Run full RAG pipeline for each question
    # ------------------------------------------------------------------
    results: list[dict] = []
    latencies: list[float] = []

    for qa in golden_dataset:
        eval_id  = qa.get("eval_id", "unknown")
        question = qa.get("question", "")
        relevant_chunk_ids = qa.get("relevant_chunk_ids", [])

        try:
            pipeline_response = _invoke_query_handler(question)
        except Exception as exc:
            logger.warning("Pipeline failed for %s: %s", eval_id, exc)
            continue

        retrieved_chunk_ids = [
            c.get("chunk_id", "") for c in pipeline_response.get("chunks", [])
        ]
        answer         = pipeline_response.get("answer", "")
        chunks         = pipeline_response.get("chunks", [])
        e2e_ms         = float(pipeline_response.get("latency_ms", {}).get("total_e2e", 0))
        conf           = float(pipeline_response.get("confidence", 0.0))

        latencies.append(e2e_ms)

        # Retrieval metrics
        recall_5  = compute_recall_at_k(retrieved_chunk_ids[:5],  relevant_chunk_ids)
        recall_10 = compute_recall_at_k(retrieved_chunk_ids[:10], relevant_chunk_ids)
        mrr_score = compute_mrr(retrieved_chunk_ids, relevant_chunk_ids)
        hit_rate  = compute_hit_rate(retrieved_chunk_ids[:10], relevant_chunk_ids)
        ndcg_10   = compute_ndcg_at_k(retrieved_chunk_ids[:10], relevant_chunk_ids, k=10)

        # Generation metrics
        faithfulness_score  = compute_faithfulness(answer, chunks)
        groundedness_score  = compute_groundedness(answer, chunks)
        citation_acc        = compute_citation_accuracy(answer, chunks)
        answer_rel          = compute_answer_relevance(question, answer, _bedrock(), BEDROCK_VERIFY_MODEL)

        result = {
            "eval_id":  eval_id,
            "question": question,
            "metrics": {
                "recall_at_5":      recall_5,
                "recall_at_10":     recall_10,
                "mrr":              mrr_score,
                "hit_rate_at_10":   hit_rate,
                "ndcg_at_10":       ndcg_10,
                "faithfulness":     faithfulness_score,
                "groundedness":     groundedness_score,
                "citation_accuracy": citation_acc,
                "answer_relevance": answer_rel,
                "e2e_latency_ms":   e2e_ms,
                "confidence":       conf,
            },
        }
        results.append(result)

        logger.debug(
            "eval_id=%s | recall@10=%.3f | faithfulness=%.3f | e2e=%.0fms",
            eval_id, recall_10, faithfulness_score, e2e_ms,
        )

    # ------------------------------------------------------------------
    # Aggregate metrics across all questions
    # ------------------------------------------------------------------
    aggregated = _aggregate_metrics(results)
    aggregated["run_id"]             = run_id
    aggregated["questions_evaluated"] = len(results)
    aggregated["duration_seconds"]   = int(time.time() - start_ts)
    aggregated["created_at"]         = datetime.now(timezone.utc).isoformat()

    logger.info(
        "Aggregated | recall@10=%.3f | faithfulness=%.3f | citation_acc=%.3f | "
        "e2e_p95=%.0fms",
        aggregated.get("recall_at_10", 0),
        aggregated.get("faithfulness", 0),
        aggregated.get("citation_accuracy", 0),
        aggregated.get("e2e_latency_p95_ms", 0),
    )

    # ------------------------------------------------------------------
    # Persist results
    # ------------------------------------------------------------------
    _store_results_to_s3(results, aggregated, run_id)
    _store_aggregated_to_dynamodb(aggregated, run_id)

    # ------------------------------------------------------------------
    # Publish aggregated metrics to CloudWatch
    # ------------------------------------------------------------------
    _publish_eval_metrics_to_cloudwatch(aggregated)

    # ------------------------------------------------------------------
    # Regression detection + alerting
    # ------------------------------------------------------------------
    historical = _load_historical_metrics(days=7)
    _check_regression_and_alert(aggregated, historical)

    return {
        "status":   "ok",
        "run_id":   run_id,
        "evaluated": len(results),
        "metrics":  aggregated,
    }


# ===========================================================================
# Internal helpers
# ===========================================================================

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_golden_dataset(version: str, max_questions: int) -> list[dict]:
    """Load golden Q&A pairs from S3."""
    key = f"golden_dataset/golden_qa_{version}.json"
    try:
        resp = _s3().get_object(Bucket=EVAL_S3_BUCKET, Key=key)
        data = json.loads(resp["Body"].read())
        return data[:max_questions]
    except Exception as exc:
        logger.error("Failed to load golden dataset from S3: %s", exc)
        # Return an empty list rather than crashing — nightly job continues
        return []


def _invoke_query_handler(question: str) -> dict:
    """Synchronously invoke the query handler Lambda."""
    payload = {
        "body": json.dumps(
            {
                "query": question,
                "options": {
                    "include_chunks": True,
                    "include_latency": True,
                    "include_evaluation": True,
                },
            }
        )
    }
    resp = _lambda().invoke(
        FunctionName=QUERY_HANDLER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    raw = json.loads(resp["Payload"].read())
    body = json.loads(raw.get("body", "{}"))
    return body


def _aggregate_metrics(results: list[dict]) -> dict:
    """Compute mean of each metric across all evaluated questions."""
    if not results:
        return {}

    keys = [
        "recall_at_5", "recall_at_10", "mrr", "hit_rate_at_10", "ndcg_at_10",
        "faithfulness", "groundedness", "citation_accuracy", "answer_relevance",
        "e2e_latency_ms", "confidence",
    ]
    sums: dict[str, float] = {k: 0.0 for k in keys}
    latencies: list[float] = []

    for r in results:
        m = r.get("metrics", {})
        for k in keys:
            sums[k] += m.get(k, 0.0)
        latencies.append(m.get("e2e_latency_ms", 0.0))

    n = len(results)
    agg = {k: round(sums[k] / n, 4) for k in keys}

    # Compute latency percentiles
    sorted_lat = sorted(latencies)
    agg["e2e_latency_p50_ms"] = _percentile(sorted_lat, 50)
    agg["e2e_latency_p95_ms"] = _percentile(sorted_lat, 95)
    agg["e2e_latency_p99_ms"] = _percentile(sorted_lat, 99)

    return agg


def _percentile(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * pct / 100)
    idx = min(idx, len(sorted_values) - 1)
    return round(sorted_values[idx], 1)


def _store_results_to_s3(results: list[dict], aggregated: dict, run_id: str) -> None:
    """Write per-question results and aggregated summary to S3."""
    today = _today()
    full_key = f"results/eval_{today}.json"
    payload = {
        "run_id":     run_id,
        "date":       today,
        "aggregated": aggregated,
        "results":    results,
    }
    try:
        _s3().put_object(
            Bucket=EVAL_S3_BUCKET,
            Key=full_key,
            Body=json.dumps(payload, indent=2),
            ContentType="application/json",
        )
        logger.info("Results stored to s3://%s/%s", EVAL_S3_BUCKET, full_key)
    except Exception as exc:
        logger.error("Failed to store results to S3: %s", exc)


def _store_aggregated_to_dynamodb(aggregated: dict, run_id: str) -> None:
    """Write aggregated metrics to DynamoDB EvalHistory table."""
    today = _today()
    item: dict[str, Any] = {
        "eval_date":             today,
        "eval_run_id":           run_id,
        "created_at":            aggregated.get("created_at", ""),
        "questions_evaluated":   aggregated.get("questions_evaluated", 0),
        "duration_seconds":      aggregated.get("duration_seconds", 0),
        **{k: str(round(v, 4)) for k, v in aggregated.items()
           if isinstance(v, (int, float)) and k not in ("questions_evaluated", "duration_seconds")},
    }
    try:
        table = _ddb().Table(EVAL_HISTORY_TABLE)
        table.put_item(Item=item)
        logger.info("Aggregated metrics stored to DynamoDB | run_id=%s", run_id)
    except Exception as exc:
        logger.error("Failed to store aggregated metrics to DynamoDB: %s", exc)


def _publish_eval_metrics_to_cloudwatch(aggregated: dict) -> None:
    """Publish aggregated evaluation metrics as CloudWatch custom metrics."""
    metric_map = {
        "recall_at_5":        "EvalRecallAt5",
        "recall_at_10":       "EvalRecallAt10",
        "mrr":                "EvalMRR",
        "hit_rate_at_10":     "EvalHitRateAt10",
        "ndcg_at_10":         "EvalNDCGAt10",
        "faithfulness":       "EvalFaithfulness",
        "groundedness":       "EvalGroundedness",
        "citation_accuracy":  "EvalCitationAccuracy",
        "answer_relevance":   "EvalAnswerRelevance",
        "confidence":         "ConfidenceScore",
        "e2e_latency_p50_ms": "EvalE2ELatencyP50Ms",
        "e2e_latency_p95_ms": "EvalE2ELatencyP95Ms",
        "e2e_latency_p99_ms": "EvalE2ELatencyP99Ms",
    }
    metrics = []
    for agg_key, cw_name in metric_map.items():
        if agg_key in aggregated:
            unit = "Milliseconds" if "ms" in agg_key.lower() else "None"
            metrics.append(
                {"MetricName": cw_name, "Value": float(aggregated[agg_key]), "Unit": unit}
            )
    _publish_metrics(metrics)


def _publish_metrics(metrics: list[dict]) -> None:
    """Batch-publish CloudWatch custom metrics (max 20 per call)."""
    if not metrics:
        return
    batch_size = 20
    for i in range(0, len(metrics), batch_size):
        batch = metrics[i: i + batch_size]
        try:
            _cw().put_metric_data(
                Namespace=CW_NAMESPACE,
                MetricData=batch,
            )
        except Exception as exc:
            logger.warning("Failed to publish CloudWatch metrics batch: %s", exc)


def _load_historical_metrics(days: int) -> list[dict]:
    """Load the last N days of aggregated metrics from DynamoDB."""
    try:
        table  = _ddb().Table(EVAL_HISTORY_TABLE)
        items: list[dict] = []
        resp = table.scan(Limit=days * 2)  # simple scan — table is small
        items.extend(resp.get("Items", []))
        # Sort by eval_date descending and take most recent `days`
        items.sort(key=lambda x: x.get("eval_date", ""), reverse=True)
        return items[:days]
    except Exception as exc:
        logger.warning("Failed to load historical metrics: %s", exc)
        return []


def _check_regression_and_alert(current: dict, historical: list[dict]) -> None:
    """
    Compare current eval metrics against 7-day baseline.
    Alert via SNS if any metric regresses beyond its threshold.
    """
    if not historical:
        logger.info("No historical data — skipping regression check")
        return

    # Build baseline: average of historical runs for each metric
    float_keys = [k for k, v in current.items() if isinstance(v, (int, float))]
    baseline: dict[str, float] = {}
    for key in float_keys:
        values = [float(h.get(key, current.get(key, 0))) for h in historical]
        baseline[key] = sum(values) / len(values) if values else 0.0

    regressions: list[dict] = []
    for metric, threshold in REGRESSION_THRESHOLDS.items():
        current_val  = float(current.get(metric, 0.0))
        baseline_val = baseline.get(metric, current_val)

        # For latency: regression = current > baseline + threshold
        if "latency" in metric:
            delta = current_val - baseline_val
            if delta > threshold:
                regressions.append(
                    {
                        "metric":   metric,
                        "baseline": round(baseline_val, 1),
                        "current":  round(current_val, 1),
                        "delta":    f"+{round(delta, 1)} ms",
                        "severity": "critical" if delta > threshold * 2 else "warning",
                    }
                )
        else:
            # For score metrics: regression = current < baseline - threshold
            delta = baseline_val - current_val
            if delta > threshold:
                regressions.append(
                    {
                        "metric":   metric,
                        "baseline": round(baseline_val, 4),
                        "current":  round(current_val, 4),
                        "delta":    f"-{round(delta, 4)}",
                        "severity": "critical" if delta > threshold * 2 else "warning",
                    }
                )

    if regressions:
        logger.warning("Regressions detected: %s", regressions)
        _publish_regression_alert(regressions, current.get("run_id", "unknown"))
    else:
        logger.info("No regressions detected — all metrics within threshold")


def _publish_regression_alert(regressions: list[dict], run_id: str) -> None:
    """Publish regression alert to SNS."""
    if not SNS_ALERTS_TOPIC_ARN:
        logger.warning("SNS_ALERTS_TOPIC_ARN not set — skipping alert")
        return

    lines = [f"⚠️  RAG Evaluation Regression — run_id: {run_id}", ""]
    for r in regressions:
        severity_icon = "🔴" if r["severity"] == "critical" else "🟡"
        lines.append(
            f"  {severity_icon}  {r['metric']:35s} "
            f"baseline={r['baseline']}  current={r['current']}  "
            f"delta={r['delta']}"
        )
    lines.append("")
    lines.append("Action: Review evaluation dashboard and investigate pipeline changes.")

    try:
        _sns().publish(
            TopicArn=SNS_ALERTS_TOPIC_ARN,
            Subject=f"⚠️ RAG Evaluation Regression — {run_id}",
            Message="\n".join(lines),
        )
        logger.info("Regression alert published to SNS")
    except Exception as exc:
        logger.error("Failed to publish regression alert: %s", exc)
