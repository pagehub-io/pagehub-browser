"""Per-session action request/response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.config import settings
from api.v1.sessions.schemas import Locator

_TIMEOUT_MAX = settings.action_timeout_max_ms

WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]


# ---- navigate ------------------------------------------------------------------


class NavigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    wait_until: WaitUntil = "load"
    timeout: int = Field(default=30000, ge=1, le=_TIMEOUT_MAX)


class NavigateResponse(BaseModel):
    url: str
    status: int | None
    title: str
    text: str


# ---- interaction (locator-bearing) ---------------------------------------------


class ClickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class TypeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    text: str
    clear: bool = True
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class SelectOptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    value: str
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class HoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class PressKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    locator: Locator | None = None
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class EvaluateResponse(BaseModel):
    text: str


class WaitForRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"
    timeout: int = Field(default=10000, ge=1, le=_TIMEOUT_MAX)


class WaitForLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_until: WaitUntil = "networkidle"
    timeout: int = Field(default=30000, ge=1, le=_TIMEOUT_MAX)


# ---- read primitives -----------------------------------------------------------


class GetTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator | None = None
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class GetHtmlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator | None = None
    outer: bool = False
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class GetAttributeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator
    attribute: str = Field(min_length=1)
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class ElementTextResponse(BaseModel):
    text: str


class ElementHtmlResponse(BaseModel):
    text: str


class ElementAttributeResponse(BaseModel):
    text: str


# ---- find ----------------------------------------------------------------------


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: Locator


class ElementInfo(BaseModel):
    tag: str
    text: str
    attributes: dict[str, str]


class FindResultResponse(BaseModel):
    count: int
    elements: list[ElementInfo]


# ---- screenshot ----------------------------------------------------------------


class ScreenshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_page: bool = False
    locator: Locator | None = None
    timeout: int = Field(default=5000, ge=1, le=_TIMEOUT_MAX)


class ScreenshotResponse(BaseModel):
    image: str


# ---- console / network ---------------------------------------------------------


class ConsoleEntry(BaseModel):
    type: str
    text: str
    location: str | None = None


class ConsoleLogsResponse(BaseModel):
    count: int
    entries: list[ConsoleEntry]


class NetworkEntry(BaseModel):
    method: str
    url: str
    status: int | None = None
    resource_type: str | None = None


class NetworkLogResponse(BaseModel):
    count: int
    entries: list[NetworkEntry]


class NetworkFilterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url_pattern: str | None = None
    resource_type: str | None = None
    status_min: int | None = None
    status_max: int | None = None
    clear: bool = False


# ---- cookies / localStorage ----------------------------------------------------


class CookieModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    domain: str | None = None
    path: str | None = None
    expires: float | None = None
    http_only: bool | None = None
    secure: bool | None = None
    same_site: Literal["Strict", "Lax", "None"] | None = None


class CookiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["get", "set", "clear"]
    cookies: list[CookieModel] | None = None

    @model_validator(mode="after")
    def _cookies_required_for_set(self) -> "CookiesRequest":
        if self.action == "set" and not self.cookies:
            raise ValueError("action 'set' requires a non-empty cookies list")
        return self


class CookiesResponse(BaseModel):
    text: str


class LocalStorageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["get", "set", "remove", "clear"]
    key: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _per_action_required_fields(self) -> "LocalStorageRequest":
        if self.action in ("set", "remove") and not self.key:
            raise ValueError(f"action '{self.action}' requires 'key'")
        if self.action == "set" and self.value is None:
            raise ValueError("action 'set' requires 'value'")
        return self


class LocalStorageResponse(BaseModel):
    text: str


# ---- generic -------------------------------------------------------------------


class ActionResponse(BaseModel):
    status: Literal["ok"] = "ok"
    message: str
