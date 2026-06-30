"""
handler.py — embedding_worker Lambda

Entry point for the Embedding Worker that consumes chunks from the SQS
Embedding Queue, re-embeds any zero-vector chunks via Amazon Bedrock
Titan Embed v2, then bulk-indexes the fully-embedded chunks to OpenSearch.

Execution flow:
  1. Receive a batch of up to 10 SQS messages (each body is a JSON list of
     chunk dicts produced by the late_chunker stage).
  2. Flatten all chunks from the batch into a single work list.
  3. For each chunk whose ``embedding`` field is a zero vector, call Bedrock
     ``amazon.titan-embed-text-v2:0`` to produce a real 1 536-dim embedding.
  4. Bulk-index all chunks to OpenSearch via ``/_bulk``.
  5. Emit CloudWatch custom metrics: chunks_indexed, chunks_reembedded,
     embedding_errors, opensearch_errors.
  6. Return a summary dict; failed SQS messages are left in the queue for
     retry via the DLQ (partial-batch failure reporting is enabled on the
     event source mapping).

Triggered by: SQS Embedding Queue (batch size 10)
Timeout:      300 s
Memory:       1 024 MB
VPC:          yes (OpenSearch lives inside the VPC)
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
import requests
from requests_aws4auth import AWS4Auth

from opensearch_client import OpenSearchClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

bedrock = boto3.client("bedrock-runtime", region_name=_AWS_REGION)
cw      = boto3.client("cloudwatch",      region_name=_AWS_REGION)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
OPENSEARCH_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"]   # e.g. https://vpc-xxx.us-east-1.es.amazonaws.com
OPENSEARCH_INDEX    = os.environ.get("OPENSEARCH_INDEX",    "paper_chunks")
EMBEDDING_MODEL_ID  = os.environ.get("EMBEDDING_MODEL_ID",  "amazon.titan-embed-text-v2:0")
CW_NAMESPACE        = os.environ.get("CW_NAMESPACE",         "ResearchRAG")

# ---------------------------------------------------------------------------
# OpenSearch auth (SigV4 via requests_aws4auth)
# ---------------------------------------------------------------------------
_credentials = boto3.Session().get_credentials().get_frozen_credentials()
_OS_AUTH = AWS4Auth(
    _credentials.access_key,
    _credentials.secret_key,
    _AWS_REGION,
    "es",
    session_token=_credentials.token,
)

# Shared requests session for connection pooling across warm invocations
_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})

# Zero-vector sentinel (1 536 floats all equal to 0.0)
_ZERO_VECTOR: list[float] = [0.0] * 1536


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point — triggered by SQS Embedding Queue.

    Parameters
    ----------
    event : dict
        Standard SQS event envelope::

            {
              "Records": [
                {
                  "messageId": "...",
                  "body": "[{chunk_dict}, ...]",   # JSON list of chunk dicts
                  ...
                },
                ...   # up to 10 records
              ]
            }

    context : LambdaContext
        Standard Lambda context object (provides remaining_time_in_millis etc.).

    Returns
    -------
    dict
        Processing summary including counts and any item-level failures for
        SQS partial-batch failure reporting::

            {
              "batchItemFailures": [{"itemIdentifier": "<messageId>"}, ...],
              "summary": {
                "total_chunks":       int,
                "chunks_indexed":     int,
                "chunks_reembedded":  int,
                "embedding_errors":   int,
                "opensearch_errors":  int,
                "messages_processed": int,
                "messages_failed":    int,
              }
            }
    """
    run_id = f"emb_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    records = event.get("Records", [])
    logger.info(
        "Embedding worker run %s | sqs_records=%d", run_id, len(records)
    )

    summary, batch_item_failures = _process_sqs_records(records)

    _emit_metrics(
        cw_client=cw,
        namespace=CW_NAMESPACE,
        metrics={
            "chunks_indexed":    summary["chunks_indexed"],
            "chunks_reembedded": summary["chunks_reembedded"],
            "embedding_errors":  summary["embedding_errors"],
            "opensearch_errors": summary["opensearch_errors"],
        },
    )

    result: dict[str, Any] = {
        "batchItemFailures": batch_item_failures,
        "summary": summary,
    }
    logger.info("Embedding worker run complete: %s", json.dumps(result))
    return result


