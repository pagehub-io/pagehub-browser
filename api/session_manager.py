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

# At shutdown, close_all() waits at most this long for DELETE's background closes before
# cancelling the stragglers. Leaves room inside docker's default 10 s stop grace period
# for the live records and engine.close().
SHUTDOWN_PENDING_CLOSE_WAIT_S = 5.0

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
    """POST /v1/sessions at MAX_SESSIONS.

    `held_by_pending_close` is True iff the cap was still reached, after create() waited,
    only because a DELETE's background close had not finished. That slot frees in
    seconds, so the route answers it with a short Retry-After.
    """

    def __init__(self, max_sessions: int, *, held_by_pending_close: bool = False) -> None:
        self.max_sessions = max_sessions
        self.held_by_pending_close = held_by_pending_close
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
        # Ids an explicit DELETE removed (bounded by tombstone_max, oldest evicted first): a
        # repeat DELETE of one of them returns 204, so a client's retry is safe.
        self._deleted: "OrderedDict[str, None]" = OrderedDict()
        # DELETE's background closes. A pending close is still a running browser process,
        # so it counts toward max_sessions until its task is done.
        self._pending_closes: set[asyncio.Task[None]] = set()

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

    def metrics_snapshot(self) -> dict[str, int | bool]:
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
            "spare_ready": self._engine.spare_ready(),
            "closes_pending": self._pending_count(),
        }

    async def engine_warm(self) -> None:
        await self._engine.warm()

    # ---- tombstones --------------------------------------------------------------

    def _tombstone_add(self, session_id: str, idle_timeout: int) -> None:
        self._tombstones[session_id] = idle_timeout
        self._tombstones.move_to_end(session_id)
        while len(self._tombstones) > self.tombstone_max:
            self._tombstones.popitem(last=False)

    def _deleted_add(self, session_id: str) -> None:
        self._deleted[session_id] = None
        self._deleted.move_to_end(session_id)
        while len(self._deleted) > self.tombstone_max:
            self._deleted.popitem(last=False)

    # ---- background closes -------------------------------------------------------

    def _pending_count(self) -> int:
        # `not task.done()`, so the count never depends on when the done-callback runs.
        return sum(1 for task in self._pending_closes if not task.done())

    def _cap_reached(self) -> bool:
        return len(self._records) + self._pending_count() >= self.max_sessions

    def _close_later(self, engine_session: EngineSession, session_id: str) -> None:
        """Close a deleted session's engine session in a task on the loop.

        The request doesn't await the task, so a client disconnect can't cancel it; only
        close_all() at shutdown does. The set holds a strong reference until it is done.
        """
        task = asyncio.get_running_loop().create_task(
            self._close_quietly(engine_session, session_id)
        )
        self._pending_closes.add(task)
        task.add_done_callback(self._pending_closes.discard)

    async def _close_quietly(self, engine_session: EngineSession, session_id: str) -> None:
        try:
            await engine_session.close()
        except Exception:  # noqa: BLE001 - the record is already gone; log and end
            # CancelledError isn't an Exception, so a cancel at shutdown still ends the task.
            logger.warning("delete: close failed sid=%s", short(session_id), exc_info=True)

    async def _make_room(self, deadline: float) -> bool:
        """While the cap is reached and closes are pending, wait for one to finish.

        Returns True iff the cap is still reached at the deadline with a close still
        pending: the slot is held by a close, not a live session, so create() returns 503
        and evicts nothing. Returns False once the cap isn't reached or no close is pending.
        """
        loop = asyncio.get_running_loop()
        while self._cap_reached():
            pending = {task for task in self._pending_closes if not task.done()}
            if not pending:
                return False
            left = deadline - loop.time()
            if left <= 0:
                return True
            await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
        return False

    async def _claim_slot(self, deadline: float, fresh: EngineSession | None) -> str | None:
        """One of create()'s capacity checks. Returns the evicted id, or None when nothing
        was evicted; raises SessionCapacityError (closing `fresh` first, if given)."""
        if not self._cap_reached():
            return None
        held_by_close = await self._make_room(deadline)
        if not self._cap_reached():
            return None
        # A pending close is never a reason to evict a live session.
        victim = None if held_by_close else self._oldest_evictable_idle()
        if victim is None:
            if fresh is not None:
                with suppress(Exception):
                    await fresh.close()
            self.sessions_rejected_total += 1
            raise SessionCapacityError(self.max_sessions, held_by_pending_close=held_by_close)
        await self._evict_one(victim)
        return victim.session_id

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
        self,
        opts: SessionOpts,
        *,
        idle_timeout_seconds: int | None,
        pending_close_wait_s: float = 0.0,
    ) -> CreateResult:
        # The cap counts live records plus DELETE's pending closes. The cap check + insert
        # path has no `await` once a slot is claimed, so it is atomic under the
        # single-threaded event loop. The pre-check waits (up to `pending_close_wait_s`,
        # one deadline for both checks) for a pending close, then fails fast OR evicts the
        # oldest idle; the post-check (after the engine await) catches a concurrent create
        # that filled the cap during `new_session`.
        deadline = asyncio.get_running_loop().time() + pending_close_wait_s
        evicted_id = await self._claim_slot(deadline, None)

        engine_session = await self._engine.new_session(opts)
        evicted_late = await self._claim_slot(deadline, engine_session)
        if evicted_late is not None:
            evicted_id = evicted_late

        sid = uuid.uuid4().hex
        while sid in self._records or sid in self._tombstones or sid in self._deleted:
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

    def still_live(self, rec: SessionRecord) -> bool:
        """Every path that acquires `rec.lock` calls this right after acquiring: a DELETE
        or an eviction may have closed the session while the caller waited on the lock."""
        return not rec.closing and self._records.get(rec.session_id) is rec

    async def delete(self, session_id: str, *, lock_acquire_timeout_s: float) -> None:
        """Make the session unreachable and return; its engine session closes in the
        background. Idempotent for ids this service deleted (the route returns 204)."""
        if session_id in self._deleted:
            return
        rec = self.get(session_id)  # raises SessionGone -> 404
        try:
            await asyncio.wait_for(rec.lock.acquire(), timeout=lock_acquire_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            raise SessionBusy()
        # Nothing from here to the end awaits, so a client disconnect can't interrupt it.
        try:
            if not self.still_live(rec):
                # A first DELETE of this id, an eviction, or the reaper (when this DELETE
                # was woken just ahead of its acquire) closed it while we waited: 204, no
                # second close, no counter change, and the id isn't added to _deleted.
                return
            rec.closing = True
            self._records.pop(session_id, None)
            self._deleted_add(session_id)
            self.sessions_deleted_total += 1
        finally:
            rec.lock.release()
        self._close_later(rec.engine_session, session_id)

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

    async def close_all(
        self, *, pending_close_wait_s: float = SHUTDOWN_PENDING_CLOSE_WAIT_S
    ) -> None:
        pending = set(self._pending_closes)
        # asyncio.wait raises ValueError on an empty set, and lifespan suppresses that, so
        # an unguarded call would skip the live records on every ordinary shutdown.
        if pending:
            _, stragglers = await asyncio.wait(pending, timeout=pending_close_wait_s)
            for task in stragglers:
                task.cancel()
            if stragglers:
                # None is still running when Playwright stops under it.
                await asyncio.gather(*stragglers, return_exceptions=True)
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
        # Refill and recycle the spare in its own try: a failed reap pass still warms.
        try:
            await manager.engine_warm()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad warm must not kill the reaper
            logger.exception("spare warm tick failed")
        await asyncio.sleep(manager.reaper_interval)
