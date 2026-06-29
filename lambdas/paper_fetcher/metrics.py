"""
metrics.py — CloudWatch metrics publisher for paper_fetcher

Publishes custom CloudWatch metrics under the ResearchRAG namespace:
  - papers_fetched        → total papers returned by arXiv
  - papers_skipped_dedup  → papers already in DynamoDB
  - papers_queued         → new papers sent to SQS
  - api_errors            → arXiv API failures

Metrics are batched into a single PutMetricData call (max 20 data points
per API call, well within our 4-metric payload).

CloudWatch custom metrics are used to drive:
  - CloudWatch Dashboard panels (ingestion throughput)
  - CloudWatch Alarms (if queued drops to 0 for > 24h → potential arXiv issue)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class IngestionMetrics:
    """
    Publishes ingestion pipeline metrics to CloudWatch.

    Usage:
        metrics = IngestionMetrics(cw_client=cw, namespace="ResearchRAG")
        metrics.publish(papers_fetched=100, papers_skipped_dedup=80,
                        papers_queued=20, api_errors=0)
    """

    def __init__(self, cw_client: Any, namespace: str) -> None:
        self._cw        = cw_client
        self._namespace = namespace

    def publish(
        self,
        papers_fetched: int,
        papers_skipped_dedup: int,
        papers_queued: int,
        api_errors: int,
    ) -> None:
        """
        Publish a batch of ingestion metrics.

        All metrics use the "Count" unit and are stamped with the current UTC time.
        Dimensions: none (single namespace, single stat per metric).

        Parameters
        ----------
        papers_fetched : int
            Total papers returned by arXiv API (across all categories).
        papers_skipped_dedup : int
            Papers that already exist in DynamoDB (not re-queued).
        papers_queued : int
            New papers sent to SQS for processing.
        api_errors : int
            Categories that failed due to arXiv API errors.
        """
        now = datetime.now(tz=timezone.utc)

        metric_data = [
            {
                "MetricName": "papers_fetched",
                "Timestamp":  now,
                "Value":      float(papers_fetched),
                "Unit":       "Count",
                "Dimensions": [{"Name": "Component", "Value": "PaperFetcher"}],
            },
            {
                "MetricName": "papers_skipped_dedup",
                "Timestamp":  now,
                "Value":      float(papers_skipped_dedup),
                "Unit":       "Count",
                "Dimensions": [{"Name": "Component", "Value": "PaperFetcher"}],
            },
            {
                "MetricName": "papers_queued",
                "Timestamp":  now,
                "Value":      float(papers_queued),
                "Unit":       "Count",
                "Dimensions": [{"Name": "Component", "Value": "PaperFetcher"}],
            },
            {
                "MetricName": "api_errors",
                "Timestamp":  now,
                "Value":      float(api_errors),
                "Unit":       "Count",
                "Dimensions": [{"Name": "Component", "Value": "PaperFetcher"}],
            },
        ]

        try:
            self._cw.put_metric_data(
                Namespace=self._namespace,
                MetricData=metric_data,
            )
            logger.info(
                "CloudWatch metrics published: fetched=%d skipped=%d queued=%d errors=%d",
                papers_fetched, papers_skipped_dedup, papers_queued, api_errors,
            )
        except Exception as exc:  # noqa: BLE001
            # Metric publishing failure should never fail the main function
            logger.warning("Failed to publish CloudWatch metrics: %s", exc)
