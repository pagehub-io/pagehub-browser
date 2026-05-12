"""In-memory FakeEngine / FakeEngineSession test doubles + a FakeClock for the reaper."""

from __future__ import annotations

import asyncio
from typing import Callable

from api.engine.base import (
    ConsoleLogEntry,
    CookieSpec,
    ElementInfo,
    Engine,
    EngineSession,
    Locator,
    NavigateResult,
    NetworkLogEntry,
    SessionOpts,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeEngineSession(EngineSession):
    def __init__(self) -> None:
        self.closed = False
        self.navigate_calls: list[tuple[str, str, int]] = []
        self._url: str | None = None
        self._console: list[ConsoleLogEntry] = [ConsoleLogEntry(type="log", text="fake-console")]
        self._network: list[NetworkLogEntry] = [
            NetworkLogEntry(method="GET", url="https://example.com/", status=200, resource_type="document")
        ]
        self._local_storage: dict[str, str] = {}
        self._cookies: list[dict] = []
        # Per-verb override: set to an exception instance to raise it, or a value to return.
        self.raise_on: dict[str, Exception] = {}
        # An optional asyncio.Event a verb will await before completing (for concurrency tests).
        self.block_on: dict[str, asyncio.Event] = {}
        # find() returns this many ElementInfo entries.
        self.find_count = 1
        # navigate result overrides
        self.navigate_status = 200
        self.navigate_title = "Example Domain"

    async def _gate(self, verb: str) -> None:
        ev = self.block_on.get(verb)
        if ev is not None:
            await ev.wait()
        exc = self.raise_on.get(verb)
        if exc is not None:
            raise exc

    async def navigate(self, url: str, wait_until: str, timeout: int) -> NavigateResult:
        await self._gate("navigate")
        self.navigate_calls.append((url, wait_until, timeout))
        self._url = url
        return NavigateResult(
            url=url,
            status=self.navigate_status,
            title=self.navigate_title,
            text=f"Navigated to {url} (status: {self.navigate_status}). Title: {self.navigate_title}",
        )

    async def click(self, locator: Locator, timeout: int) -> None:
        await self._gate("click")

    async def type(self, locator: Locator, text: str, clear: bool, timeout: int) -> None:
        await self._gate("type")

    async def select(self, locator: Locator, value: str, timeout: int) -> None:
        await self._gate("select")

    async def hover(self, locator: Locator, timeout: int) -> None:
        await self._gate("hover")

    async def press_key(self, key: str, locator: Locator | None, timeout: int) -> None:
        await self._gate("press_key")

    async def get_text(self, locator: Locator | None, timeout: int) -> str:
        await self._gate("get_text")
        return "Example Domain heading text"

    async def get_html(self, locator: Locator | None, outer: bool, timeout: int) -> str:
        await self._gate("get_html")
        return "<html><body><h1>Example Domain</h1></body></html>"

    async def get_attribute(self, locator: Locator, attribute: str, timeout: int) -> str:
        await self._gate("get_attribute")
        return "https://www.iana.org/domains/example"

    async def find(self, locator: Locator) -> list[ElementInfo]:
        await self._gate("find")
        return [
            ElementInfo(tag="a", text=f"link {i}", attributes={"href": f"https://example.com/{i}"})
            for i in range(self.find_count)
        ]

    async def wait_for_element(self, locator: Locator, state: str, timeout: int) -> None:
        await self._gate("wait_for_element")

    async def wait_for_load(self, wait_until: str, timeout: int) -> None:
        await self._gate("wait_for_load")

    async def screenshot(self, locator: Locator | None, full_page: bool, timeout: int) -> str:
        await self._gate("screenshot")
        return "data:image/png;base64,iVBORw0KGgo="

    async def evaluate(self, expression: str, timeout: int) -> str:
        await self._gate("evaluate")
        return "Example Domain"

    async def console_logs(self, clear: bool) -> list[ConsoleLogEntry]:
        await self._gate("console_logs")
        entries = list(self._console)
        if clear:
            self._console.clear()
        return entries

    async def network_log(self, clear: bool) -> list[NetworkLogEntry]:
        await self._gate("network_log")
        entries = list(self._network)
        if clear:
            self._network.clear()
        return entries

    async def network_filter(
        self,
        url_pattern: str | None,
        resource_type: str | None,
        status_min: int | None,
        status_max: int | None,
        clear: bool,
    ) -> list[NetworkLogEntry]:
        await self._gate("network_filter")
        result = [
            e
            for e in self._network
            if (not url_pattern or url_pattern in e.url)
            and (not resource_type or e.resource_type == resource_type)
        ]
        if clear:
            self._network.clear()
        return result

    async def cookies(self, action: str, cookies: list[CookieSpec] | None) -> str:
        await self._gate("cookies")
        if action == "get":
            import json

            return json.dumps(self._cookies)
        if action == "clear":
            self._cookies.clear()
            return "All cookies cleared."
        if action == "set":
            for c in cookies or []:
                self._cookies.append({"name": c.name, "value": c.value})
            return f"Set {len(cookies or [])} cookie(s)."
        return "ok"

    async def local_storage(self, action: str, key: str | None, value: str | None) -> str:
        await self._gate("local_storage")
        if action == "get":
            if key:
                return self._local_storage.get(key, f"Key '{key}' not found.")
            import json

            return json.dumps(self._local_storage)
        if action == "set" and key is not None and value is not None:
            self._local_storage[key] = value
            return f"Set localStorage[{key!r}]."
        if action == "remove" and key is not None:
            self._local_storage.pop(key, None)
            return f"Removed localStorage[{key!r}]."
        if action == "clear":
            self._local_storage.clear()
            return "localStorage cleared."
        return "ok"

    async def current_url(self) -> str | None:
        return self._url

    async def close(self) -> None:
        if "close" in self.raise_on:
            raise self.raise_on["close"]
        self.closed = True


class FakeEngine(Engine):
    def __init__(self) -> None:
        self._alive = True
        self._browser_restarts_total = 0
        self.sessions: list[FakeEngineSession] = []
        self.new_session_raises: Exception | None = None
        # Optional hook: called inside new_session before the session is built (concurrency tests).
        self.on_new_session: Callable[[], None] | None = None

    async def new_session(self, opts: SessionOpts) -> EngineSession:
        if self.on_new_session is not None:
            self.on_new_session()
        # Yield the event loop so concurrent create() coroutines interleave (mirrors a real
        # browser context creation being awaitable) — exercises the create() double-check guard.
        await asyncio.sleep(0)
        if self.new_session_raises is not None:
            raise self.new_session_raises
        s = FakeEngineSession()
        self.sessions.append(s)
        return s

    def is_alive(self) -> bool:
        return self._alive

    def browser_restarts_total(self) -> int:
        return self._browser_restarts_total

    async def close(self) -> None:
        pass
