"""Unit cases for locator resolution and the navigation interceptor: no browser,
a stubbed Playwright locator / route and real Playwright error instances.
"""

from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from api.engine.base import Locator, LocatorOptions
from api.engine.errors import (
    ActionTimeout,
    ElementNotFound,
    EngineCrash,
    InvalidLocatorSyntax,
    LocatorAmbiguous,
    RuntimeEvalError,
)
from api.engine.playwright_engine import (
    PlaywrightEngineSession,
    _classify_playwright_error,
    _locator_error,
)

STRICT_MSG = (
    'Locator.wait_for: Error: strict mode violation: get_by_test_id("dup") resolved to 2 elements:\n'
    '    1) <button data-testid="dup">a</button> aka get_by_role("button", name="a")\n'
    '    2) <button data-testid="dup">b</button> aka get_by_role("button", name="b")\n'
)


class FakePwLoc:
    def __init__(self, count: int, wait_exc: Exception | None = None, wait_delay: float = 0.0) -> None:
        self._count = count
        self._wait_exc = wait_exc
        self._wait_delay = wait_delay
        self.wait_calls: list[tuple[str, int]] = []
        self.count_calls = 0

    @property
    def first(self):  # noqa: ANN201
        return self

    async def wait_for(self, state: str, timeout: int) -> None:
        self.wait_calls.append((state, timeout))
        await asyncio.sleep(self._wait_delay)
        if self._wait_exc is not None:
            raise self._wait_exc

    async def count(self) -> int:
        self.count_calls += 1
        return self._count


def _session(fake: FakePwLoc, monkeypatch) -> PlaywrightEngineSession:
    s = object.__new__(PlaywrightEngineSession)
    monkeypatch.setattr(s, "_resolve", lambda loc: fake, raising=False)
    return s


def _loc(strategy="testid", value="x", nth=None) -> Locator:
    return Locator(strategy=strategy, value=value, options=LocatorOptions(nth=nth) if nth is not None else None)


# ---- _classify_playwright_error ----

def test_strict_mode_parses_count_across_lines():
    err = _classify_playwright_error(PlaywrightError(STRICT_MSG))
    assert isinstance(err, LocatorAmbiguous) and err.count == 2
    assert "matched 2 elements; pass options.nth" in str(err)


def test_strict_mode_without_count_falls_through_to_422():
    err = _classify_playwright_error(PlaywrightError("strict mode violation: something odd"))
    assert isinstance(err, RuntimeEvalError)


def test_dead_target_wins_over_strict():
    err = _classify_playwright_error(PlaywrightError("Target closed; strict mode violation resolved to 2 elements"))
    assert isinstance(err, EngineCrash)


# ---- _locator_error ----

def test_locator_error_css_generic_is_400(monkeypatch):
    err = _locator_error(PlaywrightError('Unexpected token "<" while parsing css selector "<<<"'), _loc("css", "<<<"))
    assert isinstance(err, InvalidLocatorSyntax) and "Invalid css selector syntax" in str(err)


def test_locator_error_css_dead_target_is_crash(monkeypatch):
    err = _locator_error(PlaywrightError("Target page, context or browser has been closed"), _loc("css", "a"))
    assert isinstance(err, EngineCrash)


def test_locator_error_role_strict_is_409(monkeypatch):
    err = _locator_error(PlaywrightError(STRICT_MSG), _loc("role", "button"))
    assert isinstance(err, LocatorAmbiguous) and err.count == 2


# ---- _resolve_single timeout arms ----

async def test_attached_timeout_is_404(monkeypatch):
    fake = FakePwLoc(0, wait_exc=PlaywrightTimeoutError("Timeout 500ms exceeded."))
    s = _session(fake, monkeypatch)
    with pytest.raises(ElementNotFound, match="no element matched testid=x"):
        await s._resolve_single(_loc(), 500)
    assert fake.wait_calls == [("attached", 500)]
    assert fake.count_calls == 0


async def test_visible_timeout_zero_is_404(monkeypatch):
    s = _session(FakePwLoc(0, wait_exc=PlaywrightTimeoutError("t")), monkeypatch)
    with pytest.raises(ElementNotFound):
        await s._resolve_single(_loc(), 500, state="visible")


async def test_visible_timeout_two_no_nth_is_ambiguous(monkeypatch):
    s = _session(FakePwLoc(2, wait_exc=PlaywrightTimeoutError("t")), monkeypatch)
    with pytest.raises(LocatorAmbiguous) as ei:
        await s._resolve_single(_loc(), 500, state="visible")
    assert ei.value.count == 2


