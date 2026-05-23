"""Admin routes: operator-initiated state purges. Bearer-token gated.

Every endpoint here MUST require a valid ADMIN_AUTH_TOKEN — fail-closed when the token
is unset, so an unconfigured deploy cannot be reset by a stranger.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request

from api.config import settings
from api.deps import get_manager
from api.session_manager import SessionManager
from api.v1.admin.schemas import ResetSessionsRequest, ResetSessionsResponse
from api.v1.sessions.schemas import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_ERROR_RESPONSES = {401: {"model": ErrorResponse}}


def require_admin(request: Request) -> None:
    """Bearer-token gate. Unset token => every request 401."""
    expected = settings.admin_auth_token
    if not expected:
        raise HTTPException(status_code=401, detail="Admin endpoints disabled.")
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or value != expected:
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")


def _short_audit_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:8]


@router.post(
    "/reset-sessions",
    response_model=ResetSessionsResponse,
    status_code=200,
    responses=_ERROR_RESPONSES,
)
async def reset_sessions(
    body: ResetSessionsRequest | None = None,
    manager: SessionManager = Depends(get_manager),
    _: None = Depends(require_admin),
) -> ResetSessionsResponse:
    reason = (body.reason if body and body.reason else "operator-initiated").strip() or "operator-initiated"
    audit_ids = [_short_audit_id(sid) for sid in list(manager._records.keys())]
    closed = 0
    for rec in list(manager._records.values()):
        rec.closing = True
        with suppress(Exception):
            await rec.engine_session.close()
        manager._records.pop(rec.session_id, None)
        manager.sessions_deleted_total += 1
        closed += 1
    logger.info(
        "reset-sessions: closed %d sessions (reason=%s) ids=%s",
        closed,
        reason,
        ",".join(audit_ids) if audit_ids else "-",
    )
    return ResetSessionsResponse(closed=closed, reason=reason)
