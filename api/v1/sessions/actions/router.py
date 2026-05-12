"""Per-session action routes: /v1/sessions/{session_id}/<verb>."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException

from api.config import settings
from api.deps import get_manager
from api.engine.base import CookieSpec, EngineSession
from api.engine.errors import EngineError
from api.http_errors import (
    engine_error_http,
    is_5xx_engine_error,
    navigation_blocked_http,
    session_busy_http,
    session_gone_http,
)
from api.session_manager import SessionBusy, SessionGone, SessionManager, SessionRecord, short
from api.ssrf import NavigationBlocked, check_navigate_url
from api.v1.sessions.actions.schemas import (
    ActionResponse,
    ClickRequest,
    ConsoleEntry,
    ConsoleLogsResponse,
    CookiesRequest,
    CookiesResponse,
    ElementAttributeResponse,
    ElementHtmlResponse,
    ElementInfo,
    ElementTextResponse,
    EvaluateRequest,
    EvaluateResponse,
    FindRequest,
    FindResultResponse,
    GetAttributeRequest,
    GetHtmlRequest,
    GetTextRequest,
    HoverRequest,
    LocalStorageRequest,
    LocalStorageResponse,
    NavigateRequest,
    NavigateResponse,
    NetworkEntry,
    NetworkFilterRequest,
    NetworkLogResponse,
    PressKeyRequest,
    ScreenshotRequest,
    ScreenshotResponse,
    SelectOptionRequest,
    TypeRequest,
    WaitForLoadRequest,
    WaitForRequest,
)
from api.v1.sessions.schemas import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions/{session_id}", tags=["actions"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

T = TypeVar("T")

_BODILESS_TIMEOUT_MS = settings.action_timeout_max_ms


async def _run_action(
    manager: SessionManager,
    session_id: str,
    verb: str,
    fn: Callable[[EngineSession, SessionRecord], Awaitable[T]],
    *,
    log_host: str | None = None,
) -> T:
    """Acquire the per-session lock, run one engine coroutine, bump the idle clock, map errors."""
    try:
        rec = manager.get(session_id)
    except SessionGone as exc:
        raise session_gone_http(exc)

    try:
        await asyncio.wait_for(
            rec.lock.acquire(), timeout=settings.lock_acquire_timeout_ms / 1000
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise session_busy_http(SessionBusy())

    manager.actions_total += 1
    try:
        result = await fn(rec.engine_session, rec)
    except EngineError as exc:
        # The caller is actively using the session even on a not-found 404 — bump the clock.
        manager.touch(rec)
        http_exc = engine_error_http(exc)
        if is_5xx_engine_error(exc) or http_exc.status_code >= 500:
            manager.action_errors_total += 1
            logger.error("action sid=%s verb=%s engine error: %s", short(session_id), verb, exc)
        logger.info(
            "action sid=%s verb=%s status=%s%s",
            short(session_id),
            verb,
            http_exc.status_code,
            f" host={log_host}" if log_host else "",
        )
        raise http_exc
    except Exception:  # noqa: BLE001 - unexpected fault -> 500, no detail in body
        manager.touch(rec)
        manager.action_errors_total += 1
        logger.exception("action sid=%s verb=%s unexpected fault", short(session_id), verb)
        raise HTTPException(status_code=500, detail="Internal error.")
    else:
        manager.touch(rec)
        logger.info(
            "action sid=%s verb=%s status=200%s",
            short(session_id),
            verb,
            f" host={log_host}" if log_host else "",
        )
        return result
    finally:
        rec.lock.release()


# ---- navigate ------------------------------------------------------------------


@router.post("/navigate", response_model=NavigateResponse, response_model_exclude_none=True, responses=_ERROR_RESPONSES)
async def navigate(
    session_id: str, body: NavigateRequest, manager: SessionManager = Depends(get_manager)
) -> NavigateResponse:
    try:
        check_navigate_url(body.url)
    except NavigationBlocked as exc:
        logger.info("action sid=%s verb=navigate status=400 (ssrf: %s)", short(session_id), exc.reason)
        raise navigation_blocked_http(exc)

    host = urlsplit(body.url).hostname

    async def do(engine: EngineSession, rec: SessionRecord) -> NavigateResponse:
        result = await engine.navigate(body.url, body.wait_until)
        rec.current_url = result.url
        return NavigateResponse(
            url=result.url, status=result.status, title=result.title, text=result.text
        )

    return await _run_action(manager, session_id, "navigate", do, log_host=host)


# ---- interaction ---------------------------------------------------------------


@router.post("/click", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def click(
    session_id: str, body: ClickRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.click(body.locator.to_engine(), body.timeout)
        return ActionResponse(message=f"Clicked {body.locator.to_engine().repr_str()}")

    return await _run_action(manager, session_id, "click", do)


@router.post("/type", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def type_text(
    session_id: str, body: TypeRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.type(body.locator.to_engine(), body.text, body.clear, body.timeout)
        return ActionResponse(message=f"Typed into {body.locator.to_engine().repr_str()}")

    return await _run_action(manager, session_id, "type", do)


@router.post("/select", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def select_option(
    session_id: str, body: SelectOptionRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.select(body.locator.to_engine(), body.value, body.timeout)
        return ActionResponse(message=f"Selected {body.value!r} in {body.locator.to_engine().repr_str()}")

    return await _run_action(manager, session_id, "select", do)


@router.post("/hover", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def hover(
    session_id: str, body: HoverRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.hover(body.locator.to_engine(), body.timeout)
        return ActionResponse(message=f"Hovered {body.locator.to_engine().repr_str()}")

    return await _run_action(manager, session_id, "hover", do)


@router.post("/press-key", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def press_key(
    session_id: str, body: PressKeyRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    loc = body.locator.to_engine() if body.locator is not None else None

    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.press_key(body.key, loc, body.timeout)
        return ActionResponse(message=f"Pressed {body.key}")

    return await _run_action(manager, session_id, "press-key", do)


@router.post("/evaluate", response_model=EvaluateResponse, responses=_ERROR_RESPONSES)
async def evaluate(
    session_id: str, body: EvaluateRequest, manager: SessionManager = Depends(get_manager)
) -> EvaluateResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> EvaluateResponse:
        return EvaluateResponse(text=await engine.evaluate(body.expression, body.timeout))

    return await _run_action(manager, session_id, "evaluate", do)


@router.post("/wait-for", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def wait_for(
    session_id: str, body: WaitForRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.wait_for_element(body.locator.to_engine(), body.state, body.timeout)
        return ActionResponse(
            message=f"Element {body.locator.to_engine().repr_str()} is now {body.state}."
        )

    return await _run_action(manager, session_id, "wait-for", do)


@router.post("/wait-for-load", response_model=ActionResponse, responses=_ERROR_RESPONSES)
async def wait_for_load(
    session_id: str, body: WaitForLoadRequest, manager: SessionManager = Depends(get_manager)
) -> ActionResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ActionResponse:
        await engine.wait_for_load(body.wait_until, body.timeout)
        return ActionResponse(message=f"Page reached {body.wait_until} state.")

    return await _run_action(manager, session_id, "wait-for-load", do)


# ---- read primitives -----------------------------------------------------------


@router.post("/get-text", response_model=ElementTextResponse, responses=_ERROR_RESPONSES)
async def get_text(
    session_id: str, body: GetTextRequest, manager: SessionManager = Depends(get_manager)
) -> ElementTextResponse:
    loc = body.locator.to_engine() if body.locator is not None else None

    async def do(engine: EngineSession, rec: SessionRecord) -> ElementTextResponse:
        return ElementTextResponse(text=await engine.get_text(loc, body.timeout))

    return await _run_action(manager, session_id, "get-text", do)


@router.post("/get-html", response_model=ElementHtmlResponse, responses=_ERROR_RESPONSES)
async def get_html(
    session_id: str, body: GetHtmlRequest, manager: SessionManager = Depends(get_manager)
) -> ElementHtmlResponse:
    loc = body.locator.to_engine() if body.locator is not None else None

    async def do(engine: EngineSession, rec: SessionRecord) -> ElementHtmlResponse:
        return ElementHtmlResponse(text=await engine.get_html(loc, body.outer, body.timeout))

    return await _run_action(manager, session_id, "get-html", do)


@router.post("/get-attribute", response_model=ElementAttributeResponse, responses=_ERROR_RESPONSES)
async def get_attribute(
    session_id: str, body: GetAttributeRequest, manager: SessionManager = Depends(get_manager)
) -> ElementAttributeResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ElementAttributeResponse:
        return ElementAttributeResponse(
            text=await engine.get_attribute(body.locator.to_engine(), body.attribute, body.timeout)
        )

    return await _run_action(manager, session_id, "get-attribute", do)


@router.post("/find", response_model=FindResultResponse, responses=_ERROR_RESPONSES)
async def find(
    session_id: str, body: FindRequest, manager: SessionManager = Depends(get_manager)
) -> FindResultResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> FindResultResponse:
        infos = await engine.find(body.locator.to_engine())
        elements = [
            ElementInfo(tag=i.tag, text=i.text, attributes=i.attributes) for i in infos[:50]
        ]
        return FindResultResponse(count=len(elements), elements=elements)

    return await _run_action(manager, session_id, "find", do)


# ---- screenshot ----------------------------------------------------------------


@router.post("/screenshot", response_model=ScreenshotResponse, responses=_ERROR_RESPONSES)
async def screenshot(
    session_id: str, body: ScreenshotRequest, manager: SessionManager = Depends(get_manager)
) -> ScreenshotResponse:
    loc = body.locator.to_engine() if body.locator is not None else None

    async def do(engine: EngineSession, rec: SessionRecord) -> ScreenshotResponse:
        return ScreenshotResponse(image=await engine.screenshot(loc, body.full_page, body.timeout))

    return await _run_action(manager, session_id, "screenshot", do)


# ---- console / network ---------------------------------------------------------


@router.get("/console-logs", response_model=ConsoleLogsResponse, response_model_exclude_none=True, responses=_ERROR_RESPONSES)
async def console_logs(
    session_id: str, clear: bool = False, manager: SessionManager = Depends(get_manager)
) -> ConsoleLogsResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> ConsoleLogsResponse:
        entries = await engine.console_logs(clear)
        return ConsoleLogsResponse(
            count=len(entries),
            entries=[ConsoleEntry(type=e.type, text=e.text, location=e.location) for e in entries],
        )

    return await _run_action(manager, session_id, "console-logs", do)


@router.get("/network-log", response_model=NetworkLogResponse, response_model_exclude_none=True, responses=_ERROR_RESPONSES)
async def network_log(
    session_id: str, clear: bool = False, manager: SessionManager = Depends(get_manager)
) -> NetworkLogResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> NetworkLogResponse:
        entries = await engine.network_log(clear)
        return NetworkLogResponse(
            count=len(entries),
            entries=[
                NetworkEntry(method=e.method, url=e.url, status=e.status, resource_type=e.resource_type)
                for e in entries
            ],
        )

    return await _run_action(manager, session_id, "network-log", do)


@router.post("/network-filter", response_model=NetworkLogResponse, response_model_exclude_none=True, responses=_ERROR_RESPONSES)
async def network_filter(
    session_id: str, body: NetworkFilterRequest, manager: SessionManager = Depends(get_manager)
) -> NetworkLogResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> NetworkLogResponse:
        entries = await engine.network_filter(
            body.url_pattern, body.resource_type, body.status_min, body.status_max, body.clear
        )
        return NetworkLogResponse(
            count=len(entries),
            entries=[
                NetworkEntry(method=e.method, url=e.url, status=e.status, resource_type=e.resource_type)
                for e in entries
            ],
        )

    return await _run_action(manager, session_id, "network-filter", do)


# ---- cookies / localStorage ----------------------------------------------------


@router.post("/cookies", response_model=CookiesResponse, responses=_ERROR_RESPONSES)
async def cookies(
    session_id: str, body: CookiesRequest, manager: SessionManager = Depends(get_manager)
) -> CookiesResponse:
    cookie_specs = None
    if body.cookies is not None:
        cookie_specs = [
            CookieSpec(
                name=c.name,
                value=c.value,
                domain=c.domain,
                path=c.path,
                expires=c.expires,
                http_only=c.http_only,
                secure=c.secure,
                same_site=c.same_site,
            )
            for c in body.cookies
        ]

    async def do(engine: EngineSession, rec: SessionRecord) -> CookiesResponse:
        return CookiesResponse(text=await engine.cookies(body.action, cookie_specs))

    return await _run_action(manager, session_id, "cookies", do)


@router.post("/local-storage", response_model=LocalStorageResponse, responses=_ERROR_RESPONSES)
async def local_storage(
    session_id: str, body: LocalStorageRequest, manager: SessionManager = Depends(get_manager)
) -> LocalStorageResponse:
    async def do(engine: EngineSession, rec: SessionRecord) -> LocalStorageResponse:
        return LocalStorageResponse(
            text=await engine.local_storage(body.action, body.key, body.value)
        )

    return await _run_action(manager, session_id, "local-storage", do)
