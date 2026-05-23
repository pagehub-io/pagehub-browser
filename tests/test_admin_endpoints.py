"""Tests for /v1/admin/* — currently POST /v1/admin/reset-sessions."""

from __future__ import annotations

import pytest

from api.config import settings as app_settings

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def admin_client(client):
    """A TestClient with ADMIN_AUTH_TOKEN set, restored on teardown."""
    original = app_settings.admin_auth_token
    app_settings.admin_auth_token = ADMIN_TOKEN
    try:
        yield client
    finally:
        app_settings.admin_auth_token = original


def _auth_headers(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(client) -> str:
    r = client.post("/v1/sessions", json={"headless": True})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ---- auth gate ----------------------------------------------------------------


def test_reset_sessions_requires_token(admin_client):
    r = admin_client.post("/v1/admin/reset-sessions")
    assert r.status_code == 401
    assert "credentials" in r.json()["detail"].lower()


def test_reset_sessions_rejects_wrong_token(admin_client):
    r = admin_client.post("/v1/admin/reset-sessions", headers=_auth_headers("nope"))
    assert r.status_code == 401


def test_reset_sessions_rejects_wrong_scheme(admin_client):
    r = admin_client.post(
        "/v1/admin/reset-sessions", headers={"Authorization": f"Basic {ADMIN_TOKEN}"}
    )
    assert r.status_code == 401


def test_reset_sessions_disabled_when_token_unset(client):
    original = app_settings.admin_auth_token
    app_settings.admin_auth_token = None
    try:
        r = client.post("/v1/admin/reset-sessions", headers=_auth_headers())
        assert r.status_code == 401
        assert "disabled" in r.json()["detail"].lower()
    finally:
        app_settings.admin_auth_token = original


# ---- close behaviour ----------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 5])
def test_reset_sessions_closes_n(admin_client, n):
    sids = [_create(admin_client) for _ in range(n)]
    engine_sessions = [
        admin_client.app.state.manager._records[sid].engine_session for sid in sids
    ]

    r = admin_client.post(
        "/v1/admin/reset-sessions",
        json={"reason": "unit-test"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"closed": n, "reason": "unit-test"}

    # All previously-live sessions are gone from the registry.
    mgr = admin_client.app.state.manager
    for sid in sids:
        assert sid not in mgr._records
    # Engine teardown actually happened for each one.
    for s in engine_sessions:
        assert s.closed is True


def test_reset_sessions_idempotent_zero(admin_client):
    r1 = admin_client.post("/v1/admin/reset-sessions", headers=_auth_headers())
    assert r1.status_code == 200 and r1.json()["closed"] == 0
    r2 = admin_client.post("/v1/admin/reset-sessions", headers=_auth_headers())
    assert r2.status_code == 200 and r2.json()["closed"] == 0


def test_reset_sessions_default_reason(admin_client):
    r = admin_client.post("/v1/admin/reset-sessions", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["reason"] == "operator-initiated"


def test_reset_sessions_subsequent_action_404(admin_client):
    """A subsequent action against a closed session id returns 404."""
    sid = _create(admin_client)
    r = admin_client.post("/v1/admin/reset-sessions", headers=_auth_headers())
    assert r.status_code == 200 and r.json()["closed"] == 1

    # The id should no longer resolve — neither status probe nor action.
    r = admin_client.get(f"/v1/sessions/{sid}")
    assert r.status_code == 404
    r = admin_client.post(f"/v1/sessions/{sid}/navigate", json={"url": "https://example.com"})
    assert r.status_code == 404


def test_reset_sessions_closes_mid_action_with_prejudice(admin_client):
    """An in-flight (locked) session must still be torn down by reset."""
    import asyncio

    sid = _create(admin_client)
    mgr = admin_client.app.state.manager
    rec = mgr._records[sid]
    engine_session = rec.engine_session

    async def runner():
        await rec.lock.acquire()  # simulate an action holding the lock
        try:
            # Reset must NOT wait on the lock — it closes with prejudice.
            from api.v1.admin.router import reset_sessions
            from api.v1.admin.schemas import ResetSessionsRequest

            body = ResetSessionsRequest(reason="forced")
            return await reset_sessions(body=body, manager=mgr, _=None)
        finally:
            if rec.lock.locked():
                rec.lock.release()

    resp = asyncio.run(runner())
    assert resp.closed == 1
    assert resp.reason == "forced"
    assert sid not in mgr._records
    assert engine_session.closed is True


def test_reset_sessions_extra_body_field_rejected(admin_client):
    r = admin_client.post(
        "/v1/admin/reset-sessions",
        json={"reason": "ok", "unknown": True},
        headers=_auth_headers(),
    )
    assert r.status_code == 422
