# RAG Retrieval Baseline (R1) — LIVE

Measured 2026-08-26 with `tools/rag_eval --live` against seeded `demo/docs`
(4 manuals, 27 chunks), Gemini `gemini-embedding-001` @1536 dims,
PostgreSQL + pgvector. Determinism verified: two consecutive runs produced
byte-identical reports (md5 `e8a738…0ae7`).

## v1 baseline (`query_rewrite: false` — pre-R2 behavior)

Command: `uv run python -m tools.rag_eval --live`
(with `rag.query_rewrite` temporarily false; CSV: `rag_baseline_r1_v1.csv`)

| tag | cases | MRR | R@1 | R@3 | R@5 | R@10 |
| --- | --- | --- | --- | --- | --- | --- |
| overall | 36 | 0.82 | 0.75 | 0.94 | 0.94 | 0.97 |
| followup | 3 | 0.44 | 0.33 | 0.67 | 0.67 | 0.67 |
| identifier | 10 | 0.82 | 0.70 | 1.00 | 1.00 | 1.00 |
| multi-hop | 3 | 0.78 | 0.67 | 1.00 | 1.00 | 1.00 |
| paraphrase | 8 | 0.90 | 0.88 | 0.88 | 0.88 | 1.00 |
| procedure | 6 | 0.89 | 0.83 | 1.00 | 1.00 | 1.00 |
| table | 6 | 0.89 | 0.83 | 1.00 | 1.00 | 1.00 |

## R2 on (`query_rewrite: true` — shipping v1.1 behavior)

Command: `uv run python -m tools.rag_eval --live --seed`
(report: `rag_baseline_r1_live.md`; CSV: `rag_baseline_r1.csv`)

| tag | cases | MRR | R@1 | R@3 | R@5 | R@10 |
| --- | --- | --- | --- | --- | --- | --- |
| overall | 36 | 0.85 | 0.78 | 0.97 | 0.97 | 1.00 |
| followup | 3 | 0.78 | 0.67 | 1.00 | 1.00 | 1.00 |
| identifier | 10 | 0.82 | 0.70 | 1.00 | 1.00 | 1.00 |
| multi-hop | 3 | 0.78 | 0.67 | 1.00 | 1.00 | 1.00 |
| paraphrase | 8 | 0.90 | 0.88 | 0.88 | 0.88 | 1.00 |
| procedure | 6 | 0.89 | 0.83 | 1.00 | 1.00 | 1.00 |
| table | 6 | 0.89 | 0.83 | 1.00 | 1.00 | 1.00 |

## Reading the numbers

- **R2 acceptance MET**: followup R@5 0.67 → 1.00 (target was ≥ 0.97), MRR
  0.44 → 0.78, with **zero regression on every other tag** and overall R@5
  0.94 → 0.97. The rewrite does exactly what the plan promised.
- **Identifier R@5 = 1.00 on this corpus** — Gemini's embeddings are strong on
  code-shaped tokens (`E-302`, `TS-004`, `A-104`) for short manuals. The
  plan's 0.72→0.91 hybrid expectation was calibrated on generic dense models;
  R3 (hybrid + RRF) may still add robustness at corpus scale, but on THIS
  golden set there is little headroom. R3's justification should be
  re-evaluated against R4's harder corpus (longer manuals, more versions)
  rather than this one.
- **Paraphrase 0.88 is the weakest tag** — one case misses at R@5 both with
  and without rewrite: genuinely fuzzy phrasing. Likely the best R3 candidate
  on this set.
- **Table 1.00** — short manuals keep tables inside single sections, so the
  R4 split-table failure mode is not yet exercised. The corpus needs longer
  manuals before R4 work to make `table` meaningful.

## What each phase must beat (updated targets)

| Metric | v1 baseline | R2 (shipped) | R3/R4 target |
| --- | --- | --- | --- |
| Recall@5 — overall | 0.94 | 0.97 | ≥ 0.97, no tag regression |
| Recall@5 — followup | 0.67 | **1.00** ✅ R2 done | hold |
| Recall@5 — paraphrase | 0.88 | 0.88 | R3's remaining headroom |
| Recall@5 — identifier | 1.00 | 1.00 | hold (no headroom on this corpus) |
| Recall@5 — table | 1.00 | 1.00 | re-baseline after R4 corpus expansion |

Fake-mode smoke numbers (hash embeddings, offline CI shape check) remain in
git history at this path's previous revision.
