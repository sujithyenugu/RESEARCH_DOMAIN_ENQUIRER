"""
handler.py — paper_processor Lambda

Triggered by SQS FIFO paper queue. Orchestrates the full
per-paper ingestion pipeline:

  Stage 2 — Download PDF → stream to S3 raw-papers
  Stage 3 — Parse: Docling (primary) → Textract (fallback)
  Stage 4 — Clean: 10 cleaning rules via text_cleaner.py
  Stage 5 — Metadata: DynamoDB conditional PutItem
  Stage 6 — Entity queue: send sections to entity_extractor SQS
  Stage 7 — Late chunk: text_cleaner → late_chunker → send to embedding SQS

Concurrency: 5 (reserved_concurrent_executions in CDK)
Timeout: 900s (15 minutes)
Memory: 512 MB
VPC: Yes (Lambda subnet, uses VPC endpoints for S3/DynamoDB/SQS/Bedrock)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pdf_downloader import PdfDownloader
from docling_parser import DoclingParser
from textract_parser import TextractParser
from text_cleaner import TextCleaner
from late_chunker import LateChunker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
_region = os.environ.get("AWS_REGION", "us-east-1")
s3_client       = boto3.client("s3",       region_name=_region)
dynamodb        = boto3.resource("dynamodb", region_name=_region)
sqs_client      = boto3.client("sqs",       region_name=_region)
lambda_client   = boto3.client("lambda",    region_name=_region)
bedrock_client  = boto3.client("bedrock-runtime", region_name=_region)
cw_client       = boto3.client("cloudwatch", region_name=_region)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
RAW_PAPERS_BUCKET      = os.environ["RAW_PAPERS_BUCKET"]
PARSED_PAPERS_BUCKET   = os.environ["PARSED_PAPERS_BUCKET"]
CLEANED_PAPERS_BUCKET  = os.environ["CLEANED_PAPERS_BUCKET"]
PAPER_METADATA_TABLE   = os.environ["PAPER_METADATA_TABLE"]
EMBEDDING_QUEUE_URL    = os.environ["EMBEDDING_QUEUE_URL"]
ENTITY_QUEUE_URL       = os.environ["ENTITY_QUEUE_URL"]
DOCLING_LAMBDA_NAME    = os.environ["DOCLING_LAMBDA_NAME"]
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
CHUNK_MIN_TOKENS       = int(os.environ.get("CHUNK_MIN_TOKENS",     "100"))
CHUNK_MAX_TOKENS       = int(os.environ.get("CHUNK_MAX_TOKENS",     "512"))
CHUNK_MAX_SECTION_TOKENS = int(os.environ.get("CHUNK_MAX_SECTION_TOKENS", "8192"))
CW_NAMESPACE           = os.environ.get("CW_NAMESPACE", "ResearchRAG")

# Lazy singletons (re-used across warm invocations)
_paper_table    = None
_downloader     = None
_cleaner        = None


def _get_paper_table():
    global _paper_table
    if _paper_table is None:
        _paper_table = dynamodb.Table(PAPER_METADATA_TABLE)
    return _paper_table


def _get_downloader():
    global _downloader
    if _downloader is None:
        _downloader = PdfDownloader(s3_client=s3_client, bucket=RAW_PAPERS_BUCKET)
    return _downloader


def _get_cleaner():
    global _cleaner
    if _cleaner is None:
        _cleaner = TextCleaner()
    return _cleaner


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    SQS event handler — processes exactly one paper per invocation
    (batch_size=1 is set in CDK event source mapping).

    SQS event shape:
      {
        "Records": [
          {
            "body": "{... paper JSON ...}",
            "receiptHandle": "...",
            "messageId": "..."
          }
        ]
      }

    Returns
    -------
    dict
        {"batchItemFailures": []} on full success, or with failed
        messageIds for partial batch failure reporting.
    """
    failed_items = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            logger.info(
                "Processing paper %s (run: %s)",
                body.get("paper_id", "?"),
                body.get("run_id", "?"),
            )
            _process_paper(body)

        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to process message %s: %s", message_id, exc
            )
            # Report to SQS for partial batch failure tracking
            failed_items.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failed_items}


