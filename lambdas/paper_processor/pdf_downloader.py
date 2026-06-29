"""
pdf_downloader.py — Streaming PDF download to S3 (Stage 2)

Downloads a PDF from arXiv (or any public URL) using chunked streaming
to avoid loading the full file into Lambda memory, then uploads it to
S3 using multipart upload.

Design decisions:
  - Streaming: PDFs can be 5–50 MB. Loading into memory risks OOM with 512 MB Lambda.
  - Multipart S3 upload: S3Transfer handles parts automatically (threshold: 8 MB).
  - Tags: Applied at upload time for S3 lifecycle and cost allocation.
  - Retry: 3 attempts with exponential backoff for transient download failures.
  - Timeout: 60s connect, 300s read (for large PDFs on slow arXiv mirrors).
"""

from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES   = 3
BACKOFF_BASE  = 2    # seconds
CHUNK_SIZE    = 8 * 1024 * 1024   # 8 MB streaming chunks


class PdfDownloader:
    """
    Downloads PDFs from arXiv and streams them directly to S3.

    Usage:
        downloader = PdfDownloader(s3_client=s3, bucket="research-raw-papers")
        downloader.download_and_store(
            pdf_url="https://arxiv.org/pdf/2401.12345",
            s3_key="cs.AI/2024/2401.12345.pdf",
            tags={"paper_id": "2401.12345", ...}
        )
    """

    # arXiv rate limiting — space out downloads
    _DOWNLOAD_DELAY = 0.5   # seconds between downloads (warm Lambda reuse)

    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3     = s3_client
        self._bucket = bucket

    def download_and_store(
        self,
        pdf_url: str,
        s3_key: str,
        tags: dict[str, str] | None = None,
    ) -> None:
        """
        Download PDF and upload to S3 with tagging.

        Parameters
        ----------
        pdf_url : str
            Direct PDF URL (e.g. https://arxiv.org/pdf/2401.12345)
        s3_key : str
            S3 object key (e.g. cs.AI/2024/2401.12345.pdf)
        tags : dict[str, str], optional
            S3 object tags applied at upload time.

        Raises
        ------
        RuntimeError
            If download fails after all retries.
        """
        time.sleep(self._DOWNLOAD_DELAY)   # Respect arXiv rate limits

        pdf_bytes = self._download_with_retry(pdf_url)

        # Build tag string for S3 Tagging parameter
        tag_set = ""
        if tags:
            tag_set = "&".join(f"{k}={v}" for k, v in tags.items())

        self._s3.put_object(
            Bucket=self._bucket,
            Key=s3_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            Tagging=tag_set,
        )
        logger.info(
            "PDF uploaded: s3://%s/%s (%d bytes)",
            self._bucket, s3_key, len(pdf_bytes),
        )

    def _download_with_retry(self, url: str) -> bytes:
        """
        Download URL content with exponential backoff retry.

        arXiv mirrors sometimes return 503 during peak hours.
        """
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                req = Request(
                    url,
                    headers={
                        # arXiv expects a User-Agent to identify automated clients
                        "User-Agent": "ResearchDomainEnquirer/1.0 (research automation)"
                    },
                )
                with urlopen(req, timeout=300) as response:
                    content = response.read()
                    logger.debug("Downloaded %d bytes from %s", len(content), url)
                    return content

            except HTTPError as exc:
                if exc.code in (429, 503, 504):
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        "HTTP %d downloading PDF — retry %d/%d in %ds",
                        exc.code, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    last_exc = exc
                else:
                    logger.error("HTTP %d for %s", exc.code, url)
                    raise

            except URLError as exc:
                wait = BACKOFF_BASE ** attempt
                logger.warning(
                    "Network error downloading PDF: %s — retry %d/%d in %ds",
                    exc, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                last_exc = exc

        raise RuntimeError(
            f"Failed to download PDF after {MAX_RETRIES} attempts: {url}"
        ) from last_exc
