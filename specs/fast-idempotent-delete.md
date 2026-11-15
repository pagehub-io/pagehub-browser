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

1. **It rechecks the record.** If `self.still_live(rec)` is false (see
   "Every lock holder rechecks" below), something else closed the session while
   this DELETE waited on the lock: a first DELETE of the same id, the reaper or
   an eviction. It releases the lock and returns, and the route returns 204.
   There is no close and no counter change. This is the retry that passed
   `get()` before the first DELETE popped the record and then queued on the
   lock behind an in-flight action.
2. Otherwise it sets `rec.closing = True`, pops the record from `_records`,
   records the id as deleted (item 2), increments `sessions_deleted_total` and
   releases the lock. None of these steps awaits.
3. It hands `rec.engine_session` to `self._close_later(engine_session)` and
   returns. The route returns 204.

`_close_later` starts one `asyncio` task per close:

- **The set.** Each task goes into `self._pending_closes: set[asyncio.Task[None]]`,
  and a done-callback discards it.
- **The task body** awaits `engine_session.close()` inside
  `try: ... except Exception:`. It logs the exception (`logger.warning`, with
  the short session id) and never re-raises it. `CancelledError` isn't an
  `Exception`, so it isn't caught: a cancel at shutdown ends the task.
- **Cancellation.** The task is created on the loop, not awaited by the
  request, so a client disconnect can't cancel it. Only shutdown cancels it.
  `PlaywrightEngineSession.close()` already marks the browser intentional
  before closing it, so `browser_restarts_total` stays a crash signal.
- **`close_all()` at shutdown** takes `pending = set(self._pending_closes)`.
  Only if `pending` is non-empty does it wait, with
  `asyncio.wait(pending, timeout=5)`: `asyncio.wait` raises `ValueError` on an
  empty set, and lifespan suppresses that, so an unguarded call would skip the
  live records on every ordinary shutdown. It then cancels every task still
  pending and awaits them with
  `asyncio.gather(*stragglers, return_exceptions=True)`, so none is still
  running when Playwright stops under it. Then, whether or not any close was
  pending, it closes the remaining live records as today. The 5 s bound
  leaves room inside docker's default 10 s stop grace period for the live
  records and `engine.close()`. Lifespan teardown order is unchanged: reaper,
  then `close_all`, then `engine.close()`.

**Every lock holder rechecks.** A request that passed `get()` and then waited
on `rec.lock` may get the lock after a DELETE has popped the record and
released it. Today that request runs only after the close has finished. Under
this plan the close has only just started, so the request would drive a page
whose context is closing, and could return 200. So
`SessionManager.still_live(rec) -> bool` is
`not rec.closing and self._records.get(rec.session_id) is rec`, and every path
that acquires `rec.lock` calls it right after acquiring:

- **The action runner** (`_run_action`, `api/v1/sessions/actions/router.py`).
  If the record isn't live, it releases the lock and raises
  `session_gone_http(SessionGone(session_id, expired=False))`, the generic
  unknown-404. It must raise the mapped `HTTPException`, not a bare
  `SessionGone`: no global exception handler maps `SessionGone`, so a bare one
  would be a 500. That happens before `actions_total` is incremented and
  before the engine verb runs.
- **The status probe** (`GET /v1/sessions/{id}`). Inside `async with rec.lock`,
  if the record isn't live, it raises the same
  `session_gone_http(SessionGone(session_id, expired=False))`, and the `with`
  releases the lock. The probe can queue: `rec.lock.locked()` is false right
  after a release, before the woken waiter runs, so a probe that passes its
  `locked()` check then waits behind that waiter.
- **`delete()`** itself, as step 1 above.

The recheck covers a request queued behind a DELETE or an eviction.
`_evict_one` sets `rec.closing` and takes no lock, so a request that gets the
lock after it finds the record closing and gets the 404. The reaper holds
`rec.lock` and never releases it (`api/session_manager.py:309`), so a request
queued behind the reaper still gets the 409 at the lock timeout, as today.
Only one woken just ahead of the reaper's acquire reaches the recheck. The
reaper takes only a lock it has just seen free, and `_evict_one` takes none,
so neither needs the recheck itself.

The reaper's idle reap and the create path's LRU eviction (`_evict_one`) stay
synchronous. They aren't on a client's DELETE path, and the evict path's
capacity re-check depends on its close having finished.

**Capacity.** A pending close is still a running Chromium process, so
`create()` counts it. That keeps the bound of at most `max_sessions` session
processes, plus the spare, from SPEC "Risks". Pending closes are never a reason
to evict a live session. Today the slot is free once DELETE returns. A pending
close holds it for about as long as the old DELETE took, so `create()` waits
for that instead of failing or evicting a bystander:

