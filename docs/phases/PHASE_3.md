# Phase 3 — Conversational Intelligence (flags on)

> Status: **COMPLETE (code + gates + live parity boot) — 2026-08-19**
> Depends on: Phase 2 (sessions, history, supersedes column)
> Plan source: architecture §8 Phase 3 · spec: blueprint §5 S1–S3

## Goal

The agent handles "no, I meant maximum", "what about truck-103?", and missing dimensions —
each behind its own feature flag, default off.

## Scope

**In:** correction loop · follow-up resolution · assume-first defaults · session digest.

**Out:** feedback mining of corrections (4) — the correction machinery here just marks
`supersedes_id`; mining is Phase 4.

## Deliverables

| File | What it does |
|---|---|
| `agent/correction.py` | S1: regex triggers ("no", "wrong", "i meant", "try again"…) with guards ("no data", "now", "know" demote to LLM); LLM classifier fallback (temp 0.0) → `is_correction` + `corrected_question`; regenerates at temp 0.3; **one regeneration cap** — second consecutive failure asks a targeted clarifying question; marks `supersedes_id` |
| `agent/followup.py` | S2: loads last N turns from history; rewrites pronouns ("it"/"that") with last entity + last metric; long sessions fold to digest |
| `agent/assume_first.py` | S3: fills missing aggregation (per-key registry + `agent.aggregation_defaults` glob patterns), time window (`agent.default_time_window`), entity (last-discussed → tenant default); emits `assumptionNote`; clarifies ONLY when nothing resolvable |
| `memory/digest.py` | rolling session summary (LLM, cached per session, invalidated on new turn) |
| `agent/pipeline.py` (extend) | stage order S0(greeting, ships here) → S1 → S2 → S3 → existing S5+; each stage flag-gated; `assumptionNote` in response envelope |
| `config/defaults.yaml` | `flags.correction_loop / assume_first / session_digest` flip to test-on (ship default off) |

## Contracts & flags

- `flags.correction_loop`, `flags.assume_first`, `flags.session_digest` — each stage is a
  no-op pass-through when off (pipeline must not import disabled stage behavior).
- New response field: `assumptionNote: string | null`.

## Acceptance criteria — results

- [x] Ask → "no, I meant maximum" → repaired SQL at **temperature 0.3**; history turn records `supersedes_id` lineage
- [x] Second consecutive complaint → clarifying question via `followUpQuestions`, no SQL (`data: null`)
- [x] "what about truck-103?" after a truck-102 turn → follow-up resolution runs (metric carry-over)
- [x] "average speed" (no time, no entity) → answer + `assumptionNote` mentioning `today` and fleet scope
- [x] "there is no data for speed" → guard demotes to classifier → NOT a correction (temp 0.0 path)
- [x] All flags off → Phase 2 contract byte-identical (parity test + live boot: greeting flag off routes "hi" through the normal pipeline)
- [x] Gates: **pytest 173 passed + 10 integration skipped · ruff clean · format clean · basedpyright 0 errors**

## Evidence

- Unit: triggers/guards/classifier/cap (`test_correction.py`, 12), followup+assume (`test_followup_assume.py`, 9),
  digest cache (`test_digest.py`, 4), greeting+context (`test_greeting_context.py`, 10)
- Acceptance e2e with flags-on config: `tests/test_phase3_acceptance.py` (8) incl. lineage assertion on the
  in-memory store and temperature capture (0.3 regen / 0.0 normal)
- Live boot on shipped defaults: `/healthz` 200; SSE stages flow; no behavior change vs Phase 2

## Test plan

Unit: trigger/guard regex matrix; classifier output handling; one-cap state machine;
pronoun rewrite; default filling + glob matching ("battery*" → latest). Fake-LLM e2e:
correction conversation; follow-up conversation. Integration: supersedes lineage in PG.


## Decisions taken (doc defaults + one deviation)

1. Clarifying-question shape: `followUpQuestions: [question]` + `data: null` on a 200 envelope
2. Correction scope: previous turn only
3. Digest threshold: 10 turns (`agent.digest_turn_threshold`)
4. **Deviation:** added `flags.followup` and `flags.greeting` (doc listed three flags) so
   "all flags off = Phase 2 identical" holds strictly — S0/S2 change behavior and needed their own gates
5. Fast-path corrections skip the LLM classifier entirely (merge string); classifier only runs
   when a guard phrase demotes the decision

## Open questions for review (resolved as above)

1. **Clarifying-question shape** — plain text question in `summary` + `followUpQuestions[]`,
   or a dedicated `clarification` field on the envelope?
2. **Correction scope** — only the previous turn correctable, or any earlier turn in session
   (current plan: previous turn only, KISS)?
3. **Digest threshold** — sessions longer than N turns get digests; N = 10?

## Review checklist

- [ ] Flag defaults (all off) agreed
- [ ] One-regen cap + clarify agreed
- [ ] Open questions answered
