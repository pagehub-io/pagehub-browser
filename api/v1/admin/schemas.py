"""Pydantic schemas for /v1/admin/*."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResetSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=512)


class ResetSessionsResponse(BaseModel):
    closed: int
    reason: str
