"""A fast, retry-safe session DELETE (specs/fast-idempotent-delete.md, issue #26).

Async unit tests against FakeEngine. The routes are called as functions on the test's own
event loop, so a FakeEngineSession.close() blocked on an asyncio.Event stays on one loop.
The numbers in the test names are the plan's Tests section.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import HTTPException

from api.config import settings
from api.engine.base import SessionOpts
from api.service_router import metrics as metrics_route
from api.session_manager import SessionCapacityError, SessionManager, SessionRecord
from api.v1.sessions.actions.router import click as click_route
from api.v1.sessions.actions.router import hover as hover_route
from api.v1.sessions.actions.schemas import ClickRequest, HoverRequest
from api.v1.sessions.router import create_session, delete_session, get_session, list_sessions
from api.v1.sessions.schemas import CreateSessionRequest, Locator
from tests.fakes import FakeClock, FakeEngine, FakeEngineSession

OPTS = SessionOpts()
LINK = Locator(strategy="role", value="link")


def _mgr(engine: FakeEngine, clock: FakeClock, **overrides: int) -> SessionManager:
    kwargs: dict[str, int] = dict(
        max_sessions=settings.max_sessions,
        idle_timeout_default=settings.idle_timeout_seconds,
        idle_timeout_min=30,
        idle_timeout_max=settings.idle_timeout_max_seconds,
        reaper_interval=settings.reaper_interval_seconds,
        tombstone_max=settings.tombstone_max,
    )
    kwargs.update(overrides)
    return SessionManager(engine, clock=clock, **kwargs)


async def _new(mgr: SessionManager, idle_timeout_seconds: int | None = 300) -> SessionRecord:
    return (await mgr.create(OPTS, idle_timeout_seconds=idle_timeout_seconds)).record


def _fake(rec: SessionRecord) -> FakeEngineSession:
    es = rec.engine_session
    assert isinstance(es, FakeEngineSession)
    return es


async def _spin(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _drain(mgr: SessionManager) -> None:
    """Cancel any close a test left blocked, so no task outlives the test's loop."""
    await mgr.close_all(pending_close_wait_s=0)


async def _status(coro: object) -> int:
    try:
        await coro  # type: ignore[misc]
    except HTTPException as exc:
        return exc.status_code
    return 200


# ---- 1. DELETE returns before the close completes ----------------------------------


async def test_01_delete_returns_204_before_close_completes():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    es = _fake(rec)
    ev = asyncio.Event()
    es.block_on["close"] = ev

    resp = await asyncio.wait_for(delete_session(rec.session_id, mgr), timeout=1)
    assert resp.status_code == 204
    await _spin()
    assert es.close_calls == 1 and es.closed is False  # the close is still running
    assert (await list_sessions(mgr)).count == 0
    assert (await metrics_route(mgr)).closes_pending == 1

    ev.set()
    await _spin()
    assert (await metrics_route(mgr)).closes_pending == 0
    assert es.close_calls == 1
    assert es.closed is True


# ---- 2. a second DELETE of the same id is 204 with no second close -----------------


async def test_02_second_delete_204_no_second_close():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    es = _fake(rec)

    assert (await delete_session(rec.session_id, mgr)).status_code == 204
    await _spin()
    assert (await delete_session(rec.session_id, mgr)).status_code == 204
    await _spin()
    assert es.close_calls == 1
    assert mgr.sessions_deleted_total == 1


# ---- 3 / 4. never-existed and reaped ids are still 404 on DELETE -------------------


async def test_03_delete_never_existed_404_unknown():
    mgr = _mgr(FakeEngine(), FakeClock())
    with pytest.raises(HTTPException) as ei:
        await delete_session("deadbeef" * 4, mgr)
    assert ei.value.status_code == 404
    assert "Unknown session" in ei.value.detail
    assert mgr.sessions_deleted_total == 0


async def test_04_delete_reaped_id_expired_404():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr, idle_timeout_seconds=30)
    clock.advance(31)
    await mgr.reap_pass()
    with pytest.raises(HTTPException) as ei:
        await delete_session(rec.session_id, mgr)
    assert ei.value.status_code == 404
    assert "expired" in ei.value.detail and "idle" in ei.value.detail
    assert mgr.sessions_deleted_total == 0


