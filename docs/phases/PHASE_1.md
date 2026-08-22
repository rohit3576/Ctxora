# Phase 1 — Core NL→SQL Vertical Slice

> Status: **COMPLETE (code + gates) — live-run items intentionally skipped per owner decision, 2026-08-19**
> Depends on: Phase 0 (complete)
> Plan source: `docs/architecture/ARCHITECTURE.md` §8 Phase 1 · spec: blueprint §5 (S5–S13)

## Goal

One natural-language question in, one validated read-only SQL statement executed, one
natural-language answer out — for a single seeded `demo` tenant, ClickHouse first.

## Scope

**In:** S5 key resolution · S6 knowledge retrieval (keyword slicing) · S7 prompt assembly ·
S8 SQL generation (LLM, temp 0.0, fenced-block contract) · S9 validation + auto-repair ·
S10 read-only execution (row cap, timeout, memory guards) · S11 formatting · S12 summarizer ·
knowledge store (PG, cached) · `/v1/query/sql` endpoint · demo seed · error mapping
(400/422/502/503/500).

**Out (later phases by design):** sessions/history (2) · correction/follow-up/assume-first (3) ·
feedback flywheel (4) · document RAG + routing (5) · Postgres adapter + semantic examples (6).

## Deliverables

| Module | File | What it does |
|---|---|---|
| Knowledge store | `knowledge/store.py`, `contracts.py`, `render.py`, `schema.sql`, `pg.py` | 10-table DDL; structured `TenantKnowledge`; TTL+LRU class cache; `NotOnboardedError` gate |
| Key resolver | `agent/key_resolver.py` | alias phrases → verified canonical keys (longest-first, word-boundary, registry-verified) |
| Retriever+prompt | `agent/prompt_builder.py` | slices knowledge by resolved keys; deterministic section order; dialect renders EAV rules |
| Generator | `agent/generator.py` | LLM call, fenced-SQL extraction, `GenerationError` |
| Validator | `agent/validator.py` | forbidden verbs (dialect), table allowlist, CTE depth ≤5, one value-cast auto-repair pass |
| Stores | `database/clickhouse_store.py`, `postgres_store.py`, `rows.py`, `factory.py` | read-only execute (cap/timeout/memory), row normalization to JSON scalars |
| LLM | `llm/openai_compat.py` | httpx2 client, chat + embeddings, typed payload narrowing, `LLMError` |
| Pipeline | `agent/pipeline.py` | S5→S13 wiring; outcome union: Success / Rejected / ExecutionFailed |
| API | `api/query.py` | POST `/v1/query/sql`; envelope; typed error mapping |
| Demo | `demo/seed_demo.py`, `demo/questions.md` | deterministic synthetic fleet (3 trucks × 5 keys × 24h), knowledge seed, acceptance script |
| Tests | `tests/test_*.py`, `tests/integration/` | unit (fakes) + fake-based e2e + live integration (skip-marked, `DATAMIND_IT=1`) |

## Contracts & flags touched

No new flags (all conversational flags stay off). New protocols in use: `TelemetryStore.execute`.
Config: `agent.row_cap`, `agent.query_timeout_s`, `stores.telemetry.mapping` drive everything.

## Acceptance criteria

- [x] Fake-based e2e: "average RPM of truck-102 yesterday?" → 200, SQL filters `engine.rpm`, summary quotes the number, `resolvedKeys` correct
- [x] "delete all telemetry" → 400 `SQL_VALIDATION_FAILED`
- [x] unknown tenant → 422 `TENANT_NOT_ONBOARDED`
- [x] generation failure → 502; DB down → 503 (connection kind)
- [x] `pytest` green — **98 passed, 5 integration skipped** (final run)
- [x] `ruff check` + `ruff format --check` clean (one ISC004 violation found and fixed during re-verification)
- [x] **`basedpyright` 0 errors** — cleanup completed (130 → 0; final: 0 errors, 50 warnings)
- [~] Integration run vs live compose — **skipped by owner decision** ("no need for this"); instructions preserved in `demo/questions.md`; tests remain in `tests/integration/` behind `DATAMIND_IT=1`
- [~] Live LLM demo — **skipped by owner decision** (needs Docker + `LLM_API_KEY`); script preserved in `demo/questions.md`
- [x] Supporting evidence recorded (leak scan clean; README + phases index updated)

## Current status (final ledger)

Complete. All four gates green on the final run:

| Gate | Result |
|---|---|
| pytest | 98 passed, 5 integration skipped, 0.48s |
| ruff check | All checks passed |
| ruff format --check | 63 files clean |
| basedpyright | **0 errors**, 50 warnings |

Type-cleanup work performed (no behavior change): typed `database/clickhouse/` gateway
package isolating the untyped optional-extra client; psycopg 3.3 `LiteralString` contract
honored via `bytes` queries for dynamic SQL; LLM responses now parsed once at the boundary
by Pydantic models (parse-don't-validate replaced manual isinstance narrowing); public
`KnowledgeStore.reset_state()` test hook replaced private-attribute pokes; standalone
protocol-conforming fakes replaced subclass overrides.

Deferred items (owner decision, recorded above): compose integration run and live LLM
demo. Neither blocks Phase 2; both have preserved run instructions.

## Open questions for your review

1. **Error envelope shape** — current: `{status, message, data, errorType, statusCode}`. Keep?
2. **camelCase wire fields** (`resolvedKeys`, `rowCount`…) — Pydantic aliases keep Python
   snake_case; wire stays camelCase. OK?
3. **Validator severity** — table-allowlist violation returns 400 with all errors listed;
   no partial retry (one repair pass only). Agreed?
4. **Seed scale** — 3 trucks × 5 keys × 24 hourly points = 360 rows. Enough for demo?
5. **ClickHouse optional extra** — `uv sync --extra clickhouse`; Postgres demo works without
   the extra. Keep this packaging?

## Review checklist for you

- [ ] Deliverable table matches what you expect from "Phase 1"
- [ ] Acceptance criteria are the right bar (add/remove any)
- [ ] Open questions answered
- [ ] Approve finishing work (items 1–3 in Current status)