- **The count.** `_pending_count()` is the number of tasks in
  `_pending_closes` that aren't done. Counting `not task.done()` means the
  count never depends on when the done-callback runs. The cap is reached when
  `len(self._records) + self._pending_count() >= self.max_sessions`.
- **`create(opts, *, idle_timeout_seconds, pending_close_wait_s)`.** The route
  passes `settings.lock_acquire_timeout_ms / 1000` (5 s), the same bound a
  DELETE waits on the lock. Both of `create()`'s capacity checks (before
  `engine.new_session()` and after it) call `_make_room(deadline)`, with one
  deadline taken when `create()` starts.
- **`_make_room(deadline)`** does this:
  1. While the cap is reached and closes are pending, it awaits
     `asyncio.wait(pending, timeout=<time left>, return_when=FIRST_COMPLETED)`
     and rechecks. It stops when the cap isn't reached, or when no close is
     pending, or at the deadline.
  2. If the cap isn't reached, `create()` goes on.
  3. If it's still reached and closes are still pending, the slot is held
     by a close, not by a live session. `create()` returns 503 with
     `Retry-After` and evicts nothing.
  4. If it's still reached and no close is pending, the live records alone
     fill the cap. `create()` evicts the oldest idle session through
     `_evict_one` or returns 503, as today.

`_oldest_evictable_idle` never returns a pending close, because those aren't
in `_records`. The wait holds no lock, so concurrent creates may wait
together. The post-`new_session` check still catches one that overshoots, and
closes its fresh engine session before the 503 as today.

### 2. DELETE of an id this service deleted returns 204

- **The LRU.** `SessionManager` keeps `self._deleted: OrderedDict[str, None]`,
  bounded by the existing `tombstone_max` (256), oldest evicted first. Only an
  explicit DELETE adds to it. The reaper's tombstones and LRU evictions don't.
  `create()`'s id-collision loop checks `_deleted` as well as `_records` and
  `_tombstones`, so a new session can never take an id a DELETE would answer
  with 204. uuid4 makes a collision theoretical.
- **`delete()`** checks `_deleted` before `get()`. An id found there returns
  at once with no lock, no close and no counter change, and the route returns
  **204**.
- **Every other route** on a deleted id is unchanged: `get()` still raises
  the generic unknown-404, because it doesn't consult `_deleted`. A
  never-existed id still gets 404 on DELETE. A reaped id still gets the
  expired-404 on DELETE and every other route.
- **A DELETE queued behind the reaper or an eviction** finds the record
  closing at step 1's recheck and returns 204. The session is going away
  either way. Its id isn't added to `_deleted`, so a later DELETE of it gets
  what the reaper or eviction leaves (the expired-404 for a reaped id, the
  generic 404 for an evicted one).
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

`closes_pending: int` is `self._pending_count()`, added to
`metrics_snapshot()` and `MetricsResponse`. No other field changes.

## What doesn't change

- No other route, status code or body changes, apart from DELETE of a
  recently deleted id.
- Session isolation is unchanged: a closing session is unreachable as soon as
  its record is popped, before its process exits. `get()` turns away a new
  request, and the recheck after acquiring the lock turns away one that was
  already queued on it.
- The warm spare, the reaper and the eviction logic are unchanged.

## Failure modes

| Case | What happens |
|---|---|
| The client times out mid-DELETE, then retries | The first DELETE has already popped the record and added the id to `_deleted` before any await, so the retry gets 204. |
| The client times out while its DELETE waits on the lock, then retries | Both DELETEs passed `get()` and queue on the lock. The first to get it deletes. The other finds the record closing at its recheck and returns 204 with no second close and no second count. |
| An action or status probe is queued on the lock when DELETE takes it | It gets the lock after the DELETE releases it, finds the record closing at its recheck, and returns the generic unknown-404 without touching the engine. |
| The client disconnects during the lock wait | Nothing has changed yet. The record is still live, and a retry behaves as a first DELETE. |
| The client disconnects after the pop | The close task is on the loop and isn't cancelled. The process closes. |
| The close raises or the process is already dead | A warning is logged and the task ends. The record is already gone. |
| A create at the cap right after a DELETE | It waits up to 5 s for a pending close to finish, then creates. It doesn't evict another client's idle session for a slot a close holds. |
| Many DELETEs under heavy load | Pending closes pile up and count toward `max_sessions`. A create waits up to 5 s for one to finish, then gets 503 with `Retry-After`, and no live session is evicted for them. `closes_pending` shows it. |
| Shutdown with closes pending | `close_all` waits up to 5 s for them, then cancels the rest and awaits them, then closes the live records. Any process a cancelled close leaves dies with the container. |
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
   close that stays blocked, create waits out `pending_close_wait_s` (short,
   injected) and raises `SessionCapacityError`. With the close released inside
   the bound, create succeeds.