# ---- 5. action routes on a deleted id: generic unknown-404 -------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "navigate", {"url": "https://example.com"}),
        ("POST", "click", {"locator": {"strategy": "role", "value": "link"}}),
        ("POST", "get-text", {}),
        ("GET", "console-logs", None),
    ],
)
def test_05_action_on_deleted_id_unknown_404(client, method, path, body):
    r = client.post("/v1/sessions", json={"headless": True})
    sid = r.json()["session_id"]
    assert client.request("DELETE", f"/v1/sessions/{sid}").status_code == 204
    r = client.request(method, f"/v1/sessions/{sid}/{path}", json=body)
    assert r.status_code == 404
    assert "Unknown session" in r.json()["detail"]
    assert "expired" not in r.json()["detail"]


# ---- 6. a request cancelled after the pop still lets the close complete ------------


async def test_06_cancelled_request_still_closes():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    es = _fake(rec)
    ev = asyncio.Event()
    es.block_on["close"] = ev

    route = asyncio.create_task(delete_session(rec.session_id, mgr))
    for _ in range(50):
        if es.close_calls:
            break
        await asyncio.sleep(0)
    assert es.close_calls == 1  # close() is running and blocked
    route.cancel()  # the client disconnects
    await asyncio.gather(route, return_exceptions=True)
    ev.set()
    await _spin()
    assert es.closed is True, "the close was cut short by the request's cancellation"
    assert mgr.metrics_snapshot()["closes_pending"] == 0


# ---- 7. create() counts pending closes ----------------------------------------------


async def test_07_create_counts_pending_closes():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=1)
    rec = await _new(mgr)
    ev = asyncio.Event()
    _fake(rec).block_on["close"] = ev
    await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    assert mgr.live_sessions == 0 and mgr.metrics_snapshot()["closes_pending"] == 1

    # The close stays blocked: create waits out the bound, then 503s.
    with pytest.raises(SessionCapacityError) as ei:
        await mgr.create(OPTS, idle_timeout_seconds=None, pending_close_wait_s=0.05)
    assert ei.value.held_by_pending_close is True
    assert mgr.sessions_rejected_total == 1
    assert mgr.sessions_lru_evicted_total == 0

    # The close finishes inside the bound: create succeeds.
    asyncio.get_running_loop().call_later(0.05, ev.set)
    result = await mgr.create(OPTS, idle_timeout_seconds=None, pending_close_wait_s=2)
    assert result.evicted_session_id is None
    assert mgr.live_sessions == 1
    assert mgr.metrics_snapshot()["closes_pending"] == 0


async def test_07b_pending_close_503_retry_after_1(monkeypatch):
    """Round-3 nit 2: the 503 for a slot a pending close holds carries Retry-After: 1."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=1)
    monkeypatch.setattr(settings, "lock_acquire_timeout_ms", 50)
    rec = await _new(mgr)
    _fake(rec).block_on["close"] = asyncio.Event()
    resp = await asyncio.wait_for(delete_session(rec.session_id, mgr), timeout=1)
    assert resp.status_code == 204

    with pytest.raises(HTTPException) as ei:
        await create_session(CreateSessionRequest(), mgr)
    assert ei.value.status_code == 503
    assert ei.value.headers == {"Retry-After": "1"}
    await _drain(mgr)

    # A cap held by a live session keeps the reaper-interval Retry-After.
    await _new(mgr)
    with pytest.raises(HTTPException) as ei2:
        await create_session(CreateSessionRequest(), mgr)
    assert ei2.value.status_code == 503
    assert ei2.value.headers == {"Retry-After": str(settings.reaper_interval_seconds)}


# ---- 8. close_all() awaits pending closes, bounded ----------------------------------


async def test_08_close_all_bounds_and_cancels_stragglers_before_live_records():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    doomed = await _new(mgr)
    live = await _new(mgr)
    _fake(doomed).block_on["close"] = asyncio.Event()  # never set
    await mgr.delete(doomed.session_id, lock_acquire_timeout_s=1)
    straggler = next(iter(mgr._pending_closes))

    seen: dict[str, bool] = {}
    live_es = _fake(live)
    real_close = live_es.close

    async def recording_close() -> None:
        seen["straggler_done"] = straggler.done()
        seen["straggler_cancelled"] = straggler.cancelled()
        await real_close()

    live_es.close = recording_close  # type: ignore[method-assign]
    await asyncio.wait_for(mgr.close_all(pending_close_wait_s=0.05), timeout=2)
    assert seen == {"straggler_done": True, "straggler_cancelled": True}
    assert live_es.closed is True
    assert mgr._records == {}
    assert mgr.metrics_snapshot()["closes_pending"] == 0


async def test_08b_close_all_with_no_close_pending_closes_live_records():
    """An unguarded asyncio.wait on the empty set would raise and skip the live records."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    a, b = await _new(mgr), await _new(mgr)
    await mgr.close_all()
    assert _fake(a).closed is True and _fake(b).closed is True
    assert mgr._records == {}


