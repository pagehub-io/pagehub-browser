"""Locator, session-lifecycle, and service-level Pydantic schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.config import settings
from api.engine.base import Locator as EngineLocator
from api.engine.base import LocatorOptions as EngineLocatorOptions

LocatorStrategy = Literal[
    "role", "text", "label", "placeholder", "testid", "alt", "title", "css", "xpath"
]


class LocatorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    exact: bool | None = None
    nth: int | None = None
    has_text: str | None = None

    @field_validator("nth")
    @classmethod
    def _nth_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("options.nth must be >= 0")
        return v


class Locator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: LocatorStrategy
    value: str = Field(min_length=1)
    options: LocatorOptions | None = None

    def to_engine(self) -> EngineLocator:
        opts = None
        if self.options is not None:
            opts = EngineLocatorOptions(
                name=self.options.name,
                exact=self.options.exact,
                nth=self.options.nth,
                has_text=self.options.has_text,
            )
        return EngineLocator(strategy=self.strategy, value=self.value, options=opts)


# ---- session lifecycle ---------------------------------------------------------


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = Field(default_factory=lambda: settings.headless)
    viewport_width: int = Field(default_factory=lambda: settings.default_viewport_width, ge=1, le=7680)
    viewport_height: int = Field(default_factory=lambda: settings.default_viewport_height, ge=1, le=4320)
    user_agent: str | None = Field(default=None, max_length=2048)
    idle_timeout_seconds: int | None = Field(default=None, ge=1)


class ViewportModel(BaseModel):
    width: int
    height: int


class SessionResponse(BaseModel):
    session_id: str
    created_at: str
    last_used_at: str
    idle_timeout_seconds: int
    headless: bool
    viewport: ViewportModel
    current_url: str | None  # NOT subject to exclude_none — always emitted
    # Set on POST /v1/sessions when an idle session was LRU-evicted to make room
    # for this one. Always null on GET responses. Not subject to exclude_none.
    evicted_session_id: str | None = None


class SessionListResponse(BaseModel):
    count: int
    sessions: list[SessionResponse]


# ---- service-level -------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    commit: str
    engine: str
    env: str
    live_sessions: int


class MetricsResponse(BaseModel):
    live_sessions: int
    max_sessions: int
    sessions_created_total: int
    sessions_reaped_total: int
    sessions_deleted_total: int
    sessions_rejected_total: int
    sessions_lru_evicted_total: int
    actions_total: int
    action_errors_total: int
    browser_restarts_total: int
    uptime_seconds: int


class ErrorResponse(BaseModel):
    detail: str
