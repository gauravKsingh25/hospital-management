"""reporting views and permissions

Revision ID: b4f9d20ac6e1
Revises: 8d31c7ae04b2
Create Date: 2026-08-12 07:31:00.000000+00:00

`reporting` owns no tables — it owns these six views, plus its permission seed.
The reasoning for that shape is in `app/modules/reporting/models.py`; the two
properties worth repeating where the SQL actually lives are:

**`WITH (security_invoker = true)` on every view, without exception.** A
Postgres view runs with the *owner's* privileges by default. The owner here is
the migration role, which on Neon (and RDS, and Cloud SQL) carries `BYPASSRLS`.
A view created without `security_invoker` would therefore read its base tables
with row-level security switched off, and one unqualified `SELECT` would return
every hospital's data to any tenant. That is precisely the leak CLAUDE.md §3
builds RLS to prevent, reopened by a default nobody looks at.
`tests/test_reporting_isolation.py` queries every view as a bound tenant and
asserts it sees only its own rows.

**Local dates, not UTC dates.** `(ts AT TIME ZONE h.timezone)::date`. For
`Asia/Kolkata` the hospital's day ends at 18:30 UTC, so grouping by UTC date
would file every evening's last five and a half hours under tomorrow. Footfall
would be wrong daily, by a margin that looks entirely plausible.

Downgrade drops the views. Nothing else depends on them, and no data is lost —
which is the other quiet advantage of a module that stores nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4f9d20ac6e1"
down_revision: str | None = "8d31c7ae04b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REPORTING = "reporting"

PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("report:operational", REPORTING, "Patient volumes, occupancy and queue analytics."),
    ("report:clinical", REPORTING, "Clinical outcome and follow-up metrics."),
    ("report:revenue", REPORTING, "Revenue, collections and outstanding balances."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

# Money is its own permission. A ward sister needs the bed board and the day's
# footfall; she has no business seeing the hospital's collections, and a single
# `report:read` would have handed her both.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "AUDITOR": _ALL,
    "DOCTOR": ("report:operational", "report:clinical"),
    "NURSE": ("report:operational",),
    "RECEPTIONIST": ("report:operational",),
    "CASHIER": ("report:operational", "report:revenue"),
    "BILLING_STAFF": ("report:operational", "report:revenue"),
    "RECORDS_OFFICER": ("report:operational", "report:clinical"),
}


# ---------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------
VIEWS: tuple[tuple[str, str], ...] = (
    (
        "reporting_encounters",
        """
        SELECT
            e.id,
            e.hospital_id,
            e.patient_id,
            e.doctor_id,
            e.department_id,
            e.encounter_number,
            e.encounter_type,
            e.status,
            (e.started_at AT TIME ZONE h.timezone)::date       AS local_date,
            e.started_at,
            e.consultation_started_at,
            e.consultation_completed_at,
            e.closed_at,
            e.closed_automatically,
            e.follow_up_date,
            -- How long the patient sat there. NULL until they are called, which
            -- is correct: an unseen patient has no final waiting time yet.
            EXTRACT(EPOCH FROM (e.consultation_started_at - e.started_at)) / 60.0
                                                               AS waited_minutes,
            EXTRACT(EPOCH FROM (e.consultation_completed_at - e.consultation_started_at)) / 60.0
                                                               AS consultation_minutes,
            -- "Seen" means a doctor started the consultation. Deliberately not
            -- derived from status: a visit can close as COMPLETED via the
            -- auto-close sweep without anybody having seen the patient, and
            -- counting that as attendance would flatter every clinic's numbers.
            (e.consultation_started_at IS NOT NULL)             AS was_seen,
            (e.status IN ('CANCELLED', 'NO_SHOW'))              AS was_lost
        FROM encounters e
        JOIN hospitals h ON h.id = e.hospital_id
        WHERE e.deleted_at IS NULL
        """,
    ),
    (
        "reporting_admissions",
        """
        SELECT
            a.id,
            a.hospital_id,
            a.patient_id,
            a.encounter_id,
            a.attending_doctor_id,
            a.department_id,
            a.admission_number,
            a.status,
            a.discharge_type,
            (a.admitted_at AT TIME ZONE h.timezone)::date       AS local_date,
            (a.discharged_at AT TIME ZONE h.timezone)::date     AS discharged_date,
            a.admitted_at,
            a.discharged_at,
            a.bed_days_charged,
            -- Counted the way a ward counts it, and the way `ipd.length_of_stay`
            -- does: a patient admitted and discharged the same afternoon still
            -- occupied a bed, so the floor is 1 rather than 0. An ALOS that
            -- records day cases as zero understates every ward's workload.
            GREATEST(
                (COALESCE(a.discharged_at, now()) AT TIME ZONE h.timezone)::date
                    - (a.admitted_at AT TIME ZONE h.timezone)::date,
                1
            )                                                   AS length_of_stay_days,
            (a.status IN ('ADMITTED', 'DISCHARGE_INITIATED'))   AS is_open
        FROM admissions a
        JOIN hospitals h ON h.id = a.hospital_id
        WHERE a.deleted_at IS NULL
          -- An admission made in error is not a stay. Including it would inflate
          -- both admission counts and ALOS.
          AND a.status <> 'CANCELLED'
        """,
    ),
    (
        "reporting_charges",
        """
        SELECT
            c.id,
            c.hospital_id,
            c.encounter_id,
            c.patient_id,
            c.invoice_id,
            c.category,
            c.status,
            c.source_module,
            c.needs_pricing,
            (c.captured_at AT TIME ZONE h.timezone)::date       AS local_date,
            c.captured_at,
            c.quantity,
            c.taxable_amount,
            c.discount_amount,
            (c.cgst_amount + c.sgst_amount + c.igst_amount)     AS tax_amount,
            c.total_amount,
            -- What the hospital actually expects to be paid for this act.
            -- Cancelled and waived charges are kept in the view rather than
            -- filtered out, because "how much did we waive last month" is a
            -- question management asks and a WHERE clause here would make it
            -- unanswerable.
            CASE
                WHEN c.status IN ('CANCELLED', 'WAIVED') THEN 0
                ELSE c.total_amount
            END                                                 AS billable_amount
        FROM charges c
        JOIN hospitals h ON h.id = c.hospital_id
        WHERE c.deleted_at IS NULL
        """,
    ),
    (
        "reporting_payments",
        """
        SELECT
            p.id,
            p.hospital_id,
            p.invoice_id,
            p.patient_id,
            p.receipt_number,
            p.method,
            p.status,
            (p.received_at AT TIME ZONE h.timezone)::date       AS local_date,
            p.received_at,
            p.amount,
            (p.reverses_payment_id IS NOT NULL)                 AS is_reversal,
            -- A reversal is two rows, not an edit: the original is stamped
            -- REVERSED and a mirrored row is written against it, so the cash
            -- drawer can still show that money was taken and given back.
            -- Both rows end up REVERSED, which makes net collections simply
            -- "the RECORDED ones" — no signed arithmetic, no double subtraction.
            CASE WHEN p.status = 'RECORDED' THEN p.amount ELSE 0 END
                                                                AS net_amount
        FROM payments p
        JOIN hospitals h ON h.id = p.hospital_id
        WHERE p.deleted_at IS NULL
        """,
    ),
    (
        "reporting_beds",
        """
        SELECT
            b.id,
            b.hospital_id,
            b.ward_id,
            w.code                                              AS ward_code,
            w.name                                              AS ward_name,
            w.department_id,
            b.code                                              AS bed_code,
            b.bed_class,
            b.status,
            b.is_active,
            -- Out-of-service beds are excluded from the denominator: a ward
            -- closed for renovation has not made the hospital 100% full, and a
            -- rate that says so sends people hunting for capacity that is not
            -- there.
            (b.is_active AND b.status <> 'OUT_OF_SERVICE')      AS is_usable,
            (b.status = 'OCCUPIED')                             AS is_occupied
        FROM beds b
        JOIN wards w ON w.id = b.ward_id
        WHERE b.deleted_at IS NULL
          AND w.deleted_at IS NULL
        """,
    ),
    (
        "reporting_queue",
        """
        SELECT
            q.id,
            q.hospital_id,
            q.queue_date,
            q.token_number,
            q.doctor_id,
            q.department_id,
            q.patient_id,
            q.status,
            q.priority,
            q.checked_in_at,
            q.called_at,
            q.started_at,
            q.completed_at,
            q.skip_count,
            -- For a patient still waiting this counts up from check-in, which is
            -- what makes "longest wait on the floor right now" answerable
            -- without a second query.
            EXTRACT(EPOCH FROM (COALESCE(q.called_at, now()) - q.checked_in_at)) / 60.0
                                                                AS waited_minutes,
            (q.called_at IS NULL AND q.status = 'WAITING')      AS is_waiting
        FROM queue_entries q
        WHERE q.deleted_at IS NULL
        """,
    ),
)


def upgrade() -> None:
    connection = op.get_bind()

    for name, body in VIEWS:
        # security_invoker is the whole ballgame — see the module docstring.
        op.execute(f"CREATE VIEW {name} WITH (security_invoker = true) AS {body}")

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

    for name, _ in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