# ---- 9. a close that raises -----------------------------------------------------------


async def test_09_close_that_raises_is_logged(caplog):
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    _fake(rec).raise_on["close"] = RuntimeError("browser already dead")
    with caplog.at_level(logging.WARNING, logger="api.session_manager"):
        assert (await delete_session(rec.session_id, mgr)).status_code == 204
        await _spin()
    assert mgr.metrics_snapshot()["closes_pending"] == 0
    msgs = [r for r in caplog.records if "close failed" in r.getMessage()]
    assert len(msgs) == 1
    assert rec.session_id[:8] in msgs[0].getMessage()
    assert msgs[0].levelno == logging.WARNING


# ---- 12. a request queued behind a DELETE gets 404 ---------------------------------


async def test_12_action_queued_behind_delete_404():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    sid = rec.session_id
    es = _fake(rec)
    ev = asyncio.Event()
    es.block_on["click"] = ev

    first = asyncio.create_task(_status(click_route(sid, ClickRequest(locator=LINK), mgr)))
    await _spin()
    assert es.calls == ["click"]  # the first action holds the lock, blocked in its verb
    delete = asyncio.create_task(_status(delete_session(sid, mgr)))
    await _spin()  # the DELETE queues on the lock
    second = asyncio.create_task(hover_route(sid, HoverRequest(locator=LINK), mgr))
    await _spin()  # the second action passes get() and queues behind the DELETE
    ev.set()

    assert await first == 200
    assert await delete == 200  # the route returned its 204 Response without raising
    with pytest.raises(HTTPException) as ei:
        await second
    assert ei.value.status_code == 404
    assert "Unknown session" in ei.value.detail
    assert "hover" not in es.calls
    assert mgr.actions_total == 1
    await _spin()


async def test_12b_status_probe_queued_behind_delete_404():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    sid = rec.session_id
    es = _fake(rec)

    await rec.lock.acquire()  # stands in for an action
    delete = asyncio.create_task(delete_session(sid, mgr))
    await _spin()  # the DELETE queues on the lock
    rec.lock.release()
    # No await in between: the probe passes get() and locked() before the DELETE wakes,
    # then queues behind it.
    with pytest.raises(HTTPException) as ei:
        await get_session(sid, mgr)
    assert ei.value.status_code == 404
    assert "Unknown session" in ei.value.detail
    assert (await delete).status_code == 204
    assert es.current_url_calls == 0
    await _spin()


# ---- 13. two DELETEs queued on the lock close once ---------------------------------


async def test_13_two_queued_deletes_close_once():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr)
    sid = rec.session_id
    es = _fake(rec)
    ev = asyncio.Event()
    es.block_on["click"] = ev

    action = asyncio.create_task(click_route(sid, ClickRequest(locator=LINK), mgr))
    await _spin()
    d1 = asyncio.create_task(delete_session(sid, mgr))
    d2 = asyncio.create_task(delete_session(sid, mgr))
    await _spin()
    ev.set()
    await action
    r1, r2 = await asyncio.gather(d1, d2)
    await _spin()
    assert (r1.status_code, r2.status_code) == (204, 204)
    assert es.close_calls == 1
    assert mgr.sessions_deleted_total == 1


# ---- 14. a create at the cap doesn't evict a bystander for a pending close ---------


