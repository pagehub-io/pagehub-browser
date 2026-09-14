# pagehub-browser

Multi-session **HTTP-over-headless-browser** service. Each browser action (navigate,
locate an element, click, read text/DOM, screenshot, …) is one ordinary HTTP request
step, scoped to an **isolated session**. Built so `pagehub-evals` can grade pure-frontend
web apps the same way it grades APIs: a collection step is a browser action; `session_id`
is captured from `POST /v1/sessions` and substituted into every following step.

Element targeting uses a **structured `Locator` object** (`getByRole` / `getByText` /
`getByLabel` / …) — `css`/`xpath` is the supported escape hatch, never the headline.
Resilient locators are the whole point; CSS-string-everywhere is why E2E suites rot.

Stack: FastAPI + Pydantic, no DB. Engine is pluggable (`api/engine/base.py`); the only
shipped engine is Playwright/Chromium. Runs on Modal in production, docker-compose locally.

## Quick start (local)

```bash
cp .env.example .env
make up                       # docker-compose, port :4010
curl localhost:4010/health
```

Or without Docker:

```bash
make install                  # pip install -e ".[dev]" + playwright install chromium
uvicorn api.main:app --port 4010
```

OpenAPI docs at `http://localhost:4010/docs` and `/redoc`.

To drive a **local target frontend** from the eval loop, point `navigate` at
`http://host.docker.internal:<port>/` (the compose file maps that to the host gateway).
`.env.example` ships `BROWSER_ALLOW_PRIVATE_HOSTS=true`, which — *only* when `ENV=development` —
lets `navigate` reach loopback / RFC1918 / link-local hosts; in staging/prod the flag is ignored
and the strict deny-list always applies.

## API surface

Base URL `http://<host>/v1`. Every route declares a Pydantic `response_model`; every body
is a Pydantic model.

### Session lifecycle

| Method & path | Body | Response |
|---|---|---|
| `POST /v1/sessions` | `headless`, `viewport_width`, `viewport_height`, `user_agent`, `idle_timeout_seconds` (all optional; idle clamped to `[30, 900]`) | `201` `{session_id, created_at, last_used_at, idle_timeout_seconds, headless, viewport, current_url, evicted_session_id}` — `current_url` is always present, `null` until the first `navigate`; `evicted_session_id` is non-null iff the LRU-evict-oldest-idle path ran (cap was hit and an idle session was reclaimed to make room) |
| `GET /v1/sessions` | — | `{count, sessions: [...]}` (live only; does not bump the idle clock) |
| `GET /v1/sessions/{id}` | — | `SessionResponse` with live `current_url` (status probe — does **not** count as activity) |
| `DELETE /v1/sessions/{id}` | — | `204`. Second `DELETE` of the same id → `404 "Unknown session"` (explicit delete never tombstones) |

### Per-session actions — `/v1/sessions/{id}/<verb>`

`navigate`, `screenshot`, `click`, `type`, `select`, `hover`, `press-key`, `evaluate`,
`wait-for`, `wait-for-load`, `get-text`, `get-html`, `get-attribute`, `find`,
`console-logs` (GET), `network-log` (GET), `network-filter`, `cookies`, `local-storage`.

Element-targeting verbs take `locator: {strategy, value, options}` where
`strategy ∈ role|text|label|placeholder|testid|alt|title|css|xpath` and
`options` carries `name` / `exact` / `nth` / `has_text` (unknown option keys → `422`).
**Prefer `role`+`name`**; fall back to `testid` / `label` / `placeholder` / `text`;
use `css`/`xpath` only when nothing else works.

`find` returns at most 50 elements; full-page screenshots of large pages produce large
JSON responses (`image` is a `data:image/png;base64,…` data URI).

### Service

