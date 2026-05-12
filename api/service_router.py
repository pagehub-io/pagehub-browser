"""Service-level routes: GET /health, GET /metrics. Both JSON, both response_model'd."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.config import settings
from api.deps import get_manager
from api.session_manager import SessionManager
from api.v1.sessions.schemas import HealthResponse, MetricsResponse

router = APIRouter(tags=["service"])


@router.get("/health", response_model=HealthResponse)
async def health(manager: SessionManager = Depends(get_manager)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        commit=settings.git_commit,
        engine="playwright-chromium",
        env=settings.env,
        live_sessions=manager.live_sessions,
    )


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(manager: SessionManager = Depends(get_manager)) -> MetricsResponse:
    return MetricsResponse(**manager.metrics_snapshot())
