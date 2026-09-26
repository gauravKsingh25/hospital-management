"""admission requests and the admission desk role

Revision ID: b35b0faab020
Revises: c7a1e5f2d9b3
Create Date: 2026-09-26 06:55:33.614310+00:00

The OPD → admission desk hand-off. Reception marks a patient seen, sends them
for admission, and the admission desk admits them from its own list.

Three parts, in the order they depend on each other:

1. **`admission_requests`**, with the same row-level security every tenant
   table carries (CLAUDE.md §3). One pending request per patient is enforced
   by a partial unique index, not only by the service's check — two counters
   clicking at once both pass the check.
2. **Two permissions** in `ipd`: `admission:request` (send a patient) and
   `admission:desk` (work the desk). Admitting still needs `admission:create`
   as well; working the list is not by itself licence to fill beds.
3. **`ADMISSION_DESK`**, a system role, added the way a hospital would add its
   own — rows in `roles` and `role_permissions`, no authorisation code
   changed. Reception keeps `admission:create` exactly as before; it gains
   only `admission:request`. The grants mirror each module's `rbac.py`, and
   `tests/test_rbac_seed.py` fails if the two ever disagree.

Downgrade removes all three, including the enum type the table created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b35b0faab020"
down_revision: str | None = "c7a1e5f2d9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

IPD = "ipd"

# (code, module, description) — mirrors `ipd/rbac.py`.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("admission:request", IPD, "Send a seen patient to the admission desk."),
    (
        "admission:desk",
        IPD,
        "Work the admission desk: admit patients sent from OPD, or turn a request away.",
    ),
)
_NEW_CODES = tuple(code for code, _, _ in PERMISSIONS)

ROLE = (
    "ADMISSION_DESK",
    "Admission Desk",
    "Admits patients sent from OPD: bed, admission paperwork.",
)

# Mirrors each module's `MODULE_ROLE_PERMISSIONS` (and identity's baseline).
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "HOSPITAL_ADMIN": _NEW_CODES,
    "RECEPTIONIST": ("admission:request",),
    "DOCTOR": ("admission:request",),
    "ADMISSION_DESK": (
        # identity
        "hospital:read",
        # patients — the address and guardian an inpatient stay needs
        "patient:read",
        "patient:update",
        # scheduling — who sent them, from which department
        "department:read",
        "doctor:read",
        # clinical — the visit being admitted, and what it was for
        "encounter:read",
        "diagnosis:read",
        # ipd — the desk itself, and the bed
        "ward:read",
        "bed:read",
        "bed:assign",
        "admission:read",
        "admission:create",
        "admission:update",
        "admission:request",
        "admission:desk",
    ),
}


def upgrade() -> None:
    connection = op.get_bind()

    # --- 1. the table ------------------------------------------------------
    op.create_table(
        "admission_requests",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hospital_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("encounter_id", sa.Uuid(), nullable=False),
        sa.Column("doctor_id", sa.Uuid(), nullable=True),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("doctor_name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "ADMITTED", "CANCELLED", name="admissionrequeststatus"),
            nullable=False,
        ),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by_id", sa.Uuid(), nullable=True),
        sa.Column("admission_id", sa.Uuid(), nullable=True),
        sa.Column(
            "cancellation_reason", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["admission_id"],
            ["admissions.id"],
            name=op.f("fk_admission_requests_admission_id_admissions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_admission_requests_department_id_departments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctors.id"],
            name=op.f("fk_admission_requests_doctor_id_doctors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["encounter_id"],
            ["encounters.id"],
            name=op.f("fk_admission_requests_encounter_id_encounters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["handled_by_id"],
            ["users.id"],
            name=op.f("fk_admission_requests_handled_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name=op.f("fk_admission_requests_hospital_id_hospitals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name=op.f("fk_admission_requests_patient_id_patients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_admission_requests_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admission_requests")),
    )
    op.create_index(
        op.f("ix_admission_requests_admission_id"),
        "admission_requests",
        ["admission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admission_requests_deleted_at"), "admission_requests", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_admission_requests_department_id"),
        "admission_requests",
        ["department_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admission_requests_doctor_id"), "admission_requests", ["doctor_id"], unique=False
    )
    op.create_index(
        op.f("ix_admission_requests_encounter_id"),
        "admission_requests",
        ["encounter_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admission_requests_hospital_id"),
        "admission_requests",
        ["hospital_id"],
        unique=False,
    )
    op.create_index(
        "ix_admission_requests_hospital_id_status_requested_at",
        "admission_requests",
        ["hospital_id", "status", "requested_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admission_requests_patient_id"), "admission_requests", ["patient_id"], unique=False
    )
    op.create_index(
        op.f("ix_admission_requests_status"), "admission_requests", ["status"], unique=False
    )
    op.create_index(
        "uq_admission_requests_patient_pending",
        "admission_requests",
        ["hospital_id", "patient_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND status = 'PENDING'"),
    )

    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    op.execute("ALTER TABLE admission_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE admission_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON admission_requests
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )

    # --- 2. permissions ----------------------------------------------------
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

    # --- 3. the role, and every grant --------------------------------------
    code, name, description = ROLE
    connection.execute(
        sa.text(
            """
            INSERT INTO roles
                (id, hospital_id, code, name, description, is_system, created_at, updated_at)
            VALUES (gen_random_uuid(), NULL, :code, :name, :description, true, now(), now())
            ON CONFLICT DO NOTHING
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
             WHERE permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
                OR role_id IN (
                    SELECT id FROM roles WHERE code = :role AND hospital_id IS NULL
                )
            """
        ),
        {"codes": list(_NEW_CODES), "role": ROLE[0]},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_NEW_CODES)}
    )
    # Only the system row. A user still holding the role blocks this via the
    # user_roles foreign key, which is the right outcome: a downgrade must not
    # silently strip somebody's access.
    connection.execute(
        sa.text("DELETE FROM roles WHERE code = :role AND hospital_id IS NULL"),
        {"role": ROLE[0]},
    )

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON admission_requests")
    op.drop_index(
        "uq_admission_requests_patient_pending",
        table_name="admission_requests",
        postgresql_where=sa.text("deleted_at IS NULL AND status = 'PENDING'"),
    )
    op.drop_index(op.f("ix_admission_requests_status"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_patient_id"), table_name="admission_requests")
    op.drop_index(
        "ix_admission_requests_hospital_id_status_requested_at", table_name="admission_requests"
    )
    op.drop_index(op.f("ix_admission_requests_hospital_id"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_encounter_id"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_doctor_id"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_department_id"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_deleted_at"), table_name="admission_requests")
    op.drop_index(op.f("ix_admission_requests_admission_id"), table_name="admission_requests")
    op.drop_table("admission_requests")

    sa.Enum(name="admissionrequeststatus").drop(connection, checkfirst=True)
