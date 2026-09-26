"""Redis cache for resolved RBAC, with immediate invalidation.

**What is cached and what deliberately is not.**

Only the *role/permission join* is cached — three tables, resolved on every
request. The `users` row itself is always re-read from the database. That split
is the whole design:

    users row      -> never cached  -> `is_active` is always the truth
    roles + perms  -> cached 60s    -> invalidated explicitly on every change

So the safety-critical question — "is this account still allowed to act at
all?" — never depends on cache correctness. A suspended clinician stops being
able to act on the very next request, with no invalidation step that could be
missed, forgotten, or lost to a Redis restart. Reading one row by primary key is
an index hit measured in microseconds; the join was the part worth removing.

**Two invalidation paths**, because RBAC changes have two blast radii:

* *One user changed* (roles reassigned, account deactivated) — drop that user's
  key. O(1), instant.
* *A role changed* (its permissions edited, a role retired) — that silently
  affects every user holding it, and enumerating them is exactly the kind of
  fan-out that gets missed. Instead a global generation counter is part of every
  cache key, so bumping it retires the entire cache in one write. Stale entries
  are never read again and expire on their own.

**Failure is not an outage.** Every operation here swallows Redis errors and
reports a miss. If Redis is down the system falls back to querying Postgres —
slower, entirely correct, and a hospital keeps running. A cache that can stop
admissions is worse than no cache.
"""

from __future__ import annotations

import json
import logging
from typing import Final

import redis.exceptions

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

# Safety net only. Correctness comes from explicit invalidation below; this TTL
# bounds the damage of an invalidation path we failed to think of.
CACHE_TTL_SECONDS: Final[int] = 60

_GENERATION_KEY: Final[str] = "rbac:generation"
_PREFIX: Final[str] = "rbac:perms"


async def _generation() -> str:
    """Current cache generation. Missing or unreachable Redis reads as '0'."""
    try:
        value = await get_redis().get(_GENERATION_KEY)
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.debug("rbac cache generation unavailable: %s", exc)
        return "0"
    return str(value) if value is not None else "0"


def _key(generation: str, user_id: str) -> str:
    return f"{_PREFIX}:{generation}:{user_id}"


async def get_permissions(user_id: str) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return cached (roles, permissions), or None on a miss."""
    try:
        raw = await get_redis().get(_key(await _generation(), user_id))
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.debug("rbac cache read failed, falling back to the database: %s", exc)
        return None

    if raw is None:
        return None

    try:
        payload = json.loads(raw)
        return frozenset(payload["roles"]), frozenset(payload["permissions"])
    except (ValueError, KeyError, TypeError):
        # A corrupt entry is a miss, not an error worth failing a request over.
        logger.warning("discarding malformed rbac cache entry for user %s", user_id)
        return None


async def set_permissions(user_id: str, roles: frozenset[str], permissions: frozenset[str]) -> None:
    payload = json.dumps({"roles": sorted(roles), "permissions": sorted(permissions)})
    try:
        await get_redis().set(_key(await _generation(), user_id), payload, ex=CACHE_TTL_SECONDS)
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.debug("rbac cache write failed: %s", exc)


async def invalidate_user(user_id: str) -> None:
    """Drop one user's entry — role reassignment, deactivation, deletion."""
    try:
        await get_redis().delete(_key(await _generation(), user_id))
    except (redis.exceptions.RedisError, OSError) as exc:
        # Worst case the entry survives its 60s TTL. Callers that need a hard
        # guarantee (deactivation) do not depend on this: `is_active` lives on
        # the uncached users row.
        logger.warning("could not invalidate rbac cache for user %s: %s", user_id, exc)


async def invalidate_all() -> None:
    """Retire every entry at once by bumping the generation counter.

    Used when a *role* changes, which affects an unbounded set of users. One
    INCR beats enumerating holders and is impossible to under-invalidate.
    """
    try:
        await get_redis().incr(_GENERATION_KEY)
        logger.info("rbac cache generation bumped")
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.warning("could not bump rbac cache generation: %s", exc)
