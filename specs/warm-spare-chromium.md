# A warm spare Chromium for session create

**Status:** proposed (14 Nov 2026). Fixes pagehub-io/pagehub-browser#20.
**Author:** the evalspot-pagehub-infra lead (Opus 5.5 medium).

## Problem

Each `POST /v1/sessions` launches its own Chromium process
(`PlaywrightEngine.new_session` calls `pw.chromium.launch(...)`). That is
deliberate: memory is reclaimed when a session closes, and a crash takes down
only its own session (see the comment on `new_session`). The cost is that a
create pays for a whole browser launch.

On hoss, the first launch after the browser has sat idle is slow. These
figures were measured, not reasoned, and #20 has the details:

- **A cold create took 18.96 s, and the next two 0.14 s and 0.13 s.** These
  were three timed creates on the idle service at 21:38 CST, 14 Nov, at
  load1 14.1 and io avg60 43.5.
- **A fresh container launched in 18.17 s, then 0.07 s and 0.06 s.** That's
  the service's image in its own throwaway container, at 21:45 CST, load1
  15.9 and io avg60 43.8. On an already-launched browser, `new_context` plus
  `new_page` took 2.66 s the first time, then 0.042 s and 0.033 s.
- **The 4 retried creates in 14 Nov's eval runs** were each the service's
  first request after an idle gap: 13.6 min, 78 min, 128 min, and one at the
  start of the log window. None of the 339 creates in the access log took
  long enough to be retried while the service was busy.

pagehub-evals gives an outbound request 10 s when the item sets no
`timeout_ms` (`_OUTBOUND_TIMEOUT_SECONDS` in `api/runs/engine.py`), and
re-fires timeouts up to 4 attempts. A cold create therefore shows up as a
step that passed on attempt 2 or later. Under the attempts rule that hoss and
spot gates apply, a pass that needed a re-fire counts as a failure. About 204
launch items across six apps set no `timeout_ms`, so any of them can trip it.

## Change

Keep **one pre-launched Chromium, the spare**, in `PlaywrightEngine`. A create
takes it instead of launching. Each session still owns exactly one browser
process, which it closes when it ends. The spare is handed to one session and
never returned or reused, so the isolation design is unchanged.

### Engine (`api/engine/playwright_engine.py`)

The engine gets three new pieces of state:

- `_spare: Browser | None`;
- `_spare_born: float`, the monotonic time the spare was launched;
- `_refill_task: asyncio.Task | None`.

1. **`new_session(opts)`** takes the spare first:
   - It takes the spare with no `await` between the read and the clear:
     `browser, self._spare = self._spare, None`. Two concurrent creates can
     therefore never get the same spare.
   - A spare that is no longer connected (`not browser.is_connected()`) is
     marked an intentional close, closed with errors ignored, and treated as
     absent.
   - With no spare, the create launches inline as today.
   - The rest of the method is unchanged: `on("disconnected")` is already
     attached, then `new_context`, `new_page` and the route. So is the
     `finally` that reaps a browser not handed to a session.
   - Before returning or raising, `new_session` calls `_schedule_refill()`.
2. **`_schedule_refill(replace=False)`** starts
   `_refill_task = asyncio.create_task(self._refill(replace))` unless one of
   these holds:
   - a refill task is already running;
   - a spare is ready and `replace` is false;
   - the engine is closing;
   - `settings.browser_spare` is false.

   It never awaits, so a create never waits on the refill.
3. **`_refill()`** launches the replacement:
   - It calls `_ensure_pw()`, launches with the same `headless` and
     `_CHROMIUM_ARGS`, and attaches `on("disconnected", self._on_disconnected)`.
   - It then warms the new browser: it opens a context and an `about:blank`
     page, then closes the context. This reads the renderer files a create's
     first `new_context` plus `new_page` needs (2.66 s cold, above). The
     closed context leaves no cookie or storage behind.
   - Only then does it store the browser as `_spare` and set `_spare_born`.
     It stores it with no `await` in between:
     `old, self._spare = self._spare, browser`. If `old` is set (a recycle,
     below), it marks `old` intentional and closes it, errors ignored.
   - If the engine started closing while the launch was in flight, it marks
     the new browser intentional and closes it instead of storing it.
   - On any exception other than `CancelledError`, in the launch or the
     warm-up, it logs a warning ("spare chromium launch failed"), closes a
     launched browser with errors ignored, leaves `_spare` unchanged and
     returns. A failed warm-up is a failed refill.
     There is no retry loop: the next create launches inline and schedules
     another refill.
   - On `CancelledError`, it closes a browser that was launched but not yet
     stored, then re-raises.
