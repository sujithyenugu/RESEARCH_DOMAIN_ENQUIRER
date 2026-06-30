"""
handler.py — Docling Parser Lambda (Container Image)

Entry point for the Docling container Lambda.
This is the FULL implementation (not the stub in paper_processor/).

Why a separate container Lambda?
  - Docling requires PyTorch for table structure detection (~2 GB model weights)
  - Container image size: ~3 GB (exceeds Lambda zip limit of 250 MB)
  - EFS mount at /mnt/tmp used for:
      * Scratch space for downloaded PDFs
      * Docling ML model cache (persists across warm starts → avoids re-download)

Execution flow:
  1. Receive invocation from paper_processor Lambda (synchronous RequestResponse)
  2. Download PDF from S3 to EFS scratch space
  3. Parse with Docling DocumentConverter
  4. Map Docling output to project schema (sections, tables, references, equations)
  5. Upload parsed JSON to S3 parsed-papers bucket
  6. Delete local PDF (EFS has limited space)
  7. Return structured parsed document to caller

Input event:
  {
    "paper_id":  "2401.12345",
    "s3_bucket": "research-raw-papers",
    "s3_key":    "cs.AI/2024/2401.12345.pdf"
  }

Output:
  {
    "paper_id": "2401.12345",
    "sections": [{"id": "s0", "title": "Abstract", "text": "...", "page_start": 1, "level": 1}],
    "tables":   [{"id": "t0", "caption": "Results", "data": [[...]], "page": 4}],
    "references": [{"key": "ref1", "title": "...", "authors": [...], "year": 2023}],
    "equations": [{"id": "eq0", "latex": "E=mc^2", "page": 2}],
    "metadata": {"page_count": 12, "char_count": 45000, "section_count": 8},
    "s3_parsed_key": "2401.12345/docling.json"
  }

Timeout: 600s (10 min) — sufficient for 50-page papers
Memory:  3008 MB — PyTorch table detection requires ~2 GB
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Docling imports — available inside the container image
from docling.document_converter import DocumentConverter, ConversionStatus
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.base_models import InputFormat

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EFS_MOUNT       = "/mnt/tmp"                          # EFS access point mount
MODELS_PATH     = "/mnt/tmp/docling-models"           # Cached ML model weights
SCRATCH_DIR     = pathlib.Path(EFS_MOUNT) / "scratch" # PDF download scratch dir

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda warm-start reuse)
# ---------------------------------------------------------------------------
_REGION    = os.environ.get("AWS_REGION", "us-east-1")
s3_client  = boto3.client("s3", region_name=_REGION)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PARSED_PAPERS_BUCKET = os.environ.get("PARSED_PAPERS_BUCKET", "research-parsed-papers")

# ---------------------------------------------------------------------------
# Docling DocumentConverter (module-level — avoid re-initialising on warm starts)
# First cold start downloads models to MODELS_PATH (EFS) — subsequent warm
# starts reuse cached models from EFS for near-instant initialisation.
# ---------------------------------------------------------------------------
_pipeline_options = PdfPipelineOptions(
    artifacts_path=MODELS_PATH,
    do_ocr=True,
    do_table_structure=True,
    table_structure_options={"mode": "accurate"},
)

_CONVERTER = DocumentConverter(
    format_options={
        InputFormat.PDF: _pipeline_options,
    }
)
logger.info("Docling DocumentConverter initialised. Models path: %s", MODELS_PATH)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entry point.

    Parameters
    ----------
    event : dict
        Invocation payload from paper_processor Lambda:
          { "paper_id": str, "s3_bucket": str, "s3_key": str }
    context : LambdaContext
        Standard Lambda context object.

    Returns
    -------
    dict
        Structured parsed document (sections, tables, references, equations, metadata).

    Raises
    ------
    DoclingParseError
        If Docling fails to convert the PDF. Caller (paper_processor) catches
        this and falls back to Textract.
    """
    paper_id  = event["paper_id"]
    s3_bucket = event["s3_bucket"]
    s3_key    = event["s3_key"]

    logger.info("[%s] Starting Docling parse | s3://%s/%s", paper_id, s3_bucket, s3_key)

    # Ensure scratch directory exists on EFS
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    local_pdf = SCRATCH_DIR / f"{paper_id}.pdf"

    try:
        # Step 1: Download PDF from S3 → EFS scratch
        _download_from_s3(s3_client, s3_bucket, s3_key, str(local_pdf))
        logger.info("[%s] PDF downloaded to %s (%d bytes)", paper_id, local_pdf, local_pdf.stat().st_size)

        # Step 2: Parse with Docling
        result = _CONVERTER.convert(str(local_pdf))

        if result.status != ConversionStatus.SUCCESS:
            raise DoclingParseError(
                f"[{paper_id}] Docling conversion failed with status: {result.status}"
            )

        # Step 3: Map Docling output → project schema
        parsed_doc = _convert_to_schema(result, paper_id)
        logger.info(
            "[%s] Docling parse complete: %d sections, %d tables, %d references, %d equations",
            paper_id,
            len(parsed_doc["sections"]),
            len(parsed_doc["tables"]),
            len(parsed_doc["references"]),
            len(parsed_doc["equations"]),
        )

        # Step 4: Upload parsed JSON → S3 parsed-papers bucket
        s3_parsed_key = _upload_to_s3(s3_client, PARSED_PAPERS_BUCKET, paper_id, parsed_doc)
        parsed_doc["s3_parsed_key"] = s3_parsed_key
        logger.info("[%s] Uploaded parsed doc to s3://%s/%s", paper_id, PARSED_PAPERS_BUCKET, s3_parsed_key)

        return parsed_doc

    except DoclingParseError:
        raise   # Re-raise to caller — it will fall back to Textract

    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        raise DoclingParseError(
            f"[{paper_id}] S3 error ({error_code}): {exc}"
        ) from exc

    except Exception as exc:
        raise DoclingParseError(
            f"[{paper_id}] Unexpected error during Docling parse: {exc}"
        ) from exc

    finally:
        # Step 5: Clean up local PDF to free EFS space
        if local_pdf.exists():
            local_pdf.unlink()
            logger.debug("[%s] Deleted local PDF %s", paper_id, local_pdf)


