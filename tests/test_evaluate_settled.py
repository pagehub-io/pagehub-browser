"""Unit tests for the navigation-race retry in localStorage evaluate
(PlaywrightEngineSession._evaluate_settled). An SPA client-side redirect can destroy
the JS execution context mid-evaluate (classic failure when seeding an auth token on
the login page); the set must settle + retry ONCE on that specific transient."""
from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

from api.engine.errors import EngineError
from api.engine.playwright_engine import PlaywrightEngineSession


class _FakePage:
    """Minimal Page double: `evaluate` raises the context-destroyed transient a fixed
    number of times, then returns; records load-state waits."""

    def __init__(self, fail_times: int, error_msg: str) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.error_msg = error_msg
        self.load_waits = 0

    def on(self, *_a, **_k) -> None:  # session __init__ registers listeners
        pass

    async def evaluate(self, _expr, *_args):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PlaywrightError(self.error_msg)
        return "ok"

    async def wait_for_load_state(self, _state, timeout=None) -> None:
        self.load_waits += 1


def _session(page: _FakePage) -> PlaywrightEngineSession:
    return PlaywrightEngineSession(engine=None, browser=None, context=None, page=page)


@pytest.mark.asyncio
async def test_localstorage_set_retries_once_on_context_destroyed():
    page = _FakePage(fail_times=1, error_msg="Execution context was destroyed, most likely because of a navigation.")
    session = _session(page)
    msg = await session.local_storage("set", "serve_access_token", "tok")
    assert "Set localStorage" in msg
    assert page.calls == 2 and page.load_waits == 1  # failed once, settled, retried, succeeded


@pytest.mark.asyncio
async def test_localstorage_set_does_not_retry_other_errors():
    page = _FakePage(fail_times=1, error_msg="some other playwright error")
    session = _session(page)
    with pytest.raises(EngineError):
        await session.local_storage("set", "k", "v")
    assert page.calls == 1 and page.load_waits == 0  # no settle, no retry — propagated


@pytest.mark.asyncio
async def test_localstorage_set_gives_up_after_one_retry():
    page = _FakePage(fail_times=2, error_msg="Execution context was destroyed, most likely because of a navigation.")
    session = _session(page)
    with pytest.raises(EngineError):
        await session.local_storage("set", "k", "v")
    assert page.calls == 2  # one retry only, then propagate


@pytest.mark.asyncio
async def test_localstorage_get_all_uses_no_arg_path_and_retries():
    """The no-arg (_UNSET) evaluate path (get-all) also settles + retries once."""
    page = _FakePage(fail_times=1, error_msg="Execution context was destroyed, most likely because of a navigation.")
    session = _session(page)
    await session.local_storage("get", None, None)  # get-all → no-arg evaluate
    assert page.calls == 2 and page.load_waits == 1
