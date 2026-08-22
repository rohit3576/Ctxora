# DataMind

**An open-source Agentic NL→SQL + RAG engine for high-cardinality telemetry and time-series data.**

> Ask plain-English questions about IoT / fleet / industrial / sensor data stored as key-value telemetry — DataMind finds the right keys, generates safe SQL, validates it, runs it, and explains the result.

```text
"What was the average RPM of truck 102 yesterday?"
                ↓
   DataMind agent pipeline
                ↓
"The average RPM of truck 102 yesterday was 1,487."
```

This document is the **complete implementation blueprint**. It describes every capability, the data model, the agent pipeline stage-by-stage, the retrieval/RAG layer, the API surface, configuration, deployment, and a rebuild checklist — in a fully **vendor-neutral, company-agnostic** way. Anyone with timestamp + key + value style data can stand this up against their own database.

---

## Table of Contents

1. [Why DataMind Exists](#1-why-datamind-exists)
2. [Positioning & Audience](#2-positioning--audience)
3. [System Architecture](#3-system-architecture)
4. [Data Model](#4-data-model)
5. [The NL→SQL Agent Pipeline (Stage by Stage)](#5-the-nlsql-agent-pipeline-stage-by-stage)
6. [Knowledge Store (PostgreSQL)](#6-knowledge-store-postgresql)
7. [Prompt Engineering Guide](#7-prompt-engineering-guide)
8. [SQL Validation & Safety Rules](#8-sql-validation--safety-rules)
9. [Document RAG Layer (pgvector)](#9-document-rag-layer-pgvector)
10. [Hybrid Intent Routing (SQL vs RAG)](#10-hybrid-intent-routing-sql-vs-rag)
11. [Conversation Memory: Sessions, History, Digests](#11-conversation-memory-sessions-history-digests)
12. [Feedback Flywheel (Continuous Learning)](#12-feedback-flywheel-continuous-learning)
13. [Self-Service Onboarding Wizard](#13-self-service-onboarding-wizard)
14. [API Surface](#14-api-surface)
15. [Configuration Reference](#15-configuration-reference)
16. [Security & Multi-Tenancy Model](#16-security--multi-tenancy-model)
17. [Deployment](#17-deployment)
18. [Observability & Performance](#18-observability--performance)
19. [Testing Strategy](#19-testing-strategy)
20. [Project Structure (Target Repo Layout)](#20-project-structure-target-repo-layout)
21. [Roadmap](#21-roadmap)
22. [Rebuild Checklist (From a Proprietary Implementation)](#22-rebuild-checklist-from-a-proprietary-implementation)

---

## 1. Why DataMind Exists

Telemetry, IoT, fleet, and observability systems converge on a hard problem:

- Data arrives as **high-cardinality key-value observations** (`timestamp + device + key + value`), because devices and sensors never share one fixed schema.
- New metrics appear constantly — you can't ALTER TABLE every time a new sensor shows up.
- The people who need answers (operations, fleet managers, plant engineers) **don't know SQL and don't know the key names** ("is it `engine.rpm`, `rpm`, or `engineRpm`?").
- Cramming 500 telemetry keys into an LLM prompt doesn't scale, degrades accuracy, and costs tokens.

Generic text-to-SQL tools fail here because they don't understand:

- **EAV (entity-attribute-value) telemetry schemas** — where every metric is a row, not a column.
- **Key vocabulary resolution** — mapping "how fast" → `speed`, "engine temp" → `engine.coolantTemp`.
- **Analytical-engine dialects** — ClickHouse-style functions (`toFloat64OrNull`, `argMax`, `JSONExtract*`), memory-bounded aggregations, MergeTree ordering.
- **Conversational context** — "what about truck 102?" following "average speed of the fleet yesterday?".

DataMind is a purpose-built agent stack that solves exactly this: **natural-language querying over arbitrary key-value telemetry**, with schema-aware retrieval (RAG over your own keys/rules/examples), validated read-only SQL generation, and a feedback loop that gets better with use.

```text
timestamp           key                 value
------------------------------------------------------
2026-08-18 10:01    rpm                 1450
2026-08-18 10:01    speed               63
2026-08-18 10:01    engine.coolantTemp  82
2026-08-18 10:02    rpm                 1520
```

## 2. Positioning & Audience

**Not** "another RAG chatbot." DataMind's core problem statement:

> *Let users ask natural-language questions against arbitrary, high-cardinality telemetry / key-value data without knowing the schema, the key names, or SQL.*

RAG is one component (schema/key retrieval + document Q&A), not the product.

**Who can deploy it:**

| Domain | Example questions |
|---|---|
| IoT / connected devices | "When did sensor-7 last report?" |
| Fleet / vehicle telemetry | "Max speed of truck 123 yesterday?" |
| Industrial / manufacturing | "Average line voltage on machine 12 this week?" |
| Energy / utilities | "Total consumption per meter in July?" |
| Smart buildings / cold chain | "Which freezers exceeded -18°C last night?" |
| Observability / metrics | "P99 latency per service this hour?" (same EAV shape) |

**Supported backends (by design):** ClickHouse first (best fit for high-cardinality telemetry), PostgreSQL/TimescaleDB next; the data-access layer is intentionally adapter-shaped.

## 3. System Architecture

```text
                        ┌────────────────────────┐
                        │      User Question      │
                        └───────────┬────────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │  Conversational Front Matter  │
                    │  greeting · correction ·      │
                    │  follow-up resolve ·          │
                    │  assume-first defaults        │
                    └───────────────┬───────────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │       Intent Router           │
                    │  (data question vs doc question│
                    │   vs hybrid)                  │
                    └──────┬───────────────┬────────┘
                           ▼               ▼
             ┌──────────────────┐  ┌──────────────────┐
             │   SQL AGENT      │  │   DOCUMENT RAG   │
             │                  │  │                  │
             │ key resolution   │  │ chunk + embed    │
             │ (NL → canonical  │  │ (pgvector,       │
             │  telemetry keys) │  │  cosine search)  │
             │                  │  │                  │
              │ knowledge        │  │ grounded answer  │
              │ retrieval (PG    │  │ + sources        │
              │ store, cached)   │  └──────────────────┘
             │                  │
             │ prompt assembly  │
             │                  │
             │ LLM SQL gen      │
             │                  │
             │ SQL validation   │
             │ + auto-repair    │
             │                  │
             │ read-only exec   │
             │ (ClickHouse)     │
             │                  │
             │ NL summary       │
             └────────┬─────────┘
                      ▼
        ┌──────────────────────────────────┐
        │  Answer + SQL + rows + session   │
        └──────────────────┬───────────────┘
                           ▼
        ┌──────────────────────────────────┐
        │  MEMORY + FEEDBACK FLYWHEEL      │
        │  sessions · history · thumbs     │
        │  up/down · auto-mined repairs    │
        │  → reviewed few-shot examples    │
        └──────────────────────────────────┘
```

**Storage split:**

| Store | Holds |
|---|---|
| Analytical engine (ClickHouse) | `{tenant}` telemetry (EAV) + events tables — the queried data |
| **Knowledge store (PostgreSQL)** | the **entire SQL knowledge base**: tenants, telemetry key registry, aliases, business rules, few-shot examples, schema columns, event types, table relationships & metadata |
| Metadata store (PostgreSQL) | sessions, history, feedback, onboarding state (same cluster) |
| Vector store (PostgreSQL + pgvector) | document chunks for RAG; embeddings on approved few-shot examples |
| Filesystem / object store | uploaded source documents (pre-ingestion) only — no knowledge base on disk |

**Runtime:** FastAPI (sync + SSE streaming endpoints), Gunicorn + Uvicorn workers, containerized, K8s-ready with health probes.

## 4. Data Model

### 4.1 Telemetry (analytical engine, per tenant)

EAV model — one row per observation:

```sql
CREATE TABLE {tenant}_telemetry (
    timestamp   DateTime64(3),
    device_id   String,                      -- entity: vehicle, machine, meter...
    key         LowCardinality(String),      -- metric name, e.g. 'speed', 'engine.rpm'
    value       String                       -- raw value; cast at query time
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, key, timestamp);
```

Why `value String`: devices report mixed types (numbers, strings, states). Numeric access is always explicit and null-safe:

```sql
toFloat64OrNull(value)   -- arithmetic / aggregations
argMax(toFloat64OrNull(value), timestamp)   -- "latest" reading per device
```

**Configurable mapping** (so users don't modify the agent):

```yaml
database:
  type: clickhouse          # postgres | timescale later
telemetry:
  table: "{tenant}_telemetry"
  columns:
    timestamp: timestamp
    entity_id: device_id
    key: key
    value: value
events:
  table: "{tenant}_events"
  columns:
    timestamp: timestamp
    event_type: event_type
    payload: event_data
```

### 4.2 Events (analytical engine, per tenant)

Discrete happenings (alerts, state transitions) rather than samples:

```sql
CREATE TABLE {tenant}_events (
    timestamp   DateTime64(3),
    event_type  LowCardinality(String),   -- 'trip_start', 'overspeed', 'zone_enter'...
    category    LowCardinality(String),
    device_id   String,
    event_data  String                    -- JSON payload
)
ENGINE = MergeTree
ORDER BY (event_type, timestamp);
```

JSON fields are read with `JSONExtract*()`:

```sql
JSONExtractFloat(event_data, 'lat')
```

### 4.3 Metadata store (PostgreSQL)

```sql
-- Conversation sessions
CREATE TABLE llm_sessions (
    id          UUID PRIMARY KEY,
    tenant      VARCHAR(255) NOT NULL,
    user_email  VARCHAR(255),
    title       VARCHAR(255),          -- deterministic, keyword-derived
    created_at  TIMESTAMP DEFAULT now()
);

-- Every executed query, linked into conversations;
-- supersedes_id chains correction turns
CREATE TABLE llm_sql_history (
    id            SERIAL PRIMARY KEY,
    tenant        VARCHAR(255) NOT NULL,
    session_id    UUID,
    nl_query      TEXT,
    sql           TEXT,
    data          JSONB,
    summary       TEXT,
    token_usage   INTEGER,
    user_email    VARCHAR(255),
    supersedes_id INTEGER REFERENCES llm_sql_history(id),
    created_at    TIMESTAMP DEFAULT now()
);

-- The SQL knowledge base itself (telemetry key registry, aliases, business
-- rules, few-shot examples, schema columns, event types, relationships,
-- table metadata) lives in the sql_agent_* tables — full DDL in §6.

-- User feedback on answers (see §12)
CREATE TABLE query_feedback (
    id             SERIAL PRIMARY KEY,
    tenant         VARCHAR(255) NOT NULL,
    history_id     INTEGER REFERENCES llm_sql_history(id),
    feedback_type  VARCHAR(20),              -- 'positive' | 'negative'
    user_comment   TEXT,
    corrected_sql  TEXT,
    status         VARCHAR(20)               -- pending|auto_pending|approved|rejected
);

-- Onboarding wizard state (see §13)
CREATE TABLE onboarding_state (
    id            SERIAL PRIMARY KEY,
    tenant        VARCHAR(255) UNIQUE NOT NULL,
    current_step  VARCHAR(50),
    step_data     JSONB,
    activation_state VARCHAR(20) DEFAULT 'OFF'
);
```

### 4.4 Vector store (pgvector)

```sql
CREATE TABLE rag_documents (
    id               UUID PRIMARY KEY,
    tenant           VARCHAR(255) NOT NULL,
    filename         VARCHAR(512),
    file_hash        VARCHAR(64),
    embedding_model  VARCHAR(255),
    chunk_size       INTEGER,
    chunk_overlap    INTEGER,
    status           VARCHAR(20) DEFAULT 'ACTIVE',
    version          INTEGER DEFAULT 1,
    created_at       TIMESTAMP DEFAULT now(),
    UNIQUE (tenant, file_hash)
);

CREATE TABLE rag_chunks (
    id            UUID PRIMARY KEY,
    document_id   UUID REFERENCES rag_documents(id) ON DELETE CASCADE,
    tenant        VARCHAR(255),
    scope         VARCHAR(255),        -- optional shared/global scope
    page_number   INTEGER,
    chunk_index   INTEGER,
    chunk_text    TEXT,
    section_title TEXT,
    chunk_hash    VARCHAR(64),
    embedding     VECTOR(1536),        -- dims must match EMBEDDING_MODEL
    metadata      JSONB,
    created_at    TIMESTAMP DEFAULT now()
);

CREATE INDEX ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON rag_chunks (tenant);
CREATE INDEX ON rag_chunks (document_id);
```

## 5. The NL→SQL Agent Pipeline (Stage by Stage)

Full request flow, in order. Stages marked ⛳ are behind feature flags (§15).

```text
user_query
  │
  ├─ S0  greeting / small-talk shortcut         (regex, zero-LLM)
  ├─ S1  correction detection ⛳                 (regex triggers + guards → LLM fallback)
  ├─ S2  follow-up resolution ⛳                 (pronoun rewrite from session history)
  ├─ S3  assume-first defaults ⛳                (fill aggregation/time/entity defaults)
  ├─ S4  intent routing                         (SQL vs RAG vs hybrid)
  ├─ S5  canonical key resolution               (NL words → registered telemetry keys)
  ├─ S6  knowledge retrieval                    (PG knowledge store, cached)
  ├─ S7  prompt assembly                        (deterministic section layout)
  ├─ S8  SQL generation                         (LLM, temp=0.0)
  ├─ S9  validation + auto-repair               (rule engine, may loop once)
  ├─ S10 execution                              (read-only, memory-bounded)
  ├─ S11 result formatting                      (rows → list-of-dicts)
  ├─ S12 NL summary                             (streamable)
  └─ S13 history write + feedback mining        (non-blocking)
```

### S0 — Greeting / small-talk shortcut

A regex list (`hi`, `hello`, `thanks`, `bye`, `good morning`…) short-circuits the whole pipeline: a canned friendly reply is stored in history and returned. Zero LLM cost.

### S1 — Correction detection ⛳ `ENABLE_CORRECTION_LOOP`

Detects "your last answer was wrong" turns so the agent repairs instead of starting fresh.

- **Regex triggers:** "no", "wrong", "incorrect", "i meant", "instead of", "try again"…
- **Regex guards:** phrases like "no data", "no results", "now", "know" are *not* complaints — they demote to LLM classification.
- **LLM fallback classifier** (temp 0.0) decides: `is_correction` + extracts `corrected_question`.
- **Repair flow:** previous turn is marked superseded (`supersedes_id`), regeneration runs at temperature **0.3** (not 0.0 — avoid reproducing the identical wrong SQL).
- **Safety cap:** exactly **one regeneration** per complaint. A second consecutive failure → the agent asks a targeted clarifying question (which entity / which aggregation / which day?) instead of guessing again.

### S2 — Follow-up resolution ⛳ `ENABLE_SESSION_DIGEST`

Turns pronoun shorthand into standalone queries:

- "what was its average speed?" + prior turn about `truck_102` → resolved query mentions `truck_102` explicitly.
- Loads last N turns from `llm_sql_history` for the session; rewrites "it"/"that"/"this" with the last entity and last metric.
- Long sessions get a compact **session digest** (rolling summary) instead of raw turns — keeps prompt size bounded.

### S3 — Assume-first defaults ⛳ `ENABLE_ASSUME_FIRST`

Most telemetry questions omit dimensions. Rather than bouncing clarifying questions back, the resolver **fills defaults and says so**:

| Missing dimension | Default source | Example |
|---|---|---|
| Aggregation | per-key registry (`speed→average`, `battery→latest`, `rpm→average`) | "average speed" |
| Time window | tenant default (e.g., "today") | "since midnight" |
| Entity | last-discussed in session, else tenant default device | "truck 102" |

Output includes an `assumption_note` ("assuming **average** speed for **today**") surfaced in the answer. **Clarify only when nothing is resolvable** — and even then the clarification suggests a default.

### S4 — Intent routing

A lightweight keyword/indicator router (configurable) classifies the question:

- **Data question** → SQL agent ("speed", "rpm", "yesterday", "maximum", "which device"…)
- **Document question** → RAG ("manual", "how do I", "specification", "troubleshooting", "maintenance"…)
- **Hybrid** → both, merged answer.

Indicators live in config — never hardcoded to one domain. (An LLM classifier is a pluggable upgrade; the keyword router is free and deterministic.)

### S5 — Canonical key resolution

The signature move for key-value data:

1. Load the tenant's **alias map** from the knowledge store (`sql_agent_aliases`, served through the cached loader): `"engine temp" → engine.coolantTemp`, `"revs" → engine.rpm`.
2. Verify candidate keys actually exist in the tenant's telemetry table (query distinct keys) — drop hallucinated ones.
3. Prefer canonical key; fall back to known alternative if canonical has no data.

Output: `resolved_keys` — the exact strings that will appear in `WHERE key = '...'`.

### S6 — Knowledge retrieval (schema RAG from the PG knowledge store)

The tenant's knowledge is loaded **from PostgreSQL** (`sql_agent_*` tables, §6) — a single connection fetches all sections, defensively renders each one, and serves them through a TTL + LRU cache. The retriever then slices by the resolved keys:

- **telemetry registry** → only definitions for matched keys (not all 500)
- **few-shot examples** → 1–2 per matched key via deterministic keyword matching; **or** ⛳ `ENABLE_SEMANTIC_EXAMPLES`: cosine retrieval over *approved, embedded* examples (similarity ≥ 0.85, top-2, usage stats bumped per hit) — fails open to the keyword path
- **business rules** → all rules (engine-level invariants, few in number)
- **schema columns + table metadata** → schema definition
- event-type keywords (e.g., "trip", "alert", "zone") pull event-focused examples too.

### S7 — Prompt assembly

Deterministic section order (§7 has the full skeleton): system rules → schema → matched telemetry definitions → business rules → few-shot examples → resolved aliases → conversation history (3–5 turns, summaries not raw SQL) → user query.

### S8 — SQL generation

- LLM call, **temperature 0.0** (0.3 for correction re-runs).
- Contract: *"Output MUST contain exactly ONE SQL statement inside a ```sql fenced block."*
- Parser extracts the fenced block; token usage recorded.
- Provider-pluggable (any OpenAI-compatible endpoint; local models work).

### S9 — Validation + auto-repair

Rule engine (full spec §8). On failure it first **auto-repairs** (cast wraps, GROUP BY injection, table-name correction) and re-validates once; hard violations (forbidden statements, out-of-scope tables) are rejected outright.

### S10 — Execution

- **Read-only** connection/user.
- Serialized execution (threading lock) with memory guards:

```sql
SETTINGS max_bytes_before_external_group_by = 500000000,
         max_bytes_before_external_sort     = 500000000
```

- Distinguishes *connection* failures (return "SQL generated but database unreachable" — still valuable output) from *query* errors (feed back into repair).
- Row cap + execution timeout per query.

### S11 — Result formatting

Tabular result → `[{column: value}, …]` preserving types (float/int/datetime/null), plus row count, column names, execution time.

### S12 — NL summary generation

The rows + the original question + (correction context, session digest if present) go to a summarizer prompt: 2–4 sentence plain-English answer. **Streamable** as SSE deltas (§14). Streaming failure falls back to sync summary — never fails the request.

### S13 — History write + feedback mining

- Query/SQL/rows/summary/token usage persisted to `llm_sql_history` (non-blocking; a DB hiccup never fails the answer).
- If this turn was a **successful correction**, an `auto_pending` feedback row is mined automatically (question → corrected SQL pair) — the flywheel's fuel (§12).

## 6. Knowledge Store (PostgreSQL)

The SQL knowledge base lives **entirely in PostgreSQL** — no markdown files, no filesystem dependency, no dual source of truth. (A markdown KB was the original design; it was migrated into these tables once and the markdown path removed. Tenants that aren't onboarded get a typed "metadata not configured" error → 4xx at the API boundary, never a silent fallback.)

### 6.1 Schema

One tenant registry + nine knowledge tables, all FK-cascaded from the tenant row:

```sql
CREATE TABLE sql_agent_tenants (
    id                SERIAL PRIMARY KEY,
    tenant_name       VARCHAR(50) UNIQUE NOT NULL,
    display_name      VARCHAR(100),
    status            VARCHAR(20) DEFAULT 'active',
    eav_rules_text    TEXT,              -- per-tenant EAV querying preamble
    onboarding_answers JSONB,
    created_at        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE sql_agent_telemetry_registry (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    canonical_key       VARCHAR(100) NOT NULL,   -- 'engine.coolantTemp'
    physical_key        VARCHAR(100),            -- actual key string in the DB, if different
    description         TEXT,
    datatype            VARCHAR(50),             -- numeric | boolean | categorical | ...
    unit                VARCHAR(50),
    aggregation         VARCHAR(255),            -- 'average' | 'latest' | 'max' ...
    cast_pattern        VARCHAR(255),            -- e.g. argMax(toFloat64OrNull(value), timestamp)
    typical_range       VARCHAR(100),
    operational_meaning TEXT,                    -- what an operator should infer from it
    verified            BOOLEAN DEFAULT TRUE,
    provenance          VARCHAR(50),             -- 'existing' | 'onboarded' | 'feedback'
    UNIQUE (tenant_id, canonical_key)
);

CREATE TABLE sql_agent_aliases (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    alias            VARCHAR(100) NOT NULL,      -- 'engine temp'
    canonical_key    VARCHAR(100) NOT NULL,
    alternative_key  VARCHAR(100),               -- fallback if canonical has no data
    owning_table     VARCHAR(100),
    UNIQUE (tenant_id, alias)
);

CREATE TABLE sql_agent_business_rules (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    rule_number    INTEGER NOT NULL,
    rule_text      TEXT NOT NULL,                -- 'Title: description' convention
    doc_reference  VARCHAR(255),
    UNIQUE (tenant_id, rule_number)
);

CREATE TABLE sql_agent_sql_examples (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    question                TEXT NOT NULL,       -- NL question (embedding key)
    sql_query               TEXT NOT NULL,
    tags                    VARCHAR(255),
    tables_used             VARCHAR(255),
    intent                  TEXT,
    query_category          VARCHAR(100),        -- telemetry | events | multi-table
    embedding               VECTOR(1536),        -- for semantic few-shot retrieval
    status                  VARCHAR(20) DEFAULT 'approved',
                            -- approved | pending | review | rejected
    provenance_feedback_id  INTEGER,             -- back-link when promoted from feedback
    embedding_model         VARCHAR(50),
    use_count               INTEGER DEFAULT 0,
    last_used_at            TIMESTAMP,
    corrections_after_use   INTEGER DEFAULT 0    -- decay signal
);

CREATE TABLE sql_agent_schema_columns (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    table_name          VARCHAR(100) NOT NULL,
    column_name         VARCHAR(100) NOT NULL,
    datatype            VARCHAR(100) NOT NULL,
    description         TEXT,
    UNIQUE (tenant_id, table_name, column_name)
);

CREATE TABLE sql_agent_event_types (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    event_type             VARCHAR(100) NOT NULL,   -- 'overspeed', 'zone_enter'...
    category               VARCHAR(100),
    alert_values           VARCHAR(255),
    description            TEXT,
    event_details_pattern  TEXT,
    event_data_schema      TEXT,                    -- JSON field documentation
    extraction_patterns    TEXT,                    -- JSONExtract* guidance
    duration_column_note   TEXT,
    UNIQUE (tenant_id, event_type)
);

CREATE TABLE sql_agent_table_relationships (
    id                    SERIAL PRIMARY KEY,
    tenant_id             INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    source_table          VARCHAR(100) NOT NULL,
    target_table          VARCHAR(100) NOT NULL,
    join_keys             TEXT NOT NULL,
    cardinality           VARCHAR(50),
    recommended_join_type TEXT,
    description           TEXT,
    business_purpose      TEXT,
    notes                 TEXT,
    UNIQUE (tenant_id, source_table, target_table)
);

CREATE TABLE sql_agent_table_metadata (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    table_name             VARCHAR(100) NOT NULL,
    fully_qualified_name   VARCHAR(200),
    table_type             VARCHAR(100),
    purpose                TEXT,
    time_column            VARCHAR(100),
    primary_identifiers    TEXT,
    tenant_scope_column    VARCHAR(100),
    important_notes        TEXT,
    storage_characteristics TEXT,
    UNIQUE (tenant_id, table_name)
);

-- Human-review queue: LLM-extracted key-mapping candidates from onboarding docs.
-- NEVER read by the SQL agent or router — only by the review/approve action,
-- which promotes approved rows into telemetry_registry / aliases.
CREATE TABLE sql_agent_key_mapping_candidates (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    canonical_key  VARCHAR(100),
    physical_key   VARCHAR(100),
    alias          VARCHAR(100),
    confidence     NUMERIC(3,2),
    source_doc     VARCHAR(255),
    status         VARCHAR(20) DEFAULT 'pending',
    extracted_at   TIMESTAMP DEFAULT NOW(),
    reviewed_at    TIMESTAMP,
    UNIQUE (tenant_id, canonical_key, alias)
);
```

### 6.2 Loader behavior (`DBKnowledgeBaseLoader`)

- **Single-connection load** — one connection/transaction fetches all sections for a tenant.
- **Defensive rendering** — each section renders independently; a failed section degrades to `""` (a partial KB beats a 500). If the PG fetch itself fails, an empty-but-valid KB shape is returned.
- **Thread-safe class-level cache** — TTL (default 300 s) + LRU eviction (default 100 tenants), shared across threads/instances, with lazy expiration cleanup.
- **Invalidation on write** — onboarding writes, promotions, and graduations call `invalidate_cache(tenant)` so changes are visible immediately.
- **Onboarded gate** — a tenant with no registry rows raises a typed error → 4xx; the agent never runs on unconfigured tenants.
- **Metrics** — cache hits/misses/invalidations/evictions and cumulative PG load time, exposed for monitoring.

### 6.3 Prompt reconstruction

Rows are rendered into the prompt's section structure at load time (in-memory strings — markdown is just the prompt's wire format now, not a storage format):

| Section | Source table(s) |
|---|---|
| `INDEX` | tenant row + table metadata |
| `TABLES` | `schema_columns` + `table_metadata` |
| `TELEMETRY` | `telemetry_registry`, prefaced by the tenant's `eav_rules_text` |
| `ALIASES` | `aliases` (grouped: synonyms → canonical | alternative) |
| `BUSINESS_RULES` | `business_rules` |
| `JOINS` | `table_relationships` |
| `QUERY_EXAMPLES` | `sql_examples` (categorized: telemetry / events / multi-table) |
| `EVENTS` *(optional)* | `event_types` |
| `RELATIONSHIPS` *(optional)* | `table_relationships` |

### 6.4 Population paths

1. **Onboarding wizard** (§13) — probe → naming → promotion from the review queue.
2. **Feedback flywheel** (§12) — approved corrections insert approved+embedded examples directly.
3. **Seed SQL** — a demo tenant ships as idempotent INSERT scripts (executable documentation).
4. *(Historical)* a one-time migration script imported the legacy markdown KB — retained only as an import utility, not a runtime dependency.

## 7. Prompt Engineering Guide

Seven techniques, all implemented:

1. **Deterministic system instructions** — EAV access rules are hardcoded (only table names are parameterized). The LLM never "decides" how to read telemetry; it's told.
2. **Metric-sliced few-shot** — only examples for keys resolved from this question. Prompt stays small no matter how big the registry grows.
3. **Business-rule injection** — engine invariants travel with every prompt, so constraints like memory-bounding CTEs survive.
4. **Alias resolution up front** — the prompt lists the *resolved canonical keys*, eliminating the #1 failure mode (wrong key strings).
5. **History as summaries** — 3–5 prior turns, compressed; context without token bloat.
6. **Temperature discipline** — 0.0 for generation (reproducible), 0.3 for correction re-runs (escape the wrong-SQL attractor).
7. **Structured output contract** — one statement, one fenced block; extraction is trivial and robust.

**System-prompt skeleton (abridged, generic):**

```text
You are a precise SQL generator for a {engine} telemetry database.

TABLES
  {telemetry_table}(timestamp, device_id, key, value)  -- EAV samples
  {events_table}(timestamp, event_type, category, device_id, event_data)  -- events

TELEMETRY RULES
  - Filter metrics with key = '<metric_key>'
  - ALL numeric math on value must use toFloat64OrNull(value)
  - Latest value per device: argMax(toFloat64OrNull(value), timestamp)
  - Multi-metric CTEs: bound each CTE with a timestamp filter

EVENT RULES
  - Filter with event_type = '<event_name>'
  - JSON fields via JSONExtract*(event_data, '<field>')

OUTPUT
  Exactly ONE SQL statement inside one ```sql fenced block.
  No prose outside the block.

[SCHEMA DEFINITION]
[MATCHED TELEMETRY DEFINITIONS]        ← sliced by resolved keys
[BUSINESS RULES]                        ← full set, always
[FEW-SHOT EXAMPLES]                     ← 1–2 per matched key
[RESOLVED KEYS / ALIASES]
[CONVERSATION HISTORY — summaries]
USER QUESTION: {query}
```

## 8. SQL Validation & Safety Rules

The validator runs before anything touches the database. It returns `{valid, errors, warnings, normalized_sql, detected_tables, detected_columns}`.

### Hard rejections

| Rule | Purpose |
|---|---|
| Forbidden statements: `INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, ATTACH, DETACH, SYSTEM, SHOW, DESCRIBE, RENAME, OPTIMIZE, GRANT, REVOKE, KILL, FLUSH` | Read-only guarantee at the SQL layer (defense in depth below the DB user's own read-only grants) |
| Table allowlist: only `{tenant}` telemetry/events tables | No cross-tenant or system-table access |
| CTE depth ≤ 5 | Runaway-complexity guard |

### Structural checks (EAV-specific)

- Metric filters use `key = '<literal>'` (not `LIKE '%…%'` guesses).
- Numeric operations wrap `value` in `toFloat64OrNull()`.
- Aggregate queries have `GROUP BY` (and `ORDER BY` where meaningful).

### Auto-repairs (one repair pass, then re-validate)

- Wrap bare `value` in `toFloat64OrNull(...)` where arithmetic detected.
- Fix nested aggregate casts: `toFloat64(argMax(value, ts))` → `argMax(toFloat64OrNull(value), ts)`.
- Inject missing `GROUP BY device_id` / `ORDER BY`.
- Correct near-miss table names to the tenant's actual tables.

### Execution-layer defenses

- Dedicated **read-only DB user** (validation is belt; grants are suspenders).
- Memory-bounded settings + query timeout + row limit.
- Serialized execution on a shared client (lock).

## 9. Document RAG Layer (pgvector)

For *document* questions (manuals, SOPs, specs) — independent of the SQL path.

**Ingestion pipeline:**

```text
upload (PDF/DOCX/XLSX/HTML/MD)
  → parse (pymupdf / python-docx / openpyxl / bs4 / markdown)
  → chunk (configurable size + overlap, section-aware)
  → embed (configured model, 1536-dim default; pluggable)
  → store (rag_documents + rag_chunks, HNSW cosine index)
```

**Retrieval:**

```sql
SELECT chunk_text, section_title, 1 - (embedding <=> :query_vec) AS score
FROM rag_chunks
WHERE tenant = :tenant            -- tenant docs
   OR (tenant IS NULL AND scope = :scope)  -- shared/global docs
ORDER BY embedding <=> :query_vec
LIMIT :top_k;
```

Tenant-private + shared-scope results are merged by score. Answers return **sources** (document, page) alongside the text.

**A purpose-built advisor mode** runs the same RAG core over an *event description + telemetry snapshot*: it returns structured JSON — `{summary, possible_causes, possible_consequences, immediate_actions, inspection_checklist, recommended_action, estimated_risk, can_continue, confidence, sources}` — for maintenance/incident-response use cases.

## 10. Hybrid Intent Routing (SQL vs RAG)

One entry point (`/v1/query`) classifies intent, then:

- **data** → SQL agent → rows + summary
- **docs** → RAG → answer + sources
- **hybrid** → both, merged ("what overheated yesterday, and what should I do?")

Router indicators are **config lists**, e.g.:

```yaml
router:
  sql_indicators: [speed, rpm, temperature, yesterday, average, maximum, ...]
  rag_indicators:  [manual, specification, how do I, troubleshooting, ...]
```

Swap in an LLM classifier behind the same interface when keyword routing tops out.

## 11. Conversation Memory: Sessions, History, Digests

- **Sessions** (`llm_sessions`): created on first query; deterministic keyword title ("Average Speed Query") — no extra LLM call; UUID id; tenant-bound.
- **Turns** (`llm_sql_history`): every Q/SQL/rows/summary persisted; `session_id` groups them; `supersedes_id` chains corrections so a conversation shows the *final* answer with full lineage.
- **Digests** ⛳: rolling session summary, injected into later prompts instead of raw turns.
- **History API**: sessions newest-first with their turns, so any UI can render threads.
- Writes are **non-blocking**: memory failure never breaks answering.

## 12. Feedback Flywheel (Continuous Learning)

The system improves from use, with humans in the loop:

```text
                 ┌─────────────────────────────────────────┐
                 │  1. CAPTURE                             │
                 │  thumbs up/down + comment per answer    │
                 │  (POST /v1/feedback)                    │
                 └───────────────────┬─────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────┐
                 │  2. AUTO-MINE                           │
                 │  successful in-session corrections      │
                 │  (question → corrected SQL) become      │
                 │  'auto_pending' rows automatically      │
                 └───────────────────┬─────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────┐
                 │  3. HUMAN REVIEW (admin, token-gated)   │
                 │  pending queue · stats · approve/reject │
                 └───────────────────┬─────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────┐
                 │  4. PROMOTION                            │
                 │  approved pairs → sql_agent_sql_examples│
                 │  rows with embedding + 'approved' +    │
                 │  provenance back to the feedback       │
                 │  → future prompts retrieve them        │
                 └───────────────────┬─────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────┐
                 │  5. CRYSTALLIZE / GRADUATE               │
                 │  recurring fixes → registry updates     │
                 │  (new alias, changed default) via       │
                 │  'graduation candidates' queue; every   │
                 │  applied fix invalidates the KB cache   │
                 └─────────────────────────────────────────┘
```

Promotion inserts the question→SQL pair directly into `sql_agent_sql_examples` with its embedding, `status='approved'`, and a provenance link to the feedback row. **Decay runs in the other direction:** when a user correction supersedes SQL matching an approved example, that example is demoted for re-review (`corrections_after_use` tracks repeats; re-promotion after a fix is supported). The loop is closed entirely inside the PG knowledge store — no file writes anywhere.

Extras: batch auto-promote of pending positives, golden-eval-set export from approved pairs, flywheel metrics (correction rate, latency trends) over a time window.

## 13. Self-Service Onboarding Wizard

Get a new tenant from zero to first question without an engineer:

```text
PROBE ──► NAMING ──► KNOWLEDGE ──► ACTIVATE ──► READY
```

1. **Probe** — introspect the tenant's telemetry table: distinct keys (with sample rates), distinct event types, categories, devices. Result cached in `onboarding_state`.
2. **Naming** — suggest friendly names + aliases per key (rule-based defaults from key names; optional LLM polish from uploaded docs). LLM-extracted candidates land in a **review queue** (`sql_agent_key_mapping_candidates` — never read by the agent itself); the user-approved rows are promoted into `sql_agent_telemetry_registry` + `sql_agent_aliases`, and the loader's metadata cache is invalidated.
3. **Knowledge** — upload manuals/SOPs → RAG ingestion (§9).
4. **Activate** — flips `activation_state`; gates both SQL and RAG endpoints (fail-closed 4xx before activation).
5. **Readiness** — checklist endpoint (probe done? keys mapped? docs uploaded? activation on?) + saved wizard progress (step, % complete, next action).

A thin Streamlit panel exercises the whole wizard for demos and testing.

## 14. API Surface

FastAPI, generic response envelope:

```json
{ "status": "Success|Failure", "message": "…", "data": { … }, "errorType": "…", "statusCode": 200 }
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness (no DB) |
| GET | `/readyz` | readiness (DB round-trip) |
| POST | `/v1/query | unified entry: intent routing → SQL agent or RAG |
| POST | `/v1/query/sql | NL→SQL directly |
| POST | `/v1/query/sql/stream | same, **SSE streaming** |
| POST | `/v1/rag/query | document Q&A (answer + sources) |
| POST | `/v1/rag/advisor | structured incident/event analysis |
| POST | `/v1/documents | upload/ingest docs (PDF/DOCX/XLSX/HTML/MD) |
| GET | `/v1/documents | list |
| DELETE | `/v1/documents/{id} | delete doc + chunks |
| GET | `/v1/history | sessions with turns |
| GET/POST/PUT/DELETE | `/v1/preferences | user suggestion preferences |
| POST | `/v1/feedback | thumbs up/down + comment |
| GET | `/admin/feedback/pending · /stats · /examples` | review queue (token-gated) |
| POST | `/admin/feedback/{id}/approve · /reject` | human review |
| POST | `/admin/feedback/auto-promote-positive` | batch promotion |
| GET | `/admin/feedback/golden-eval` | export eval set |
| GET | `/admin/feedback/graduation-candidates · POST /graduation/apply` | registry-fix pipeline |
| POST | `/v1/onboarding/{tenant}/probe · /naming · /enable · /disable` | wizard |
| GET | `/v1/onboarding/{tenant}/readiness · /state · /naming-suggestions` | wizard |

**SSE stream events:**

```text
event: stage          {"stage": "retrieving|generating|validating|executing|summarizing"}
event: summary_delta  {"text": "…"}        ← token streaming
event: final          {full response}      ← rows + sql + session + usage
event: error          {message}
: ping                                     ← 15s heartbeat
```

Headers: `X-Accel-Buffering: no`, `Cache-Control: no-cache`. The sync endpoint and `final` event share an identical payload shape — UIs can support both trivially.

**Query response (`data`):**

```json
{
  "data": [ {"device_id": "truck_102", "avg_speed": 61.4} ],
  "sql": "SELECT …",
  "summary": "Truck 102 averaged 61.4 km/h yesterday.",
  "sessionId": "uuid", "historyId": 4821,
  "tokenUsage": 1834,
  "assumptionNote": "assuming average speed for today",
  "followUpQuestions": []
}
```

Graceful states: clarifying question (200 + question, no SQL), validation failure (400 + rule errors), database offline (200 + generated SQL + notice), gating (403/422 pre-activation).

## 15. Configuration Reference

```bash
# ── Analytical engine (telemetry) ────────────────────────────
TELEMETRY_DB_HOST=localhost
TELEMETRY_DB_PORT=8123
TELEMETRY_DB_NAME=datamind
TELEMETRY_DB_USER=readonly_user
TELEMETRY_DB_PASSWORD=changeme

# ── Metadata store (sessions/history/feedback/registry) ─────
METADATA_DB_HOST=localhost
METADATA_DB_PORT=5432
METADATA_DB_NAME=datamind_meta
METADATA_DB_USER=datamind
METADATA_DB_PASSWORD=changeme

# ── LLM (any OpenAI-compatible endpoint) ────────────────────
LLM_API_KEY=sk-…
LLM_BASE_URL=https://api.openai.com/v1      # or local server
LLM_MODEL=gpt-4o                            # any capable model
EMBEDDING_MODEL=text-embedding-3-small

# ── Feature flags (all default false — progressive enablement)
ENABLE_CORRECTION_LOOP=true     # S1 in-session repair
ENABLE_ASSUME_FIRST=true        # S3 defaults + assumption notes
ENABLE_SESSION_DIGEST=true      # S2 long-session memory
ENABLE_STREAMING=true           # SSE endpoint
ENABLE_SEMANTIC_EXAMPLES=true   # S6 semantic few-shot (cosine ≥ 0.85, top-2)
ENABLE_FEEDBACK_CAPTURE=true    # flywheel + admin endpoints
BOOTSTRAP_SCHEMA=true           # auto-create tables on start

# ── Routing / retrieval ──────────────────────────────────────
RAG_RETRIEVER=pgvector
INTENT_CLASSIFIER=keyword       # keyword | llm

# ── Admin ────────────────────────────────────────────────────
FEEDBACK_ADMIN_TOKEN=generate-a-long-random-token
```

Every stage beyond the core pipeline is flag-gated → the project runs minimal on day one and grows feature by feature.

## 16. Security & Multi-Tenancy Model

- **Tenant identity from auth claims** — whatever your IdP puts in the JWT (a tenant claim); a `tenant` request field is the dev-mode fallback. Never trust client-supplied tenant alone.
- **Server-side isolation** — every query filters by the authenticated tenant, enforced in the data-access layer, not in handlers.
- **Per-tenant tables + per-tenant KB folders + per-tenant vector rows** — three-layer isolation.
- **SQL-layer read-only enforcement** (§8) on top of a **read-only DB user**.
- **Admin surface fail-closed** — shared-secret header (`X-Admin-Token`); missing/mismatched → 403.
- **Secrets via env only** — `.env.example` documents keys; real values never in the repo; optional Vault/secret-manager sidecar in K8s.
- **Note for rebuilders:** if your production auth decodes JWTs without signature verification (trusting the upstream gateway), the open-source version should verify signatures properly.

## 17. Deployment

### Local (docker-compose)

```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:24
    ports: ["8123:8123"]
    ulimits: { nofile: 262144 }

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: datamind
      POSTGRES_PASSWORD: changeme
      POSTGRES_DB: datamind_meta
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U datamind"]
      interval: 10s
      retries: 10

  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      clickhouse: { condition: service_started }
```

### API container

- Base `python:3.11-slim`, venv, non-root user in the optimized variant.
- `gunicorn main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120` (+ `--max-requests 1000 --max-requests-jitter 100 --preload` for the optimized image).
- Healthcheck hits `/healthz`.

### Kubernetes sketch

- Deployment: readiness/liveness on `/healthz`, resource requests/limits, topology spread.
- Secrets from your secret manager (env injection); no plaintext in manifests.
- Service: 80/443 → container port.
- CI: build multi-arch image, push to your registry, tag by commit SHA. (Ship a GitHub Actions workflow; keep vendor-agnostic.)

## 18. Observability & Performance

- **Two-tier health:** `/healthz` (process alive — probes) vs `/readyz` (DB round-trips — dashboards).
- **Per-stage latency:** retrieval / prompt / generation / validation / execution / formatting timed per request.
- **Token accounting** per request in history.
- **Query metrics:** latency, cache hit, error rate; breakdowns per tenant and query class (LATEST / TIME_RANGE / AGGREGATION / MULTI_KEY / SIMPLE_FILTER).
- **Retrieval benchmarks:** harness comparing retriever options (latency, overlap, chunk-match quality) — the project measured FAISS vs pgvector and standardized on pgvector for persistence + tenant scoping.
- **Console run-cards** per request: tokens used, KB cache hit/miss, matched keys, session reused?, follow-up?, metadata counts (telemetry · aliases · rules · examples · schema columns) — one-glance debugging.

## 19. Testing Strategy

| Layer | What's covered |
|---|---|
| Unit | validator rules + auto-repairs; alias resolution; assume-first defaults; greeting/correction regex triggers **and guards** |
| Integration | pipeline end-to-end vs real ClickHouse/Postgres; execution memory settings; tenant isolation |
| Conversation | follow-up resolution, correction lineage (`supersedes_id`), digest injection |
| Flywheel | capture → auto-mine → review → promotion → example retrieval |
| Onboarding | probe → naming → activation gating → readiness |
| Regression | golden question set (exported from approved feedback) run on every change |
| Latency | stage budgets and p95s |

The golden-eval export (§12) is the connective tissue: real approved question→SQL pairs become the regression suite.

## 20. Project Structure (Target Repo Layout)

```text
datamind/
├── api/                      # FastAPI app, routers, SSE
├── agent/
│   ├── pipeline.py           # S0–S13 orchestration
│   ├── key_resolver.py       # S5 aliases → canonical keys
│   ├── knowledge_retriever.py# S6 slice PG knowledge by resolved keys
│   ├── prompt_builder.py     # S7
│   ├── generator.py          # S8 LLM call
│   ├── validator.py          # S9 rules + auto-repair
│   ├── executor.py           # S10 read-only execution
│   ├── formatter.py          # S11
│   ├── summarizer.py         # S12
│   ├── correction.py         # S1 ⛳
│   ├── followup.py           # S2 ⛳
│   └── assume_first.py       # S3 ⛳
├── routing/                  # S4 intent router (config-driven)
├── rag/                      # §9 document RAG (pgvector)
│   ├── ingest.py  ├── retriever.py  └── schema.sql
├── memory/                   # §11 sessions, history, digests
├── feedback/                 # §12 flywheel + admin
├── onboarding/               # §13 wizard
├── knowledge/
│   ├── store.py              # PG knowledge loader (cache, invalidation, metrics)
│   ├── schema.sql            # sql_agent_* DDL + incremental migrations
│   └── seed/demo.sql         # example tenant seed (idempotent INSERTs)
├── database/
│   ├── clickhouse_adapter.py
│   ├── postgres_adapter.py
│   └── schema.sql
├── config/                   # §15 env + flags + router indicators
├── tests/
├── demo/                     # seed script + sample telemetry + streamlit panel
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── README.md
```

## 21. Roadmap

| Phase | Item |
|---|---|
| v0.1 | ClickHouse + single tenant, core S4–S13 pipeline, sync API |
| v0.2 | Sessions/history; SSE streaming; PG knowledge store + cached loader; semantic few-shot (flag-gated); onboarding probe |
| v0.3 | Correction loop, assume-first, digests (flag-gated) |
| v0.4 | Feedback flywheel + admin review; golden-eval export |
| v0.5 | Document RAG + hybrid routing + advisor mode |
| v0.6 | Hybrid registry retrieval (BM25 + vector over telemetry keys) for paraphrase-proof key resolution |
| v0.7 | PostgreSQL/TimescaleDB adapter; configurable column mapping (§4.1 yaml) end-to-end |
| v0.8 | LLM classifier for intent routing; signature-verified JWT auth module |
| v1.0 | Web UI; plugin interface (new engines: e.g. wide-row stores); Helm chart |

## 22. Rebuild Checklist (From a Proprietary Implementation)

If you (or anyone) rebuild this from a production codebase, **sanitize first**:

**Strip — never publish:**
- [ ] Real tenants/customers: every tenant name, key set, device id pattern, customer data, and per-customer test reports
- [ ] Company names, product names, internal hostnames, internal IPs, registry URLs, DNS aliases
- [ ] Vault paths, service-account names, CI project keys, internal registry credentials
- [ ] `.env` with real secrets → ship only `.env.example`
- [ ] Auth specifics (your IdP's claim names, unverified-JWT shortcuts) → generic verified-JWT module
- [ ] Internal business rules that encode proprietary domain logic → keep only generic engine invariants

**Generalize:**
- [ ] `{company}_time_series` → configurable `{tenant}_telemetry` mapping
- [ ] Hardcoded domain keyword lists (titles, intent indicators) → config files
- [ ] Single LLM vendor → OpenAI-compatible interface (base URL + model + key)
- [ ] Company prompts → the generic skeletons in §7
- [ ] Tenant seed data → one synthetic `demo` tenant (trucks/machines with speed, rpm, temp, fuel, battery)

**Keep (the engineering value):**
- [ ] The full staged pipeline (S0–S13) — this doc is its spec
- [ ] Validation/auto-repair rule engine (§8)
- [ ] PostgreSQL knowledge store schema + loader with cache/invalidation (§6)
- [ ] Feedback flywheel with human gates (§12)
- [ ] Onboarding wizard (§13)
- [ ] Stage latency + token accounting (§18)

**Portfolio positioning:** *"DataMind — an open-source agentic NL→SQL + RAG engine for high-cardinality telemetry. Schema-aware key retrieval, validated read-only SQL generation over an EAV telemetry model, conversation memory with correction repair, and a human-in-the-loop feedback flywheel."* Demonstrate with a seeded `demo` tenant: ask questions in plain English, watch keys resolve, SQL generate/validate/execute, answers stream — then show the flywheel promoting a real correction into a few-shot example.

---

*DataMind blueprint · company-agnostic by construction · CC-BY-4.0 when published*
