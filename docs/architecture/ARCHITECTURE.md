# Ctxora — Repository Architecture & Build Plan

> **Companion doc:** [`../blueprint/IMPLEMENTATION_BLUEPRINT.md`](../blueprint/IMPLEMENTATION_BLUEPRINT.md) is the *specification* (what the system does, stage-by-stage pipeline, schemas, safety rules). **This doc is the builder's guide**: repo layout, module contracts, the backend-agnostic adapter design, and a phase-by-phase implementation plan with copy-vs-rewrite guidance.

**Design constraint #1, stated up front:** Ctxora is **not a ClickHouse tool**. It is a natural-language query engine for **any key-value telemetry store**. ClickHouse is the first adapter; PostgreSQL (and TimescaleDB) are first-class peers. Every engine-specific detail lives behind two interfaces (`Dialect` + `TelemetryStore`, §4) — the agent core never imports an engine client directly.

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [Architecture Overview](#2-architecture-overview)
3. [Repository Layout](#3-repository-layout)
4. [Core Contracts & Interfaces](#4-core-contracts--interfaces)
5. [Configuration System](#5-configuration-system)
6. [Data Contracts](#6-data-contracts)
7. [Dialect Portability Matrix](#7-dialect-portability-matrix)
8. [Phase Plan (v0.1 → v1.0)](#8-phase-plan-v01--v10)
9. [Copy Map: Company Repo → Ctxora](#9-copy-map-company-repo--ctxora)
10. [Testing Layout](#10-testing-layout)
11. [Dependencies](#11-dependencies)
12. [Conventions & Guardrails](#12-conventions--guardrails)

---

## 1. Goals & Non-Goals

**Goals**

- Natural-language → validated, read-only SQL over **key-value telemetry** (`timestamp + entity + key + value`), on **multiple storage engines**.
- Anyone with KV data points the service at their database, declares a column mapping, and gets an ask-in-English API.
- Modular phases: each phase ships a runnable, demoable increment behind feature flags.
- Every module unit-testable with fakes (no live LLM/DB required for tests).

**Non-Goals (explicitly out of scope)**

- Write-path SQL (INSERT/UPDATE/DELETE generation) — never.
- Being a general-purpose text-to-SQL over arbitrary relational warehouses (focus = KV/EAV telemetry; relational tables enter only as join targets).
- A heavy frontend framework in core (a thin Streamlit demo panel ships in `demo/`).
- Multi-region / horizontal-scale concerns before v1.0.

---

## 2. Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                            API LAYER  (api/)                             │
│   FastAPI routers · request/response models · SSE streaming · auth hook  │
└───────────────┬──────────────────────────────────────────┬───────────────┘
                │                                          │
                ▼                                          ▼
┌───────────────────────────────┐          ┌───────────────────────────────┐
│      AGENT CORE (agent/)      │          │     ROUTER (routing/)         │
│  S0 greeting   S1 correction  │◄─────────│  data-question vs doc-question│
│  S2 follow-up  S3 assume-first│          │  vs hybrid (config indicators │
│  S5 key resolution            │          │  or LLM classifier)           │
│  S7 prompt      S8 generate   │          └───────────────┬───────────────┘
│  S9 validate    S11 format    │                          │
│  S12 summarize                │                          ▼
└──────┬─────────┬──────────────┘          ┌───────────────────────────────┐
       │         │                         │      DOCUMENT RAG (rag/)      │
       │         │                         │  ingest → chunk → embed →     │
       │         │                         │  pgvector retrieve → answer   │
       │         │                         └───────────────────────────────┘
       ▼         ▼
┌──────────────────────┐   ┌──────────────────────────────────────────────┐
│ KNOWLEDGE (knowledge/)│   │          MEMORY (memory/)                    │
│ sql_agent_* tables   │   │  sessions · history · digests                │
│ cached loader        │   └──────────────────────────────────────────────┘
│ aliases/rules/examples│   ┌──────────────────────────────────────────────┐
└──────────────────────┘   │          FEEDBACK (feedback/)                 │
┌──────────────────────┐   │  capture · auto-mine · review · promote ·    │
│  ONBOARDING          │   │  decay · graduation                           │
│  probe→naming→activate│  └──────────────────────────────────────────────┘
└──────────────────────┘
       │
       ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                     STORAGE ABSTRACTION (database/)                       │
│                                                                            │
│  TelemetryStore (protocol)              MetadataStore (protocol)           │
│  ├─ ClickHouseStore                     │  (Postgres + pgvector — single   │
│  ├─ PostgresStore  (plain / Timescale)  │   implementation: knowledge,     │
│  └─ (future adapters plug here)         │   sessions, feedback, vectors)   │
│                                                                            │
│  Dialect (protocol) — per-engine SQL rendering for prompts & validation    │
│  ├─ ClickHouseDialect   ├─ PostgresDialect   └─ (future…)                  │
└───────────────────────────────────────────────────────────────────────────┘
```

**Dependency rule (enforced in review):** arrows point *down* only. `agent/` imports `database/` protocols, never a concrete client. `api/` orchestrates; modules never import `api/`. Everything engine- or provider-specific is an adapter.

---

## 3. Repository Layout

```text
ctxora/
├── main.py                        # app factory, router registration, startup
├── api/
│   ├── query.py                   # POST /v1/query, /v1/query/sql[/stream]
│   ├── documents.py               # doc upload/list/delete (RAG)
│   ├── feedback_admin.py          # admin review surface (token-gated)
│   ├── onboarding.py              # wizard endpoints
│   ├── health.py                  # /healthz, /readyz
│   └── schemas.py                 # Pydantic request/response models + envelope
│
├── agent/
│   ├── pipeline.py                # S0–S13 orchestration (the only state machine)
│   ├── key_resolver.py            # S5: NL → canonical keys (via knowledge/)
│   ├── knowledge_retriever.py     # S6: slice knowledge by resolved keys
│   ├── prompt_builder.py          # S7: deterministic section assembly
│   ├── generator.py               # S8: LLM call, fenced-SQL extraction
│   ├── validator.py               # S9: rule engine + auto-repair
│   ├── executor.py                # S10: via TelemetryStore (no engine import)
│   ├── formatter.py               # S11: rows → typed list-of-dicts
│   ├── summarizer.py              # S12: rows → NL answer (streamable)
│   ├── correction.py              # S1 ⛳   followup.py  # S2 ⛳
│   └── assume_first.py            # S3 ⛳
│
├── routing/
│   ├── router.py                  # S4: intent classification interface
│   └── keyword_router.py          # config-indicator implementation
│
├── knowledge/
│   ├── store.py                   # PG knowledge loader: TTL+LRU cache,
│   │                              #   invalidation, metrics, onboarded gate
│   ├── schema.sql                 # sql_agent_* DDL + migrations
│   └── seed/
│       └── demo.sql               # idempotent seed for the demo tenant
│
├── database/
│   ├── contracts.py               # TelemetryStore + Dialect protocols
│   ├── dialects/
│   │   ├── clickhouse.py          # casts, argMax, JSONExtract, time buckets
│   │   └── postgres.py            # casts, DISTINCT ON, jsonb, date_trunc
│   ├── clickhouse_store.py        # read-only client, memory settings, lock
│   ├── postgres_store.py          # plain-KV + TimescaleDB variant
│   └── metadata.py                # metadata/pgvector connection + migrations
│
├── rag/
│   ├── ingest.py                  # parse (pdf/docx/xlsx/html/md) → chunk → embed
│   ├── retriever.py               # pgvector cosine search, tenant+scope
│   ├── advisor.py                 # structured incident analysis mode
│   └── schema.sql
│
├── memory/
│   ├── sessions.py                # llm_sessions CRUD + deterministic titles
│   ├── history.py                 # llm_sql_history + supersedes lineage
│   └── digest.py                  # rolling session summaries ⛳
│
├── feedback/
│   ├── capture.py                 # thumbs up/down
│   ├── mining.py                  # auto-mine successful corrections
│   ├── promotion.py               # approve → examples (+embedding), decay
│   ├── graduation.py              # recurring-fix → registry change
│   └── schema.sql
│
├── onboarding/
│   ├── wizard.py                  # probe → naming → knowledge → activate
│   ├── probe.py                   # key/event introspection via TelemetryStore
│   ├── naming.py                  # suggestions + review queue
│   └── schema.sql
│
├── llm/
│   ├── client.py                  # LLMClient protocol + OpenAI-compatible impl
│   └── embeddings.py              # embed() (pluggable model)
│
├── config/
│   ├── settings.py                # env + YAML load, validation (fail-fast)
│   └── defaults.yaml              # flags, router indicators, column mapping
│
├── demo/
│   ├── seed_demo.py               # generate synthetic KV telemetry
│   ├── panel.py                   # Streamlit demo UI
│   └── questions.md               # scripted demo questions + expected results
├── tests/                         # §10
├── docker-compose.yml             # postgres+pgvector, clickhouse (optional), api
├── Dockerfile
├── .env.example
├── README.md
└── LICENSE
```

---

## 4. Core Contracts & Interfaces

These protocols are the load-bearing walls. Get them right in Phase 0 and phases 1–7 never touch them again.

### 4.1 `Dialect` — per-engine SQL rendering

Engine knowledge is *data*, not code branches. The prompt builder and validator ask the dialect; they never hardcode `toFloat64OrNull`.

```python
class Dialect(Protocol):
    name: str  # "clickhouse" | "postgres"

    def cast_numeric(self, value_expr: str) -> str:
        """Null-safe numeric cast of the EAV value column.
        clickhouse: toFloat64OrNull(value)
        postgres:   NULLIF(value, '')::double precision
        """

    def latest_value_expr(self, value_expr: str, ts_col: str) -> str:
        """'latest reading' aggregate.
        clickhouse: argMax(v, ts)
        postgres:   (array_agg(v ORDER BY ts DESC))[1]  -- or DISTINCT ON at query level
        """

    def json_field_float(self, json_col: str, field: str) -> str:
        """clickhouse: JSONExtractFloat(event_data, 'lat')
        postgres:   (event_data::jsonb->>'lat')::double precision
        """

    def time_bucket(self, ts_col: str, interval: str) -> str:
        """clickhouse: toStartOfInterval(ts, INTERVAL 1 HOUR)
        postgres:   date_trunc('hour', ts) / time_bucket(...) for Timescale
        """

    def now_minus(self, unit: str, n: int) -> str: ...
    def quote_ident(self, name: str) -> str: ...
    @property
    def sqlglot_name(self) -> str:
        """sqlglot dialect identifier for AST parsing ("clickhouse"/"postgres")."""

    def eav_system_rules(self, mapping: ColumnMapping) -> str:
        """Renders the dialect-specific EAV rules block injected into the
        system prompt (§7 of the blueprint) using mapped column names."""
```

### 4.2 `TelemetryStore` — the only door to tenant data

```python
class TelemetryStore(Protocol):
    dialect: Dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        """Read-only execution. Implementations MUST: use a read-only
        credential, enforce cap/timeout, and map engine errors to a
        normalized ConnectionError vs QueryError distinction."""

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        """DISTINCT key + count + min/max ts — powers onboarding probe
        and S5 key verification."""

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]: ...
```

One adapter per engine; constructed from config (`database/clickhouse_store.py`, `database/postgres_store.py`). The Postgres adapter serves **plain KV tables** and **TimescaleDB hypertables** — same interface, `time_bucket` renders differently.

### 4.3 `LLMClient` — provider pluggable

```python
class LLMClient(Protocol):
    def generate(self, system: str, user: str, *, temperature: float) -> GenResult: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

One OpenAI-compatible implementation covers OpenAI, Azure, Groq, OpenRouter, and local servers (llama.cpp/vLLM) — only `base_url` changes. No vendor SDK imports outside `llm/`.

### 4.4 `KnowledgeStore` — cached PG knowledge access

```python
class KnowledgeStore(Protocol):
    def load(self, tenant: str) -> TenantKnowledge:   # TTL+LRU cached
    def invalidate(self, tenant: str) -> None:         # write paths call this
    def alias_map(self, tenant: str) -> dict[str, KeyMapping]:
    def fetch_semantic_examples(self, tenant: str, query: str) -> list[SQLExample] | None:  # ⛳
    def is_onboarded(self, tenant: str) -> bool:
```

### 4.5 Pipeline stage signature

Every conversational stage is a pure-ish component with the same shape — easy to test, easy to flag-gate:

```python
@dataclass
class StageResult:
    handled: bool  # True → short-circuit pipeline
    rewritten_query: str | None
    payload: dict[str, Any]  # assumption_note, correction context, …


class Stage(Protocol):
    def evaluate(self, ctx: QueryContext) -> StageResult: ...
```

---

## 5. Configuration System

Two layers, loaded once at startup, validated fail-fast (no silent defaults for secrets):

1. **Environment** — credentials & deployment knobs (`.env.example` documents every key; see blueprint §15).
2. **`config/defaults.yaml`** — behavior: flags, router indicator lists, column mapping, per-key aggregation defaults.

**The file that makes Ctxora "anyone with KV data":**

```yaml
stores:
  telemetry:
    adapter: clickhouse                 # clickhouse | postgres
    # dsn from env: TELEMETRY_DB_*
    mapping:
      table: "{tenant}_telemetry"       # or a fixed table with a tenant column
      tenant_column: null               # set if one shared table instead of per-tenant
      timestamp: event_time             # ← your column names go here
      entity_id: device_id
      key: metric
      value: reading
      extra_dimensions: [site, region]  # optional GROUP BY dimensions
  events:
    adapter: clickhouse
    enabled: true
    mapping:
      table: "{tenant}_events"
      timestamp: occurred_at
      event_type: kind
      entity_id: device_id
      payload: attrs                    # JSON/JSONB column

agent:
  default_time_window: "today"
  row_cap: 1000
  query_timeout_s: 30
  aggregation_defaults:                 # assume-first (S3)
    "*": average
    "battery*": latest
    "*temperature*": average

flags:
  correction_loop: false
  assume_first: false
  session_digest: false
  streaming: true
  semantic_examples: false
  feedback_capture: false

routing:
  sql_indicators: [average, maximum, minimum, last, yesterday, today, trend, per, which, when, how many, …]
  rag_indicators:  [manual, how do i, specification, troubleshooting, policy, maintenance, …]
```

**Rule:** a user changing only this file (plus env) gets a working service on their data. If a use case forces them to edit Python, that's a design bug.

---

## 6. Data Contracts

Internal dataclasses shared across modules (in `api/schemas.py` / `database/contracts.py`) — keep them narrow and stable:

| Contract | Fields | Produced → Consumed |
|---|---|---|
| `QueryContext` | tenant, user_query, session_id, flags, auth claims | api → pipeline |
| `KeyMapping` | alias, canonical_key, physical_key, alternative_key | knowledge → resolver/prompt |
| `RetrievedKnowledge` | sections dict (index/tables/telemetry/aliases/rules/examples/…) | knowledge → prompt_builder |
| `GeneratedSQL` | sql, raw_response, tokens | generator → validator |
| `ValidationResult` | valid, errors, warnings, normalized_sql, repairs_applied | validator → executor/api |
| `ExecutionResult` | success, rows, row_count, column_names, execution_time_ms, error_kind | store → formatter |
| `AnswerEnvelope` | data, sql, summary, sessionId, historyId, tokenUsage, assumptionNote, followUps | pipeline → api |

---

## 7. Dialect Portability Matrix

Everything the agent knows about an engine, in one table (this is what `Dialect` implementations own):

| Capability | ClickHouse | PostgreSQL / Timescale |
|---|---|---|
| Numeric cast | `toFloat64OrNull(value)` | `NULLIF(value,'')::double precision` |
| Latest value | `argMax(cast(value), ts)` per group | `DISTINCT ON (entity)` + `ORDER BY ts DESC`, or `(array_agg(... ORDER BY ts DESC))[1]` |
| JSON field | `JSONExtractFloat(payload,'lat')` | `(payload::jsonb->>'lat')::double precision` |
| Time bucket | `toStartOfInterval(ts, INTERVAL 1 HOUR)` | `date_trunc('hour', ts)` / `time_bucket('1 hour', ts)` |
| Now minus | `now() - INTERVAL 1 DAY` | `now() - INTERVAL '1 day'` |
| Read-only statement gate | AST root allowlist (sqlglot) — identical for both engines | AST root allowlist (sqlglot) — identical for both engines |
| Read-only enforcement | read-only user + settings | read-only role + `default_transaction_read_only` |
| Memory guard | `max_bytes_before_external_*` | `statement_timeout`, `work_mem` discipline |
| KV table shape | MergeTree, `ORDER BY (entity, key, ts)` | plain table / Timescale hypertable + `(entity, key, ts)` index |

The validator's *generic* rules are engine-neutral and structural: it parses each statement with the dialect's sqlglot grammar, allows only SELECT-shaped roots, denies mutation/lock nodes anywhere in the tree, enforces the table allowlist (unqualified names only), and caps CTE depth; the dialect only supplies rendering (`sqlglot_name` picks the parse grammar). **Adding a third engine = one file in `database/dialects/` + one in `database/`** (e.g., DuckDB for single-file demos).

---

## 8. Phase Plan (v0.1 → v1.0)

Each phase: **scope · deliverables · acceptance demo**. Nothing later than its phase is built earlier; flags keep future stages dark.

### Phase 0 — Scaffold (repo skeleton)

**Scope:** runnable empty service + infra + contracts.
**Deliverables:** repo layout (§3); `config/settings.py` with YAML+env validation; `api/health.py`; `docker-compose.yml` (postgres+pgvector, clickhouse, api); `database/contracts.py` (all protocols, stubbed impls); `llm/client.py`; CI (lint + tests on push); `.env.example`; LICENSE; README skeleton.
**Acceptance:** `docker compose up` → `/healthz` 200, `/readyz` 200; `pytest` green with contracts imported; config validation rejects a bad YAML.

### Phase 1 — v0.1 Core NL→SQL (the heart)

**Scope:** S5→S13 minimal vertical slice, single demo tenant, ClickHouse adapter first.
**Deliverables:** `knowledge/` (schema.sql + store.py with cache + demo seed); `agent/` pipeline, key_resolver, knowledge_retriever (keyword slicing), prompt_builder (dialect-rendered system rules), generator, validator (generic rules + dialect patterns + one auto-repair pass), executor (read-only, row cap, timeout), formatter, summarizer; `api/query.py` sync endpoint; `demo/seed_demo.py` (synthetic trucks: speed, rpm, coolant temp, fuel, battery, plus a few event types).
**Acceptance demo:** *"What was the average RPM of truck-102 yesterday?"* → resolved key `engine.rpm` (via seeded alias "rpm"), generated SQL passes validator, executes on ClickHouse, answer with summary + assumption note. *"Delete all telemetry"* → validator rejection.
**Copy-heavy:** validator, prompt section layout, loader — see §9 map.

### Phase 2 — v0.2 Memory + streaming + onboarding probe

**Scope:** conversations persist; answers stream; introspection endpoint.
**Deliverables:** `memory/sessions.py` + `history.py` (+ DDL); `api/query.py` SSE stream (stage/summary_delta/final/error/ping events, identical final payload as sync); `onboarding/probe.py` + `GET /v1/onboarding/{tenant}/readiness`-lite; `POST /v1/query/sql/stream` UI-usable.
**Acceptance demo:** two-turn conversation in the same `sessionId` visible in `/v1/history`; SSE stream shows stages then deltas; probe endpoint lists distinct keys for demo tenant.

### Phase 3 — v0.3 Conversational intelligence (flags on)

**Scope:** S1–S3 behind `flags:` — correction, follow-up, assume-first.
**Deliverables:** `agent/correction.py` (regex triggers + guards, LLM fallback, one-regen cap, temp 0.3 on repair, `supersedes_id` lineage), `agent/followup.py` (pronoun rewrite from history), `agent/assume_first.py` (aggregation/time/entity defaults from `agent.aggregation_defaults` + history; assumption_note), `memory/digest.py`.
**Acceptance demo:** ask → get answer → *"no, I meant maximum"* → repaired SQL, supersede lineage visible in history; *"what about truck-103?"* resolves entity from context; unaggregated question returns an answer with an assumption note, not a clarification.

### Phase 4 — v0.4 Feedback flywheel

**Scope:** capture → mine → review → promote → decay, all inside the PG knowledge store.
**Deliverables:** `feedback/` (capture, mining on successful corrections, admin approve/reject with token gate, promotion into `sql_agent_sql_examples` + embedding, decay demotion, graduation queue, golden-eval export); `api/feedback_admin.py`.
**Acceptance demo:** thumbs-down + comment → appears in pending queue → approve → example row has embedding and `approved`; next similar question retrieves it (keyword path); correction of a query whose SQL matches an approved example → example demoted to `review`.

### Phase 5 — v0.5 Document RAG + hybrid routing + advisor

**Scope:** the "docs" half of the product.
**Deliverables:** `rag/` (ingest pdf/docx/xlsx/html/md, chunk, embed, pgvector retriever with tenant+scope), `routing/` (keyword router from config), `POST /v1/query` unified entry, `rag/advisor.py` structured JSON incident analysis, `api/documents.py`.
**Acceptance demo:** upload a maintenance manual → *"what's the acceptable coolant temperature range?"* routed to RAG with sources; *"which trucks exceeded it yesterday, and what should I do?"* → hybrid answer (rows + doc-grounded actions).

### Phase 6 — v0.6 Backend pluralism + semantic retrieval

**Scope:** make "any KV store" true, and few-shot smart.
**Deliverables:** `database/postgres_store.py` + `database/dialects/postgres.py` (plain KV + Timescale `time_bucket`); config-driven adapter selection; demo tenant re-seeded into Postgres and full test suite re-run against **both** adapters (suite parameterized by adapter); `flags.semantic_examples` on: cosine retrieval (≥0.85, top-2, usage stats) over approved examples, keyword fallback.
**Acceptance demo:** flip `adapter: postgres` in YAML, same question set, same answers (modulo dialect SQL shown to user).

### Phase 7 — v1.0 Polish & release

**Scope:** production-hardening and story.
**Deliverables:** onboarding wizard completion (naming + review queue + activation gating); auth module (verified-JWT tenant claim, dev-mode fallback); rate limiting; OpenAPI docs + README with GIFs; `demo/panel.py` Streamlit; Helm chart; golden regression suite wired to CI; LICENSE + CONTRIBUTING.
**Acceptance:** fresh-clone → `docker compose up` → seed → ask 10 scripted questions in `demo/questions.md` → all green in one command.

---

## 9. Copy Map: Production Reference → Ctxora

> The detailed file-by-file reference (which production module informs which
> Ctxora module, plus the do-not-copy list) lives in
> `docs/internal/REBUILD_NOTES.md` — **internal, gitignored, never published**.
> Rebuild against YOUR production implementation; strip every identifier.

| Ctxora module | Action |
|---|---|
| `agent/validator.py`, `knowledge/store.py`, `agent/prompt_builder.py` | rewrite-from-reference (keep the mechanisms: rule engine, cache, section layout) |
| `agent/key_resolver.py`, `generator.py`, `memory/*`, `feedback/*` | adapt: same flow, generic vocabulary, config-driven lists |
| `rag/*` | near-port: already vendor-neutral concepts |
| `api/*`, `main.py`, `agent/pipeline.py`, `demo/*` | fresh (thin routes, staged orchestration, synthetic data only) |
| `database/dialects/*`, `llm/*`, `config/*`, `tests/*` | fresh by design |

---

## 10. Testing Layout

```text
tests/
├── unit/
│   ├── test_validator.py          # every rule + every auto-repair, per dialect (parametrized)
│   ├── test_dialects.py           # rendering matrix vs §7 golden strings
│   ├── test_key_resolver.py       # alias→canonical→physical, verification gate
│   ├── test_correction_triggers.py# triggers AND guards ("no data" ≠ complaint)
│   ├── test_assume_first.py       # default fill, assumption note, clarify-only-when-stuck
│   ├── test_knowledge_cache.py    # TTL expiry, LRU eviction, invalidation, metrics
│   └── test_prompt_builder.py     # section order, dialect rules injection, token slicing
├── integration/
│   ├── conftest.py                # docker-compose-backed PG + CH fixtures, demo seed
│   ├── test_pipeline_e2e.py       # 20-question golden set, run per adapter
│   ├── test_streaming.py          # SSE event sequence + final == sync payload
│   ├── test_flywheel.py           # capture→mine→approve→retrieve→decay
│   └── test_onboarding.py         # probe→naming→gating
├── regression/
│   └── test_golden_eval.py        # questions exported from approved feedback
└── fakes.py                       # FakeLLM (scripted SQL), FakeStore (canned rows)
```

**Rule:** unit tests never touch a network. Pipeline e2e runs against both adapters — the Postgres run is what keeps "any KV store" honest from Phase 6 onward.

---

## 11. Dependencies

```text
# core
fastapi
uvicorn[standard]
gunicorn
pydantic>=2
pydantic-settings
python-dotenv
pyyaml

# storage
psycopg[binary]            # metadata + pgvector
pgvector                    # vector type for psycopg
clickhouse-connect          # telemetry adapter (optional install extra)

# llm
openai>=1                   # OpenAI-compatible client (any base_url)
httpx

# rag ingestion
pymupdf
python-docx
openpyxl
beautifulsoup4

# demo / dev
streamlit
pytest
pytest-asyncio
ruff
```

Keep `clickhouse-connect` an optional extra (`pip install ctxora[clickhouse]`) — a Postgres-only user shouldn't carry it.

---

## 12. Conventions & Guardrails

- **Types:** strict Pydantic v2 at the boundary; protocols + dataclasses inside; no `Any`, no suppressed errors.
- **Size ceiling:** ~250 LOC per module; a module that grows past it splits (the company `custom_sql.py` route file is the cautionary tale — orchestration lives in `agent/pipeline.py`, routes stay thin).
- **Flags:** every post-v0.1 stage is flag-gated and defaults **off**; the v0.1 path must never import disabled stages' modules.
- **Engine-agnostic core:** a PR that imports `clickhouse_connect` or `psycopg` anywhere outside `database/` is rejected. Same for `openai` outside `llm/`.
- **Sanitization standing rule:** before any commit, run the leak grep with YOUR identifier list (kept in `docs/internal/REBUILD_NOTES.md`, never committed) over the whole repo; it must return nothing.
- **Commit hygiene:** conventional commits; one phase = one merge train of small commits; every phase ends with the acceptance demo recorded in `demo/questions.md` results.
- **No secrets in repo:** `.env.example` only; CI scans for key patterns.

---

*Build order memory hook: **contracts (0) → slice (1) → memory (2) → conversation (3) → flywheel (4) → docs/RAG (5) → plural backends (6) → polish (7).*** 