def _process_paper(paper_msg: dict[str, Any]) -> None:
    """
    Full ingestion pipeline for a single paper.

    Parameters
    ----------
    paper_msg : dict
        Message body from the SQS paper queue (assembled by paper_fetcher).
        Required fields: paper_id, title, authors, abstract, pdf_url,
                         categories, primary_category, published, run_id
    """
    paper_id         = paper_msg["paper_id"]
    primary_category = paper_msg["primary_category"]
    run_id           = paper_msg["run_id"]
    year             = paper_msg["published"][:4]   # "2024" from "2024-01-15T..."

    # ------------------------------------------------------------------
    # Stage 2 — Download PDF → S3 raw-papers
    # ------------------------------------------------------------------
    s3_pdf_key = f"{primary_category}/{year}/{paper_id}.pdf"
    logger.info("[%s] Stage 2: Downloading PDF → s3://%s/%s",
                paper_id, RAW_PAPERS_BUCKET, s3_pdf_key)

    downloader = _get_downloader()
    downloader.download_and_store(
        pdf_url=paper_msg["pdf_url"],
        s3_key=s3_pdf_key,
        tags={
            "paper_id":   paper_id,
            "category":   primary_category,
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "source":     "arxiv",
        },
    )

    # ------------------------------------------------------------------
    # Stage 3 — Parse: Docling (primary) with Textract fallback
    # ------------------------------------------------------------------
    logger.info("[%s] Stage 3: Parsing document", paper_id)
    parsed_doc = _parse_document(paper_id, s3_pdf_key)

    # Store raw parsed output → S3 parsed-papers
    s3_parsed_key = f"{paper_id}/sections.json"
    s3_client.put_object(
        Bucket=PARSED_PAPERS_BUCKET,
        Key=s3_parsed_key,
        Body=json.dumps(parsed_doc, ensure_ascii=False),
        ContentType="application/json",
    )
    logger.info("[%s] Parsed doc stored → s3://%s/%s",
                paper_id, PARSED_PAPERS_BUCKET, s3_parsed_key)

    # ------------------------------------------------------------------
    # Stage 4 — Clean text (10 cleaning rules)
    # ------------------------------------------------------------------
    logger.info("[%s] Stage 4: Cleaning text", paper_id)
    cleaner = _get_cleaner()
    clean_doc = cleaner.clean(parsed_doc, paper_metadata=paper_msg)

    # Store cleaned output → S3 cleaned-papers
    s3_clean_key = f"{paper_id}/clean.json"
    s3_client.put_object(
        Bucket=CLEANED_PAPERS_BUCKET,
        Key=s3_clean_key,
        Body=json.dumps(clean_doc, ensure_ascii=False),
        ContentType="application/json",
    )
    logger.info("[%s] Clean doc stored → s3://%s/%s",
                paper_id, CLEANED_PAPERS_BUCKET, s3_clean_key)

    # ------------------------------------------------------------------
    # Stage 5 — Metadata: DynamoDB conditional PutItem
    # ------------------------------------------------------------------
    logger.info("[%s] Stage 5: Writing metadata to DynamoDB", paper_id)
    _write_metadata(paper_msg, s3_pdf_key, s3_clean_key, clean_doc, run_id)

    # ------------------------------------------------------------------
    # Stage 6 — Entity Queue: send cleaned sections
    # ------------------------------------------------------------------
    logger.info("[%s] Stage 6: Sending to entity queue", paper_id)
    _send_to_entity_queue(paper_id, clean_doc, paper_msg)

    # ------------------------------------------------------------------
    # Stage 7 — Late Chunking → Embedding Queue
    # ------------------------------------------------------------------
    logger.info("[%s] Stage 7: Late chunking", paper_id)
    chunker = LateChunker(
        bedrock_client=bedrock_client,
        model_id=BEDROCK_EMBEDDING_MODEL,
        min_tokens=CHUNK_MIN_TOKENS,
        max_tokens=CHUNK_MAX_TOKENS,
        max_section_tokens=CHUNK_MAX_SECTION_TOKENS,
    )
    chunks = chunker.chunk_document(clean_doc, paper_metadata=paper_msg)
    _send_chunks_to_embedding_queue(paper_id, chunks, paper_msg)

    # Update DynamoDB: chunk_count + status = "chunked"
    _get_paper_table().update_item(
        Key={"paper_id": paper_id},
        UpdateExpression="SET processing_status = :s, chunk_count = :c, updated_at = :u",
        ExpressionAttributeValues={
            ":s": "chunked",
            ":c": len(chunks),
            ":u": datetime.now(tz=timezone.utc).isoformat(),
        },
    )

    logger.info(
        "[%s] Ingestion complete — %d chunks produced",
        paper_id, len(chunks),
    )

    # Publish success metric
    _publish_metric("papers_processed", 1)
    _publish_metric("chunks_produced", len(chunks))


# ---------------------------------------------------------------------------
# Stage 3 helpers
# ---------------------------------------------------------------------------

def _parse_document(paper_id: str, s3_pdf_key: str) -> dict[str, Any]:
    """
    Try Docling first; fall back to Textract on failure.

    Docling is invoked as a separate Lambda (container image with ML deps).
    Textract is called via AWS SDK (async analysis).

    Returns
    -------
    dict
        Parsed document with sections, tables, equations, references.
    """
    # Primary: Docling Lambda (synchronous invoke)
    try:
        response = lambda_client.invoke(
            FunctionName=DOCLING_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "paper_id":  paper_id,
                "s3_bucket": RAW_PAPERS_BUCKET,
                "s3_key":    s3_pdf_key,
            }),
        )
        payload = json.loads(response["Payload"].read())

        if response.get("FunctionError"):
            logger.warning(
                "[%s] Docling Lambda error: %s — falling back to Textract",
                paper_id, payload,
            )
            raise RuntimeError(f"Docling failed: {payload}")

        return payload

    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Docling failed (%s), using Textract fallback", paper_id, exc)

    # Fallback: Amazon Textract
    parser = TextractParser(
        s3_client=s3_client,
        bucket=RAW_PAPERS_BUCKET,
        s3_key=s3_pdf_key,
        paper_id=paper_id,
    )
    return parser.parse()


