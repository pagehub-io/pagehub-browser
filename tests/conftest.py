"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ENGINE", "fake")
os.environ.setdefault("GIT_COMMIT", "test-sha")
os.environ.setdefault("LOCK_ACQUIRE_TIMEOUT_MS", "5000")

from fastapi.testclient import TestClient  # noqa: E402

from api.config import settings  # noqa: E402
from api.main import create_app  # noqa: E402
from api.session_manager import SessionManager  # noqa: E402
from tests.fakes import FakeClock, FakeEngine  # noqa: E402


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manager(fake_engine: FakeEngine, fake_clock: FakeClock) -> SessionManager:
    return SessionManager(
        fake_engine,
        max_sessions=settings.max_sessions,
        idle_timeout_default=settings.idle_timeout_seconds,
        idle_timeout_min=30,
        idle_timeout_max=settings.idle_timeout_max_seconds,
        reaper_interval=settings.reaper_interval_seconds,
        tombstone_max=settings.tombstone_max,
        clock=fake_clock,
    )


@pytest.fixture
def client():
    """A TestClient backed by a FakeEngine. The engine is reachable as client.app.state.engine."""
    app = create_app()
    app.state.engine = FakeEngine()
    with TestClient(app) as c:
        yield c
