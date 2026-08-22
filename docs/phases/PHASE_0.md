# Phase 0 — Scaffold

> Status: **COMPLETE** · Date: 2026-08-19 · Plan: `docs/architecture/ARCHITECTURE.md` §8 Phase 0

## Scope

Runnable empty service + infra + the load-bearing contracts. No agent logic.

## Delivered

| Deliverable | Files | Notes |
|---|---|---|
| Repo skeleton | per §3 layout | `api/ agent/(deferred) database/ config/ llm/ tests/` — agent dirs arrive with Phase 1 |
| Config system | `config/settings.py`, `config/defaults.yaml` | Two layers: env `Settings` (pydantic-settings) + YAML `AppConfig` (frozen, `extra="forbid"` → typos fail at boot); column mapping is the integration surface for any KV store |
| Contracts | `database/contracts.py`, `llm/client.py` | `Dialect`, `TelemetryStore`, `LLMClient` protocols + frozen result dataclasses — the walls everything later builds against |
| Dialects | `database/dialects/{clickhouse,postgres}.py` | Full portability matrix (§7) as code; golden-string tests lock it |
| Store stubs | `database/{clickhouse,postgres}_store.py` | Adapter wiring only; `execute`/`introspect_*` raise `NotImplementedError("… Phase 1")` |
| Metadata probe | `database/metadata.py` | `SELECT 1` readiness check only |
| Health API | `api/health.py`, `api/schemas.py`, `main.py` | `/healthz` (no DB) + `/readyz` (503 path proven); typed envelope; router factory DI, no ambient `app.state` reads |
| Infra | `docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml` | pg+pgvector (CH behind `--profile clickhouse`); multi-stage non-root image; CI: ruff + basedpyright + pytest on 3.11/3.12 |
| Docs & legal | `README.md`, `LICENSE` (MIT), `.env.example`, `.gitignore` | Phase table in README tracks progress |

## Acceptance evidence

| Criterion (from §8) | Result |
|---|---|
| `pytest` green with contracts imported | **43/43 passed** (0.31s) — config validation (9), dialect goldens (19), contracts/stubs/fakes (11), health E2E over real app (3) + fixture |
| Config validation rejects bad YAML | proven: unknown key, missing column, wrong type, unknown adapter, malformed YAML → `ConfigError` naming the field |
| `/healthz` 200 without any DB | **live boot verified**: `{"status":"Success","data":{"status":"ok"}}` HTTP 200 |
| `/readyz` reflects DB state | **live boot verified**: DB unreachable → HTTP 503 `Failure/unreachable`; monkeypatched-ok → 200 (test) |
| Quality gates | `ruff check` **clean** (select=ALL) · `ruff format --check` clean · `basedpyright` **0 errors** (mode=all) |
| Module size ceiling (250 LOC) | max file = 162 pure LOC (`config/settings.py`) |
| Leak scan | 0 matches for company identifiers across `.py/.md/.toml/.yml` |

## Decisions & deviations

1. **`httpx2` instead of `httpx`** as the test-client transport — starlette's 2026 deprecation + project standard.
2. **`reportUnusedParameter = "none"`** (pyproject) — protocol stubs and fakes legitimately ignore params.
3. **`reportAny = "warning"`** (pyproject, documented inline) — `types-PYYAML` is untyped upstream; the single boundary lives in `_parse_yaml_file`, result narrowed to `object` + isinstance-checked.
4. **Router factory DI** instead of `request.app.state` — keeps `app.state` reads out of typed code (it's an `Any` hole).
5. **Docker compose boot not exercised locally** (no engine confirmed on this machine); image build is CI-covered; Phase 1 integration tests will boot the compose stack.

## Not in this phase (by design)

Agent pipeline (S0–S13), knowledge store, LLM implementation, sessions/history, streaming — all Phase 1+. Stubs raise `NotImplementedError` naming their phase.

## Next: Phase 1 — Core NL→SQL vertical slice

See `docs/architecture/ARCHITECTURE.md` §8 Phase 1. Internal working reference: `docs/internal/REBUILD_NOTES.md` (gitignored, never published).
