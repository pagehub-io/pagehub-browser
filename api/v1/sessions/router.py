"""Session lifecycle routes: POST/GET/GET-by-id/DELETE /v1/sessions."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response

from api.config import settings
from api.deps import get_manager
from api.engine.base import SessionOpts
from api.http_errors import capacity_http, session_busy_http, session_gone_http
from api.session_manager import SessionBusy, SessionCapacityError, SessionGone, SessionManager, SessionRecord
from api.v1.sessions.schemas import (
    CreateSessionRequest,
    ErrorResponse,
    SessionListResponse,
    SessionResponse,
    ViewportModel,
)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

_ERROR_RESPONSES = {404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def serialize_session(rec: SessionRecord, *, current_url: str | None) -> SessionResponse:
    return SessionResponse(
        session_id=rec.session_id,
        created_at=_iso(rec.created_at),
        last_used_at=_iso(rec.last_used_at),
        idle_timeout_seconds=rec.idle_timeout,
        headless=rec.headless,
        viewport=ViewportModel(width=rec.viewport[0], height=rec.viewport[1]),
        current_url=current_url,
    )


@router.post("", response_model=SessionResponse, status_code=201, responses=_ERROR_RESPONSES)
async def create_session(
    body: CreateSessionRequest, manager: SessionManager = Depends(get_manager)
) -> SessionResponse:
    opts = SessionOpts(
        headless=body.headless,
        viewport_width=body.viewport_width,
        viewport_height=body.viewport_height,
        user_agent=body.user_agent,
    )
    try:
        rec = await manager.create(opts, idle_timeout_seconds=body.idle_timeout_seconds)
    except SessionCapacityError as exc:
        raise capacity_http(exc, settings.reaper_interval_seconds)
    return serialize_session(rec, current_url=rec.current_url)


@router.get("", response_model=SessionListResponse)
async def list_sessions(manager: SessionManager = Depends(get_manager)) -> SessionListResponse:
    recs = manager.list()
    return SessionListResponse(
        count=len(recs),
        sessions=[serialize_session(r, current_url=r.current_url) for r in recs],
    )


@router.get("/{session_id}", response_model=SessionResponse, responses=_ERROR_RESPONSES)
async def get_session(
    session_id: str, manager: SessionManager = Depends(get_manager)
) -> SessionResponse:
    # Status probe — does NOT bump last_used_*.
    try:
        rec = manager.get(session_id)
    except SessionGone as exc:
        raise session_gone_http(exc)
    live_url = rec.current_url
    if not rec.lock.locked():
        async with rec.lock:
            try:
                live_url = await rec.engine_session.current_url()
            except Exception:  # noqa: BLE001 - status probe is best-effort
                live_url = rec.current_url
            else:
                rec.current_url = live_url
    return serialize_session(rec, current_url=live_url)


@router.delete("/{session_id}", status_code=204, responses=_ERROR_RESPONSES)
async def delete_session(
    session_id: str, manager: SessionManager = Depends(get_manager)
) -> Response:
    try:
        await manager.delete(
            session_id, lock_acquire_timeout_s=settings.lock_acquire_timeout_ms / 1000
        )
    except SessionGone as exc:
        raise session_gone_http(exc)
    except SessionBusy as exc:
        raise session_busy_http(exc)
    return Response(status_code=204)