# ---------------------------------------------------------------------------
# Core processing helpers
# ---------------------------------------------------------------------------

def _process_sqs_records(
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """
    Process a list of SQS records end-to-end.

    Each record body is expected to be a JSON-encoded list of chunk dicts.
    The function:

    1. Deserialises every record and collects chunks per message.
    2. Re-embeds zero-vector chunks via Bedrock.
    3. Bulk-indexes the full set of chunks to OpenSearch.
    4. Returns a summary dict and a list of ``batchItemFailures`` for any
       SQS messages that could not be fully processed.

    Parameters
    ----------
    records : list[dict]
        Raw ``event["Records"]`` from the Lambda invocation.

    Returns
    -------
    tuple[dict, list[dict]]
        ``(summary, batch_item_failures)`` where *summary* contains aggregate
        counters and *batch_item_failures* follows the SQS partial-batch
        failure reporting format.
    """
    total_chunks       = 0
    chunks_reembedded  = 0
    embedding_errors   = 0
    messages_processed = 0
    messages_failed    = 0
    batch_item_failures: list[dict[str, str]] = []

    # Accumulate all chunks from all messages before bulk-indexing
    all_chunks: list[dict[str, Any]] = []
    # Track which message each chunk index belongs to (for failure attribution)
    message_id_for_chunk: list[str] = []

    for record in records:
        message_id = record.get("messageId", "unknown")
        try:
            chunks: list[dict[str, Any]] = json.loads(record["body"])
            if not isinstance(chunks, list):
                raise ValueError("SQS message body is not a JSON list")
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error(
                "Failed to deserialise SQS message %s: %s", message_id, exc
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            messages_failed += 1
            continue

        logger.info(
            "Message %s — %d chunks to process", message_id, len(chunks)
        )

        for chunk in chunks:
            reembedded, err = _reembed_if_needed(chunk, bedrock)
            if err:
                embedding_errors += 1
            elif reembedded:
                chunks_reembedded += 1

            all_chunks.append(chunk)
            message_id_for_chunk.append(message_id)

        total_chunks += len(chunks)
        messages_processed += 1

    # Bulk-index everything collected across the batch
    os_client = OpenSearchClient(endpoint=OPENSEARCH_ENDPOINT, region=_AWS_REGION)
    chunks_indexed, opensearch_errors = os_client.bulk_index(
        index_name=OPENSEARCH_INDEX,
        documents=all_chunks,
    )

    summary = {
        "total_chunks":       total_chunks,
        "chunks_indexed":     chunks_indexed,
        "chunks_reembedded":  chunks_reembedded,
        "embedding_errors":   embedding_errors,
        "opensearch_errors":  opensearch_errors,
        "messages_processed": messages_processed,
        "messages_failed":    messages_failed,
    }
    return summary, batch_item_failures


def _reembed_if_needed(
    chunk: dict[str, Any],
    bedrock_client: Any,
) -> tuple[bool, bool]:
    """
    Re-embed a chunk if its ``embedding`` field is a zero vector.

    Calls Amazon Bedrock ``amazon.titan-embed-text-v2:0`` with the chunk's
    ``text`` field and updates ``chunk["embedding"]`` in-place.

    Parameters
    ----------
    chunk : dict
        Chunk dict as produced by the late_chunker stage.  Must contain at
        least the keys ``chunk_id``, ``text``, and ``embedding``.
    bedrock_client : botocore.client.BedrockRuntime
        Module-level Bedrock runtime client.

    Returns
    -------
    tuple[bool, bool]
        ``(was_reembedded, had_error)``

        * ``was_reembedded`` — True if Bedrock was called and the embedding
          was successfully updated.
        * ``had_error`` — True if the Bedrock call failed; the chunk's
          ``embedding`` remains as-is (zero vector) in that case.
    """
    embedding = chunk.get("embedding", _ZERO_VECTOR)

    # Fast path: embedding already populated, nothing to do
    if embedding and any(v != 0.0 for v in embedding):
        return False, False

    chunk_id = chunk.get("chunk_id", "<unknown>")
    text     = chunk.get("text", "")

    if not text.strip():
        logger.warning("Chunk %s has empty text; skipping re-embedding", chunk_id)
        return False, False

    logger.info("Re-embedding zero-vector chunk %s via Bedrock", chunk_id)
    try:
        response = bedrock_client.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text}),
        )
        body      = json.loads(response["body"].read())
        new_embed = body["embedding"]          # list[float], len == 1536
        chunk["embedding"] = new_embed
        logger.debug(
            "Re-embedded chunk %s — vector norm check: first5=%s",
            chunk_id,
            new_embed[:5],
        )
        return True, False

    except (ClientError, KeyError, json.JSONDecodeError) as exc:
        logger.error("Bedrock re-embedding failed for chunk %s: %s", chunk_id, exc)
        return False, True