# ---------------------------------------------------------------------------
# Helper: S3 download
# ---------------------------------------------------------------------------

def _download_from_s3(
    s3: Any,
    bucket: str,
    key: str,
    local_path: str,
) -> None:
    """
    Download a file from S3 to a local path on EFS.

    Parameters
    ----------
    s3 : boto3 S3 client
    bucket : str
    key : str
    local_path : str
        Destination path on EFS (e.g. /mnt/tmp/scratch/2401.12345.pdf).
    """
    s3.download_file(Bucket=bucket, Key=key, Filename=local_path)


# ---------------------------------------------------------------------------
# Helper: S3 upload
# ---------------------------------------------------------------------------

def _upload_to_s3(
    s3: Any,
    bucket: str,
    paper_id: str,
    parsed_doc: dict[str, Any],
) -> str:
    """
    Upload the parsed document JSON to S3.

    Parameters
    ----------
    s3 : boto3 S3 client
    bucket : str
        Target bucket (research-parsed-papers).
    paper_id : str
    parsed_doc : dict
        The complete parsed document dict.

    Returns
    -------
    str
        The S3 key of the uploaded object.
    """
    s3_key = f"{paper_id}/docling.json"
    body   = json.dumps(parsed_doc, ensure_ascii=False, indent=2)

    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    return s3_key


# ---------------------------------------------------------------------------
# Helper: Docling output → project schema
# ---------------------------------------------------------------------------

def _convert_to_schema(
    docling_result: Any,
    paper_id: str,
) -> dict[str, Any]:
    """
    Map Docling's internal document model to the project schema.

    Docling exposes a rich object model (DoclingDocument). This function
    walks the document tree and produces flat, serialisable Python dicts
    that match the schema consumed by paper_processor and the embedding
    / graph pipelines downstream.

    Parameters
    ----------
    docling_result : ConversionResult
        The result object from DocumentConverter.convert().
    paper_id : str

    Returns
    -------
    dict
        Structured document with keys:
          paper_id, sections, tables, references, equations, metadata
    """
    doc = docling_result.document

    sections:   list[dict] = []
    tables:     list[dict] = []
    references: list[dict] = []
    equations:  list[dict] = []

    section_counter  = 0
    table_counter    = 0
    equation_counter = 0

    # Walk the document body
    for item, level in doc.iterate_items():
        item_type = type(item).__name__

        # ----- Section / Paragraph text -----
        if item_type in ("SectionHeaderItem", "TextItem"):
            text = getattr(item, "text", "") or ""
            if not text.strip():
                continue

            is_header = item_type == "SectionHeaderItem"
            section_level = getattr(item, "level", 2) if is_header else 3

            # Attempt to get page number
            page_num = None
            if hasattr(item, "prov") and item.prov:
                page_num = getattr(item.prov[0], "page_no", None)

            sections.append({
                "id":         f"s{section_counter}",
                "title":      text if is_header else "",
                "text":       "" if is_header else text,
                "page_start": page_num,
                "level":      section_level,
            })
            section_counter += 1

        # ----- Tables -----
        elif item_type == "TableItem":
            caption = ""
            if hasattr(item, "caption_text"):
                caption = item.caption_text(doc) or ""

            # Export table as 2D list (rows × cols)
            table_data: list[list[str]] = []
            if hasattr(item, "export_to_dataframe"):
                try:
                    df = item.export_to_dataframe()
                    table_data = df.values.tolist()
                except Exception:  # noqa: BLE001
                    table_data = []

            page_num = None
            if hasattr(item, "prov") and item.prov:
                page_num = getattr(item.prov[0], "page_no", None)

            tables.append({
                "id":      f"t{table_counter}",
                "caption": caption,
                "data":    table_data,
                "page":    page_num,
            })
            table_counter += 1

        # ----- Equations -----
        elif item_type == "EquationItem":
            latex = getattr(item, "text", "") or ""
            page_num = None
            if hasattr(item, "prov") and item.prov:
                page_num = getattr(item.prov[0], "page_no", None)

            equations.append({
                "id":    f"eq{equation_counter}",
                "latex": latex,
                "page":  page_num,
            })
            equation_counter += 1

    # ----- References -----
    if hasattr(doc, "references"):
        for ref_key, ref in doc.references.items():
            references.append({
                "key":     ref_key,
                "title":   getattr(ref, "title", "") or "",
                "authors": [str(a) for a in getattr(ref, "authors", [])],
                "year":    getattr(ref, "year", None),
            })

    # ----- Metadata -----
    total_chars = sum(len(s["text"]) for s in sections)
    metadata = {
        "page_count":    getattr(doc, "num_pages", None),
        "char_count":    total_chars,
        "section_count": len([s for s in sections if s["title"] == ""]),
    }

    return {
        "paper_id":   paper_id,
        "sections":   sections,
        "tables":     tables,
        "references": references,
        "equations":  equations,
        "metadata":   metadata,
    }


# ---------------------------------------------------------------------------
# Custom Exception
# ---------------------------------------------------------------------------

class DoclingParseError(Exception):
    """
    Raised when the Docling Lambda fails to parse a PDF.

    paper_processor catches this and falls back to Textract.
    """
    pass
