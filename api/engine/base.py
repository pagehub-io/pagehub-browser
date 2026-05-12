"""The pluggable Engine interface. The HTTP layer only ever touches this; it never imports playwright."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Literal

LocatorStrategy = Literal[
    "role", "text", "label", "placeholder", "testid", "alt", "title", "css", "xpath"
]

LOCATOR_STRATEGIES: tuple[str, ...] = (
    "role", "text", "label", "placeholder", "testid", "alt", "title", "css", "xpath",
)


@dataclass(frozen=True)
class LocatorOptions:
    name: str | None = None
    exact: bool | None = None
    nth: int | None = None
    has_text: str | None = None


@dataclass(frozen=True)
class Locator:
    strategy: str
    value: str
    options: LocatorOptions | None = None

    def repr_str(self) -> str:
        opts = self.options
        suffix = ""
        if opts is not None:
            parts: list[str] = []
            if opts.name is not None:
                parts.append(f"name={opts.name!r}")
            if opts.nth is not None:
                parts.append(f"nth={opts.nth}")
            if opts.has_text is not None:
                parts.append(f"has_text={opts.has_text!r}")
            if parts:
                suffix = " (" + ", ".join(parts) + ")"
        return f"{self.strategy}={self.value}{suffix}"


@dataclass
class SessionOpts:
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    user_agent: str | None = None


@dataclass
class ElementInfo:
    tag: str
    text: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class ConsoleLogEntry:
    type: str
    text: str
    location: str | None = None


@dataclass
class NetworkLogEntry:
    method: str
    url: str
    status: int | None = None
    resource_type: str | None = None


@dataclass
class NavigateResult:
    url: str
    status: int | None
    title: str
    text: str


@dataclass
class CookieSpec:
    name: str
    value: str
    domain: str | None = None
    path: str | None = None
    expires: float | None = None
    http_only: bool | None = None
    secure: bool | None = None
    same_site: str | None = None


class EngineSession(abc.ABC):
    """A single isolated browser session (a BrowserContext + Page in the Playwright impl)."""

    @abc.abstractmethod
    async def navigate(self, url: str, wait_until: str) -> NavigateResult: ...

    @abc.abstractmethod
    async def click(self, locator: Locator, timeout: int) -> None: ...

    @abc.abstractmethod
    async def type(self, locator: Locator, text: str, clear: bool, timeout: int) -> None: ...

    @abc.abstractmethod
    async def select(self, locator: Locator, value: str, timeout: int) -> None: ...

    @abc.abstractmethod
    async def hover(self, locator: Locator, timeout: int) -> None: ...

    @abc.abstractmethod
    async def press_key(self, key: str, locator: Locator | None, timeout: int) -> None: ...

    @abc.abstractmethod
    async def get_text(self, locator: Locator | None, timeout: int) -> str: ...

    @abc.abstractmethod
    async def get_html(self, locator: Locator | None, outer: bool, timeout: int) -> str: ...

    @abc.abstractmethod
    async def get_attribute(self, locator: Locator, attribute: str, timeout: int) -> str: ...

    @abc.abstractmethod
    async def find(self, locator: Locator) -> list[ElementInfo]: ...

    @abc.abstractmethod
    async def wait_for_element(self, locator: Locator, state: str, timeout: int) -> None: ...

    @abc.abstractmethod
    async def wait_for_load(self, wait_until: str, timeout: int) -> None: ...

    @abc.abstractmethod
    async def screenshot(self, locator: Locator | None, full_page: bool, timeout: int) -> str: ...

    @abc.abstractmethod
    async def evaluate(self, expression: str, timeout: int) -> str: ...

    @abc.abstractmethod
    async def console_logs(self, clear: bool) -> list[ConsoleLogEntry]: ...

    @abc.abstractmethod
    async def network_log(self, clear: bool) -> list[NetworkLogEntry]: ...

    @abc.abstractmethod
    async def network_filter(
        self,
        url_pattern: str | None,
        resource_type: str | None,
        status_min: int | None,
        status_max: int | None,
        clear: bool,
    ) -> list[NetworkLogEntry]: ...

    @abc.abstractmethod
    async def cookies(self, action: str, cookies: list[CookieSpec] | None) -> str: ...

    @abc.abstractmethod
    async def local_storage(self, action: str, key: str | None, value: str | None) -> str: ...

    @abc.abstractmethod
    async def current_url(self) -> str | None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class Engine(abc.ABC):
    @abc.abstractmethod
    async def new_session(self, opts: SessionOpts) -> EngineSession: ...

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """True iff the shared browser (if any) is launched and still connected.

        The SessionManager uses this to detect a shared-browser death; the reaper's
        self-correcting sweep drops all records when this returns False.
        """

    @abc.abstractmethod
    def browser_restarts_total(self) -> int:
        """Count of detected shared-Browser deaths (browser.on('disconnected') firings)."""

    @abc.abstractmethod
    async def close(self) -> None: ...
