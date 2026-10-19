"""Request (correlation) IDs — see app/core/request_id.py."""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.logging import RequestIdFilter
from app.core.request_id import (
    NO_REQUEST,
    REQUEST_ID_HEADER,
    _resolve,
    current_request_id,
    request_id_var,
)


async def test_every_response_carries_a_generated_request_id(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers[REQUEST_ID_HEADER]
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_a_well_formed_incoming_id_is_kept(client: AsyncClient) -> None:
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "lb-trace.42_a"})

    assert response.headers[REQUEST_ID_HEADER] == "lb-trace.42_a"


async def test_the_id_does_not_outlive_its_request(client: AsyncClient) -> None:
    """HTTPX's ASGITransport runs the app inside *this* task. A request ID left
    set afterwards would stamp a finished request onto unrelated log lines."""
    await client.get("/health", headers={REQUEST_ID_HEADER: "finished-request"})

    assert current_request_id() == NO_REQUEST


@pytest.mark.parametrize(
    "hostile",
    [
        "a" * 65,  # too long to be anyone's ID
        "abc\nlevel=ERROR forged",  # would forge a log line
        "<script>",
        "",
    ],
)
def test_an_unsafe_incoming_id_is_replaced(hostile: str) -> None:
    resolved = _resolve(hostile)

    assert resolved != hostile
    assert len(resolved) == 32


async def test_an_unhandled_error_returns_the_id_so_support_can_find_the_log() -> None:
    from app.main import create_app

    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        response = await raw.get("/boom", headers={REQUEST_ID_HEADER: "support-ticket-1"})

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "support-ticket-1"
    body = response.json()["error"]
    assert body["request_id"] == "support-ticket-1"
    # Still no internals on a hospital workstation.
    assert "kaboom" not in response.text


def test_log_records_are_stamped_with_the_current_request_id() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)

    token = request_id_var.set("req-123")
    try:
        RequestIdFilter().filter(record)
    finally:
        request_id_var.reset(token)

    assert record.request_id == "req-123"  # type: ignore[attr-defined]


def test_log_records_outside_a_request_get_a_placeholder() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)

    RequestIdFilter().filter(record)

    assert record.request_id == NO_REQUEST  # type: ignore[attr-defined]
