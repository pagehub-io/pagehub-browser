# SPEC

## Manager

### Problem
`pagehub-evals` can grade HTTP APIs today, but it cannot grade a pure-frontend web app that has no backend — there is nothing to send a request to. The existing `platform/playwright` service is a single global Chromium instance: two eval runs hitting it at once corrupt each other's page state, so it can't back a real eval suite. We need a public HTTP service where each browser action (navigate, locate an element, click, read text/DOM, screenshot) is one ordinary request step in an eval collection, scoped to an isolated session, so frontend-only apps become eval-driven the same way APIs already are. Crucially, the element-locating primitive must default to **resilient structured locators** (Playwright `getByRole`/`getByText`/`getByLabel`/…), not bare CSS strings — brittle CSS selectors are the classic reason E2E suites rot, breaking on every cosmetic UI tweak; if this service made raw CSS the headline, the frontend eval suites built on it would inherit that fragility. Cost of doing nothing: frontend deliverables (starting with the planned chess app) can't be objectively scored, and eval authors keep hand-checking UIs.

### Users
- **pagehub-evals collection authors** — write a collection whose steps are browser actions; `session_id` is captured from `POST /v1/sessions` and substituted into every following step via the existing capture/substitution mechanism.
- **The LLM harnesses being graded** — their frontend output is exercised through a real browser and scored deterministically; concurrent runs of the same suite must not collide.
- **Gavin (operator)** — deploys to Modal, provisions secrets, owns the public-repo deploy path, watches `/health` and `/metrics`, relies on the idle reaper to keep cost bounded.

### Success criteria
- A `pagehub-browser` collection in `pagehub-evals` runs green end-to-end: create session → navigate → click → read text → screenshot → delete session, with `session_id` threaded by capture/substitution.
- Two concurrent sessions are provably isolated: an eval drives session A and session B against the same target and neither sees the other's DOM/URL/storage.
- Every per-page `platform/playwright` action is reachable per-session (navigate/click/type/select/hover/press-key/evaluate/screenshot/get-text/get-html/get-attribute/query-selector-all/wait-for-selector/wait-for-load/console-logs/network-log/network-filter/cookies/local-storage) — `launch`/`close` are subsumed by session create (`POST /v1/sessions`) / delete (`DELETE /v1/sessions/{id}`).
- Interaction primitives accept a **structured locator object** (`strategy` + `value` + `options`); the worked eval collection and the API docs use `role`+`name` — not a bare CSS string — as the primary, demonstrated form. `css`/`xpath` work but are documented as the escape hatch.
- `navigate` to a loopback / RFC1918-private / link-local-or-metadata host is refused with `400` and `host not allowed`; an eval asserts this.
- Idle reaper: a session with no activity past the timeout is closed automatically; an eval asserts the reaper fired.
- Deployed on Modal and publicly reachable; `/health` reports the running commit; `/metrics` exposes live-session count.
- `make up` (docker-compose, port `:4010`) gives a working local instance; boot is idempotent.

### In scope (this build)
- FastAPI service in `api/`: `POST /v1/sessions` → `{session_id}`, `DELETE /v1/sessions/{id}`, and per-session equivalents of all per-page `platform/playwright` actions. Every route a Pydantic `response_model`; every body a Pydantic model; schemas grouped.
- **Structured locator object across all interaction primitives** (click / type / read-text / read-html / get-attribute / wait-for / hover / screenshot-element / query-all): `{strategy: "role"|"text"|"label"|"placeholder"|"testid"|"alt"|"title"|"css"|"xpath", value, options}`. Playwright `getBy*` strategies are the documented default; `css`/`xpath` the escape hatch. Docs and fixtures steer authors to `role`+`name`.
- Multi-session registry + idle-timeout reaper; concurrency-safe.
- Pluggable engine interface with a Playwright/Chromium implementation as the only shipped engine.
- **Minimal hardcoded SSRF deny-list on `navigate`** — refuse loopback (`127.0.0.0/8`, `::1`, `localhost`), RFC1918 private ranges, and link-local / cloud-metadata (`169.254.0.0/16`); `400` with reason `host not allowed`. (Security engineer fleshes out the exact list in PLAN.) A *configurable allowlist* is a documented follow-up, not v1.
- `X-Twin-*` override-header pattern for any outbound deps (dev-only per global CLAUDE.md), `/health`, `/metrics`, docker-compose + Makefile (local port `:4010`, confirmed).
- Modal deployment config and the public-repo deploy wiring.
- Eval coverage: specs under `platform/evals/specs/pagehub-browser/` and seeds `platform/evals/seeds/pagehub_browser_*.py` — create session, navigate, locate(`role`+`name`)+click+read, screenshot, session isolation between two concurrent sessions, idle reaper, SSRF deny-list refusal.

### Out of scope / non-goals
- The chess rule-conformance collection and the frontend chess app (separate follow-up that *consumes* this service).
- A *configurable* SSRF allowlist (the v1 deny-list above ships; a per-deploy allowlist is the next-step hardening).
- Any second browser engine (camoufox / stealth) — interface only, no implementation.
- Authentication / multi-tenant quotas / per-caller rate limiting beyond the idle reaper.
- A database or durable session storage beyond what a session registry needs (prefer none).
- Recording/replay, video capture, proxy management.
- Merging, cutting deployable tags, or flipping repo visibility.

### Resolved (decided downstream — Gavin to ratify implications)
- **Modal session affinity — RESOLVED.** Single-container pinning (`min_containers=1` / `max_containers=1`, `scaledown_window` ≥ idle timeout); on container recycle, in-flight sessions vanish and the caller's next step gets a `404` + must re-create. See Architect. Gavin to ratify the deploy/cost (memory-tier-for-`max_sessions`) implications.
- **Idle timeout + max sessions — RESOLVED.** Defaults `IDLE_TIMEOUT_SECONDS=300`, `MAX_SESSIONS=20`; evals may pass `idle_timeout_seconds` per session, clamped `[30, server_max]`. See Designer/Architect.
- **SSRF — RESOLVED.** v1 ships the hardcoded deny-list in In-scope above; configurable allowlist is a documented follow-up. Security engineer fleshes out the exact CIDR list in PLAN.
- **Local dev port — RESOLVED.** `:4010` (Architect; free in the pagehub range).

### Open questions (need Gavin before or during build)
- **Public-repo deploy mirror setup.** Confirm which path ships (in-repo `modal deploy` workflow vs. `pagehub-deploy-public-repos` mirror) and do the by-hand setup: org-secret repo-access or repo-level `MODAL_TOKEN_*` secrets, and put the deployed Modal URL into the pagehub-evals environment as `pagehub-browser_url`.
- **Modal secret provisioning by hand.** Which secrets must Gavin create in the Modal dashboard / via `gh secret set` vs. inherit from existing org secrets.

## Designer

The product surface is the HTTP API. The "user" is a `pagehub-evals` collection author writing a `.py` seed (style of `evals/seeds/playwright.py`, but with its own `req()` helper — see below) and the harness that runs it. Design goal: a step chain that reads naturally, threads one captured value (`session_id`) through every later step with the *minimum* substitution machinery, and targets elements with **resilient structured locators** — never a bare CSS string in the headline.

### Locators — the primary targeting primitive (replaces bare `selector` strings)

Every interaction primitive that points at an element — `click`, `type`, `select`, `hover`, `press-key` (when it targets an element), `get-text`, `get-html`, `get-attribute`, the "find" primitive (was `query-selector-all`), `wait-for` (the element-state variant, was `wait-for-selector`), and `screenshot` (element variant, when `locator` is given) — takes a **structured `Locator` object** as its targeting input, not a CSS string:

```jsonc
Locator = {
  "strategy": "role" | "text" | "label" | "placeholder" | "testid" | "alt" | "title" | "css" | "xpath",
  "value":   "<string>",        // role name for "role"; visible text for "text"; selector for "css"; xpath expr for "xpath"; etc.
  "options": {                  // optional, strategy-specific
    "name":     "<string>",     // accessible name — used with strategy:"role" → Playwright getByRole(role, {name})
    "exact":    true,           // exact vs. substring/normalized match (text/role-name/label/etc.)
    "nth":      0,              // 0-based; disambiguate when multiple elements match
    "has_text": "<string>"      // further-constrain a match to elements containing this text
    // ... other strategy-specific keys pass through to the engine
  }
}
```

Mapping to Playwright (the engine): `role` → `getByRole(value, {name, exact})`, `text` → `getByText(value, {exact})`, `label` → `getByLabel(value, {exact})`, `placeholder` → `getByPlaceholder(value, {exact})`, `testid` → `getByTestId(value)`, `alt` → `getByAltText(value, {exact})`, `title` → `getByTitle(value, {exact})`, `css` → `locator(value)`, `xpath` → `locator("xpath=" + value)`. After resolution, `options.nth` selects the n-th match; `options.has_text` applies `.filter({hasText})`.

