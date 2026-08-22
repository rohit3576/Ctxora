# Phase 2 — Memory, Streaming, Onboarding Probe

> Status: **COMPLETE (code + gates + live boot verification) — 2026-08-19**
> Depends on: Phase 1 (complete)
> Plan source: architecture §8 Phase 2 · spec: blueprint §11 (memory), §14 (SSE), §13 (probe)

## Goal

Conversations persist and stream. The service remembers what it answered, answers arrive
as SSE token streams, and a tenant can be introspected without a DB client.

## Scope

**In:** sessions + history persistence · SSE streaming endpoint · history API · onboarding
probe (key/event introspection) · readiness-lite check.

**Out:** digests/corrections (3) · feedback (4) · full wizard naming (7).

## Deliverables (as built)

| File | What it does |
|---|---|
| `memory/schema.sql` | `llm_sessions` + `llm_sql_history` (incl. `supersedes_id` for Phase 3) |
| `memory/contracts.py` | `Session`, `TurnInsert`, `HistoryTurn`, `SessionHistory`, `MemoryStore` protocol |
| `memory/fake.py` | in-memory fake (own protocol test) with deterministic newest-first ordering |
| `memory/pg.py` | PG store over injected executor; jsonb rows parsed via Pydantic TypeAdapter |
| `agent/titles.py` | deterministic keyword titles — `title_keywords` from `config/defaults.yaml` |
| `api/flow.py` | session resolve/create + non-blocking turn recording (any failure logged, swallowed) |
| `api/query.py` | `sessionId` in/out; `sessionId`+`historyId` in payload; typed exception mapping |
| `api/history.py` | `GET /v1/history?tenant=` — sessions newest-first, oldest-first turns |
| `api/stream.py` | `POST /v1/query/sql/stream` — SSE: `stage` x5 -> `summary_delta` chunks -> `final`/`error`; `: ping` heartbeat (interval injectable); worker thread + queue |
| `api/onboarding.py` | `GET /v1/onboarding/{tenant}/probe` (keys+event types, write-through cache) · `GET .../readiness` (keysRegistered, probeCached) |
| `onboarding/schema.sql` + `state.py` | minimal `onboarding_state` table + access |
| stores | real `introspect_event_types` in both adapters ([] when events unconfigured); factory passes events table template |
| `database/metadata.py` | bootstrap now applies knowledge + memory + onboarding schemas |

## Acceptance criteria — results

- [x] Two-turn conversation in one `sessionId`: both turns in `/v1/history`, deterministic title (`Rpm Query`)
- [x] No `sessionId` -> new session created and returned
- [x] SSE: stages in pipeline order -> deltas -> `final`; **`final` == sync response** (per-request ids normalized)
- [x] History/memory down -> query still answers 200 (`sessionId: null`), write failures logged
- [x] Probe returns 5 demo keys + counts (fake store); readiness checklist correct in all three states
- [x] Gates: **pytest 131 passed + 10 integration skipped · ruff check clean · format clean · basedpyright 0 errors**

## Evidence

- **Live boot** (uvicorn, metadata DB unreachable):
  - `/healthz` -> 200
  - sync query -> typed `503 PIPELINE_UNAVAILABLE` envelope (not a stack-trace 500)
  - stream -> SSE `stage` events then `event: error` carrying the same typed envelope
- **Outage tests**: metadata-down -> 503 (sync), probe store-down -> `503 STORE_UNAVAILABLE`
- **Ping**: verified with 0.05s interval + 0.3s stalling store (`: ping` frames present, `final` still last)
- **Integration tests** (`tests/integration/test_memory_integration.py`): sessions round-trip,
  probe cache, knowledge registration — behind `DATAMIND_IT=1` (compose), consistent with
  the Phase-1 owner decision to skip live infra runs locally

## Decisions taken (doc defaults)

1. Session identity = bare UUID held by the client (auth lands Phase 7)
2. Stream path = `/v1/query/sql/stream`
3. History retention deferred to Phase 7 ops
4. (Added during build) typed 503 envelopes for dependency outages — live-boot finding
