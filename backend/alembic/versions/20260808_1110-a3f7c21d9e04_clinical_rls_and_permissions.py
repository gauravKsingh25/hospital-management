"""clinical rls and permissions

Revision ID: a3f7c21d9e04
Revises: 5bb9e496ac94
Create Date: 2026-08-08 11:10:00.000000+00:00

Row-level security for the clinical tables, on the pattern the identity,
patients and scheduling migrations established, plus this module's permission
seed.

`FORCE ROW LEVEL SECURITY` matters as much as `ENABLE` here: without it the
table owner is exempt from its own policies, and clinical data is the last place
we want a silent isolation hole. The application connects as a restricted role
that cannot bypass RLS at all — see `scripts/bootstrap_db_role.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f7c21d9e04"
down_revision: str | None = "5bb9e496ac94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "encounters",
    "encounter_events",
    "encounter_sequences",
    "vitals",
    "clinical_notes",
    "note_templates",
    "diagnoses",
    "orders",
)

CLINICAL = "clinical"

# (code, module, description) — mirrors `clinical/rbac.py`, which is where route
# handlers import these codes from.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("encounter:read", CLINICAL, "View a visit and its status."),
    ("encounter:create", CLINICAL, "Open a visit."),
    ("encounter:update", CLINICAL, "Edit visit details."),
    ("encounter:admit", CLINICAL, "Convert a visit to admission."),
    ("encounter:cancel", CLINICAL, "Cancel or mark a visit no-show."),
    ("encounter:complete", CLINICAL, "Complete a consultation or close a visit."),
    ("encounter:record_death", CLINICAL, "Record a patient death."),
    ("encounter:record_referral", CLINICAL, "Record a referral to another facility."),
    ("encounter:record_lama", CLINICAL, "Record leaving against medical advice."),
    ("vitals:read", CLINICAL, "View recorded vitals."),
    ("vitals:record", CLINICAL, "Record vitals."),
    ("note:read", CLINICAL, "Read clinical notes."),
    ("note:write", CLINICAL, "Write clinical notes."),
    ("note:sign", CLINICAL, "e-Sign a clinical note."),
    ("diagnosis:read", CLINICAL, "View diagnoses."),
    ("diagnosis:record", CLINICAL, "Record or code a diagnosis."),
    ("order:read", CLINICAL, "View orders."),
    ("order:place", CLINICAL, "Place a clinical order."),
    ("order:fulfil", CLINICAL, "Start or complete an order."),
    ("order:cancel", CLINICAL, "Cancel an order."),
    ("template:read", CLINICAL, "Use note templates."),
    ("template:manage", CLINICAL, "Create or edit note templates."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

# Reading a visit is not reading a chart: a cashier needs the encounter to raise
# an invoice and has no business in the examination note.
_CHART_READ = ("encounter:read", "vitals:read", "note:read", "diagnosis:read", "order:read")

# CLAUDE.md §7 — the doctor's surface is clinical acts only.
_DOCTOR = (
    *_CHART_READ,
    "encounter:update",
    "encounter:admit",
    "encounter:complete",
    "encounter:record_death",
    "encounter:record_referral",
    "encounter:record_lama",
    "vitals:record",
    "note:write",
    "note:sign",
    "diagnosis:record",
    "order:place",
    "order:cancel",
    "template:read",
    "template:manage",
)

_NURSE = (
    *_CHART_READ,
    "encounter:create",
    "encounter:update",
    "vitals:record",
    "note:write",
    "note:sign",
    "template:read",
)

_RECEPTION = (
    "encounter:read",
    "encounter:create",
    "encounter:update",
    "encounter:cancel",
    "encounter:complete",
    "order:read",
)

# Records staff own the death, referral and LAMA registers, and assign the ICD
# codes the doctor did not stop to look up (CLAUDE.md §8).
_RECORDS = (
    *_CHART_READ,
    "encounter:record_death",
    "encounter:record_referral",
    "encounter:record_lama",
    "diagnosis:record",
)

# Money-side roles see that a visit exists and what was ordered on it, never the
# clinical narrative. Their own clearance step arrives with `billing`.
_FINANCE = ("encounter:read", "order:read")

_TECH = ("encounter:read", "order:read", "order:fulfil")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "DOCTOR": _DOCTOR,
    "NURSE": _NURSE,
    "RECEPTIONIST": _RECEPTION,
    "RECORDS_OFFICER": _RECORDS,
    "CASHIER": _FINANCE,
    "BILLING_STAFF": _FINANCE,
    "LAB_TECH": _TECH,
    "RADIOLOGIST": (*_TECH, "note:read", "diagnosis:read"),
    "PHARMACIST": (*_TECH, "diagnosis:read"),
    "AUDITOR": _CHART_READ,
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
