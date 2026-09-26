from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.database import check_database, dispose_engine


async def test_liveness_needs_no_dependencies(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_unknown_route_returns_a_structured_error(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404


@pytest.mark.integration
async def test_connects_to_the_configured_database() -> None:
    """Phase 0 acceptance: the app really reaches Neon.

    Deselect with `-m "not integration"` when offline.
    """
    try:
        result = await check_database()
    finally:
        await dispose_engine()

    assert result["status"] == "ok"
    assert int(result["server_version"].split(".")[0]) >= 16
