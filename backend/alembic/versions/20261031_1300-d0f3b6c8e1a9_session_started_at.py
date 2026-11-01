"""refresh token session start

Revision ID: d0f3b6c8e1a9
Revises: c9e2a5b7d0f8
Create Date: 2026-10-31 13:00:00.000000+00:00

SEC-04. A session now ends at a shift's length after sign-in however often
it refreshes, so each refresh token carries the time its chain began.
Existing tokens are left null and counted from their own creation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0f3b6c8e1a9"
down_revision: str | None = "c9e2a5b7d0f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("session_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "session_started_at")
