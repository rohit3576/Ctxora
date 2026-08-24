# Ctxora — Architecture Diagrams

> Visual companion to [`ARCHITECTURE.md`](ARCHITECTURE.md) (repo/module reference) and
> [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md) (design rationale). All diagrams render on
> GitHub (Mermaid). Where a diagram and code disagree, the code wins — open a PR.

**Index**

1. [System context — who talks to Ctxora](#1-system-context)
2. [Component map — the running service](#2-component-map)
3. [The life of a query — NL→SQL pipeline](#3-the-life-of-a-query)
4. [Hybrid routing — SQL vs documents vs chat](#4-hybrid-routing)
5. [The SQL safety gauntlet — AST validation](#5-the-sql-safety-gauntlet)
6. [The feedback flywheel — how Ctxora learns](#6-the-feedback-flywheel)
7. [Document RAG — ingest and retrieve](#7-document-rag)
8. [Onboarding wizard — zero-code tenant bring-up](#8-onboarding-wizard)
9. [Data stores — what lives where](#9-data-stores)
10. [Deployment topology](#10-deployment-topology)

---

## 1. System context

```mermaid
flowchart LR
    user["End user<br/>(asks questions in English)"] --> ui["Streamlit demo panel<br/>or any HTTP client"]
    ui --> api["Ctxora API"]

    ops["Tenant operator"] -- "maps columns,<br/>onboards tenants" --> api
    reviewer["Feedback reviewer<br/>(admin token)"] -- "approves / rejects<br/>learned examples" --> api

    api -- "read-only SQL" --> telemetry["Telemetry DB<br/>ClickHouse or PostgreSQL/Timescale"]
    api -- "metadata + vectors" --> meta["Metadata DB<br/>PostgreSQL + pgvector"]
    api -- "generation + embeddings" --> llm["LLM endpoint<br/>any OpenAI-compatible API"]
```

Key idea: **the user's data never moves.** Ctxora points at an existing key-value
telemetry database and generates read-only SQL against it. The metadata DB and LLM
are Ctxora's own support infrastructure.

## 2. Component map

Arrows point down only (the dependency rule — `agent/` imports protocols, never engine clients):

```mermaid
flowchart TD
    subgraph API["api/ — FastAPI routers, SSE, auth, rate limit"]
        q["query.py · documents.py · onboarding.py<br/>feedback_admin.py · history.py · health.py"]
    end

    subgraph ROUTING["routing/"]
        router["Intent router<br/>data-question vs doc-question vs chat"]
    end

    subgraph AGENT["agent/ — the pipeline state machine (S0–S13)"]
        stages["greeting · correction · follow-up · assume-first<br/>key_resolver · knowledge_retriever<br/>prompt_builder · generator · validator · executor<br/>formatter · summarizer"]
    end

    subgraph RAG["rag/"]
        ragm["ingest → chunk → embed<br/>pgvector retrieve → cited answer"]
    end

    subgraph LEARNING["learning surfaces"]
        memory["memory/<br/>sessions · history · digests"]
        feedback["feedback/<br/>capture · promotion · decay · graduation"]
        knowledge["knowledge/<br/>aliases · rules · examples"]
    end

    subgraph STORAGE["database/ — the only engine boundary"]
        store["TelemetryStore protocol"]
        ch["ClickHouseStore"]
        pg["PostgresStore"]
        dialect["Dialect protocol<br/>sqlglot_name · renders engine SQL"]
        store --> ch
        store --> pg
    end

    llm["llm/ — LLMClient protocol<br/>one OpenAI-compatible impl"]

    API --> ROUTING
    API --> AGENT
    ROUTING --> AGENT
    ROUTING --> RAG
    AGENT --> knowledge
    AGENT --> memory
    AGENT --> store
    AGENT --> dialect
    AGENT --> llm
    RAG --> llm
    API --> feedback
    feedback --> knowledge
```

## 3. The life of a query

Happy path for `"What was the average RPM of truck-102 yesterday?"` through
`POST /v1/query/sql` (SSE streaming variant streams S12):

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API (auth, rate limit)
    participant P as Pipeline (agent/)
    participant K as Knowledge cache
    participant L as LLM
    participant V as Validator (AST)
    participant T as Telemetry store (read-only)
    participant M as Memory

    C->>A: POST /v1/query/sql {tenant, query}
    A->>A: verify JWT tenant claim (or dev mode)
    A->>P: QueryContext
    P->>P: S0 greeting? S1 correction? S2 follow-up? S3 assume-first (flag-gated)
    P->>K: S5 resolve "RPM" → canonical key engine.rpm
    K-->>P: KeyMapping + examples + rules
    P->>P: S7 assemble deterministic prompt sections
    P->>L: S8 generate (temperature 0)
    L-->>P: fenced SQL
    P->>V: S9 validate + one value-cast repair pass
    alt invalid
        V-->>P: errors → QueryRejected
        P-->>C: 400 SQL_VALIDATION_FAILED (nothing executed)
    else valid
        V-->>P: normalized_sql (repaired string)
        P->>T: S10 execute (row_cap, timeout)
        T-->>P: rows
        P->>P: S11 format → typed rows
        P->>L: S12 summarize rows → natural-language answer
        P->>M: S13 record history (+ mine correction, flag-gated)
        M-->>C: 200 {sql, rows, summary, sessionId, historyId}
    end
```

The pipeline is the **only** state machine: every conversational behavior (correction
loop, follow-ups, assume-first) is a stage that either short-circuits or rewrites the
query, each behind a feature flag in `config/defaults.yaml`.

## 4. Hybrid routing

```mermaid
flowchart TD
    q["User question"] --> r{"Router (S4)<br/>config keyword indicators<br/>or LLM classifier"}

    r -- "average · last · per · yesterday …" --> sql["SQL agent path<br/>(diagram 3)"]
    r -- "manual · how do I · policy …" --> rag["Document RAG path<br/>(diagram 7)"]
    r -- "greeting / small talk" --> chat["Chit-chat stage S0<br/>short conversational reply"]
    r -- "mixed signals" --> hybrid["Both paths, answers merged"]

    sql --> a1["Answer grounded in live telemetry"]
    rag --> a2["Answer grounded in uploaded documents, with citations"]
```

## 5. The SQL safety gauntlet

Every generated statement walks this gauntlet **before** execution — all checks
fail-closed, in this exact order (`agent/validator.py`). This replaced the old
regex blocklists in v1.0 (sqlglot AST — comments, smuggling tricks, and comma-joins
have no string surface to hide on):

```mermaid
flowchart TD
    s["generated SQL string"] --> p{"1 · Parse<br/>sqlglot, engine dialect<br/>ParseError + TokenError"}
    p -- "any parse/tokenize failure" --> rej1["REJECT: unparseable statement"]
    p -- "parsed" --> n{"2 · Single statement?"}
    n -- "no (stacked ; )" --> rej2["REJECT: multi-statement"]
    n -- "yes" --> root{"3 · Root allowlist<br/>SELECT / UNION / INTERSECT / EXCEPT<br/>(Subquery unwrapped)"}
    root -- "anything else incl. Command" --> rej3["REJECT: forbidden statement"]
    root -- "SELECT-shaped" --> mut{"4 · Mutation-node walk<br/>Insert Update Delete Drop Alter Create<br/>Into (SELECT…INTO) Lock (FOR UPDATE/SHARE)<br/>+ Grant Revoke Truncate Merge Copy"}
    mut -- "any mutation node" --> rej4["REJECT: forbidden statement"]
    mut -- "clean" --> fn{"5 · Dangerous functions<br/>read_csv · pg_read_file ·<br/>pg_read_binary_file · s3<br/>+ FROM/JOIN must be plain tables"}
    fn -- "named or table-function" --> rej5["REJECT: forbidden function"]
    fn -- "clean" --> tbl{"6 · Table allowlist<br/>CTE-aware, tenant tables only,<br/>qualified names NEVER allowed"}
    tbl -- "off-allowlist or schema-qualified" --> rej6["REJECT: table not allowed"}
    tbl -- "allowed" --> cte{"7 · CTE depth ≤ 5?"}
    cte -- "deeper" --> rej7["REJECT: CTE depth exceeds 5"]
    cte -- "ok" --> exec["EXECUTE via read-only store connection"]
```

Two invariants worth knowing:

- **One repair pass, max.** Bare aggregates get the dialect's null-safe cast wrapped
  once, then the gauntlet runs — no blind LLM retry loops.
- **The executed string is never re-emitted from the AST.** Parsing only gates; the
  SQL that runs is the (possibly repaired) original text, byte-identical.

Belt and suspenders: the validator assumes nothing about database privileges — deploy
with a read-only DB role anyway ([SECURITY.md](../../SECURITY.md)).

## 6. The feedback flywheel

Ctxora improves from usage without self-modifying prompts blindly — humans approve
what the system learns:

```mermaid
flowchart TD
    u["User reacts"] -->|"thumbs up / down<br/>POST /v1/feedback"| cap["capture (feedback/)"]
    corr["Successful correction<br/>(pipeline mines it automatically)"] --> mine["auto_pending row<br/>+ decay contradicted examples"]
    cap --> pend["query_feedback rows<br/>(pending review)"]
    mine --> pend

    pend -->|"GET /admin/feedback/pending<br/>X-Admin-Token (fail-closed)"| rev["Reviewer decides"]

    rev -- approve --> promo["promote → semantic example<br/>(question, SQL, embedding, provenance)"]
    rev -- reject --> drop["dropped"]

    promo --> serve["next prompts include the example<br/>(semantic_examples flag, pgvector match)"]
    serve --> better["better first-shot SQL"]
    better --> u

    promo -.->|"same fix recurs?"| grad["graduation: recurring fix<br/>becomes an alias/rule change"]
    grad --> knowledge["knowledge registry"]
```

Decay uses canonical SQL comparison (`normalize_sql` — sqlglot canonical form), so the
same query written with different spacing/casing/comments is recognized as one.

## 7. Document RAG

```mermaid
flowchart LR
    subgraph ingest["INGEST — POST /v1/documents"]
        up["upload pdf / docx / xlsx / html / md"] --> parse["extract text + structure"]
        parse --> chunk["chunker"]
        chunk --> embed["embed (EMBEDDING_MODEL, 1536-d)"]
        embed --> store[("rag_documents + rag_chunks<br/>VECTOR(1536), HNSW cosine<br/>tenant + scope scoped")]
    end

    subgraph retrieve["RETRIEVE — doc-question routed here"]
        q2["question"] --> qe["embed question"]
        qe --> search["cosine search, tenant-scoped<br/>shared-scope merge"]
        search --> ctx["top chunks + section titles"]
        ctx --> ans["LLM answer WITH citations"]
    end

    store --> search
```

## 8. Onboarding wizard

Zero-code tenant bring-up — the operator never edits Python:

```mermaid
flowchart LR
    a["1 · PROBE<br/>GET /v1/onboarding/{tenant}/probe<br/>introspect keys + event types<br/>via TelemetryStore"] --> b["2 · NAMING<br/>suggest friendly key names<br/>review queue"]
    b --> c["3 · KNOWLEDGE<br/>seed aliases/rules/examples<br/>for the tenant"]
    c --> d["4 · ACTIVATE<br/>is_onboarded = true<br/>queries start flowing"]
```

## 9. Data stores

```mermaid
flowchart TD
    subgraph MD["Metadata DB — PostgreSQL + pgvector (Ctxora-owned)"]
        direction TB
        k["knowledge/<br/>sql_agent_aliases · rules · examples<br/>(example embedding VECTOR(1536))"]
        m["memory/<br/>llm_sessions · llm_sql_history<br/>(supersedes lineage) · digests"]
        f["feedback/<br/>query_feedback · promoted examples"]
        rg["rag/<br/>rag_documents · rag_chunks<br/>(VECTOR(1536) HNSW cosine)"]
        ob["onboarding/<br/>naming suggestions queue"]
    end

    subgraph TD_["Telemetry DB — USER-OWNED (read-only creds)"]
        direction TB
        kt["{tenant}_telemetry<br/>timestamp · entity · key · value<br/>(+ optional dimensions)"]
        ev["{tenant}_events<br/>timestamp · event_type · entity · payload JSON"]
    end

    agent["agent pipeline"] -- "metadata reads/writes,<br/>vector search" --> MD
    agent -- "read-only SQL only" --> TD_
```

Column names in the telemetry store are whatever the user's schema has — they are
declared once in `config/defaults.yaml` (`stores.telemetry.mapping`).

## 10. Deployment topology

```mermaid
flowchart TD
    subgraph compose["docker compose (dev) / Helm (deploy/helm)"]
        api_c["api container<br/>uvicorn main:app"]
        pg_c["postgres + pgvector<br/>(metadata DB, default profile)"]
        ch_c["clickhouse<br/>(optional --profile clickhouse)"]
    end

    client["clients / demo panel"] -- ":8000 /healthz /readyz /v1/*" --> api_c
    api_c --> pg_c
    api_c -- "only if adapter=clickhouse" --> ch_c
    api_c -- "HTTPS" --> llmc["LLM API (external)"]

    note["Production posture:<br/>AUTH_DISABLED=false + JWT_SECRET + TENANT_CLAIM<br/>FEEDBACK_ADMIN_TOKEN set<br/>read-only telemetry role<br/>flags.ratelimit: true"] --- api_c
```

v1.0 is single-instance by design (in-memory rate limits, per-process knowledge
cache); see [SYSTEM_DESIGN.md § operational notes](SYSTEM_DESIGN.md#12-operational-notes)
for what changes at scale.
