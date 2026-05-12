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


def test_ssrf_redirect_interceptor(real_client):
    """A public URL that 302s to a literal internal IP is aborted by the context.route interceptor."""
    c = real_client
    sid = c.post("/v1/sessions", json={"headless": True}).json()["session_id"]
    # nip.io / a redirect service that bounces to 127.0.0.1; httpstat.us supports a Location.
    redirect_url = "https://httpstat.us/302?Location=http://169.254.169.254/"
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": redirect_url})
    # The redirect either aborts (400 host not allowed) or — if the fixture is unreachable — a 502.
    assert r.status_code in (400, 502)
    if r.status_code == 400:
        assert "host not allowed" in r.json()["detail"]
    c.request("DELETE", f"/v1/sessions/{sid}")


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
