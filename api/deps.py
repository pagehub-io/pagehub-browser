"""FastAPI dependencies — pull shared state off app.state."""

from __future__ import annotations

from fastapi import Request

from api.session_manager import SessionManager


def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager
