# SQL/sqlglot Upgrade Plan — AST-Native Pipeline Program

**Status:** Proposed (not started) · **Track:** NL→SQL agent · **Prerequisite:** v1.0 + sqlglot migration (Phase 4 validator rewrite, Phase 6 flywheel normalization, both shipped)

The companion doc [`RAG_UPGRADE_PLAN.md`](RAG_UPGRADE_PLAN.md) covers the document-RAG
side. This one covers the **SQL side**: finishing the sqlglot migration from "AST-based
validation" to an AST-native pipeline — repairs, flywheel similarity, gauntlet
hardening, cross-engine transpilation, and version governance.

Grounded in the current code: `agent/validator.py`, `feedback/hooks.py`,
`agent/prompt_builder.py`, `agent/pipeline.py`, `knowledge/store.py`,
`routing/router.py`, `database/contracts.py` + dialects, `tests/test_sqlglot_semantics.py`,
`tests/golden/test_golden_parity.py`, `pyproject.toml` (`sqlglot>=26,<28`).

---

## Table of Contents

1. [Why This Program Exists](#1-why-this-program-exists)
2. [Guiding Principles](#2-guiding-principles)
3. [Current State (Audited)](#3-current-state-audited)
4. [Phase Overview & Order Rationale](#4-phase-overview--order-rationale)
5. [Phase S1 — AST Repair Loop v2](#5-phase-s1--ast-repair-loop-v2)
6. [Phase S2 — Qualified Gauntlet Hardening](#6-phase-s2--qualified-gauntlet-hardening)
7. [Phase S3 — Structural Flywheel Similarity + Correction Diff Mining](#7-phase-s3--structural-flywheel-similarity--correction-diff-mining)
8. [Phase S4 — Single-Generation Cross-Engine Transpilation](#8-phase-s4--single-generation-cross-engine-transpilation)
9. [Phase S5 — sqlglot Version Governance](#9-phase-s5--sqlglot-version-governance)
10. [Cross-Cutting Concerns](#10-cross-cutting-concerns)
11. [Configuration Reference (End State)](#11-configuration-reference-end-state)
12. [Success Metrics](#12-success-metrics)
13. [Timeline & Sequencing](#13-timeline--sequencing)
14. [Out of Scope / Deferred](#14-out-of-scope--deferred)

---

## 1. Why This Program Exists

The sqlglot migration landed the foundation: dialect-aware AST validation, the behavior
pin suite, canonical normalization for dedupe. But four capabilities still run on
pre-migration machinery, and one governance gap is structural:

| # | Gap | Today's behavior | Cost |
|---|---|---|---|
| 1 | **Repair is regex, single-shot** | `_repair_value_casts` is a regex substitution for one pattern (bare aggregates), one pass; everything else → 400 | Fixable generations fail; auto-repair coverage is one pattern deep |
| 2 | **Gauntlet works on raw ASTs** | Table/CTE checks walk the un-qualified tree; CTE shadowing resolved via name-set heuristics | Subtle bypass/over-block classes the qualify pass makes exact |
| 3 | **Flywheel similarity is exact-match** | `normalize_sql` equality: alias-renamed or column-reordered SQL counts as "different"; decay misses semantically identical bad examples | Flywheel accumulates near-duplicates; decay under-fires |
| 4 | **Corrections are stored as opaque pairs** | (bad SQL, good SQL) rows; *what changed* is never extracted | Promoted examples can't teach the generator *classes* of fixes |
| 5 | **Cross-engine parity = generate-per-dialect** | Each engine gets its own prompt+generation path; parity test scripts SQL by hand (`DEMO_SQL` per dialect) | 2× generation cost drift, dialect templates diverge silently |
| 6 | **Version upgrades are ad-hoc** | `sqlglot>=26,<28` pin + 253-line semantics suite, but no runbook for moving inside the range | Upgrades happen late, under pressure, or never |

## 2. Guiding Principles

1. **Fail-closed stays fail-closed.** Every new validator capability must default to
   reject on any internal error; hardening may only *narrow* gray zones, never widen them.
2. **Semantics pins are the contract.** `tests/test_sqlglot_semantics.py` locks observed
   behavior; any phase that depends on new AST behaviors adds pins first, code second
   (the migration's proven pattern).
3. **Flag-gated, config-over-code** (project convention): each phase ships behind a
   `flags`/config switch; flag-off = byte-identical v1 behavior, proven by the existing
   suite (313+ tests, 20-question golden parity).
4. **Repairs are bounded and observable.** A repair without a telemetry counter is a
   silent behavior change; every repair pass logs which taxonomy class fired.
5. **Vendor-neutral** (project rule): everything below is sqlglot-native — no engine
   SDKs, no proprietary validators.

## 3. Current State (Audited)

Shipped and solid (do not rebuild):

- **Validation (Layer 1, hard)**: dialect-aware parse (both `TokenError` and
  `ParseError` caught — the load-bearing pin), single statement, root allowlist after
  bounded Subquery unwrap, mutation-node deny walk (incl. best-effort admin nodes),
  dangerous-function deny with last-segment matching, table-function deny, CTE-aware
  table allowlist (qualified names rejected), CTE depth ≤ 5. (`agent/validator.py`)
- **Canonical normalization**: `sqlglot.parse_one(sql).sql(comments=False).lower()` for
  flywheel dedupe/decay, with pinned fallback. (`feedback/hooks.py::normalize_sql`)
- **Dialect abstraction**: `Dialect.sqlglot_name` protocol; ClickHouse + Postgres
  implementations. (`database/contracts.py`, `database/dialects/`)
- **Semantic example selection**: embedding cosine over approved examples
  (`knowledge/store.py::fetch_semantic_examples`, wired at `agent/pipeline.py:279`).
- **Behavior pins**: `tests/test_sqlglot_semantics.py` — 253 lines of observed-behavior
  locks against sqlglot 27.29.0; upgrades fail loudly instead of silently drifting.

The gaps this plan closes are listed in §1.

## 4. Phase Overview & Order Rationale

| Phase | Scope | Why this position | Effort | Independently shippable |
|---|---|---|---|---|
| **S1** | AST repair loop v2 | Continues the shipped trajectory (regex → AST); most user-visible win (fewer 400s); measurement exists (repair counters + golden suite) | 3–4 d | ✅ flag `agent.repair_v2` |
| **S2** | Qualify-based gauntlet hardening | Safety core; qualify makes table/CTE analysis exact rather than heuristic; must precede S4 (transpile needs canonical ASTs) | 2–3 d | ✅ flag `agent.qualify` |
| **S3** | Structural flywheel similarity + diff mining | Builds on normalize_sql; improves learning precision; independent of S1/S2 runtime paths | 3–5 d | ✅ flag `flywheel.structural_similarity` |
| **S4** | Single-generation transpile for parity | Needs S2 (canonical ASTs) and the parity suite as its gate; halves generation cost on hybrid parity paths | 3–4 d | ✅ flag `generation.transpile_parity` |
| **S5** | Version governance | Small, always-on; lands last so it governs the full surface the other phases added | 1 d | ✅ (CI job + runbook) |

Dependency edges: S2 → S4 (strict); S1, S3 independent; S5 references all.

---

## 5. Phase S1 — AST Repair Loop v2

**Goal:** replace the single regex repair with a bounded, AST-transform-based repair
taxonomy and a multi-pass budget, with per-class telemetry.

### 5.1 Design

- **New module** `agent/repairs.py`; `validator.py` keeps Layer-1 rules untouched and
  delegates Layer 2 to it (current `_repair_value_casts` becomes taxonomy class
  `value-cast`, reimplemented as an AST transform via `exp.AggFunc.this` replacement).
- **Repair taxonomy (initial set, each an AST transform + its inverse test):**

| Class | Trigger | Transform |
|---|---|---|
| `value-cast` | bare aggregate over raw value column (existing behavior) | wrap arg in dialect `cast_numeric` |
| `add-limit` | unbounded `SELECT` (no LIMIT) on telemetry tables | append dialect LIMIT (bounded, e.g. 1000) |
| `strip-semicolon` / `strip-comments` | trailing junk that survives parse | AST-level regenerate |
| `qualify-columns` | unqualified/ambiguous column refs | S2's qualify output, reuse |
| `inline-cte-depth` | CTE depth > 5 | inline the deepest CTE once |

- **Loop shape:** `for pass in range(config.repair_passes)`: repair → re-validate →
  stop at first clean result; repairs applied cumulatively recorded in
  `ValidationResult.repairs_applied` (field exists — start populating it with real names).
- **Bounds:** max 3 passes; any transform that fails to re-parse → revert to original
  SQL and let validation reject (fail-closed — never execute a half-transformed tree).
- **Telemetry:** structured log + counter per class; golden suite question set gains
  adversarial repair cases (each class: input that only that repair can save).

### 5.2 Changes

| File | Change |
|---|---|
| `agent/repairs.py` | new — taxonomy transforms + bounded loop |
| `agent/validator.py` | Layer 2 delegates; `_repair_value_casts` regex removed |
| `config/settings.py` | `repair_passes: int = 1` (v1) / up to 3 when `repair_v2` |
| `tests/test_validator_adversarial.py` | per-class repair cases + revert-on-broken-transform cases |
| `tests/test_sqlglot_semantics.py` | pins for any newly relied-on transform behaviors |

### 5.3 Acceptance

- Flag off: all existing tests green, no behavior change (regex path byte-identical).
- Flag on: every taxonomy class has a test that fails v1 and passes v2 (provable coverage).
- A transform that produces unparseable SQL never reaches execution (fuzz: mutate the
  golden SQL corpus, assert invariant "repaired output either validates or reverts").
- Repair counters observable in logs; golden parity suite unaffected (repairs are
  generation-side only).

---

## 6. Phase S2 — Qualified Gauntlet Hardening

**Goal:** run `sqlglot.optimizer.qualify` (column/table qualification + canonicalization)
before the rule checks so the gauntlet reasons over fully-resolved ASTs.

### 6.1 Design

- `validator._hard_errors`: after parse + root unwrap, attempt
  `qualify(ast, dialect=..., schema=tenant_mapping)`; the tenant's EAV mapping supplies
  the schema so qualification resolves against *known* tables only.
- **What this makes exact:**
  - CTE shadowing: a CTE named like a real table can no longer confuse the table
    allowlist (qualify renames/normalizes scopes; checks walk qualified refs).
  - Ambiguous/qualified columns: `value` refs resolved to their source table;
    cross-CTE column leaks become explicit.
  - Star selects: `SELECT *` expands against the schema → deny-listable by config.
- **Failure semantics:** qualify raises (unknown table, ambiguous column, unsupported
  construct) → **reject** with the qualify error class. Qualify failure is a finding,
  not an inconvenience — the generator produced something that can't be resolved
  against the tenant's schema. (Fail-closed preserved; this *replaces* gray-zone
  accepts with explicit rejects.)
- **Pins first:** add semantics pins for qualify behaviors relied upon (CTE rename
  behavior, unknown-table error type, star expansion) before wiring.

### 6.2 Changes

| File | Change |
|---|---|
| `agent/validator.py` | qualify step + schema injection; table/CTE checks consume qualified refs |
| `config/settings.py` | `flags.qualify: true` default-off; `deny_star_selects: false` (initially) |
| `tests/test_validator_adversarial.py` | CTE-shadowing bypass attempts (must reject), star-select cases |
| `tests/test_sqlglot_semantics.py` | qualify behavior pins |

### 6.3 Acceptance

- Flag off: byte-identical v1 (suite green).
- Flag on: a hand-built battery of shadowing/ambiguity/qualification bypasses — all
  rejected; legitimate golden-suite SQL — all still pass (no over-blocking regression).
- Every qualify-rejected case returns a typed error string naming the cause
  (schema-unknown-table / ambiguous-column / star-select).

---

## 7. Phase S3 — Structural Flywheel Similarity + Correction Diff Mining

**Goal:** the flywheel stops seeing alias-renamed SQL as "new", and corrections are
mined as *structured deltas* instead of opaque pairs.

### 7.1 Structural similarity

- **Fingerprint:** qualify (S2) → `exp` tree walk producing a normalized shape signature
  (node-type histogram + table/key/filter/aggregation structure). Two SQLs with equal
  signatures are semantically identical for dedupe/decay purposes regardless of aliases,
  column order, or formatting.
- `normalize_sql` stays (cheap exact path); similarity layer above it:
  exact → structural → different. Applied to:
  - `save`-side dedupe of promoted examples (near-duplicate prevented at write),
  - `_decay` matching (corrected SQL decays *all* structural matches, not just exact).
- Threshold-free by design (signature equality, not a score) — no magic numbers.

### 7.2 Correction diff mining

- `sqlglot.diff` (or a thin AST-comparison over qualified trees) turns a correction
  into labeled deltas: `+filter(key=door.open_duration_s)`, `agg avg→max`,
  `+time_window(last 24h)`.
- Stored as JSONB on the feedback row (`correction_delta`); the review UI and the
  prompt builder can then surface *what kind of fix* each promoted example teaches,
  and the RAG-plan R2-style rewrite can use recurring delta patterns as signal.
- Purely additive mining (non-blocking, boundary-wrapped) — answers never depend on it.

### 7.3 Changes

| File | Change |
|---|---|
| `rag/`… no — `knowledge/` + `feedback/` | new `feedback/similarity.py` (signature + diff); `hooks.py::_decay` upgraded; feedback schema + `correction_delta` column (additive migration) |
| `feedback/store.py` (or equivalent) | signature persisted with promoted examples; structural dedupe on promote |
| `api/feedback_admin.py` | review payload exposes deltas |
| `tests/test_feedback_loop.py` | alias-rename dedupe cases, decay-matches-structural cases, delta extraction cases |

### 7.4 Acceptance

- Alias-renamed / column-reordered duplicate example is rejected at promote time (test).
- A correction to SQL X decays a *structurally identical* approved example (test).
- Delta extraction correctness: golden correction pairs → expected delta labels.
- All mining non-blocking: forced mining failure never breaks a query (test with
  sabotaged diff path).

---

## 8. Phase S4 — Single-Generation Cross-Engine Transpilation

**Goal:** generate SQL **once** in a canonical form, then transpile per engine with
sqlglot — parity becomes structural instead of prompt-parallel.

### 8.1 Design

- Today: each engine path carries its own dialect template in prompt assembly; parity
  is enforced by generating twice (and the golden parity suite hand-scripts `DEMO_SQL`
  per dialect). Divergence risk lives in the prompt layer.
- New path (flag-gated): generate against one canonical dialect prompt (Postgres
  grammar, engine-neutral function set) → validate → `sqlglot.transpile(sql,
  read="postgres", write=dialect.sqlglot_name)` → validate the transpiled AST **again
  through the same gauntlet in the target dialect** (transpilation is a transformation,
  not a trust boundary — the second validation is mandatory).
- Fallback: transpile output fails target validation → fall back to per-dialect
  generation (v1 path), log the divergence (this doubles as a drift detector).
- **Golden parity suite upgrade:** the scripted-SQL leg is replaced by a
  transpile-driven leg: one canonical answer per question, transpiled, executed on both
  engines, strict row equality — the parity claim becomes mechanical.

### 8.2 Changes

| File | Change |
|---|---|
| `agent/pipeline.py` / `agent/generator.py` | flag-gated canonical-generation + transpile step + second validation |
| `database/dialects/*` | any transpile post-fixes (e.g. ClickHouse cast functions) as dialect methods, tested |
| `tests/golden/test_golden_parity.py` | transpile-driven leg |
| `config/settings.py` | `generation.transpile_parity: false` default |

### 8.3 Acceptance

- Flag off: current behavior identical.
- Flag on: 20-question golden suite green through the transpile path on **both**
  engines, strict row equality — no hand-scripted per-dialect SQL.
- Divergence counter: any transpile→validate failure logged with both SQL forms
  (canonical + transpiled) for dialect-template repair.
- Generation calls on the parity path: exactly one LLM call per question (was two).

---

## 9. Phase S5 — sqlglot Version Governance

**Goal:** make sqlglot upgrades routine instead of scary.

### 9.1 Design

- **Runbook** `docs/internal/SQLGLOT_UPGRADE_RUNBOOK.md`: bump inside `>=26,<28` → run
  semantics pins → run full suite → triage per the pre-specced resolution rules already
  referenced in `test_sqlglot_semantics.py`'s header contract.
- **Weekly CI probe job** (`.github/workflows/`): installs the latest in-range sqlglot,
  runs the semantics suite only, reports drift as a non-blocking check. New pin
  behaviors surface days before anyone needs them.
- **Range-bump policy:** major bumps (28.x) get a dedicated branch: run probe at head,
  catalog pin failures, decide pin-by-pin — the migration's evidence-probe pattern
  (`.omo/evidence/task-2-sqlglot-implementation.txt` precedent) institutionalized.
- Phases S1–S4 add their new pins to the same suite → governance covers the whole AST
  surface automatically.

### 9.2 Acceptance

- CI probe job exists, green on current pin, scheduled weekly.
- Runbook exercised once for a minor in-range bump (dry-run acceptable) — end-to-end.

---

## 10. Cross-Cutting Concerns

- **Performance budget:** qualify + repairs + transpile add per-query CPU (no extra
  LLM calls anywhere in this plan except none). Budget: p95 validation-path increase
  < 50 ms; measured in the phase report.
- **Security review:** S2 changes the gauntlet — run the adversarial battery
  (`test_validator_adversarial.py`) plus a fresh red-team pass before flag default
  flips to on. S1's revert-on-broken-transform invariant is the safety property;
  fuzz it.
- **Provenance:** all mechanisms are sqlglot/Open Source-native (CONTRIBUTING.md
  clean-room rules as usual).
- **Interaction with the RAG plan:** none at runtime (separate subsystems); the plans
  share the flag-gated rollout pattern and can proceed in parallel or interleave.

## 11. Configuration Reference (End State)

```yaml
flags:
  repair_v2: true              # S1: AST repair taxonomy (false = single regex repair)
  qualify: true                # S2: qualified gauntlet (false = raw-AST checks)
  structural_similarity: true  # S3: signature-based flywheel dedupe/decay
  transpile_parity: false      # S4: single-generation + transpile (opt-in until proven)
agent:
  repair_passes: 3             # S1 bound
  deny_star_selects: false     # S2 option
```

## 12. Success Metrics

| Metric | Baseline | Target |
|---|---|---|
| Auto-repaired generations (share of validator inputs) | *measure: value-cast only* | ≥ 2× classes firing on real traffic |
| Validator 400 rate on golden adversarial battery | current pass rate | shadowing/ambiguity class: 100% reject |
| Flywheel near-duplicate promoted examples | *count before S3* | 0 |
| Decay precision (corrections that decay ≥1 example) | *measure* | ≥ exact-match baseline (strictly more) |
| Parity-path LLM calls per question | 2 (per-dialect) | 1 (S4 flag on) |
| sqlglot upgrade lead time | ad-hoc | < 1 day, runbook-driven |

## 13. Timeline & Sequencing

```text
Week 1      S1  AST repair loop v2                    (3–4 d)
Week 2      S2  qualified gauntlet hardening          (2–3 d)
Week 2–3    S3  flywheel similarity + diff mining     (3–5 d)
Week 3–4    S4  transpile parity (after S2)           (3–4 d)
any time    S5  governance (1 d; last, so it governs the full surface)
```

S1 and S3 are independent; S2 → S4 is the only hard edge. This program interleaves
freely with the RAG plan (R1–R4) — different subsystems, same rollout pattern.

## 14. Out of Scope / Deferred

- **LLM-based SQL repair** (feed validator errors back for a regeneration attempt):
  the repair-then-regenerate loop is a separate, cost-bearing design; revisit after S1
  shows which classes remain unrepairable.
- **sqlglot optimizer full pipeline** (`optimize` with pushdown/join planning): overkill
  for read-only single-table EAV queries; qualify alone closes the real gaps.
- **Query result semantic diffing** (row-level parity beyond strict equality):
  the golden suite's strict equality is the contract; approximate parity invites drift.
- **Dialect expansion beyond ClickHouse/Postgres** (BigQuery, Snowflake): the
  `Dialect.sqlglot_name` protocol makes it mechanical, but no demand yet.
