"""
handler.py — graph_builder Lambda

Entry point for the Knowledge-Graph builder.

Execution flow:
  1. Receive SQS batch of up to 5 Entity Queue messages.
  2. For each message: parse JSON body (paper_id, title, abstract,
     categories, authors, published, chunk_texts).
  3. Call EntityExtractor → Bedrock Claude 3 Haiku to extract
     entities, relationships, topics, and citation hints.
  4. Call NeptuneClient to upsert all vertices and edges via
     idempotent coalesce patterns.
  5. Publish CloudWatch metrics per batch
     (entities_extracted, vertices_upserted, edges_upserted,
      extraction_errors, graph_errors).

Triggered by: SQS Entity Queue (batch size = 5)
Timeout:      300 seconds
Memory:       512 MB
VPC:          Attached (Neptune requires VPC)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from entity_extractor import EntityExtractor
from neptune_client import NeptuneClient

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
NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
NEPTUNE_PORT     = int(os.environ.get("NEPTUNE_PORT", "8182"))
ENTITY_MODEL_ID  = os.environ.get(
    "ENTITY_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
)
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "ResearchRAG")

# ---------------------------------------------------------------------------
# Lazy Neptune connection (re-used across warm invocations)
# ---------------------------------------------------------------------------
_neptune_client: NeptuneClient | None = None


def _get_neptune_client() -> NeptuneClient:
    """
    Return a cached NeptuneClient, creating and connecting it on first call.

    The connection is intentionally kept at module scope so that warm
    Lambda invocations reuse the existing Gremlin WebSocket session,
    which is far cheaper than re-opening the connection each time.

    Returns
    -------
    NeptuneClient
        An open, ready-to-use NeptuneClient instance.
    """
    global _neptune_client
    if _neptune_client is None:
        logger.info(
            "Opening Neptune connection to %s:%s", NEPTUNE_ENDPOINT, NEPTUNE_PORT
        )
        _neptune_client = NeptuneClient(
            endpoint=NEPTUNE_ENDPOINT, port=NEPTUNE_PORT
        )
        _neptune_client.connect()
    return _neptune_client


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point — processes an SQS batch of entity-queue messages.

    Parameters
    ----------
    event : dict
        SQS trigger payload.  Expected structure::

            {
              "Records": [
                {
                  "messageId": "abc-123",
                  "body": "{...}",   # JSON-encoded paper payload
                  ...
                },
                ...
              ]
            }

        Each ``body`` must be a JSON object with the keys:
        ``paper_id``, ``title``, ``abstract``, ``categories``,
        ``authors``, ``published``, ``chunk_texts``.

    context : LambdaContext
        Standard AWS Lambda context object (unused, kept for signature
        compatibility).

    Returns
    -------
    dict
        Batch summary with counts of successes and failures.
        Raising an exception here would cause SQS to re-drive *all*
        records; we therefore always return a summary and rely on
        per-message error tracking via metrics.
    """
    run_ts = datetime.now(tz=timezone.utc).isoformat()
    records = event.get("Records", [])
    logger.info(
        "graph_builder invoked | ts=%s | record_count=%d", run_ts, len(records)
    )

    summary = _process_sqs_records(records)

    _emit_metrics(
        cw_client=cw,
        namespace=CW_NAMESPACE,
        metrics={
            "entities_extracted":  summary["entities_extracted"],
            "vertices_upserted":   summary["vertices_upserted"],
            "edges_upserted":      summary["edges_upserted"],
            "extraction_errors":   summary["extraction_errors"],
            "graph_errors":        summary["graph_errors"],
        },
    )

    logger.info("Batch complete: %s", json.dumps(summary))
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _process_sqs_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Iterate over SQS records and build/update the graph for each paper.

    Each record is processed independently so that a failure on one
    paper does not abort the entire batch.

    Parameters
    ----------
    records : list[dict]
        SQS ``Records`` list from the Lambda event.

    Returns
    -------
    dict
        Aggregated counts::

            {
              "messages_processed": int,
              "messages_failed":    int,
              "entities_extracted": int,
              "vertices_upserted":  int,
              "edges_upserted":     int,
              "extraction_errors":  int,
              "graph_errors":       int,
            }
    """
    neptune = _get_neptune_client()

    messages_processed = 0
    messages_failed    = 0
    total_entities     = 0
    total_vertices     = 0
    total_edges        = 0
    total_ext_errors   = 0
    total_graph_errors = 0

    for record in records:
        message_id = record.get("messageId", "unknown")
        try:
            paper_data: dict[str, Any] = json.loads(record["body"])
        except (KeyError, json.JSONDecodeError) as exc:
            logger.error("Failed to parse message %s: %s", message_id, exc)
            messages_failed  += 1
            total_ext_errors += 1
            continue

        paper_id = paper_data.get("paper_id", message_id)
        logger.info("Processing paper %s", paper_id)

        # --- Entity extraction via Bedrock ---
        try:
            extraction = _extract_entities_for_paper(paper_data, bedrock)
            entity_count = len(extraction.get("entities", []))
            total_entities += entity_count
            logger.info(
                "  Extracted %d entities for %s", entity_count, paper_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Entity extraction failed for %s: %s", paper_id, exc)
            total_ext_errors += 1
            messages_failed  += 1
            continue

        # --- Graph upserts via Neptune ---
        try:
            vertices, edges = _upsert_paper_graph(paper_data, extraction, neptune)
            total_vertices += vertices
            total_edges    += edges
            logger.info(
                "  Upserted %d vertices, %d edges for %s",
                vertices, edges, paper_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Graph upsert failed for %s: %s", paper_id, exc)
            total_graph_errors += 1
            messages_failed    += 1
            continue

        messages_processed += 1

    return {
        "messages_processed": messages_processed,
        "messages_failed":    messages_failed,
        "entities_extracted": total_entities,
        "vertices_upserted":  total_vertices,
        "edges_upserted":     total_edges,
        "extraction_errors":  total_ext_errors,
        "graph_errors":       total_graph_errors,
    }


def _extract_entities_for_paper(
    paper_data: dict[str, Any],
    bedrock_client: Any,  # boto3 bedrock-runtime client
) -> dict[str, Any]:
    """
    Use Bedrock Claude 3 Haiku to extract entities and relationships
    from a paper's title, abstract, and full-text chunks.

    Parameters
    ----------
    paper_data : dict
        Parsed SQS message body.  Required keys:
        ``paper_id``, ``title``, ``abstract``, ``chunk_texts``.
    bedrock_client : boto3 client
        Pre-initialised ``bedrock-runtime`` boto3 client.

    Returns
    -------
    dict
        Extraction result from :class:`EntityExtractor`::

            {
              "entities":      [{"type": str, "name": str, "description": str}, ...],
              "relationships": [{"source_type": str, "source_name": str,
                                 "relation": str,
                                 "target_type": str, "target_name": str}, ...],
              "topics":        [str, ...],
              "citations":     [{"title_snippet": str, "authors": str}, ...],
            }

    Raises
    ------
    RuntimeError
        If Bedrock returns an error response or the extractor raises.
    """
    extractor = EntityExtractor(bedrock_client=bedrock_client, model_id=ENTITY_MODEL_ID)
    return extractor.extract(
        paper_id    = paper_data["paper_id"],
        title       = paper_data.get("title", ""),
        abstract    = paper_data.get("abstract", ""),
        chunk_texts = paper_data.get("chunk_texts", []),
    )


def _upsert_paper_graph(
    paper_data: dict[str, Any],
    extraction: dict[str, Any],
    gremlin_client: NeptuneClient,
) -> tuple[int, int]:
    """
    Upsert all vertices and edges for a single paper into Neptune.

    Graph structure built:

    * One ``Paper`` vertex (keyed on ``paper_id``).
    * One ``Author`` vertex per author → ``AUTHORED_BY`` edge.
    * One ``Topic`` vertex per topic  → ``BELONGS_TO`` edge.
    * One vertex per extracted entity (label = entity type).
    * Relationship edges between entity vertices.

    All upserts are idempotent (coalesce pattern); re-processing the
    same paper will not create duplicate vertices or edges.

    Parameters
    ----------
    paper_data : dict
        Original SQS message body.
    extraction : dict
        Output of :meth:`EntityExtractor.extract`.
    gremlin_client : NeptuneClient
        Open Neptune connection.

    Returns
    -------
    tuple[int, int]
        ``(vertices_upserted, edges_upserted)`` counts for this paper.
    """
    vertices_count = 0
    edges_count    = 0

    paper_id = paper_data["paper_id"]

    # --- Paper vertex ---
    paper_vid = gremlin_client.upsert_vertex(
        label        = "Paper",
        unique_key   = "paper_id",
        unique_value = paper_id,
        properties   = {
            "title":      paper_data.get("title", ""),
            "abstract":   paper_data.get("abstract", ""),
            "published":  paper_data.get("published", ""),
            "categories": json.dumps(paper_data.get("categories", [])),
        },
    )
    vertices_count += 1

    # --- Author vertices + AUTHORED_BY edges ---
    for author_name in paper_data.get("authors", []):
        author_name = str(author_name).strip()
        if not author_name:
            continue
        author_vid = gremlin_client.upsert_vertex(
            label        = "Author",
            unique_key   = "name",
            unique_value = author_name,
            properties   = {},
        )
        vertices_count += 1
        gremlin_client.upsert_edge(
            from_id    = paper_vid,
            edge_label = "AUTHORED_BY",
            to_id      = author_vid,
        )
        edges_count += 1

    # --- Topic vertices + BELONGS_TO edges ---
    for topic in extraction.get("topics", []):
        topic = str(topic).strip()
        if not topic:
            continue
        topic_vid = gremlin_client.upsert_vertex(
            label        = "Topic",
            unique_key   = "name",
            unique_value = topic,
            properties   = {},
        )
        vertices_count += 1
        gremlin_client.upsert_edge(
            from_id    = paper_vid,
            edge_label = "BELONGS_TO",
            to_id      = topic_vid,
        )
        edges_count += 1

    # --- Extracted entity vertices ---
    entity_vid_map: dict[tuple[str, str], str] = {}

    for entity in extraction.get("entities", []):
        etype = str(entity.get("type", "Concept")).strip()
        ename = str(entity.get("name", "")).strip()
        if not ename:
            continue
        vid = gremlin_client.upsert_vertex(
            label        = etype,
            unique_key   = "name",
            unique_value = ename,
            properties   = {
                "description": entity.get("description", ""),
            },
        )
        vertices_count += 1
        entity_vid_map[(etype, ename)] = vid

    # --- Relationship edges between extracted entities ---
    for rel in extraction.get("relationships", []):
        src_type = str(rel.get("source_type", "")).strip()
        src_name = str(rel.get("source_name", "")).strip()
        tgt_type = str(rel.get("target_type", "")).strip()
        tgt_name = str(rel.get("target_name", "")).strip()
        relation  = str(rel.get("relation", "")).strip().upper()

        if not (src_name and tgt_name and relation):
            continue

        # Look up vertex IDs; fall back to a live graph query if not in
        # the local map (entity may exist from a prior invocation).
        src_vid = entity_vid_map.get((src_type, src_name)) or gremlin_client.get_vertex_id(
            label=src_type, key="name", value=src_name
        )
        tgt_vid = entity_vid_map.get((tgt_type, tgt_name)) or gremlin_client.get_vertex_id(
            label=tgt_type, key="name", value=tgt_name
        )

        if src_vid and tgt_vid:
            gremlin_client.upsert_edge(
                from_id    = src_vid,
                edge_label = relation,
                to_id      = tgt_vid,
            )
            edges_count += 1
        else:
            logger.debug(
                "Skipping edge %s -[%s]-> %s: vertex not found",
                src_name, relation, tgt_name,
            )

    return vertices_count, edges_count


def _emit_metrics(
    cw_client: Any,
    namespace: str,
    metrics: dict[str, int | float],
) -> None:
    """
    Publish a set of CloudWatch metrics in a single ``put_metric_data`` call.

    Parameters
    ----------
    cw_client : boto3 client
        Pre-initialised ``cloudwatch`` boto3 client.
    namespace : str
        CloudWatch namespace (e.g. ``"ResearchRAG"``).
    metrics : dict[str, int | float]
        Mapping of metric name to value.  All metrics are published with
        the unit ``Count``.

    Notes
    -----
    CloudWatch ``put_metric_data`` accepts at most 1 000 metric data
    points per call (20 per standard call without high-resolution).
    Batching here is safe for the small set of metrics we emit.
    """
    if not metrics:
        return

    metric_data = [
        {
            "MetricName": name,
            "Value":      float(value),
            "Unit":       "Count",
            "Dimensions": [
                {"Name": "Service", "Value": "GraphBuilder"},
            ],
        }
        for name, value in metrics.items()
    ]

    try:
        cw_client.put_metric_data(
            Namespace  = namespace,
            MetricData = metric_data,
        )
        logger.debug("Published %d metrics to CW namespace %s", len(metrics), namespace)
    except ClientError as exc:
        # Metric failures must never crash the Lambda; log and continue.
        logger.warning("CloudWatch put_metric_data failed: %s", exc)
