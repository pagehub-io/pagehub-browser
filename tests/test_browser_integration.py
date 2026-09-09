"""Real-Chromium integration tests — the only place the live lifespan + reaper + engine run together.

Run with `make test-browser` (CI installs Playwright + Chromium); skipped otherwise.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture
def real_client(monkeypatch):
    """A TestClient backed by the real PlaywrightEngine, with a fast reaper.

    No module reloads — those mutate the global `settings` / `api.main` modules and leak
    into later tests. The engine is injected directly; the reaper cadence is shrunk via a
    monkeypatched setting (auto-restored) so the lifespan builds the manager with it, and
    the idle floor is dropped on the live manager after startup.
    """
    from fastapi.testclient import TestClient

    from api.config import settings
    from api.engine.playwright_engine import PlaywrightEngine
    from api.main import create_app

    monkeypatch.setattr(settings, "reaper_interval_seconds", 1)

    app = create_app()
    app.state.engine = PlaywrightEngine()
    with TestClient(app) as c:
        c.app.state.manager.idle_timeout_min = 1
        c.app.state.manager.idle_timeout_default = 1
        c.app.state.manager.reaper_interval = 1
        yield c


def test_full_flow_real_browser(real_client):
    c = real_client
    sid = c.post("/v1/sessions", json={"headless": True}).json()["session_id"]

    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == 200

    r = c.post(f"/v1/sessions/{sid}/get-text", json={"locator": {"strategy": "role", "value": "heading"}})
    assert r.status_code == 200
    assert "Example" in r.json()["text"]

    # example.com has exactly one link; nth:0 disambiguates regardless of its label.
    r = c.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "role", "value": "link", "options": {"nth": 0}}},
    )
    assert r.status_code == 200

    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 200

    r = c.post(f"/v1/sessions/{sid}/screenshot", json={})
    assert r.status_code == 200
    assert r.json()["image"].startswith("data:image/png;base64,")

    r = c.post(f"/v1/sessions/{sid}/find", json={"locator": {"strategy": "role", "value": "link"}})
    assert r.status_code == 200
    assert r.json()["count"] >= 1

    # ambiguous read primitive -> 409; no-match -> 404
    r = c.post(f"/v1/sessions/{sid}/get-text", json={"locator": {"strategy": "css", "value": "a"}})
    # example.com has exactly one link, so this is fine; use a broader locator for the ambiguous case
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    r_amb = c.post(f"/v1/sessions/{sid}/get-text", json={"locator": {"strategy": "css", "value": "*"}})
    assert r_amb.status_code == 409
    r_404 = c.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "role", "value": "button", "options": {"name": "Nope Not Here"}}},
    )
    assert r_404.status_code == 404
    assert "no element matched" in r_404.json()["detail"]

    r = c.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "document.title"})
    assert r.status_code == 200
    assert "Example" in r.json()["text"]

    r = c.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "throw new Error('boom')"})
    assert r.status_code == 422
    assert "Action failed" in r.json()["detail"]

    assert c.request("DELETE", f"/v1/sessions/{sid}").status_code == 204
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404
    assert "Unknown session" in r.json()["detail"]


def test_ssrf_real_browser_precheck(real_client):
    c = real_client
    sid = c.post("/v1/sessions", json={"headless": True}).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "http://127.0.0.1:1/"})
    assert r.status_code == 400
    assert "host not allowed" in r.json()["detail"]
    c.request("DELETE", f"/v1/sessions/{sid}")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Playwright/Chromium does not route redirected requests (crNetworkManager.js: a request "
        "with redirectedFrom gets Fetch.continueRequest, no route), so the navigation interceptor "
        "never sees a redirect hop; verified 2026-09-09. Flips to XPASS, loudly, when the redirect "
        "gap is closed (separate security-reviewed plan; pagehub-browser#5)."
    ),
)
def test_ssrf_redirect_interceptor_blocks_redirect_hop(monkeypatch):
    """Deterministic: fulfil a public-looking URL with a 302 to a live loopback
    listener and expect the interceptor to abort the hop (BlockedNavigation)."""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from api.config import settings
    from api.engine.base import SessionOpts
    from api.engine.errors import BlockedNavigation
    from api.engine.playwright_engine import PlaywrightEngine

    # Pin the guard on regardless of a developer's .env, so the XFAIL is for
    # the redirect gap and the flip-to-XPASS after the fix is real.
    monkeypatch.setattr(settings, "env", "staging")
    monkeypatch.setattr(settings, "browser_allow_private_hosts", False)

    class _Secret(BaseHTTPRequestHandler):
        hits: list[str] = []

        def do_GET(self):  # noqa: N802
            _Secret.hits.append(self.path)
            body = b"<html><title>SECRET</title><body>INTERNAL-ONLY</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    async def scenario() -> None:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Secret)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        target = f"http://127.0.0.1:{srv.server_address[1]}/secret"
        engine = PlaywrightEngine()
        sess = await engine.new_session(SessionOpts())
        try:
            async def fulfil(route, request):  # noqa: ANN001
                await route.fulfill(status=302, headers={"Location": target}, body="")

            await sess._context.route("http://example.com/redir", fulfil)
            with pytest.raises(BlockedNavigation):
                await sess.navigate("http://example.com/redir", "load", 15000)
            assert _Secret.hits == []
        finally:
            await engine.close()
            srv.shutdown()
            srv.server_close()

    asyncio.run(scenario())


def test_evaluate_timeout_real(real_client):
    """A never-resolving Promise must be killed by the asyncio.wait_for wrapper, not the 120s ceiling."""
    import time

    c = real_client
    sid = c.post("/v1/sessions", json={"headless": True}).json()["session_id"]
    c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    started = time.monotonic()
    r = c.post(
        f"/v1/sessions/{sid}/evaluate",
        json={"expression": "new Promise(resolve => {})", "timeout": 1500},
    )
    elapsed = time.monotonic() - started
    assert r.status_code == 409, r.text
    assert "Timed out" in r.json()["detail"]
    assert elapsed < 10
    c.request("DELETE", f"/v1/sessions/{sid}")


def test_idle_reaper_real(real_client):
    c = real_client
    sid = c.post("/v1/sessions", json={"headless": True, "idle_timeout_seconds": 1}).json()["session_id"]
    import time

    time.sleep(4)  # > idle_timeout(1) + reaper_interval(1) with margin
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404
    assert "expired" in r.json()["detail"]
    assert c.get("/metrics").json()["sessions_reaped_total"] >= 1