4. **`warm()`**, a new method on `Engine` with a default no-op body, so
   `FakeEngine` and any future engine are unaffected:
   - In `PlaywrightEngine` it calls `_schedule_refill()`, and recycles the
     spare when it is older than `settings.browser_spare_max_age_seconds`.
   - To recycle, it calls `_schedule_refill(replace=True)`. The old spare stays
     in `_spare` until the new one is launched and warmed, and `_refill` then
     swaps them and closes the old one. A create during the recycle still
     takes the old spare, so there is no window where it must launch inline.
     If a create took the old spare first, the swap finds `None` and just
     stores the new one.
   - `warm()` is cheap and never raises: exceptions are logged and swallowed.
5. **Recycling keeps the spare warm.** A spare that has sat for two hours may
   itself have gone cold. Its files can drop out of the page cache, so its
   first `new_context` plus `new_page` could cost seconds, like the 2.66 s
   above. Relaunching and re-warming it every `browser_spare_max_age_seconds`
   (default 300) keeps Chromium's files hot, both the launch path and the
   context and page path. The cost is one launch and one blank page every
   5 minutes, about 0.1 s of work when warm. 300 s was chosen because the
   evidence shows no slow create within a few minutes of the last one.
   - **This premise is reasoned-not-executed.** That a 5-minute launch plus a
     blank page keeps those files cached through a 1 to 2 hour idle gap
     depends on hoss's page cache under its real load. It can't be shown in a
     sandbox, and this spec's review may not start Chromium. Its test is the
     hoss check in "Proof". If that check fails, the fallback there applies.
6. **`_on_disconnected` is unchanged.** A spare that crashes is an
   unexpected disconnect and counts in `browser_restarts_total`, as it should.
   `new_session` and `warm()` drop a disconnected spare the next time they
   look.
7. **`close()`** closes the spare before stopping Playwright:
   - set a `_closing` flag;
   - cancel and await `_refill_task`, suppressing `CancelledError`;
   - take the spare, mark it intentional and close it;
   - then the existing `pw.stop()`.
8. **`spare_ready() -> bool`**, a new `Engine` method that defaults to
   `False`. It returns `True` when `_spare` is set and connected.

### Wiring

- **The reaper loop** (`reaper_loop` in `api/session_manager.py`) calls
  `await manager.engine_warm()` once per tick, after `reap_pass()`, in its own
  `try`. A tick where `reap_pass()` raises still refills and recycles the
  spare. `SessionManager.engine_warm()` delegates to `self._engine.warm()`.
  The reaper already ticks every `reaper_interval_seconds` (30), so a spare
  older than its max age is recycled within 30 s of its deadline. A crashed
  or failed spare is refilled within 30 s, even with no create.
- **Startup:** the lifespan calls `engine.warm()` without awaiting the launch.
  `warm()` only schedules the refill task, so boot isn't delayed and `/health`
  answers at once. The first create after boot may still launch inline if the
  spare isn't ready yet. That case is no worse than today.
- **`/metrics`** gains `spare_ready: bool`, from `engine.spare_ready()`
  through `metrics_snapshot()`, so an eval or operator can see the spare.
  `MetricsResponse` gets the field, and `/health` is unchanged.
  `metrics_snapshot()` is typed `dict[str, int]` today. Its type widens to
  `dict[str, int | bool]`.
- **Settings** (`api/config.py`, documented in `.env.example`):
  - `browser_spare: bool = True` (`BROWSER_SPARE`). False restores today's
    behaviour exactly: no spare, and `warm()` does nothing.
  - `browser_spare_max_age_seconds: int = 300` (`BROWSER_SPARE_MAX_AGE_SECONDS`).

### What doesn't change

- **`max_sessions`** counts sessions, not the spare. One spare is at most one
  extra Chromium (about 100 to 150 MB idle) on top of `max_sessions`. The
  container has no memory limit in `docker-compose.yml`. SPEC.md "Risks" is
  amended in this PR to size memory for `(max_sessions + 1)` browsers.
- **No status code, body or locator behaviour changes,** and `SessionOpts`
  still applies per context: viewport and user agent are context options, so
  a spare launched before the request needs nothing from it.
- **Session isolation.** A spare holds no context, page, cookie or storage
  before it is taken. Its warm-up context is closed before it is stored. Once taken, it belongs to one session and is closed with
  it. Nothing ever returns a browser to `_spare`.

