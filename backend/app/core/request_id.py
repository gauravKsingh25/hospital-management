"""Request (correlation) IDs: one identifier per HTTP request, on every log line.

Why this exists: a receptionist reports "billing failed at 11:40". Without a
correlation ID, finding that request means guessing among every log line from
that minute. With one, the error response carries the ID, the receptionist (or
the frontend) quotes it, and one search finds every log line the request wrote
— including those from in-process event handlers, which run inside the same
request and therefore inherit the ID automatically.

How it flows:
    incoming `X-Request-ID` (if well-formed) or a fresh one
      -> `scope["state"]["request_id"]` (survives to the outermost 500 handler)
      -> `request_id_var` (a ContextVar, read by the logging filter)
      -> `X-Request-ID` response header
      -> `request_id` field in 500 responses (see core/exceptions.py)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

# Shown on log lines written outside any request (startup, workers, scripts).
NO_REQUEST: Final[str] = "-"

# Only IDs that are safe to echo into a log line are accepted from the caller.
# Anything else — too long, or carrying newlines that could forge log entries —
# is ignored and replaced with a fresh ID.
_ACCEPTED_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")

request_id_var: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST)

# Where the ID is kept on the ASGI scope (as `request.state.request_id`).
_SCOPE_KEY: Final[str] = "request_id"


def current_request_id() -> str:
    """The ID of the request being handled, or `-` outside a request."""
    return request_id_var.get()


def request_id_from_scope(scope: Scope) -> str:
    """The ID `RequestIdMiddleware` stored on this request, or `-`."""
    state = scope.get("state") or {}
    return str(state.get(_SCOPE_KEY, NO_REQUEST))


@contextmanager
def bound_request_id(request_id: str) -> Iterator[None]:
    """Make `request_id` the current one for the block, then restore the old one."""
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


def _resolve(incoming: str | None) -> str:
    if incoming and _ACCEPTED_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Pure-ASGI middleware that assigns and propagates the request ID.

    Deliberately not `BaseHTTPMiddleware`: that wrapper runs the app in a
    separate task, which breaks ContextVar propagation and buffers streaming
    responses.

    The ContextVar is restored when the request finishes, so the ID never
    leaks into later, unrelated log lines from the same task (HTTPX's
    `ASGITransport`, for one, runs the app inside the caller's task).

    That restore happens *before* Starlette's `ServerErrorMiddleware` — which
    sits outside every user middleware — handles an uncaught exception. So the
    ID is also stored on the scope, and the 500 handler re-binds it from there
    (`request_id_from_scope`) for its log line and response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming: str | None = None
        for name, value in scope.get("headers", ()):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = _resolve(incoming)
        scope.setdefault("state", {})[_SCOPE_KEY] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with bound_request_id(request_id):
            await self.app(scope, receive, send_with_id)
