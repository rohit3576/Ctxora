# Phase 4 — Feedback Flywheel

> Status: **COMPLETE (code + gates + live token-gate boot) — 2026-08-19**
> Depends on: Phase 3 (correction turns exist to mine)
> Plan source: architecture §8 Phase 4 · spec: blueprint §12

## Goal

Thumbs up/down → review queue → approved pairs become few-shot examples (with embeddings)
→ future prompts retrieve them; bad examples decay when contradicted.

## Scope

**In:** feedback capture · auto-mining of successful corrections · admin review endpoints ·
promotion + embedding · decay/demotion · golden-eval export.

**Out:** graduation (registry fixes from recurring feedback) — deferred to Phase 7 polish;
advisor/RAG untouched.

## Deliverables

| File | What it does |
|---|---|
| `feedback/schema.sql` | `query_feedback(id, tenant, session_id, history_id, nl_query, generated_sql, feedback_type, user_comment, corrected_sql, status[pending/auto_pending/approved/rejected/review], reviewed_by, created_at)` + example-table migration (`embedding`, `status`, `provenance_feedback_id`, `use_count`, `last_used_at`, `corrections_after_use` — some columns already exist from Phase 1 schema) |
| `feedback/capture.py` | `POST /v1/feedback` `{historyId, rating: up|down, comment?}` → row; tenant derived server-side from the history row (never client-supplied) |
| `feedback/mining.py` | after a successful correction turn: insert `auto_pending` row (question → corrected SQL pair); non-blocking |
| `feedback/promotion.py` | approve → INSERT/UPDATE `sql_agent_sql_examples` with embedding (`llm.embed`), `status='approved'`, provenance link; **invalidate knowledge cache** for the tenant; decay: when a correction supersedes SQL matching an approved example → demote to `review`, bump `corrections_after_use`; re-promotion after fix supported |
| `api/feedback_admin.py` | token-gated (`X-Admin-Token` == `FEEDBACK_ADMIN_TOKEN`, fail-closed 403): `GET /admin/feedback/pending|stats`, `POST /admin/feedback/{id}/approve|reject`, `POST /admin/feedback/auto-promote-positive` (batch), `GET /admin/feedback/golden-eval` (export approved pairs as JSON) |
| `flags` | `flags.feedback_capture` gates capture + admin routers |

## Acceptance criteria — results

- [x] Thumbs-down with comment lands in pending (tenant derived server-side from the history row); stats endpoint counts it
- [x] Approve → example row carries embedding + `approved` + provenance; **knowledge cache invalidation observable** (invalidations counter increments; re-load pulls fresh)
- [x] Promoted example retrieval: post-approve prompts serve the promoted Q/SQL via the example-slice path (wiretap test)
- [x] Successful in-session correction auto-creates an `auto_pending` row (question → corrected SQL), zero user action
- [x] Correcting SQL that matches an approved example → example demoted to `review`, `corrections_after_use` bumped (normalized-string match)
- [x] Admin endpoints 403 without/with-wrong token, **fail-closed when `FEEDBACK_ADMIN_TOKEN` unset** (live-verified: 403/403; typed `503 FEEDBACK_STORE_UNAVAILABLE` when PG is down with the right token)
- [x] Golden-eval export returns `{question, sql, tenant}[]` on a 200 envelope
- [x] Gates: **pytest 196 passed + 10 integration skipped · ruff clean · format clean · basedpyright 0 errors**

## Evidence

- Unit (`tests/test_feedback_loop.py`, 13): protocol conformance, SQL normalization, mining
  (auto_pending, skip-without-history), decay demotion, approve+embed+invalidate, reject,
  re-approve guard, auto-promote-positive filtering, golden export, capture history guard
- API (`tests/test_feedback_api.py`, 11): capture→pending→stats; 404 unknown history;
  routers absent flag-off; token matrix (missing/wrong/unset); full loop with cache
  invalidation + re-promotion; correction auto-mining e2e; golden export shape; store
  outage → typed 503
- Live boot (uvicorn, flag temporarily on, PG down): 403 / 403 / typed-503 sequence
  recorded above; flag restored and re-verified off afterwards

## Decisions taken (doc defaults)

1. Decay match: normalized string (whitespace-collapsed, lowercased) equality
2. auto-promote-positive: manual batch endpoint only (no scheduler)
3. Golden-eval: `{question, sql, tenant}[]`
4. Anonymous feedback allowed (`user_email` column reserved; no auth layer yet)
5. (Added during build) admin handlers wrapped in an outage boundary (`_with_boundary`)
   after live boot exposed a raw 500 on PG-down with a valid token — same boundary
   discipline as Phase 2

## Open questions for review

1. **SQL match for decay** — normalized-string equality (current plan: strip whitespace,
   lowercase) vs fuzzy similarity threshold?
2. **auto-promote-positive** — batch endpoint exists; should it also be a scheduled job
   (no — Phase 7 ops), keep manual?
3. **Golden-eval format** — `{question, sql, tenant}[]` JSON; agree?
4. **Feedback from anonymous users** — allowed with `user_email=null`? (current plan: yes)

## Review checklist

- [ ] Loop scope (no graduation yet) agreed
- [ ] Decay semantics agreed
- [ ] Open questions answered
