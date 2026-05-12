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
| `POST /v1/sessions` | `headless`, `viewport_width`, `viewport_height`, `user_agent`, `idle_timeout_seconds` (all optional; idle clamped to `[30, 900]`) | `201` `{session_id, created_at, last_used_at, idle_timeout_seconds, headless, viewport, current_url}` — `current_url` is always present, `null` until the first `navigate` |
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
`GET /metrics` → JSON with the frozen 10-field set (`live_sessions`, `max_sessions`,
`sessions_created_total`, `sessions_reaped_total`, `sessions_deleted_total`,
`sessions_rejected_total`, `actions_total`, `action_errors_total`,
`browser_restarts_total`, `uptime_seconds`).

## Behaviour notes

- **Concurrency.** Actions on the *same* session are serialized by a per-session lock; a
  second in-flight action waits up to `LOCK_ACQUIRE_TIMEOUT_MS` (5s) then → `409 "Session
  busy"`. Different sessions run independently.
- **Idle reaper.** A background task wakes every `REAPER_INTERVAL_SECONDS` (30s) and closes
  any session idle past its `idle_timeout` (default 300s). A reaped session's id is kept in
  a small tombstone LRU so the next request → `404 "Session '<id>' expired (idle > Ns)."`
  A session is not reliably gone until ≈ `idle_timeout + reaper_interval` after last use.
- **SSRF.** `navigate` to a non-http(s) scheme, a malformed URL, or a loopback / RFC1918 /
  link-local-or-metadata host → `400 "Refused to navigate to '<url>': <reason>."` A
  committed `context.route` interceptor also aborts redirects to literal internal IPs (no
  DNS in the hot path). **Residual (v1):** `fetch()` from `evaluate` to an internal IP is
  *not* blocked — the interceptor gates navigations only; the configurable per-deploy
  allowlist is the follow-up fix. Don't put this service behind a domain that implies it's
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
