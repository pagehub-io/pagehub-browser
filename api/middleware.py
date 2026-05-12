"""ASGI middleware: X-Twin-* dev-only gate (no-op stub in v1) + request body size limit."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.config import settings

_DEV_ENV = "development"


class TwinHeaderMiddleware:
    """Scaffold per the global twin-override pattern.

    v1 has zero outbound *_BASE_URL deps, so this is a structural no-op — but the
    env gate must be correct: in any env other than `development` the X-Twin-*
    headers are silently stripped before any handler sees them, and their values
    are never logged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._active = settings.env == _DEV_ENV

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._active:
            await self.app(scope, receive, send)
            return
        headers = [
            (name, value)
            for (name, value) in scope.get("headers", [])
            if not name.lower().startswith(b"x-twin-")
        ]
        scope = dict(scope)
        scope["headers"] = headers
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject request bodies larger than REQUEST_BODY_MAX_BYTES with a 413.

    Checks Content-Length up front and counts streamed bytes (chunked / missing
    Content-Length) so an over-limit body can't OOM the JSON parser.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._reject(send)
                        return
                except ValueError:
                    pass
                break

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {"detail": f"Request body too large (limit {self.max_bytes} bytes)."}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass
