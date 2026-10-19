"""admission desk takes vitals

Revision ID: 75eb16a8e222
Revises: 0666a25031c9
Create Date: 2026-10-05 09:03:01.187202+00:00

The doctor's consultation screen is gone: doctors see OPD patients in person
and enter nothing. Vitals for a patient being admitted are now taken at the
admission desk, at the counter, as part of admitting them. The role gains
`vitals:record` and `vitals:read`, mirroring `clinical/rbac.py`
(`tests/test_rbac_seed.py` fails if the two disagree).

Data only: no schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75eb16a8e222"
down_revision: str | None = "0666a25031c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "ADMISSION_DESK"
_CODES = ("vitals:read", "vitals:record")


def upgrade() -> None:
    connection = op.get_bind()
    for code in _CODES:
        connection.execute(
            sa.text(
                """
                INSERT INTO role_permissions (role_id, permission_id, created_at)
                SELECT r.id, p.id, now()
                  FROM roles r, permissions p
                 WHERE r.code = :role AND r.hospital_id IS NULL AND p.code = :code
                ON CONFLICT DO NOTHING
                """
            ),
            {"role": _ROLE, "code": code},
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE role_id IN (SELECT id FROM roles WHERE code = :role AND hospital_id IS NULL)
               AND permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"role": _ROLE, "codes": list(_CODES)},
    )
