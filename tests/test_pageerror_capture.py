"""Uncaught page errors land in the console buffer as type "pageerror".

Playwright reports uncaught exceptions and unhandled rejections on the `pageerror` event, never
on `console`, so an eval asserting "no console entries" used to pass on a page whose script had
crashed. The real-Chromium check (sandboxed iframe included) is in test_browser_integration.py.
"""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

from api.engine.playwright_engine import PlaywrightEngineSession


class _RecordingPage:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:  # noqa: ANN001
        self.handlers[event] = handler


class _NamedError:
    """Shape of a page error that carries a JS error name (e.g. TypeError)."""

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message


def _session() -> tuple[PlaywrightEngineSession, _RecordingPage]:
    page = _RecordingPage()
    return PlaywrightEngineSession(engine=None, browser=None, context=None, page=page), page


def test_session_listens_for_pageerror():
    _, page = _session()
    assert "pageerror" in page.handlers
    assert "console" in page.handlers


@pytest.mark.asyncio
async def test_pageerror_is_recorded_with_its_js_name():
    session, page = _session()
    page.handlers["pageerror"](_NamedError("TypeError", "x is not a function"))
    page.handlers["pageerror"](PlaywrightError("boom"))
    entries = await session.console_logs(clear=False)
    assert [(e.type, e.text) for e in entries] == [
        ("pageerror", "TypeError: x is not a function"),
        ("pageerror", "boom"),
    ]


@pytest.mark.asyncio
async def test_pageerror_name_is_not_doubled_when_the_message_already_has_it():
    session, page = _session()
    page.handlers["pageerror"](_NamedError("Error", "Error: already prefixed"))
    assert [e.text for e in await session.console_logs(clear=True)] == ["Error: already prefixed"]
    assert await session.console_logs(clear=False) == []
