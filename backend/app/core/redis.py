"""Redis client — backing store for the ARQ task queue and short-lived caches.

Phase 0 only establishes and health-checks the connection. The workers that use
it (encounter auto-close timeout, notification dispatch) arrive with their
modules.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


def get_redis() -> Redis:
    """Process-wide Redis client singleton."""
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
        logger.info("redis client created url=%s", settings.REDIS_URL)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        logger.info("redis client closed")
    _client = None


async def check_redis() -> dict[str, Any]:
    started = time.perf_counter()
    pong = await get_redis().ping()
    return {
        "status": "ok" if pong else "error",
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
