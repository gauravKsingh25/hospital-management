"""medicine list and items issued to inpatients

Revision ID: 0666a25031c9
Revises: b35b0faab020
Create Date: 2026-09-26 08:21:31.978500+00:00

The medicine list — presets such as "NS 500 ml" or "Ceftriaxone 1 g", each
with a dose and a price — and the two ways an inpatient stay is charged from
it: a dose on the chart signed as given, and an item recorded as used.

Four parts:

1. **`medicine_items`**, the list. Prices are not stored here: they live in
   billing's `service_prices`, joined by `code`, like every other catalogue.
2. **`medicine_issues`**, what a stay used outside the chart.
3. **Three columns on `medication_orders`** linking a prescribed drug to the
   list, so a given dose can be billed. Existing orders get `units_per_dose` of
   1 and no item — they were typed free-hand and stay unbilled, as before.
4. **Permissions** `medicine:read`, `medicine:manage`, `medicine:issue`, and
   `charge:read` for the admission desk (the cost of a stay). The grants mirror
   each module's `rbac.py`; `tests/test_rbac_seed.py` fails if they disagree.

Both tables carry the same row-level security as every tenant table
(CLAUDE.md §3). Downgrade removes all of it, including the two enum types the
tables created — but not `medicationroute` or `drugschedule`, which
`medication_orders` already owned and still uses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0666a25031c9"
down_revision: str | None = "b35b0faab020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TABLES = ("medicine_items", "medicine_issues")

IPD = "ipd"

# (code, module, description) — mirrors `ipd/rbac.py`.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("medicine:read", IPD, "View the medicine list and its prices."),
    ("medicine:manage", IPD, "Add items to the medicine list and set their price."),
    (
        "medicine:issue",
        IPD,
        "Record medicines and supplies used on an inpatient; they go on the bill.",
    ),
)
_NEW_CODES = tuple(code for code, _, _ in PERMISSIONS)
_ALL_THREE = _NEW_CODES
_READ = ("medicine:read",)

# Mirrors each module's `MODULE_ROLE_PERMISSIONS`.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL_THREE,
    "HOSPITAL_ADMIN": _ALL_THREE,
    # Keeps the list and prices it; records what the stay used; sees the cost.
    "ADMISSION_DESK": (*_ALL_THREE, "charge:read"),
    # The formulary is the pharmacy's.
    "PHARMACIST": _ALL_THREE,
    # Records the cannula it used; does not price it.
    "NURSE": ("medicine:read", "medicine:issue"),
    "DOCTOR": _READ,
    "RECEPTIONIST": _READ,
    "CASHIER": _READ,
    "BILLING_STAFF": _READ,
    "RECORDS_OFFICER": _READ,
    "AUDITOR": _READ,
}

# Enum types `medication_orders` created long ago. Referenced, never re-created.
_ROUTE = postgresql.ENUM(
    "ORAL",
    "IV",
    "IM",
    "SC",
    "TOPICAL",
    "INHALED",
    "RECTAL",
    "OPHTHALMIC",
    "NASOGASTRIC",
    "OTHER",
    name="medicationroute",
    create_type=False,
)
_SCHEDULE = postgresql.ENUM(
    "NONE", "H", "H1", "X", "NARCOTIC", name="drugschedule", create_type=False
)


def upgrade() -> None:
    connection = op.get_bind()

    # --- 1. the list -------------------------------------------------------
    op.create_table(
        "medicine_items",
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
        sa.Column("code", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column(
            "form",
            sa.Enum(
                "INJECTION",
                "IV_FLUID",
                "TABLET",
                "CAPSULE",
                "SYRUP",
                "OINTMENT",
                "DROPS",
                "INHALER",
                "CONSUMABLE",
                "OTHER",
                name="medicineform",
            ),
            nullable=False,
        ),
        sa.Column("dose", sqlmodel.sql.sqltypes.AutoString(length=60), nullable=True),
        sa.Column("route", _ROUTE, nullable=True),
        sa.Column("unit", sqlmodel.sql.sqltypes.AutoString(length=24), nullable=False),
        sa.Column("drug_schedule", _SCHEDULE, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name=op.f("fk_medicine_items_hospital_id_hospitals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medicine_items")),
    )
    op.create_index(op.f("ix_medicine_items_code"), "medicine_items", ["code"], unique=False)
    op.create_index(
        op.f("ix_medicine_items_deleted_at"), "medicine_items", ["deleted_at"], unique=False
    )
    op.create_index(op.f("ix_medicine_items_form"), "medicine_items", ["form"], unique=False)
    op.create_index(
        op.f("ix_medicine_items_hospital_id"), "medicine_items", ["hospital_id"], unique=False
    )
    op.create_index(
        "ix_medicine_items_hospital_id_form",
        "medicine_items",
        ["hospital_id", "form"],
        unique=False,
    )
    op.create_index(
        op.f("ix_medicine_items_is_active"), "medicine_items", ["is_active"], unique=False
    )
    op.create_index(op.f("ix_medicine_items_name"), "medicine_items", ["name"], unique=False)
    op.create_index(
        "uq_medicine_items_code_live",
        "medicine_items",
        ["hospital_id", "code"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- 2. what a stay used -----------------------------------------------
    op.create_table(
        "medicine_issues",
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
        sa.Column("admission_id", sa.Uuid(), nullable=False),
        sa.Column("encounter_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("medicine_item_id", sa.Uuid(), nullable=False),
        sa.Column("item_code", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("item_name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column("unit", sqlmodel.sql.sqltypes.AutoString(length=24), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column(
            "status", sa.Enum("ISSUED", "REVERSED", name="medicineissuestatus"), nullable=False
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by_id", sa.Uuid(), nullable=True),
        sa.Column("issued_by_name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_by_id", sa.Uuid(), nullable=True),
        sa.Column("reversal_reason", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["admission_id"],
            ["admissions.id"],
            name=op.f("fk_medicine_issues_admission_id_admissions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["encounter_id"],
            ["encounters.id"],
            name=op.f("fk_medicine_issues_encounter_id_encounters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name=op.f("fk_medicine_issues_hospital_id_hospitals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"],
            ["users.id"],
            name=op.f("fk_medicine_issues_issued_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["medicine_item_id"],
            ["medicine_items.id"],
            name=op.f("fk_medicine_issues_medicine_item_id_medicine_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name=op.f("fk_medicine_issues_patient_id_patients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_id"],
            ["users.id"],
            name=op.f("fk_medicine_issues_reversed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medicine_issues")),
    )
    op.create_index(
        op.f("ix_medicine_issues_admission_id"), "medicine_issues", ["admission_id"], unique=False
    )
    op.create_index(
        "ix_medicine_issues_admission_id_status",
        "medicine_issues",
        ["admission_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_medicine_issues_deleted_at"), "medicine_issues", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_medicine_issues_encounter_id"), "medicine_issues", ["encounter_id"], unique=False
    )
    op.create_index(
        op.f("ix_medicine_issues_hospital_id"), "medicine_issues", ["hospital_id"], unique=False
    )
    op.create_index(
        op.f("ix_medicine_issues_issued_at"), "medicine_issues", ["issued_at"], unique=False
    )
    op.create_index(
        op.f("ix_medicine_issues_medicine_item_id"),
        "medicine_issues",
        ["medicine_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_medicine_issues_patient_id"), "medicine_issues", ["patient_id"], unique=False
    )
    op.create_index(op.f("ix_medicine_issues_status"), "medicine_issues", ["status"], unique=False)

    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # --- 3. the chart, linked to the list ----------------------------------
    op.add_column("medication_orders", sa.Column("medicine_item_id", sa.Uuid(), nullable=True))
    op.add_column(
        "medication_orders",
        sa.Column("item_code", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
    )
    # Backfilled through a temporary default: every existing order is one unit
    # a dose. The default is dropped afterwards so the column matches the
    # model, which sets it in Python.
    op.add_column(
        "medication_orders",
        sa.Column(
            "units_per_dose",
            sa.Numeric(precision=9, scale=2),
            server_default=sa.text("1.00"),
            nullable=False,
        ),
    )
    op.alter_column("medication_orders", "units_per_dose", server_default=None)
    op.create_index(
        op.f("ix_medication_orders_medicine_item_id"),
        "medication_orders",
        ["medicine_item_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_medication_orders_medicine_item_id_medicine_items"),
        "medication_orders",
        "medicine_items",
        ["medicine_item_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- 4. permissions and grants -----------------------------------------
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

    # The desk's `charge:read` is the one grant here of a permission this
    # migration did not create; it goes, the permission stays.
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE role_id IN (
                    SELECT id FROM roles WHERE code = 'ADMISSION_DESK' AND hospital_id IS NULL
                   )
               AND permission_id IN (SELECT id FROM permissions WHERE code = 'charge:read')
            """
        )
    )
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

    op.drop_constraint(
        op.f("fk_medication_orders_medicine_item_id_medicine_items"),
        "medication_orders",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_medication_orders_medicine_item_id"), table_name="medication_orders")
    op.drop_column("medication_orders", "units_per_dose")
    op.drop_column("medication_orders", "item_code")
    op.drop_column("medication_orders", "medicine_item_id")

    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_index(op.f("ix_medicine_issues_status"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_patient_id"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_medicine_item_id"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_issued_at"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_hospital_id"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_encounter_id"), table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_deleted_at"), table_name="medicine_issues")
    op.drop_index("ix_medicine_issues_admission_id_status", table_name="medicine_issues")
    op.drop_index(op.f("ix_medicine_issues_admission_id"), table_name="medicine_issues")
    op.drop_table("medicine_issues")
    op.drop_index(
        "uq_medicine_items_code_live",
        table_name="medicine_items",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_medicine_items_name"), table_name="medicine_items")
    op.drop_index(op.f("ix_medicine_items_is_active"), table_name="medicine_items")
    op.drop_index("ix_medicine_items_hospital_id_form", table_name="medicine_items")
    op.drop_index(op.f("ix_medicine_items_hospital_id"), table_name="medicine_items")
    op.drop_index(op.f("ix_medicine_items_form"), table_name="medicine_items")
    op.drop_index(op.f("ix_medicine_items_deleted_at"), table_name="medicine_items")
    op.drop_index(op.f("ix_medicine_items_code"), table_name="medicine_items")
    op.drop_table("medicine_items")

    sa.Enum(name="medicineissuestatus").drop(connection, checkfirst=True)
    sa.Enum(name="medicineform").drop(connection, checkfirst=True)
