"""Reviewer scratch: unit-level coverage of the auto-wait engine code with NO browser.

Proves the paths the spec calls 'only reachable through browser tests' are reachable
with a stubbed Playwright locator + real PlaywrightError/TimeoutError instances.
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
    fake = FakePwLoc(1, wait_delay=0.2)
    s = _session(fake, monkeypatch)
    pw, remaining = await s._resolve_single(_loc(), 1000)
    assert pw is fake
    assert 650 <= remaining <= 800, remaining
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
