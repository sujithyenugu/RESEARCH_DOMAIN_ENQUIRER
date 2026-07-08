# Annotation Guidelines — Research Domain Enquirer Golden Dataset

> **Version:** v1  
> **Last updated:** 2024-01-10  
> **Maintained by:** Research Domain Enquirer Evaluation Team

---

## Purpose

This document describes the standards and procedures for creating, reviewing, and maintaining the golden Q&A dataset used to evaluate the Research Domain Enquirer RAG pipeline.

The golden dataset is the ground truth for **offline evaluation**. Every question-answer pair must meet the quality bar defined here before inclusion.

---

## Dataset Schema

Each entry in `golden_qa_v1.json` conforms to this schema:

```json
{
  "eval_id":               "eval_NNN",
  "question":              "Precise research question (string)",
  "difficulty":            "easy | medium | hard",
  "category":              "<one of 7 categories below>",
  "ground_truth_answer":   "Complete, accurate answer (string)",
  "relevant_paper_ids":    ["YYYY.NNNNN", ...],
  "relevant_chunk_ids":    ["YYYY.NNNNN_section_chunkN", ...],
  "ground_truth_citations": ["[YYYY.NNNNN]", ...],
  "expected_entities":     ["entity1", "entity2", ...],
  "annotated_by":          "human | gpt4-assisted | gpt4-reviewed",
  "created_at":            "YYYY-MM-DD"
}
```

---

## Categories

| Category | Target Count | Description |
|----------|-------------|-------------|
| `method_comparison` | 25 | Compare two or more ML methods, architectures, or algorithms |
| `model_evaluation` | 20 | Specific metric/benchmark scores for named models |
| `concept_explanation` | 15 | Explain a technical concept (what is X, how does X work) |
| `historical_progression` | 10 | How a field/technique evolved over time |
| `dataset_description` | 10 | What is a dataset / how is it used / what does it measure |
| `author_attribution` | 5 | Who introduced concept X / in which paper |
| `multi_hop_reasoning` | 15 | Requires synthesising information from 2+ papers |

---

## Difficulty Levels

| Level | Criteria |
|-------|----------|
| **easy** | Answer is found directly in a single paper abstract or intro. No inference required. |
| **medium** | Answer requires reading the methodology or results section. Some comparison or synthesis needed. |
| **hard** | Answer requires deep reading of 2+ sections, mathematical understanding, or synthesising across multiple papers. |

---

## Annotation Process

### Step 1 — Question Selection

Write questions that:
- Have **objective, verifiable answers** — avoid opinion questions
- Are **specific** — not "What is a transformer?" but "How does the Transformer attention mechanism scale with sequence length?"
- Are **resolvable from arXiv papers** — the answer must exist in an indexed paper
- Cover a **diverse set of topics** across ML, NLP, CV, RL

Avoid:
- Questions whose answers change over time (e.g., "What is the current state of the art on X?")
- Questions requiring private or proprietary knowledge
- Questions with no single clear answer

### Step 2 — Answer Writing

Ground truth answers must:
- Be **complete**: cover all key aspects of the question
- Be **accurate**: cite specific numbers, model names, paper titles
- Be **concise**: 2–5 sentences maximum for easy/medium; up to 8 sentences for hard
- Include **citations** using `[YYYY.NNNNN]` notation for every factual claim
- **Not** include hedging language like "approximately" unless genuinely uncertain

### Step 3 — Paper and Chunk Identification

For each answer:
1. Identify the **primary paper(s)** that contain the information
2. Identify the **specific chunk IDs** that contain the relevant text
3. Chunk ID format: `{paper_id}_{section}_{chunk_index}`
   - Example: `2106.09685_sec3_chunk1` = paper 2106.09685, section 3, chunk 1
   - Common sections: `abstract`, `sec1`, `sec2`, `sec3`, `sec4`, `sec5`, `table1`, `fig1`

### Step 4 — Entity Annotation

List 3–6 expected entities that the answer generator should mention:
- Model names (LoRA, BERT, GPT-4)
- Metric names (Recall@10, BLEU, MRR)
- Concept names (low-rank adaptation, attention, scaling laws)
- Dataset names (SQuAD, MMLU, WinoGrande)

### Step 5 — Quality Review

Each entry requires review by a second annotator who checks:
- [ ] Answer is factually correct
- [ ] Paper IDs are valid arXiv IDs
- [ ] Chunk IDs follow the naming convention
- [ ] Citations in the answer match `relevant_paper_ids`
- [ ] Difficulty level is appropriate
- [ ] Category assignment is correct

---

## Versioning

| Version | Date | Changes |
|---------|------|---------|
| v1 | 2024-01-10 | Initial 50 Q&A pairs across all 7 categories |
| v2 | Planned | Expand to 100 Q&A pairs, add more multi_hop_reasoning |

---

## Regression Thresholds

After each nightly eval run, the system checks for regressions against the 7-day baseline:

| Metric | Regression Threshold | Severity |
|--------|---------------------|---------|
| `recall_at_10` | drop > 0.05 | warning; drop > 0.10 = critical |
| `faithfulness` | drop > 0.05 | warning; drop > 0.10 = critical |
| `groundedness` | drop > 0.07 | warning; drop > 0.14 = critical |
| `citation_accuracy` | drop > 0.03 | warning; drop > 0.06 = critical |
| `e2e_latency_p95_ms` | increase > 1000ms | warning; > 2000ms = critical |

Regressions trigger an SNS alert to the evaluation team.

---

## S3 Layout

```
s3://research-evaluation/
├── golden_dataset/
│   ├── golden_qa_v1.json           ← 50 Q&A pairs (this file's data)
│   ├── golden_qa_v2.json           ← 100 Q&A pairs (planned)
│   └── annotation_guidelines.md   ← This document
├── results/
│   ├── eval_YYYY-MM-DD.json        ← Per-run full results
│   └── ...
└── reports/
    └── weekly_eval_report.html     ← Auto-generated weekly summary
```

---

*See [EVALUATION_PIPELINE.md](../../EVALUATION_PIPELINE.md) for the full metric definitions and pipeline architecture.*
