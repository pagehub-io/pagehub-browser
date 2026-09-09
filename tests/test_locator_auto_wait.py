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
    "/late-hidden": """<html><body><h1>late hidden</h1>
      <script>setTimeout(() => {
        const d = document.createElement('div'); d.setAttribute('data-testid','ghost'); d.style.display='none'; d.textContent='boo';
        document.body.appendChild(d);
      }, 1000);</script></body></html>""",
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
        srv.server_close()


@pytest.fixture
def browser_client(monkeypatch):
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


def test_late_element_is_clicked_within_timeout(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/late")
    r = browser_client.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "testid", "value": "go"}, "timeout": 3000},
    )
    assert r.status_code == 200, r.text
    t = browser_client.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "document.title"})
    assert t.json()["text"] == "clicked"


def test_zero_match_is_404_after_timeout(browser_client, local_pages):
    import time

    sid = _session(browser_client, local_pages, "/plain")
    started = time.monotonic()
    r = browser_client.post(
        f"/v1/sessions/{sid}/click",
        json={"locator": {"strategy": "role", "value": "button", "options": {"name": "Definitely Not Here"}}, "timeout": 400},
    )
    assert time.monotonic() - started >= 0.4, "did not wait for the element before answering 404"
    assert r.status_code == 404
    assert "no element matched role=button" in r.json()["detail"]


def test_bad_css_on_click_is_400(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/plain")
    r = browser_client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "css", "value": "<<<"}, "timeout": 400})
    assert r.status_code == 400
    assert "Invalid css selector syntax" in r.json()["detail"]


def test_late_attached_but_never_visible_shares_one_budget(browser_client, local_pages):
    """The element attaches ~1 s in and never becomes visible: the wait consumes
    ~1 s of the 2 s budget and the click gets only the remainder, so the whole
    verb fails at about `timeout`, not at about 2 x `timeout`."""
    import time

    sid = _session(browser_client, local_pages, "/late-hidden")
    started = time.monotonic()
    r = browser_client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "testid", "value": "ghost"}, "timeout": 2000})
    elapsed = time.monotonic() - started
    assert r.status_code == 409, r.text
    assert "Timed out after 2000ms waiting for testid=ghost to be actionable" in r.json()["detail"]
    assert elapsed < 2.6, f"took {elapsed:.2f}s, wait and action did not share one budget"


def test_nth_beyond_match_count_is_404_after_timeout(browser_client, local_pages):
    import time

    sid = _session(browser_client, local_pages, "/dup")
    started = time.monotonic()
    r = browser_client.post(
        f"/v1/sessions/{sid}/get-text",
        json={"locator": {"strategy": "testid", "value": "dup", "options": {"nth": 5}}, "timeout": 400},
    )
    assert time.monotonic() - started >= 0.4
    assert r.status_code == 404 and "no element matched" in r.json()["detail"]


def test_wait_for_hidden_zero_match_is_still_success(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/plain")
    for state in ("hidden", "detached"):
        r = browser_client.post(
            f"/v1/sessions/{sid}/wait-for",
            json={"locator": {"strategy": "testid", "value": "never"}, "state": state, "timeout": 300},
        )
        assert r.status_code == 200, (state, r.text)


def test_get_text_on_hidden_element_is_409_row_157(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/hidden")
    r = browser_client.post(f"/v1/sessions/{sid}/get-text", json={"locator": {"strategy": "testid", "value": "ghost"}, "timeout": 500})
    assert r.status_code == 409, r.text
    assert "waiting for testid=ghost to be visible" in r.json()["detail"]


def test_duplicate_testid_is_409_on_click_and_wait_for(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/dup")
    r = browser_client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "testid", "value": "dup"}, "timeout": 500})
    assert r.status_code == 409 and "matched 2 elements" in r.json()["detail"]
    for state in ("visible", "hidden"):
        r = browser_client.post(
            f"/v1/sessions/{sid}/wait-for",
            json={"locator": {"strategy": "testid", "value": "dup"}, "state": state, "timeout": 500},
        )
        assert r.status_code == 409, (state, r.text)
        assert "matched 2 elements" in r.json()["detail"]


def test_nth_bypasses_strict_check(browser_client, local_pages):
    sid = _session(browser_client, local_pages, "/dup")
    r = browser_client.post(
        f"/v1/sessions/{sid}/get-text",
        json={"locator": {"strategy": "testid", "value": "dup", "options": {"nth": 1}}, "timeout": 500},
    )
    assert r.status_code == 200 and r.json()["text"] == "b"
