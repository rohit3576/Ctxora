# Vector Search Tuning Guide

Date: 2026-08-24 · Status: reference (nothing implemented here yet — this doc describes available options and when to apply them)

Applies to: pgvector surfaces — RAG document chunks, knowledge semantic examples, promoted feedback examples.

---

## Current state (as of this writing)

| Surface | Vector column | Index | Search query |
|---|---|---|---|
| `rag_chunks` | `VECTOR(1536)` | **HNSW** cosine (`idx_rag_chunks_hnsw`) | `rag/store.py` — `ORDER BY embedding <=> q LIMIT k` |
| `knowledge.semantic_examples` | `VECTOR(1536)` | none (btree only) | `knowledge/store.py` — same shape, seq scan |
| Promoted feedback examples | `VECTOR(1536)` | none | `feedback/store.py` |

- Embedding model: `text-embedding-3-small` (default in `config/settings.py`, overridable via `EMBEDDING_MODEL`). Any swap must produce exactly **1536-dim** vectors — the column type is fixed (`docs/phases/PHASE_5.md`).
- `hnsw.ef_search` is **never set** anywhere — we run pgvector's default of 40.
- At demo/small scale the planner picks a sequential scan for these queries, so none of the tuning below has measurable effect until the corpus grows (thousands+ of vectors per tenant).

Two tuning levers exist. Both are optional; neither is implemented yet.

---

## Option 1 — Vector storage format (quantization)

Controls the memory footprint and speed of stored vectors. Chosen once at setup/deployment time — **it is a config + schema decision, not a per-request toggle** (one format per column; rows cannot mix formats).

| Configuration | Accuracy | Memory (per 1536-d vector) | Speed |
|---|---|---|---|
| `VECTOR(1536)` — today, no quantization | 🟢 baseline | 🔴 6 KB | 🟢 good |
| `HALFVEC(1536)` — half precision | 🟡 near-negligible loss | 🟢 3 KB | 🟢 faster index build + search |
| `BIT(1536)` — binary quantization | 🔴 real recall drop alone | 🟢 192 B (~32× less) | 🟢 fastest, **requires re-rank** |

### Constraints if implemented

1. **All three vector tables are affected** — `rag_chunks`, `knowledge.semantic_examples`, promoted feedback. One config flag (e.g. `rag.vector_storage: float32 | half | binary`) would drive all three DDLs and search paths.
2. **No re-embedding needed** — halfvec and bit are derivable from existing float vectors (cast / sign-bits backfill). Dims stay 1536.
3. **Binary needs a re-rank step** in search code: oversample candidates (e.g. 10× k) via the bit index, then re-score exactly with the original floats and return top-k. This hybrid (quantized recall + exact re-rank) is the standard pattern; without it, recall suffers.
4. **Halfvec needs nothing extra** — direct drop-in, no re-rank.
5. Provenance: the per-row `embedding_model` column pattern would get a sibling (e.g. `vector_format`) so rows stay interpretable across a format switch.

### Recommendation

Default path `float32 → half` is the sweet spot (near-lossless, half memory, faster). Offer `binary` only as an explicit opt-in for large corpora, and only together with the re-rank path.

---

## Option 2 — Raise `hnsw.ef_search` (100–200)

Controls the recall/latency trade-off of HNSW index traversal. Session-level Postgres GUC; pgvector default is 40.

- Effort: trivial (<1 hr). No schema change, no migration, instantly reversible.
- Payoff: up to ~+30% recall — **only when the HNSW index is actually used**, i.e. large corpora where the planner picks an index scan over a seq scan.

### Mechanics

`ef_search` applies per connection/session. With the psycopg pool, set it per transaction inside the search path:

```sql
BEGIN;
SET LOCAL hnsw.ef_search = 100;
SELECT ... ORDER BY embedding <=> q LIMIT k;
COMMIT;
```

(`SET LOCAL` scopes the setting to the transaction — safe with pooled connections; a connection-init hook is the alternative.)

### When to actually do this

1. Corpus grows (thousands+ vectors per tenant).
2. Run `EXPLAIN (ANALYZE)` on the search query — confirm it uses `idx_rag_chunks_hnsw` (not a seq scan). If seq scan: ef_search is irrelevant; growth or the missing indexes below are the problem.
3. Set `ef_search = 100`, A/B recall@k against 40, step to 200 if recall still lags and latency budget allows.

### Prerequisite gaps

- `knowledge.semantic_examples` and promoted-feedback searches have **no vector index at all** — before ef_search matters for them they need their own `USING hnsw (embedding vector_cosine_ops)` indexes (that change also needs a tenant-scoped search pattern that the planner can index-scan).

### Risk

None at any scale: worst case is slightly higher per-search latency. At small scale it is simply inert.

---

## Decision summary

| Lever | Do it when | Cost | Expected gain |
|---|---|---|---|
| `ef_search` 100–200 | corpus is large AND index scan confirmed | <1 hr, reversible | up to ~+30% recall (index-scan regime) |
| `float32 → half` | memory pressure or big corpus | schema migration + backfill cast | ~50% vector memory reduction, faster search, negligible loss |
| `binary` + re-rank | very large corpus, latency-bound | migration + re-rank code path | ~32× memory reduction, fastest search |

None of these are scheduled yet — revisit when real corpus sizes make the seq-scan regime obsolete.