# ---------------------------------------------------------------------------
# Stage 5 helpers
# ---------------------------------------------------------------------------

def _write_metadata(
    paper_msg: dict[str, Any],
    s3_pdf_key: str,
    s3_clean_key: str,
    clean_doc: dict[str, Any],
    run_id: str,
) -> None:
    """
    Write paper metadata to DynamoDB.

    Uses ConditionExpression = "attribute_not_exists(paper_id)" to prevent
    overwriting records if two processor instances race on the same paper.
    A ConditionalCheckFailedException means another processor got there first
    and is treated as a no-op (not an error).
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    item = {
        "paper_id":          paper_msg["paper_id"],
        "title":             paper_msg["title"],
        "authors":           paper_msg["authors"],
        "published":         paper_msg["published"],
        "categories":        paper_msg["categories"],
        "category":          paper_msg["primary_category"],  # GSI-1 PK
        "abstract":          paper_msg["abstract"],
        "doi":               paper_msg.get("doi", ""),
        "pdf_url":           paper_msg["pdf_url"],
        "s3_pdf_key":        s3_pdf_key,
        "s3_clean_key":      s3_clean_key,
        "processing_status": "processed",
        "chunk_count":       0,    # Will be updated in Stage 7
        "entity_count":      0,    # Will be updated by entity_extractor
        "section_count":     len(clean_doc.get("cleaned_sections", [])),
        "created_at":        now,
        "updated_at":        now,
        "ingestion_run":     run_id,
    }

    try:
        _get_paper_table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(paper_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "[%s] DynamoDB conditional check failed — paper already exists (race condition, safe to ignore)",
                paper_msg["paper_id"],
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Stage 6 helpers
# ---------------------------------------------------------------------------

def _send_to_entity_queue(
    paper_id: str,
    clean_doc: dict[str, Any],
    paper_meta: dict[str, Any],
) -> None:
    """
    Send cleaned sections to the entity extraction SQS queue.

    Message schema (matches INGESTION_PIPELINE.md §Stage 6):
      {
        "paper_id": "2401.12345",
        "sections": [...],
        "metadata": { "title": ..., "authors": ..., "published": ... }
      }
    """
    # Only send substantive sections (skip very short abstract-like entries)
    sections = [
        {
            "section_id":    sec["section_id"],
            "title":         sec.get("title", ""),
            "text":          sec["text"],
        }
        for sec in clean_doc.get("cleaned_sections", [])
        if len(sec.get("text", "")) > 100   # Skip trivial sections
    ]

    if not sections:
        logger.warning("[%s] No substantive sections to send for entity extraction", paper_id)
        return

    message = {
        "paper_id": paper_id,
        "sections": sections,
        "metadata": {
            "title":     paper_meta["title"],
            "authors":   paper_meta["authors"],
            "published": paper_meta["published"],
        },
    }

    sqs_client.send_message(
        QueueUrl=ENTITY_QUEUE_URL,
        MessageBody=json.dumps(message),
    )
    logger.info("[%s] Sent %d sections to entity queue", paper_id, len(sections))


# ---------------------------------------------------------------------------
# Stage 7 helpers
# ---------------------------------------------------------------------------

def _send_chunks_to_embedding_queue(
    paper_id: str,
    chunks: list[dict[str, Any]],
    paper_meta: dict[str, Any],
) -> None:
    """
    Send late-chunked paper chunks to the embedding SQS queue.

    Batch strategy:
      - SQS standard queue supports batches of up to 10 messages.
      - Each batch message contains all chunks for the paper (typically 40–80).
      - For very large papers, split into multiple batches of ≤10 chunks each.

    Message schema (matches INGESTION_PIPELINE.md §Stage 7):
      {
        "paper_id": "...",
        "chunks":   [...],
      }
    """
    if not chunks:
        logger.warning("[%s] No chunks to send to embedding queue", paper_id)
        return

    # Send in batches of 10 to stay within SQS SendMessageBatch limits
    BATCH_SIZE = 10
    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start: batch_start + BATCH_SIZE]

        message = {
            "paper_id": paper_id,
            "chunks":   batch,
        }
        sqs_client.send_message(
            QueueUrl=EMBEDDING_QUEUE_URL,
            MessageBody=json.dumps(message),
        )

    logger.info(
        "[%s] Sent %d chunks to embedding queue (%d batches of %d)",
        paper_id, len(chunks),
        (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE,
        BATCH_SIZE,
    )


# ---------------------------------------------------------------------------
# CloudWatch metric helper
# ---------------------------------------------------------------------------

def _publish_metric(metric_name: str, value: float) -> None:
    """Publish a single count metric to CloudWatch. Swallows errors silently."""
    try:
        cw_client.put_metric_data(
            Namespace=CW_NAMESPACE,
            MetricData=[{
                "MetricName": metric_name,
                "Value":      value,
                "Unit":       "Count",
                "Dimensions": [{"Name": "Component", "Value": "PaperProcessor"}],
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CloudWatch metric publish failed: %s", exc)
