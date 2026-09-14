"""Deterministic unit tests for the SSRF navigation interceptor
(PlaywrightEngineSession._navigation_interceptor + api.ssrf.navigation_request_is_blocked).

Replaces the former real-browser `test_ssrf_redirect_interceptor`, which depended on an
external redirect service (httpstat.us issuing a 302 to a link-local IP) and went
persistently red when that service stopped redirecting. A real-browser redirect-BLOCK
test is infeasible to make self-contained here: blocking is only active when
`_private_hosts_allowed()` is False, but with the flag off the pre-check blocks any
private/localhost initial host AND the `data:` scheme — so exercising the redirect
interceptor requires a PUBLIC host that redirects to a blocked IP (an external dep).
These unit tests pin the flag off and drive the interceptor directly, keeping the teeth
(block decision + route.abort wiring) with zero network."""
from __future__ import annotations

import pytest

import api.ssrf as ssrf
from api.engine.playwright_engine import PlaywrightEngineSession

pytestmark = pytest.mark.asyncio


class _FakePage:
    def on(self, *_a, **_k) -> None:
        pass


class _FakeRequest:
    def __init__(self, url: str, is_nav: bool) -> None:
        self.url = url
        self._is_nav = is_nav

    def is_navigation_request(self) -> bool:
        return self._is_nav


class _FakeRoute:
    def __init__(self, url: str, is_nav: bool = True) -> None:
        self.request = _FakeRequest(url, is_nav)
        self.aborted: str | None = None
        self.continued = False

    async def abort(self, reason: str | None = None) -> None:
        self.aborted = reason

    async def continue_(self) -> None:
        self.continued = True


def _session() -> PlaywrightEngineSession:
    return PlaywrightEngineSession(engine=None, browser=None, context=None, page=_FakePage())


@pytest.fixture(autouse=True)
def _blocking_active(monkeypatch):
    # Force blocking on regardless of the ambient .env (local dev sets
    # BROWSER_ALLOW_PRIVATE_HOSTS=true, which would disable the interceptor).
    monkeypatch.setattr(ssrf, "_private_hosts_allowed", lambda: False)


async def test_interceptor_aborts_navigation_to_blocked_host():
    """A navigation (e.g. a server redirect target) to the cloud-metadata IP is aborted."""
    session = _session()
    route = _FakeRoute("http://169.254.169.254/")
    await session._navigation_interceptor(route)
    assert route.aborted == "aborted"
    assert route.continued is False
    assert session._last_blocked_nav == "http://169.254.169.254/"


async def test_interceptor_allows_public_navigation():
    session = _session()
    route = _FakeRoute("https://example.com/")
    await session._navigation_interceptor(route)
    assert route.continued is True
    assert route.aborted is None


async def test_interceptor_ignores_non_navigation_requests():
    """A blocked URL that is NOT a navigation (a subresource) is not the interceptor's
    job — it continues (the request-level SSRF guard is elsewhere)."""
    session = _session()
    route = _FakeRoute("http://169.254.169.254/", is_nav=False)
    await session._navigation_interceptor(route)
    assert route.continued is True
    assert route.aborted is None
