"""Maps engine-typed errors and manager errors to FastAPI HTTPExceptions.

The detail strings here are the canonical wording from the SPEC's Error states table.
"""

from __future__ import annotations

from fastapi import HTTPException

from api.engine.errors import (
    ActionTimeout,
    AttributeNotPresent,
    BlockedNavigation,
    ElementNotFound,
    EngineCrash,
    EngineError,
    InvalidLocator,
    InvalidLocatorSyntax,
    LocatorAmbiguous,
    NavigationError,
    RuntimeEvalError,
)
from api.session_manager import SessionBusy, SessionCapacityError, SessionGone
from api.ssrf import NavigationBlocked

_MAX_ENGINE_MSG = 400


def _sanitize(msg: str) -> str:
    return " ".join(msg.split())[:_MAX_ENGINE_MSG]


def session_gone_http(exc: SessionGone) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def navigation_blocked_http(exc: NavigationBlocked) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def capacity_http(exc: SessionCapacityError, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=503, detail=str(exc), headers={"Retry-After": str(retry_after)}
    )


def session_busy_http(exc: SessionBusy) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def engine_error_http(exc: EngineError) -> HTTPException:
    """Map an engine-typed error. Returns (HTTPException, is_5xx) — caller bumps action_errors_total on 5xx."""
    if isinstance(exc, ElementNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AttributeNotPresent):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, LocatorAmbiguous):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidLocatorSyntax):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, InvalidLocator):
        return HTTPException(status_code=422, detail=str(exc) or "Invalid locator.")
    if isinstance(exc, ActionTimeout):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, BlockedNavigation):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, NavigationError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, RuntimeEvalError):
        return HTTPException(status_code=422, detail=f"Action failed: {_sanitize(exc.engine_message)}.")
    if isinstance(exc, EngineCrash):
        return HTTPException(status_code=500, detail="Internal error.")
    return HTTPException(status_code=500, detail="Internal error.")


def is_5xx_engine_error(exc: EngineError) -> bool:
    return isinstance(exc, EngineCrash)
