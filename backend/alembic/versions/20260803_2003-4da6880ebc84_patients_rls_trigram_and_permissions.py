"""patients rls trigram and permissions

Revision ID: 4da6880ebc84
Revises: 73970796b94b
Create Date: 2026-08-03 20:03:36.326462+00:00

Three things the patients tables need beyond their columns:

1. **Row-level security**, on the same pattern established for `identity` — a
   backstop so a query that forgets its `hospital_id` filter cannot surface
   another hospital's patients. Patient data is the most sensitive in the
   system; this is the table where the guarantee matters most.

2. **pg_trgm**, for duplicate detection. Reception types a name at speed and
   spells it differently each visit; trigram similarity is what turns `Sunta
   Devi` into a suggestion of the existing `Sunita Devi` instead of a second
   medical history. The GIN index keeps that fast enough to run on every
   keystroke.

3. **The module's permissions**, seeded per the rule in `identity.rbac`: a
   module ships the permissions it actually enforces, and only those.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4da6880ebc84"
down_revision: str | None = "73970796b94b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "patients",
    "patient_alerts",
    "patient_consents",
    "patient_identifiers",
    "uhid_sequences",
)

MODULE = "patients"

# (code, description)
PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("patient:create", "Register a patient."),
    ("patient:read", "View and search patient records."),
    ("patient:update", "Edit patient demographics."),
    ("patient:merge", "Merge duplicate patient records."),
    ("patient:read_identifiers", "View government and scheme identifiers."),
    ("patient:manage_identifiers", "Add or remove patient identifiers."),
    ("patient:manage_alerts", "Record or retire patient safety alerts."),
    ("patient:manage_consent", "Capture or withdraw patient consent."),
)

_ALL = tuple(code for code, _ in PERMISSIONS)
_CLINICAL_READ = ("patient:read", "patient:manage_alerts")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "RECEPTIONIST": (
        "patient:create",
        "patient:read",
        "patient:update",
        "patient:manage_identifiers",
        "patient:manage_consent",
    ),
    "DOCTOR": _CLINICAL_READ,
    "NURSE": _CLINICAL_READ,
    "LAB_TECH": ("patient:read",),
    "RADIOLOGIST": ("patient:read",),
    "PHARMACIST": ("patient:read",),
    "CASHIER": ("patient:read", "patient:read_identifiers"),
    "BILLING_STAFF": (
        "patient:read",
        "patient:read_identifiers",
        "patient:manage_identifiers",
    ),
    "RECORDS_OFFICER": (
        "patient:read",
        "patient:update",
        "patient:merge",
        "patient:read_identifiers",
        "patient:manage_identifiers",
        "patient:manage_consent",
    ),
    "AUDITOR": ("patient:read",),
}


def upgrade() -> None:
    connection = op.get_bind()

    # --- fuzzy name matching ----------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_patients_name_normalized_trgm "
        "ON patients USING gin (name_normalized gin_trgm_ops)"
    )

    # --- row-level security ------------------------------------------------
    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE, because the migration owner would otherwise skip its own
        # policies — see the identity RLS migration for the full reasoning.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # --- permissions -------------------------------------------------------
    for code, description in PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, module, description, created_at, updated_at)
                VALUES (gen_random_uuid(), :code, :module, :description, now(), now())
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "module": MODULE, "description": description},
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
             WHERE permission_id IN (SELECT id FROM permissions WHERE module = :module)
            """
        ),
        {"module": MODULE},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE module = :module"), {"module": MODULE}
    )

    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP INDEX IF EXISTS ix_patients_name_normalized_trgm")
    # pg_trgm is left installed: another module may rely on it, and dropping an
    # extension is not the business of this migration's reversal.
