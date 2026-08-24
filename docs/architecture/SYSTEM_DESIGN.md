# Ctxora — System Design

> **What this doc is:** the design rationale — *why* Ctxora is shaped the way it is,
> how the pieces interact, and which trade-offs were chosen. For file-by-file
> reference see [`ARCHITECTURE.md`](ARCHITECTURE.md); for visuals see
> [`DIAGRAMS.md`](DIAGRAMS.md). For the stage-by-stage specification see the
> [blueprint](../blueprint/IMPLEMENTATION_BLUEPRINT.md).

---

## Table of contents

1. [Problem & one-liner](#1-problem--one-liner)
2. [Design principles](#2-design-principles)
3. [Component responsibilities](#3-component-responsibilities)
4. [The life of a query (narrative)](#4-the-life-of-a-query)
5. [The SQL safety model](#5-the-sql-safety-model)
6. [The learning loop](#6-the-learning-loop)
7. [Data model](#7-data-model)
8. [Cross-cutting concerns](#8-cross-cutting-concerns)
9. [Failure modes & error taxonomy](#9-failure-modes--error-taxonomy)
10. [Testing strategy](#10-testing-strategy)
11. [Key design decisions (mini-ADRs)](#11-key-design-decisions-mini-adrs)
12. [Operational notes](#12-operational-notes)
13. [Glossary](#13-glossary)

---

## 1. Problem & one-liner

IoT fleets (and any key-value telemetry) drown in data but starve for answers.
Business users ask *"what was the average RPM of truck-102 yesterday?"* and the
answer requires SQL nobody in the room wants to write.

**Ctxora is an open-source semantic query layer for key-value telemetry.** Point it
at any `timestamp + entity + key + value` database, and your users ask in plain
English: Ctxora resolves the right metric keys from a registry, generates
**validated read-only SQL** for your engine (ClickHouse and PostgreSQL/Timescale are
tested peers), executes it safely, and explains the result — with conversation
memory, a correction loop, a human-reviewed feedback flywheel, and document RAG
with cited sources.

## 2. Design principles

These six principles explain almost every structural choice in the repo.

**P1 — The user's data stays put.** Ctxora generates SQL against *your* database
with read-only credentials. No ingestion pipeline, no copy of your telemetry, no
lock-in. Everything engine-specific lives behind two protocols (`Dialect` +
`TelemetryStore`); the agent core never imports an engine client.

**P2 — LLM output is untrusted input.** The model is a translator, not an
authority. Every statement it produces must survive a structural safety gauntlet
(§5) before execution. This is why validation is AST-based, fail-closed, and tested
by an adversarial battery.

**P3 — Learning is human-gated.** The system gets *better* from usage (corrections,
thumbs, promoted examples) but a reviewer approves anything that changes future
prompting (§6). No silent self-modification.

**P4 — Configuration over code.** An operator changing only `config/defaults.yaml`
+ environment variables gets a working service on their schema — column mapping,
routing keywords, aggregation defaults, feature flags. "If a use case forces them
to edit Python, that's a design bug."

**P5 — Every module is testable with fakes.** The LLM, the telemetry store, the
knowledge store, memory, feedback — all protocols with in-memory fakes. The full
313+ test suite (including a 20-question golden parity suite across both engines)
runs with no network, no database, no API keys.

**P6 — One state machine.** All conversational behavior (greeting, correction,
follow-ups, assume-first) is stages in the single `agent/pipeline.py` orchestration,
each flag-gated — not scattered middleware.

## 3. Component responsibilities

| Module | Owns | Never does |
|---|---|---|
| `api/` | HTTP surface, auth, rate limit, SSE streaming, error envelopes | Business logic |
| `routing/` | Intent classification: data-question vs doc-question vs chat | Execute anything |
| `agent/` | The S0–S13 pipeline: resolve → prompt → generate → **validate** → execute → summarize | Import engine clients |
| `agent/validator.py` | The SQL safety gauntlet (§5) + one value-cast repair pass | Re-execute or blind-retry |
| `knowledge/` | Per-tenant registry: aliases, rules, semantic examples (TTL+LRU cached) | Prompt assembly |
| `memory/` | Sessions, SQL history with supersedes lineage, rolling digests | Store telemetry |
| `feedback/` | Capture, auto-mining, promotion to examples, decay, graduation | Auto-approve anything |
| `rag/` | Document ingest (pdf/docx/xlsx/html/md), pgvector retrieval, cited answers, advisor mode | Query telemetry |
| `database/` | `TelemetryStore` + `Dialect` protocols; ClickHouse & Postgres/Timescale adapters | Know anything about NL |
| `llm/` | `LLMClient` protocol; one OpenAI-compatible client (works with OpenAI, Azure, Groq, OpenRouter, llama.cpp/vLLM — only `base_url` changes) | Vendor SDKs elsewhere |
| `onboarding/` | Probe a tenant's real keys → suggest names → seed knowledge → activate | Require code edits |
| `config/` | Env + YAML, validated fail-fast | Silent defaults for secrets |

Dependency rule (enforced in review): arrows point down — `api/` → `agent/`/`routing/` →
`knowledge/`/`memory/`/`database/` protocols. Modules never import `api/`.

## 4. The life of a query

Narrative companion to [diagram 3](DIAGRAMS.md#3-the-life-of-a-query).

`"What was the average RPM of truck-102 yesterday?"` arrives at `POST /v1/query/sql`:

1. **Gate** — auth (JWT tenant claim, or loud dev mode), rate limit (flag-gated token bucket).
2. **Conversational pre-stages** — is it a greeting (S0)? a correction of the last
   query (S1)? a follow-up needing history/digest context (S2)? does "the average"
   need an assumption filled (S3, flag-gated)? Each stage may rewrite the query or
   short-circuit with a chat reply.
3. **Key resolution (S5)** — "RPM" is not a column value; the knowledge registry maps
   it to the canonical key `engine.rpm` (aliases +, optionally, semantic example match).
   Unknown-but-similar keys can be auto-mapped with a visible assumption note.
4. **Prompt assembly (S7)** — deterministic sections: registry index, table shape with
   the *user's* column names, dialect rules block, few-shot examples, resolved keys.
   Determinism matters: same question + same knowledge → same prompt.
5. **Generation (S8)** — one LLM call at temperature 0, fenced-SQL extraction.
6. **Validation (S9)** — the gauntlet (§5). One repair pass (bare aggregate → dialect
   null-safe cast) then hard rules. Rejection → HTTP 400 `SQL_VALIDATION_FAILED`;
   **nothing is executed**.
7. **Execution (S10)** — via the tenant's `TelemetryStore` adapter: read-only
   credential, row cap, timeout, error-kind classification.
8. **Format & summarize (S11–S12)** — typed rows; LLM writes the natural-language
   answer grounded in *the actual rows* (streamed over SSE when enabled).
9. **Record (S13)** — session turn + SQL history (with lineage when a query
   supersedes an earlier one); flag-gated correction mining feeds the flywheel.

## 5. The SQL safety model

Full visual: [diagram 5](DIAGRAMS.md#5-the-sql-safety-gauntlet). Principles:

- **Structural, not lexical.** Since v1.0 the validator parses every statement with
  [sqlglot](https://github.com/tobymao/sqlglot) into an AST using the engine's
  grammar. Comments vanish in tokenization; tables are nodes; statement type is the
  root class. There is no string surface left for the classic smuggling tricks
  (comment-split verbs, comma-join table smuggling, `SELECT … INTO`, `FOR UPDATE`,
  qualified-name tricks, file-reading functions, stacked statements).
- **Fail-closed, ordered.** Parse gate → single statement → root allowlist
  (`SELECT`/set-operations only) → mutation-node deny (including `Into` and `Lock`) →
  dangerous-function deny (4-name list + "FROM/JOIN must be plain tables") →
  CTE-aware, unqualified-only table allowlist → CTE depth cap.
- **Allowlist over blocklist.** The default answer is *no*: anything that is not a
  plain SELECT over the tenant's own tables is rejected. Blocklists (the 4 function
  names) exist only where structure can't express the rule.
- **Tenant tables are unqualified names.** `other_schema.demo_telemetry` is rejected
  even though the leaf name matches — a qualified reference is a different object.
- **Defense in depth.** The validator is a belt; database-level read-only grants are
  the suspenders. Row caps and timeouts bound blast radius of anything that *is*
  SELECT-shaped. All SQL safety details: [`SECURITY.md`](../../SECURITY.md).

The behavior is pinned by three permanent test files: an adversarial battery
(every attack shape must be rejected), sqlglot semantics locks (library behaviors
the gauntlet depends on), and the golden parity suite (real questions must keep
working on both engines).

## 6. The learning loop

Full visual: [diagram 6](DIAGRAMS.md#6-the-feedback-flywheel).

Signals in: explicit thumbs up/down (`POST /v1/feedback`) and *implicit* successful
corrections (user corrected the SQL and the retry worked → auto-mined, status
`auto_pending`). Everything lands in one review queue behind `X-Admin-Token`
(fail-closed: no token configured → every admin request is 403).

- **Approve → promote**: the (question, SQL) pair becomes a semantic example with an
  embedding; future prompts for similar questions include it, so the first shot
  gets better.
- **Decay**: when a user corrects SQL that matches a promoted example (canonical
  comparison — sqlglot-normalized, so whitespace/case/comment variants count as the
  same query), that example is demoted back to review.
- **Graduate**: a fix that keeps recurring graduates from "example" to "registry
  change" (alias/rule) — the fix stops being few-shot context and becomes part of
  the tenant's knowledge.

## 7. Data model

Two databases with very different ownership ([diagram 9](DIAGRAMS.md#9-data-stores)):

**Metadata DB (Ctxora-owned, PostgreSQL + pgvector).** Knowledge registry
(`sql_agent_*`: aliases, rules, semantic examples with embeddings), memory
(`llm_sessions`, `llm_sql_history` with supersedes lineage, digests), feedback
queue and promoted examples, RAG documents/chunks (HNSW cosine, tenant-scoped),
onboarding naming queue. All tenant-scoped server-side.

**Telemetry DB (user-owned).** Per-tenant KV tables — `{tenant}_telemetry`
(`timestamp · entity · key · value` + optional dimensions) and optionally
`{tenant}_events` (typed events with JSON payload). Column names are whatever the
user's schema already has, declared in the mapping. Accessed **read-only**.

Embeddings: one model (`EMBEDDING_MODEL`, default OpenAI `text-embedding-3-small`,
1536-d — vector columns are typed to it). Model choice is config; a dimension change
is a schema migration. Tuning options (halfvec, ef_search): see
[`docs/tuning/VECTOR_SEARCH_TUNING.md`](../tuning/VECTOR_SEARCH_TUNING.md).

## 8. Cross-cutting concerns

- **Tenancy** — every store scopes by tenant server-side; enforced mode takes the
  tenant from a verified JWT claim (never the request body). Dev mode
  (`AUTH_DISABLED=true`, the default) is loud about being dev mode.
- **Errors** — one envelope shape everywhere (`status / message / data /
  errorType`); typed error types (`SQL_VALIDATION_FAILED`, `GENERATION_FAILED`,
  `EXECUTION_CONNECTION`, `TENANT_NOT_ONBOARDED`, …) with stable HTTP codes (§9).
- **Streaming** — SSE on the SQL path (summary tokens stream as generated).
- **Flags** — every post-MVP behavior (correction loop, assume-first, digests,
  semantic examples, feedback capture, rate limiting) is dark until enabled in
  `config/defaults.yaml`.
- **Config** — env for secrets/deployment, YAML for behavior, validated fail-fast
  at startup.

## 9. Failure modes & error taxonomy

| Failure | Behavior | HTTP |
|---|---|---|
| LLM returns no/unfenced SQL | `GENERATION_FAILED`, no retry storm | 502 |
| SQL fails the gauntlet | `SQL_VALIDATION_FAILED`, validator errors listed, **nothing executed** | 400 |
| Telemetry DB unreachable | `EXECUTION_CONNECTION` | 503 |
| Query error at engine | `EXECUTION_QUERY` | 500 |
| Tenant not onboarded | `TENANT_NOT_ONBOARDED` | 422 |
| Metadata DB down | readiness fails (`/readyz`), `PIPELINE_UNAVAILABLE` | 503 |
| Admin token missing on admin surface | every admin call 403 (fail-closed) | 403 |

Degradation postures: knowledge cache serves stale-on-error (TTL cache); memory
features no-op cleanly when the pipeline succeeds without a session; the service
never degrades into executing unvalidated SQL.

## 10. Testing strategy

Four gates on every change: `ruff check` · `ruff format --check` · `basedpyright`
(mode=all, 0 errors) · `pytest` (313+ tests, no network, no keys — everything via
protocol fakes). Notable suites:

- **Golden parity** — 20 real questions answered against **both** engines with
  fakes; the portability contract in executable form.
- **Adversarial validator battery** — every attack shape (comma-join, comment-split
  verbs, INTO, lock clauses, qualified names, file-read functions, stacked
  statements, garbage) must be rejected.
- **sqlglot semantics locks** — library behaviors the validator depends on are
  pinned so an upstream upgrade that changes them fails loudly.
- **Pipeline e2e** — full HTTP round-trips with fakes, including the
  DELETE-query → 400 path.
- **Integration (opt-in)** — 11 live tests behind `CTXORA_IT=1` + Docker.

## 11. Key design decisions (mini-ADRs)

| # | Decision | Why | Trade-off accepted |
|---|---|---|---|
| 1 | KV/EAV focus, not general text-to-SQL | The shape (`timestamp+entity+key+value`) makes prompts deterministic and validation tractable | Not a fit for arbitrary relational warehouses |
| 2 | Adapter protocols for engines (`Dialect` + `TelemetryStore`) | New engine = one dialect file + one store file; agent core untouched | Slight indirection cost |
| 3 | One OpenAI-compatible LLM client | Covers OpenAI/Azure/Groq/OpenRouter/local servers with one `base_url` | No provider-specific features |
| 4 | AST validation (sqlglot) over regex blocklists | Regexes had 9 verified bypasses; structure has no string surface | Third-party parser dependency — mitigated by semantics-lock tests + pinned version range |
| 5 | Executed SQL never re-emitted from the AST | Byte-identical behavior; the AST only *gates* | Slightly larger attack-relevant surface in parse output — accepted for auditability |
| 6 | Human-gated flywheel | Learning without prompt-drift risk | Reviewer in the loop (deliberate) |
| 7 | Single metadata store (PG + pgvector) for knowledge/memory/feedback/RAG | One DB to back up/secure; vectors co-located with metadata | Not the telemetry engine for vectors (correct — vectors are Ctxora's data, not the user's) |
| 8 | Generic-dialect canonical SQL for dedupe | Flywheel comparison needs determinism, not engine fidelity | CH idioms re-render in canonical form — fine, it's compare-only |
| 9 | Stage-per-behavior pipeline behind flags | Ship dark, enable per deployment; each stage unit-testable | Pipeline file is the complexity hotspot — kept as the *only* one |
| 10 | Per-tenant telemetry tables (`{tenant}_telemetry`) | Isolation at the table level; allowlist trivially per-tenant | Many tables per fleet — fine at IoT scale |

## 12. Operational notes

- **Dev:** `docker compose up -d` (postgres default; `--profile clickhouse` adds CH),
  bootstrap schema, seed demo, `uv run uvicorn main:app`.
- **Prod posture:** `AUTH_DISABLED=false` + `JWT_SECRET` + `TENANT_CLAIM`;
  `FEEDBACK_ADMIN_TOKEN` set; telemetry role read-only at the DB; `flags.ratelimit: true`.
- **Helm:** `deploy/helm/` (deployment, service, configmap, probes `/healthz` `/readyz`).
- **Scale ceiling (v1.0, by design):** single instance — in-memory rate limits,
  per-process knowledge cache. Horizontal scale needs: externalized rate limiter,
  cache invalidation bus (TTL already limits staleness), and that's mostly it; the
  stateless pipeline and protocol boundaries were chosen for this moment.
- **Vector scale-up:** when corpora grow, the levers (halfvec storage, ef_search,
  per-table HNSW) are documented in
  [`docs/tuning/VECTOR_SEARCH_TUNING.md`](../tuning/VECTOR_SEARCH_TUNING.md).

## 13. Glossary

- **EAV / KV telemetry** — entity-attribute-value shaped time series: one row per
  (timestamp, entity, key, value) reading.
- **Canonical key** — the physical metric key in the telemetry table (e.g.
  `engine.rpm`); users say aliases ("RPM", "revs"), the registry maps them.
- **Semantic example** — a promoted (question, SQL) pair, embedded, injected into
  similar future prompts.
- **Supersedes lineage** — when a corrected query replaces an earlier one in
  history, the link is recorded (corrections are traceable).
- **The gauntlet** — the 7-check AST validation pipeline every generated statement
  must survive (§5).
- **Golden parity** — the 20-question suite run against both storage engines.
- **Onboarding** — probe → naming → knowledge seed → activate; makes a tenant
  queryable without code changes.
