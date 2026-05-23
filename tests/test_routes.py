"""FastAPI route tests against the FakeEngine via TestClient."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.engine.errors import (
    ActionTimeout,
    ElementNotFound,
    EngineCrash,
    InvalidLocatorSyntax,
    LocatorAmbiguous,
    NavigationError,
    RuntimeEvalError,
)

ROLE_LINK = {"strategy": "role", "value": "link", "options": {"name": "More information"}}
ROLE_HEADING = {"strategy": "role", "value": "heading"}


def _create(client, **body):
    r = client.post("/v1/sessions", json={"headless": True, **body})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def _engine_session(client, sid: str):
    """Return the FakeEngineSession backing a session id."""
    mgr = client.app.state.manager
    return mgr.get(sid).engine_session


# ---- session lifecycle ---------------------------------------------------------


def test_create_session_201_current_url_null(client):
    r = client.post("/v1/sessions", json={"headless": True})
    assert r.status_code == 201
    body = r.json()
    assert "session_id" in body
    assert body["current_url"] is None  # present and null
    assert body["viewport"] == {"width": 1280, "height": 720}
    assert body["idle_timeout_seconds"] == 300


def test_get_session_and_list(client):
    sid = _create(client)
    r = client.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200
    r = client.get("/v1/sessions")
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_list_empty(client):
    r = client.get("/v1/sessions")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "sessions": []}


def test_delete_204_then_404(client):
    sid = _create(client)
    r = client.request("DELETE", f"/v1/sessions/{sid}")
    assert r.status_code == 204
    r = client.request("DELETE", f"/v1/sessions/{sid}")
    assert r.status_code == 404
    assert "Unknown session" in r.json()["detail"]


def test_unknown_session_404(client):
    r = client.post("/v1/sessions/deadbeef/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404
    assert "Unknown session" in r.json()["detail"]


def test_reaped_session_404_expired(client):
    sid = _create(client)
    # Seed a tombstone directly to simulate a reap.
    mgr = client.app.state.manager
    mgr._records.pop(sid)
    mgr._tombstone_add(sid, 30)
    r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "expired" in detail and "idle" in detail


def test_idle_timeout_negative_422(client):
    r = client.post("/v1/sessions", json={"idle_timeout_seconds": -1})
    assert r.status_code == 422


# ---- action happy paths --------------------------------------------------------


def test_navigate_happy(client):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert "status: 200" in body["text"]
    # current_url is now reflected on the session
    r = client.get(f"/v1/sessions/{sid}")
    assert r.json()["current_url"] == "https://example.com"


@pytest.mark.parametrize(
    "path,body,expected_status_field,expected_value",
    [
        ("click", {"locator": ROLE_LINK}, "status", "ok"),
        ("type", {"locator": ROLE_LINK, "text": "hi"}, "status", "ok"),
        ("select", {"locator": ROLE_LINK, "value": "a"}, "status", "ok"),
        ("hover", {"locator": ROLE_LINK}, "status", "ok"),
        ("press-key", {"key": "Enter"}, "status", "ok"),
        ("wait-for", {"locator": ROLE_LINK}, "status", "ok"),
        ("wait-for-load", {"wait_until": "load"}, "status", "ok"),
    ],
)
def test_action_responses(client, path, body, expected_status_field, expected_value):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/{path}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()[expected_status_field] == expected_value


def test_evaluate_happy(client):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "document.title"})
    assert r.status_code == 200
    assert "Example" in r.json()["text"]


def test_get_text_html_attribute(client):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/get-text", json={"locator": ROLE_HEADING})
    assert r.status_code == 200 and "Example" in r.json()["text"]
    r = client.post(f"/v1/sessions/{sid}/get-html", json={})
    assert r.status_code == 200 and "<html" in r.json()["text"]
    r = client.post(
        f"/v1/sessions/{sid}/get-attribute", json={"locator": ROLE_LINK, "attribute": "href"}
    )
    assert r.status_code == 200 and "http" in r.json()["text"]


def test_find_happy(client):
    sid = _create(client)
    _engine_session(client, sid).find_count = 3
    r = client.post(f"/v1/sessions/{sid}/find", json={"locator": {"strategy": "css", "value": "a"}})
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_screenshot_happy(client):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/screenshot", json={})
    assert r.status_code == 200
    assert r.json()["image"].startswith("data:image/png;base64,")


def test_console_and_network_logs(client):
    sid = _create(client)
    r = client.get(f"/v1/sessions/{sid}/console-logs")
    assert r.status_code == 200 and r.json()["count"] >= 1
    r = client.get(f"/v1/sessions/{sid}/network-log")
    assert r.status_code == 200 and r.json()["count"] >= 1
    r = client.post(f"/v1/sessions/{sid}/network-filter", json={"resource_type": "document"})
    assert r.status_code == 200 and r.json()["count"] >= 1


def test_cookies_and_local_storage(client):
    sid = _create(client)
    r = client.post(
        f"/v1/sessions/{sid}/cookies",
        json={"action": "set", "cookies": [{"name": "k", "value": "v"}]},
    )
    assert r.status_code == 200
    r = client.post(f"/v1/sessions/{sid}/cookies", json={"action": "get"})
    assert r.status_code == 200 and "k" in r.json()["text"]
    r = client.post(
        f"/v1/sessions/{sid}/local-storage", json={"action": "set", "key": "x", "value": "1"}
    )
    assert r.status_code == 200
    r = client.post(f"/v1/sessions/{sid}/local-storage", json={"action": "get", "key": "x"})
    assert r.status_code == 200 and r.json()["text"] == "1"


# ---- error matrix --------------------------------------------------------------


def test_element_not_found_404(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["click"] = ElementNotFound("role=button (name='Submit')")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "role", "value": "button", "options": {"name": "Submit"}}})
    assert r.status_code == 404
    assert "no element matched role=button" in r.json()["detail"]


def test_locator_ambiguous_409(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["click"] = LocatorAmbiguous(3)
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": ROLE_HEADING})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "matched" in detail and "options.nth" in detail


def test_get_text_ambiguous_409(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["get_text"] = LocatorAmbiguous(2)
    r = client.post(f"/v1/sessions/{sid}/get-text", json={"locator": ROLE_HEADING})
    assert r.status_code == 409
    assert "options.nth" in r.json()["detail"]


def test_bad_strategy_422(client):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "magic", "value": "x"}})
    assert r.status_code == 422


@pytest.mark.parametrize("path,body", [
    ("click", {}),
    ("type", {"text": "x"}),
    ("select", {"value": "x"}),
    ("hover", {}),
    ("get-attribute", {"attribute": "href"}),
    ("wait-for", {}),
    ("find", {}),
])
def test_missing_required_locator_422(client, path, body):
    sid = _create(client)
    r = client.post(f"/v1/sessions/{sid}/{path}", json=body)
    assert r.status_code == 422


def test_capacity_503_with_retry_after(client):
    """Cap reached with the one live session still in its recent-activity window
    -> 503 (no idle candidate to evict, same as the pre-LRU behaviour)."""
    mgr = client.app.state.manager
    mgr.max_sessions = 1
    _create(client)
    r = client.post("/v1/sessions", json={})
    assert r.status_code == 503
    assert "Retry-After" in r.headers


def test_create_evicts_oldest_idle_and_reports_evicted_session_id(client):
    """At cap with an idle session present -> 201 with evicted_session_id set."""
    mgr = client.app.state.manager
    mgr.max_sessions = 1
    sid_old = _create(client)
    # Drag the existing session past the 5s recent-activity window.
    rec = mgr._records[sid_old]
    rec.last_used_monotonic -= 10.0
    old_engine = rec.engine_session

    r = client.post("/v1/sessions", json={"headless": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["evicted_session_id"] == sid_old
    sid_new = body["session_id"]
    assert sid_new != sid_old
    # The new session is live; the old one is gone (and its engine session was closed).
    assert sid_new in mgr._records
    assert sid_old not in mgr._records
    assert old_engine.closed is True
    # Subsequent action against the evicted id -> 404 Unknown session.
    r = client.post(f"/v1/sessions/{sid_old}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404
    assert "Unknown session" in r.json()["detail"]


def test_create_evicted_session_id_null_when_under_cap(client):
    r = client.post("/v1/sessions", json={"headless": True})
    assert r.status_code == 201
    assert r.json()["evicted_session_id"] is None


def test_create_503_when_all_sessions_active_no_eviction(client):
    """Cap reached but every session was used within the last 5s -> 503 (back-pressure)."""
    mgr = client.app.state.manager
    mgr.max_sessions = 2
    _create(client)
    _create(client)  # both touched right now; recent-activity window protects them
    r = client.post("/v1/sessions", json={"headless": True})
    assert r.status_code == 503
    assert "Retry-After" in r.headers
    assert mgr.sessions_lru_evicted_total == 0


@pytest.mark.parametrize("url,reason_fragment", [
    ("http://127.0.0.1/", "host not allowed"),
    ("http://169.254.169.254/latest/meta-data/", "host not allowed"),
    ("http://10.0.0.1/", "host not allowed"),
    ("http://[::1]/", "host not allowed"),
    ("http://localhost:8000/", "host not allowed"),
    ("file:///etc/passwd", "unsupported scheme"),
    ("chrome://version", "unsupported scheme"),
    ("not a url at all", "malformed URL"),
])
def test_ssrf_blocked_400_engine_not_called(client, url, reason_fragment):
    sid = _create(client)
    eng_session = _engine_session(client, sid)
    r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": url})
    assert r.status_code == 400, r.text
    assert reason_fragment in r.json()["detail"]
    # the SSRF check ran before the engine — zero navigate calls
    assert eng_session.navigate_calls == []


def test_dev_escape_hatch_allows_private_hosts(client):
    """ENV=development + BROWSER_ALLOW_PRIVATE_HOSTS=true lets navigate reach localhost/RFC1918."""
    from api.config import settings as app_settings

    orig_env, orig_flag = app_settings.env, app_settings.browser_allow_private_hosts
    try:
        sid = _create(client)
        eng_session = _engine_session(client, sid)

        # flag on + dev env -> private host reaches the engine
        app_settings.env = "development"
        app_settings.browser_allow_private_hosts = True
        r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "http://127.0.0.1:3000/"})
        assert r.status_code == 200, r.text
        assert eng_session.navigate_calls and eng_session.navigate_calls[-1][0] == "http://127.0.0.1:3000/"
        # ...but file:// is still blocked (scheme allowlist is not relaxed)
        r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "file:///etc/passwd"})
        assert r.status_code == 400 and "unsupported scheme" in r.json()["detail"]

        # flag on but env != development -> ignored, strict deny-list applies
        app_settings.env = "staging"
        before = len(eng_session.navigate_calls)
        r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "http://127.0.0.1:3000/"})
        assert r.status_code == 400 and "host not allowed" in r.json()["detail"]
        assert len(eng_session.navigate_calls) == before  # engine not called
    finally:
        app_settings.env, app_settings.browser_allow_private_hosts = orig_env, orig_flag


def test_action_timeout_409(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["wait_for_load"] = ActionTimeout(
        "Timed out after 30000ms waiting for page to reach load."
    )
    r = client.post(f"/v1/sessions/{sid}/wait-for-load", json={"wait_until": "load"})
    assert r.status_code == 409
    assert "Timed out" in r.json()["detail"]


def test_invalid_locator_syntax_400(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["click"] = InvalidLocatorSyntax("css", "<<<", "bad selector")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "css", "value": "<<<"}})
    assert r.status_code == 400
    assert "Invalid css selector syntax" in r.json()["detail"]


def test_runtime_eval_error_422(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["evaluate"] = RuntimeEvalError("boom thrown")
    r = client.post(f"/v1/sessions/{sid}/evaluate", json={"expression": "throw new Error('boom')"})
    assert r.status_code == 422
    assert "Action failed" in r.json()["detail"]


def test_engine_crash_500_no_detail(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["click"] = EngineCrash("target closed")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": ROLE_LINK})
    assert r.status_code == 500
    assert r.json()["detail"] == "Internal error."


def test_navigation_error_502(client):
    sid = _create(client)
    _engine_session(client, sid).raise_on["navigate"] = NavigationError(
        "https://does-not-exist.invalid/", "DNS lookup failed"
    )
    r = client.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://does-not-exist.invalid/"})
    assert r.status_code == 502
    assert "failed" in r.json()["detail"]


def test_oversize_body_413(client):
    sid = _create(client)
    big = "x" * (1_048_576 + 100)
    r = client.post(f"/v1/sessions/{sid}/evaluate", json={"expression": big})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


# ---- concurrency / lock --------------------------------------------------------


def test_concurrent_action_same_session_409(client):
    """Two clicks on the same session: one wins, the other 409s 'Session busy'."""
    from api.config import settings as app_settings

    original = app_settings.lock_acquire_timeout_ms
    app_settings.lock_acquire_timeout_ms = 100
    try:
        sid = _create(client)
        eng_session = _engine_session(client, sid)
        ev = asyncio.Event()
        eng_session.block_on["click"] = ev

        async def hit():
            mgr = client.app.state.manager
            from api.v1.sessions.actions.router import click as click_route
            from api.v1.sessions.actions.schemas import ClickRequest
            from api.v1.sessions.schemas import Locator

            body = ClickRequest(locator=Locator(strategy="role", value="link"))
            try:
                await click_route(sid, body, mgr)
                return 200
            except Exception as exc:  # HTTPException
                return getattr(exc, "status_code", 500)

        async def runner():
            t1 = asyncio.create_task(hit())
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(hit())
            # let t2 hit the lock-acquire timeout
            await asyncio.sleep(0.2)
            ev.set()
            return await asyncio.gather(t1, t2)

        results = asyncio.run(runner())
        assert 200 in results
        assert 409 in results
    finally:
        app_settings.lock_acquire_timeout_ms = original


def test_delete_during_in_flight_action_409(client):
    from api.config import settings as app_settings

    original = app_settings.lock_acquire_timeout_ms
    app_settings.lock_acquire_timeout_ms = 100
    try:
        sid = _create(client)
        eng_session = _engine_session(client, sid)
        ev = asyncio.Event()
        eng_session.block_on["click"] = ev
        mgr = client.app.state.manager

        async def runner():
            from api.session_manager import SessionBusy
            from api.v1.sessions.actions.router import click as click_route
            from api.v1.sessions.actions.schemas import ClickRequest
            from api.v1.sessions.schemas import Locator

            body = ClickRequest(locator=Locator(strategy="role", value="link"))
            t = asyncio.create_task(click_route(sid, body, mgr))
            await asyncio.sleep(0.01)
            delete_status = None
            try:
                await mgr.delete(sid, lock_acquire_timeout_s=0.1)
            except SessionBusy:
                delete_status = 409
            ev.set()
            await t
            # now delete succeeds
            await mgr.delete(sid, lock_acquire_timeout_s=1)
            return delete_status

        assert asyncio.run(runner()) == 409
    finally:
        app_settings.lock_acquire_timeout_ms = original


def test_idle_clock_bumped_on_not_found_404(client):
    sid = _create(client)
    mgr = client.app.state.manager
    rec = mgr.get(sid)
    before = rec.last_used_monotonic
    time.sleep(0.01)
    _engine_session(client, sid).raise_on["click"] = ElementNotFound("role=button")
    r = client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "role", "value": "button"}})
    assert r.status_code == 404
    after = mgr.get(sid).last_used_monotonic
    assert after > before
    # status probe does NOT bump
    probe_before = mgr.get(sid).last_used_monotonic
    time.sleep(0.01)
    client.get(f"/v1/sessions/{sid}")
    assert mgr.get(sid).last_used_monotonic == probe_before


# ---- health / metrics ----------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["commit"] == "test-sha"
    assert body["engine"] == "playwright-chromium"
    assert "env" in body
    assert body["live_sessions"] == 0


def test_metrics_full_field_set_and_counters(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    expected_fields = {
        "live_sessions", "max_sessions", "sessions_created_total", "sessions_reaped_total",
        "sessions_deleted_total", "sessions_rejected_total", "sessions_lru_evicted_total",
        "actions_total", "action_errors_total", "browser_restarts_total", "uptime_seconds",
    }
    assert set(body.keys()) == expected_fields

    sid = _create(client)
    assert client.get("/metrics").json()["sessions_created_total"] == 1

    # ElementNotFound 404 must NOT bump action_errors_total
    _engine_session(client, sid).raise_on["click"] = ElementNotFound("role=button")
    client.post(f"/v1/sessions/{sid}/click", json={"locator": {"strategy": "role", "value": "button"}})
    assert client.get("/metrics").json()["action_errors_total"] == 0

    # EngineCrash 500 DOES bump it
    _engine_session(client, sid).raise_on["hover"] = EngineCrash("target closed")
    client.post(f"/v1/sessions/{sid}/hover", json={"locator": {"strategy": "role", "value": "link"}})
    assert client.get("/metrics").json()["action_errors_total"] == 1

    client.request("DELETE", f"/v1/sessions/{sid}")
    m = client.get("/metrics").json()
    assert m["sessions_deleted_total"] == 1