`GET /health` → `{status, commit, engine, env, live_sessions}` (`commit` is the running
build's short SHA — check it against `git rev-parse --short HEAD`).
`GET /metrics` → JSON with the frozen 11-field set (`live_sessions`, `max_sessions`,
`sessions_created_total`, `sessions_reaped_total`, `sessions_deleted_total`,
`sessions_rejected_total`, `sessions_lru_evicted_total`, `actions_total`,
`action_errors_total`, `browser_restarts_total`, `uptime_seconds`).
`sessions_lru_evicted_total` counts admits-via-eviction (`POST /v1/sessions` succeeded
because the oldest idle session was reclaimed); a nonzero rate in steady state is the
signal that `MAX_SESSIONS` is undersized for the workload, not just a transient blip.

### Admin (bearer-token gated)

| Method & path | Body | Response |
|---|---|---|
| `POST /v1/admin/reset-sessions` | `{reason?: str}` (defaults to `"operator-initiated"`) | `200 {closed: int, reason: str}` |

Send `Authorization: Bearer <ADMIN_AUTH_TOKEN>`. The token comes from the
`ADMIN_AUTH_TOKEN` env var; **when it is unset every admin request is `401`** (fail-closed
— an unconfigured deploy cannot be reset by a stranger). `reset-sessions` synchronously
tears down every live session (Playwright contexts released, registry emptied) — including
sessions mid-action — and is intended as the operator escape hatch when the registry has
drifted into a stuck state and the eval loop is being told "503 capacity reached" on
every create. Sessions are NOT tombstoned; subsequent requests against a purged id return
`404 "Unknown session"`. Each reset logs a single line with the 8-char sha256 prefix of
every closed session id (full ids are never logged).

## Behaviour notes

- **Concurrency.** Actions on the *same* session are serialized by a per-session lock; a
  second in-flight action waits up to `LOCK_ACQUIRE_TIMEOUT_MS` (5s) then → `409 "Session
  busy"`. Different sessions run independently.
- **Idle reaper.** A background task wakes every `REAPER_INTERVAL_SECONDS` (30s) and closes
  any session idle past its `idle_timeout` (default 300s). A reaped session's id is kept in
  a small tombstone LRU so the next request → `404 "Session '<id>' expired (idle > Ns)."`
  A session is not reliably gone until ≈ `idle_timeout + reaper_interval` after last use.
- **Capacity & LRU eviction.** Cap = `MAX_SESSIONS` (default 20). When a `POST /v1/sessions`
  hits the cap, the manager looks for the oldest session that is **safe to evict** (lock
  not held, no activity in the last 5s) and closes it to make room — the response is
  `201` with `evicted_session_id` naming the displaced session. If *every* session is
  active (locked or used within the last 5s) the request gets the legacy
  `503 + Retry-After` (this is genuine concurrent-overload back-pressure, not registry
  drift). Evicted ids are **not tombstoned** — a later request against one gets generic
  `404 "Unknown session"`. The operator escape hatch for a stuck registry is
  `POST /v1/admin/reset-sessions` (see below).
- **SSRF.** `navigate` to a non-http(s) scheme, a malformed URL, or a loopback / RFC1918 /
  link-local-or-metadata host → `400 "Refused to navigate to '<url>': <reason>."` A
  committed `context.route` interceptor also aborts *top-level* navigations to literal
  internal IPs (no DNS in the hot path). **Residuals (v1):** (a) a public URL that 302s to
  an internal IP is **not** blocked — Playwright continues redirected requests without
  routing them, so the interceptor never sees a redirect hop (see
  `specs/locator-auto-wait.md`, "Redirect gap"; tracked in #5); (b) `fetch()` from
  `evaluate` to an internal IP is *not* blocked — the interceptor gates navigations only.
  The configurable per-deploy allowlist is the follow-up fix for both. Don't put this service behind a domain that implies it's
  trustworthy: it is unauthenticated and SSRF-capable by design. **Dev escape hatch:**
  `BROWSER_ALLOW_PRIVATE_HOSTS=true` relaxes the host deny-list (not the scheme allowlist),
  but *only* when `ENV=development` — deny-by-default everywhere else.
- **Body size.** Request bodies over 1 MiB → `413`.

## Errors

Every error body is `{"detail": "<human string>"}` (FastAPI default; `422` is the
validation array). Key codes: `404` unknown/expired session or no element matched; `409`
ambiguous locator / action timeout / session busy; `422` bad locator shape / runtime eval
error; `400` SSRF-blocked host or bad css/xpath syntax; `502` navigation network failure;
`503 + Retry-After` session cap reached; `504` server hard request ceiling (~120s).

## Deploy

Tag-triggered: pushing a `v*` tag (production) or `staging-*` tag (staging) runs
`modal deploy modal_app.py` (see `.github/workflows/deploy.yml`). `modal_app.py` pins the
container to one instance, sets `@modal.concurrent`, bakes Chromium + the git SHA into the
image, and serves the FastAPI app via `@modal.asgi_app()`.

**Operator handoff (one-time):** put the deployed Modal URL into the `pagehub-evals`
environments as `pagehub-browser_url` (production = the Modal URL, local runs =
`http://localhost:4010`); provision `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` for this repo;
set `ENV=production`/`staging` on every non-dev deploy (the `Settings` default
`development` activates the X-Twin-* header-override path).

## Tests

```bash
make test          # pytest -m "not browser" — unit + route tests against a FakeEngine
make test-browser  # pytest -m browser — launches a real Chromium, hits example.com
```

Eval seeds + specs for this service live in the `platform/evals` repo:
`evals/seeds/pagehub_browser_*.py` (+ `_pagehub_browser_common.py`) and
`evals/specs/pagehub-browser/*.md`.
