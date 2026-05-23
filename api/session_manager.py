"""In-process multi-session registry + idle reaper.

A session is a live in-memory browser handle — not serializable, no DB. Container
recycle drops everything; the next request to a vanished id gets the 404 contract.
The asyncio event loop is single-threaded, so counters need no lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from api.engine.base import Engine, EngineSession, SessionOpts

# A session is "idle enough to evict" iff its last activity was at least this many
# seconds ago. Keeps the LRU eviction path from yanking a session out from under a
# burst of legitimate concurrent traffic — that case should still back-pressure (503).
EVICT_RECENT_ACTIVITY_WINDOW_S = 5.0

logger = logging.getLogger(__name__)


class SessionGone(Exception):
    """A session id is not live. expired=True iff it was tombstoned by the idle reaper."""

    def __init__(self, session_id: str, *, expired: bool, idle_timeout: int | None = None) -> None:
        self.session_id = session_id
        self.expired = expired
        self.idle_timeout = idle_timeout
        if expired:
            super().__init__(
                f"Session '{session_id}' expired (idle > {idle_timeout}s). Create a new session."
            )
        else:
            super().__init__(f"Unknown session '{session_id}'.")


class SessionCapacityError(Exception):
    """POST /v1/sessions at MAX_SESSIONS."""

    def __init__(self, max_sessions: int) -> None:
        self.max_sessions = max_sessions
        super().__init__(
            f"Session capacity reached ({max_sessions} active). Retry shortly or DELETE an idle session."
        )


class SessionBusy(Exception):
    """A same-session action (or DELETE) could not acquire the per-session lock in time."""

    def __init__(self) -> None:
        super().__init__("Session busy: another action is in progress.")


class CreateResult(NamedTuple):
    """Return value of SessionManager.create().

    `evicted_session_id` is set iff the LRU-evict-oldest-idle path fired (capacity
    was hit, but an idle session was reclaimed to make room). Otherwise None.
    """

    record: "SessionRecord"
    evicted_session_id: str | None


@dataclass
class SessionRecord:
    session_id: str
    created_at: float                 # wall-clock epoch seconds
    last_used_at: float               # wall-clock epoch seconds
    last_used_monotonic: float        # monotonic — THE reaper's idle clock
    viewport: tuple[int, int]
    idle_timeout: int                 # effective per-session timeout (seconds)
    headless: bool
    engine_session: EngineSession
    current_url: str | None = None
    closing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def short(session_id: str) -> str:
    return session_id[:8]


class SessionManager:
    def __init__(
        self,
        engine: Engine,
        *,
        max_sessions: int,
        idle_timeout_default: int,
        idle_timeout_min: int,
        idle_timeout_max: int,
        reaper_interval: int,
        tombstone_max: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self.max_sessions = max_sessions
        self.idle_timeout_default = idle_timeout_default
        self.idle_timeout_min = idle_timeout_min
        self.idle_timeout_max = idle_timeout_max
        self.reaper_interval = reaper_interval
        self.tombstone_max = tombstone_max
        self._clock = clock

        self._records: dict[str, SessionRecord] = {}
        self._tombstones: "OrderedDict[str, int]" = OrderedDict()  # sid -> idle_timeout used

        self.sessions_created_total = 0
        self.sessions_reaped_total = 0
        self.sessions_deleted_total = 0
        self.sessions_rejected_total = 0
        self.sessions_lru_evicted_total = 0
        self.actions_total = 0
        self.action_errors_total = 0

        self.started_at_monotonic = self._clock()

    # ---- counters / introspection ------------------------------------------------

    @property
    def live_sessions(self) -> int:
        return len(self._records)

    @property
    def browser_restarts_total(self) -> int:
        return self._engine.browser_restarts_total()

    def uptime_seconds(self) -> int:
        return int(self._clock() - self.started_at_monotonic)

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "live_sessions": self.live_sessions,
            "max_sessions": self.max_sessions,
            "sessions_created_total": self.sessions_created_total,
            "sessions_reaped_total": self.sessions_reaped_total,
            "sessions_deleted_total": self.sessions_deleted_total,
            "sessions_rejected_total": self.sessions_rejected_total,
            "sessions_lru_evicted_total": self.sessions_lru_evicted_total,
            "actions_total": self.actions_total,
            "action_errors_total": self.action_errors_total,
            "browser_restarts_total": self.browser_restarts_total,
            "uptime_seconds": self.uptime_seconds(),
        }

    # ---- tombstones --------------------------------------------------------------

    def _tombstone_add(self, session_id: str, idle_timeout: int) -> None:
        self._tombstones[session_id] = idle_timeout
        self._tombstones.move_to_end(session_id)
        while len(self._tombstones) > self.tombstone_max:
            self._tombstones.popitem(last=False)

    # ---- lifecycle ---------------------------------------------------------------

    def _effective_idle_timeout(self, requested: int | None) -> int:
        if requested is None:
            return self.idle_timeout_default
        return max(self.idle_timeout_min, min(self.idle_timeout_max, requested))

    def _oldest_evictable_idle(self) -> SessionRecord | None:
        """Return the SessionRecord with the oldest `last_used_monotonic` that is safe
        to evict (not closing, lock not held, no activity in the last
        EVICT_RECENT_ACTIVITY_WINDOW_S seconds), or None if no session qualifies."""
        now = self._clock()
        candidates = [
            rec
            for rec in self._records.values()
            if not rec.closing
            and not rec.lock.locked()
            and (now - rec.last_used_monotonic) >= EVICT_RECENT_ACTIVITY_WINDOW_S
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda r: r.last_used_monotonic)

    async def _evict_one(self, rec: SessionRecord) -> None:
        """Close one session synchronously to make room for a new create.

        Does NOT acquire the per-session lock — the caller has already filtered
        out locked sessions in `_oldest_evictable_idle`. This stays atomic under the
        single-threaded event loop because there are no other awaits between the
        candidate-selection here and the new-session insert below — the only `await`
        here is `engine_session.close()`, which yields the loop, but on resume we
        re-check capacity in `create()` before inserting.
        """
        rec.closing = True
        with suppress(Exception):
            await rec.engine_session.close()
        self._records.pop(rec.session_id, None)
        # No tombstone — evicted sessions and explicit DELETEs both return generic
        # `Unknown session` 404 (the caller did not ask for them to expire).
        self.sessions_lru_evicted_total += 1
        logger.info(
            "evicted-idle: sid=%s idle=%ss (cap=%d) to admit new session",
            short(rec.session_id),
            int(self._clock() - rec.last_used_monotonic),
            self.max_sessions,
        )

    async def create(
        self, opts: SessionOpts, *, idle_timeout_seconds: int | None
    ) -> CreateResult:
        # The cap check + insert path has no `await` once we have an engine_session, so
        # it is atomic under the single-threaded event loop. The pre-check fails fast OR
        # evicts the oldest idle to free a slot; the post-check (after the engine await)
        # catches a concurrent create that filled the cap during `new_session`.
        evicted_id: str | None = None
        if len(self._records) >= self.max_sessions:
            victim = self._oldest_evictable_idle()
            if victim is None:
                self.sessions_rejected_total += 1
                raise SessionCapacityError(self.max_sessions)
            evicted_id = victim.session_id
            await self._evict_one(victim)

        engine_session = await self._engine.new_session(opts)
        if len(self._records) >= self.max_sessions:
            # Concurrent create filled the cap during our await. Try one more LRU pass
            # — if a slot can still be freed without disturbing an active session, take
            # it; otherwise 503 back-pressure.
            victim = self._oldest_evictable_idle()
            if victim is None:
                with suppress(Exception):
                    await engine_session.close()
                self.sessions_rejected_total += 1
                raise SessionCapacityError(self.max_sessions)
            evicted_id = victim.session_id
            await self._evict_one(victim)

        sid = uuid.uuid4().hex
        while sid in self._records or sid in self._tombstones:  # documentation-grade
            sid = uuid.uuid4().hex
        now_wall = time.time()
        rec = SessionRecord(
            session_id=sid,
            created_at=now_wall,
            last_used_at=now_wall,
            last_used_monotonic=self._clock(),
            viewport=(opts.viewport_width, opts.viewport_height),
            idle_timeout=self._effective_idle_timeout(idle_timeout_seconds),
            headless=opts.headless,
            engine_session=engine_session,
        )
        self._records[sid] = rec
        self.sessions_created_total += 1
        return CreateResult(record=rec, evicted_session_id=evicted_id)

    def get(self, session_id: str) -> SessionRecord:
        rec = self._records.get(session_id)
        if rec is not None and not rec.closing:
            return rec
        if session_id in self._tombstones:
            raise SessionGone(session_id, expired=True, idle_timeout=self._tombstones[session_id])
        raise SessionGone(session_id, expired=False)

    def list(self) -> list[SessionRecord]:
        return [rec for rec in self._records.values() if not rec.closing]

    def touch(self, rec: SessionRecord) -> None:
        rec.last_used_at = time.time()
        rec.last_used_monotonic = self._clock()

    async def delete(self, session_id: str, *, lock_acquire_timeout_s: float) -> None:
        rec = self.get(session_id)  # raises SessionGone -> 404
        try:
            await asyncio.wait_for(rec.lock.acquire(), timeout=lock_acquire_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            raise SessionBusy()
        try:
            rec.closing = True
            with suppress(Exception):
                await rec.engine_session.close()
        finally:
            self._records.pop(session_id, None)
            self.sessions_deleted_total += 1
            rec.lock.release()

    async def reap_pass(self) -> None:
        if not self._engine.is_alive():
            for sid in list(self._records):
                rec = self._records.pop(sid, None)
                if rec is not None:
                    with suppress(Exception):
                        await rec.engine_session.close()
            return
        now = self._clock()
        for sid, rec in list(self._records.items()):
            try:
                if rec.closing:
                    continue
                if rec.lock.locked():
                    continue
                if (now - rec.last_used_monotonic) <= rec.idle_timeout:
                    continue
                rec.closing = True
                await rec.lock.acquire()
                with suppress(Exception):
                    await rec.engine_session.close()
                self._records.pop(sid, None)
                self._tombstone_add(sid, rec.idle_timeout)
                self.sessions_reaped_total += 1
                logger.info(
                    "reaper: reaped sid=%s idle=%ss timeout=%ss",
                    short(sid),
                    int(now - rec.last_used_monotonic),
                    rec.idle_timeout,
                )
            except Exception:  # noqa: BLE001 - one bad session must not abort the pass
                logger.exception("reaper: failed to reap %s", short(sid))
                continue

    async def close_all(self) -> None:
        for rec in list(self._records.values()):
            with suppress(Exception):
                await rec.engine_session.close()
        self._records.clear()


async def reaper_loop(manager: SessionManager) -> None:
    while True:
        try:
            await manager.reap_pass()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad tick must not kill the reaper
            logger.exception("reaper tick failed")
        await asyncio.sleep(manager.reaper_interval)
