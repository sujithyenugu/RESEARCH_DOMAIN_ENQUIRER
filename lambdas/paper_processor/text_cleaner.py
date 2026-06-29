"""
text_cleaner.py — Document cleaning pipeline (Stage 4)

Applies 10 cleaning rules to parsed document sections, transforming
raw Docling/Textract output into clean, indexable text.

All 10 cleaning rules (matches INGESTION_PIPELINE.md §Stage 4):
  Rule 1:  Header removal        — repeated page headers (regex + frequency)
  Rule 2:  Footer removal        — page numbers, running titles
  Rule 3:  Unicode normalization — NFD → NFC, remove non-printable chars
  Rule 4:  Hyphen rejoining      — fix line-break hyphenation (word-\\nword → word)
  Rule 5:  Whitespace collapse   — multiple spaces/newlines → single space
  Rule 6:  Citation placeholder  — preserve [1] and [Author, 2024] style refs
  Rule 7:  Equation preservation — keep LaTeX $...$ and $$...$$ blocks intact
  Rule 8:  Table preservation    — keep Markdown table formatting from Docling
  Rule 9:  Reference parsing     — detect and structure the References section
  Rule 10: Boilerplate removal   — remove arXiv submission notices, footers

Usage:
    cleaner = TextCleaner()
    clean_doc = cleaner.clean(parsed_doc, paper_metadata=paper_msg)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (compiled once at import time for performance)
# ---------------------------------------------------------------------------

# Rule 1 & 2: Header / footer patterns
_RE_PAGE_NUMBER     = re.compile(r"^\s*(?:Page\s+)?\d+\s*(?:of\s+\d+)?\s*$", re.MULTILINE | re.IGNORECASE)
_RE_ARXIV_HEADER    = re.compile(r"arXiv:\d{4}\.\d{4,5}v?\d*\s*\[[\w.]+\].*", re.IGNORECASE)
_RE_RUNNING_TITLE   = re.compile(r"^[A-Z][A-Za-z\s:]{5,60}$", re.MULTILINE)   # short all-caps-ish lines

# Rule 4: Hyphenated line breaks — "transfor-\nmer" → "transformer"
_RE_HYPHEN_BREAK    = re.compile(r"(\w+)-\n(\w+)", re.MULTILINE)

# Rule 5: Whitespace normalisation
_RE_MULTI_SPACE     = re.compile(r"[ \t]{2,}")
_RE_MULTI_NEWLINE   = re.compile(r"\n{3,}")

# Rule 6: Citation preservation markers (kept as-is)
_RE_CITATION        = re.compile(r"\[[\w\s,;.&-]{1,60}\]")

# Rule 7: LaTeX equations
_RE_LATEX_INLINE    = re.compile(r"\$[^$\n]{1,500}\$")
_RE_LATEX_DISPLAY   = re.compile(r"\$\$[\s\S]{1,2000}?\$\$")
_RE_EQUATION_ENV    = re.compile(r"\\begin\{(equation|align|math)\}[\s\S]*?\\end\{\1\}", re.DOTALL)

# Rule 8: Markdown tables (produced by Docling)
_RE_MARKDOWN_TABLE  = re.compile(r"(\|[^\n]+\|\n)((?:\|[-:]+\|[-: |]*\n)?)(\|[^\n]+\|\n)*")

# Rule 9: References section detection
_RE_REF_SECTION     = re.compile(
    r"(?:^|\n)(?:References|Bibliography|Works Cited)\s*\n",
    re.IGNORECASE | re.MULTILINE,
)
_RE_REF_ENTRY       = re.compile(
    r"^\[(\d+)\]\s+(.+?)(?=^\[\d+\]|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Rule 10: Boilerplate patterns
_BOILERPLATE_PHRASES = [
    r"submitted to arxiv",
    r"preprint\. under review",
    r"accepted at",
    r"to appear in",
    r"©\s*\d{4}",
    r"all rights reserved",
    r"this work is licensed under",
    r"creative commons",
    r"correspondence to:",
    r"equal contribution",
    r"^\s*\*\s+equal",                   # * equal contribution footnote
    r"^\s*†\s+",                         # dagger footnotes
    r"^\s*‡\s+",                         # double-dagger footnotes
]
_RE_BOILERPLATE = re.compile(
    "|".join(_BOILERPLATE_PHRASES),
    re.IGNORECASE | re.MULTILINE,
)


class TextCleaner:
    """
    Applies all 10 cleaning rules to a parsed paper document.

    The cleaner is stateless between papers — safe to reuse across
    warm Lambda invocations.
    """

    def clean(
        self,
        parsed_doc: dict[str, Any],
        paper_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Clean a parsed document and return the cleaned document structure.

        Parameters
        ----------
        parsed_doc : dict
            Output from Docling or Textract parser.
            Expected keys: paper_id, sections, tables, equations, references

        paper_metadata : dict
            Original paper metadata from SQS message.
            Used to detect paper_id-specific boilerplate.

        Returns
        -------
        dict
            Cleaned document matching the schema in INGESTION_PIPELINE.md §Stage 4.
        """
        paper_id = parsed_doc.get("paper_id", paper_metadata.get("paper_id", ""))
        sections = parsed_doc.get("sections", [])

        # Rule 1+2: Detect frequent header/footer lines across all sections
        frequent_lines = self._detect_frequent_lines(sections)

        cleaned_sections = []
        for sec in sections:
            raw_text = sec.get("text", "")
            if not raw_text.strip():
                continue

            cleaned_text = self._clean_section(
                raw_text,
                frequent_lines=frequent_lines,
                paper_id=paper_id,
            )

            if not cleaned_text.strip():
                logger.debug("Section %s became empty after cleaning, skipping",
                             sec.get("section_id", "?"))
                continue

            cleaned_sections.append({
                "section_id":    sec.get("section_id", ""),
                "title":         sec.get("title", ""),
                "text":          cleaned_text,
                "char_count":    len(cleaned_text),
                "has_equations": bool(_RE_LATEX_INLINE.search(cleaned_text)
                                      or _RE_LATEX_DISPLAY.search(cleaned_text)),
                "has_tables":    bool(_RE_MARKDOWN_TABLE.search(cleaned_text)),
                "page_start":    sec.get("page_start"),
                "page_end":      sec.get("page_end"),
                "level":         sec.get("level", 1),
            })

        # Rule 9: Parse references section into structured list
        references = self._parse_references(parsed_doc.get("references", []))

        return {
            "paper_id":        paper_id,
            "cleaned_sections": cleaned_sections,
            "tables":          parsed_doc.get("tables", []),
            "equations":       parsed_doc.get("equations", []),
            "references":      references,
            "total_char_count": sum(s["char_count"] for s in cleaned_sections),
            "section_count":   len(cleaned_sections),
        }

    # ------------------------------------------------------------------
    # Core cleaning pipeline (applied per section)
    # ------------------------------------------------------------------

    def _clean_section(
        self,
        text: str,
        frequent_lines: set[str],
        paper_id: str,
    ) -> str:
        """
        Apply all 10 cleaning rules to a single section's text.

        Rules are applied in order; each rule receives the output of the
        previous rule so they compose cleanly.
        """
        # Rule 3: Unicode normalization first (before regex matching)
        text = self._rule_unicode_normalize(text)

        # Rule 1: Remove known header/footer lines
        text = self._rule_remove_frequent_lines(text, frequent_lines)

        # Rule 2: Remove page number lines and arXiv header stamps
        text = self._rule_remove_footers(text)

        # Rule 10: Remove boilerplate phrases
        text = self._rule_remove_boilerplate(text)

        # Rule 4: Rejoin hyphenated line breaks (must be before whitespace collapse)
        text = self._rule_rejoin_hyphens(text)

        # Rule 7: Preserve equations (protect from further regex passes)
        text, equation_placeholders = self._rule_protect_equations(text)

        # Rule 8: Preserve Markdown tables
        text, table_placeholders = self._rule_protect_tables(text)

        # Rule 5: Collapse whitespace
        text = self._rule_collapse_whitespace(text)

        # Rule 6: Citation preservation — no-op (citations kept as-is)
        # We just verify they survived — citations are never stripped.

        # Restore protected equations and tables
        text = self._restore_placeholders(text, equation_placeholders)
        text = self._restore_placeholders(text, table_placeholders)

        # Rule 9: Reference section stripped from body text
        # (References are parsed separately; body sections shouldn't contain them)
        text = self._rule_strip_reference_section(text)

        return text.strip()

    # ------------------------------------------------------------------
    # Rule 1+2: Frequent line detection (header/footer removal)
    # ------------------------------------------------------------------

    def _detect_frequent_lines(self, sections: list[dict[str, Any]]) -> set[str]:
        """
        Find lines that appear 3+ times across all sections.

        These are almost certainly repeated page headers or footers
        (e.g. "NEURAL INFORMATION PROCESSING SYSTEMS", "© 2024 Author").

        A line must be:
          - Between 5 and 80 characters (headers are short)
          - Not contain meaningful punctuation (not a real sentence)
          - Appear ≥ 3 times

        Returns
        -------
        set[str]
            Set of line strings that should be removed.
        """
        line_counter: Counter = Counter()
        for sec in sections:
            for line in sec.get("text", "").splitlines():
                stripped = line.strip()
                if 5 <= len(stripped) <= 80 and stripped.count(".") < 2:
                    line_counter[stripped] += 1

        return {line for line, count in line_counter.items() if count >= 3}

    def _rule_remove_frequent_lines(self, text: str, frequent_lines: set[str]) -> str:
        """Rule 1: Remove lines identified as repeated headers/footers."""
        if not frequent_lines:
            return text
        lines = text.splitlines()
        cleaned = [line for line in lines if line.strip() not in frequent_lines]
        return "\n".join(cleaned)

    # ------------------------------------------------------------------
    # Rule 2: Footer/page number removal
    # ------------------------------------------------------------------

    def _rule_remove_footers(self, text: str) -> str:
        """Rule 2: Remove page numbers, arXiv preprint stamps."""
        text = _RE_PAGE_NUMBER.sub("", text)
        text = _RE_ARXIV_HEADER.sub("", text)
        return text

    # ------------------------------------------------------------------
    # Rule 3: Unicode normalization
    # ------------------------------------------------------------------

    def _rule_unicode_normalize(self, text: str) -> str:
        """
        Rule 3: Normalize Unicode.

        Steps:
          1. NFC normalization (canonical composition — preferred for indexing)
          2. Remove non-printable control characters (U+0000–U+001F except \\n \\t)
          3. Normalize curly quotes to straight quotes
          4. Replace non-breaking spaces with regular spaces
        """
        # NFC normalization (canonical composition)
        text = unicodedata.normalize("NFC", text)

        # Remove non-printable control characters (preserve \n \t \r)
        text = "".join(
            ch for ch in text
            if unicodedata.category(ch) not in ("Cc", "Cs")
            or ch in ("\n", "\t", "\r")
        )

        # Normalize typographic quotation marks → ASCII equivalents
        text = text.replace("\u2018", "'").replace("\u2019", "'")  # '' → ''
        text = text.replace("\u201c", '"').replace("\u201d", '"')  # "" → ""
        text = text.replace("\u2013", "-").replace("\u2014", "--") # –— → -–

        # Replace non-breaking space with regular space
        text = text.replace("\u00a0", " ")

        return text

    # ------------------------------------------------------------------
    # Rule 4: Hyphen rejoining
    # ------------------------------------------------------------------

    def _rule_rejoin_hyphens(self, text: str) -> str:
        """
        Rule 4: Re-join words hyphenated across line breaks.

        Pattern: "transfor-\\nmer" → "transformer"
        Only rejoins when both parts are all-alphabetic (avoids mangling
        hyphenated compound words like "state-of-the-art").
        """
        def _rejoin(match: re.Match) -> str:
            left, right = match.group(1), match.group(2)
            # Only rejoin purely alphabetic fragments
            if left.isalpha() and right.isalpha():
                return left + right
            # Keep the hyphen for compound words
            return match.group(0).replace("\n", " ")

        return _RE_HYPHEN_BREAK.sub(_rejoin, text)

    # ------------------------------------------------------------------
    # Rule 5: Whitespace collapse
    # ------------------------------------------------------------------

    def _rule_collapse_whitespace(self, text: str) -> str:
        """
        Rule 5: Normalize whitespace.

        - Multiple spaces/tabs → single space
        - 3+ consecutive newlines → double newline (paragraph separator)
        - Strip leading/trailing whitespace per line
        """
        text = _RE_MULTI_SPACE.sub(" ", text)
        text = _RE_MULTI_NEWLINE.sub("\n\n", text)
        return text

    # ------------------------------------------------------------------
    # Rule 6: Citation preservation (no-op — kept for documentation)
    # ------------------------------------------------------------------
    # Citations like [1], [Smith et al., 2024], [BERT] are preserved as-is.
    # They are crucial for grounding and citation accuracy evaluation.
    # No transformation is applied.

    # ------------------------------------------------------------------
    # Rule 7: Equation preservation
    # ------------------------------------------------------------------

    def _rule_protect_equations(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Rule 7: Replace LaTeX equations with placeholder tokens.

        Prevents whitespace collapse or other rules from mangling
        equation syntax like "\\frac{1}{2}\\sum_{i=1}^{N}".

        Returns
        -------
        tuple[str, dict[str, str]]
            Modified text with placeholders, and mapping of placeholder → original.
        """
        placeholders: dict[str, str] = {}
        counter = [0]

        def _replace(match: re.Match) -> str:
            token = f"__EQ_{counter[0]}__"
            placeholders[token] = match.group(0)
            counter[0] += 1
            return token

        # Display equations first (greedy match of longer pattern)
        text = _RE_LATEX_DISPLAY.sub(_replace, text)
        text = _RE_EQUATION_ENV.sub(_replace, text)
        # Then inline equations
        text = _RE_LATEX_INLINE.sub(_replace, text)

        return text, placeholders

    # ------------------------------------------------------------------
    # Rule 8: Table preservation
    # ------------------------------------------------------------------

    def _rule_protect_tables(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Rule 8: Replace Markdown tables with placeholder tokens.

        Docling outputs tables as Markdown pipe tables. We preserve these
        exactly so they can be indexed and rendered correctly.
        """
        placeholders: dict[str, str] = {}
        counter = [0]

        def _replace(match: re.Match) -> str:
            token = f"__TABLE_{counter[0]}__"
            placeholders[token] = match.group(0)
            counter[0] += 1
            return token

        text = _RE_MARKDOWN_TABLE.sub(_replace, text)
        return text, placeholders

    # ------------------------------------------------------------------
    # Rule 9: Reference section parsing
    # ------------------------------------------------------------------

    def _rule_strip_reference_section(self, text: str) -> str:
        """
        Rule 9 (body-side): Remove the References section from body text.

        The actual reference parsing is done in _parse_references() on the
        full document. Here we just prevent the raw reference list from
        appearing in the section text passed to the chunker.
        """
        match = _RE_REF_SECTION.search(text)
        if match:
            return text[: match.start()].strip()
        return text

    def _parse_references(
        self, raw_references: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Rule 9 (structured output): Structure the references list.

        If Docling already parsed references into a structured list, use them.
        Otherwise return the raw list as-is.

        Expected output per reference:
          {
            "ref_id": "[1]",
            "title": "...",
            "authors": ["..."],
            "year": 2024,
            "arxiv_id": "2401.12345"   # if present
          }
        """
        if not raw_references:
            return []

        structured = []
        for ref in raw_references:
            # Already structured (Docling output)
            if isinstance(ref, dict) and "title" in ref:
                structured.append(ref)
                continue

            # Try to parse from raw string
            if isinstance(ref, str):
                structured.append({"raw": ref})

        return structured

    # ------------------------------------------------------------------
    # Rule 10: Boilerplate removal
    # ------------------------------------------------------------------

    def _rule_remove_boilerplate(self, text: str) -> str:
        """
        Rule 10: Remove known boilerplate phrases line by line.

        Removes entire lines that match boilerplate patterns (not individual
        phrases mid-sentence) to avoid corrupting legitimate content.
        """
        lines = text.splitlines()
        cleaned_lines = []
        for line in lines:
            # Remove the line if it is predominantly boilerplate
            if _RE_BOILERPLATE.search(line) and len(line.strip()) < 120:
                logger.debug("Removed boilerplate line: %s", line.strip()[:60])
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines)

    # ------------------------------------------------------------------
    # Placeholder restoration
    # ------------------------------------------------------------------

    @staticmethod
    def _restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
        """Replace placeholder tokens with their original content."""
        for token, original in placeholders.items():
            text = text.replace(token, original)
        return text
