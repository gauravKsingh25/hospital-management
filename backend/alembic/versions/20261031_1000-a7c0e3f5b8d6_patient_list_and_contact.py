"""patient directory and contact permissions

Revision ID: a7c0e3f5b8d6
Revises: f6b9d2e4a7c5
Create Date: 2026-10-31 10:00:00.000000+00:00

SEC-01. Any staff login could page the whole patient directory, phone numbers
included, without an audit row. Two rights now split off `patient:read`:

* `patient:list` — browsing the directory, for the front desk, records and
  admin. Everybody else works from the visit in front of them;
* `patient:contact` — a patient's full phone number, for the people who ring
  patients. Everybody else sees it masked.

Data only: no schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c0e3f5b8d6"
down_revision: str | None = "f6b9d2e4a7c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("patient:list", "patients", "Browse the whole patient directory."),
    ("patient:contact", "patients", "See patients' full phone numbers."),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "HOSPITAL_ADMIN": _NEW_CODES,
    "RECEPTIONIST": _NEW_CODES,
    "RECORDS_OFFICER": _NEW_CODES,
    "ADMISSION_DESK": ("patient:contact",),
    "DOCTOR": ("patient:contact",),
    "NURSE": ("patient:contact",),
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
