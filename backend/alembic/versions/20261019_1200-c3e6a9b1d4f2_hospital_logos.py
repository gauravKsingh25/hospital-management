"""hospital logos

Revision ID: c3e6a9b1d4f2
Revises: b2d5f8a0c3e1
Create Date: 2026-10-19 12:00:00.000000+00:00

The logo printed at the top of every bill. One row per upload in
`hospital_logos` — never overwritten, so an issued bill keeps the version it
was printed with — and `hospitals.logo_version` names the current one, so the
profile and the letterhead need no extra lookup. Tenant-isolated by RLS like
every other tenant table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e6a9b1d4f2"
down_revision: str | None = "b2d5f8a0c3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"


def upgrade() -> None:
    op.add_column("hospitals", sa.Column("logo_version", sa.Integer(), nullable=True))
    op.create_table(
        "hospital_logos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("hospital_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hospital_id", "version", name="uq_hospital_logos_version"),
    )
    op.create_index(
        op.f("ix_hospital_logos_hospital_id"), "hospital_logos", ["hospital_id"], unique=False
    )
    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    op.execute("ALTER TABLE hospital_logos ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hospital_logos FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON hospital_logos
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON hospital_logos")
    op.drop_index(op.f("ix_hospital_logos_hospital_id"), table_name="hospital_logos")
    op.drop_table("hospital_logos")
    op.drop_column("hospitals", "logo_version")
