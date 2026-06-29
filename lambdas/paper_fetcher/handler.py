"""
handler.py — paper_fetcher Lambda

Entry point for the arXiv paper fetcher.

Execution flow:
  1. Parse EventBridge scheduled event (categories, lookback_hours, max_results)
  2. For each arXiv category, call fetch_category()
  3. For each paper returned: check DynamoDB deduplication
  4. Send new papers to SQS FIFO paper queue
  5. Publish CloudWatch metrics (fetched / skipped / queued / errors)

Triggered by: EventBridge rule every 6 hours
Timeout: 30 seconds
Memory: 128 MB
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from arxiv_client import ArxivClient, ArxivPaper
from dedup import DedupChecker
from metrics import IngestionMetrics

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
sqs      = boto3.client("sqs",       region_name=os.environ.get("AWS_REGION", "us-east-1"))
cw       = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ARXIV_CATEGORIES     = os.environ["ARXIV_CATEGORIES"].split(",")
ARXIV_LOOKBACK_HOURS = int(os.environ.get("ARXIV_LOOKBACK_HOURS", "7"))
ARXIV_MAX_RESULTS    = int(os.environ.get("ARXIV_MAX_RESULTS",    "100"))
PAPER_QUEUE_URL      = os.environ["PAPER_QUEUE_URL"]
PAPER_METADATA_TABLE = os.environ["PAPER_METADATA_TABLE"]
CW_NAMESPACE         = os.environ.get("CW_NAMESPACE", "ResearchRAG")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point.

    Parameters
    ----------
    event : dict
        EventBridge payload with optional overrides:
          {
            "source": "scheduled",
            "categories": ["cs.AI", ...],          # optional override
            "lookback_hours": 7,                   # optional override
            "max_results": 100                     # optional override
          }
    context : LambdaContext
        Standard Lambda context object.

    Returns
    -------
    dict
        Summary of the run: papers fetched, skipped, queued, errors.
    """
    run_id = f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    logger.info("Starting ingestion run %s | event=%s", run_id, json.dumps(event))

    # Allow EventBridge payload to override defaults at runtime
    categories     = event.get("categories",     ARXIV_CATEGORIES)
    lookback_hours = int(event.get("lookback_hours", ARXIV_LOOKBACK_HOURS))
    max_results    = int(event.get("max_results",    ARXIV_MAX_RESULTS))

    arxiv  = ArxivClient(lookback_hours=lookback_hours, max_results=max_results)
    dedup  = DedupChecker(table=dynamodb.Table(PAPER_METADATA_TABLE))
    metrics = IngestionMetrics(cw_client=cw, namespace=CW_NAMESPACE)

    total_fetched  = 0
    total_skipped  = 0
    total_queued   = 0
    total_errors   = 0

    for category in categories:
        logger.info("Fetching category: %s", category)
        try:
            papers = arxiv.fetch_category(category)
        except Exception as exc:  # noqa: BLE001
            logger.error("arXiv fetch failed for %s: %s", category, exc)
            total_errors += 1
            continue

        logger.info("  %d papers returned for %s", len(papers), category)
        total_fetched += len(papers)

        for paper in papers:
            try:
                if dedup.is_duplicate(paper.paper_id):
                    total_skipped += 1
                    logger.debug("  Skipping duplicate: %s", paper.paper_id)
                    continue

                _send_to_queue(paper, run_id, category)
                total_queued += 1
                logger.info("  Queued: %s — %s", paper.paper_id, paper.title[:60])

            except ClientError as exc:
                logger.error("Error processing paper %s: %s", paper.paper_id, exc)
                total_errors += 1

    # Publish CloudWatch metrics for this run
    metrics.publish(
        papers_fetched=total_fetched,
        papers_skipped_dedup=total_skipped,
        papers_queued=total_queued,
        api_errors=total_errors,
    )

    result = {
        "run_id":       run_id,
        "papers_fetched":      total_fetched,
        "papers_skipped_dedup": total_skipped,
        "papers_queued":       total_queued,
        "api_errors":          total_errors,
        "categories_processed": len(categories),
    }
    logger.info("Ingestion run complete: %s", json.dumps(result))
    return result


def _send_to_queue(paper: ArxivPaper, run_id: str, primary_category: str) -> None:
    """
    Send a single paper to the SQS FIFO paper queue.

    Message body contains everything paper_processor needs:
      - paper_id, title, authors, abstract, pdf_url
      - categories, published date
      - primary_category (for S3 path)
      - run_id (for audit trail)

    MessageGroupId is set per-category so papers from different
    categories can be processed in parallel by separate Lambda instances.
    MessageDeduplicationId is set to paper_id (content-based dedup
    would also work but explicit IDs are clearer).
    """
    message_body = {
        "paper_id":         paper.paper_id,
        "title":            paper.title,
        "authors":          paper.authors,
        "abstract":         paper.abstract,
        "pdf_url":          paper.pdf_url,
        "categories":       paper.categories,
        "primary_category": primary_category,
        "published":        paper.published,
        "doi":              paper.doi,
        "run_id":           run_id,
        "queued_at":        datetime.now(tz=timezone.utc).isoformat(),
    }

    sqs.send_message(
        QueueUrl=PAPER_QUEUE_URL,
        MessageBody=json.dumps(message_body),
        MessageGroupId=primary_category,          # One group per arXiv category
        MessageDeduplicationId=paper.paper_id,   # Dedup by arXiv ID
    )