async def test_14_create_at_cap_waits_and_never_evicts_bystander():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=2)
    bystander = await _new(mgr)
    clock.advance(10)  # idle past the 5 s recent-activity window: evictable
    mine = await _new(mgr)
    ev = asyncio.Event()
    _fake(mine).block_on["close"] = ev
    await mgr.delete(mine.session_id, lock_acquire_timeout_s=1)

    create = asyncio.create_task(
        mgr.create(OPTS, idle_timeout_seconds=300, pending_close_wait_s=2)
    )
    await asyncio.sleep(0.02)
    assert not create.done()  # waiting on the pending close, not evicting
    ev.set()
    result = await create
    assert result.evicted_session_id is None
    assert bystander.session_id in mgr._records
    assert _fake(bystander).closed is False
    assert mgr.sessions_lru_evicted_total == 0

    # Now a close that never finishes: the create 503s and the bystander stays live.
    _fake(result.record).block_on["close"] = asyncio.Event()
    await mgr.delete(result.record.session_id, lock_acquire_timeout_s=1)
    with pytest.raises(SessionCapacityError):
        await mgr.create(OPTS, idle_timeout_seconds=300, pending_close_wait_s=0.05)
    assert bystander.session_id in mgr._records
    assert _fake(bystander).closed is False
    assert mgr.sessions_lru_evicted_total == 0
    await _drain(mgr)


# ---- round-3 nit 1: a DELETE queued behind the reaper or an eviction ---------------


async def test_delete_queued_behind_reaper_409_at_lock_timeout(monkeypatch):
    """The reaper holds rec.lock and never releases it, so a DELETE queued behind it
    gets the 409 at the lock timeout, as before this change."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    monkeypatch.setattr(settings, "lock_acquire_timeout_ms", 50)
    rec = await _new(mgr, idle_timeout_seconds=30)
    clock.advance(31)

    delete = asyncio.create_task(delete_session(rec.session_id, mgr))
    await asyncio.sleep(0)  # the DELETE passes get(); its lock acquire isn't run yet
    await mgr.reap_pass()  # the reaper takes the free lock first and keeps it
    with pytest.raises(HTTPException) as ei:
        await delete
    assert ei.value.status_code == 409
    assert mgr.sessions_reaped_total == 1
    assert mgr.sessions_deleted_total == 0


async def test_delete_woken_ahead_of_reaper_acquire_204():
    """A DELETE woken just ahead of the reaper's acquire finds the record closing at its
    recheck: 204, no close of its own, and the id keeps the reaper's expired-404."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await _new(mgr, idle_timeout_seconds=30)
    sid = rec.session_id
    es = _fake(rec)

    await rec.lock.acquire()
    delete = asyncio.create_task(delete_session(sid, mgr))
    await _spin()  # the DELETE queues on the lock
    clock.advance(31)
    rec.lock.release()  # wakes the DELETE; locked() is false until it runs
    await mgr.reap_pass()  # sets closing, then queues its acquire behind the DELETE
    assert (await delete).status_code == 204
    assert es.close_calls == 1  # the reaper's close only
    assert mgr.sessions_deleted_total == 0
    assert mgr.sessions_reaped_total == 1
    assert sid not in mgr._deleted
    with pytest.raises(HTTPException) as ei:
        await delete_session(sid, mgr)
    assert ei.value.status_code == 404 and "expired" in ei.value.detail


async def test_delete_queued_behind_eviction_204():
    """A DELETE that passed get() before an eviction set closing gets 204 at its
    recheck; the id isn't added to _deleted, so a later DELETE gets the generic 404."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=1)
    victim = await _new(mgr)
    sid = victim.session_id
    es = _fake(victim)
    clock.advance(10)  # evictable
    ev = asyncio.Event()
    es.block_on["close"] = ev

    # Both start before either yields: the DELETE passes get() and schedules its lock
    # acquire, then the create's eviction (lock still free) sets closing and awaits the
    # close before that acquire runs.
    delete = asyncio.create_task(delete_session(sid, mgr))
    create = asyncio.create_task(mgr.create(OPTS, idle_timeout_seconds=300))
    await asyncio.sleep(0)
    assert victim.closing is True  # the eviction is closing it
    assert (await delete).status_code == 204
    ev.set()
    result = await create
    assert result.evicted_session_id == sid
    assert es.close_calls == 1
    assert mgr.sessions_deleted_total == 0
    assert sid not in mgr._deleted
    with pytest.raises(HTTPException) as ei:
        await delete_session(sid, mgr)
    assert ei.value.status_code == 404 and "Unknown session" in ei.value.detail