8. `close_all()` awaits pending closes. When a close never finishes, it
   returns after the bound (short, injected for the test), and the straggler
   task has been cancelled and is done before the live records are closed.
   **With no close pending**, `close_all()` on two live records closes both
   engine sessions and leaves `_records` empty. That catches an unguarded
   `asyncio.wait` on the empty set.
9. A close that raises is logged, and leaves `closes_pending` at 0.
10. `/metrics` has `closes_pending`. Update the exact-field-set test,
    `tests/test_routes.py::test_metrics_full_field_set_and_counters`.
11. **Existing tests that change.**
    - `tests/test_routes.py::test_delete_204_then_404` becomes
      `test_delete_twice_204`: the second DELETE returns 204.
    - `tests/test_session_manager.py::test_double_delete`: the second
      `delete()` returns without raising, and `sessions_deleted_total` stays 1.
    - `tests/test_session_manager.py::test_create_up_to_cap_then_capacity_error_when_all_active`:
      its last create now reaches the cap with the deleted session's close
      pending. It passes only through `_make_room`'s wait, so the build passes
      `pending_close_wait_s` and adds asserts that the deleted session's
      `close()` ran and `closes_pending` is 0. Its existing asserts stay. The
      review executed this sequence against the plan without the wait, and it
      raised `SessionCapacityError`.
12. **An action queued behind a DELETE gets 404.** Block an action on the
    engine verb. Start a DELETE, which queues on the lock. Then start a second
    action, which passes `get()` and queues behind the DELETE. Release the
    first action. The DELETE returns 204. The second action returns
    `404 "Unknown session"`, its engine verb isn't called, and
    `actions_total` counts only the first action.
    **The probe variant.** The test holds `rec.lock` itself and starts a
    DELETE, which queues on it. It releases the lock and, with no await in
    between, awaits `get_session(id, manager)` directly, so the probe passes
    `get()` and `locked()` before the DELETE wakes and then queues behind it.
    The DELETE returns 204. The probe raises `404 "Unknown session"`, and the
    engine session's `current_url()` isn't called. This ordering was executed in a
    pure-`asyncio` model on Python 3.11.11: `['delete-ran', 'probe-404']`.
13. **Two DELETEs queued on the lock close once.** Block an action so it holds
    the lock. Start two DELETEs of the same id, then release the action. Both
    return 204, `close()` is called once, and `sessions_deleted_total` is 1.
14. **A create at the cap doesn't evict a bystander for a pending close.**
    `max_sessions = 2`. One session belongs to another client and is idle past
    the 5 s recent-activity window. The other is deleted with its `close()`
    blocked, so one close is pending. Start a create, release the close while
    it waits, and assert the create succeeds with `evicted_session_id` None,
    the bystander is still live, and `sessions_lru_evicted_total` is 0. Then,
    with the close never released and a short `pending_close_wait_s`, the
    create raises `SessionCapacityError` and the bystander is still live.

## Proof (before and after)

- **Sandbox, part of the merge gate.** Tests 1, 2, 6, 12 and 13 fail on
  `main`. There, DELETE awaits the close, the second DELETE is a 404, and a
  cancel mid-close skips the rest of the close. An action queued behind a
  DELETE runs its verb on `main`, which has no recheck, and `FakeEngineSession`
  verbs don't check `closed`. Two DELETEs queued on the lock both close and
  both count on `main` too. Executed 15 Nov 2026 against `FakeEngine`
  at `1571e6c`, whose `api/session_manager.py` is `main`'s, with the lock held
  as a stand-in for an action: `results [None, None] close calls 2
  deleted_total 2`.
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

One slice of about 180 lines across four areas (`api/session_manager.py`, the
lock rechecks in `api/v1/sessions/router.py` and
`api/v1/sessions/actions/router.py`, `api/v1/sessions/schemas.py`, and tests).
SPEC.md and PLAN.md are amended in this spec PR. It touches cancellation
and process lifetime, so the build, its review and this spec's review run on
Opus 5.5 high, as the github session asked. It isn't security-relevant by the
fleet's definition: no authn, authz, data-visibility or credential path
changes. A deleted session becomes unreachable before its process exits, so
there's no new exposure.
