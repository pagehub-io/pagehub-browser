"""Playwright/Chromium implementation of the Engine interface — the only shipped engine in v1."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from api.config import settings
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
from api.engine.errors import (
    ActionTimeout,
    AttributeNotPresent,
    BlockedNavigation,
    ElementNotFound,
    EngineCrash,
    InvalidLocator,
    InvalidLocatorSyntax,
    LocatorAmbiguous,
    NavigationError,
    RuntimeEvalError,
)
from api.ssrf import navigation_request_is_blocked

logger = logging.getLogger(__name__)

_CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_FIND_ATTRS_JS = """el => {
    const out = {};
    for (const a of ['id','class','href','src','data-testid','type','name','value','role','aria-label','alt','title']) {
        if (el.hasAttribute(a)) out[a] = el.getAttribute(a);
    }
    return out;
}"""


def _is_dead_target_error(message: str) -> bool:
    needles = (
        "target page, context or browser has been closed",
        "target closed",
        "browser has been closed",
        "connection closed",
        "browser has disconnected",
    )
    low = message.lower()
    return any(n in low for n in needles)


def _classify_playwright_error(exc: PlaywrightError, *, locator: Locator | None = None) -> Exception:
    msg = exc.message if hasattr(exc, "message") else str(exc)
    if isinstance(exc, PlaywrightTimeoutError):
        if locator is not None:
            return ActionTimeout(f"Timed out waiting for {locator.repr_str()}.")
        return ActionTimeout("Timed out.")
    if _is_dead_target_error(msg):
        return EngineCrash(msg)
    return RuntimeEvalError(msg)


class PlaywrightEngineSession(EngineSession):
    def __init__(self, engine: "PlaywrightEngine", context: BrowserContext, page: Page) -> None:
        self._engine = engine
        self._context = context
        self._page = page
        self._console: deque[ConsoleLogEntry] = deque(maxlen=settings.max_log_entries)
        self._network: deque[NetworkLogEntry] = deque(maxlen=settings.max_log_entries)
        self._last_blocked_nav: str | None = None
        page.on("console", self._on_console)
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    async def _navigation_interceptor(self, route: Route) -> None:
        request = route.request
        try:
            if request.is_navigation_request() and navigation_request_is_blocked(request.url):
                self._last_blocked_nav = request.url
                await route.abort("aborted")
                return
            await route.continue_()
        except PlaywrightError:
            # Route already handled / page gone — nothing to do.
            pass

    # ---- listeners ----

    def _on_console(self, msg: ConsoleMessage) -> None:
        loc = msg.location or {}
        location = None
        if loc.get("url"):
            location = f"{loc.get('url', '')}:{loc.get('lineNumber', '')}"
        self._console.append(ConsoleLogEntry(type=msg.type, text=msg.text, location=location))

    def _on_request(self, request: Request) -> None:
        self._network.append(
            NetworkLogEntry(method=request.method, url=request.url, resource_type=request.resource_type)
        )

    def _on_response(self, response: Response) -> None:
        for entry in reversed(self._network):
            if entry.url == response.url and entry.status is None:
                entry.status = response.status
                break

    # ---- locator resolution ----

    def _resolve(self, loc: Locator):
        opts = loc.options
        name = opts.name if opts else None
        exact = opts.exact if opts else None
        try:
            if loc.strategy == "role":
                kwargs: dict[str, Any] = {}
                if name is not None:
                    kwargs["name"] = name
                if exact is not None:
                    kwargs["exact"] = exact
                pw_loc = self._page.get_by_role(loc.value, **kwargs)  # type: ignore[arg-type]
            elif loc.strategy == "text":
                pw_loc = self._page.get_by_text(loc.value, **({"exact": exact} if exact is not None else {}))
            elif loc.strategy == "label":
                pw_loc = self._page.get_by_label(loc.value, **({"exact": exact} if exact is not None else {}))
            elif loc.strategy == "placeholder":
                pw_loc = self._page.get_by_placeholder(
                    loc.value, **({"exact": exact} if exact is not None else {})
                )
            elif loc.strategy == "testid":
                pw_loc = self._page.get_by_test_id(loc.value)
            elif loc.strategy == "alt":
                pw_loc = self._page.get_by_alt_text(loc.value, **({"exact": exact} if exact is not None else {}))
            elif loc.strategy == "title":
                pw_loc = self._page.get_by_title(loc.value, **({"exact": exact} if exact is not None else {}))
            elif loc.strategy == "css":
                pw_loc = self._page.locator(loc.value)
            elif loc.strategy == "xpath":
                pw_loc = self._page.locator(f"xpath={loc.value}")
            else:
                raise InvalidLocator(
                    f"Unsupported locator strategy '{loc.strategy}'; expected one of "
                    "role,text,label,placeholder,testid,alt,title,css,xpath."
                )
        except PlaywrightError as exc:
            if loc.strategy in ("css", "xpath"):
                raise InvalidLocatorSyntax(loc.strategy, loc.value, getattr(exc, "message", str(exc)))
            raise

        if opts is not None and opts.has_text is not None:
            pw_loc = pw_loc.filter(has_text=opts.has_text)
        if opts is not None and opts.nth is not None:
            pw_loc = pw_loc.nth(opts.nth)
        return pw_loc

    async def _count(self, pw_loc, loc: Locator) -> int:
        try:
            return await pw_loc.count()
        except PlaywrightError as exc:
            if loc.strategy in ("css", "xpath"):
                raise InvalidLocatorSyntax(loc.strategy, loc.value, getattr(exc, "message", str(exc)))
            raise

    async def _resolve_single(self, loc: Locator, timeout: int):
        """Resolve to exactly one element: 0 -> ElementNotFound, >1 (no nth) -> LocatorAmbiguous."""
        pw_loc = self._resolve(loc)
        if loc.options is not None and loc.options.nth is not None:
            return pw_loc
        count = await self._count(pw_loc, loc)
        if count == 0:
            raise ElementNotFound(loc.repr_str())
        if count > 1:
            raise LocatorAmbiguous(count)
        return pw_loc

    # ---- navigate ----

    async def navigate(self, url: str, wait_until: str, timeout: int) -> NavigateResult:
        self._last_blocked_nav = None
        try:
            response = await self._page.goto(url, wait_until=wait_until, timeout=timeout)  # type: ignore[arg-type]
        except PlaywrightTimeoutError as exc:
            raise ActionTimeout(f"Timed out waiting for page to reach {wait_until}.") from exc
        except PlaywrightError as exc:
            msg = getattr(exc, "message", str(exc))
            if self._last_blocked_nav is not None:
                raise BlockedNavigation(self._last_blocked_nav) from exc
            if _is_dead_target_error(msg):
                raise EngineCrash(msg) from exc
            raise NavigationError(url, msg) from exc

        if response is None:
            try:
                title = await self._page.title()
            except PlaywrightError:
                title = ""
            return NavigateResult(
                url=self._page.url,
                status=None,
                title=title,
                text=f"Navigated to {url} but it triggered a download (no page load).",
            )
        try:
            title = await self._page.title()
        except PlaywrightError:
            title = ""
        final_url = self._page.url
        status = response.status
        return NavigateResult(
            url=final_url,
            status=status,
            title=title,
            text=f"Navigated to {final_url} (status: {status}). Title: {title}",
        )

    # ---- interaction ----

    async def click(self, locator: Locator, timeout: int) -> None:
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            await pw_loc.click(timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def type(self, locator: Locator, text: str, clear: bool, timeout: int) -> None:
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            if clear:
                await pw_loc.fill(text, timeout=timeout)
            else:
                await pw_loc.press_sequentially(text, timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def select(self, locator: Locator, value: str, timeout: int) -> None:
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            await pw_loc.select_option(value, timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def hover(self, locator: Locator, timeout: int) -> None:
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            await pw_loc.hover(timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def press_key(self, key: str, locator: Locator | None, timeout: int) -> None:
        try:
            if locator is None:
                await self._page.keyboard.press(key)
            else:
                pw_loc = await self._resolve_single(locator, timeout)
                await pw_loc.press(key, timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def evaluate(self, expression: str, timeout: int) -> str:
        try:
            result = await asyncio.wait_for(self._page.evaluate(expression), timeout=timeout / 1000)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ActionTimeout(f"Timed out after {timeout}ms running evaluate.") from exc
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc) from exc
        if isinstance(result, (dict, list)):
            return json.dumps(result, indent=2)
        return str(result)

    async def wait_for_element(self, locator: Locator, state: str, timeout: int) -> None:
        pw_loc = self._resolve(locator)
        try:
            await pw_loc.wait_for(state=state, timeout=timeout)  # type: ignore[arg-type]
        except PlaywrightTimeoutError as exc:
            if state in ("hidden", "detached"):
                # Already absent -> success per the SPEC negative-state rule.
                count = await self._count(pw_loc, locator)
                if count == 0:
                    return
            raise ActionTimeout(
                f"Timed out after {timeout}ms waiting for {locator.repr_str()} to be {state}."
            ) from exc
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def wait_for_load(self, wait_until: str, timeout: int) -> None:
        try:
            await self._page.wait_for_load_state(wait_until, timeout=timeout)  # type: ignore[arg-type]
        except PlaywrightTimeoutError as exc:
            raise ActionTimeout(f"Timed out after {timeout}ms waiting for page to reach {wait_until}.") from exc
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc) from exc

    # ---- read primitives ----

    async def get_text(self, locator: Locator | None, timeout: int) -> str:
        if locator is None:
            try:
                return await self._page.inner_text("body", timeout=timeout)
            except PlaywrightError as exc:
                raise _classify_playwright_error(exc) from exc
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            text = await pw_loc.text_content(timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc
        return text or ""

    async def get_html(self, locator: Locator | None, outer: bool, timeout: int) -> str:
        if locator is None:
            try:
                return await self._page.evaluate("() => document.documentElement.outerHTML")
            except PlaywrightError as exc:
                raise _classify_playwright_error(exc) from exc
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            if outer:
                return await pw_loc.evaluate("el => el.outerHTML")
            return await pw_loc.inner_html(timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc

    async def get_attribute(self, locator: Locator, attribute: str, timeout: int) -> str:
        pw_loc = await self._resolve_single(locator, timeout)
        try:
            value = await pw_loc.get_attribute(attribute, timeout=timeout)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc
        if value is None:
            raise AttributeNotPresent(attribute, locator.repr_str())
        return value

    async def find(self, locator: Locator) -> list[ElementInfo]:
        pw_loc = self._resolve(locator)
        try:
            count = await self._count(pw_loc, locator)
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc
        out: list[ElementInfo] = []
        for i in range(min(count, 50)):
            el = pw_loc.nth(i)
            try:
                handle = await el.element_handle(timeout=5000)
                if handle is None:
                    continue
                tag = (await handle.evaluate("el => el.tagName.toLowerCase()")) or ""
                text = ((await handle.text_content()) or "").strip()[:200]
                attrs = await handle.evaluate(_FIND_ATTRS_JS)
            except PlaywrightError:
                continue
            out.append(ElementInfo(tag=tag, text=text, attributes=attrs or {}))
        return out

    # ---- screenshot ----

    async def screenshot(self, locator: Locator | None, full_page: bool, timeout: int) -> str:
        import base64

        try:
            if locator is None:
                raw = await self._page.screenshot(type="png", full_page=full_page, timeout=timeout)
            else:
                pw_loc = await self._resolve_single(locator, timeout)
                raw = await pw_loc.screenshot(type="png", timeout=timeout)
        except PlaywrightTimeoutError as exc:
            if locator is not None:
                raise ActionTimeout(f"Timed out after {timeout}ms waiting for {locator.repr_str()}.") from exc
            raise ActionTimeout(f"Timed out after {timeout}ms taking a screenshot.") from exc
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc, locator=locator) from exc
        return f"data:image/png;base64,{base64.b64encode(raw).decode()}"

    # ---- console / network ----

    async def console_logs(self, clear: bool) -> list[ConsoleLogEntry]:
        entries = list(self._console)
        if clear:
            self._console.clear()
        return entries

    async def network_log(self, clear: bool) -> list[NetworkLogEntry]:
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
        result: list[NetworkLogEntry] = []
        for e in list(self._network):
            if url_pattern and url_pattern not in e.url:
                continue
            if resource_type and e.resource_type != resource_type:
                continue
            if status_min is not None and (e.status is None or e.status < status_min):
                continue
            if status_max is not None and (e.status is None or e.status > status_max):
                continue
            result.append(e)
        if clear:
            self._network.clear()
        return result

    # ---- cookies / localStorage ----

    async def cookies(self, action: str, cookies: list[CookieSpec] | None) -> str:
        try:
            if action == "get":
                return json.dumps(await self._context.cookies(), indent=2)
            if action == "clear":
                await self._context.clear_cookies()
                return "All cookies cleared."
            if action == "set":
                payload: list[dict[str, Any]] = []
                for c in cookies or []:
                    item: dict[str, Any] = {"name": c.name, "value": c.value}
                    if c.domain is not None:
                        item["domain"] = c.domain
                    if c.path is not None:
                        item["path"] = c.path
                    if c.expires is not None:
                        item["expires"] = c.expires
                    if c.http_only is not None:
                        item["httpOnly"] = c.http_only
                    if c.secure is not None:
                        item["secure"] = c.secure
                    if c.same_site is not None:
                        item["sameSite"] = c.same_site
                    if c.domain is None and c.path is None:
                        item["url"] = self._page.url
                    payload.append(item)
                await self._context.add_cookies(payload)  # type: ignore[arg-type]
                return f"Set {len(payload)} cookie(s)."
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc) from exc
        raise RuntimeEvalError(f"unsupported cookies action {action!r}")

    async def local_storage(self, action: str, key: str | None, value: str | None) -> str:
        try:
            if action == "get":
                if key:
                    result = await self._page.evaluate(
                        "k => window.localStorage.getItem(k)", key
                    )
                    return result if result is not None else f"Key '{key}' not found."
                return await self._page.evaluate(
                    "() => JSON.stringify(Object.fromEntries(Object.entries(window.localStorage)))"
                )
            if action == "set":
                await self._page.evaluate(
                    "([k, v]) => window.localStorage.setItem(k, v)", [key, value]
                )
                return f"Set localStorage[{key!r}]."
            if action == "remove":
                await self._page.evaluate("k => window.localStorage.removeItem(k)", key)
                return f"Removed localStorage[{key!r}]."
            if action == "clear":
                await self._page.evaluate("() => window.localStorage.clear()")
                return "localStorage cleared."
        except PlaywrightError as exc:
            raise _classify_playwright_error(exc) from exc
        raise RuntimeEvalError(f"unsupported local-storage action {action!r}")

    async def current_url(self) -> str | None:
        try:
            url = self._page.url
        except PlaywrightError:
            return None
        if not url or url == "about:blank":
            return None
        return url

    async def close(self) -> None:
        try:
            await self._context.close()
        except PlaywrightError:
            pass


class PlaywrightEngine(Engine):
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_restarts_total = 0
        self._launch_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=settings.headless, args=_CHROMIUM_ARGS
            )
            self._browser.on("disconnected", self._on_disconnected)
            return self._browser

    def _on_disconnected(self, _browser: Browser) -> None:
        self._browser_restarts_total += 1
        self._browser = None
        logger.warning("shared chromium disconnected; will relaunch on next session-create")

    async def new_session(self, opts: SessionOpts) -> EngineSession:
        try:
            browser = await self._ensure_browser()
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": opts.viewport_width, "height": opts.viewport_height},
            }
            if opts.user_agent:
                context_kwargs["user_agent"] = opts.user_agent
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            session = PlaywrightEngineSession(self, context, page)
            await context.route("**/*", session._navigation_interceptor)
        except PlaywrightError as exc:
            raise EngineCrash(getattr(exc, "message", str(exc))) from exc
        return session

    def is_alive(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def browser_restarts_total(self) -> int:
        return self._browser_restarts_total

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except PlaywrightError:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except PlaywrightError:
            pass
        self._browser = None
        self._pw = None