def _bulk_index_chunks(
    chunks: list[dict[str, Any]],
    os_endpoint: str,
    os_index: str,
    auth: AWS4Auth,
) -> tuple[int, int]:
    """
    Bulk-index a list of chunk dicts to OpenSearch using the ``/_bulk`` API.

    .. note::
        This function is a thin wrapper kept for testability; the main code
        path uses :class:`~opensearch_client.OpenSearchClient`.

    The ``/_bulk`` body alternates between action/metadata lines and source
    lines::

        {"index": {"_index": "<index>", "_id": "<chunk_id>"}}
        {"chunk_id": "...", "text": "...", "embedding": [...], ...}
        ...

    Parameters
    ----------
    chunks : list[dict]
        Fully-embedded chunk dicts to index.
    os_endpoint : str
        Base URL of the OpenSearch domain (no trailing slash).
    os_index : str
        Target index name.
    auth : AWS4Auth
        SigV4 auth object for the ``es`` service.

    Returns
    -------
    tuple[int, int]
        ``(success_count, error_count)`` where *success_count* is the number
        of documents successfully indexed and *error_count* is the number that
        OpenSearch reported as failed.
    """
    if not chunks:
        return 0, 0

    lines: list[str] = []
    for chunk in chunks:
        action = {"index": {"_index": os_index, "_id": chunk["chunk_id"]}}
        lines.append(json.dumps(action))
        lines.append(json.dumps(chunk))
    bulk_body = "\n".join(lines) + "\n"

    url = f"{os_endpoint.rstrip('/')}/_bulk"
    try:
        resp = _SESSION.post(url, data=bulk_body, auth=auth, timeout=60)
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenSearch /_bulk request failed: %s", exc)
        return 0, len(chunks)

    success_count = 0
    error_count   = 0
    for item in result.get("items", []):
        action_result = item.get("index", {})
        if action_result.get("result") in ("created", "updated"):
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
            "/_bulk completed with errors — success=%d error=%d",
            success_count,
            error_count,
        )
    else:
        logger.info("/_bulk OK — %d documents indexed", success_count)

    return success_count, error_count


def _emit_metrics(
    cw_client: Any,
    namespace: str,
    metrics: dict[str, int | float],
) -> None:
    """
    Publish custom CloudWatch metrics for one Lambda invocation.

    All metrics are emitted as ``Count`` unit with no additional dimensions,
    making them straightforward to graph and alarm on.

    Parameters
    ----------
    cw_client : botocore.client.CloudWatch
        Module-level CloudWatch client.
    namespace : str
        CloudWatch custom namespace (e.g. ``"ResearchRAG"``).
    metrics : dict[str, int | float]
        Mapping of metric name to value.  Expected keys:

        * ``chunks_indexed``    — documents successfully written to OpenSearch
        * ``chunks_reembedded`` — zero-vector chunks re-embedded via Bedrock
        * ``embedding_errors``  — Bedrock call failures
        * ``opensearch_errors`` — OpenSearch indexing failures

    Returns
    -------
    None
    """
    timestamp = datetime.now(tz=timezone.utc)
    metric_data = [
        {
            "MetricName": name,
            "Value":      float(value),
            "Unit":       "Count",
            "Timestamp":  timestamp,
        }
        for name, value in metrics.items()
    ]

    try:
        cw_client.put_metric_data(
            Namespace=namespace,
            MetricData=metric_data,
        )
        logger.debug("Emitted %d CloudWatch metrics to %s", len(metric_data), namespace)
    except ClientError as exc:
        # Metric emission failures must not cause the Lambda to fail
        logger.error("Failed to emit CloudWatch metrics: %s", exc)
