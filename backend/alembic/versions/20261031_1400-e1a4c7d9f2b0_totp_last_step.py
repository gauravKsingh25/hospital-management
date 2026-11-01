"""two-step sign-in: the last code spent

Revision ID: e1a4c7d9f2b0
Revises: d0f3b6c8e1a9
Create Date: 2026-10-31 14:00:00.000000+00:00

SEC-05. The `two_factor_enabled` and `totp_secret` columns were reserved on
`users` from the start; this adds the 30-second step of the last code
accepted, so a code signs nobody in twice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4c7d9f2b0"
down_revision: str | None = "d0f3b6c8e1a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "totp_last_step")
