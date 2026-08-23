# Security Notes

Ctxora executes LLM-generated SQL against your telemetry store. The
safety model is layered:

## SQL safety

- **Read-only by construction**: generated statements must be `SELECT`/`WITH`;
  a forbidden-verb blocklist (dialect-specific, incl. engine admin verbs)
  rejects everything else before execution. One auto-repair pass (value-cast
  wrapping) — no blind retries.
- **Table allowlist**: only the tenant's mapped tables are permitted.
- **Defense in depth**: use a read-only DB user/role for the telemetry store.
  The validator is a belt, grants are the suspenders.

## Tenant isolation

- Tenant identity comes from a **verified JWT claim** when `AUTH_DISABLED=false`
  (default is dev mode with loud warnings — never run dev mode in production).
- Knowledge, sessions, feedback, and RAG documents are all tenant-scoped
  server-side; clients never assert their own tenant in enforced mode.

## Admin surface

- `/admin/feedback/*` is fail-closed behind `X-Admin-Token`
  (`FEEDBACK_ADMIN_TOKEN`); unset token means every request is 403.

## Secrets

- All credentials via environment variables (`.env.example` documents them);
  never commit `.env`. Rotate the admin token and JWT secret independently.

## Rate limiting

- Per-tenant in-memory token bucket (`flags.ratelimit`). Counters reset on
  restart — front with a real limiter for hostile environments.

## Reporting

Open a private security advisory (GitHub Security Advisories) rather than a
public issue for anything exploitable.
