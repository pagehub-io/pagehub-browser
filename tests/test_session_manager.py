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


async def test_create_up_to_cap_then_capacity_error_when_all_active():
    """Cap reached and every session is "recently active" (just created) -> 503."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=3)
    results = [await mgr.create(OPTS, idle_timeout_seconds=None) for _ in range(3)]
    assert mgr.sessions_created_total == 3
    # All 3 are "recently active" (last_used_monotonic == clock.now()), so the
    # LRU-evict path finds no candidate -> 503 SessionCapacityError.
    with pytest.raises(SessionCapacityError):
        await mgr.create(OPTS, idle_timeout_seconds=None)
    assert mgr.sessions_rejected_total == 1
    assert mgr.sessions_lru_evicted_total == 0
    # After explicit delete one slot is free -> the next create succeeds without eviction.
    await mgr.delete(results[0].record.session_id, lock_acquire_timeout_s=1)
    r = await mgr.create(OPTS, idle_timeout_seconds=None)
    assert r.evicted_session_id is None
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
    # The 10 concurrent creates all start while the registry is empty; they all see
    # last_used_monotonic == clock.now() once their slot lands, so no LRU eviction
    # fires (the recent-activity window is 5s). Exactly 5 succeed, 5 are rejected.
    assert len(ok) == 5
    assert len(rejected) == 5
    assert mgr.live_sessions == 5
    assert mgr.sessions_lru_evicted_total == 0
    # surplus engine sessions must have been closed (no leaked contexts)
    leaked = [s for s in engine.sessions if not s.closed]
    assert len(leaked) == 5  # exactly the 5 live ones; the 5 surplus were closed
    assert len([s for s in engine.sessions if s.closed]) == 5


async def test_delete_removes_and_does_not_tombstone():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = (await mgr.create(OPTS, idle_timeout_seconds=None)).record
    await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    with pytest.raises(SessionGone) as ei:
        mgr.get(rec.session_id)
    assert ei.value.expired is False
    assert mgr.sessions_deleted_total == 1


async def test_double_delete():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = (await mgr.create(OPTS, idle_timeout_seconds=None)).record
    await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    with pytest.raises(SessionGone) as ei:
        await mgr.delete(rec.session_id, lock_acquire_timeout_s=1)
    assert ei.value.expired is False
    assert mgr.sessions_deleted_total == 1


async def test_reaper_closes_idle_session():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock)
    rec = (await mgr.create(OPTS, idle_timeout_seconds=30)).record
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
    rec = (await mgr.create(OPTS, idle_timeout_seconds=30)).record
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
    bad = (await mgr.create(OPTS, idle_timeout_seconds=30)).record
    good = (await mgr.create(OPTS, idle_timeout_seconds=30)).record
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
        rec = (await mgr.create(OPTS, idle_timeout_seconds=30)).record
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
    r1 = (await mgr.create(OPTS, idle_timeout_seconds=5)).record
    assert r1.idle_timeout == 30
    r2 = (await mgr.create(OPTS, idle_timeout_seconds=99_999)).record
    assert r2.idle_timeout == settings.idle_timeout_max_seconds
    r3 = (await mgr.create(OPTS, idle_timeout_seconds=600)).record
    assert r3.idle_timeout == 600
    r4 = (await mgr.create(OPTS, idle_timeout_seconds=None)).record
    assert r4.idle_timeout == settings.idle_timeout_seconds


def test_timeout_config_ordering():
    assert settings.action_timeout_max_ms < settings.modal_function_timeout_ms
    assert settings.action_timeout_max_ms == 110000
    assert settings.modal_function_timeout_ms == 120000
    assert 30 <= settings.idle_timeout_seconds <= settings.idle_timeout_max_seconds


# ---- LRU eviction at MAX_SESSIONS --------------------------------------------


async def test_lru_evict_at_cap_when_all_idle():
    """20 idle sessions + 21st create -> 201 + evicted_session_id = the oldest one."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=20)
    sids = []
    for _ in range(20):
        # advance the clock between creates so each session has a distinct
        # last_used_monotonic — the oldest one becomes the LRU victim.
        rec = (await mgr.create(OPTS, idle_timeout_seconds=300)).record
        sids.append(rec.session_id)
        clock.advance(1)
    # Push every session past the 5s recent-activity window so all are evictable.
    clock.advance(10)

    result = await mgr.create(OPTS, idle_timeout_seconds=300)
    assert result.evicted_session_id == sids[0], "expected the oldest session to be evicted"
    assert mgr.live_sessions == 20
    assert mgr.sessions_lru_evicted_total == 1
    assert mgr.sessions_rejected_total == 0
    # The evicted session id is GONE from the registry — and no tombstone for it.
    assert sids[0] not in mgr._records
    assert sids[0] not in mgr._tombstones
    # Subsequent .get on that id returns generic SessionGone (expired=False).
    with pytest.raises(SessionGone) as ei:
        mgr.get(sids[0])
    assert ei.value.expired is False


