"""
docling_parser.py — Docling Lambda invocation wrapper (Stage 3, primary)

Wraps the synchronous Lambda:InvokeFunction call to the Docling container Lambda.
Docling is run as a separate Lambda container because it requires:
  - PyTorch (for table structure detection)
  - ~3 GB of container image space
  - EFS mount for model caching

This module is intentionally thin — all parsing logic lives in the
Docling Lambda itself. This file just handles:
  - Request construction
  - Response parsing
  - Error classification (hard fail vs. fallback)

The actual Docling Lambda is deployed as part of Day 3 (EmbeddingStack
or a dedicated DoclingStack). This stub makes the processor testable
without the container Lambda deployed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DoclingParser:
    """
    Invokes the Docling container Lambda for PDF parsing.

    This class is not used directly in handler.py — the invocation
    is inlined there for clarity. This module exists as a testable
    abstraction for unit tests.

    Usage:
        parser = DoclingParser(lambda_client=lambda_client,
                               function_name="research-docling-parser")
        result = parser.parse(paper_id="2401.12345",
                              s3_bucket="research-raw-papers",
                              s3_key="cs.AI/2024/2401.12345.pdf")
    """

    def __init__(self, lambda_client: Any, function_name: str) -> None:
        self._lambda         = lambda_client
        self._function_name  = function_name

    def parse(self, paper_id: str, s3_bucket: str, s3_key: str) -> dict[str, Any]:
        """
        Invoke Docling Lambda and return parsed document.

        Parameters
        ----------
        paper_id : str
        s3_bucket : str
        s3_key : str

        Returns
        -------
        dict
            Parsed document with sections, tables, equations, references.

        Raises
        ------
        DoclingParseError
            If the Lambda invocation fails or returns a function error.
        """
        payload = {
            "paper_id":  paper_id,
            "s3_bucket": s3_bucket,
            "s3_key":    s3_key,
        }

        logger.debug("[%s] Invoking Docling Lambda: %s", paper_id, self._function_name)

        response = self._lambda.invoke(
            FunctionName=self._function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        if response.get("FunctionError"):
            error_payload = json.loads(response["Payload"].read())
            raise DoclingParseError(
                f"Docling Lambda returned error: {error_payload}"
            )

        result = json.loads(response["Payload"].read())
        logger.debug(
            "[%s] Docling parsed %d sections",
            paper_id, len(result.get("sections", [])),
        )
        return result


class DoclingParseError(Exception):
    """Raised when the Docling Lambda returns an error."""
    pass
