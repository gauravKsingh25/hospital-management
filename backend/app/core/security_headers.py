"""Security headers on every API response (SEC-09).

The web app sets its own, strict ones; the API set none. That protected only
the people who reach the API through the web app — not an insider calling
:8000 directly, a misconfigured proxy, or a browser pointed at a JSON URL.

Pure ASGI rather than `BaseHTTPMiddleware`: it only adds headers to the
response start message, so it neither buffers bodies nor breaks streaming.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

__all__ = ["SecurityHeadersMiddleware"]

# Interactive docs (local only) load Swagger from a CDN and run inline script,
# so they are the one place the strict policy is relaxed.
_DOCS_PATHS = ("/docs", "/redoc")

_ALWAYS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)
# The API serves JSON, never a page: nothing may load, frame or run from it.
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
# Patient data must not be kept by a shared tablet's browser or an
# intermediate cache.
_NO_STORE = (b"cache-control", b"no-store")
# Two years, the preload-list minimum. Only outside local development: a
# laptop on plain http ignores it anyway, and a developer who turns TLS on
# locally should not find `localhost` pinned to https for two years.
_HSTS = (b"strict-transport-security", b"max-age=63072000; includeSubDomains")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        extra: list[tuple[bytes, bytes]] = list(_ALWAYS)
        if not path.startswith(_DOCS_PATHS):
            extra.append(_API_CSP)
        if path.startswith(settings.API_V1_PREFIX):
            extra.append(_NO_STORE)
        if settings.ENVIRONMENT != "local":
            extra.append(_HSTS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(header for header in extra if header[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
