"""hospital upi id

Revision ID: 3b9e4c1d7a52
Revises: 75eb16a8e222
Create Date: 2026-10-06 18:00:00.000000+00:00

Reception collects from the queue row, and the payment dialog shows a UPI QR
for the exact amount. That QR pays the hospital's own UPI ID, which lives on
the tenant: two hospitals on one deployment are two payees. Nullable — until
an administrator sets it the dialog says so and the counter takes the
reference from whatever QR it already has on the desk.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b9e4c1d7a52"
down_revision: str | None = "75eb16a8e222"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hospitals", sa.Column("upi_vpa", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("hospitals", "upi_vpa")
