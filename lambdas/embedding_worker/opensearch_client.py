"""
opensearch_client.py — embedding_worker Lambda

OpenSearch HTTP client using ``requests`` + ``requests_aws4auth`` for SigV4
authentication.  Designed to be instantiated once per Lambda invocation and
reused across calls within the same container warm-up lifecycle.

This module intentionally avoids the ``opensearch-py`` SDK to keep the
deployment package small and to remove a transitive dependency that conflicts
with the Lambda runtime's bundled ``urllib3`` version.

Bulk API wire format
--------------------
The ``/_bulk`` endpoint expects a newline-delimited JSON (NDJSON) body where
each document is preceded by an action/metadata line::

    POST /_bulk
    Content-Type: application/json

    {"index": {"_index": "paper_chunks", "_id": "<chunk_id>"}}
    {"chunk_id": "...", "paper_id": "...", "text": "...", "embedding": [...], ...}
    {"index": {"_index": "paper_chunks", "_id": "<chunk_id2>"}}
    {"chunk_id": "...", ...}

The body MUST end with a trailing newline (``\\n``).

The response JSON contains an ``errors`` boolean and an ``items`` array with
one entry per action.  Each entry has the action name as key (``"index"``
here) and a sub-object with ``"result"`` (``"created"`` or ``"updated"`` on
success) or ``"error"`` (on failure).

Index Management
----------------
``create_index`` wraps a ``PUT /<index>`` call with a combined ``mappings``
and ``settings`` body.  It is idempotent — a 400 response with error type
``resource_already_exists_exception`` is silently ignored.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import requests
from requests_aws4auth import AWS4Auth

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))


class OpenSearchClient:
    """
    Lightweight OpenSearch HTTP client with SigV4 request signing.

    Parameters
    ----------
    endpoint : str
        Full base URL of the OpenSearch domain, e.g.
        ``"https://vpc-my-domain-xxxx.us-east-1.es.amazonaws.com"``.
        A trailing slash is stripped automatically.
    region : str
        AWS region of the OpenSearch domain (e.g. ``"us-east-1"``).

    Attributes
    ----------
    endpoint : str
        Normalised base URL (no trailing slash).
    auth : AWS4Auth
        Frozen-credential SigV4 auth object for the ``es`` service.
    session : requests.Session
        Shared HTTP session for connection pooling.

    Examples
    --------
    >>> client = OpenSearchClient("https://vpc-xxx.us-east-1.es.amazonaws.com", "us-east-1")
    >>> ok, err = client.bulk_index("paper_chunks", chunk_dicts)
    >>> client.create_index("paper_chunks", mapping=PAPER_CHUNKS_MAPPING, settings=INDEX_SETTINGS)
    """

    def __init__(self, endpoint: str, region: str) -> None:
        self.endpoint: str = endpoint.rstrip("/")
        self.region:   str = region

        # Obtain frozen credentials (works with instance role, assumed role,
        # and explicit key/secret in env vars)
        credentials = boto3.Session().get_credentials().get_frozen_credentials()
        self.auth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            region,
            "es",
            session_token=credentials.token,
        )

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bulk_index(
        self,
        index_name: str,
        documents: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """
        Bulk-index a list of documents into an OpenSearch index.

        Each document **must** contain a ``chunk_id`` field that is used as
        the OpenSearch document ``_id``.  Duplicate ``chunk_id`` values result
        in an upsert (the existing document is overwritten).

        The NDJSON request body is assembled as::

            {"index": {"_index": "<index_name>", "_id": "<doc['chunk_id']>"}}
            <full document JSON>
            ...
            <trailing newline>

        Parameters
        ----------
        index_name : str
            Target OpenSearch index (e.g. ``"paper_chunks"``).
        documents : list[dict]
            List of document dicts to index.  Each dict is serialised to JSON
            as-is; all fields (including ``embedding``) are sent verbatim.

        Returns
        -------
        tuple[int, int]
            ``(success_count, error_count)`` — number of documents
            successfully indexed and number that OpenSearch rejected.

        Raises
        ------
        requests.HTTPError
            Re-raised only for unexpected HTTP-level failures (e.g. 503).
            Per-document errors from OpenSearch are handled gracefully and
            counted in ``error_count`` instead.
        """
        if not documents:
            logger.debug("bulk_index called with empty document list; skipping")
            return 0, 0

        bulk_body = self._build_bulk_body(index_name, documents)
        url = f"{self.endpoint}/_bulk"

        try:
            response = self.session.post(
                url,
                data=bulk_body,
                auth=self.auth,
                timeout=60,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "/_bulk HTTP error %s: %s", exc.response.status_code, exc
            )
            return 0, len(documents)
        except requests.RequestException as exc:
            logger.error("/_bulk connection error: %s", exc)
            return 0, len(documents)

        return self._parse_bulk_response(response.json())

    def create_index(
        self,
        index_name: str,
        mapping: dict[str, Any],
        settings: dict[str, Any],
    ) -> bool:
        """
        Create an OpenSearch index with explicit mappings and settings.

        The call is idempotent — if the index already exists OpenSearch
        returns HTTP 400 with ``error.type == "resource_already_exists_exception"``
        which this method silently ignores and returns ``True``.

        Parameters
        ----------
        index_name : str
            Name of the index to create (e.g. ``"paper_chunks"``).
        mapping : dict
            OpenSearch index mappings dict (the value of the ``"mappings"``
            key in the index body).  See ``index_mapping.PAPER_CHUNKS_MAPPING``
            for the full schema used by this project.
        settings : dict
            OpenSearch index settings dict (the value of the ``"settings"``
            key in the index body).  See ``index_mapping.INDEX_SETTINGS``.

        Returns
        -------
        bool
            ``True`` if the index was created or already exists.
            ``False`` if creation failed for any other reason.
        """
        url  = f"{self.endpoint}/{index_name}"
        body = json.dumps({"mappings": mapping, "settings": settings})

        try:
            response = self.session.put(
                url,
                data=body,
                auth=self.auth,
                timeout=30,
            )

            if response.status_code == 400:
                error_type = (
                    response.json()
                    .get("error", {})
                    .get("type", "")
                )
                if error_type == "resource_already_exists_exception":
                    logger.info("Index '%s' already exists — skipping creation", index_name)
                    return True
                logger.error(
                    "create_index '%s' returned 400: %s", index_name, response.text
                )
                return False

            response.raise_for_status()
            logger.info("Index '%s' created successfully", index_name)
            return True

        except requests.RequestException as exc:
            logger.error("create_index '%s' failed: %s", index_name, exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_bulk_body(
        index_name: str,
        documents: list[dict[str, Any]],
    ) -> str:
        """
        Assemble the NDJSON body for a ``/_bulk`` request.

        Parameters
        ----------
        index_name : str
            Target index name embedded in each action line.
        documents : list[dict]
            Documents to serialise.

        Returns
        -------
        str
            NDJSON string ending with a trailing newline character.
        """
        lines: list[str] = []
        for doc in documents:
            doc_id = doc.get("chunk_id", "")
            action = {"index": {"_index": index_name, "_id": doc_id}}
            lines.append(json.dumps(action))
            lines.append(json.dumps(doc))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _parse_bulk_response(
        result: dict[str, Any],
    ) -> tuple[int, int]:
        """
        Parse the OpenSearch ``/_bulk`` response and tally successes/errors.

        Parameters
        ----------
        result : dict
            Parsed JSON response from ``/_bulk``.

        Returns
        -------
        tuple[int, int]
            ``(success_count, error_count)``
        """
        success_count = 0
        error_count   = 0

        for item in result.get("items", []):
            action_result = item.get("index", {})
            status = action_result.get("result")
            if status in ("created", "updated"):
                success_count += 1
            else:
                error_count += 1
                logger.warning(
                    "OpenSearch index error for _id=%s: %s",
                    action_result.get("_id"),
                    action_result.get("error"),
                )

        if result.get("errors"):
            logger.warning(
                "/_bulk response contains errors — success=%d error=%d",
                success_count,
                error_count,
            )
        else:
            logger.info("/_bulk OK — %d documents indexed", success_count)

        return success_count, error_count
