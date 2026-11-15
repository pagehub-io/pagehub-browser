# A fast, retry-safe session DELETE

**Status:** proposed (15 Nov 2026). Fixes pagehub-io/pagehub-browser#26.
**Author:** the evalspot-pagehub-infra lead (Opus 5.5 medium).

## Problem

`DELETE /v1/sessions/{id}` can take seconds, and a client that times out and
retries then fails on a delete that worked. #26 has the numbers.

- **Mechanism.** This is read from the code, and the timing below was
  executed. `SessionManager.delete` takes the session's lock and awaits
  `engine_session.close()`. That closes the context, then the session's own
  Chromium process (`browser.close()`), and waits for the process to exit. Only
  after that does it pop the record and return 204.
- **Under CPU and IO pressure, that exit takes seconds.** In an A/B at load1
  11.6 to 12.4, the DELETE mean was 1.5 to 1.8 s with `BROWSER_SPARE=false`,
  and 0.35 to 0.73 s with `true`. At load1 about 8 it was 32 to 49 ms. So load
  is the driver, and the warm spare isn't.
- **Why the retry fails.** Explicit deletion never tombstones (SPEC.md,
  "Double DELETE"). A retry after a first DELETE that did finish gets
  `404 "Unknown session '<id>'."`. A retry while the first still holds the lock
  waits up to `lock_acquire_timeout_ms` and gets 409. Either fails a client
  that asserts 204.
- **A cancellation hole,** reasoned and not executed. If the client disconnects
  and the handler is cancelled mid-close, `suppress(Exception)` doesn't catch
  `CancelledError`. The `finally` pops the record anyway, and that Chromium
  process may not finish closing. Nothing tracks it afterwards.

## Change

### 1. DELETE returns once the session is unreachable; the process closes in the background

`SessionManager.delete(session_id, *, lock_acquire_timeout_s)` keeps its
lookup and lock acquire, including the 404 for unknown ids and the 409 when the
lock can't be had. Once it holds the lock:

1. It sets `rec.closing = True`, pops the record from `_records`, records the
   id as deleted (item 2), increments `sessions_deleted_total` and releases
   the lock. None of these steps awaits.
2. It hands `rec.engine_session` to `self._close_later(engine_session)` and
   returns. The route returns 204.

`_close_later` starts one `asyncio` task per close:

- **The set.** Each task goes into `self._pending_closes: set[asyncio.Task[None]]`,
  and a done-callback discards it.
- **The task body** awaits `engine_session.close()`. It logs any exception
  except `CancelledError` (`logger.warning`, with the short session id) and
  never re-raises.
- **Cancellation.** The task is created on the loop, not awaited by the
  request, so a client disconnect can't cancel it. Only shutdown cancels it.
  `PlaywrightEngineSession.close()` already marks the browser intentional
  before closing it, so `browser_restarts_total` stays a crash signal.
- **`close_all()` at shutdown** first awaits every pending close, with
  `asyncio.gather(*self._pending_closes, return_exceptions=True)`, bounded by a
  10 s `asyncio.wait`. Then it closes the remaining live records as today.
  Lifespan teardown order is unchanged: reaper, then `close_all`, then
  `engine.close()`.

The reaper's idle reap and the create path's LRU eviction (`_evict_one`) stay
synchronous. They aren't on a client's DELETE path, and the evict path's
capacity re-check depends on its close having finished.

**Capacity.** A pending close is still a running Chromium process, so
`create()` counts it. Its two capacity checks become
`len(self._records) + len(self._pending_closes) >= self.max_sessions`.
That keeps the bound of at most `max_sessions` session processes, plus the
spare, from SPEC "Risks". When the cap is reached only by pending closes,
`create()` returns 503 with `Retry-After` as today. `_oldest_evictable_idle`
never returns a pending close, because those aren't in `_records`.

### 2. DELETE of an id this service deleted returns 204

- **The LRU.** `SessionManager` keeps `self._deleted: OrderedDict[str, None]`,
  bounded by the existing `tombstone_max` (256), oldest evicted first. Only an
  explicit DELETE adds to it. The reaper's tombstones and LRU evictions don't.
- **`delete()`** checks `_deleted` before `get()`. An id found there returns
  at once with no lock, no close and no counter change, and the route returns
  **204**.
- **Every other route** on a deleted id is unchanged: `get()` still raises
  the generic unknown-404, because it doesn't consult `_deleted`. A
  never-existed id still gets 404 on DELETE. A reaped id still gets the
  expired-404 on DELETE and every other route.
