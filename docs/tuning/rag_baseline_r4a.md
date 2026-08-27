# RAG Retrieval Baseline (R4a corpus) — LIVE

Measured 2026-08-27 with `tools/rag_eval --live --seed` against the expanded
`demo/docs` corpus (6 manuals, 47 chunks — R4a added the GT-800 family:
~2800-word manuals whose Telemetry Channel Map and Installation Procedure
exceed the 800-word chunk window). Gemini `gemini-embedding-001` @1536 dims,
PostgreSQL + pgvector (port 5433). Determinism verified: two consecutive runs
produced byte-identical CSVs (md5 `bce70e…4957`).

Command: `uv run python -m tools.rag_eval --live --seed`
(raw report: `rag_baseline_r4a_live.md`; CSV: `rag_baseline_r4a.csv`)

| tag | cases | MRR | R@1 | R@3 | R@5 | R@10 |
| --- | --- | --- | --- | --- | --- | --- |
| overall | 43 | 0.74 | 0.65 | 0.88 | 0.91 | 0.93 |
| followup | 3 | 0.78 | 0.67 | 1.00 | 1.00 | 1.00 |
| identifier | 10 | 0.82 | 0.70 | 1.00 | 1.00 | 1.00 |
| multi-hop | 3 | 0.78 | 0.67 | 1.00 | 1.00 | 1.00 |
| paraphrase | 8 | 0.90 | 0.88 | 0.88 | 0.88 | 1.00 |
| procedure | 7 | 0.76 | 0.71 | 0.86 | 0.86 | 0.86 |
| table | 8 | 0.67 | 0.62 | 0.75 | 0.75 | 0.75 |
| version | 4 | 0.31 | 0.00 | 0.75 | 1.00 | 1.00 |

## Where the drops come from (per-case evidence)

The original 36 cases are **rank-identical** to the R1 live baseline — the
entire overall drop (0.97 → 0.91 R@5) is the 7 new R4a cases failing as
designed. Both engineered failure modes now show live:

- **Split-content misses (3 cases, rank ∅ at any k)** — `gt800-tank-note-interval`
  (CH-48 → 900 s), `gt800-lvd-threshold` (CH-92 → 11.8 V),
  `gt800-gland-torque` (PG-9 → 2.5 Nm). The key sits in one chunk, the answer
  >120 words away in another; no chunk satisfies the `contains` expectation.
  This is the chunker-v2 (R4d) before-picture: table R@5 1.00 → 0.75,
  procedure 1.00 → 0.86.
- **Version pinning never wins rank 1 (4 cases, MRR 0.31, R@1 0.00)** — every
  version case hits at R@5 but at ranks 3/3/4/3: both revisions stay ACTIVE
  and near-tie, and the obsolete sibling outranks or ties the pinned revision
  (`ds200-v2-torque` is the sharpest example — fake mode ranked it 1, live
  embeddings put v1's 45 Nm chunk above v2's 50 Nm). This is the version
  lifecycle (R4e) before-picture.

Held exactly vs R1 live: followup 1.00 (R2 rewrite intact), identifier 1.00,
paraphrase 0.88, multi-hop 1.00. GT-800 distractors caused **zero regression**
on the original corpus.

## What R4 must beat (acceptance targets for 4g)

| Metric | R4a baseline (this doc) | R4g target | Fixed by |
| --- | --- | --- | --- |
| Recall@5 — table | 0.75 | 1.00 (split cases hit) | 4d chunker v2 |
| Recall@5 — procedure | 0.86 | 1.00 (split case hits) | 4d chunker v2 |
| Recall@1 — version | 0.00 | 1.00 (newest revision first) | 4e lifecycle |
| MRR — version | 0.31 | ≥ 0.75 | 4e lifecycle |
| Recall@5 — overall | 0.91 | ≥ 0.97 | 4d + 4e |
| All other tags | followup 1.00, identifier 1.00, paraphrase 0.88, multi-hop 1.00 | no regression | — |

Split-case acceptance is defined by `contains` expectations (the full row —
key and answer — must land in one retrieved parent chunk), not by section
title match: the assertion lives in `tests/golden/rag_golden.yaml`.
