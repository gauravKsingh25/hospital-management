"""SEC-09: the API map is a local-development convenience, and the API sends
its own security headers.

The docs were switched off only for `production`, so a staging deployment
published the full map of every route. And the API itself sent no security
headers at all — the web app's were the only ones, which protects nobody who
talks to :8000 directly.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings


async def _client() -> AsyncClient:
    from app.main import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
async def test_the_api_map_is_not_published_outside_local(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    async with await _client() as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path


async def test_local_development_keeps_the_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    async with await _client() as client:
        assert (await client.get("/openapi.json")).status_code == 200


async def test_every_response_carries_the_security_headers() -> None:
    async with await _client() as client:
        response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_api_responses_are_never_cached() -> None:
    """Patient data must not sit in a shared tablet's browser cache."""
    async with await _client() as client:
        response = await client.get("/api/v1/auth/me")
    assert response.headers["cache-control"] == "no-store"


async def test_hsts_is_sent_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    async with await _client() as client:
        response = await client.get("/health")
    assert response.headers["strict-transport-security"].startswith("max-age=")


async def test_hsts_is_not_sent_on_a_developers_laptop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    async with await _client() as client:
        response = await client.get("/health")
    assert "strict-transport-security" not in response.headers
