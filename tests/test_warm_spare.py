"""Unit tests for the warm spare Chromium (specs/warm-spare-chromium.md).

A create takes one pre-launched, warmed browser instead of paying a cold launch; the
spare is never returned or reused, so per-session isolation is unchanged. These are
deterministic and never launch a real Chromium.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable

import pytest
from playwright.async_api import Error as PlaywrightError

from api.config import settings
from api.engine.base import SessionOpts
from api.engine.playwright_engine import PlaywrightEngine
from api.session_manager import SessionManager, reaper_loop
from tests.fakes import FakeEngine


class _FakePage:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on(self, event: str, cb: object) -> None:
        self.events.append(event)


class _FakeContext:
    def __init__(self, browser: "_FakeBrowser") -> None:
        self._browser = browser
        self.closed = False

    async def new_page(self) -> _FakePage:
        self._browser.pages_opened += 1
        return _FakePage()

    async def route(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
        if self in self._browser.contexts:
            self._browser.contexts.remove(self)


class _FakeBrowser:
    """A Browser double recording closes, connectivity, contexts and disconnect callbacks."""

    def __init__(
        self,
        *,
        connected: bool = True,
        fail_new_context: BaseException | None = None,
        hold_context: asyncio.Event | None = None,
        context_started: asyncio.Event | None = None,
    ) -> None:
        self._disconnect_cbs: list[Callable[["_FakeBrowser"], None]] = []
        self.closed = False
        self._connected = connected
        self.contexts: list[_FakeContext] = []
        self.contexts_created = 0
        self.pages_opened = 0
        self.fail_new_context = fail_new_context
        self.hold_context = hold_context
        self.context_started = context_started
        self.on_close: Callable[[], None] | None = None

    def on(self, event: str, cb: Callable[["_FakeBrowser"], None]) -> None:
        if event == "disconnected":
            self._disconnect_cbs.append(cb)

    def is_connected(self) -> bool:
        return self._connected and not self.closed

    async def new_context(self, **_kwargs: object) -> _FakeContext:
        if self.context_started is not None:
            self.context_started.set()
        if self.hold_context is not None:
            await self.hold_context.wait()
        if self.fail_new_context is not None:
            raise self.fail_new_context
        context = _FakeContext(self)
        self.contexts.append(context)
        self.contexts_created += 1
        return context

    async def close(self) -> None:
        if self.on_close is not None:
            self.on_close()
        self.closed = True
        self._connected = False
        callbacks, self._disconnect_cbs = self._disconnect_cbs, []
        for cb in callbacks:
            cb(self)


class _FakePw:
    """Stands in for a started Playwright: pw.chromium.launch(...) -> a fake browser."""

    def __init__(self, factory: Callable[[], _FakeBrowser] | None = None) -> None:
        self.chromium = self
        self.factory: Callable[[], _FakeBrowser] = factory or _FakeBrowser
        self.launched: list[_FakeBrowser] = []
        self.launch_count = 0
        self.stopped = False

    async def launch(self, **_kwargs: object) -> _FakeBrowser:
        self.launch_count += 1
        browser = self.factory()
        self.launched.append(browser)
        return browser

    async def stop(self) -> None:
        self.stopped = True


def _engine_with(pw: _FakePw) -> PlaywrightEngine:
    engine = PlaywrightEngine()
    engine._pw = pw  # skip the real _ensure_pw / async_playwright().start()
    return engine


@pytest.fixture
def spare_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "browser_spare", True)
    monkeypatch.setattr(settings, "browser_spare_max_age_seconds", 300)


# ---- taking the spare ----------------------------------------------------------


async def test_create_takes_ready_spare_with_no_launch(spare_on: None) -> None:
    """Test 1: a ready spare is taken with no launch; a refill then launches once."""
    pw = _FakePw()
    engine = _engine_with(pw)
    spare = _FakeBrowser()
    spare.on("disconnected", engine._on_disconnected)
    engine._spare = spare
    engine._spare_born = time.monotonic()

    session = await engine.new_session(SessionOpts())

    assert pw.launch_count == 0
    assert session._browser is spare
    assert engine._spare is None
    assert engine._refill_task is not None
    await engine._refill_task
    assert pw.launch_count == 1
    assert engine._spare is pw.launched[0]
    assert engine.spare_ready() is True


async def test_create_without_spare_launches_inline(spare_on: None) -> None:
    """Test 2: no spare -> the create launches inline once, as today."""
    pw = _FakePw()
    engine = _engine_with(pw)

    session = await engine.new_session(SessionOpts())

    assert pw.launch_count == 1
    assert session._browser is pw.launched[0]
    assert engine._spare is None  # the refill has not run yet
    assert engine._refill_task is not None
    await engine._refill_task
    assert engine._spare is pw.launched[1]


async def test_two_concurrent_creates_share_one_spare(spare_on: None) -> None:
    """Test 3: one spare, two concurrent creates -> one takes it, one launches, one refill."""
    pw = _FakePw()
    engine = _engine_with(pw)
    gate = asyncio.Event()
    spare = _FakeBrowser(hold_context=gate)
    engine._spare = spare
    engine._spare_born = time.monotonic()

    first = asyncio.create_task(engine.new_session(SessionOpts()))
    await asyncio.sleep(0)  # first takes the spare, then blocks in new_context
    second = await engine.new_session(SessionOpts())  # spare is gone -> launches inline
    gate.set()
    first_session = await first

    assert first_session._browser is spare
    assert second._browser is not spare
    await engine._refill_task
    # one inline launch + one refill launch; a second refill would make it three
    assert pw.launch_count == 2
    assert engine._spare is pw.launched[1]


async def test_disconnected_spare_is_closed_and_skipped(spare_on: None) -> None:
    """Test 4: a dead spare is closed, not handed out; the create launches inline."""
    pw = _FakePw()
    engine = _engine_with(pw)
    dead = _FakeBrowser(connected=False)
    dead.on("disconnected", engine._on_disconnected)
    engine._spare = dead
    engine._spare_born = time.monotonic()

    session = await engine.new_session(SessionOpts())

    assert dead.closed is True
    assert pw.launch_count == 1
    assert session._browser is pw.launched[0]
    assert engine.browser_restarts_total() == 0
    await engine._refill_task


# ---- refill failures -----------------------------------------------------------


async def test_refill_warm_up_failure_is_logged_and_closed(
    spare_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 5: a failed warm-up leaves no spare, raises nowhere and is not a crash."""
    pw = _FakePw(lambda: _FakeBrowser(fail_new_context=PlaywrightError("warm boom")))
    engine = _engine_with(pw)

    with caplog.at_level(logging.WARNING):
        await engine.warm()
        first_task = engine._refill_task
        assert first_task is not None
        await first_task

    assert engine._spare is None
    assert pw.launch_count == 1
    assert pw.launched[0].closed is True
    # Nit 1: the launched browser is marked intentional BEFORE it is closed.
    assert engine.browser_restarts_total() == 0
    assert "spare chromium launch failed" in caplog.text

    # The next warm() schedules a new refill, and this one succeeds.
    pw.factory = _FakeBrowser
    await engine.warm()
    assert engine._refill_task is not None
    assert engine._refill_task is not first_task
    await engine._refill_task
    assert engine._spare is not None


