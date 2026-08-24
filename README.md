# Ctxora

**The open-source semantic query layer for IoT telemetry.**

Point it at any key-value telemetry database (`timestamp + entity + key + value`), and your users can ask:

> "What was the average RPM of truck-102 yesterday?"

…in plain English. Ctxora resolves the right keys from your registry, generates validated **read-only** SQL for your engine (ClickHouse and PostgreSQL/Timescale are tested peers), executes it safely, and explains the result — with conversation memory, a correction loop, a human-reviewed feedback flywheel, and document RAG with cited sources.

```text
User question → auth + rate limit → intent routing ─┬→ SQL agent: key resolution (RAG over your registry)
                                                    │   → prompt assembly → LLM → validation + auto-repair
                                                    │   → read-only execution → natural-language answer
                                                    └→ Document RAG: ingest → chunk → embed → cited answer
Memory: sessions, history, correction lineage · Flywheel: capture → review → promote → decay
```

## Status — v1.0

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold: contracts, config system, health endpoints, infra | ✅ |
| 1 | Core NL→SQL vertical slice (demo tenant, ClickHouse) | ✅ |
| 2 | Sessions, history, SSE streaming, onboarding probe | ✅ |
| 3 | Correction loop, follow-ups, assume-first (all flag-gated) | ✅ |
| 4 | Feedback flywheel (capture → review → promote → decay) | ✅ |
| 5 | Document RAG + hybrid routing + advisor | ✅ |
| 6 | PostgreSQL/Timescale adapter + semantic examples | ✅ |
| 7 | v1.0: auth, rate limiting, onboarding wizard, demo panel, Helm, CI | ✅ |

Spec: [`docs/blueprint/IMPLEMENTATION_BLUEPRINT.md`](docs/blueprint/IMPLEMENTATION_BLUEPRINT.md) · architecture: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) · diagrams: [`docs/architecture/DIAGRAMS.md`](docs/architecture/DIAGRAMS.md) · system design: [`docs/architecture/SYSTEM_DESIGN.md`](docs/architecture/SYSTEM_DESIGN.md) · phase logs: [`docs/phases/`](docs/phases/)

## Quickstart

```bash
uv sync                                  # deps (uv: https://docs.astral.sh/uv/)
cp .env.example .env                     # fill LLM_API_KEY + DB credentials
docker compose up -d                     # postgres (+ --profile clickhouse for CH)
uv run python -c "from database.metadata import bootstrap_schema; from config.settings import get_settings; bootstrap_schema(get_settings())"
uv run python -m demo.seed_demo          # synthetic fleet + knowledge seed
uv run uvicorn main:app --port 8000      # API at /docs

curl -s localhost:8000/v1/query/sql \
  -H 'content-type: application/json' \
  -d '{"tenant":"demo","query":"What was the average RPM of truck-102 yesterday?"}' | jq
```

Demo UI: `uv run streamlit run demo/panel.py`

## Point it at your data

Edit `config/defaults.yaml` — map your column names, choose your adapter. No code changes:

```yaml
stores:
  telemetry:
    adapter: postgres        # or clickhouse
    mapping:
      table: "{tenant}_telemetry"
      timestamp: event_time  # ← your column names
      entity_id: device_id
      key: metric
      value: reading
```

Onboard a tenant without touching code: probe its keys, review suggested
names, activate — all through `/v1/onboarding/*` (or the demo panel).

## Production notes

- **Auth**: set `AUTH_DISABLED=false` + `JWT_SECRET` + `TENANT_CLAIM`; the
  verified claim overrides the request tenant. Dev mode (default) is loud.
- **Rate limiting**: `flags.ratelimit: true` (per-tenant, in-memory).
- **Admin review**: `FEEDBACK_ADMIN_TOKEN` gates `/admin/feedback/*` (fail-closed).
- **Helm**: `deploy/helm/` (deployment, service, configmap, probes).

## Quality gates

`ruff check` · `ruff format --check` · `basedpyright` (mode=all, 0 errors) ·
`pytest` — 313+ tests including a 20-question golden parity suite that runs
every question against **both** storage engines.

## Resources

- Spec: [`docs/blueprint/IMPLEMENTATION_BLUEPRINT.md`](docs/blueprint/IMPLEMENTATION_BLUEPRINT.md)
- Architecture: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) · Diagrams: [`docs/architecture/DIAGRAMS.md`](docs/architecture/DIAGRAMS.md) · System design: [`docs/architecture/SYSTEM_DESIGN.md`](docs/architecture/SYSTEM_DESIGN.md)
- Phase logs: [`docs/phases/`](docs/phases/)
- Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md) · Security: [`SECURITY.md`](SECURITY.md)
- API reference: run the server and open `/docs` (OpenAPI UI)

## License

MIT — see [LICENSE](LICENSE). Built as a clean-room rebuild; see
[CONTRIBUTING.md](CONTRIBUTING.md) for the provenance rules.
