"""Application settings — env-driven, no secrets in code (CLAUDE.md §11).

Every variable read here is documented in `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

# repo root: backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> repo
REPO_ROOT = Path(__file__).resolve().parents[3]

Environment = Literal["local", "development", "staging", "production"]

# libpq-only query parameters. asyncpg does not understand them; TLS is
# configured explicitly in `core.database` instead, so strip them from the URL.
_LIBPQ_ONLY_PARAMS = frozenset(
    {"sslmode", "channel_binding", "sslrootcert", "target_session_attrs", "pgbouncer"}
)

# Hosts we will talk to without TLS (compose service names and loopback).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})


def normalize_asyncpg_url(raw: str) -> URL:
    """Rewrite a stock Postgres URL into one the asyncpg driver accepts.

    Neon hands out `postgresql://…?sslmode=require&channel_binding=require`.
    asyncpg rejects those query params, so we drop them and force the
    `postgresql+asyncpg` driver. TLS is not lost — it is applied via an explicit
    SSLContext in `core.database`, which also verifies the server certificate
    (`sslmode=require` on its own does not verify anything).
    """
    url = make_url(raw)
    query = {k: v for k, v in url.query.items() if k not in _LIBPQ_ONLY_PARAMS}
    return url.set(drivername="postgresql+asyncpg", query=query)


def requires_tls(url: URL) -> bool:
    """False only for an explicitly local/plaintext Postgres."""
    return (url.host or "").lower() not in _LOCAL_HOSTS


def is_pooled(url: URL) -> bool:
    """Neon's pooled endpoint is identified by a `-pooler` host segment.

    Behind PgBouncer in transaction mode, server-side prepared statements
    cannot be reused across checkouts — `core.database` disables the statement
    cache when this is true.
    """
    return "-pooler." in (url.host or "")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # compose-only vars (POSTGRES_*, *_PORT) live in the same file
    )

    # --- Application -------------------------------------------------------
    APP_NAME: str = "Hospital Management API"
    ENVIRONMENT: Environment = "local"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # --- Database ----------------------------------------------------------
    # Pooled (PgBouncer) endpoint — the running application.
    DATABASE_URL: str
    # Unpooled (direct) endpoint — Alembic migrations only.
    DIRECT_URL: str

    DB_ECHO: bool = False
    DB_POOL_SIZE: Annotated[int, Field(ge=1)] = 5
    DB_MAX_OVERFLOW: Annotated[int, Field(ge=0)] = 10
    DB_POOL_RECYCLE_SECONDS: Annotated[int, Field(ge=30)] = 300
    DB_POOL_PRE_PING: bool = True
    DB_CONNECT_TIMEOUT_SECONDS: Annotated[int, Field(ge=1)] = 15
    DB_COMMAND_TIMEOUT_SECONDS: Annotated[int, Field(ge=0)] = 30

    # --- Least-privilege application role ----------------------------------
    # Managed Postgres owners (Neon's `neondb_owner` included) carry BYPASSRLS,
    # which overrides even FORCE ROW LEVEL SECURITY — an app connecting as the
    # owner ignores every tenant policy. `scripts/bootstrap_db_role.py` creates
    # this role; DATABASE_URL should then use it while DIRECT_URL stays on the
    # owner for migrations.
    APP_DB_ROLE: str = "hms_app"
    APP_DB_PASSWORD: str = ""

    # Local Postgres used by the integration test suite. Tests never touch the
    # configured DATABASE_URL — an accidental `DROP` against a shared Neon
    # branch is not a mistake worth being able to make.
    TEST_DATABASE_URL: str = "postgresql://hospital:hospital@localhost:5432/hospital_test"

    # --- Redis -------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Authentication (CLAUDE.md §12) ------------------------------------
    # Signing key for JWTs. Generate with:  python -c "import secrets;
    # print(secrets.token_urlsafe(64))". Rotating it invalidates every
    # outstanding token, which is the intended emergency lever.
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "hospital-management"
    # Short-lived access token: a leaked one expires before it is useful.
    ACCESS_TOKEN_TTL_MINUTES: Annotated[int, Field(ge=1, le=1440)] = 15
    # Refresh tokens rotate on every use and are revoked on reuse.
    REFRESH_TOKEN_TTL_DAYS: Annotated[int, Field(ge=1, le=90)] = 7

    PASSWORD_MIN_LENGTH: Annotated[int, Field(ge=8)] = 12

    # Argon2id work factors. Defaults follow OWASP's second recommended
    # configuration (46 MiB, 1 iteration, 1 lane); raise MEMORY_KIB before
    # raising TIME_COST if login latency budget allows.
    ARGON2_TIME_COST: Annotated[int, Field(ge=1)] = 2
    ARGON2_MEMORY_KIB: Annotated[int, Field(ge=8192)] = 65536
    ARGON2_PARALLELISM: Annotated[int, Field(ge=1)] = 2

    # --- Background jobs (CLAUDE.md §6) ------------------------------------
    # The auto-close safety net: how long a visit may sit in a non-terminal
    # state with nothing outstanding before a background job closes it, so a
    # forgotten click does not leave encounters open forever.
    #
    # Expressed as a rolling window rather than a wall-clock "end of day" on
    # purpose. A fixed hour needs a timezone, and a hospital's day does not end
    # at midnight UTC — 12 hours after the patient arrived is the same guarantee
    # without a class of bug that only shows up in production in another
    # timezone. Raise it for a hospital whose OPD genuinely runs longer.
    ENCOUNTER_AUTO_CLOSE_HOURS: Annotated[int, Field(ge=1, le=168)] = 12
    # How often the sweep runs. Cheap: it scans one index on non-terminal rows.
    WORKER_SWEEP_INTERVAL_MINUTES: Annotated[int, Field(ge=1, le=1440)] = 60
    # Master switch, so a deployment can run the API without the worker.
    WORKER_SWEEPS_ENABLED: bool = True

    # --- Billing (CLAUDE.md §6, §7b) ---------------------------------------
    # Whether an unpaid bill keeps a visit in PENDING_CLEARANCE. True matches
    # CLAUDE.md §6, which names the cashier as one of the clearance stations, and
    # matches how a cash OPD actually runs: the patient settles before they leave.
    #
    # Turn it off for a hospital on corporate credit accounts, where patients
    # legitimately walk out and finance invoices the employer next month. The
    # balance still shows on the counter's screen either way — what changes is
    # whether it holds the encounter open and lands on the auto-close sweep's
    # "held for staff" list.
    BILLING_BLOCKS_ENCOUNTER_CLOSURE: bool = True

    # --- Inpatient (CLAUDE.md §13 step 9) ----------------------------------
    # How long after a dose was due before the sweep records it as MISSED with
    # `auto_missed` set. Two hours is the usual ward tolerance: long enough that
    # a busy drug round running late is not defamed as a missed dose, short
    # enough that a genuinely missed antibiotic surfaces within the shift that
    # missed it rather than at handover.
    IPD_DOSE_GRACE_HOURS: Annotated[int, Field(ge=1, le=24)] = 2
    # How far ahead the medication chart is materialised. Deliberately short: a
    # chart built weeks ahead is full of doses for orders that will be stopped
    # tomorrow, and every one of them would age into a false "missed".
    IPD_CHART_HORIZON_DAYS: Annotated[int, Field(ge=1, le=14)] = 2

    # --- Reporting (CLAUDE.md §7b, §13 step 10) ----------------------------
    # How long a patient may be waiting before their clinic is flagged as
    # running late on reception's screen (§7b's doctor delay alert). Thirty
    # minutes is the point at which people start coming to the desk to ask,
    # so the alert should reach reception slightly before the patient does.
    REPORTING_QUEUE_DELAY_MINUTES: Annotated[int, Field(ge=5, le=480)] = 30

    # --- Notifications (CLAUDE.md §9, §13 step 8) --------------------------
    # Master switch. Off still records every message as SUPPRESSED with a
    # reason rather than skipping the row: "we chose not to send" has to be
    # visible, otherwise a hospital that turned messaging off in a config file
    # six months ago has no way to discover that is why nobody is being
    # reminded of anything.
    NOTIFICATIONS_ENABLED: bool = True

    # Which provider adapter handles delivery. `console` logs the message and
    # reports success; `null` accepts silently. A real WhatsApp/SMS adapter
    # registers itself in `notifications/gateways.py` and is named here.
    NOTIFICATION_GATEWAY: str = "console"

    # The fallback ladder (CLAUDE.md §9: WhatsApp -> SMS -> email). Ordered,
    # comma-separated, and configurable because the right order is a fact about
    # a hospital's patients rather than about the software — a clinic whose
    # patients are not on WhatsApp should be able to reorder this without a
    # deploy. Channels the recipient has no address for are skipped.
    NOTIFICATION_CHANNEL_PRIORITY: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["WHATSAPP", "SMS", "EMAIL"]
    )

    # How many times the whole ladder may be walked before a message is written
    # off as FAILED. One walk already tries every channel, so this is about
    # transient provider outages, not about bad numbers.
    NOTIFICATION_MAX_ATTEMPTS: Annotated[int, Field(ge=1, le=10)] = 3

    # The retry sweep: picks up messages that failed every channel and gives the
    # ladder another walk, up to NOTIFICATION_MAX_ATTEMPTS. Separate switch from
    # WORKER_SWEEPS_ENABLED so a deployment can keep the encounter auto-close
    # safety net while a provider incident is in progress.
    NOTIFICATION_RETRY_ENABLED: bool = True
    NOTIFICATION_RETRY_BATCH_SIZE: Annotated[int, Field(ge=1, le=1000)] = 100

    # --- CORS --------------------------------------------------------------
    # `NoDecode` stops pydantic-settings from trying to JSON-parse the env
    # value: we want a plain comma-separated list in .env, not `["a","b"]`.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("CORS_ORIGINS", "NOTIFICATION_CHANNEL_PRIORITY", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("NOTIFICATION_CHANNEL_PRIORITY")
    @classmethod
    def _known_channels(cls, value: list[str]) -> list[str]:
        """Reject a typo at boot rather than at the first message.

        A misspelt channel silently shortens the ladder, and the symptom — some
        patients stop getting messages — surfaces weeks later and looks like a
        provider problem.
        """
        known = {"WHATSAPP", "SMS", "EMAIL"}
        normalised = [item.strip().upper() for item in value if item.strip()]
        unknown = [item for item in normalised if item not in known]
        if unknown:
            raise ValueError(
                f"Unknown notification channel(s): {', '.join(unknown)}. "
                f"Valid channels: {', '.join(sorted(known))}."
            )
        if not normalised:
            raise ValueError("NOTIFICATION_CHANNEL_PRIORITY must list at least one channel.")
        return normalised

    @field_validator("LOG_LEVEL")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """Refuse to start a production deployment with a weak signing key.

        Failing loudly at boot is far better than discovering forgeable tokens
        against live patient data.
        """
        if self.is_production and len(self.SECRET_KEY) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters in production")
        if self.is_production and self.DEBUG:
            raise ValueError("DEBUG must be false in production")
        return self

    # --- Derived -----------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def async_database_url(self) -> URL:
        """Runtime engine URL (pooled endpoint, asyncpg driver)."""
        return normalize_asyncpg_url(self.DATABASE_URL)

    @property
    def async_direct_url(self) -> URL:
        """Migration engine URL (unpooled endpoint, asyncpg driver)."""
        return normalize_asyncpg_url(self.DIRECT_URL)

    @property
    def async_test_url(self) -> URL:
        """Integration-test engine URL (always a local Postgres)."""
        return normalize_asyncpg_url(self.TEST_DATABASE_URL)

    @property
    def requires_tls(self) -> bool:
        """True unless the runtime target is an explicitly local Postgres."""
        return requires_tls(self.async_database_url)

    @property
    def is_pooled_connection(self) -> bool:
        """Whether the runtime target is Neon's pooled (PgBouncer) endpoint."""
        return is_pooled(self.async_database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Import this, never instantiate Settings directly."""
    return Settings()  # values are supplied by the environment / .env


settings = get_settings()
