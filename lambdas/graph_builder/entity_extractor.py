"""
entity_extractor.py — graph_builder Lambda

Wraps Bedrock Claude 3 Haiku to extract structured entities,
relationships, topics, and citation hints from an academic paper.

The extractor builds a prompt from the paper's title, abstract, and
full-text chunks, calls the Bedrock Converse API, then parses the
model's JSON response into a normalised Python dict.

Supported entity types
----------------------
Paper, Author, Model, Dataset, Method, Benchmark, Concept, Topic

Supported edge/relation types
------------------------------
CITES, INTRODUCES, PROPOSES, EVALUATES_ON, AUTHORED_BY,
IMPROVES, USES, BELONGS_TO, BASED_ON
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — instructs Claude to return strict JSON
# ---------------------------------------------------------------------------
ENTITY_EXTRACTION_PROMPT = """You are a scientific knowledge-graph extraction specialist.
Your job is to read an academic paper excerpt and extract structured information.

OUTPUT FORMAT — you MUST return ONLY a single valid JSON object with exactly these four keys:

{
  "entities": [
    {
      "type":        "<one of: Model, Dataset, Method, Benchmark, Concept, Author, Paper>",
      "name":        "<canonical short name of the entity>",
      "description": "<one-sentence description>"
    }
  ],
  "relationships": [
    {
      "source_type": "<entity type>",
      "source_name": "<entity name>",
      "relation":    "<one of: CITES, INTRODUCES, PROPOSES, EVALUATES_ON, IMPROVES, USES, BASED_ON>",
      "target_type": "<entity type>",
      "target_name": "<entity name>"
    }
  ],
  "topics": ["<high-level research topic string>"],
  "citations": [
    {
      "title_snippet": "<first 8-12 words of the cited paper title>",
      "authors":       "<first author surname et al., or full list if <= 3>"
    }
  ]
}