async def test_lru_evict_releases_engine_resources():
    """The evicted session's engine_session.close() is invoked (no Playwright leak)."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=2)
    r1 = (await mgr.create(OPTS, idle_timeout_seconds=300)).record
    clock.advance(1)
    r2 = (await mgr.create(OPTS, idle_timeout_seconds=300)).record
    clock.advance(10)  # both past the recent-activity window

    closes_before = sum(1 for s in engine.sessions if s.closed)
    result = await mgr.create(OPTS, idle_timeout_seconds=300)
    closes_after = sum(1 for s in engine.sessions if s.closed)

    assert result.evicted_session_id == r1.session_id
    # Exactly one new close() — the evicted session. r2 is untouched.
    assert closes_after - closes_before == 1
    assert r1.engine_session.closed is True
    assert r2.engine_session.closed is False


async def test_no_eviction_when_all_sessions_recently_active():
    """All sessions used within the last 5s -> 503, no eviction.

    This is genuine concurrent-overload back-pressure, not capacity-exhaust-via-leak.
    """
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=3)
    for _ in range(3):
        await mgr.create(OPTS, idle_timeout_seconds=300)
        clock.advance(0.1)
    # All 3 are within the 5s recent-activity window — no eviction candidate.
    with pytest.raises(SessionCapacityError):
        await mgr.create(OPTS, idle_timeout_seconds=300)
    assert mgr.sessions_rejected_total == 1
    assert mgr.sessions_lru_evicted_total == 0
    assert mgr.live_sessions == 3


async def test_no_eviction_when_only_active_session_remains():
    """Mixed pool: 19 idle (past window) + 1 active (just used) — 21st succeeds
    by evicting the oldest of the 19 idle sessions; the 1 active one is untouched."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=20)
    idle_sids = []
    for _ in range(19):
        rec = (await mgr.create(OPTS, idle_timeout_seconds=300)).record
        idle_sids.append(rec.session_id)
        clock.advance(1)
    active = (await mgr.create(OPTS, idle_timeout_seconds=300)).record
    # Bump all 19 past the 5s window; "active" is touched right now.
    clock.advance(10)
    mgr.touch(active)

    result = await mgr.create(OPTS, idle_timeout_seconds=300)
    assert result.evicted_session_id == idle_sids[0]  # oldest idle, not the active
    assert mgr.live_sessions == 20
    assert mgr.sessions_lru_evicted_total == 1
    # The active session was not disturbed.
    assert active.session_id in mgr._records
    assert active.engine_session.closed is False
    # FakeEngine appends in creation order, so engine.sessions[0] is the very first
    # session — the one we expect to have been evicted (engine_session.close() called).
    assert engine.sessions[0].closed is True


async def test_evict_skips_locked_session_even_if_oldest():
    """The oldest session is locked (in-flight action) -> evict skips it and picks the
    next-oldest idle. Locked sessions are never yanked out from under an action."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=3)
    r1 = (await mgr.create(OPTS, idle_timeout_seconds=300)).record  # oldest
    clock.advance(1)
    r2 = (await mgr.create(OPTS, idle_timeout_seconds=300)).record  # next-oldest
    clock.advance(1)
    r3 = (await mgr.create(OPTS, idle_timeout_seconds=300)).record  # newest
    clock.advance(10)  # all past the recent-activity window
    await r1.lock.acquire()  # r1 is the oldest BUT locked
    try:
        result = await mgr.create(OPTS, idle_timeout_seconds=300)
        # r2 is the oldest *evictable* idle session.
        assert result.evicted_session_id == r2.session_id
        assert r1.engine_session.closed is False  # the locked one survived
        assert r2.engine_session.closed is True
        assert r3.engine_session.closed is False
    finally:
        r1.lock.release()


async def test_evicted_session_id_null_when_below_cap():
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=5)
    result = await mgr.create(OPTS, idle_timeout_seconds=300)
    assert result.evicted_session_id is None
    assert mgr.sessions_lru_evicted_total == 0


async def test_no_eviction_when_all_locked():
    """3 sessions, all locked (and all past the 5s window) — 4th create still 503s.
    Locked == active for back-pressure purposes; we never yank a locked session."""
    engine, clock = FakeEngine(), FakeClock()
    mgr = _mgr(engine, clock, max_sessions=3)
    recs = [(await mgr.create(OPTS, idle_timeout_seconds=300)).record for _ in range(3)]
    clock.advance(10)
    for r in recs:
        await r.lock.acquire()
    try:
        with pytest.raises(SessionCapacityError):
            await mgr.create(OPTS, idle_timeout_seconds=300)
        assert mgr.sessions_rejected_total == 1
        assert mgr.sessions_lru_evicted_total == 0
    finally:
        for r in recs:
            r.lock.release()
