"""seed permissions and system roles

Revision ID: 2106661f1238
Revises: 62b13fba8c82
Create Date: 2026-08-01 16:42:36.041107+00:00

Seeds the RBAC catalogue described in CLAUDE.md §8.

The data is written out **literally** here rather than imported from
`app.modules.identity.rbac`. A migration is a snapshot of an intended change:
if it imported the live catalogue, editing that catalogue would retroactively
change what this already-applied migration means, and two databases at the same
revision could end up with different rows. Later modules seed their own
permissions in their own migrations.

Idempotent, so re-running against a partially seeded database is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2106661f1238"
down_revision: str | None = "62b13fba8c82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (code, module, description)
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("hospital:create", "tenancy", "Register a new hospital tenant."),
    ("hospital:read", "tenancy", "View hospital details."),
    ("hospital:update", "tenancy", "Edit hospital details."),
    ("hospital:deactivate", "tenancy", "Suspend or reactivate a hospital."),
    ("user:create", "identity", "Create a staff account."),
    ("user:read", "identity", "View staff accounts."),
    ("user:update", "identity", "Edit a staff account."),
    ("user:deactivate", "identity", "Deactivate a staff account."),
    ("user:reset_password", "identity", "Reset another user's password."),
    ("role:create", "identity", "Define a hospital-specific role."),
    ("role:read", "identity", "View roles and their permissions."),
    ("role:update", "identity", "Edit a hospital-specific role."),
    ("role:assign", "identity", "Assign or remove a user's roles."),
    ("audit:read", "identity", "Read the audit log."),
)

# (code, name, description)
ROLES: tuple[tuple[str, str, str], ...] = (
    ("PLATFORM_ADMIN", "Platform Administrator", "Manages hospitals across tenants."),
    ("HOSPITAL_ADMIN", "Hospital Administrator", "Full access within one hospital."),
    ("DOCTOR", "Doctor", "Clinical actions on own or assigned patients."),
    ("NURSE", "Nurse", "Vitals, nursing notes, queue and medication records."),
    ("RECEPTIONIST", "Receptionist", "Registration, scheduling and check-in."),
    ("CASHIER", "Cashier", "Payments and invoices."),
    ("LAB_TECH", "Lab Technician", "Laboratory result entry."),
    ("RADIOLOGIST", "Radiologist", "Radiology reporting."),
    ("PHARMACIST", "Pharmacist", "Dispensing and pharmacy stock."),
    ("BILLING_STAFF", "Billing Staff", "Invoices and insurance or scheme claims."),
    (
        "RECORDS_OFFICER",
        "Records Officer",
        "Records death, referral and LAMA; manages medical records.",
    ),
    ("AUDITOR", "Auditor", "Read-only access to audit logs and reports."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)
_HOSPITAL_ADMIN = (
    "hospital:read",
    "hospital:update",
    "user:create",
    "user:read",
    "user:update",
    "user:deactivate",
    "user:reset_password",
    "role:create",
    "role:read",
    "role:update",
    "role:assign",
    "audit:read",
)
_AUDITOR = ("hospital:read", "user:read", "role:read", "audit:read")
# Clinical and operational roles look sparse deliberately: they gain
# permissions when the modules that enforce them are built.
_STAFF = ("hospital:read",)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _HOSPITAL_ADMIN,
    "AUDITOR": _AUDITOR,
    "DOCTOR": _STAFF,
    "NURSE": _STAFF,
    "RECEPTIONIST": _STAFF,
    "CASHIER": _STAFF,
    "LAB_TECH": _STAFF,
    "RADIOLOGIST": _STAFF,
    "PHARMACIST": _STAFF,
    "BILLING_STAFF": _STAFF,
    "RECORDS_OFFICER": _STAFF,
}


def upgrade() -> None:
    connection = op.get_bind()

    # Seed rows predate any application call, so they use gen_random_uuid()
    # rather than the app's UUIDv7 generator. Nothing depends on a seed id
    # being time-ordered.
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

    for code, name, description in ROLES:
        connection.execute(
            sa.text(
                """
                INSERT INTO roles
                    (id, hospital_id, code, name, description, is_system,
                     created_at, updated_at)
                -- Casts are explicit because the same placeholder appears in
                -- both the SELECT list and the NOT EXISTS predicate, and
                -- asyncpg refuses to infer two different types for one
                -- parameter.
                SELECT gen_random_uuid(), NULL,
                       CAST(:code AS varchar), CAST(:name AS varchar),
                       CAST(:description AS varchar), true, now(), now()
                 WHERE NOT EXISTS (
                     SELECT 1 FROM roles
                      WHERE code = CAST(:code AS varchar)
                        AND hospital_id IS NULL
                        AND deleted_at IS NULL
                 )
                """
            ),
            {"code": code, "name": name, "description": description},
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
             WHERE role_id IN (SELECT id FROM roles WHERE is_system AND hospital_id IS NULL)
            """
        )
    )
    connection.execute(sa.text("DELETE FROM roles WHERE is_system AND hospital_id IS NULL"))
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": list(_ALL)},
    )
