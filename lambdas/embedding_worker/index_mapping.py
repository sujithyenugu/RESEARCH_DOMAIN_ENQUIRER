"""
index_mapping.py — embedding_worker Lambda

OpenSearch index mapping and settings for the ``paper_chunks`` index.

Index design decisions
----------------------

knn_vector field (``embedding``)
    * Dimensionality: 1 536 (Amazon Titan Embed Text v2 output size).
    * Engine: ``nmslib`` HNSW via the OpenSearch k-NN plugin.
    * ``ef_construction=512`` — higher value means more accurate graph
      construction at index time at the cost of slower ingestion.
    * ``m=16`` — number of bi-directional links per node; 16 is the
      recommended default for dense retrieval at ~1 M document scale.
    * Space type: ``cosinesimil`` — cosine similarity, which is standard for
      normalised text embeddings.

BM25 full-text field (``text``)
    * Uses the built-in ``english`` analyser (stop words + Porter stemming)
      so lexical queries like BM25 hybrid retrieval work correctly.

Keyword fields
    * ``chunk_id``, ``paper_id``, ``section_id``, ``section_title``,
      ``authors``, ``categories``, ``entities``, ``concepts`` — all stored
      as ``keyword`` for exact-match filters and aggregations.

Numeric fields
    * ``chunk_index``, ``page``, ``char_count`` — ``integer``.
    * ``token_start``, ``token_end`` — ``integer``.

Date field
    * ``published_date`` — ``date`` (ISO-8601 format, e.g.
      ``"2024-03-15T00:00:00Z"``).

Index settings
--------------
* 6 primary shards — tuned for an expected corpus of ~10 M chunks across
  multiple arXiv categories; each shard stays well under 50 GB.
* 1 replica — one replica per primary for availability; increase to 2 for
  production clusters with 3+ data nodes.
* ``refresh_interval: "30s"`` — trade near-real-time visibility for higher
  bulk-indexing throughput.
* ``knn: true`` — enables the OpenSearch k-NN plugin for this index.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------

PAPER_CHUNKS_MAPPING: dict = {
    "properties": {
        # ------------------------------------------------------------------ #
        # Vector field — HNSW cosine similarity, 1 536 dims (Titan Embed v2) #
        # ------------------------------------------------------------------ #
        "embedding": {
            "type": "knn_vector",
            "dimension": 1536,
            "method": {
                "name":       "hnsw",
                "engine":     "nmslib",
                "space_type": "cosinesimil",
                "parameters": {
                    "ef_construction": 512,
                    "m":               16,
                },
            },
        },
        # ------------------------------------------------------------------ #
        # Full-text field — English analyser for BM25 lexical retrieval      #
        # ------------------------------------------------------------------ #
        "text": {
            "type":     "text",
            "analyzer": "english",
        },
        # ------------------------------------------------------------------ #
        # Identity / reference keyword fields                                 #
        # ------------------------------------------------------------------ #
        "chunk_id": {
            "type": "keyword",
        },
        "paper_id": {
            "type": "keyword",
        },
        "section_id": {
            "type": "keyword",
        },
        "section_title": {
            "type": "keyword",
        },
        # ------------------------------------------------------------------ #
        # Numeric fields                                                       #
        # ------------------------------------------------------------------ #
        "chunk_index": {
            "type": "integer",
        },
        "page": {
            "type": "integer",
        },
        "char_count": {
            "type": "integer",
        },
        "token_start": {
            "type": "integer",
        },
        "token_end": {
            "type": "integer",
        },
        # ------------------------------------------------------------------ #
        # Date field                                                           #
        # ------------------------------------------------------------------ #
        "published_date": {
            "type":   "date",
            "format": "strict_date_optional_time||epoch_millis",
        },
        # ------------------------------------------------------------------ #
        # Multi-value keyword fields (stored as JSON arrays)                  #
        # ------------------------------------------------------------------ #
        "authors": {
            "type": "keyword",
        },
        "categories": {
            "type": "keyword",
        },
        "entities": {
            "type": "keyword",
        },
        "concepts": {
            "type": "keyword",
        },
    }
}


# ---------------------------------------------------------------------------
# Index settings
# ---------------------------------------------------------------------------

INDEX_SETTINGS: dict = {
    "index": {
        # Number of primary shards.  For ~10 M chunks at ~2 KB each the
        # total index size is ~20 GB; 6 shards keeps each shard ~3.3 GB,
        # well within the recommended 10–50 GB per shard limit.
        "number_of_shards":   6,

        # One replica per primary for basic availability.
        "number_of_replicas": 1,

        # Refresh interval — 30 s gives much higher bulk-indexing throughput
        # than the default 1 s at the cost of a slight search staleness window.
        "refresh_interval": "30s",

        # Enable the OpenSearch k-NN plugin for this index so that
        # knn_vector fields are indexed by the HNSW graph builder.
        "knn": True,
    }
}