async def test_visible_timeout_one_is_row_157(monkeypatch):
    s = _session(FakePwLoc(1, wait_exc=PlaywrightTimeoutError("t")), monkeypatch)
    with pytest.raises(ActionTimeout) as ei:
        await s._resolve_single(_loc(), 500, state="visible")
    assert str(ei.value) == "Timed out after 500ms waiting for testid=x to be visible."


async def test_visible_timeout_with_nth_is_row_157_even_if_count_gt_1(monkeypatch):
    s = _session(FakePwLoc(2, wait_exc=PlaywrightTimeoutError("t")), monkeypatch)
    with pytest.raises(ActionTimeout):
        await s._resolve_single(_loc(nth=1), 500, state="visible")


async def test_generic_error_css_is_400(monkeypatch):
    s = _session(FakePwLoc(0, wait_exc=PlaywrightError('Unexpected token "<" while parsing css selector')), monkeypatch)
    with pytest.raises(InvalidLocatorSyntax):
        await s._resolve_single(_loc("css", "<<<"), 500)


# ---- _resolve_single success path + remaining ----

async def test_remaining_deducts_elapsed(monkeypatch):
    # Scripted clock: the wait "takes" 200 ms without sleeping, so the
    # assertion is exact and cannot flake on a loaded runner.
    fake = FakePwLoc(1)
    # State-driven so an extra clock read by the event loop cannot skew the
    # result: 100.0 until the wait has run, 100.2 after (200 ms elapsed).
    monkeypatch.setattr(
        "api.engine.playwright_engine.time.monotonic",
        lambda: 100.2 if fake.wait_calls else 100.0,
    )
    s = _session(fake, monkeypatch)
    pw, remaining = await s._resolve_single(_loc(), 1000)
    assert pw is fake
    assert remaining == 800, remaining
    assert isinstance(remaining, int)


async def test_remaining_clamped_to_1(monkeypatch):
    fake = FakePwLoc(1, wait_delay=0.05)
    s = _session(fake, monkeypatch)
    _, remaining = await s._resolve_single(_loc(), 10)
    assert remaining == 1


async def test_success_two_no_nth_is_ambiguous(monkeypatch):
    s = _session(FakePwLoc(2), monkeypatch)
    with pytest.raises(LocatorAmbiguous):
        await s._resolve_single(_loc(), 500)


async def test_success_nth_skips_count(monkeypatch):
    fake = FakePwLoc(2)
    s = _session(fake, monkeypatch)
    await s._resolve_single(_loc(nth=1), 500)
    assert fake.count_calls == 0


async def test_success_zero_after_wait_is_404(monkeypatch):
    # wait resolved but count says 0 (element detached between wait and count)
    s = _session(FakePwLoc(0), monkeypatch)
    with pytest.raises(ElementNotFound):
        await s._resolve_single(_loc(), 500)


# ---- navigation interceptor (handler logic only; redirect hops never reach it) ----

class _FakeRequest:
    def __init__(self, url: str, nav: bool = True) -> None:
        self.url = url
        self._nav = nav

    def is_navigation_request(self) -> bool:
        return self._nav


class _FakeRoute:
    def __init__(self, url: str, nav: bool = True) -> None:
        self.request = _FakeRequest(url, nav)
        self.aborted: str | None = None
        self.continued = False

    async def abort(self, reason: str) -> None:
        self.aborted = reason

    async def continue_(self) -> None:
        self.continued = True


def _bare_session(monkeypatch) -> PlaywrightEngineSession:
    monkeypatch.setattr("api.engine.playwright_engine.settings.env", "staging", raising=False)
    s = object.__new__(PlaywrightEngineSession)
    s._last_blocked_nav = None
    return s


@pytest.mark.asyncio
async def test_interceptor_aborts_literal_internal_ip_navigation(monkeypatch):
    s = _bare_session(monkeypatch)
    route = _FakeRoute("http://169.254.169.254/latest/meta-data/")
    await s._navigation_interceptor(route)
    assert route.aborted == "aborted" and not route.continued
    assert s._last_blocked_nav == route.request.url


@pytest.mark.asyncio
async def test_interceptor_continues_public_and_subresource_requests(monkeypatch):
    s = _bare_session(monkeypatch)
    public = _FakeRoute("https://example.com/")
    await s._navigation_interceptor(public)
    assert public.continued and public.aborted is None
    sub = _FakeRoute("http://169.254.169.254/x.png", nav=False)
    await s._navigation_interceptor(sub)
    assert sub.continued and sub.aborted is None  # sub-resources are the documented residual


# ---- _evaluate_settled (hydration-race retry) ----