**Authoring story (this paragraph goes in the seed docs and the `/docs` description):** *prefer `role` + `name`* — it survives cosmetic refactors and matches how a screen-reader user perceives the page; *fall back to `testid`* when a control has no good accessible role/name (add a `data-testid` to the app); *use `label`/`placeholder`/`text`/`alt`/`title`* for form fields and content you can identify by their human-visible string; *use `css`/`xpath` only when nothing else works* (deeply nested layout containers, third-party widgets you can't annotate). The `css`/`xpath` escape hatch is fully supported — it just never appears in a worked example here, because an eval suite that leans on it is the brittle suite this service exists to avoid.

Every locator-bearing example and every worked chain below uses `{"strategy": "role", "value": "...", "options": {"name": "..."}}` or a `testid`/`text` form — there is **no bare `selector` field** anywhere in the request schemas.

### Session-id placement decision — **path param** `/v1/sessions/{session_id}/<action>`

Options weighed: (a) path segment, (b) body field `session_id`, (c) header `X-Session-Id`.

Decision: **path segment.** Rationale:
- The evals substitution engine already substitutes `{{NAME}}` into `path`, `body`, and `headers` (see `prayers_e2e_anonymous.py`: `path="/prayer-groups/{{GRP_ID}}/join"`). All three forms work — but path is the only one that is **uniform across every action** including the ones whose body is `{}` (`get-text` with no locator, `screenshot` with defaults) and the `GET` ones (`console-logs`, `network-log`). A body field would force a non-empty body on otherwise-bodiless calls and make `capture` targets inconsistent; a header would put a load-bearing value somewhere reviewers don't look.
- The collection step **reads as the sentence it is**: `POST /v1/sessions/{{SID}}/navigate` body `{"url": "..."}` — the resource ("this session") and the verb ("navigate") are both visible in the line, matching the existing `platform/playwright` mental model (`POST /v1/browser/navigate`) with a session scope spliced in.
- RESTful: a session is a real resource with a lifecycle (`POST` to create, `GET` to inspect, `DELETE` to destroy); actions are sub-resources of it.

So: capture `session_id` once from `POST /v1/sessions`, and every later step's `path` contains `{{SID}}`. Bodies change vs. `platform/playwright` only where a `selector: str` becomes a `locator: Locator`.

### `pagehub_browser_*.py` seeds need their own `req()` helper

`evals/seeds/playwright.py`'s `req()` hardcodes `factory_key="playwright"` and supports neither `headers=` nor `capture=`. **Do not copy it verbatim** — `pagehub_browser_*.py` seeds get a fresh `req()` that (a) sets `factory_key="pagehub-browser"`, (b) plumbs `capture=` through (the evals API supports it — see `pagehub_evals_runs_capture_chain.py`), and (c) accepts `headers=` for completeness. The `ev()` / condition shapes are unchanged.

### Endpoint inventory

**Conventions:** base URL `http://<host>/v1`. All bodies are Pydantic models; all responses have a `response_model`; `response_model_exclude_none=True` on anything with optional fields **except `SessionResponse.current_url`, which is exempt** so it is always *present* (value `null` until the first `navigate`) — collection authors can write `body.current_url == null` and have it match. Unknown/expired session on any `/v1/sessions/{id}/*` route → `404` (see Error states). Schema names: `CreateSessionRequest`, `SessionResponse`, `SessionListResponse`, `Locator`, `NavigateRequest`/`NavigateResponse`, `ScreenshotRequest`/`ScreenshotResponse`, `ClickRequest`, `ElementTextResponse`, `FindRequest`/`FindResultResponse`, `MetricsResponse`, etc. — never `In`/`Out`.

#### Session lifecycle

| Method & path | Request body | Response (`200`/`201`) | Notes |
|---|---|---|---|
| `POST /v1/sessions` | `CreateSessionRequest`: `headless: bool = true`, `viewport_width: int = 1280`, `viewport_height: int = 720`, `user_agent: str \| None = null`, `idle_timeout_seconds: int \| None = null` (clamped to `[30, server_max]`; `null` ⇒ server default) | `201` `SessionResponse`: `session_id: str` (uuid4), `created_at: str` (ISO-8601 UTC), `last_used_at: str`, `idle_timeout_seconds: int` (effective), `headless: bool`, `viewport: {width, height}`, `current_url: str \| null` (always `null` at create; field always present — exempt from `exclude_none`) | Creates an isolated browser context+page. No `navigate` happens implicitly. `503 + Retry-After` if at session cap (see Error states). |
| `GET /v1/sessions` | — | `SessionListResponse`: `count: int`, `sessions: [SessionResponse, ...]` | Operator/debug. No body, no side effects, does **not** bump `last_used_at`. |
| `GET /v1/sessions/{session_id}` | — | `SessionResponse` (with live `current_url`, `last_used_at`) | Status probe. Does **not** count as activity (won't keep a dying session alive — otherwise polling would defeat the reaper). `404` if unknown/reaped. |
| `DELETE /v1/sessions/{session_id}` | — | `204 No Content` | Closes the context, frees the slot. A request on an already-deleted id → `404 "Unknown session '<id>'."` — explicit deletion does **not** tombstone; only the idle reaper does (see Edge cases: double-delete, and Idle-reap UX). |

> Decision: there is **no** `POST /v1/sessions/{id}/launch` analog. `platform/playwright` needs `launch` because the browser is a global lazily-started singleton; here, `POST /v1/sessions` *is* the launch. Likewise `POST /v1/browser/close` becomes `DELETE /v1/sessions/{id}`. This keeps the verb count down and the lifecycle unambiguous.

#### Per-session actions — `/v1/sessions/{session_id}/<verb>`

Bodies that targeted an element by `selector: str` in `platform/playwright` now take `locator: Locator`. Everything else mirrors `platform/playwright` 1:1; only the path gains the session scope. Every action bumps `last_used_at` on success **and** on a locator-not-found `404` (the caller is actively using the session; only true reap/idle stops the clock). The two `GET` action endpoints (`console-logs`, `network-log`) also bump `last_used_at` — they are per-session actions, distinct from the status probes which don't (see Idle-reap UX).

| Method & path | Request body (Pydantic) | Response |
|---|---|---|
| `POST .../navigate` | `NavigateRequest`: `url: str`, `wait_until: str = "load"` (`load`\|`domcontentloaded`\|`networkidle`\|`commit`) | `NavigateResponse`: `url: str` (final, post-redirect), `status: int \| null`, `title: str`, `text: str` (human summary, keeps `platform/playwright`'s `"Navigated to … (status: 200). Title: …"` so existing `contains "status: 200"` assertions port over) |
| `POST .../screenshot` | `ScreenshotRequest`: `full_page: bool = false`, `locator: Locator \| null = null` (null ⇒ viewport/full-page; non-null ⇒ that element) | `ScreenshotResponse`: `image: str` — `data:image/png;base64,…`. **JSON body, data-URI** (not raw `image/png`) — the evals consumer asserts on JSON (`body.image contains "data:image/png;base64,"`). |
| `POST .../click` | `ClickRequest`: `locator: Locator`, `timeout: int = 5000` | `ActionResponse`: `status: "ok"`, `message: str` |
| `POST .../type` | `TypeRequest`: `locator: Locator`, `text: str`, `clear: bool = true`, `timeout: int = 5000` | `ActionResponse` |
| `POST .../select` | `SelectOptionRequest`: `locator: Locator`, `value: str`, `timeout: int = 5000` | `ActionResponse` |
| `POST .../hover` | `HoverRequest`: `locator: Locator`, `timeout: int = 5000` | `ActionResponse` |
| `POST .../press-key` | `PressKeyRequest`: `key: str`, `locator: Locator \| null = null` (null ⇒ page-level key press) | `ActionResponse` |
| `POST .../evaluate` | `EvaluateRequest`: `expression: str` | `EvaluateResponse`: `text: str` (scalars stringified; objects/arrays pretty-printed JSON, as today) |
| `POST .../wait-for` | `WaitForRequest`: `locator: Locator`, `state: str = "visible"` (`visible`\|`hidden`\|`attached`\|`detached`), `timeout: int = 10000` | `ActionResponse` |
| `POST .../wait-for-load` | `WaitForLoadRequest`: `wait_until: str = "networkidle"`, `timeout: int = 30000` | `ActionResponse` (page-level — no locator) |
| `POST .../get-text` | `GetTextRequest`: `locator: Locator \| null = null` (null ⇒ whole-`body` text — **not** subject to the single-match rule), `timeout: int = 5000` | `ElementTextResponse`: `text: str` — when a `locator` **is** given it must match exactly one element (`platform/playwright`'s `.text_content()` throws on a strict multi-match locator): ambiguous → `409` (`locator matched N elements; pass options.nth`), zero → `404` |
| `POST .../get-html` | `GetHtmlRequest`: `locator: Locator \| null = null` (null ⇒ full `<html>` outerHTML — **not** subject to the single-match rule), `outer: bool = false` | `ElementHtmlResponse`: `text: str` — when a `locator` **is** given it must match exactly one element (`.inner_html()`/`.outer_html()` throw on a strict multi-match locator): ambiguous → `409` (`locator matched N elements; pass options.nth`), zero → `404` |
| `POST .../get-attribute` | `GetAttributeRequest`: `locator: Locator`, `attribute: str`, `timeout: int = 5000` | `ElementAttributeResponse`: `text: str` — locator must match exactly one element (ambiguous → `409`, zero → `404`); if the one matched element exists but lacks `attribute` → `404 "attribute '<attr>' not present on <strategy>=<value>"` (mirrors `platform/playwright`'s 404 for a missing attribute) |
| `POST .../find` | `FindRequest`: `locator: Locator` | `FindResultResponse`: `count: int`, `elements: [{tag, text, attributes}]` (capped at 50, as today) — concept rename of `query-selector-all`; resolves the locator and enumerates all matches |
| `GET .../console-logs?clear=false` | — (query param `clear: bool`) | `ConsoleLogsResponse`: `count: int`, `entries: [{type, text, location}]` (bumps `last_used_at`) |
| `GET .../network-log?clear=false` | — | `NetworkLogResponse`: `count: int`, `entries: [{method, url, status, resource_type}]` (bumps `last_used_at`) |
| `POST .../network-filter` | `NetworkFilterRequest`: `url_pattern: str \| null`, `resource_type: str \| null`, `status_min: int \| null`, `status_max: int \| null`, `clear: bool = false` | `NetworkLogResponse` (page-level — no locator) |
| `POST .../cookies` | `CookiesRequest`: `action: "get"\|"set"\|"clear"`, `cookies: [obj] \| null` | `CookiesResponse`: `text: str` (JSON dump for `get`, summary for `set`/`clear`) (page-level — no locator) |
| `POST .../local-storage` | `LocalStorageRequest`: `action: "get"\|"set"\|"remove"\|"clear"`, `key: str \| null`, `value: str \| null` | `LocalStorageResponse`: `text: str` (page-level — no locator) |

> Note: `console-logs` / `network-log` are `GET` with a `?clear=` query param (mirrors `platform/playwright`) — they read per-session buffers and don't take a JSON body, so substitution into `path` (`/v1/sessions/{{SID}}/console-logs`) is the only place `SID` appears, which is exactly why the path-param decision is uniform. They *do* count as session activity (a caller polling the network log is actively using the session); only the `GET /v1/sessions...` *status* probes don't.

#### Service-level (page-level — no locator)

| Method & path | Response (`response_model`) | Purpose |
|---|---|---|
| `GET /health` | `{status: "ok", commit: "<short-sha>", engine: "playwright-chromium", env: "<env>", live_sessions: <int>}` | Liveness; `commit` must match `git rev-parse --short HEAD` of the running build. (Superset; Architect conforms to this — field name is `commit`, not `git_sha`.) |
| `GET /metrics` | **JSON** with a Pydantic `response_model` (`MetricsResponse`): `{live_sessions: int, sessions_created_total: int, sessions_reaped_total: int, max_sessions: int, ...}` | Operator dashboard. **Not** Prometheus text — JSON, so the "every route has a `response_model`" rule holds and Chain C's `body.path = "sessions_reaped_total"` assertion works. Architect drops the "Prometheus text" option. |

### Error states

Every error response body is `{"detail": "<human, actionable string>"}` (FastAPI default; `422` is FastAPI's validation array) so the eval failure message is the `detail` string verbatim.

| Condition | Status | `detail` body | How the caller recovers |
|---|---|---|---|
| Unknown `session_id` (never existed, or bad uuid) on any `/v1/sessions/{id}/*` | **404** | `"Unknown session '<id>'."` | Re-`POST /v1/sessions`, capture a fresh id. (Collection author: the `POST /v1/sessions` step must precede and capture.) |
| Session was idle-reaped | **404** | `"Session '<id>' expired (idle > <N>s). Create a new session."` | Same — re-create. The `idle > Ns` phrasing makes the reaper visible in the failure log. |
| Malformed or unsupported `Locator` (unknown `strategy`, missing `value`, bad `options` type) | **422** | FastAPI validation array (or `"Unsupported locator strategy '<x>'; expected one of role,text,label,placeholder,testid,alt,title,css,xpath."` when it parses structurally but the strategy isn't recognized) | Fix the `locator` object — check `strategy` is one of the nine. |
| Locator matched **zero** elements **after waiting up to `timeout`** for it to attach (`click`, `type`, `select`, `hover`, `press-key` with a locator, `get-text`/`get-html` with a locator, `get-attribute`, `screenshot` with a locator) | **404** | `"no element matched <strategy>=<value>"` (e.g. `"no element matched role=button (name='Submit')"`) | Fix the locator, or check the page actually rendered the control; a `wait-for` on the same locator is no longer needed for elements that render late, because every single-element verb waits like Playwright's own actions do. Matches `platform/playwright`'s element-missing `404`. (`wait-for` reports zero-match as the **409** timeout row below; with `state: "hidden"`/`"detached"` it treats zero-match as **success**.) |
| Locator matched **multiple** elements and the action needs exactly one (`click`, `type`, `select`, `hover`, element `screenshot`, `get-attribute`, `get-text` *with a locator*, `get-html` *with a locator*) and `options.nth` was **not** given | **409 Conflict** | `"locator matched <N> elements; pass options.nth to disambiguate (or narrow with options.name / options.has_text)."` | Add `options.nth`, or tighten the locator (`name`, `has_text`, `exact`). `find` accepts multi-match without `nth`; `wait-for` is strict like every other verb and answers this same **409** on a multi-match (Playwright's strict-mode violation); `get-text`/`get-html` with **no** locator (whole-`<body>` text / full `<html>`) are unaffected. (Playwright's `.text_content()` / `.inner_html()` / `.outer_html()` throw on a strict multi-match locator, so these read primitives need `nth` the same as the action primitives.) |
| `get-attribute` where the locator matched **exactly one** element but that element does **not** have the requested attribute | **404** | `"attribute '<attr>' not present on <strategy>=<value>"` (e.g. `"attribute 'href' not present on role=button (name='Submit')"`) | Check the attribute name; confirm the element actually carries it (Playwright `get_attribute` returns `None` for an absent attribute — we surface that as `404`, mirroring `platform/playwright`'s 404 for a missing attribute, so it's distinguishable from the element being absent entirely). |
| Action timeout — the request-param `timeout` lapsed: element never reached the requested state (for `get-text`, an element that exists but never became visible), or page never reached its load state. `timeout` is one budget for waiting plus the action itself | **409 Conflict** | `"Timed out after <ms>ms waiting for <strategy>=<value> to be <state>."` from a wait (`wait-for`, or the pre-action wait of a single-element verb); `"Timed out after <ms>ms waiting for <strategy>=<value> to be actionable."` from the action phase of click/type/select/hover/press-key/get-text/get-html/get-attribute (or `"… waiting for page to reach <state>."`) | Increase `timeout`, or fix the precondition. Chose **409 over 408**: `408 Request Timeout` means *the client was too slow sending the request*; here the request arrived fine and the *page/element state* didn't converge — a conflict between expected and actual state, and it must be distinguishable from a transport timeout so the eval can assert on it. (Distinct from the server hard-ceiling `504` below.) |
| Server hard request ceiling hit (Modal function timeout, ~120s) — the whole request ran past the platform cap, regardless of the action `timeout` | **504 Gateway Timeout** | `"Request exceeded the server time limit (~<N>s)."` | Use a smaller action `timeout`; split the work. Document that action `timeout` values above the server ceiling are effectively clamped to it. |
| Bad CSS / XPath *syntax* in `locator` with `strategy: "css"` or `"xpath"` (engine rejects the string itself, not "not found") | **400** | `"Invalid <css\|xpath> selector syntax: <value> (<engine message>)."` | Fix the selector string. Distinct from `404` not-found so a grammar typo isn't confused with "element absent". (Doesn't arise for `role`/`text`/etc. — those values are plain strings.) |
| Malformed request body unrelated to the locator (missing required field, wrong type) | **422** | FastAPI validation array | Fix the step body. Standard FastAPI. |
| `navigate` to a blocked, unparseable, or non-http(s) URL (`file://`, `chrome://`, loopback / RFC1918-private / link-local-or-metadata host) | **400** | `"Refused to navigate to '<url>': <reason>."` (`reason` ∈ `unsupported scheme` / `malformed URL` / `host not allowed`) | Use an `http(s)` URL to a public host. (Per the success criteria an eval asserts the `host not allowed` case.) |
| `navigate` to a valid URL that the *remote* server fails (DNS failure, connection refused) | **502 Bad Gateway** | `"Navigation to '<url>' failed: <network error>."` | Check the target is up. |
| `navigate` to a URL that *loaded* but returned a `4xx`/`5xx` page | **200** with `status: <that code>` in the body | (no `detail` — success at the HTTP level) | The page loaded; the eval decides via `body.status` whether that's a failure. Lets evals assert `body.status` rather than HTTP-level. |
| Session cap reached on `POST /v1/sessions` | **503 Service Unavailable** + `Retry-After: <s>` header | `"Session capacity reached (<max> active). Retry shortly or DELETE an idle session."` | Wait `Retry-After` and retry, or `DELETE` a finished session. Chose **503 over 429**: 429 implies *this caller* is rate-limited (per-caller quota), but there's no per-caller accounting here — the *service* as a whole is at capacity, which is 503's meaning. `Retry-After` makes it actionable. |
| `evaluate` expression throws / element `screenshot` of a detached-but-matched element / other engine runtime error not covered above | **422** | `"Action failed: <engine message>."` | Fix the expression / re-navigate. (Not 500 — caused by caller input; a 500 would look like a service bug.) |
| Concurrent action on the *same* session while one is already in flight, and the wait would exceed a bound | **409 Conflict** | `"Session busy: another action is in progress."` | Don't share a `session_id` across overlapping calls; let steps run in order. |
| Genuine internal fault (engine crashed, container OOM mid-action) | **500** | `"Internal error."` (details to logs, not body) | Retry; if persistent, operator territory. The container-died case also surfaces as `404` on the *next* request to a vanished session — see the 404 contract. |

(Note: this does **not** port the `platform/playwright` `no-browser-400` eval 1:1 — there is no global "browser not launched" state here; ops on a deleted/unknown session are `404` because a session is a resource. The element-missing `404`, the bad-syntax `400`, and the timeout cases do port, modulo `selector` → `locator`.)

### Idle-reap UX

- A background reaper wakes every `reaper_interval` (~30s) and closes any session whose `last_used_at` is older than its effective `idle_timeout_seconds`, incrementing `sessions_reaped_total`. **Consequence for eval authors:** a session created with `idle_timeout_seconds: 30` is not reliably dead until roughly `idle_timeout + reaper_interval` ≈ 60s after its last use — a reaper eval must wait *more* than that (≈ 90s with margin), not just 30s. (See Chain C.)
- After reap, the `session_id` is **kept in a small tombstone set** (bounded LRU) so the *next* request to it returns the specific **404 `"Session '<id>' expired (idle > <N>s). Create a new session."`** rather than the generic `"Unknown session"`. This is the actionable-failure-message requirement: an eval that's slow between steps gets a log line that says *exactly* why, not a mystery 404.
- `GET /v1/sessions/{id}` on a reaped session returns the same expired-404 — so a debugging operator can confirm "yes, it was reaped" vs "I typo'd the id".
- `GET /v1/sessions` never lists reaped/tombstoned sessions (only live ones).
- **What bumps the idle clock:** every per-session `/v1/sessions/{id}/<verb>` action — including the `GET` ones `console-logs` and `network-log` — refreshes `last_used_at` on success or on a locator-not-found `404`. The *status probes* `GET /v1/sessions/{id}` and `GET /v1/sessions` deliberately do **not** — otherwise an eval that polls status between steps would never let a session die and the reaper eval couldn't be written.
- The reaper never closes a session whose per-session lock is held (an action is in flight) — see Edge cases.

### Worked collection step chains (style of `evals/seeds/playwright.py`, with the `pagehub-browser` `req()` helper)

These use the evals primitives: `req(name, description, method, path, body=..., headers=..., )` with `factory_key="pagehub-browser"`, `ev(rid, name, desc, conditions)`, `capture={"NAME": "$.json_path"}`, and `{{NAME}}` substitution into `path`/`body`/`headers`. **Every element target is a `Locator` object — `role`+`name` first.**

**Chain A — happy path: create → navigate → read → click → screenshot → delete**

```python
# 1. Create an isolated session; capture its id.
rid = req("create-session", "Create an isolated browser session",
          "POST", "/v1/sessions", {"headless": True})
ev(rid, "session-created-201", "201 with a session_id; current_url present and null", [
    {"target": "status_code", "operator": "eq", "expected": 201},
    {"target": "body", "path": "session_id", "operator": "exists"},
    {"target": "body", "path": "current_url", "operator": "eq", "expected": None},
], capture={"SID": "$.session_id"})

# 2. Navigate — {{SID}} goes in the PATH, body is identical to platform/playwright.
rid = req("nav-example", "Navigate the session to example.com",
          "POST", "/v1/sessions/{{SID}}/navigate", {"url": "https://example.com"})
ev(rid, "nav-200", "navigate reports status 200", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "status", "operator": "eq", "expected": 200},
    {"target": "body", "path": "text", "operator": "contains", "expected": "status: 200"},
])

# 3. Read the page heading — by ROLE, not a CSS selector.
rid = req("read-heading", "Read the page heading",
          "POST", "/v1/sessions/{{SID}}/get-text",
          {"locator": {"strategy": "role", "value": "heading"}})
ev(rid, "heading-text", "heading contains 'Example'", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "text", "operator": "contains", "expected": "Example"},
])

# 4. Click a link — by ROLE + accessible NAME.
rid = req("click-more", "Click the 'More information' link",
          "POST", "/v1/sessions/{{SID}}/click",
          {"locator": {"strategy": "role", "value": "link",
                       "options": {"name": "More information"}}})
ev(rid, "click-ok", "click succeeded", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "status", "operator": "eq", "expected": "ok"},
])

# 5. Screenshot — JSON body, base64 data URI (no locator ⇒ viewport).
rid = req("shot", "Viewport screenshot",
          "POST", "/v1/sessions/{{SID}}/screenshot", {})
ev(rid, "shot-png", "returns a PNG data URI", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "image", "operator": "contains", "expected": "data:image/png;base64,"},
])

# 6. Tear down.
rid = req("del-session", "Delete the session", "DELETE", "/v1/sessions/{{SID}}")
ev(rid, "session-deleted-204", "204 on delete", [
    {"target": "status_code", "operator": "eq", "expected": 204},
])

# 7. Using it after delete is a 404 with the 'Unknown session' message.
rid = req("nav-after-del", "Navigate a deleted session", "POST",
          "/v1/sessions/{{SID}}/navigate", {"url": "https://example.com"})
ev(rid, "deleted-404", "404 after delete", [
    {"target": "status_code", "operator": "eq", "expected": 404},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "Unknown session"},
])
```

**Chain A′ — locator error cases** (illustrates the 404 / 409 / 422 split; runs against a session navigated to a page with several links)

```python
# Zero match → 404 'no element matched ...'.
rid = req("missing-el", "Click a button that isn't there", "POST",
          "/v1/sessions/{{SID}}/click",
          {"locator": {"strategy": "role", "value": "button",
                       "options": {"name": "Definitely Not Here"}}})
ev(rid, "no-match-404", "404, message names the locator", [
    {"target": "status_code", "operator": "eq", "expected": 404},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "no element matched role=button"},
])

# Many matches, no nth → 409.
rid = req("ambiguous", "Click 'a link' when many match", "POST",
          "/v1/sessions/{{SID}}/click",
          {"locator": {"strategy": "role", "value": "link"}})
ev(rid, "ambiguous-409", "409 asking for options.nth", [
    {"target": "status_code", "operator": "eq", "expected": 409},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "matched"},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "options.nth"},
])

# Bad strategy → 422.
rid = req("bad-strategy", "Use an unknown locator strategy", "POST",
          "/v1/sessions/{{SID}}/click",
          {"locator": {"strategy": "magic", "value": "whatever"}})
ev(rid, "bad-strategy-422", "422", [
    {"target": "status_code", "operator": "eq", "expected": 422},
])
```

**Chain B — two concurrent sessions are isolated** (success criterion: A and B don't see each other's DOM/URL)

```python
ra = req("create-A", "Session A", "POST", "/v1/sessions", {"headless": True})
ev(ra, "A-created", "A has id", [{"target":"status_code","operator":"eq","expected":201}],
   capture={"SID_A": "$.session_id"})
rb = req("create-B", "Session B", "POST", "/v1/sessions", {"headless": True})
ev(rb, "B-created", "B has id", [{"target":"status_code","operator":"eq","expected":201}],
   capture={"SID_B": "$.session_id"})

# A → example.com, B → example.org — different paths, same body shape.
rid = req("A-nav", "A → example.com", "POST", "/v1/sessions/{{SID_A}}/navigate",
          {"url": "https://example.com"})
ev(rid, "A-nav-ok", "ok", [{"target":"status_code","operator":"eq","expected":200}])
rid = req("B-nav", "B → example.org", "POST", "/v1/sessions/{{SID_B}}/navigate",
          {"url": "https://example.org"})
ev(rid, "B-nav-ok", "ok", [{"target":"status_code","operator":"eq","expected":200}])

# Each session reports its OWN current_url.
rid = req("A-status", "A status", "GET", "/v1/sessions/{{SID_A}}")
ev(rid, "A-on-com", "A is on example.com", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "current_url", "operator": "contains", "expected": "example.com"},
])
rid = req("B-status", "B status", "GET", "/v1/sessions/{{SID_B}}")
ev(rid, "B-on-org", "B is on example.org (not contaminated by A)", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "current_url", "operator": "contains", "expected": "example.org"},
])
# (clean up both with DELETE /v1/sessions/{{SID_A}} and /{{SID_B}})
```

**Chain C — idle reaper fires** (success criterion: an eval asserts the reaper fired)

The naïve "create with `idle_timeout_seconds: 30`, sleep 30s, expect 404" does **not** work: the reaper interval is ~30s, so the session isn't reliably gone until ≈ `idle_timeout + reaper_interval` ≈ 60s after last use, and there is no `…/sleep` endpoint or slow endpoint on this service to burn that wall-clock against the *target* session without bumping its clock. Concretely, one of:

1. **Preferred — `pagehub-evals` native delay step.** If the evals runner has a native delay/sleep step type, the seed inserts a single `delay ≈ 90s` between create and the reaped-use step. (Reliability engineer to confirm in PLAN whether such a step type exists.)
2. **Fallback — throwaway second session burns wall-clock.** The seed creates a *second*, disposable session and runs `wait-for-load` (or a few repeated `wait-for` calls) against a deliberately-slow fixture URL **on that second session**, so each call eats several seconds of real time *without touching `SID_T`*. Sum the throwaway waits to ≈ 90s, then probe `SID_T`. (`SID_T`'s `last_used_at` is untouched because actions on the throwaway session don't affect it, and we never `GET /v1/sessions/{{SID_T}}` between create and probe — a status probe wouldn't bump it anyway, but skipping it keeps the chain honest.)
3. **Hard dependency.** If neither (1) nor (2) is workable in the eval harness, this is a blocker for the reliability engineer to resolve in PLAN; option (2) is the spelled-out default.

```python
# Create with the smallest allowed idle timeout so the reaper acts within the suite's runtime.
rid = req("create-shortlived", "Session with 30s idle timeout",
          "POST", "/v1/sessions", {"headless": True, "idle_timeout_seconds": 30})
ev(rid, "shortlived-created", "created with the requested (clamped) timeout", [
    {"target": "status_code", "operator": "eq", "expected": 201},
    {"target": "body", "path": "idle_timeout_seconds", "operator": "eq", "expected": 30},
], capture={"SID_T": "$.session_id"})

# --- burn ≳ idle_timeout + reaper_interval (~90s) of wall-clock WITHOUT touching SID_T ---
# Option 1: a single native delay step (~90s), if the runner supports it.
# Option 2 (shown): a throwaway session repeatedly wait-for-load's a slow fixture URL.
rid = req("create-burner", "Throwaway session just to burn time",
          "POST", "/v1/sessions", {"headless": True})
ev(rid, "burner-created", "ok", [{"target":"status_code","operator":"eq","expected":201}],
   capture={"SID_BURN": "$.session_id"})
for i in range(9):  # ~9 × ~10s ≈ 90s; SLOW_URL is a fixture that stalls ~10s before load
    rid = req(f"burn-{i}", "Wait on a slow URL to pass time", "POST",
              "/v1/sessions/{{SID_BURN}}/wait-for-load",
              {"wait_until": "load", "timeout": 12000})
    ev(rid, f"burn-{i}-done", "returned (ok or 409 timeout — either way, time passed)", [
        {"target": "status_code", "operator": "in", "expected": [200, 409]},
    ])
rid = req("del-burner", "Drop the throwaway session", "DELETE", "/v1/sessions/{{SID_BURN}}")
ev(rid, "burner-gone", "204", [{"target":"status_code","operator":"eq","expected":204}])

# After the idle window, SID_T is gone — with the actionable message.
rid = req("use-reaped", "Use the reaped session", "POST",
          "/v1/sessions/{{SID_T}}/navigate", {"url": "https://example.com"})
ev(rid, "reaped-404", "404 with an 'expired (idle > Ns)' message", [
    {"target": "status_code", "operator": "eq", "expected": 404},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "expired"},
    {"target": "body", "path": "detail", "operator": "contains", "expected": "idle"},
])

# /metrics shows at least one reap happened (JSON body, response_model'd).
rid = req("metrics-after-reap", "Check reaper metric", "GET", "/metrics")
ev(rid, "reaped-counted", "sessions_reaped_total >= 1", [
    {"target": "status_code", "operator": "eq", "expected": 200},
    {"target": "body", "path": "sessions_reaped_total", "operator": "gte", "expected": 1},
])
```

### Edge cases

- **Action before any `navigate`** — the session has a fresh `about:blank` page. `get-text`/`get-html`/`screenshot` (no locator) succeed (empty-ish text, blank PNG); locator-based actions return the normal `404 "no element matched …"`. `SessionResponse.current_url` is **`null` until the first `navigate`** (field present, value `null` — consistent with the inventory). Not an error: matches `platform/playwright` right after `launch`.
- **Double `DELETE`** — first `DELETE` → `204`. Second `DELETE` of the same id → `404 "Unknown session '<id>'."` (it's gone), **not** another `204` — collection authors should be able to tell "I deleted something" from "there was nothing there". Explicit deletion never tombstones (only the idle reaper does), so the second `DELETE` — and any other request on that id — gets the generic unknown-404, never the expired-message variant. (Fully-idempotent would be `204` both times; chose `404`-on-second because `DELETE` of a never-existed id is unambiguously `404` anyway, so symmetry favors `404`, and it's more diagnostic.)
- **`DELETE` of an unknown id, or a request on an already-deleted id** — `404 "Unknown session '<id>'."` (generic). The expired-message variant (`"Session '<id>' expired (idle > <N>s). ..."`) is reserved for ids the idle reaper tombstoned.
- **Concurrent calls on the *same* session** — actions on one session are **serialized** (a per-session lock); a second in-flight action waits for the first. If the wait itself would exceed a bound, the second returns `409 "Session busy: another action is in progress."` rather than hanging the eval. (Within a single eval collection this is rare — steps run in order — but two collections sharing a session id, or a retry overlapping the original, must not corrupt the page.)
- **Session reaped *mid-action*** — the reaper never closes a session whose per-session lock is held; it checks the lock and skips/defers. A long `wait-for-load` won't be yanked out from under itself; the idle clock is "no action *completed or started* since…".
- **Locator that resolves but the element is detached by the time the action runs** — engine error → `422 "Action failed: <engine message>."` (the element matched, then went away — not a "no match" 404). Re-`wait-for` then retry.
- **`wait-for` with `state: "hidden"` / `"detached"` and the element is already gone** — *success* (`200`), not `404` — the requested state is "absent" and it's absent. Only the *positive* states (`visible`, `attached`) treat zero-match as failure.
- **Very long inputs** — `evaluate` expression or `type` text in the megabytes: accepted up to a request-size limit (`413 "Request body too large (limit <N> bytes)."` past it). `find` on a locator matching thousands of nodes: response capped at 50 elements with `count` reflecting the *returned* (truncated) count (same as `platform/playwright` — noted in `/docs`). A huge `Locator.value` (a giant XPath): same `413` if it pushes the body over the limit; otherwise it just runs and probably `404`s.
- **Long-running `navigate` / `wait-for-load`** that exceeds the *server's* hard request ceiling (~120s, separate from the action `timeout`) — `504` (see Error states); action `timeout` values above the ceiling are effectively clamped. Evals keep `timeout` ≤ the documented ceiling.
- **`screenshot` of a 0×0 or `display:none` element** (locator matched it) — Playwright errors; surface as `422 "Action failed: element is not visible."` not `404` (the element *exists*, it just can't be shot). `wait-for` with `state: "visible"` first, or accepting it won't render, is the workaround.
- **Empty `GET /v1/sessions`** — `{"count": 0, "sessions": []}`, `200` (not `404`). Operators expect an empty list, not an error, on a quiet service.
- **`POST /v1/sessions` with `idle_timeout_seconds` out of range** — clamped silently to `[30, server_max]`; the *effective* value is echoed in `SessionResponse.idle_timeout_seconds`; no `422` (a too-eager clamp-to-error would make the reaper eval brittle across config changes). A negative or non-int value is still `422` (type error).
- **`navigate` that triggers a download instead of a page load** — no page navigation; `200` with `status: null`, `title: ""`, `text: "Navigated to <url> but it triggered a download (no page load)."` so the eval can detect it rather than seeing a confusing success.
- **Capture target missing** — if an eval mis-types `capture={"SID": "$.sessionId"}` (wrong field), substitution yields an empty/unsubstituted `{{SID}}` and the next step's path becomes `/v1/sessions//navigate` → `404` (or `400` on the malformed path). Not our bug, but `404 "Unknown session ''."` (empty id in the message) is a clear breadcrumb.
- **`locator` field omitted on an action that requires it** (`click`, `type`, `select`, `hover`, `get-attribute`, `wait-for`, `find`) — `422` (FastAPI required-field). On actions where it's optional (`screenshot`, `get-text`, `get-html`, `press-key`), omitting it selects the page-level behavior described in the inventory.

## Architect

### Components

- **`api/` FastAPI app** (runs on Modal as the ASGI app; locally under uvicorn in docker-compose). Standard stack: every route declares a Pydantic `response_model`, every request body is a Pydantic model (incl. the `Locator` field — see below), schemas grouped in `schemas.py` per route module. Routers: `api/v1/sessions/` (lifecycle: `POST /v1/sessions`, `GET /v1/sessions`, `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`) and `api/v1/sessions/{id}/...` action routes mirroring `platform/playwright`'s verb set. Plus `/health` and `/metrics` — **both JSON with a Pydantic `response_model`** (not Prometheus text). The route layer is also where the **SSRF deny-list check** lives: on `navigate`, parse the `url`, reject non-`http(s)` scheme / malformed URL / a host in the hardcoded deny-list (loopback `127.0.0.0/8` + `::1` + `localhost`, RFC1918 private ranges, `169.254.0.0/16` link-local/metadata) → `400 "Refused to navigate to '<url>': host not allowed."` — *before* any `EngineSession` call (security engineer pins the exact CIDR set + resolution strategy in PLAN). Idempotent boot (no DB; `SessionManager` constructed empty in lifespan, reaper task started; lifespan shutdown closes all sessions + the shared browser). `X-Twin-*` middleware mounted per standard stack but a structural no-op in practice (no outbound platform deps — see Integrations).
- **`SessionManager`** (in-process singleton, lives in app state). Owns `dict[str, SessionRecord]` plus a bounded LRU **tombstone set** of recently *reaped* ids — only the idle reaper tombstones an id; an explicit `DELETE /v1/sessions/{id}` removes the session WITHOUT tombstoning it (so a follow-up request on a reaped id gets the specific expired-404, while a request on an explicitly-deleted id — or a double-DELETE — gets the generic unknown-404; Designer's idle-reap UX). A `SessionRecord` holds: `session_id` (uuid4 str), `created_at`, `last_used_at`, `current_url`, `viewport`, `idle_timeout` (effective per-session value), an `EngineSession`, and a per-session `asyncio.Lock`. Methods: `create(opts) -> SessionRecord` (raises `SessionCapacityError` at `max_sessions`), `get(id) -> SessionRecord` (raises `SessionGone`, distinguishing tombstoned-by-the-reaper → expired message from never-existed-or-explicitly-deleted → unknown message), `delete(id)`, `list()`, `reap_idle(now)`. A separate background `asyncio.Task` (the **idle reaper**) wakes every `reaper_interval` (~30s, `REAPER_INTERVAL_SECONDS`) and `delete()`s any record whose `last_used_at` is older than its `idle_timeout` (default 300s) — but never one whose lock is held (skip/defer to the next tick). `create()` enforces `max_sessions` (default 20). The route layer: `record = mgr.get(id)`; `async with record.lock:` do the engine call; on success `record.last_used_at = now()` (+ refresh `current_url` on navigate); the GET *status* endpoints (`GET /v1/sessions`, `GET /v1/sessions/{id}`) deliberately do **not** bump `last_used_at`, but the GET *action* endpoints (`console-logs`, `network-log`) DO bump it — they're session actions. Same-session requests serialize (Playwright pages are not concurrency-safe); different sessions run in parallel (a perf property contingent on `@modal.concurrent` — see Trade-offs).
- **`Engine` interface** (abstract; `api/engine/base.py`). The HTTP layer **only ever touches this interface** — it never imports `playwright`, and the engine layer never sees Pydantic. `async new_session(opts: SessionOpts) -> EngineSession`; `async close()` (tears down the shared browser + playwright driver). **`Locator` is the primary element-targeting primitive** — a plain engine-layer dataclass/typed shape (the route layer maps the inbound Pydantic `Locator` body field onto it): `Locator(strategy: Literal["role","text","label","placeholder","testid","alt","title","css","xpath"], value: str, options: LocatorOptions | None)` where `LocatorOptions` carries `name`, `exact`, `nth`, `has_text`, … (Playwright `getBy*` kwargs). `EngineSession` per-session verbs — every targeting method takes a `Locator`: `navigate(url, wait_until)`, `click(locator, timeout)`, `type(locator, text, clear, timeout)`, `select(locator, value, timeout)`, `hover(locator, timeout)`, `press_key(key, locator | None)`, `get_text(locator | None, timeout)`, `get_html(locator | None, outer)` (locator null ⇒ full `<html>` outerHTML — matches `GetHtmlRequest.locator: Locator | null = null`), `get_attribute(locator, attr, timeout)`, `find(locator) -> list[ElementInfo]` (the `query-selector-all` analog; `ElementInfo` = `{tag, text, attributes}`, capped 50), `wait_for_element(locator, state, timeout)` (note: `state: "hidden"|"detached"` treats a zero-match as success → 200, NOT `ElementNotFound`/404 — the engine implementer must not raise on negative states), `screenshot(locator | None, full_page)`, `evaluate(expression)`, `wait_for_load(wait_until, timeout)`, `console_logs(clear)`, `network_log(clear)`, `network_filter(...)`, `cookies(action, cookies)`, `local_storage(action, key, value)`, `current_url()`, `close()`. Engine raises the typed-error set the route layer maps (see Contracts): `ElementNotFound`, `LocatorAmbiguous`, `InvalidLocator` (unknown `strategy` / structurally malformed `Locator`), `InvalidLocatorSyntax` (engine rejects a `css`/`xpath` value string's grammar), `ActionTimeout`, `NavigationError`, `RuntimeEvalError`, `EngineCrash`/`EngineError`; `SessionManager` raises `SessionGone` / `SessionCapacityError`. `RemoteHttpError` is deliberately **not** an exception: a remote 4xx/5xx *page* still loaded, so `navigate` returns normally with that `status` in the body.
- **`PlaywrightEngine`** (concrete; `api/engine/playwright_engine.py`). On first `new_session`, lazily `async_playwright().start()` and `chromium.launch(headless=...)` once → one shared `Browser`. Each `new_session` = `browser.new_context(viewport=...)` + `context.new_page()` + attach `console`/`request`/`response` listeners that feed *that session's own* bounded log buffers (`MAX_LOG_ENTRIES`, default 500, ring-trimmed — same logic as the reference `BrowserState`). **`_resolve(page, loc) -> playwright.Locator`** is the locator dispatch: `role` → `page.get_by_role(loc.value, name=loc.options.name, exact=loc.options.exact)`; `text` → `page.get_by_text(loc.value, exact=…)`; `label` → `page.get_by_label(...)`; `placeholder` → `page.get_by_placeholder(...)`; `testid` → `page.get_by_test_id(loc.value)`; `alt` → `page.get_by_alt_text(...)`; `title` → `page.get_by_title(...)`; `css` → `page.locator(loc.value)`; `xpath` → `page.locator(f"xpath={loc.value}")`; then `.filter(has_text=…)` / `.nth(loc.options.nth)` if given. The action then operates on that resolved Playwright `Locator` with the request-param `timeout`. Unsupported `strategy` / structurally malformed `Locator` → `InvalidLocator`; a `css`/`xpath` value the engine rejects as bad grammar → `InvalidLocatorSyntax`; the resolved locator matching zero → `ElementNotFound`; matching >1 when the action needs exactly one and no `nth` was passed → `LocatorAmbiguous("locator matched N; pass options.nth")`. `EngineSession.close()` closes the `BrowserContext` (cheap; isolates cookies/storage/cache). The shared `Browser` stays warm until app shutdown. Session create *is* "launch" and session delete *is* "close" — `platform/playwright`'s `launch`/`close` endpoints have no per-session analog; `GET /v1/sessions/{id}` exposes `current_url` / `created_at` / `last_used_at` in their place. Note `{strategy:"css", value:"..."}` is *exactly* the old `selector:` string behavior — `Locator` is a strict superset of `platform/playwright`'s string selector, so migration is "wrap your selector in `{strategy:"css", value:...}`" while `role`+`name` becomes the documented default.
- **`modal_app.py`** (repo root). `modal.App` + custom `modal.Image` (Playwright base image, or `python:3.11-slim` + system libs + `playwright install chromium`, mirroring the reference Dockerfile), `image.add_local_dir("api", ...)`, then `@app.function(min_containers=1, max_containers=1, scaledown_window=..., timeout=120)` **+ `@modal.concurrent(max_inputs=N)` (N ≈ 8–16) — REQUIRED**, not optional: a `max_containers=1` function processes one input per container by default, which would serialize *all* sessions and falsify "different sessions run in parallel" (session isolation itself still holds even serialized; `@modal.concurrent` is what makes the concurrency real). Then `@modal.asgi_app()` returning the FastAPI `app`. This is the only Modal-specific file; nothing under `api/` knows it runs on Modal.

### Contracts

- **`pagehub-evals` collection step → `pagehub-browser` HTTP** (REST/JSON over HTTPS). `POST /v1/sessions` body `{headless?: bool, viewport_width?: int, viewport_height?: int, user_agent?: str, idle_timeout_seconds?: int}` → `201 SessionResponse` (`session_id, created_at, last_used_at, idle_timeout_seconds, headless, viewport, current_url`). Eval captures `session_id` and substitutes it into every following step's path (`/v1/sessions/{{session_id}}/navigate`, etc. — the Designer's path-param decision). Action bodies/responses follow `platform/playwright/app/schemas/browser.py` **with one structural change: every element-targeting body field that was `selector: str` becomes `locator: Locator` (`{strategy, value, options}`)** — so `seeds/playwright.py` does **not** port 1:1; the worked collection and `/docs` examples use `role`+`name`, with `{strategy:"css", value:"..."}` as the documented escape hatch. Caveat on the `platform/playwright` evals: they don't all port verbatim — e.g. its `no-browser-400` becomes our `404` on a dead session (a session is a resource), and `missing-element-404` ports with the locator change. Error semantics → see the route-layer mapping table next.
- **Route-layer error mapping table** — the HTTP `detail` body for each row is the canonical string in the Designer's *Error states* table — that is the single source of truth; this table maps only `(engine error / route condition) → status code`. (Short glosses below are descriptive only, not verbatim wording.)
  - `SessionGone` (manager — unknown id, bad uuid, or idle-reaped) → **404** (separate `detail` wording for unknown-id vs tombstoned-expired — see Designer).
  - `ElementNotFound` (resolved locator matched zero) → **404**. The attribute-not-found variant (`get_attribute`, attribute absent on the matched element) also → **404**.
  - `LocatorAmbiguous` (matched >1, action needs one, no `options.nth`) → **409**.
  - `InvalidLocator` (unknown `strategy`, or a structurally malformed `Locator` shape) → **422**. Pydantic body-shape errors (missing field, wrong type) are the standard FastAPI **422** validation array, separately.
  - `InvalidLocatorSyntax` (a `strategy:"css"|"xpath"` whose *value string* the engine rejects as bad grammar) → **400**. Distinct from `InvalidLocator` — the `Locator` shape is well-formed; the selector grammar is not.
  - `409 Session busy` (route-layer per-session lock-wait timeout — a concurrent call on the same session waited past the bound; not an engine-typed error) → **409**.
  - `ActionTimeout` (request-param `timeout` lapsed — element never reached state / page never reached load state) → **409**. NOT 408 (that means client-was-slow-sending), NOT 504. The **only** thing that yields **504** is the **server hard request ceiling** — the Modal function `timeout` (~120s), a transport-level cutoff above any per-action `timeout`; action `timeout` values above the ceiling are effectively clamped (documented).
  - `NavigationError` (DNS failure / connection refused / a bad scheme reaching the network layer) → **502** — *except* the SSRF-blocked-host / non-http(s)-scheme / malformed-URL case, which the **route layer catches first → 400** before the engine is called.
  - `RemoteHttpError` — **not an exception**: a remote 4xx/5xx *page* still loaded → return **200** with `NavigateResponse.status` = that code; the eval decides if that's a failure (`body.status` assertion).
  - `SessionCapacityError` (`POST /v1/sessions` at `max_sessions`) → **503** + `Retry-After: <s>` header. NOT 429 — there is no per-caller accounting; the *service* is at capacity, which is 503's meaning.
  - `RuntimeEvalError` (`evaluate` expression threw, screenshot of a detached / 0×0 / `display:none` element, other engine runtime fault caused by caller input) → **422**. Not 500 — caller input caused it.
  - `EngineCrash` / `EngineError` (browser process died, container OOM mid-action, unexpected) → **500** (details to logs only).
  - `413 Payload Too Large` (`evaluate` expression / `type` text past the request-size limit) → **413** — enforced at the ASGI/route boundary, not the engine.
- **Route layer → `SessionManager` → `EngineSession`** (in-process async). Route: `rec = mgr.get(id)` (→ `SessionGone` → 404); for `navigate`, run the SSRF deny-list check (→ 400 on a blocked host, *before* touching the engine); `async with rec.lock:` await the one engine coroutine; on success update `rec.last_used_at` (+ `rec.current_url` for navigate); map engine typed errors per the table. The per-session lock is the entire same-session concurrency contract — no engine call happens outside it; the reaper also checks `rec.lock.locked()` so a session is never closed mid-action. (Concurrent calls on the *same* session: the second waits on the lock; if the wait exceeds a bound it gets `409 "Session busy: another action is in progress."` rather than hanging the eval.)
- **Reaper → `SessionManager`** (in-process). Every `reaper_interval`: for each record, if `now - last_used_at > idle_timeout` and `not rec.lock.locked()`, `await rec.close()`, drop it, add the id to the tombstone LRU, increment `sessions_reaped_total`. Never reaps a session mid-operation (lock guard). **Feasibility note for PLAN (reliability engineer):** the reaper eval (Designer's Chain C) needs a way to "burn > `idle_timeout + reaper_interval` of wall-clock without touching the target session" — there is no sleep endpoint. The likely mechanism is a *throwaway second session* running `wait-for-load` (or repeated `wait-for-element`) against a deliberately slow fixture URL while the short-lived session sits idle; the reliability engineer pins the exact arithmetic and fixture in PLAN.
- **`/health` / `/metrics`** (HTTP, unauthenticated; **both JSON with a Pydantic `response_model`**). `GET /health` → `{status: "ok", commit: "<short-sha>", engine: "playwright-chromium", env: "<env>", live_sessions: int}` — field name is `commit` (the running build's `git rev-parse --short HEAD`), not `git_sha`, matching the Designer. `GET /metrics` → `{live_sessions: int, sessions_created_total: int, sessions_reaped_total: int, max_sessions: int, ...}` JSON — **not** Prometheus text (keeps the standard-stack `response_model` rule and the Designer's Chain C `body.sessions_reaped_total` path assertion). A Prometheus exposition surface (`/metrics/prometheus`) is a documented follow-up, not v1.

### Integrations

- **`pagehub-evals`** (inbound consumer; the only one in v1). The evals runner resolves a request's target from the env var `{factory_key}_url` in the run's environment variables (see `platform/evals/app/core/runner.py:206-208`). So the seed registers `factory_key: "pagehub-browser"` and the eval environment sets `pagehub-browser_url = https://<modal-app-url>` (plus a localhost value, `http://localhost:4010`, for local runs). Specs land under `platform/evals/specs/pagehub-browser/`, seeds as `platform/evals/seeds/pagehub_browser_*.py` — same convention as `seeds/playwright.py`, but element-targeting steps carry a `locator` object, not a `selector` string. (And the ported `platform/playwright` evals don't all map 1:1 — see the Contracts caveat.)
- **Modal** (runtime platform). Modal serves the ASGI app via `@modal.asgi_app()` from `modal_app.py`; the custom image bakes in Chromium so cold starts don't `playwright install` at request time. `@modal.concurrent(max_inputs=N)` is on the function (required — see Components/Trade-offs). Modal token (`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`) is used only by the deploy step, not at runtime.
- **`X-Twin-*` override pattern** — scaffolded per `~/.claude/CLAUDE.md` (middleware + `resolve_base_url` helper + contextvar, dev-only gate on `env == "development"`, silently strips the headers in every other env), but **a structural no-op in v1**: this service has zero outbound `*_BASE_URL` dependencies. It drives whatever target site the eval names, passed inline as the `url` field of `navigate` — that's not a platform dependency, so there's no env var and nothing to override. The scaffold exists so a future dependency is one env-var + one helper-call away, and so the eval-twin assertion machinery has the same shape here as everywhere else.
- **Deploy: in-repo tag-triggered GitHub Action** (recommended over the `pagehub-deploy-public-repos` mirror). `pagehub-infra/deploy-app.yml` has **no Modal path** (Vercel / Cloudflare / terragrunt / alembic only); the `pagehub-deploy-public-repos` mirror exists specifically so a *public* repo can run *private reusable workflows* — but `modal deploy` needs neither a reusable workflow nor private infra logic, it's just `pip install modal && modal deploy modal_app.py`. So `.github/workflows/deploy.yml` *in this repo*, triggered on `v*` (production) and `staging-*` (staging) tags, runs that one command with `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` from secrets. Because the repo is **public**, those token secrets must be reachable from it (see Trade-offs for the two options + manual steps). The mirror-repo route stays available as a fallback if org policy forbids exposing the Modal token to a public repo at all — then `deploy.yml` degrades to "print the manual `modal deploy` command", the same handoff pattern as `pagehub-evals`' `deploy.yml`.
- **Local dev** — `docker-compose.yml` with one service (`pagehub-browser`, port **`4010`** — decided; reference playwright is `:4006`, pagehub-evals is `:8002`, so `:4010` is free in the pagehub range), built from a `Dockerfile` mirroring the reference (`python:3.11-slim` + Playwright system libs + `pip install -e .` + `playwright install chromium`). `Makefile`: `up` / `down` / `test` / `logs`. `.env.example`: `ENV=development`, `IDLE_TIMEOUT_SECONDS=300`, `MAX_SESSIONS=20`, `MAX_LOG_ENTRIES=500`, `REAPER_INTERVAL_SECONDS=30`, `HEADLESS=true`, default viewport `1280x720`.

### Trade-offs

- **Structured `Locator` over a bare CSS string as the primary primitive — chosen.** A `selector: str` field everywhere is the classic reason E2E suites rot: a cosmetic class rename breaks every step. `Locator = {strategy, value, options}` makes `getByRole`/`getByText`/`getByLabel`/`getByPlaceholder`/`getByTestId`/`getByAltText`/`getByTitle` first-class, with `css`/`xpath` as the escape hatch — and because `{strategy:"css", value:"..."}` is *exactly* the old behavior, `Locator` is a strict superset of `platform/playwright`'s string selector; migration cost is "wrap your selector in `{strategy:"css", value:...}`" while the documented default becomes `role`+`name`. Cost: a slightly heavier request body and one dispatch (`_resolve`) in the engine; worth it for resilient suites. Alternative (rejected): keep `selector: str`, add `role:`/`text:` as *separate optional fields* — combinatorially messy in the schema and in `/docs`, and doesn't force the resilient form to be the obvious one.
- **Rich engine error taxonomy, not the coarse `ElementNotFound`/`Timeout`/`EngineError` triple** — the Designer's status matrix needs distinctions that the triple can't carry: ambiguous-locator (409) vs not-found (404), malformed-`Locator`-shape (422, `InvalidLocator`) vs bad-css/xpath-grammar (400, `InvalidLocatorSyntax`) vs not-found, action-timeout (409) vs server-ceiling-timeout (504), navigation-network-failure (502) vs blocked-host (400, caught at the route), runtime-eval-error (422) vs genuine crash (500), and a remote 4xx/5xx page that *isn't* an error at all (200 + body status). So `Engine` raises `ElementNotFound` / `LocatorAmbiguous` / `InvalidLocator` / `InvalidLocatorSyntax` / `ActionTimeout` / `NavigationError` / `RuntimeEvalError` / `EngineCrash`(`EngineError`), and `SessionManager` raises `SessionGone` / `SessionCapacityError`; the route layer owns the one mapping table (see Contracts). Cost: more exception classes; payoff: every Designer error row maps to exactly one, and `pagehub-evals` can assert the precise status.
- **THE Modal problem — chosen: (a) single-container pinning + `@modal.concurrent`.** Modal autoscales across multiple ephemeral containers; a `BrowserContext` created on container A is invisible to container B, and any container can be recycled mid-flight. Three options:
  - **(a) Single-container pinning** — `min_containers=1` (keep-warm: no cold start on the eval's first call), `max_containers=1` (everything lives in one process's memory → the registry is always consistent), generous `scaledown_window` ≥ `idle_timeout` (so a brief gap between eval steps doesn't kill a container holding live sessions; the reaper, not Modal, owns session lifetime), `timeout=120` (the slowest single browser op; this is also the *only* thing that produces a 504). **`@modal.concurrent(max_inputs=N)` (N ≈ 8–16) is required, not optional** — without it the one container processes one input at a time, serializing all sessions and falsifying "different sessions run in parallel" (isolation still holds; only the perf property is at stake). **This is the v1 choice — simple and correct; the alternatives are strictly heavier with no v1 payoff.**
  - **(b) Modal Dict registry + sticky routing** — a `modal.Dict` maps `session_id → container_id`; but Modal has no first-class request affinity, so a container that receives a call for a session it doesn't own must self-address a dispatch to the owning container and proxy the response. Real distributed-systems work (failure handling, container-death cleanup, the proxy hop). **Documented upgrade path** if throughput ever matters; not v1.
  - **(c) Stateless re-hydration** — persist cookies/localStorage/url after each call, rebuild a `BrowserContext` on whatever container handles the next call. Loses in-page JS state, SPA client state, scroll position, and the console/network buffers — exactly what a frontend-app eval needs. **Rejected.**
- **`BrowserContext` per session, not `Browser` per session** — contexts are cheap (~tens of MB vs. launching a whole Chromium), give full cookie/storage/cache isolation, and share one warm browser process. Trade-off accepted: a crash in the shared browser process takes down all sessions (acceptable for an eval tool; reaper + caller-retries absorb it → `EngineCrash` → 500, then re-create).
- **In-process registry, no DB** — sessions are inherently live browser handles that can't be serialized, so durability buys nothing and a DB would just add a dependency. Consequence: every restart drops all sessions (fine — the next request to a dropped id gets the 404 contract).
- **`/metrics` as JSON, not Prometheus text** — the standard stack mandates a Pydantic `response_model` on every route, and the Designer's reaper eval asserts `body.sessions_reaped_total` (a JSON path); a Prometheus text exposition would satisfy neither. A `/metrics/prometheus` surface is a cheap follow-up if a scraper ever needs it.
- **SSRF check in the route layer, not the engine** — the deny-list is policy, not browser mechanics, and it must run *before* any engine call so a blocked host never even spawns a navigation; placing it at the route layer also keeps it in one spot for the security engineer to extend (the configurable-allowlist follow-up slots in here). v1 is a minimal hardcoded deny-list (loopback / RFC1918 / `169.254.0.0/16`) → `400 host not allowed`; the exact CIDR set + a note on DNS-rebinding is the security engineer's PLAN item.
- **In-repo `modal deploy` workflow, not the `pagehub-deploy-public-repos` mirror** — `modal deploy` is self-contained (no private reusable workflow, no terragrunt/Vercel logic), so the mirror's reason-for-being doesn't apply. Cost: a **public** repo's Action needs the Modal token. Two options, **manual steps for Gavin (feed-up to the Manager's Open questions)**: (1) **make org secrets visible to the public repo** — set `PAGEHUB_MODAL_TOKEN_ID` / `PAGEHUB_MODAL_TOKEN_SECRET` (the existing org `PAGEHUB_MODAL_*` pair) repository-access to include `pagehub-io/pagehub-browser`, reference them in `deploy.yml`; or (2) **repo-level secrets** — `gh secret set MODAL_TOKEN_ID -R pagehub-io/pagehub-browser` and `gh secret set MODAL_TOKEN_SECRET -R ...`. Recommend (1) — avoids a second copy of the token. Also: create the Modal app/workspace, confirm the deployed app's public URL, and put that URL into the pagehub-evals environment as `pagehub-browser_url`. If exposing the token to a public repo is unacceptable at all, fall back to the mirror repo (add a `modal deploy` step there) or run `modal deploy` by hand with `deploy.yml` just printing the command.
- **Risks:** ~50–150 MB of container memory per live Chromium context → `max_sessions` cap + idle reaper bound it; size the Modal container memory tier for `max_sessions * ~150 MB` plus headroom, and remember `@modal.concurrent(max_inputs=N)` means up to N actions in flight at once in that one process. Full-page screenshots are large base64 blobs in JSON — same as the reference service; document a soft size expectation, no separate binary endpoint in v1. Cold start of a Chromium-bearing Modal image is slow (image pull + browser launch) — `min_containers=1` keep-warm hides it from evals; the first deploy pays it once. "The one container dies mid-eval": every in-flight session vanishes; the caller's next step gets **404** and the eval must `POST /v1/sessions` again to recover — a documented, tested behavior, not a bug. Public + drives a headless browser to arbitrary URLs = an SSRF surface — v1 mitigates with the hardcoded route-layer deny-list; a configurable allowlist + DNS-rebinding hardening is the documented follow-up.
