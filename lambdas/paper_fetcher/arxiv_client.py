"""
arxiv_client.py — arXiv Atom API client

Handles all communication with the arXiv export API:
  - Constructs search queries for each category
  - Implements exponential backoff for 429 / 503 responses
  - Parses Atom XML into ArxivPaper dataclasses
  - Filters by submission date (lookback window)

arXiv API docs: https://info.arxiv.org/help/api/user-manual.html

Rate limits:
  - arXiv asks for max 1 request per 3 seconds for bulk access
  - We wait 1 second between category requests (7 categories × 1s = 7s total)
  - Exponential backoff on 429: 2^attempt seconds (max 30s)
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Generator
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

# arXiv Atom API base URL
ARXIV_BASE_URL = "http://export.arxiv.org/api/query"

# XML namespaces used in Atom feed
NS = {
    "atom":   "http://www.w3.org/2005/Atom",
    "arxiv":  "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# Retry config
MAX_RETRIES    = 5
BACKOFF_BASE   = 2    # seconds
MAX_BACKOFF    = 30   # seconds cap
REQUEST_DELAY  = 1.0  # seconds between category requests (respect rate limit)


@dataclass
class ArxivPaper:
    """Parsed metadata for a single arXiv paper."""
    paper_id:   str                      # e.g. "2401.12345"
    title:      str
    authors:    list[str]
    abstract:   str
    pdf_url:    str                      # Direct PDF download link
    categories: list[str]               # All arXiv categories
    published:  str                      # ISO-8601, e.g. "2024-01-15T00:00:00Z"
    updated:    str                      # ISO-8601
    doi:        str = ""                 # May be empty for preprints
    journal_ref: str = ""               # Journal reference if published


class ArxivClient:
    """
    Client for the arXiv Atom API.

    Usage:
        client = ArxivClient(lookback_hours=7, max_results=100)
        papers = client.fetch_category("cs.AI")
        for paper in papers:
            print(paper.paper_id, paper.title)
    """

    def __init__(self, lookback_hours: int = 7, max_results: int = 100) -> None:
        self.lookback_hours = lookback_hours
        self.max_results    = max_results
        self._cutoff_dt     = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)

    def fetch_category(self, category: str) -> list[ArxivPaper]:
        """
        Fetch papers from arXiv for a single category, filtered to
        the lookback window.

        Parameters
        ----------
        category : str
            arXiv category string, e.g. "cs.AI", "stat.ML"

        Returns
        -------
        list[ArxivPaper]
            Papers submitted within the lookback window (may be empty).
        """
        params = {
            "search_query": f"cat:{category}",
            "start":        0,
            "max_results":  self.max_results,
            "sortBy":       "submittedDate",
            "sortOrder":    "descending",
        }
        url = f"{ARXIV_BASE_URL}?{urlencode(params)}"
        logger.debug("arXiv query: %s", url)

        # Respect rate limit between categories
        time.sleep(REQUEST_DELAY)

        xml_content = self._get_with_retry(url)
        papers      = list(self._parse_atom_feed(xml_content))

        # Filter to lookback window
        recent = [p for p in papers if self._is_within_window(p.published)]
        logger.debug(
            "Category %s: %d total → %d within %dh window",
            category, len(papers), len(recent), self.lookback_hours,
        )
        return recent

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_with_retry(self, url: str) -> bytes:
        """
        HTTP GET with exponential backoff on transient failures.

        Retries on:
          - HTTP 429 Too Many Requests
          - HTTP 503 Service Unavailable
          - URLError (network timeout, DNS failure)

        Raises the final exception if all retries are exhausted.
        """
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with urlopen(url, timeout=20) as response:
                    return response.read()
            except HTTPError as exc:
                if exc.code in (429, 503):
                    wait = min(BACKOFF_BASE ** attempt, MAX_BACKOFF)
                    logger.warning(
                        "arXiv HTTP %d — retry %d/%d in %ds",
                        exc.code, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    last_exc = exc
                else:
                    # 404, 400, etc — don't retry
                    logger.error("arXiv API error %d for %s", exc.code, url)
                    raise
            except URLError as exc:
                wait = min(BACKOFF_BASE ** attempt, MAX_BACKOFF)
                logger.warning(
                    "Network error fetching arXiv: %s — retry %d/%d in %ds",
                    exc, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                last_exc = exc

        raise RuntimeError(
            f"arXiv API failed after {MAX_RETRIES} retries"
        ) from last_exc

    def _parse_atom_feed(self, xml_content: bytes) -> Generator[ArxivPaper, None, None]:
        """
        Parse arXiv Atom XML feed into ArxivPaper objects.

        The Atom feed structure:
          <feed>
            <entry>
              <id>http://arxiv.org/abs/2401.12345v1</id>
              <title>Paper Title</title>
              <author><name>Author Name</name></author>
              <summary>Abstract text...</summary>
              <published>2024-01-15T00:00:00Z</published>
              <updated>2024-01-15T12:00:00Z</updated>
              <category term="cs.AI"/>
              <arxiv:doi>10.48550/arXiv.2401.12345</arxiv:doi>
              <link rel="related" title="pdf" href="..."/>
            </entry>
          </feed>
        """
        root = ET.fromstring(xml_content)

        for entry in root.findall("atom:entry", NS):
            try:
                paper = self._parse_entry(entry)
                if paper:
                    yield paper
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse arXiv entry: %s", exc)
                continue

    def _parse_entry(self, entry: ET.Element) -> ArxivPaper | None:
        """Parse a single <entry> element into an ArxivPaper."""
        # Extract arXiv ID from URL: http://arxiv.org/abs/2401.12345v1 → 2401.12345
        id_elem = entry.find("atom:id", NS)
        if id_elem is None or not id_elem.text:
            return None
        raw_id   = id_elem.text.strip()
        paper_id = raw_id.split("/abs/")[-1].split("v")[0]  # strip version

        # Title
        title_elem = entry.find("atom:title", NS)
        title = (title_elem.text or "").strip().replace("\n", " ")

        # Authors
        authors = [
            (author.find("atom:name", NS).text or "").strip()
            for author in entry.findall("atom:author", NS)
            if author.find("atom:name", NS) is not None
        ]

        # Abstract
        summary_elem = entry.find("atom:summary", NS)
        abstract = (summary_elem.text or "").strip().replace("\n", " ")

        # Published / Updated timestamps
        pub_elem = entry.find("atom:published", NS)
        upd_elem = entry.find("atom:updated", NS)
        published = (pub_elem.text or "").strip()
        updated   = (upd_elem.text or "").strip()

        # Categories
        categories = [
            cat.get("term", "")
            for cat in entry.findall("atom:category", NS)
            if cat.get("term")
        ]

        # PDF URL — look for <link rel="related" title="pdf">
        pdf_url = ""
        for link in entry.findall("atom:link", NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break
        # Fallback: construct from paper_id
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{paper_id}"

        # DOI (optional)
        doi_elem = entry.find("arxiv:doi", NS)
        doi = (doi_elem.text or "").strip() if doi_elem is not None else ""

        # Journal ref (optional)
        jref_elem = entry.find("arxiv:journal_ref", NS)
        journal_ref = (jref_elem.text or "").strip() if jref_elem is not None else ""

        return ArxivPaper(
            paper_id=paper_id,
            title=title,
            authors=authors,
            abstract=abstract,
            pdf_url=pdf_url,
            categories=categories,
            published=published,
            updated=updated,
            doi=doi,
            journal_ref=journal_ref,
        )

    def _is_within_window(self, published_str: str) -> bool:
        """
        Return True if the paper's published date is within the lookback window.

        published_str is ISO-8601, e.g. "2024-01-15T00:00:00Z"
        """
        if not published_str:
            return False
        try:
            pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            return pub_dt >= self._cutoff_dt
        except ValueError:
            logger.warning("Could not parse published date: %s", published_str)
            return False
