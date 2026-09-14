"""Unit tests for the per-session-browser crash signal + failed-create cleanup.

`browser_restarts_total()` is an oncall crash/OOM signal: an UNEXPECTED per-session
browser 'disconnected' must increment it, while an intentional close (healthy session
teardown / failed create) must NOT (review I-1). And a browser launched by new_session
that never reaches the returned session must be reaped — on a PlaywrightError AND on any
other exception (CancelledError/MemoryError) — or it leaks the very memory the
per-session model exists to bound (review I-A). These are deterministic, no real Chromium.
"""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from api.engine.base import SessionOpts
from api.engine.errors import EngineCrash
from api.engine.playwright_engine import PlaywrightEngine


class _FakeBrowser:
    """A weak-referenceable Browser double that fires 'disconnected' on close(),
    exactly as real Chromium does — so the intentional-close accounting is exercised
    end-to-end. Optionally fails new_context to drive the failed-create path."""

    def __init__(self, *, fail_new_context: BaseException | None = None) -> None:
        self._disconnect_cb = None
        self.closed = False
        self._fail_new_context = fail_new_context

    def on(self, event: str, cb) -> None:  # noqa: ANN001
        if event == "disconnected":
            self._disconnect_cb = cb

    async def new_context(self, **_kwargs):
        if self._fail_new_context is not None:
            raise self._fail_new_context
        raise AssertionError("unexpected: this fake is only used for the failed-create path")

    async def close(self) -> None:
        self.closed = True
        if self._disconnect_cb is not None:  # real chromium fires 'disconnected' on close
            self._disconnect_cb(self)


class _FakePw:
    """Stands in for a started Playwright: pw.chromium.launch(...) -> the fake browser."""

    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = self
        self._browser = browser

    async def launch(self, **_kwargs) -> _FakeBrowser:
        return self._browser


# ---- browser_restarts_total direction (I-B) ----

def test_on_disconnected_counts_unexpected_crash():
    """The crash direction: a 'disconnected' for a browser we did NOT close on purpose
    increments the signal. (The existing browser-integration test only guards that a
    HEALTHY close leaves it at 0 — this pins the other half of the contract.)"""
    engine = PlaywrightEngine()
    browser = _FakeBrowser()
    assert engine.browser_restarts_total() == 0
    engine._on_disconnected(browser)  # never marked intentional → a crash/OOM
    assert engine.browser_restarts_total() == 1


def test_on_disconnected_skips_intentional_then_counts_a_later_crash():
    """A marked (intentional) close is not counted, the mark is consumed, and a
    subsequent UNEXPECTED disconnect of the same identity still counts — proving the
    WeakSet can't permanently mask a real crash."""
    engine = PlaywrightEngine()
    browser = _FakeBrowser()
    engine._note_intentional_close(browser)
    engine._on_disconnected(browser)  # marked → skipped, mark discarded
    assert engine.browser_restarts_total() == 0
    engine._on_disconnected(browser)  # no longer marked → counts
    assert engine.browser_restarts_total() == 1


# ---- failed-create browser reap (I-C + the I-A non-PlaywrightError gap) ----

@pytest.mark.asyncio
async def test_failed_create_playwright_error_reaps_browser_without_counting():
    """new_context raising a PlaywrightError → EngineCrash, the launched browser is
    closed (no process leak), and the intentional close is not counted as a crash."""
    engine = PlaywrightEngine()
    browser = _FakeBrowser(fail_new_context=PlaywrightError("context boom"))
    engine._pw = _FakePw(browser)  # skip real _ensure_pw / chromium launch
    with pytest.raises(EngineCrash):
        await engine.new_session(SessionOpts())
    assert browser.closed is True
    assert engine.browser_restarts_total() == 0


@pytest.mark.asyncio
async def test_failed_create_non_playwright_error_still_reaps_browser():
    """Review I-A: a NON-PlaywrightError between launch() and return (CancelledError
    from a client disconnect / shutdown, or MemoryError under OOM) must still reap the
    launched chromium PROCESS — the old code only cleaned up on PlaywrightError, leaking
    one process per occurrence. The original exception propagates unchanged (not remapped
    to EngineCrash), and the reap is not counted as a crash."""
    engine = PlaywrightEngine()
    browser = _FakeBrowser(fail_new_context=asyncio.CancelledError())
    engine._pw = _FakePw(browser)
    with pytest.raises(asyncio.CancelledError):
        await engine.new_session(SessionOpts())
    assert browser.closed is True
    assert engine.browser_restarts_total() == 0