- **This reverses one documented decision.** SPEC.md "Double `DELETE`" chose
  404 on a second DELETE so authors could tell "I deleted something" from
  "there was nothing there". A client's retry of a timed-out DELETE can't be
  told apart from a second DELETE, so 404 makes every retry unsafe. HTTP
  defines DELETE as idempotent (RFC 9110 §9.2.2), and 204 on a repeat is
  allowed. An id that never existed still gets 404, so "there was nothing
  there" stays diagnosable for any id the service didn't create. This PR
  amends that SPEC.md bullet, the `DELETE /v1/sessions/{session_id}` table
  row, and the `SessionManager` paragraph to match.

### 3. `/metrics` gains `closes_pending`

`closes_pending: int` is `len(self._pending_closes)`, added to
`metrics_snapshot()` and `MetricsResponse`. No other field changes.

## What doesn't change

- No other route, status code or body changes, apart from DELETE of a
  recently deleted id.
- Session isolation is unchanged: a closing session is unreachable as soon as
  its record is popped, before its process exits.
- The warm spare, the reaper and the eviction logic are unchanged.

## Failure modes

| Case | What happens |
|---|---|
| The client times out mid-DELETE, then retries | The first DELETE has already popped the record and added the id to `_deleted` before any await, so the retry gets 204. |
| The client disconnects during the lock wait | Nothing has changed yet. The record is still live, and a retry behaves as a first DELETE. |
| The client disconnects after the pop | The close task is on the loop and isn't cancelled. The process closes. |
| The close raises or the process is already dead | A warning is logged and the task ends. The record is already gone. |
| Many DELETEs under heavy load | Pending closes pile up and count toward `max_sessions`, so new creates get 503 with `Retry-After` until the closes finish. `closes_pending` shows it. |
| Shutdown with closes pending | `close_all` waits up to 10 s for them, then proceeds. Any processes left die with the container. |
| More than 256 deletes since an id was deleted | That id drops out of `_deleted`, and a very late retry gets 404 as today. A retry comes within seconds, so 256 is plenty. |

## Tests (with the change)

Unit tests, with `FakeEngine` and a `FakeEngineSession` whose `close()` can
block on an `asyncio.Event`:

1. With `close()` blocked, DELETE returns 204 before the close completes. The
   session is gone from `GET /v1/sessions`, and `closes_pending` is 1. After
   the event is released, `closes_pending` is 0 and `close()` ran once.
2. A second DELETE of the same id returns 204 and doesn't call `close()`
   again. `sessions_deleted_total` counts it once.
3. DELETE of a never-existed id returns 404 "Unknown session".
4. A reaped id returns the expired-404 on DELETE.
5. Any action route on a deleted id returns the generic unknown-404.
6. A request cancelled after the pop still lets the close complete: cancel the
   route task while `close()` is blocked, release it, and assert it ran.
7. `create()` counts pending closes: with `max_sessions = 1` and one pending
   close, create returns 503. After the close finishes, create succeeds.
8. `close_all()` awaits pending closes, and returns after the 10 s bound when a
   close never finishes. Use a short bound injected for the test.
9. A close that raises is logged, and leaves `closes_pending` at 0.
10. `/metrics` has `closes_pending`. Update the exact-field-set test,
    `tests/test_routes.py::test_metrics_full_field_set_and_counters`.
11. The existing double-DELETE test changes from 404 to 204 on the second
    DELETE. Name it in the PR.

## Proof (before and after)

- **Sandbox, part of the merge gate.** Tests 1, 2 and 6 fail on `main`.
  There, DELETE awaits the close, the second DELETE is a 404, and a cancel
  mid-close skips the rest of the close.
- **On hoss after deploy.** The github session rebuilds and restarts the
  shared container. One check runs under the eval-gate lock, or in a slot
  github gives:
  - 20 cycles of create, 2 s, DELETE;
  - **pass:** DELETE p90 under 100 ms at the box's usual load;
  - **pass:** a deliberate double DELETE returns 204 both times.

  The next full suites of the apps that DELETE sessions should show no DELETE
  retries that end in 404.

## Not in scope

- pagehub-evals re-firing non-idempotent requests (pagehub-evals#49), and
  per-attempt error recording (#50).
- Making the reaper's or eviction's close asynchronous.

## Build slicing

One slice of about 120 lines across three areas (`api/session_manager.py`,
`api/v1/sessions/schemas.py` and tests). SPEC.md is amended in this spec
PR. It touches cancellation
and process lifetime, so the build, its review and this spec's review run on
Opus 5.5 high, as the github session asked. It isn't security-relevant by the
fleet's definition: no authn, authz, data-visibility or credential path
changes. A deleted session becomes unreachable before its process exits, so
there's no new exposure.
