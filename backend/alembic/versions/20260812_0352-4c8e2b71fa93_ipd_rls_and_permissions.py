"""ipd rls and permissions

Revision ID: 4c8e2b71fa93
Revises: 576a17eb0126
Create Date: 2026-08-12 03:52:00.000000+00:00

Row-level security for the inpatient tables, on the pattern the identity,
patients, scheduling, clinical, diagnostics, billing and notifications
migrations established, plus this module's permission seed.

The seed mirrors `ipd/rbac.py`, which is where the route handlers import these
codes from. Three splits in it are deliberate and match how a ward actually runs:

* **Giving a drug is not prescribing one.** `medication:administer` goes to
  nurses, `medication:prescribe` to doctors. A ward where the person who writes
  the order also signs for the dose has no second pair of eyes, and the second
  pair of eyes is most of what a medication chart is for.
* **Moving a patient is not managing capacity.** `bed:assign` is held widely;
  `ward:manage` is not. A hospital whose bed count changes because somebody
  mistyped has lost its census.
* **Discharging is not writing the summary.** The ward clerk can complete the
  paperwork; only a clinician signs the document.

`bed:clean` is housekeeping's action, but CLAUDE.md §8 has no housekeeping role
to give it to, so it sits with nursing and hospital administration for now. A
hospital with a housekeeping team adds the role as data — RBAC is data-driven,
so that is an INSERT, not a deploy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8e2b71fa93"
down_revision: str | None = "576a17eb0126"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "wards",
    "beds",
    "admissions",
    "bed_assignments",
    "medication_orders",
    "medication_administrations",
    "discharge_summaries",
    "admission_sequences",
)

IPD = "ipd"

# (code, module, description) — mirrors `ipd/rbac.py`.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("ward:read", IPD, "View wards and the bed board."),
    ("ward:manage", IPD, "Create or edit wards and beds."),
    ("bed:read", IPD, "View bed occupancy."),
    ("bed:assign", IPD, "Assign or transfer a patient's bed."),
    ("bed:clean", IPD, "Mark a bed cleaned and ready."),
    ("bed:block", IPD, "Take a bed out of service, or restore it."),
    ("admission:read", IPD, "View admissions and the ward census."),
    ("admission:create", IPD, "Admit a patient."),
    ("admission:update", IPD, "Edit admission details."),
    ("admission:discharge", IPD, "Discharge an inpatient."),
    ("admission:cancel", IPD, "Cancel an admission made in error."),
    ("medication:read", IPD, "View the medication chart."),
    ("medication:prescribe", IPD, "Prescribe or stop a drug."),
    ("medication:administer", IPD, "Record a dose given or withheld."),
    ("summary:read", IPD, "Read discharge summaries."),
    ("summary:write", IPD, "Compile or edit a discharge summary."),
    ("summary:sign", IPD, "Sign and release a discharge summary."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

_READ = ("ward:read", "bed:read", "admission:read", "medication:read", "summary:read")

# The ward: everything at the bedside, and nothing that changes what the hospital
# owns or what a document says clinically.
_NURSE = (
    *_READ,
    "bed:assign",
    "bed:clean",
    "admission:create",
    "admission:update",
    "medication:administer",
    "summary:write",
)

# The consultant: prescribes, writes up, signs. Sees the bed board without
# running it — "is there an ICU bed" changes a clinical decision.
_DOCTOR = (
    *_READ,
    "admission:create",
    "admission:update",
    "admission:discharge",
    "medication:prescribe",
    "summary:write",
    "summary:sign",
)

_RECEPTION = (
    *_READ,
    "bed:assign",
    "admission:create",
    "admission:update",
    "admission:discharge",
)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "NURSE": _NURSE,
    "DOCTOR": _DOCTOR,
    "RECEPTIONIST": _RECEPTION,
    # The counter needs the bed class to price the stay, and whether the patient
    # is still in.
    "CASHIER": ("ward:read", "bed:read", "admission:read"),
    "BILLING_STAFF": ("ward:read", "bed:read", "admission:read"),
    # The pharmacy reads the chart to dispense against it, and does not sign for
    # a dose given at the bedside.
    "PHARMACIST": ("admission:read", "medication:read"),
    # The lab and radiology need to know which ward a report goes to.
    "LAB_TECH": ("ward:read", "admission:read"),
    "RADIOLOGIST": ("ward:read", "admission:read"),
    # Records officers hold the medical record, including summaries, and record
    # the death and LAMA entries that end a stay.
    "RECORDS_OFFICER": (*_READ, "admission:discharge", "summary:write"),
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
