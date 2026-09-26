"""The URL rewriting is load-bearing: get it wrong and the app cannot connect
at all, or connects without TLS. Pin the behaviour."""

from __future__ import annotations

from app.core.config import normalize_asyncpg_url, settings

NEON_POOLED = (
    "postgresql://user:pw@ep-example-000000-pooler.c-5.us-east-2.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)


def test_driver_is_rewritten_to_asyncpg() -> None:
    assert normalize_asyncpg_url(NEON_POOLED).drivername == "postgresql+asyncpg"


def test_libpq_only_params_are_stripped() -> None:
    url = normalize_asyncpg_url(NEON_POOLED)
    assert "sslmode" not in url.query
    assert "channel_binding" not in url.query


def test_credentials_and_database_survive_rewriting() -> None:
    url = normalize_asyncpg_url(NEON_POOLED)
    assert url.username == "user"
    assert url.password == "pw"
    assert url.database == "neondb"
    assert url.host is not None and url.host.endswith("neon.tech")


def test_configured_urls_point_at_the_right_endpoints() -> None:
    """Pooled for the app, unpooled for migrations (CLAUDE.md §5)."""
    app_host = settings.async_database_url.host or ""
    migration_host = settings.async_direct_url.host or ""

    if "neon.tech" in app_host:
        assert "-pooler." in app_host, "DATABASE_URL must be Neon's pooled endpoint"
        assert "-pooler." not in migration_host, "DIRECT_URL must be the unpooled endpoint"
        assert settings.is_pooled_connection is True
        assert settings.requires_tls is True


def test_local_postgres_does_not_require_tls() -> None:
    from app.core.config import Settings

    local = Settings(
        DATABASE_URL="postgresql://u:p@postgres:5432/hospital",
        DIRECT_URL="postgresql://u:p@postgres:5432/hospital",
    )
    assert local.requires_tls is False
    assert local.is_pooled_connection is False
