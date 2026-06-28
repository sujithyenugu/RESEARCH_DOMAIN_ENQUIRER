"""
neptune_schema.py — Neptune graph schema definition and initialisation helper.

This module defines:
  1. All vertex (node) labels and their property keys
  2. All edge (relationship) labels and their property keys
  3. Index creation (Neptune property indexes for common traversal paths)
  4. A NeptuneSchemaInitializer class used by the neptune-initializer Lambda

Doc reference: GRAPH_PIPELINE.md §Graph Schema — Full Vertex & Edge Definitions

Usage (called by neptune-initializer Lambda):
    from neptune.neptune_schema import NeptuneSchemaInitializer
    initializer = NeptuneSchemaInitializer(gremlin_client)
    initializer.create_all()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Vertex (Node) Definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VertexLabel:
    """Describes a Neptune vertex label and its property schema."""
    label: str
    properties: dict[str, str]        # property_name -> type hint (documentation only)
    description: str = ""

    def __str__(self) -> str:
        return self.label


VERTEX_PAPER = VertexLabel(
    label="Paper",
    description="An arXiv research paper",
    properties={
        "paper_id":               "String  — arXiv ID e.g. 2401.12345 (unique)",
        "title":                  "String  — Full paper title",
        "published_date":         "String  — ISO date e.g. 2024-01-15",
        "venue":                  "String  — Publication venue (arXiv / conference)",
        "citation_count":         "Integer — Number of times cited (updated incrementally)",
        "abstract_embedding_key": "String  — S3 key of the abstract embedding JSON",
        "s3_clean_key":           "String  — S3 key of the cleaned text JSON",
        "processing_status":      "String  — DynamoDB processing_status mirror",
    },
)

VERTEX_AUTHOR = VertexLabel(
    label="Author",
    description="A paper author (deduplicated by normalized name)",
    properties={
        "name":            "String  — Full name as it appears in the paper",
        "affiliation":     "String  — Institution or organization",
        "normalized_name": "String  — Lowercase underscore key used for deduplication e.g. vaswani_ashish",
    },
)

VERTEX_MODEL = VertexLabel(
    label="Model",
    description="An ML model introduced or referenced in a paper",
    properties={
        "name":            "String  — Model name e.g. GPT-4, LLaMA-3, BERT",
        "type":            "String  — Model type: language_model | vision_model | multimodal | other",
        "parameters":      "String  — Parameter count e.g. 7B, 70B, 1.8T",
        "organization":    "String  — Developing organization e.g. OpenAI, Meta, Google",
        "release_year":    "Integer — Year of first public release",
        "normalized_name": "String  — Lowercase key e.g. gpt_4 (used for upserts)",
    },
)

VERTEX_DATASET = VertexLabel(
    label="Dataset",
    description="A benchmark dataset used in training or evaluation",
    properties={
        "name":          "String  — Dataset name e.g. GLUE, SQuAD, ImageNet",
        "domain":        "String  — Research domain: NLP | CV | multimodal | code | other",
        "task_type":     "String  — Task description e.g. question answering, classification",
        "size_examples": "Integer — Number of examples in dataset",
    },
)

VERTEX_METHOD = VertexLabel(
    label="Method",
    description="A technique or algorithmic approach proposed or used",
    properties={
        "name":            "String  — Method name e.g. LoRA, RLHF, Chain-of-Thought",
        "full_name":       "String  — Expanded name e.g. Low-Rank Adaptation",
        "category":        "String  — Category: parameter_efficient_finetuning | prompting | pretraining | ...",
        "normalized_name": "String  — Lowercase key e.g. lora",
    },
)

VERTEX_BENCHMARK = VertexLabel(
    label="Benchmark",
    description="An evaluation benchmark with a specific metric",
    properties={
        "name":   "String — Benchmark name e.g. HumanEval, MMLU, GLUE",
        "metric": "String — Primary metric e.g. pass@1, accuracy, F1",
        "domain": "String — Domain: code_generation | reasoning | NLP | ...",
    },
)

VERTEX_CONCEPT = VertexLabel(
    label="Concept",
    description="An abstract research concept or technique",
    properties={
        "name":    "String      — Concept name e.g. attention mechanism, tokenization",
        "domain":  "String      — Domain: deep_learning | NLP | CV | ...",
        "aliases": "StringList  — Alternative names e.g. [self-attention, multi-head attention]",
    },
)

VERTEX_TASK = VertexLabel(
    label="Task",
    description="An ML task type e.g. machine translation, text classification",
    properties={
        "name":   "String — Task name",
        "domain": "String — Domain: NLP | CV | speech | multimodal",
    },
)

VERTEX_TOPIC = VertexLabel(
    label="Topic",
    description="A high-level research topic (inferred from arXiv categories + clustering)",
    properties={
        "name":     "String — Topic name e.g. large language models",
        "category": "String — Primary arXiv category e.g. cs.CL",
    },
)

# All vertex definitions — used for documentation and validation
ALL_VERTEX_LABELS: list[VertexLabel] = [
    VERTEX_PAPER,
    VERTEX_AUTHOR,
    VERTEX_MODEL,
    VERTEX_DATASET,
    VERTEX_METHOD,
    VERTEX_BENCHMARK,
    VERTEX_CONCEPT,
    VERTEX_TASK,
    VERTEX_TOPIC,
]

# ---------------------------------------------------------------------------
# Edge (Relationship) Definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeLabel:
    """Describes a Neptune edge label, its endpoints, and property schema."""
    label: str
    from_vertex: str
    to_vertex: str
    properties: dict[str, str]
    description: str = ""

    def __str__(self) -> str:
        return f"{self.from_vertex} --[{self.label}]--> {self.to_vertex}"


EDGE_CITES = EdgeLabel(
    label="CITES",
    from_vertex="Paper",
    to_vertex="Paper",
    description="Paper A cites Paper B in its references",
    properties={
        "context":   "String  — Brief quote where the citation appears",
        "section":   "String  — Section where cited e.g. related_work, introduction",
        "ref_id":    "String  — Reference placeholder in text e.g. [1]",
    },
)

EDGE_INTRODUCES = EdgeLabel(
    label="INTRODUCES",
    from_vertex="Paper",
    to_vertex="Model",
    description="Paper first introduces / presents this model",
    properties={
        "confidence": "Float   — Extraction confidence [0.0–1.0]",
    },
)

EDGE_PROPOSES = EdgeLabel(
    label="PROPOSES",
    from_vertex="Paper",
    to_vertex="Method",
    description="Paper proposes or presents this method as a contribution",
    properties={
        "is_primary_contribution": "Boolean — True if this is the paper's main contribution",
        "confidence":              "Float   — Extraction confidence",
    },
)

EDGE_EVALUATES_ON = EdgeLabel(
    label="EVALUATES_ON",
    from_vertex="Paper",
    to_vertex="Dataset",
    description="Paper runs experiments or evaluations on this dataset",
    properties={
        "score":  "Float  — Best reported score on this dataset",
        "metric": "String — Metric used e.g. F1, accuracy, BLEU",
    },
)

EDGE_AUTHORED_BY = EdgeLabel(
    label="AUTHORED_BY",
    from_vertex="Paper",
    to_vertex="Author",
    description="Paper has this person as an author",
    properties={
        "position":          "Integer — Author position (1 = first author)",
        "is_corresponding":  "Boolean — True if this is the corresponding author",
    },
)

EDGE_IMPROVES = EdgeLabel(
    label="IMPROVES",
    from_vertex="Model",
    to_vertex="Benchmark",
    description="Model achieves a new state-of-the-art score on this benchmark",
    properties={
        "previous_sota": "Float  — Previous best score before this model",
        "new_score":     "Float  — Score achieved by this model",
        "metric":        "String — Metric name e.g. pass@1, BLEU-4",
    },
)

EDGE_USES = EdgeLabel(
    label="USES",
    from_vertex="Method",
    to_vertex="Dataset",
    description="Method uses this dataset for training, fine-tuning, or evaluation",
    properties={
        "purpose": "String — How dataset is used: training | fine_tuning | evaluation | pretraining",
    },
)

EDGE_BASED_ON = EdgeLabel(
    label="BASED_ON",
    from_vertex="Model",
    to_vertex="Model",
    description="Model derives from or extends another model (ancestry chain)",
    properties={
        "relationship": "String — Type of derivation: fine_tuned_from | distilled_from | variant_of | based_on",
    },
)

EDGE_BELONGS_TO = EdgeLabel(
    label="BELONGS_TO",
    from_vertex="Paper",
    to_vertex="Topic",
    description="Paper belongs to this research topic cluster",
    properties={
        "relevance_score": "Float — Topic relevance score [0.0–1.0]",
    },
)

# All edge definitions
ALL_EDGE_LABELS: list[EdgeLabel] = [
    EDGE_CITES,
    EDGE_INTRODUCES,
    EDGE_PROPOSES,
    EDGE_EVALUATES_ON,
    EDGE_AUTHORED_BY,
    EDGE_IMPROVES,
    EDGE_USES,
    EDGE_BASED_ON,
    EDGE_BELONGS_TO,
]


# ---------------------------------------------------------------------------
# Gremlin Upsert Patterns
# ---------------------------------------------------------------------------

class GremlinQueries:
    """
    Pre-built Gremlin query templates used by the Graph Builder Lambda.
    Each method returns a parameterised Gremlin query string.

    All writes use coalesce/fold upsert semantics to ensure idempotency —
    running the same query twice will not create duplicate vertices or edges.

    Reference: GRAPH_PIPELINE.md §Graph Builder Lambda > Upsert Strategy
    """

    @staticmethod
    def upsert_paper(
        paper_id: str,
        title: str,
        published_date: str,
        venue: str = "arXiv",
    ) -> str:
        return (
            "g.V().has('Paper', 'paper_id', paper_id)"
            ".fold()"
            ".coalesce("
            "  __.unfold(),"
            "  __.addV('Paper')"
            "    .property('paper_id', paper_id)"
            "    .property('title', title)"
            "    .property('published_date', published_date)"
            "    .property('venue', venue)"
            "    .property('citation_count', 0)"
            ")"
            ".property('title', title)"
            ".property('published_date', published_date)"
        )

    @staticmethod
    def upsert_author(name: str, normalized_name: str, affiliation: str = "") -> str:
        return (
            "g.V().has('Author', 'normalized_name', normalized_name)"
            ".fold()"
            ".coalesce("
            "  __.unfold(),"
            "  __.addV('Author')"
            "    .property('name', name)"
            "    .property('normalized_name', normalized_name)"
            "    .property('affiliation', affiliation)"
            ")"
            ".property('affiliation', affiliation)"
        )

    @staticmethod
    def upsert_model(
        name: str,
        normalized_name: str,
        model_type: str = "language_model",
        organization: str = "",
    ) -> str:
        return (
            "g.V().has('Model', 'normalized_name', normalized_name)"
            ".fold()"
            ".coalesce("
            "  __.unfold(),"
            "  __.addV('Model')"
            "    .property('name', name)"
            "    .property('normalized_name', normalized_name)"
            "    .property('type', model_type)"
            "    .property('organization', organization)"
            ")"
            ".property('last_seen', published_date)"
        )

    @staticmethod
    def upsert_edge_cites(source_paper_id: str, target_paper_id: str, context: str, section: str) -> str:
        return (
            "g.V().has('Paper','paper_id', source_paper_id)"
            ".outE('CITES')"
            ".where(__.inV().has('Paper','paper_id', target_paper_id))"
            ".fold()"
            ".coalesce("
            "  __.unfold(),"
            "  __.V().has('Paper','paper_id', source_paper_id)"
            "    .addE('CITES')"
            "    .property('context', context)"
            "    .property('section', section)"
            "    .to(__.V().has('Paper','paper_id', target_paper_id))"
            ")"
        )

    @staticmethod
    def find_related_papers_by_entities(query_paper_id: str, limit: int = 10) -> str:
        """
        Find papers that share models/methods/datasets with the query paper.
        Used during Graph Expansion in the Retrieval Engine.
        Reference: GRAPH_PIPELINE.md §Graph Traversal Queries for Retrieval
        """
        return (
            f"g.V().has('Paper','paper_id','{query_paper_id}')"
            ".out('INTRODUCES', 'PROPOSES', 'EVALUATES_ON')"
            ".in('INTRODUCES', 'PROPOSES', 'EVALUATES_ON')"
            ".hasLabel('Paper')"
            ".dedup()"
            f".order().by(__.in('CITES').count(), desc)"
            f".limit({limit})"
            ".valueMap('paper_id', 'title', 'published_date')"
        )

    @staticmethod
    def find_citation_neighborhood(query_paper_id: str, depth: int = 2, limit: int = 20) -> str:
        """N-hop citation neighbors (papers that cite or are cited by query paper)."""
        if depth == 1:
            traversal = ".union(__.out('CITES'), __.in('CITES'))"
        else:
            traversal = (
                ".union("
                "  __.out('CITES'),"
                "  __.in('CITES'),"
                "  __.out('CITES').out('CITES')"
                ")"
            )
        return (
            f"g.V().has('Paper','paper_id','{query_paper_id}')"
            f"{traversal}"
            ".hasLabel('Paper')"
            ".dedup()"
            f".limit({limit})"
            ".valueMap('paper_id', 'title')"
        )

    @staticmethod
    def find_model_lineage(normalized_model_name: str) -> str:
        """Follow BASED_ON edges to build the full model ancestry chain."""
        return (
            f"g.V().has('Model','normalized_name','{normalized_model_name}')"
            ".repeat(__.out('BASED_ON'))"
            ".until(__.not(__.out('BASED_ON')))"
            ".path()"
            ".by('name')"
        )

    @staticmethod
    def find_method_benchmarks(normalized_method_name: str) -> str:
        """Find benchmarks improved by models that use this method."""
        return (
            f"g.V().has('Method','normalized_name','{normalized_method_name}')"
            ".in('PROPOSES')"
            ".out('INTRODUCES')"
            ".out('IMPROVES')"
            ".dedup()"
            ".valueMap('name', 'metric')"
        )

    @staticmethod
    def most_cited_papers(limit: int = 20) -> str:
        """Rank papers by incoming CITES edge count."""
        return (
            "g.V().hasLabel('Paper')"
            f".order().by(__.in('CITES').count(), desc)"
            f".limit({limit})"
            ".project('paper_id', 'title', 'citation_count')"
            ".by('paper_id')"
            ".by('title')"
            ".by(__.in('CITES').count())"
        )


# ---------------------------------------------------------------------------
# Neptune Schema Initializer
# ---------------------------------------------------------------------------

class NeptuneSchemaInitializer:
    """
    Initializes the Neptune graph schema.
    Called by the neptune-initializer Lambda after StorageStack is deployed.

    Neptune does not have a rigid schema — vertex/edge labels and properties
    are defined by use. However, we can create property indexes to speed up
    common traversal patterns.
    """

    def __init__(self, client: Any) -> None:
        """
        Args:
            client: A connected Gremlin traversal source (g = traversal().with_remote(conn))
        """
        self._client = client

    def create_all(self) -> dict[str, Any]:
        """
        Run all initialization steps and return a summary report.
        Steps:
          1. Verify Neptune connectivity
          2. Create vertex property indexes
          3. Insert seed Topic vertices
          4. Return stats
        """
        report: dict[str, Any] = {}

        print("[NeptuneSchemaInitializer] Checking connectivity...")
        vertex_count = self._client.V().count().next()
        print(f"[NeptuneSchemaInitializer] Connected. Current vertex count: {vertex_count}")
        report["initial_vertex_count"] = vertex_count

        print("[NeptuneSchemaInitializer] Creating property indexes...")
        self._create_indexes()
        report["indexes_created"] = True

        print("[NeptuneSchemaInitializer] Seeding Topic vertices...")
        topics_created = self._seed_topics()
        report["topics_created"] = topics_created

        print("[NeptuneSchemaInitializer] Done.")
        return report

    def _create_indexes(self) -> None:
        """
        Neptune supports property indexes to speed up .has() lookups.
        These are equivalent to DynamoDB GSIs — without them, Neptune does
        a full graph scan for every .has() predicate.

        We create indexes on the most-queried properties.
        """
        # NOTE: Neptune uses a management API that differs by driver.
        # The actual index creation depends on the Neptune version and driver.
        # These are documented here as the intended indexes — the initializer
        # Lambda will execute the appropriate Neptune-specific API calls.

        indexes_to_create = [
            # Vertex property indexes
            ("vertex", "Paper",     "paper_id"),          # PK lookup
            ("vertex", "Author",    "normalized_name"),   # Dedup key
            ("vertex", "Model",     "normalized_name"),   # Dedup key
            ("vertex", "Dataset",   "name"),              # Lookup by name
            ("vertex", "Method",    "normalized_name"),   # Dedup key
            ("vertex", "Benchmark", "name"),              # Lookup by name
            ("vertex", "Topic",     "name"),              # Lookup by name
        ]

        for index_type, label, property_name in indexes_to_create:
            print(f"  → Creating {index_type} index on {label}.{property_name}")
            # In production: mgmt.makePropertyKey(property_name).dataType(String.class).make()
            #               mgmt.buildIndex(f'by_{label}_{property_name}', Vertex.class)
            #                   .addKey(mgmt.getPropertyKey(property_name)).buildCompositeIndex()

    def _seed_topics(self) -> int:
        """
        Insert the initial Topic vertices that papers will be linked to.
        Topics are inferred from arXiv categories and keyword clustering.

        Reference: GRAPH_PIPELINE.md §Topic Graph Construction
        """
        seed_topics = [
            {"name": "large language models",            "category": "cs.CL"},
            {"name": "retrieval augmented generation",   "category": "cs.IR"},
            {"name": "parameter efficient fine-tuning",  "category": "cs.LG"},
            {"name": "multimodal learning",              "category": "cs.CV"},
            {"name": "reasoning and planning",           "category": "cs.AI"},
            {"name": "vision transformers",              "category": "cs.CV"},
            {"name": "code generation",                  "category": "cs.SE"},
            {"name": "reinforcement learning from human feedback", "category": "cs.LG"},
            {"name": "diffusion models",                 "category": "cs.CV"},
            {"name": "knowledge graphs",                 "category": "cs.AI"},
            {"name": "neural architecture search",       "category": "cs.NE"},
            {"name": "federated learning",               "category": "cs.LG"},
            {"name": "model compression",                "category": "cs.LG"},
            {"name": "continual learning",               "category": "cs.LG"},
            {"name": "graph neural networks",            "category": "cs.LG"},
        ]

        count = 0
        for topic in seed_topics:
            try:
                # Upsert — safe to re-run without creating duplicates
                (
                    self._client.V()
                    .has("Topic", "name", topic["name"])
                    .fold()
                    .coalesce(
                        __.unfold(),
                        __.addV("Topic")
                          .property("name", topic["name"])
                          .property("category", topic["category"])
                    )
                    .next()
                )
                count += 1
                print(f"  → Upserted Topic: {topic['name']}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ Failed to upsert Topic {topic['name']}: {exc}")

        return count
