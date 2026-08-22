# Phase 5 — Document RAG + Hybrid Routing + Advisor

> Status: **COMPLETE (code + gates + live outage boot) — 2026-08-19**
> Depends on: Phase 2 (sessions) — independent of 3/4
> Plan source: architecture §8 Phase 5 · spec: blueprint §9, §10

## Goal

The "docs" half: upload manuals → ask documentation questions with cited sources; one
unified entry routes data questions to SQL and doc questions to RAG; structured incident
analysis (advisor) on top of RAG.

## Scope

**In:** document ingestion (PDF/DOCX/XLSX/HTML/MD) · pgvector storage + cosine retrieval ·
`/v1/rag/query` · `/v1/documents` CRUD · keyword intent router + `/v1/query` unified ·
advisor mode.

**Out:** semantic few-shot for SQL examples (6) · LLM router (v1.0+).

## Deliverables

| File | What it does |
|---|---|
| `rag/schema.sql` | `rag_documents`, `rag_chunks(embedding VECTOR(1536), tenant, scope, chunk_hash…)`, HNSW cosine index |
| `rag/ingest.py` | parse (pymupdf/python-docx/openpyxl/bs4/markdown) → chunk (configurable size/overlap, section-aware) → embed → store; re-upload same file hash = no-op; delete removes chunks (FK cascade) |
| `rag/retriever.py` | embed query → `ORDER BY embedding <=> q LIMIT k`, tenant scope + optional shared scope merge; returns chunks + scores |
| `rag/answer.py` | grounded answer LLM call: answer + `sources[{document, page}]`; refuse-when-ungrounded instruction |
| `rag/advisor.py` | event description + telemetry snapshot → structured JSON `{summary, possible_causes, possible_consequences, immediate_actions, inspection_checklist, recommended_action, estimated_risk, can_continue, confidence, sources}` |
| `routing/keyword_router.py` | config-driven indicator lists (already in `defaults.yaml`); returns `data | docs | hybrid` |
| `api/documents.py` | `POST /v1/documents` (multipart), `GET /v1/documents`, `DELETE /v1/documents/{id}` |
| `api/query.py` (extend) | `POST /v1/query` — route → SQL pipeline and/or RAG; hybrid = rows + doc-grounded recommendations, one merged answer |
| `api/rag.py` | `POST /v1/rag/query`, `POST /v1/rag/advisor` |

## Contracts & flags

- Embedding dims must match `EMBEDDING_MODEL` (schema fixed at 1536 for v1; document).
- Router is a Protocol (`routing/router.py`) so the LLM classifier can swap in later.

## Acceptance criteria — results

- [x] Upload markdown manual (multipart) → listed with chunk counts; delete removes rows (404 on repeat); re-upload of identical content dedupes by hash
- [x] "acceptable coolant temperature range?" via `/v1/rag/query` → grounded answer citing `coolant.md` with page; no-docs tenant → typed 404 `NO_DOCUMENTS`; ungrounded → 404 `NOT_GROUNDED`
- [x] "average rpm yesterday?" via `/v1/query` → intent `data`, byte-equal outcome to `/v1/query/sql`
- [x] Hybrid question → one envelope: SQL rows + summary + `docAnswer` + `sources`
- [x] Advisor returns parseable JSON with all 10 fields (`sources` included) on a typed model
- [x] Router matrix: pure-SQL / pure-RAG / hybrid (both lists) / ambiguous→data / word-boundary (`manuals` ≠ `manual`)
- [x] Gates: **pytest 228 passed + 11 integration skipped · ruff clean · format clean · basedpyright 0 errors**

## Evidence

- Unit (`tests/test_rag_core.py`, 21): chunker overlap/hashes; md/html parsers + unsupported-suffix;
  ingest dedupe/empty-parse/size-cap; tenant-isolated cosine ranking; grounded answer + refusal;
  advisor JSON + invalid-JSON; router matrix; protocol conformance
- API (`tests/test_rag_api.py`, 12): document lifecycle (upload→list→delete→404), 415 unsupported,
  dedupe, grounded query with sources, no-docs 404, unified data/docs/hybrid routes, advisor
  10-field shape, **RAG outage → typed 503, hybrid degrades to SQL-only** (regression after
  live-boot catch)
- Integration (`tests/integration/test_rag_integration.py`): pgvector ingest→retrieve→delete
  round-trip behind `QUERYPULSE_IT=1` (per standing owner decision on local infra runs)
- Live boot (uvicorn, PG down): `/v1/rag/query` → typed `503 RAG_STORE_UNAVAILABLE` envelope;
  `/v1/query` data route → typed `503 PIPELINE_UNAVAILABLE`

## Decisions taken (doc defaults + build notes)

1. pgvector via existing compose image; extension created by schema bootstrap
2. Upload cap 20 MB (`rag.max_upload_mb`), chunk 800/120, top_k 5 — all in `defaults.yaml`
3. Hybrid merge: single envelope; SQL payload + `docAnswer` + `sources`
4. Advisor generic; prompt template from `rag.advisor_template`; reply parsed into a typed
   Pydantic model (parse-don't-validate at the LLM boundary)
5. (Build note) binary-format parsers (pymupdf/docx/openpyxl) isolated behind `rag/office/`
   package boundary — typed `ChunkedPage` out, untyped libs contained
6. (Build note) live boot caught RAG endpoints returning raw 500 on PG-down → typed 503 +
   hybrid degradation added and regression-tested (fourth such catch; boot verification stays
   in the loop)

## Test plan

Unit: router classification matrix; chunker boundaries; advisor JSON parse (fake LLM).
Fake-LLM e2e: all three routes. Integration: real pgvector round-trip with a tiny fixture PDF.

## Open questions for review

1. **pgvector dependency** — compose image already `pgvector/pgvector:pg16`. Extension
   created by bootstrap; OK?
2. **Max upload size** — propose 20 MB default, configurable. 
3. **Hybrid merge shape** — one `answer` string + `rows` + `sources`, or two separate
   sections? (current plan: single envelope, `answer` merges, `rows` and `sources` separate)
4. **Advisor scope** — generic (any event+telemetry) with domain prompts in config, or
   hardcode one maintenance template? (current: generic, template in config)

## Review checklist

- [ ] Router indicators in `defaults.yaml` match your expectation
- [ ] Source-citation requirement confirmed
- [ ] Open questions answered