## Failure modes

| Case | What happens |
|---|---|
| The spare launch or warm-up fails (OOM, missing binary) | A warning is logged and `_spare` stays `None`. Creates launch inline as today. There is at most one refill in flight, started by the next create or reaper tick. |
| The spare crashes while idle | `browser_restarts_total` +1. The next `new_session` or `warm()` sees `is_connected()` false, drops it and refills. |
| Two creates arrive at once | One takes the spare. The other launches inline (today's cost) and the refill is scheduled once. |
| The client disconnects mid-create | Unchanged: the `finally` reaps a browser not handed to a session, whether it was the spare or a fresh launch. A refill was already scheduled. |
| Shutdown mid-refill | `close()` cancels the refill. `_refill` closes a launched, unstored browser on `CancelledError`, or on seeing `_closing`. |
| `BROWSER_SPARE=false` | Today's behaviour. |

## Tests (built with the change)

In `tests/`, with the fake Playwright objects these tests already use, or a
small fake `Browser` that records `close()` and `is_connected()`:

1. With a spare ready, `new_session` calls `chromium.launch` zero times, and
   the returned session's browser is the former spare. A refill is then
   scheduled and launches once.
2. With no spare, `new_session` launches inline once, as today.
3. Two concurrent `new_session` calls with one spare: exactly one gets it, the
   other launches, and only one refill task starts.
4. A disconnected spare is closed and skipped: the create launches inline.
5. A refill failure is logged, leaves `_spare` `None`, and doesn't raise into
   the create. The next `warm()` schedules a new refill. A failure in the
   warm-up's `new_context` or `new_page` counts the same way, and the
   launched browser is closed.
6. `warm()` recycles a spare older than its max age: the new one is launched
   and stored before the old one is closed, the old one is closed as
   intentional, and `browser_restarts_total` doesn't move.
7. The stored spare has been warmed: a context was opened on it and closed,
   and `browser.contexts` is empty when it is stored.
8. `close()` during an in-flight refill leaves no unclosed browser.
9. `BROWSER_SPARE=false`: `warm()` and `new_session` never create a spare.
10. `/metrics` returns `spare_ready` (FakeEngine: `false`). The exact-field-set
    test, `tests/test_routes.py::test_metrics_full_field_set_and_counters`,
    gains the key.
11. A tick where `reap_pass()` raises still calls `engine_warm()`.

The existing browser integration test (`tests/test_browser_integration.py`,
run in the Docker image) gains one case. It boots the engine, waits until
`spare_ready()`, then times a create and asserts it took no new
`chromium.launch`, by counting launches with a spy.

## Proof (before and after)

- **Sandbox, part of the merge gate.**
  - Tests 1 and 4 are the before/after proof. Each sets `engine._spare`
    directly, so on `main` each fails on behaviour. There `new_session`
    ignores `_spare`: test 1 sees a launch, and test 4 sees the dead spare
    left unclosed.
  - They pass on the branch. Test 6 is a regression test only, since on
    `main` it fails just because `warm()` doesn't exist.
- **On hoss, after deploy.**
  - The operator (the github session) rebuilds and restarts the shared
    `pagehub-browser` container. No lead does that.
  - Then time the first create after at least 60 minutes of no sessions,
    under the box's usual load. The retried creates in #20 came after 78
    and 128 min idle, so a shorter gap wouldn't test the hard case.
  - **Pass:** under 2 s, and `/metrics` shows `spare_ready: true` before it.
  - Then the next RA or Give Happy eval run that would have retried shows
    every launch step at attempts 1.
- **If the hoss check fails** (a create over 2 s with a ready spare), the
  fallback is a pagehub-evals change: a longer default timeout for browser
  session creates. That's a separate spec. Until then, #20 stays open.

## Not in scope

- **Per-item `timeout_ms` on the ~204 launch items.** It isn't needed if this
  lands, and an eval writer can still add one.
- **pagehub-evals#49** (re-firing a non-idempotent POST) and **#50**
  (per-attempt errors not recorded) stay separate.
- **A pool of more than one spare.** The evidence shows one cold create per
  idle gap. The creates right after it are warm, so a single spare covers it.

## Build slicing

One slice: the engine, wiring, settings, metrics field and tests are about
150 lines in four files. It's not security-relevant, because no authn, authz,
data visibility or credential path changes, and session isolation is
preserved by construction (the spare is never reused). So it builds on
DeepSeek v4.1 flash through opencode, and its review runs on Sonnet 5.5 high.
