"""Unit tests that the localStorage code path ROUTES THROUGH the navigation-race
retry (PlaywrightEngineSession._evaluate_settled).

`_evaluate_settled` itself is unit-tested directly in test_engine_locator_unit.py
(retry count, give-up, non-race passthrough, arg passthrough). What THESE tests pin
is the integration the nav-race fix exists for: seeding an auth token into
localStorage right after navigating raced the SPA's client-side redirect, destroying
the JS execution context mid-evaluate → "Execution context was destroyed" → 422 →
token never set → downstream waits time out. So `local_storage` set/get MUST go
through the settle-retry, and only for that specific transient.
"""
from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

from api.engine.errors import EngineError
from api.engine.playwright_engine import PlaywrightEngineSession

# #4's _evaluate_settled(retries=3) → 4 total attempts before it gives up.
_RETRIES = 3
_ATTEMPTS = _RETRIES + 1
_CTX_DESTROYED = "Execution context was destroyed, most likely because of a navigation."


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """_evaluate_settled backs off with asyncio.sleep between retries; make it a
    no-op so these unit tests are fast and deterministic (no real wall-clock waits)."""
    async def _instant(_delay):  # noqa: ANN001
        return None

    monkeypatch.setattr("api.engine.playwright_engine.asyncio.sleep", _instant)


class _FakePage:
    """Minimal Page double: `evaluate` raises the context-destroyed transient a fixed
    number of times, then returns; records how many times it was called."""

    def __init__(self, fail_times: int, error_msg: str) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.error_msg = error_msg

    def on(self, *_a, **_k) -> None:  # session __init__ registers console/request listeners
        pass

    async def evaluate(self, _expr, *_args):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PlaywrightError(self.error_msg)
        return "ok"


def _session(page: _FakePage) -> PlaywrightEngineSession:
    return PlaywrightEngineSession(engine=None, browser=None, context=None, page=page)


@pytest.mark.asyncio
async def test_localstorage_set_retries_on_context_destroyed():
    """The set (arg) path settles + retries through the nav-race, then succeeds."""
    page = _FakePage(fail_times=1, error_msg=_CTX_DESTROYED)
    msg = await _session(page).local_storage("set", "serve_access_token", "tok")
    assert "Set localStorage" in msg
    assert page.calls == 2  # failed once mid-hydration, retried into the settled context


@pytest.mark.asyncio
async def test_localstorage_set_does_not_retry_other_errors():
    """A non-race Playwright error propagates on the first attempt — not masked/delayed."""
    page = _FakePage(fail_times=1, error_msg="some other playwright error")
    with pytest.raises(EngineError):
        await _session(page).local_storage("set", "k", "v")
    assert page.calls == 1  # no retry — surfaced immediately


@pytest.mark.asyncio
async def test_localstorage_set_gives_up_after_retries():
    """When every attempt races, the transient surfaces (as an EngineError) rather
    than looping forever — exactly _RETRIES+1 attempts, then propagate."""
    page = _FakePage(fail_times=_ATTEMPTS, error_msg=_CTX_DESTROYED)
    with pytest.raises(EngineError):
        await _session(page).local_storage("set", "k", "v")
    assert page.calls == _ATTEMPTS


@pytest.mark.asyncio
async def test_localstorage_get_all_uses_no_arg_path_and_retries():
    """The get-all (no-arg / _NO_ARG) evaluate path also settles + retries."""
    page = _FakePage(fail_times=1, error_msg=_CTX_DESTROYED)
    await _session(page).local_storage("get", None, None)  # get-all → no-arg evaluate
    assert page.calls == 2
