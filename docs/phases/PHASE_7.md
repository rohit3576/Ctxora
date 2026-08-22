# Phase 7 — v1.0 Polish & Release

> Status: **COMPLETE (code + gates + live boot) — 2026-08-19 · project at v1.0**
> Depends on: Phase 6
> Plan source: architecture §8 Phase 7

## Goal

Production-hardening and the public story: self-service onboarding, real auth, demo UI,
release packaging. This is the "portfolio-ready" phase.

## Scope

**In:** onboarding wizard completion · auth module · rate limiting · Streamlit demo panel ·
Helm chart · golden regression in CI · README/branding.

**Out:** multi-region/scaling; LLM intent router (backlog); graduation queue (backlog).

## Deliverables

| File | What it does |
|---|---|
| `onboarding/wizard.py`, `naming.py` | probe → naming suggestions (rule-based from key names + optional LLM polish) → review queue (`sql_agent_key_mapping_candidates`) → promote approved rows into registry+aliases (cache invalidation) → activation gate flips tenant live; `POST /v1/onboarding/{tenant}/enable|disable`, `GET …/readiness|state` |
| `api/auth.py` | verified-JWT tenant claim (`TENANT_CLAIM` env names the claim); dev-mode fallback: explicit `tenant` field allowed when `AUTH_DISABLED=true` (default in compose, warned in logs); activation gate: un-activated tenant → 403 on query endpoints |
| `api/ratelimit.py` | per-tenant token bucket (in-memory; Redis hook stubbed behind a Protocol for later) — default 60 req/min, config-exposed |
| `demo/panel.py` | Streamlit: chat with streaming answers, SQL display, feedback buttons, upload docs, probe wizard |
| `deploy/helm/` | chart: deployment (probes /healthz /readyz), service, configmap for defaults.yaml, secret refs, HPA stub |
| `.github/workflows/ci.yml` | add golden regression job (consume Phase 4 golden-eval export) + docker build + adapter matrix from Phase 6 |
| `README.md` | final: GIFs, quickstart, architecture diagram, "point at your data" section, roadmap table → done |
| `CONTRIBUTING.md`, `SECURITY.md` | contribution guide; security notes (read-only store user, token gate, tenant isolation) |
| `docs/` sweep | phases marked complete; blueprint/architecture "as-built" pass |

## Acceptance criteria — results

- [x] Fresh-clone path documented end-to-end (README quickstart: sync → compose → bootstrap → seed → serve); demo panel (`demo/panel.py`) boots and parses
- [x] Onboarding wizard complete: probe → **naming suggestions** (rule-based from key names) → **review queue** (`sql_agent_key_mapping_candidates`; agent never reads it) → approve promotes into registry + aliases with cache invalidation → **enable/disable** flips status; full loop e2e-tested
- [x] Auth: `AUTH_DISABLED=false` + `JWT_SECRET` + `TENANT_CLAIM` → verified HS256 (exp required, optional iss); **claim overrides request tenant** (gate returns the effective request — a real bug the tests caught); missing/expired/wrong-signature/missing-claim/no-secret → 401; dev mode warns once and passes (live-verified in boot log)
- [x] Activation gate: disabled tenant → 403 `TENANT_NOT_ACTIVE`; **unknown tenant stays 422** (gate checks exists-and-disabled, fail-open on outage — second real bug found by wiring)
- [x] Rate limit: `flags.ratelimit` + in-memory token bucket; burst exhaustion → 429 with retry-after; per-tenant isolation proven (no cross-tenant 429 leakage)
- [x] CI (`.github/workflows/ci.yml`): quality (2 pythons) → docker build → **PG integration on main** (pgvector service) → **ClickHouse integration behind the `clickhouse` PR label** — the Phase-6 deferred matrix, now wired
- [x] Helm chart (`deploy/helm/`): Chart/values/deployment/service/configmap with probes and resource limits
- [x] `CONTRIBUTING.md` (docs-first workflow, gates, provenance rules) + `SECURITY.md` (layered SQL safety, tenant isolation, fail-closed admin, secrets) + README final (v1.0 phase table all ✅)
- [x] Gates: **pytest 314 passed + 11 integration skipped · ruff clean · format clean · basedpyright 0 errors** · leak scan 0

## Evidence

- `tests/test_auth.py` (8) · `tests/test_ratelimit.py` (4) · `tests/test_wizard.py` (10) ·
  `tests/test_phase7_acceptance.py` (10) · readiness-outage regression (1)
- Live boots (uvicorn, PG down): dev-mode warning logged once; query → typed 503;
  readiness → 200 with honest `false` checklist (catch #5: raw 500 found live, fixed,
  regression-tested); wizard enable → typed 503
- Panel: `ast.parse` clean + boots under streamlit (attribute-dynamic UI, env-scoped
  type rules like demo seeds)

## Decisions taken (doc defaults + build notes)

1. `TENANT_CLAIM=tenant`, HS256, in-memory rate limits, Streamlit panel, lean Helm, MIT — all as the doc proposed
2. **Dev-mode default** (`AUTH_DISABLED=true`): the wire contract and every existing test stay intact; enabling auth is a pure config flip. Dev mode warns loudly, once per process
3. (Build note) two real bugs surfaced by wiring the gates: the auth tenant override
   mutated a copy (fixed: `_gate` returns the effective request) and the activation
   check mislabeled unknown tenants as inactive (fixed: exists-AND-disabled semantics,
   fail-open on outage)
4. (Build note) `api/auth` became a package so the PyJWT partial-stubs env applies
   (executionEnvironment roots must be directories — same lesson as rag/office)
5. Deferred (recorded): Redis-backed limits, graduation queue, LLM intent router — backlog

## Test plan

E2E: wizard flow (fake LLM); auth matrix (valid/invalid/missing/dev-mode); rate-limit
bursts; panel smoke (Streamlit app boots, one scripted exchange). CI: golden job green on
both adapters.

## Open questions for review

1. **Auth claim name** — `TENANT_CLAIM=tenant` default; what does your target IdP use?
2. **Rate limit store** — in-memory (restart resets counters) acceptable for v1.0?
3. **Demo panel tech** — Streamlit (current plan) vs nothing-but-curl in README?
4. **Helm vs compose-only** — ship Helm chart in v1.0 or defer to v1.1?
5. **License/authorship** — MIT, your name; confirm before first push.

## Review checklist

- [ ] v1.0 demo definition agreed
- [ ] Auth approach agreed
- [ ] Defer-list agreed (graduation, LLM router, Redis limits)
