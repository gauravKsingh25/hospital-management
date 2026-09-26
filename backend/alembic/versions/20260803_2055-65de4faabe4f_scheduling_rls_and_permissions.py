"""scheduling rls and permissions

Revision ID: 65de4faabe4f
Revises: c9c90527110f
Create Date: 2026-08-03 20:55:24.136757+00:00

Row-level security for the scheduling and department tables, on the pattern
established in the identity migration, plus this module's permission seed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "65de4faabe4f"
down_revision: str | None = "c9c90527110f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "departments",
    "doctors",
    "doctor_availabilities",
    "availability_exceptions",
    "appointments",
    "queue_entries",
    "token_sequences",
)

SCHEDULING = "scheduling"
TENANCY = "tenancy"

# (code, module, description)
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("department:read", TENANCY, "View departments."),
    ("department:manage", TENANCY, "Create or edit departments."),
    ("doctor:read", SCHEDULING, "View doctor profiles."),
    ("doctor:manage", SCHEDULING, "Create or edit doctor profiles."),
    ("availability:manage", SCHEDULING, "Set clinic hours and leave."),
    ("appointment:create", SCHEDULING, "Book an appointment."),
    ("appointment:read", SCHEDULING, "View appointments."),
    ("appointment:update", SCHEDULING, "Edit or reschedule."),
    ("appointment:cancel", SCHEDULING, "Cancel or mark an appointment no-show."),
    ("queue:read", SCHEDULING, "View the OPD queue."),
    ("queue:manage", SCHEDULING, "Check in, call, skip and complete tokens."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)
_READ_ONLY = ("department:read", "doctor:read", "appointment:read", "queue:read")
_RECEPTION = (
    *_READ_ONLY,
    "appointment:create",
    "appointment:update",
    "appointment:cancel",
    "queue:manage",
)
# A doctor calls their own next patient and sets their own hours, and does not
# run the counter — keeping their surface small is the point of CLAUDE.md §7.
_DOCTOR = (*_READ_ONLY, "queue:manage", "availability:manage")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "RECEPTIONIST": _RECEPTION,
    "DOCTOR": _DOCTOR,
    "NURSE": (*_READ_ONLY, "queue:manage"),
    "CASHIER": _READ_ONLY,
    "BILLING_STAFF": _READ_ONLY,
    "RECORDS_OFFICER": _READ_ONLY,
    "AUDITOR": _READ_ONLY,
    "LAB_TECH": ("department:read", "doctor:read"),
    "RADIOLOGIST": ("department:read", "doctor:read"),
    "PHARMACIST": ("department:read", "doctor:read"),
}


def upgrade() -> None:
    connection = op.get_bind()

    # --- row-level security ------------------------------------------------
    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # --- permissions -------------------------------------------------------
    for code, module, description in PERMISSIONS:
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
        {"codes": list(_ALL)},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_ALL)}
    )

    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