import api.engine.playwright_engine as _pe  # noqa: E402

_CONTEXT_DESTROYED = PlaywrightError(
    "Execution context was destroyed, most likely because of a navigation."
)


class _FakeEvalPage:
    """A stand-in page whose evaluate() fails `fail_times` then returns `result`."""

    def __init__(self, fail_times: int, exc: Exception, result: str = "ok") -> None:
        self._fail_times = fail_times
        self._exc = exc
        self._result = result
        self.calls: list[tuple] = []

    async def evaluate(self, expression, *args):  # noqa: ANN001, ANN201
        self.calls.append((expression, args))
        if len(self.calls) <= self._fail_times:
            raise self._exc
        return self._result


def _eval_session(page) -> PlaywrightEngineSession:
    s = object.__new__(PlaywrightEngineSession)
    s._page = page
    return s


async def _instant_sleep(_seconds):  # noqa: ANN001, ANN202
    return None


@pytest.mark.parametrize("fail_times", [0, 1, 3])
async def test_evaluate_settled_retries_context_destroyed(fail_times, monkeypatch):
    # The navigation race resolves within the retry budget → the caller sees success.
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=fail_times, exc=_CONTEXT_DESTROYED, result="v")
    out = await _eval_session(page)._evaluate_settled("expr", retries=3)
    assert out == "v"
    assert len(page.calls) == fail_times + 1


async def test_evaluate_settled_gives_up_after_retries(monkeypatch):
    # Persistent context-destroyed still surfaces (bounded), not a hang.
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=99, exc=_CONTEXT_DESTROYED)
    with pytest.raises(PlaywrightError, match="Execution context was destroyed"):
        await _eval_session(page)._evaluate_settled("expr", retries=3)
    assert len(page.calls) == 4  # first try + 3 retries


async def test_evaluate_settled_does_not_retry_other_errors(monkeypatch):
    # A real (non-race) error must propagate on the first attempt, never masked or delayed.
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=99, exc=PlaywrightError("some other failure"))
    with pytest.raises(PlaywrightError, match="some other failure"):
        await _eval_session(page)._evaluate_settled("expr")
    assert len(page.calls) == 1


async def test_evaluate_settled_passes_arg_through(monkeypatch):
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=0, exc=_CONTEXT_DESTROYED, result="x")
    s = _eval_session(page)
    await s._evaluate_settled("expr", ["k", "v"])
    assert page.calls[0] == ("expr", (["k", "v"],))
    await s._evaluate_settled("expr2")
    assert page.calls[1] == ("expr2", ())  # no-arg form omits the evaluate arg


async def test_evaluate_action_routes_through_settled(monkeypatch):
    # Wiring guard: the general evaluate() must retry the hydration race (a
    # silent revert to bare page.evaluate would drop the retry and this fails).
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=1, exc=_CONTEXT_DESTROYED, result="v")
    out = await _eval_session(page).evaluate("expr", timeout=5000)
    assert out == "v"
    assert len(page.calls) == 2  # first attempt raced, retry succeeded


async def test_get_html_no_locator_routes_through_settled(monkeypatch):
    # Wiring guard for the whole-document read path.
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=1, exc=_CONTEXT_DESTROYED, result="<html>x</html>")
    out = await _eval_session(page).get_html(locator=None, outer=True, timeout=5000)
    assert out == "<html>x</html>"
    assert len(page.calls) == 2


class _HangingPage:
    """evaluate() never completes — used to drive the outer wait_for deadline."""

    async def evaluate(self, expression, *args):  # noqa: ANN001, ANN201
        await asyncio.sleep(10)


async def test_evaluate_persistent_race_surfaces_as_runtime_eval(monkeypatch):
    # Long caller timeout: the retry budget is exhausted first, so a persistent
    # hydration race surfaces as RuntimeEvalError (422), not a timeout — and does
    # so through evaluate()'s wait_for wrapper.
    monkeypatch.setattr(_pe.asyncio, "sleep", _instant_sleep)
    page = _FakeEvalPage(fail_times=99, exc=_CONTEXT_DESTROYED)
    with pytest.raises(RuntimeEvalError):
        await _eval_session(page).evaluate("expr", timeout=5000)
    assert len(page.calls) == 4  # 1 + 3 retries, then re-raised and classified


async def test_evaluate_short_timeout_maps_to_action_timeout():
    # When the caller timeout is shorter than the work, evaluate() surfaces
    # ActionTimeout (409) via the outer wait_for — real sleep, no monkeypatch.
    with pytest.raises(ActionTimeout):
        await _eval_session(_HangingPage()).evaluate("expr", timeout=50)
