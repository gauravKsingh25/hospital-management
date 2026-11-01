"""nurse may record LAMA

Revision ID: f2b5d8e0a3c1
Revises: e1a4c7d9f2b0
Create Date: 2026-11-01 10:00:00.000000+00:00

UX-04. On a night ward the doctor may not be on the floor, and a patient who
has absconded must be recorded when it is noticed. The nurse in charge was
refused (403); `encounter:record_lama` is now theirs too. Death and referral
stay with doctors and records. Data only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b5d8e0a3c1"
down_revision: str | None = "e1a4c7d9f2b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {"NURSE": ("encounter:record_lama",)}


def upgrade() -> None:
    connection = op.get_bind()
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
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE role_id IN (SELECT id FROM roles WHERE code = 'NURSE' AND hospital_id IS NULL)
               AND permission_id IN (SELECT id FROM permissions WHERE code = 'encounter:record_lama')
            """
        )
    )