async def test_refill_launch_failure_is_logged(
    spare_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 5 (launch half): a launch exception is logged and leaves _spare None."""

    def _boom() -> _FakeBrowser:
        raise PlaywrightError("launch boom")

    pw = _FakePw(_boom)
    engine = _engine_with(pw)

    with caplog.at_level(logging.WARNING):
        await engine.warm()
        assert engine._refill_task is not None
        await engine._refill_task

    assert engine._spare is None
    assert engine.browser_restarts_total() == 0
    assert "spare chromium launch failed" in caplog.text


# ---- recycling -----------------------------------------------------------------


async def test_warm_recycles_an_old_spare(spare_on: None) -> None:
    """Test 6: an old spare is replaced and closed as intentional, not as a crash."""
    new_browser = _FakeBrowser()
    pw = _FakePw(lambda: new_browser)
    engine = _engine_with(pw)
    old = _FakeBrowser()
    old.on("disconnected", engine._on_disconnected)
    engine._spare = old
    engine._spare_born = time.monotonic() - settings.browser_spare_max_age_seconds - 10
    seen: dict[str, object] = {}
    old.on_close = lambda: seen.update(spare=engine._spare)

    await engine.warm()
    assert engine._refill_task is not None
    await engine._refill_task

    assert engine._spare is new_browser
    assert old.closed is True
    assert seen["spare"] is new_browser  # stored before the old one is closed
    assert engine.browser_restarts_total() == 0


async def test_stored_spare_has_been_warmed(spare_on: None) -> None:
    """Test 7: the refill opens a context and a blank page, then closes the context."""
    browser = _FakeBrowser()
    pw = _FakePw(lambda: browser)
    engine = _engine_with(pw)

    await engine.warm()
    assert engine._refill_task is not None
    await engine._refill_task

    assert engine._spare is browser
    assert browser.contexts_created == 1
    assert browser.pages_opened == 1
    assert browser.contexts == []  # no cookie or storage left behind


async def test_close_during_refill_leaves_no_unclosed_browser(spare_on: None) -> None:
    """Test 8: shutdown mid-refill cancels the task and closes the launched browser."""
    hold = asyncio.Event()
    started = asyncio.Event()
    browser = _FakeBrowser(hold_context=hold, context_started=started)
    pw = _FakePw(lambda: browser)
    engine = _engine_with(pw)

    await engine.warm()
    task = engine._refill_task
    assert task is not None
    await started.wait()

    await engine.close()

    assert task.cancelled() is True
    assert browser.closed is True
    assert pw.stopped is True


async def test_spare_disabled_never_creates_a_spare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 9: BROWSER_SPARE=false restores today's behaviour exactly."""
    monkeypatch.setattr(settings, "browser_spare", False)
    pw = _FakePw()
    engine = _engine_with(pw)

    await engine.warm()
    assert engine._refill_task is None

    session = await engine.new_session(SessionOpts())

    assert pw.launch_count == 1
    assert engine._spare is None
    assert engine._refill_task is None
    await session.close()


# ---- nit 2: a taken spare that fails before ownership --------------------------


async def test_taken_spare_failure_falls_back_inline_once(spare_on: None) -> None:
    """A spare whose new_context fails is reaped, then the create launches inline once."""
    pw = _FakePw()
    engine = _engine_with(pw)
    spare = _FakeBrowser(fail_new_context=PlaywrightError("spare boom"))
    spare.on("disconnected", engine._on_disconnected)
    engine._spare = spare
    engine._spare_born = time.monotonic()

    session = await engine.new_session(SessionOpts())

    assert spare.closed is True
    assert engine.browser_restarts_total() == 0
    assert pw.launch_count == 1
    assert session._browser is pw.launched[0]
    await engine._refill_task
    assert engine._spare is pw.launched[1]


# ---- reaper wiring -------------------------------------------------------------


class _WarmTrackingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.warm_calls = 0

    async def warm(self) -> None:
        self.warm_calls += 1


async def test_reaper_warms_even_when_reap_pass_raises() -> None:
    """Test 11: a tick whose reap_pass() raises still calls engine_warm()."""
    engine = _WarmTrackingEngine()
    manager = SessionManager(
        engine,
        max_sessions=settings.max_sessions,
        idle_timeout_default=settings.idle_timeout_seconds,
        idle_timeout_min=30,
        idle_timeout_max=settings.idle_timeout_max_seconds,
        reaper_interval=0,
        tombstone_max=settings.tombstone_max,
    )

    async def _boom() -> None:
        raise RuntimeError("reap boom")

    manager.reap_pass = _boom  # type: ignore[method-assign]

    task = asyncio.create_task(reaper_loop(manager))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if engine.warm_calls:
                break
        assert engine.warm_calls >= 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
