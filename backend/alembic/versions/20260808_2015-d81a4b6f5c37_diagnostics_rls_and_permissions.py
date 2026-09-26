"""diagnostics rls and permissions

Revision ID: d81a4b6f5c37
Revises: 3c03c7405b82
Create Date: 2026-08-08 20:15:00.000000+00:00

Row-level security for the diagnostics tables, on the pattern the identity,
patients, scheduling and clinical migrations established, plus this module's
permission seed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d81a4b6f5c37"
down_revision: str | None = "3c03c7405b82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "test_catalogue_items",
    "test_analytes",
    "reference_ranges",
    "specimens",
    "diagnostic_reports",
    "result_values",
    "accession_sequences",
)

DIAGNOSTICS = "diagnostics"

# (code, module, description) — mirrors `diagnostics/rbac.py`, which is where
# route handlers import these codes from.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("catalogue:read", DIAGNOSTICS, "View the test catalogue."),
    ("catalogue:manage", DIAGNOSTICS, "Edit tests, analytes and ranges."),
    ("specimen:read", DIAGNOSTICS, "View specimens."),
    ("specimen:collect", DIAGNOSTICS, "Collect and label a sample."),
    ("specimen:receive", DIAGNOSTICS, "Receive or reject a sample at the lab."),
    ("report:read", DIAGNOSTICS, "Read diagnostic reports."),
    ("result:enter", DIAGNOSTICS, "Enter results and findings."),
    ("result:verify", DIAGNOSTICS, "Verify and release a report."),
    ("result:amend", DIAGNOSTICS, "Amend a released report."),
    ("report:cancel", DIAGNOSTICS, "Cancel a report."),
    ("result:acknowledge_critical", DIAGNOSTICS, "Record a critical-value callback."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

_READ = ("catalogue:read", "specimen:read", "report:read")

# The lab bench: everything up to but not including the signature. A technician
# who could verify their own work would make the second pair of eyes optional,
# which is the one thing verification exists to prevent.
_LAB_TECH = (
    *_READ,
    "specimen:collect",
    "specimen:receive",
    "result:enter",
    "result:acknowledge_critical",
)

# A radiologist performs and signs their own reporting — that IS the job.
_RADIOLOGIST = (
    *_READ,
    "result:enter",
    "result:verify",
    "result:amend",
    "report:cancel",
    "result:acknowledge_critical",
)

# Amending and cancelling come with the signature, not with the bench:
# whoever put their name to a report has to be able to correct it.
_DOCTOR = (
    *_READ,
    "result:verify",
    "result:amend",
    "report:cancel",
    "result:acknowledge_critical",
)
_NURSE = (*_READ, "specimen:collect", "result:acknowledge_critical")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "LAB_TECH": _LAB_TECH,
    "RADIOLOGIST": _RADIOLOGIST,
    "DOCTOR": _DOCTOR,
    "NURSE": _NURSE,
    # Reception tells a waiting patient whether their report is ready; they do
    # not get to read what is in it.
    "RECEPTIONIST": ("catalogue:read", "specimen:read"),
    "CASHIER": ("catalogue:read",),
    "BILLING_STAFF": ("catalogue:read",),
    "PHARMACIST": ("catalogue:read",),
    "RECORDS_OFFICER": _READ,
    "AUDITOR": _READ,
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
