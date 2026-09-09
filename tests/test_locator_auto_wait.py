"""Integration cases for locator auto-wait and uniform ambiguity reporting.

Pages are served from a local HTTP server so timing is deterministic; the
SSRF guard is opened for private hosts the way a local eval loop does.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.browser

PAGES = {
    "/late": """<html><body><h1>late</h1>
      <script>setTimeout(() => {
        const b = document.createElement('button'); b.setAttribute('data-testid','go'); b.textContent='Go';
        b.onclick = () => { document.title = 'clicked'; }; document.body.appendChild(b);
      }, 600);</script></body></html>""",
    "/hidden": """<html><body><div data-testid="ghost" style="display:none">boo</div></body></html>""",
    "/dup": """<html><body><button data-testid="dup">a</button><button data-testid="dup">b</button></body></html>""",
    "/plain": """<html><body><p>plain</p></body></html>""",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path, "<html><body>404</body></html>").encode()
        self.send_response(200 if self.path in PAGES else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ANN002
        pass


@pytest.fixture
def local_pages():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.config import settings
    from api.engine.playwright_engine import PlaywrightEngine
    from api.main import create_app

    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setattr(settings, "browser_allow_private_hosts", True)
    app = create_app()
    app.state.engine = PlaywrightEngine()
    with TestClient(app) as c:
        yield c


def _session(c, base: str, path: str) -> str:
    sid = c.post("/v1/sessions", json={"headless": True}).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/navigate", json={"url": base + path})
    assert r.status_code == 200, r.text
    return sid


def test_late_element_is_clicked_within_timeout(client, local_pages):
    sid = _session(client, local_pages, "/late")
    r = client.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "testid", "value": "go"}, "timeout": 3000},
    )
    assert r.status_code == 200, r.text
    t = client.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "document.title"})
    assert t.json()["text"] == "clicked"


def test_zero_match_is_404_after_timeout(client, local_pages):
    sid = _session(client, local_pages, "/plain")
    r = client.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "role", "value": "button", "options": {"name": "Definitely Not Here"}}, "timeout": 400},
    )
    assert r.status_code == 404
    assert "no element matched role=button" in r.json()["detail"]


def test_bad_css_on_click_is_400(client, local_pages):
    sid = _session(client, local_pages, "/plain")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "css", "value": "<<<"}, "timeout": 400})
    assert r.status_code == 400
    assert "Invalid css selector syntax" in r.json()["detail"]


def test_attached_but_never_visible_fails_at_about_timeout_not_double(client, local_pages):
    import time

    sid = _session(client, local_pages, "/hidden")
    started = time.monotonic()
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "testid", "value": "ghost"}, "timeout": 1000})
    elapsed = time.monotonic() - started
    assert r.status_code == 409, r.text
    assert elapsed < 1.9, f"took {elapsed:.2f}s, wait and action did not share one budget"


def test_get_text_on_hidden_element_is_409_row_157(client, local_pages):
    sid = _session(client, local_pages, "/hidden")
    r = client.post(f"/v1/sessions/{sid}/get-text", json={"locator": {"strategy": "testid", "value": "ghost"}, "timeout": 500})
    assert r.status_code == 409, r.text
    assert "waiting for testid=ghost to be visible" in r.json()["detail"]


def test_duplicate_testid_is_409_on_click_and_wait_for(client, local_pages):
    sid = _session(client, local_pages, "/dup")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "testid", "value": "dup"}, "timeout": 500})
    assert r.status_code == 409 and "matched 2 elements" in r.json()["detail"]
    for state in ("visible", "hidden"):
        r = client.post(
            f"/v1/sessions/{sid}/wait-for",
            json={"locator": {"strategy": "testid", "value": "dup"}, "state": state, "timeout": 500},
        )
        assert r.status_code == 409, (state, r.text)
        assert "matched 2 elements" in r.json()["detail"]


def test_nth_bypasses_strict_check(client, local_pages):
    sid = _session(client, local_pages, "/dup")
    r = client.post(
        f"/v1/sessions/{sid}/get-text",
        json={"locator": {"strategy": "testid", "value": "dup", "options": {"nth": 1}}, "timeout": 500},
    )
    assert r.status_code == 200 and r.json()["text"] == "b"
