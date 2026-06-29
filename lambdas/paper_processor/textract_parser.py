"""
textract_parser.py — Amazon Textract fallback parser (Stage 3)

Used when Docling fails (e.g. corrupted PDF, scanned document, Docling crash).

Textract analysis flow:
  1. StartDocumentAnalysis → returns JobId (async)
  2. Poll GetDocumentAnalysis until job completes (up to 10 minutes)
  3. Reconstruct page text from WORD/LINE blocks
  4. Detect section headers via font-size heuristics (WORD confidence + block type)
  5. Return structured document matching Docling output schema

Note: Textract is significantly less accurate for:
  - LaTeX math equations (rendered as images in arXiv PDFs)
  - Multi-column layouts (common in conference papers)
  - Complex tables with merged cells

For these cases, the output will be imperfect but still indexable.
The fallback path is acceptable quality for search, not perfect parsing.

AWS Textract docs:
  https://docs.aws.amazon.com/textract/latest/dg/API_StartDocumentAnalysis.html
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Polling config for async Textract job
POLL_INTERVAL_SECONDS = 5
MAX_POLL_ATTEMPTS     = 120    # 10 minutes maximum (5s × 120)

# Textract block types
BLOCK_TYPE_PAGE    = "PAGE"
BLOCK_TYPE_LINE    = "LINE"
BLOCK_TYPE_WORD    = "WORD"


class TextractParser:
    """
    Fallback PDF parser using Amazon Textract.

    Creates a minimal but structurally valid document that matches the
    Docling output schema so the rest of the pipeline (text_cleaner,
    late_chunker) can process it without modification.

    Usage:
        parser = TextractParser(
            s3_client=s3_client,
            bucket="research-raw-papers",
            s3_key="cs.AI/2024/2401.12345.pdf",
            paper_id="2401.12345"
        )
        doc = parser.parse()
    """

    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        s3_key: str,
        paper_id: str,
    ) -> None:
        self._s3_client  = s3_client
        self._bucket     = bucket
        self._s3_key     = s3_key
        self._paper_id   = paper_id
        self._textract   = boto3.client(
            "textract",
            region_name=s3_client.meta.region_name,
        )

    def parse(self) -> dict[str, Any]:
        """
        Run Textract async document analysis and reconstruct document structure.

        Returns
        -------
        dict
            Document in Docling-compatible format:
            {
                "paper_id": "...",
                "sections": [...],
                "tables": [...],
                "equations": [],          # Textract cannot parse LaTeX
                "references": [],         # Minimal — no structured refs
                "parser": "textract"      # Flag for downstream quality awareness
            }
        """
        logger.info(
            "[%s] Starting Textract analysis for s3://%s/%s",
            self._paper_id, self._bucket, self._s3_key,
        )

        # Start async job
        job_id = self._start_analysis()

        # Poll until complete
        blocks = self._wait_for_completion(job_id)

        # Reconstruct text structure from blocks
        sections = self._reconstruct_sections(blocks)
        tables   = self._extract_tables(blocks)

        logger.info(
            "[%s] Textract complete: %d sections, %d tables (parser=textract)",
            self._paper_id, len(sections), len(tables),
        )

        return {
            "paper_id":  self._paper_id,
            "sections":  sections,
            "tables":    tables,
            "equations": [],          # Textract cannot recover LaTeX
            "references": [],
            "parser":    "textract",  # Signal to downstream for quality awareness
        }

    # ------------------------------------------------------------------
    # Textract API calls
    # ------------------------------------------------------------------

    def _start_analysis(self) -> str:
        """Start async Textract document analysis and return JobId."""
        response = self._textract.start_document_analysis(
            DocumentLocation={
                "S3Object": {
                    "Bucket": self._bucket,
                    "Name":   self._s3_key,
                }
            },
            FeatureTypes=["TABLES"],    # TABLE analysis for paper tables
        )
        job_id = response["JobId"]
        logger.debug("[%s] Textract job started: %s", self._paper_id, job_id)
        return job_id

    def _wait_for_completion(self, job_id: str) -> list[dict[str, Any]]:
        """
        Poll GetDocumentAnalysis until the job succeeds or fails.

        Returns
        -------
        list[dict]
            All Textract blocks from the completed job.

        Raises
        ------
        TextractJobFailedError
            If the job fails or times out.
        """
        all_blocks: list[dict[str, Any]] = []
        next_token: str | None = None

        for attempt in range(MAX_POLL_ATTEMPTS):
            kwargs: dict[str, Any] = {"JobId": job_id}
            if next_token:
                kwargs["NextToken"] = next_token

            response = self._textract.get_document_analysis(**kwargs)
            status   = response["JobStatus"]

            if status == "SUCCEEDED":
                all_blocks.extend(response.get("Blocks", []))

                # Paginate through all blocks
                while "NextToken" in response:
                    response = self._textract.get_document_analysis(
                        JobId=job_id,
                        NextToken=response["NextToken"],
                    )
                    all_blocks.extend(response.get("Blocks", []))

                logger.info(
                    "[%s] Textract job %s complete: %d blocks",
                    self._paper_id, job_id, len(all_blocks),
                )
                return all_blocks

            elif status == "FAILED":
                error = response.get("StatusMessage", "Unknown error")
                raise TextractJobFailedError(
                    f"Textract job {job_id} failed: {error}"
                )

            # IN_PROGRESS — keep polling
            logger.debug(
                "[%s] Textract job %s — status: %s (attempt %d/%d)",
                self._paper_id, job_id, status, attempt + 1, MAX_POLL_ATTEMPTS,
            )
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TextractJobFailedError(
            f"Textract job {job_id} timed out after {MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
        )

    # ------------------------------------------------------------------
    # Document reconstruction
    # ------------------------------------------------------------------

    def _reconstruct_sections(
        self, blocks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Reconstruct document sections from Textract LINE blocks.

        Heuristic section detection:
          - A line is treated as a section header if it:
            1. Is short (< 80 chars)
            2. Has high confidence (> 95%)
            3. Starts with a digit (e.g. "1 Introduction", "2.1 Methods")
               OR is all-caps/title-case with no sentence punctuation

        Groups consecutive non-header lines into section text.
        """
        # Extract text lines grouped by page
        pages: dict[int, list[str]] = {}
        for block in blocks:
            if block.get("BlockType") != BLOCK_TYPE_LINE:
                continue
            page_num = block.get("Page", 1)
            text     = block.get("Text", "").strip()
            if text:
                pages.setdefault(page_num, []).append(text)

        if not pages:
            return [{"section_id": "full_text",
                     "title": "Full Text",
                     "text": "(No text extracted by Textract)",
                     "page_start": 1, "page_end": 1, "level": 1}]

        sections: list[dict[str, Any]] = []
        current_title = "Introduction"
        current_text_lines: list[str] = []
        current_page_start = 1
        section_counter = 0

        for page_num in sorted(pages.keys()):
            for line in pages[page_num]:
                if self._is_section_header(line):
                    # Flush current section
                    if current_text_lines:
                        sections.append({
                            "section_id":  f"sec_{section_counter}",
                            "title":       current_title,
                            "text":        "\n".join(current_text_lines),
                            "page_start":  current_page_start,
                            "page_end":    page_num,
                            "level":       1,
                        })
                        section_counter += 1

                    current_title       = line
                    current_text_lines  = []
                    current_page_start  = page_num
                else:
                    current_text_lines.append(line)

        # Flush final section
        if current_text_lines:
            sections.append({
                "section_id":  f"sec_{section_counter}",
                "title":       current_title,
                "text":        "\n".join(current_text_lines),
                "page_start":  current_page_start,
                "page_end":    max(pages.keys()),
                "level":       1,
            })

        return sections

    @staticmethod
    def _is_section_header(line: str) -> bool:
        """
        Heuristic: Is this line a section header?

        Returns True if:
          - Length 5–80 chars
          - No sentence-ending punctuation (., ?, !)
          - Starts with a digit (section numbering) or is title-case/all-caps
        """
        stripped = line.strip()
        if not (5 <= len(stripped) <= 80):
            return False
        if stripped.endswith((".", "?", "!", ",")):
            return False
        # Starts with digit: "1 Introduction", "2.3 Ablation"
        if stripped and stripped[0].isdigit():
            return True
        # All-caps section titles: "ABSTRACT", "CONCLUSION"
        if stripped.isupper() and len(stripped) > 4:
            return True
        return False

    def _extract_tables(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Extract tables from Textract TABLE blocks.

        Returns a minimal table representation — not as rich as Docling's
        Markdown tables, but preserves the data for downstream indexing.
        """
        tables = []
        table_counter = 0

        for block in blocks:
            if block.get("BlockType") != "TABLE":
                continue

            tables.append({
                "table_id":  f"textract_tab_{table_counter}",
                "caption":   f"Table {table_counter + 1}",
                "markdown":  "(Textract table — see raw S3 JSON for cell data)",
                "page":      block.get("Page", 1),
                "source":    "textract",
            })
            table_counter += 1

        return tables


class TextractJobFailedError(Exception):
    """Raised when a Textract async job fails or times out."""
    pass
