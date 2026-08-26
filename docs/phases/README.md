# Phase Plans & Review Workflow

Working agreement: **docs first → your review → implement → verify → mark complete.**
No phase starts implementation before its doc is approved.

| Phase | Title | Status | Doc |
|---|---|---|---|
| 0 | Scaffold | ✅ COMPLETE | [PHASE_0.md](PHASE_0.md) |
| 1 | Core NL→SQL vertical slice | ✅ COMPLETE (live-run items skipped by owner) | [PHASE_1.md](PHASE_1.md) |
| 2 | Memory, streaming, probe | ✅ COMPLETE | [PHASE_2.md](PHASE_2.md) |
| 3 | Conversational intelligence (flags) | ✅ COMPLETE | [PHASE_3.md](PHASE_3.md) |
| 4 | Feedback flywheel | ✅ COMPLETE | [PHASE_4.md](PHASE_4.md) |
| 5 | Document RAG + routing + advisor | ✅ COMPLETE | [PHASE_5.md](PHASE_5.md) |
| 6 | Backend pluralism + semantic examples | ✅ COMPLETE | [PHASE_6.md](PHASE_6.md) |
| 7 | v1.0 polish & release | ✅ COMPLETE — **project v1.0** | [PHASE_7.md](PHASE_7.md) |
| — | *v1.1 RAG upgrade track (R1–R4)* | | [*plan*](../blueprint/RAG_UPGRADE_PLAN.md) |
| R1 | Golden set + retrieval eval harness | ✅ COMPLETE (live baseline pending owner) | [R1.md](R1.md) |
| R2 | Session-aware query rewriting | ✅ COMPLETE (live followup R@5 pending owner) | [R2.md](R2.md) |
| R3 | Hybrid BM25 + dense, RRF fusion | ⬜ planned | — |
| R4 | Structure-aware chunking + metadata/versioning | ⬜ planned | — |
| — | *v1.1 SQL/sqlglot track (S1–S5)* | | [*plan*](../blueprint/SQLGLOT_UPGRADE_PLAN.md) |

## Review workflow

1. You read a phase doc: scope, acceptance criteria, open questions, review checklist.
2. You answer open questions / request changes — anything from rename to re-scope.
3. Doc updated, re-reviewed if the change is structural.
4. On your "go": implementation runs to the doc's acceptance criteria only.
5. Evidence (gate results, live outputs) recorded in the phase doc; status → COMPLETE.
6. Next phase.

## Ground rules

- Acceptance criteria are verifiable or they don't ship.
- Every phase leaves all four gates green: pytest, ruff check, ruff format --check, basedpyright.
- Scope creep found mid-phase → new/updated doc line first, then code.
- The build-order memory hook: contracts (0) → slice (1) → memory (2) → conversation (3) →
  flywheel (4) → docs/RAG (5) → plural backends (6) → polish (7).