RULES:
1. Return ONLY the JSON object — no markdown fences, no prose.
2. Keep entity names concise and canonical (e.g. "GPT-4", "ImageNet", "BERT").
3. Relation values must be UPPERCASE and from the allowed list above.
4. If no citations are detected, return an empty list for "citations".
5. If no entities are detected, return an empty list for "entities".
6. Limit to the 20 most salient entities and 30 most important relationships.
7. Topics should be 3-8 broad research area strings (e.g. "large language models").
"""


class EntityExtractor:
    """
    Extract entities and relationships from academic paper text using
    Amazon Bedrock (Claude 3 Haiku).

    Parameters
    ----------
    bedrock_client : boto3 client
        Pre-initialised ``bedrock-runtime`` boto3 client.
    model_id : str
        Bedrock model identifier.  Defaults to
        ``"anthropic.claude-3-haiku-20240307-v1:0"``.

    Examples
    --------
    >>> extractor = EntityExtractor(bedrock_client=client, model_id="anthropic.claude-3-haiku-20240307-v1:0")
    >>> result = extractor.extract(
    ...     paper_id="2401.00001",
    ...     title="Attention Is All You Need",
    ...     abstract="We propose the Transformer ...",
    ...     chunk_texts=["chunk 1 text", "chunk 2 text"],
    ... )
    >>> result["entities"][0]
    {'type': 'Model', 'name': 'Transformer', 'description': '...'}
    """

    # Maximum characters sent to the model (context window safety guard)
    _MAX_CHUNK_CHARS = 6_000

    def __init__(self, bedrock_client: Any, model_id: str) -> None:
        self._bedrock = bedrock_client
        self._model_id = model_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        paper_id: str,
        title: str,
        abstract: str,
        chunk_texts: list[str],
    ) -> dict[str, Any]:
        """
        Run entity extraction for a single paper.

        Parameters
        ----------
        paper_id : str
            arXiv / internal paper identifier (used only for logging).
        title : str
            Paper title.
        abstract : str
            Paper abstract.
        chunk_texts : list[str]
            Full-text chunks from the paper (order preserved).

        Returns
        -------
        dict
            Normalised extraction result::

                {
                  "entities":      [{"type": str, "name": str, "description": str}, ...],
                  "relationships": [{"source_type": str, "source_name": str,
                                     "relation": str,
                                     "target_type": str, "target_name": str}, ...],
                  "topics":        [str, ...],
                  "citations":     [{"title_snippet": str, "authors": str}, ...],
                }

            Returns an empty skeleton on parse errors so that downstream
            graph-building can continue gracefully.

        Raises
        ------
        RuntimeError
            If Bedrock's ``invoke_model`` call fails at the transport level.
        """
        prompt = self._build_prompt(paper_id, title, abstract, chunk_texts)
        logger.debug("Calling Bedrock model %s for paper %s", self._model_id, paper_id)

        response_body = self._call_bedrock(prompt)
        result = self._parse_claude_response(response_body)

        logger.debug(
            "Extraction for %s: %d entities, %d relationships, %d topics, %d citations",
            paper_id,
            len(result.get("entities", [])),
            len(result.get("relationships", [])),
            len(result.get("topics", [])),
            len(result.get("citations", [])),
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        paper_id: str,
        title: str,
        abstract: str,
        chunks: list[str],
    ) -> str:
        """
        Assemble the user-turn prompt from paper metadata and text chunks.

        Chunks are concatenated up to ``_MAX_CHUNK_CHARS`` characters to
        stay well within Claude's context window and to keep latency low.

        Parameters
        ----------
        paper_id : str
            Paper identifier (included in prompt for model traceability).
        title : str
            Paper title.
        abstract : str
            Paper abstract.
        chunks : list[str]
            Raw text chunks; may be empty.

        Returns
        -------
        str
            The fully assembled user-turn prompt string.
        """
        # Concatenate chunks up to the character limit
        combined_chunks: list[str] = []
        running_len = 0
        for chunk in chunks:
            if running_len + len(chunk) > self._MAX_CHUNK_CHARS:
                remaining = self._MAX_CHUNK_CHARS - running_len
                if remaining > 200:
                    combined_chunks.append(chunk[:remaining])
                break
            combined_chunks.append(chunk)
            running_len += len(chunk)

        chunk_section = "\n\n---\n\n".join(combined_chunks) if combined_chunks else "(no full text available)"

        return (
            f"PAPER ID: {paper_id}\n\n"
            f"TITLE:\n{title}\n\n"
            f"ABSTRACT:\n{abstract}\n\n"
            f"FULL TEXT EXCERPTS:\n{chunk_section}\n\n"
            "Extract entities, relationships, topics, and citations as instructed. "
            "Return ONLY the JSON object."
        )

    def _call_bedrock(self, user_prompt: str) -> str:
        """
        Invoke the Bedrock Claude model and return the raw response text.

        Uses the ``converse`` API (unified across model families) with a
        system prompt that locks Claude into JSON-only output mode.

        Parameters
        ----------
        user_prompt : str
            The assembled user-turn content.

        Returns
        -------
        str
            The model's text output (expected to be a JSON string).

        Raises
        ------
        RuntimeError
            Wraps any Bedrock / botocore exception with a descriptive message.
        """
        try:
            response = self._bedrock.converse(
                modelId=self._model_id,
                system=[{"text": ENTITY_EXTRACTION_PROMPT}],
                messages=[
                    {
                        "role":    "user",
                        "content": [{"text": user_prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens":   2048,
                    "temperature": 0.0,   # deterministic extraction
                },
            )
        except Exception as exc:
            raise RuntimeError(
                f"Bedrock converse call failed: {exc}"
            ) from exc

        # converse response structure:
        # response["output"]["message"]["content"][0]["text"]
        try:
            return response["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected Bedrock response structure: {exc}\n"
                f"Response: {json.dumps(response, default=str)}"
            ) from exc

    def _parse_claude_response(self, response_text: str) -> dict[str, Any]:
        """
        Parse Claude's response text into a normalised extraction dict.

        Claude should return a raw JSON object, but may occasionally wrap
        it in markdown code fences.  This method strips fences, attempts
        JSON parsing, and falls back to an empty skeleton on failure.

        Parameters
        ----------
        response_text : str
            Raw text output from Claude.

        Returns
        -------
        dict
            Parsed extraction dict with keys ``entities``, ``relationships``,
            ``topics``, and ``citations``.  Values default to empty lists
            when a key is missing or the parse fails entirely.
        """
        _EMPTY: dict[str, Any] = {
            "entities":      [],
            "relationships": [],
            "topics":        [],
            "citations":     [],
        }

        if not response_text or not response_text.strip():
            logger.warning("Claude returned an empty response")
            return _EMPTY

        # Strip optional markdown code fences (```json ... ``` or ``` ... ```)
        cleaned = re.sub(r"^```(?:json)?\s*", "", response_text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            # Attempt to extract the first JSON object from the text
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                except json.JSONDecodeError as exc:
                    logger.error(
                        "JSON parse failed even after regex extraction: %s\n"
                        "Raw response (first 500 chars): %.500s",
                        exc,
                        response_text,
                    )
                    return _EMPTY
            else:
                logger.error(
                    "No JSON object found in Claude response. "
                    "Raw response (first 500 chars): %.500s",
                    response_text,
                )
                return _EMPTY

        # Normalise — ensure all expected keys are present and are lists
        return {
            "entities":      list(parsed.get("entities",      [])),
            "relationships": list(parsed.get("relationships", [])),
            "topics":        list(parsed.get("topics",        [])),
            "citations":     list(parsed.get("citations",     [])),
        }
