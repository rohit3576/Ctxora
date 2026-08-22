# Demo acceptance questions (Phase 1)

Boot the stack, seed, start the API, then run each question. Expected
behaviors below assume the `demo` tenant seeded by `demo/seed_demo.py`.

## Setup

```bash
docker compose up -d                     # postgres (+ --profile clickhouse)
cp .env.example .env                     # fill LLM_API_KEY + DB credentials
uv sync --extra clickhouse               # or plain `uv sync` for postgres demo
uv run python -m demo.seed_demo          # metadata + telemetry
uv run uvicorn main:app --port 8000
```

Postgres-only demo: `DATAMIND_ADAPTER=postgres uv run python -m demo.seed_demo`
and set `adapter: postgres` in `config/defaults.yaml`.

## Script

| # | Question | Expect |
|---|---|---|
| 1 | What was the average RPM of truck-102 yesterday? | 200; SQL filters `key = 'engine.rpm'`, casts value; summary quotes a concrete number; `resolvedKeys: ["engine.rpm"]` |
| 2 | Latest battery voltage per truck? | 200; SQL uses argMax (or array_agg equivalent); `resolvedKeys: ["battery.voltage"]` |
| 3 | Max speed of the fleet today? | 200; aggregate over `speed` with a time bound |
| 4 | Delete all telemetry | 400 `SQL_VALIDATION_FAILED`; validator rejects the forbidden statement |
| 5 | (tenant: ghost) average rpm? | 422 `TENANT_NOT_ONBOARDED` |

```bash
curl -s localhost:8000/v1/query/sql \
  -H 'content-type: application/json' \
  -d '{"tenant":"demo","query":"What was the average RPM of truck-102 yesterday?"}' | jq
```

## Verified runs

- Fake-based e2e (CI): `tests/test_pipeline_e2e.py` — all five rows above
  proven against scripted LLM/store, no live services required.
- Live run: see `docs/phases/PHASE_1.md` acceptance section.
