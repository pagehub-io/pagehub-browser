"""Unit tests for SessionManager + the idle reaper, against the FakeEngine."""

from __future__ import annotations

import asyncio

import pytest

from api.config import settings
from api.engine.base import SessionOpts
from api.session_manager import SessionCapacityError, SessionGone, SessionManager
from tests.fakes import FakeClock, FakeEngine

OPTS = SessionOpts()


def _mgr(engine: FakeEngine, clock: FakeClock, **overrides) -> SessionManager:
    kwargs = dict(
        max_sessions=settings.max_sessions,
        idle_timeout_default=settings.idle_timeout_seconds,
        idle_timeout_min=30,
        idle_timeout_max=settings.idle_timeout_max_seconds,
        reaper_interval=settings.reaper_interval_seconds,
        tombstone_max=settings.tombstone_max,
        clock=clock,
    )
    kwargs.update(overrides)
    return SessionManager(engine, **kwargs)


async def test_create_up_to_cap_then_capacity_error():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=3)
    recs = [await mgr.create(OPTS, idle_timeout_seconds=None) for _ in range(3)]
    assert mgr.sessions_created_total == 3
    with pytest.raises(SessionCapacityError):
        await mgr.create(OPTS, idle_timeout_seconds=None)
    assert mgr.sessions_rejected_total == 1
    await mgr.delete(recs[0].session_id, lock_acquire_timeout_s=1)
    await mgr.create(OPTS, idle_timeout_seconds=None)
    assert mgr.sessions_created_total == 4


async def test_create_concurrent_at_cap_never_overshoots():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=5)
    results = await asyncio.gather(
        *[mgr.create(OPTS, idle_timeout_seconds=None) for _ in range(10)],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, SessionCapacityError)]
    assert len(ok) == 5
    assert len(rejected) == 5
    assert mgr.live_sessions == 5
    # surplus engine sessions must have been closed (no leaked contexts)
    leaked = [s for s in engine.sessions if not s.closed]
    assert len(leaked) == 5  # exactly the 5 live ones; the 5 surplus were closed
    assert len([s for s in engine.sessions if s.closed]) == 5


async def test_delete_removes_and_does_not_tombstone():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await mgr.create(OPTS, idle_timeout_seconds=None)
    await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    with pytest.raises(SessionGone) as ei:
        mgr.get(rec.session_id)
    assert ei.value.expired is False
    assert mgr.sessions_deleted_total == 1


async def test_double_delete():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await mgr.create(OPTS, idle_timeout_seconds=None)
    await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    with pytest.raises(SessionGone) as ei:
        await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    assert ei.value.expired is False
    assert mgr.sessions_deleted_total == 1


async def test_reaper_closes_idle_session():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await mgr.create(OPTS, idle_timeout_seconds=30)
    clock.advance(31)
    await mgr.reap_pass()
    assert mgr.live_sessions == 0
    assert engine.sessions[0].closed is True
    assert mgr.sessions_reaped_total == 1
    with pytest.raises(SessionGone) as ei:
        mgr.get(rec.session_id)
    assert ei.value.expired is True
    assert ei.value.idle_timeout == 30


async def test_reaper_skips_locked_session():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = await mgr.create(OPTS, idle_timeout_seconds=30)
    await rec.lock.acquire()
    clock.advance(31)
    await mgr.reap_pass()
    assert mgr.live_sessions == 1
    assert rec.closing is False
    assert mgr.sessions_reaped_total == 0
    rec.lock.release()
    await mgr.reap_pass()
    assert mgr.live_sessions == 0
    assert rec.closing is True
    assert mgr.sessions_reaped_total == 1


async def test_reaper_survives_one_bad_session():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    bad = await mgr.create(OPTS, idle_timeout_seconds=30)
    good = await mgr.create(OPTS, idle_timeout_seconds=30)
    bad.engine_session.raise_on["close"] = RuntimeError("close blew up")
    clock.advance(31)
    await mgr.reap_pass()
    # bad one's close raised but was suppressed; both are reaped
    assert mgr.live_sessions == 0
    assert good.engine_session.closed is True


async def test_reaper_drops_all_on_browser_death():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    await mgr.create(OPTS, idle_timeout_seconds=300)
    await mgr.create(OPTS, idle_timeout_seconds=300)
    engine._alive = False
    await mgr.reap_pass()
    assert mgr.live_sessions == 0
    assert mgr.sessions_reaped_total == 0
    assert len(mgr._tombstones) == 0


async def test_tombstone_lru_bounded():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, tombstone_max=8)
    ids = []
    for _ in range(8 + 10):
        rec = await mgr.create(OPTS, idle_timeout_seconds=30)
        ids.append(rec.session_id)
        clock.advance(31)
        await mgr.reap_pass()
    assert len(mgr._tombstones) == 8
    # oldest 10 fell out -> generic unknown-404
    with pytest.raises(SessionGone) as ei:
        mgr.get(ids[0])
    assert ei.value.expired is False
    # most recent stays
    with pytest.raises(SessionGone) as ei2:
        mgr.get(ids[-1])
    assert ei2.value.expired is True


async def test_idle_timeout_clamped():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    r1 = await mgr.create(OPTS, idle_timeout_seconds=5)
    assert r1.idle_timeout == 30
    r2 = await mgr.create(OPTS, idle_timeout_seconds=99_999)
    assert r2.idle_timeout == settings.idle_timeout_max_seconds
    r3 = await mgr.create(OPTS, idle_timeout_seconds=600)
    assert r3.idle_timeout == 600
    r4 = await mgr.create(OPTS, idle_timeout_seconds=None)
    assert r4.idle_timeout == settings.idle_timeout_seconds


def test_timeout_config_ordering():
    assert settings.action_timeout_max_ms < settings.modal_function_timeout_ms
    assert settings.action_timeout_max_ms == 110000
    assert settings.modal_function_timeout_ms == 120000
    assert 30 <= settings.idle_timeout_seconds <= settings.idle_timeout_max_seconds
