"""
dedup.py — DynamoDB-backed deduplication checker for paper_fetcher

Checks whether a paper_id already exists in DynamoDB before
queuing it for processing. This prevents re-downloading and
re-processing papers that were seen in a previous arXiv poll.

Two-level deduplication:
  1. Fetcher-side (this module): fast GetItem → skip if found
  2. Processor-side (conditional PutItem): prevents race conditions
     when two overlapping fetcher runs queue the same paper.

DynamoDB Table: ResearchPaperMetadata
  PK: paper_id (String)

Access pattern:
  - GetItem(Key={'paper_id': paper_id})
  - Item exists → paper is a duplicate → skip
  - Item missing → paper is new → send to SQS
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class DedupChecker:
    """
    Checks DynamoDB to determine if a paper has already been seen.

    Usage:
        checker = DedupChecker(table=dynamodb.Table("ResearchPaperMetadata"))
        if checker.is_duplicate("2401.12345"):
            skip_paper()
    """

    def __init__(self, table: Any) -> None:
        """
        Parameters
        ----------
        table : boto3 DynamoDB Table resource
            The ResearchPaperMetadata table.
        """
        self._table = table
        # Local in-memory cache within a single Lambda invocation.
        # Prevents multiple GetItem calls for the same paper_id
        # (can happen if arXiv returns the same paper in multiple category queries).
        self._seen_cache: set[str] = set()

    def is_duplicate(self, paper_id: str) -> bool:
        """
        Return True if paper_id already exists in DynamoDB or was
        seen earlier in this invocation.

        Parameters
        ----------
        paper_id : str
            arXiv paper ID, e.g. "2401.12345"

        Returns
        -------
        bool
            True if already processed / queued in this run.
        """
        # Fast path: already queued this invocation
        if paper_id in self._seen_cache:
            logger.debug("Dedup (cache hit): %s", paper_id)
            return True

        # DynamoDB GetItem — only retrieve paper_id attribute (projection)
        # This minimises read capacity consumption.
        try:
            response = self._table.get_item(
                Key={"paper_id": paper_id},
                ProjectionExpression="paper_id",  # Only fetch the PK
                ConsistentRead=False,             # Eventual consistency is fine for dedup
            )
        except ClientError as exc:
            # On DynamoDB errors (throttle, network), log and allow through.
            # The processor-side conditional PutItem is the safety net.
            error_code = exc.response["Error"]["Code"]
            logger.warning(
                "DynamoDB GetItem failed for %s (%s) — treating as new paper",
                paper_id, error_code,
            )
            return False

        if "Item" in response:
            logger.debug("Dedup (DynamoDB hit): %s", paper_id)
            self._seen_cache.add(paper_id)
            return True

        # Mark as seen for this invocation so cross-category duplicates are caught
        self._seen_cache.add(paper_id)
        return False

    def mark_queued(self, paper_id: str) -> None:
        """
        Explicitly add a paper_id to the in-memory cache after queuing.
        Prevents the same paper from being queued twice within one run
        if it appears in multiple arXiv category results.
        """
        self._seen_cache.add(paper_id)

    @property
    def cache_size(self) -> int:
        """Number of paper_ids seen in this invocation."""
        return len(self._seen_cache)
