# Contributing to DataMind

## Setup

```bash
uv sync                       # core deps
uv sync --extra clickhouse    # + ClickHouse adapter
uv run pytest                 # unit + golden parity suite
```

## Workflow

1. **Docs first.** Anything phase-sized lands as a doc under `docs/phases/`
   and gets reviewed before implementation.
2. **TDD.** Red → green → refactor. Behavior ships with the tests that lock it.
3. **Gates before merge:** `ruff check`, `ruff format --check`, `basedpyright`,
   `pytest` — all clean, no exceptions (`make`-style: see CI).
4. **Flag-gated features.** New agent stages default OFF in
   `config/defaults.yaml`; flags-off behavior must stay byte-identical.

## Code conventions

- Python 3.11+, strict typing (basedpyright `all`), frozen dataclasses,
  Pydantic v2 at boundaries, protocols over ABCs, no `Any`/`# type: ignore`.
- Engine-specific code lives ONLY in `database/dialects/` + store adapters;
  the agent core never imports a DB client.
- Fail-open boundaries: dependency outages degrade (keyword fallback,
  SQL-only hybrid), logged — never a raw 500.
- Multiline SQL/prompt strings use implicit concatenation (house style).

## Commit style

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`); one
logical change per commit.

## Provenance

This project is a clean-room rebuild. Contributions must not copy code,
prompts, schemas, or identifiers from any proprietary system the
contributors have worked on. See `SECURITY.md` and the phase docs for the
sanitization rules that apply before anything is published.
