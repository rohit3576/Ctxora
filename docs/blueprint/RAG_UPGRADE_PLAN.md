# RAG Upgrade Plan — Retrieval Quality Program

**Status:** Proposed (not started) · **Track:** RAG · **Prerequisite:** v1.0 (Phase 0–7 complete)

This is the step-by-step implementation plan for upgrading the document-RAG layer from a
dense-only v1 vertical slice to a measured, hybrid, context-aware retrieval system.
Order is deliberate: **measure first**, then cheapest-per-impact, then the schema-heavy
rework batched into one migration.

Grounded in the current code: `rag/store.py`, `rag/chunker.py`, `rag/rag_flow.py`,
`rag/ingest.py`, `rag/contracts.py`, `rag/schema.sql`, `api/rag.py`, `api/query.py`,
`config/settings.py` (`RagConfig`).

---

## Table of Contents

1. [Why This Program Exists](#1-why-this-program-exists)
2. [Guiding Principles](#2-guiding-principles)
3. [Current State (Audited)](#3-current-state-audited)
4. [Phase Overview & Order Rationale](#4-phase-overview--order-rationale)
5. [Phase R1 — Golden Set + Retrieval Eval Harness](#5-phase-r1--golden-set--retrieval-eval-harness)
6. [Phase R2 — Session-Aware Query Rewriting](#6-phase-r2--session-aware-query-rewriting)
7. [Phase R3 — Hybrid BM25 + Dense with RRF Fusion](#7-phase-r3--hybrid-bm25--dense-with-rrf-fusion)
8. [Phase R4 — Structure-Aware Chunking + Metadata & Versioned Retrieval](#8-phase-r4--structure-aware-chunking--metadata--versioned-retrieval)
9. [Cross-Cutting Concerns](#9-cross-cutting-concerns)
10. [Configuration Reference (End State)](#10-configuration-reference-end-state)
11. [Success Metrics](#11-success-metrics)
12. [Timeline & Sequencing](#12-timeline--sequencing)
13. [Out of Scope / Deferred](#13-out-of-scope--deferred)

---

## 1. Why This Program Exists

Five structural failure modes exist in the v1 RAG layer today:

| # | Failure mode | User-visible symptom | Root cause in code |
|---|---|---|---|
| 1 | **Exact-match blindness** | Questions containing identifiers (`E-302`, `TS-001`, `240V`) miss the right chunk — RAG-miss or hallucination around thin context | `PGRagStore.search` is pure pgvector cosine; no lexical path |
| 2 | **Broken procedures / split tables** | Retrieval returns "Step 1–3" while Step 4 is in another chunk; spec tables cut mid-row so the LLM reassembles `voltage` and `240V` wrongly | Within-section splitting is a fixed 800-word window (`_split`); no procedure/table awareness; no parent-child retrieval |
| 3 | **Follow-ups retrieve garbage** | "How do I fix it?" retrieves generic "fixing" chunks, not the door-sensor discussed two turns ago | RAG path retrieves on the raw question; `RAGQueryRequest` carries no `session_id` at all |
| 4 | **Wrong manual / wrong version** | Two device models' manuals blended ("torque to 45–50 Nm"); updated manual uploaded → old one stays ACTIVE → obsolete procedure quoted | Chunks filtered only by tenant/scope; no device/firmware/version metadata; new file hash = new doc, old doc never superseded |
| 5 | **Flying blind on quality** | None of the above can be proven or regression-tested | `tests/golden/` covers SQL parity only; zero retrieval-quality measurement |

Literature numbers for the planned fixes (to be **replaced by measured numbers in R1+**):
hybrid + RRF Recall@5 ≈ 0.72 → 0.91; parent-doc chunking faithfulness +10–20%;
filtered retrieval precision 0.73 → 0.82.

## 2. Guiding Principles

1. **Measure before and after every phase.** R1 harness runs before R2 starts (baseline)
   and after each phase (delta). No phase is "done" without a harness run.
2. **Flag-gated, config-over-code** (project convention): every new behavior gets a
   `RagConfig` flag defaulting to current behavior; rollout is a config flip, not a deploy.
3. **Contracts first**: `RagStore` protocol changes ship with `rag/fake.py` updates in the
   same commit so the fake-testable pattern survives.
4. **One schema migration batch** (R4): parent-child + metadata + versioning land together
   so tenants re-ingest **once**, not three times.
5. **Degrade, never fail**: rewrite failure → raw question; lexical leg failure → dense-only;
   reranker timeout → RRF order. Every new leg is non-blocking (matches `_rag_answer` style).
6. **Vendor-neutral** (project rule): no proprietary service assumptions; BM25 lives in
   Postgres (`tsvector`), rerank goes through the existing `LLMClient` boundary.

## 3. Current State (Audited)

What exists today and works well (do not rebuild):

- Clean ingestion pipeline: `parse → chunk → embed → store` with content-hash dedupe
  (`rag/ingest.py`), upload limits, typed `IngestError`/`UnsupportedFormatError`.
- Section-aware chunk titles (`section_title` preserved through to citations).
- pgvector HNSW cosine index; shared-scope retrieval
  (`tenant OR (other tenant AND scope = shared)`).
- Grounded-answer discipline: `NO_GROUNDED_ANSWER` refusal → typed 404 `NOT_GROUNDED`.
- Advisor flow (`advise()`) reuses `retrieve()`, so retrieval upgrades benefit it for free.

The gaps (this plan):

| Area | Today | File |
|---|---|---|
| Retrieval | Dense-only cosine, fixed `top_k=5` | `rag/store.py::search` |
| Chunking | 800-word window / 120-word overlap inside sections; no tables, no parent-child | `rag/chunker.py` |
| Query understanding | Raw question embedded; no session context; endpoint stateless | `rag/rag_flow.py::retrieve`, `api/rag.py` |
| Metadata / filtering | Only `tenant`, `scope`; `status` exists but nothing ever sets it non-ACTIVE | `rag/schema.sql` |
| Evaluation | None for retrieval | — |
| Schema notes | `VECTOR(1536)` hardcodes one embedding model; citations = `chunks[:5]` regardless of grounding | `rag/schema.sql`, `rag/rag_flow.py` |

## 4. Phase Overview & Order Rationale

| Phase | Scope | Why this position | Effort | Independently shippable |
|---|---|---|---|---|
| **R1** | Golden set + Recall@k harness | Without it, R2–R4 are vibes. Turns every later claim into a measured number | 1–2 days | ✅ (CI-ready) |
| **R2** | Session-aware query rewriting | Cheapest per line; no schema change; fixes follow-ups (a marketed capability that is currently broken for docs) | 1–2 days | ✅ flag `rag.query_rewrite` |
| **R3** | Hybrid BM25 + dense, RRF, (optional) rerank | Biggest measured win (identifier questions); pure Postgres, no re-ingest needed | 3–5 days | ✅ flag `rag.retrieval_mode` |
| **R4** | Parent-doc chunking + metadata + version lifecycle | Schema migration forces re-ingest → must batch everything re-ingest-touching into one wave, and go last so it only happens once | 1.5–2 weeks | ✅ flag `rag.chunking_v2` |

Dependency edges: R1 → (R2, R3, R4) for measurement; R3's tsvector column is generated
(no re-embed needed, backfills instantly); R4 changes chunk shape (re-embed required).

---

## 5. Phase R1 — Golden Set + Retrieval Eval Harness

**Goal:** a reproducible measurement of retrieval quality that runs in CI (fake-mode) and
on demand against a live seeded environment.

**Why first:** every subsequent phase reports "before → after" against this harness.

### 5.1 Golden set

- **Location:** `tests/golden/rag_golden.yaml` (alongside the SQL parity suite).
- **Format** (one entry per question):

```yaml
- id: err-code-302
  tenant: demo
  question: "what does error code E-302 mean?"
  expect:
    document: coldchain-door-sensor-manual-v2.pdf   # filename must appear in top-k
    page_range: [41, 43]                            # optional page window
  tags: [identifier]                                # identifier | paraphrase | table |
                                                    # procedure | followup | multi-hop
```

- **Size:** 30–50 entries minimum, tag-balanced:
  - ~10 identifier/code/part-number questions (they justify R3),
  - ~10 paraphrase questions (dense's home turf — guards against hybrid regressions),
  - ~5 table-lookups and ~5 procedure questions (baseline the R4 pain),
  - ~5 follow-up questions (baseline the R2 pain; scored after R2 wires sessions),
  - ~5 multi-hop/vague.
- **Source of truth:** seeded demo documents (`demo/seed_demo.py` gains a doc-seeding
  leg, or a new `demo/seed_docs.py`), so the set is fully reproducible.

### 5.2 Harness

- **Location:** `tools/rag_eval.py` (repo follows scripts-in-place convention; no package).
- **Modes:**
  - `--live`: embeds questions through the real `LLMClient`, searches the real store
    (requires seeded Postgres; env-gated like the existing `CTXORA_IT=1` integration runs).
  - `--fake`: deterministic fake embeddings (extend `rag/fake.py` only if needed for CI
    smoke; metrics from fakes are meaningless but the pipeline stays exercised).
- **Metrics:** Recall@5 (primary), Recall@k curve (k=1,3,5,10), MRR, per-tag breakdown,
  per-question dump (CSV) for failure inspection.
- **Baseline report:** first `--live` run output committed as
  `docs/tuning/rag_baseline_r1.md` (numbers, not prose).

### 5.3 Changes

| File | Change |
|---|---|
| `tests/golden/rag_golden.yaml` | new — the golden set |
| `tools/rag_eval.py` | new — harness (load set → retrieve → score → report) |
| `demo/seed_docs.py` (or `seed_demo.py` leg) | new — deterministic demo documents (2 device manuals × 2 versions, one table-heavy, one procedure-heavy) |
| `docs/tuning/rag_baseline_r1.md` | new — baseline numbers |

**No production code changes in R1.**

### 5.4 Acceptance

- `uv run python tools/rag_eval.py --live` produces a full report from a clean
  `docker compose up` + seed.
- Baseline numbers recorded (expected: identifier-tagged Recall@5 materially below
  paraphrase-tagged — that gap is R3's justification).
- Harness re-runs are deterministic for a fixed index state (same scores twice).

---

## 6. Phase R2 — Session-Aware Query Rewriting

**Goal:** follow-up questions ("and the door sensor?", "how do I fix it?") retrieve as if
the user had written the full question.

**Why second:** highest impact-per-line; touches no schema; unblocks a marketed capability.

### 6.1 API contract

- `RAGQueryRequest` gains optional `session_id: str | None` (default `None` keeps the
  endpoint fully backward compatible / stateless when omitted).
- `api/query.py` `_rag_answer` (hybrid path) passes the existing conversation context —
  it already has it for SQL follow-ups; RAG simply starts using it.

### 6.2 Rewrite step

- **New module:** `rag/rewrite.py`.

```python
def rewrite_query(
    llm: LLMClient, question: str, recent_turns: list[str] | None
) -> str:
    """Self-contained retrieval query from question + recent turns.

    No context or already self-contained -> question unchanged.
    Rewrite failure (any exception) -> question unchanged (non-blocking).
    """
```

- One `llm.generate` call, `temperature=0.0`, strict system prompt:
  "Rewrite the question into a standalone retrieval query containing every identifier,
  device, and metric mentioned. Reply with ONLY the rewritten query. If the question is
  already self-contained, reply with it unchanged."
- Recent turns source: last 2–4 turns of session history (reuse `memory` history lookup;
  SQL-path already resolves sessions this way).
- Called from `rag_flow.retrieve()` — so `query`, hybrid `_rag_answer`, **and `advise()`**
  all inherit it.
- **Cheap guard:** if `session_id` is `None` or history is empty → skip the LLM call
  entirely (zero added latency for the stateless case).

### 6.3 Config

```yaml
rag:
  query_rewrite: true        # false => byte-identical to v1 behavior
  rewrite_history_turns: 3
```

### 6.4 Changes

| File | Change |
|---|---|
| `rag/rewrite.py` | new — rewrite function |
| `rag/rag_flow.py` | `retrieve()` accepts optional `recent_turns`, calls rewrite first |
| `api/rag.py` | optional `session_id`; resolve history; pass turns |
| `api/query.py` | `_rag_answer` passes conversation context turns |
| `rag/fake.py` + `tests/test_rag_core.py` | fake LLM rewrite; contract tests |

### 6.5 Acceptance

- Golden set `followup` tag: Recall@5 baseline → target ≥ baseline + 0.3 (follow-ups
  currently retrieve near-randomly; this should be a step change, not incremental).
- `paraphrase`/`identifier` tags: no regression (rewrite is a no-op for self-contained
  questions by construction — verify in per-question dump).
- Latency: stateless path unchanged (no extra LLM call); session path +1 cheap call.
- All existing RAG tests green with `query_rewrite: false` (backward-compat proof).

---

## 7. Phase R3 — Hybrid BM25 + Dense with RRF Fusion

**Goal:** exact tokens (codes, part numbers, spec values) and paraphrases both land in
top-k; fusion replaces neither leg.

**Why third:** biggest measured win; pure additive Postgres (generated column = no
re-embedding of existing chunks); needs R1 to prove it.

### 7.1 Lexical leg (Postgres-native, vendor-neutral)

- **Migration** (`rag/schema.sql` + `database/metadata.py` bootstrap bump):

```sql
ALTER TABLE rag_chunks
    ADD COLUMN IF NOT EXISTS body_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(section_title,'') || ' ' || chunk_text)) STORED;
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv ON rag_chunks USING gin (body_tsv);
```

- Generated + STORED → backfills for existing rows automatically, zero application
  changes needed for old data; no re-ingest.

### 7.2 Retrieval orchestration

- `PGRagStore` gains a lexical search method; fusion lives in a new pure function so it
  is unit-testable and fake-compatible:

```python
# rag/fusion.py
def rrf_fuse(
    dense: list[RetrievedChunk], lexical: list[RetrievedChunk],
    k: int = 60, top_k: int = 5,
) -> list[RetrievedChunk]:
    """Reciprocal-rank fusion: score = Σ 1/(k + rank); dedupe by (document, chunk text)."""
```

- Orchestration in `rag_flow.retrieve()`: dense top-50 + lexical top-50 → RRF → top_k.
- Candidate pool (`top_k_candidates: 50`) becomes a `RagConfig` field; final `top_k`
  stays 5.
- **Degradation:** lexical query throws/empty → dense-only result, log warning
  (mirror of `_rag_answer` boundary style). Dense throws → symmetric.

### 7.3 Rerank (flag-gated second step, ship after RRF proves out)

- `rag.rerank: false` initially. When on: fuse → take top-N candidates (20) → one
  listwise `llm.generate` call ordering by relevance to the question → keep top_k.
- Reranker is any configured LLM through the existing `LLMClient` boundary — no new
  dependency, no vendor lock.
- Timeout/degenerate output → RRF order stands (non-blocking).

### 7.4 Config

```yaml
rag:
  retrieval_mode: dense     # dense | hybrid
  top_k_candidates: 50
  rrf_k: 60
  rerank: false
```

### 7.5 Changes

| File | Change |
|---|---|
| `rag/schema.sql` | `body_tsv` generated column + GIN index |
| `database/metadata.py` | bootstrap includes new DDL (idempotent) |
| `rag/store.py` | lexical search method (ts_rank on `plainto_tsquery`) |
| `rag/fusion.py` | new — `rrf_fuse` (+ later rerank wrapper) |
| `rag/contracts.py` | `RagStore` protocol extended; `rag/fake.py` same commit |
| `rag/rag_flow.py` | orchestration per `retrieval_mode` |
| `config/settings.py` | new `RagConfig` fields |

### 7.6 Acceptance

- Golden set `identifier` tag: Recall@5 target ≥ 0.85 (literature: 0.72 → 0.91).
- `paraphrase` tag: no regression vs baseline (the classic hybrid failure is BM25 noise
  drowning paraphrases — RRF's rank-only fusion is the mitigation; verify with data).
- `retrieval_mode: dense` reproduces baseline numbers exactly (flag-off = v1 proof).
- Query latency: p95 increase ≤ 2× the lexical query cost (one extra indexed scan).
- Rerank (when enabled): `identifier` + `table` tags improve again; p95 within budget.

---

## 8. Phase R4 — Structure-Aware Chunking + Metadata & Versioned Retrieval

**Goal:** procedures retrieve whole, tables retrieve intact, retrieval returns parent
context, and the right manual/version is selected deterministically.

**Why last and batched:** single schema migration + single forced re-ingest for all of it.

### 8.1 Schema migration (one batch)

```sql
ALTER TABLE rag_documents
    ADD COLUMN IF NOT EXISTS doc_family    VARCHAR(512),  -- logical manual identity (e.g. base filename)
    ADD COLUMN IF NOT EXISTS doc_version   VARCHAR(50),   -- e.g. "v2.3"
    ADD COLUMN IF NOT EXISTS metadata      JSONB DEFAULT '{}';

ALTER TABLE rag_chunks
    ADD COLUMN IF NOT EXISTS parent_id     UUID,          -- parent section chunk
    ADD COLUMN IF NOT EXISTS chunk_kind    VARCHAR(20),   -- section | step | table | child
    ADD COLUMN IF NOT EXISTS metadata      JSONB DEFAULT '{}';
    -- device_model / firmware_version / deprecated live in metadata (JSONB),
    -- queryable via ->> operators; promote to columns later only if filters
    -- show up in EXPLAIN as bottlenecks.
```

- **Vector column note:** `VECTOR(1536)` hardcodes today's embedding model. Fold the
  decision into this migration: either (a) keep 1536 and document it as the contract
  (cheapest; `embedding_model` column already records it), or (b) plan a re-embed path
  before any model change. Default: (a) + a startup check that warns when
  `settings.embedding_model` dimensions ≠ column dimensions.

### 8.2 Structure-aware parsing & chunking

- `rag/parsers.py`: preserve markdown tables and numbered-step runs as structural units
  (do not let a table become free text).
- `rag/chunker.py` v2 (`rag.chunking_v2: true` gates it):
  - **Parent** = whole section (bounded, e.g. ≤ 2000 words; larger sections split at
    sub-heading boundaries).
  - **Children** = paragraphs, procedure step-groups, whole tables (≤ 400 words);
    children get embedded, parents get returned.
  - Tables over the child bound split **on row boundaries only**, never mid-row, with
    header row repeated into each piece.
- `rag/store.py::search` joins child hits → parent rows (dedupe parents, parent text +
  child `section_title` in `RetrievedChunk`).
- Hash-dedupe interplay: chunk hashes salt with schema version
  (`sha256("v2:" + text)`) so re-uploading the same file after upgrade re-chunks
  instead of no-op'ing on stale `rag_documents.file_hash` rows — plus a one-time
  `UPDATE rag_documents SET status = 'SUPERSEDED'` cleanup script for pre-v2 rows.

### 8.3 Metadata & filtered retrieval

- Ingestion accepts document metadata (`device_model`, `firmware_version`, `doc_version`,
  free-form) via the upload API; chunk metadata inherits document metadata by default.
- **Version lifecycle:** upload matching an existing `doc_family` with a newer
  `doc_version` (or same filename, different hash) → old document flips
  `status: ACTIVE → SUPERSEDED` in the same transaction; `search()` filters
  `status = 'ACTIVE'` (the column already exists — R4 is the first thing to ever use it).
- `RagStore.search` contract gains `filters: RagFilters | None` (dataclass:
  `device_model`, `firmware_min`, `include_deprecated=False`, …) → SQL `WHERE` clauses
  on `metadata ->> '…'`; API request accepts an optional `filters` object; advisor
  passes device metadata from the incident event when present.

### 8.4 Config

```yaml
rag:
  chunking_v2: true
  parent_max_words: 2000
  child_max_words: 400
  table_row_bound_split: true
  filter_by_default: true      # exclude SUPERSEDED/deprecated unless explicitly requested
```

### 8.5 Changes

| File | Change |
|---|---|
| `rag/schema.sql`, `database/metadata.py` | migration above (idempotent, additive) |
| `rag/parsers.py` | table/step structural preservation |
| `rag/chunker.py` | parent/child splitting, row-bound table splits, hash salt |
| `rag/ingest.py` | metadata in, doc_family/version resolution, supersede transaction |
| `rag/contracts.py` + `rag/fake.py` | `RagFilters`, parent-returning search, same commit |
| `rag/store.py` | child→parent join, metadata filters, ACTIVE-only |
| `api/documents.py` | upload accepts metadata; response marks supersessions |
| `api/rag.py`, `api/query.py` | optional `filters` pass-through |
| `tools/reingest.py` | one-time re-ingest driver (re-parse originals via stored file hash → re-chunk → re-embed), or documented "re-upload" path |

### 8.6 Acceptance

- Golden set `procedure` tag: no question whose answer spans steps 1–N retrieves a
  partial procedure (add dedicated "step-4-only" cases: expect the parent containing it).
- `table` tag: row integrity — retrieved chunk for a spec lookup contains the full row
  (assertable in harness with `page_range` + contains-check on values).
- Version test: seed manual v1 + v2 (same family), ask a question whose answer changed →
  answer cites v2 only, v1 rows unreachable in default search.
- Filter test: `device_model` filter excludes the other manual's chunks deterministically.
- Literature targets for the report: faithfulness +10–20%, precision 0.73 → 0.82.
- Migration rehearsal: `docker compose down -v && up` fresh-install path **and**
  in-place upgrade path both verified.

---

## 9. Cross-Cutting Concerns

### 9.1 Citation precision (small, rides along with R3/R4)

`answer_grounded` currently cites `chunks[:5]` regardless of which chunks grounded the
answer. With rerank (R3) and parent returns (R4) this improves naturally; additionally
ask the answer model to mark which `[doc p.N]` blocks it used and cite only those.
Tighten the `sources` contract in R4.

### 9.2 Rollout & rollback

Every phase is flag-gated with the flag-off path being byte-identical v1 behavior,
proven by the harness reproducing baseline numbers with flags off. Rollback = config
flip; the only irreversible step is the R4 re-ingest (mitigated: old rows are marked
SUPERSEDED, not deleted, until a post-verification cleanup).

### 9.3 Cost model

- R2: +1 cheap LLM call per session-attached RAG query (skipped when stateless).
- R3: +1 indexed Postgres scan per query; rerank (optional) +1 LLM call over ~20 candidates.
- R4: one-time re-embedding of all documents; steady-state embedding cost unchanged
  (child tokens ≈ today's chunk tokens; parents are returned, not embedded).

### 9.4 Testing strategy (per project conventions)

- Unit: fusion math (RRF), rewrite guards, chunker invariants (row-bound splits, step
  grouping, hash salt), lifecycle transitions — all fake-backed.
- Golden: harness before/after each phase, numbers in the phase report.
- Integration (CTXORA_IT-style, live Postgres): generated tsvector behavior, child→parent
  SQL, metadata filter pushdown, supersede transaction.
- Contract: `rag/fake.py` tracks `RagStore` protocol changes in every commit that touches it.

### 9.5 Provenance

All designs here are Postgres/pgvector-native and vendor-neutral; no proprietary
patterns (CONTRIBUTING.md clean-room rules apply as usual).

## 10. Configuration Reference (End State)

```yaml
rag:
  # v1 (unchanged defaults)
  chunk_size: 800
  chunk_overlap: 120
  top_k: 5
  max_upload_mb: 20
  shared_scope: shared
  # R2
  query_rewrite: true
  rewrite_history_turns: 3
  # R3
  retrieval_mode: hybrid          # dense | hybrid
  top_k_candidates: 50
  rrf_k: 60
  rerank: false
  # R4
  chunking_v2: true
  parent_max_words: 2000
  child_max_words: 400
  filter_by_default: true
```

## 11. Success Metrics

| Metric | Baseline (R1 measures) | R3 target | R4 target |
|---|---|---|---|
| Recall@5 — identifier tag | *measure (expect low)* | ≥ 0.85 | ≥ 0.87 |
| Recall@5 — paraphrase tag | *measure* | no regression | no regression |
| Recall@5 — followup tag | *measure (expect near-random)* | — | ≥ baseline + 0.3 (R2) |
| Procedure completeness | *measure (partial-retrieval count)* | — | 0 partial procedures |
| Version correctness | n/a (fails today) | — | 100% current-version citations |
| Precision@5 (filtered) | *measure* | — | ≥ 0.82 |

Literature numbers are planning inputs only; R1 replaces them with this system's truth.

## 12. Timeline & Sequencing

```text
Week 1      R1  golden set + harness + baseline report         (1–2 d)
            R2  query rewriting, session-aware RAG             (1–2 d)
Week 2–3    R3  hybrid + RRF; rerank flag-gated after          (3–5 d)
Week 3–5    R4  migration batch + parsers/chunker v2 + re-ingest + lifecycle (1.5–2 w)
```

Serial by design (each phase's report is the next phase's baseline); R2 and R3 have no
code dependency on each other and may interleave if desired — R1 → R4 order is fixed
only by wanting exactly one re-ingest.

## 13. Out of Scope / Deferred

- **Multi-embedding-model support** (dimension-agnostic vector column / model swap):
  startup warning only; real support deferred until a model change is actually planned.
- **RAGAS or external eval frameworks**: hand-rolled harness first; add RAGAS later only
  if automated faithfulness metrics become a need.
- **GraphRAG / knowledge-graph retrieval**: not justified by current failure modes.
- **Cross-tenant retrieval beyond the existing shared scope**: security model unchanged.
- **Semantic cache of query rewrites**: latency is fine; revisit under real traffic.
