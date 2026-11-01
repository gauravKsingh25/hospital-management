"""payment back-dating permission

Revision ID: c9e2a5b7d0f8
Revises: b8d1f4a6c9e7
Create Date: 2026-10-31 12:00:00.000000+00:00

SEC-07. A payment's `received_at` was taken from the caller unchecked, so a
receipt could be back-dated into a day whose cash was already counted. It must
now be today on the hospital's clock unless the caller holds
`payment:backdate`, granted to administrators. Data only: no schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2a5b7d0f8"
down_revision: str | None = "b8d1f4a6c9e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("payment:backdate", "billing", "Record a payment as received on an earlier day."),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "HOSPITAL_ADMIN": _NEW_CODES,
}


def upgrade() -> None:
    connection = op.get_bind()
    for code, module, description in NEW_PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, module, description, created_at, updated_at)
                VALUES (gen_random_uuid(), :code, :module, :description, now(), now())
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "module": module, "description": description},
        )
    for role_code, permission_codes in ROLE_PERMISSIONS.items():
        for permission_code in permission_codes:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id, created_at)
                    SELECT r.id, p.id, now()
                      FROM roles r, permissions p
                     WHERE r.code = :role_code
                       AND r.hospital_id IS NULL
                       AND p.code = :permission_code
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"role_code": role_code, "permission_code": permission_code},
            )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"codes": list(_NEW_CODES)},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_NEW_CODES)}
    )
