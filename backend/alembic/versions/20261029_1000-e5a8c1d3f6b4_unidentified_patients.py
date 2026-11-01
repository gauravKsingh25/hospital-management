"""unidentified emergency patients

Revision ID: e5a8c1d3f6b4
Revises: d4f7b0c2e5a3
Create Date: 2026-10-29 10:00:00.000000+00:00

An unconscious patient has no name and no mobile. Until now registration
demanded both, so casualty would have typed fake ones. This lets a record say
plainly that nobody knows yet who the patient is:

* `patients.phone` becomes nullable — but only on an unidentified record, which
  a check constraint holds the database to;
* `is_unidentified`, `emergency_tag` and `identified_at` record the state;
* `uhid_sequences` gains a `series`, so the `EM-0034` tags count on the same
  locked-row rule as UHIDs;
* `suppressionreason` gains `UNIDENTIFIED`, the reason a patient message to
  such a record is held.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e5a8c1d3f6b4"
down_revision: str | None = "d4f7b0c2e5a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("is_unidentified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "patients",
        sa.Column("emergency_tag", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True),
    )
    op.add_column(
        "patients",
        sa.Column("identified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_patients_is_unidentified"), "patients", ["is_unidentified"], unique=False
    )
    op.alter_column(
        "patients",
        "phone",
        existing_type=sqlmodel.sql.sqltypes.AutoString(length=15),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_patients_phone_or_unidentified",
        "patients",
        "phone IS NOT NULL OR is_unidentified",
    )

    op.add_column(
        "uhid_sequences",
        sa.Column(
            "series",
            sqlmodel.sql.sqltypes.AutoString(length=8),
            server_default="UHID",
            nullable=False,
        ),
    )
    op.drop_index("uq_uhid_sequences_hospital_year", table_name="uhid_sequences")
    op.create_index(
        "uq_uhid_sequences_hospital_series_year",
        "uhid_sequences",
        ["hospital_id", "series", "year"],
        unique=True,
    )

    # Postgres 12+ accepts this inside the migration's transaction; the value
    # is not used until a later one.
    op.execute("ALTER TYPE suppressionreason ADD VALUE IF NOT EXISTS 'UNIDENTIFIED'")


def downgrade() -> None:
    # `UNIDENTIFIED` stays in the enum type: Postgres cannot drop an enum value,
    # and an unused label is harmless. Rows that carry it are reworded first so
    # a re-upgrade finds nothing it does not expect.
    op.execute(
        "UPDATE notifications SET suppression_reason = 'STAFF_BLOCK' "
        "WHERE suppression_reason = 'UNIDENTIFIED'"
    )

    op.execute("DELETE FROM uhid_sequences WHERE series <> 'UHID'")
    op.drop_index("uq_uhid_sequences_hospital_series_year", table_name="uhid_sequences")
    op.create_index(
        "uq_uhid_sequences_hospital_year", "uhid_sequences", ["hospital_id", "year"], unique=True
    )
    op.drop_column("uhid_sequences", "series")

    op.drop_constraint("ck_patients_phone_or_unidentified", "patients", type_="check")
    # A patient still unidentified has no number to restore; the column cannot
    # go back to NOT NULL while they exist, so they are given an empty one.
    op.execute("UPDATE patients SET phone = '' WHERE phone IS NULL")
    op.alter_column(
        "patients",
        "phone",
        existing_type=sqlmodel.sql.sqltypes.AutoString(length=15),
        nullable=False,
    )
    op.drop_index(op.f("ix_patients_is_unidentified"), table_name="patients")
    op.drop_column("patients", "identified_at")
    op.drop_column("patients", "emergency_tag")
    op.drop_column("patients", "is_unidentified")
