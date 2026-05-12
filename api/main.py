"""FastAPI app factory + lifespan for pagehub-browser."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from api.config import settings
from api.engine.base import Engine
from api.middleware import BodySizeLimitMiddleware, TwinHeaderMiddleware
from api.service_router import router as service_router
from api.session_manager import SessionManager, reaper_loop
from api.v1.sessions.actions.router import router as actions_router
from api.v1.sessions.router import router as sessions_router

logger = logging.getLogger(__name__)


def _build_engine() -> Engine:
    if settings.engine == "fake":
        from tests.fakes import FakeEngine

        return FakeEngine()
    from api.engine.playwright_engine import PlaywrightEngine

    return PlaywrightEngine()


def _build_manager(engine: Engine) -> SessionManager:
    return SessionManager(
        engine,
        max_sessions=settings.max_sessions,
        idle_timeout_default=settings.idle_timeout_seconds,
        idle_timeout_min=30,
        idle_timeout_max=settings.idle_timeout_max_seconds,
        reaper_interval=settings.reaper_interval_seconds,
        tombstone_max=settings.tombstone_max,
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.sentry_dsn, environment=settings.env, traces_sample_rate=0.0
            )
        except Exception:  # noqa: BLE001 - Sentry must never block boot
            logger.exception("sentry init failed")

    engine: Engine = app.state.engine if hasattr(app.state, "engine") else _build_engine()
    manager = _build_manager(engine)
    app.state.engine = engine
    app.state.manager = manager
    app.state.reaper_task = asyncio.create_task(reaper_loop(manager))

    try:
        yield
    finally:
        task: asyncio.Task = app.state.reaper_task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await manager.close_all()
        with contextlib.suppress(Exception):
            await engine.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="pagehub-browser",
        description=(
            "Multi-session HTTP-over-headless-browser service. Each browser action is an "
            "ordinary HTTP step; sessions are isolated; elements are targeted with resilient "
            "structured Locator objects (getByRole / getByText / ...) — css/xpath is the escape hatch. "
            "find() returns at most 50 elements; full-page screenshots of large pages produce large JSON responses."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.request_body_max_bytes)
    app.add_middleware(TwinHeaderMiddleware)

    app.include_router(service_router)
    app.include_router(sessions_router)
    app.include_router(actions_router)
    return app


app = create_app()
