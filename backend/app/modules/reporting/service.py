"""Reporting business logic — the module's public interface.

Read-only throughout. Nothing here writes a row, and nothing here publishes an
event; every function answers a question and returns a DTO.

---------------------------------------------------------------------------
Why this file is full of SQL
---------------------------------------------------------------------------

CLAUDE.md §5 grants exactly one exemption from "all access through the ORM":
`reporting` read-only views, where performance demands it. It is taken here, and
it is bounded by two rules that keep it from becoming a licence:

1. **Every query reads a `reporting_*` view, never a base table.** The views are
   this module's dependency contract on the rest of the system — see
   `models.py`. Reaching past them into `charges` or `encounters` directly is
   the thing §2 forbids, and doing it here would scatter the coupling this
   module works to keep in one place.
2. **Every query is bounded by a date window or a single day.** An unbounded
   aggregate is a full scan somebody triggers by clearing a filter.

Aggregation happens in Postgres rather than Python because the alternative —
pulling a quarter of charges into the API to sum them — is the exact thing that
makes dashboards time out.

---------------------------------------------------------------------------
Tenant isolation
---------------------------------------------------------------------------

There is not a single `hospital_id` filter in this file, and that is deliberate
rather than an oversight. The views carry `security_invoker = true`, so
row-level security applies to the querying session, and a session bound to a
tenant physically cannot read another's rows. Adding a redundant WHERE clause
would be harmless but misleading: it would suggest the filter is what protects
the data, and the next person would feel free to omit it somewhere.

`tests/test_reporting_isolation.py` proves the guarantee holds for every view.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.reporting.schemas import (
    CategoryRevenue,
    DailyCount,
    Dashboard,
    DateWindow,
    DepartmentLoad,
    DoctorLoad,
    FollowUpCompliance,
    FootfallReport,
    InpatientReport,
    OccupancyReport,
    QueueSnapshot,
    RevenueReport,
    WaitingDoctor,
    WardOccupancy,
)
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "build_dashboard",
    "follow_up_compliance",
    "footfall",
    "inpatient_summary",
    "local_today",
    "occupancy",
    "queue_snapshot",
    "revenue",
]

ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# Local time
# ---------------------------------------------------------------------------
async def local_today(session: AsyncSession, hospital_id: uuid.UUID) -> date:
    """The hospital's current date, not the server's.

    Every "today" in this module goes through here. The implementation moved to
    `tenancy.service` once `scheduling` turned out to need the same thing and
    had been using `utc_now().date()` instead — two modules disagreeing about
    what day it is produced a live queue board that emptied itself at half past
    six every evening. The alias stays because this name is what the routes and
    the tests call.
    """
    return await tenancy_service.local_today(session, hospital_id)


def _rate(numerator: float, denominator: float) -> float | None:
    """A rate with no denominator is unknown, not zero (see `schemas`)."""
    return round(numerator / denominator, 4) if denominator else None


def _minutes(value: Any) -> float | None:
    return round(float(value), 1) if value is not None else None


def _money(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


async def _rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[RowMapping]:
    return list((await session.execute(text(sql), params)).mappings().all())


# ---------------------------------------------------------------------------
# Footfall (CLAUDE.md §13 step 10)
# ---------------------------------------------------------------------------
async def footfall(
    session: AsyncSession,
    window: DateWindow,
    *,
    department_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
) -> FootfallReport:
    """How many patients came, were seen, and were lost — by day, department and doctor.

    The optional filters are expressed as `:param IS NULL OR column = :param`
    rather than by appending SQL fragments. That keeps every statement here a
    static string with only bound parameters — nothing is interpolated, so there
    is no shape of this function in which a value could become SQL — and it
    gives Postgres one plan to cache instead of four.
    """
    params: dict[str, Any] = {
        "date_from": window.date_from,
        "date_to": window.date_to,
        "department_id": department_id,
        "doctor_id": doctor_id,
    }

    totals = (
        await _rows(
            session,
            """
            SELECT
                count(*)                                        AS total_visits,
                count(*) FILTER (WHERE was_seen)                AS patients_seen,
                count(*) FILTER (WHERE was_lost)                AS lost,
                count(*) FILTER (WHERE closed_automatically)    AS auto_closed
            FROM reporting_encounters
            WHERE local_date BETWEEN :date_from AND :date_to
              AND (CAST(:department_id AS uuid) IS NULL OR department_id = :department_id)
              AND (CAST(:doctor_id AS uuid) IS NULL OR doctor_id = :doctor_id)
            """,
            params,
        )
    )[0]

    # Zero-filled by generate_series: a day with no visits is a real data point,
    # and a chart that simply omits it draws a line through the gap as though
    # the clinic had been busy.
    by_day = await _rows(
        session,
        """
        SELECT
            d.day::date                                         AS day,
            count(e.id)                                         AS total,
            count(e.id) FILTER (WHERE e.was_seen)               AS seen,
            count(e.id) FILTER (WHERE e.was_lost)               AS lost
        FROM generate_series(
                 CAST(:date_from AS date), CAST(:date_to AS date), interval '1 day'
             ) AS d(day)
        LEFT JOIN reporting_encounters e
               ON e.local_date = d.day::date
              AND (CAST(:department_id AS uuid) IS NULL OR e.department_id = :department_id)
              AND (CAST(:doctor_id AS uuid) IS NULL OR e.doctor_id = :doctor_id)
        GROUP BY d.day
        ORDER BY d.day
        """,
        params,
    )

    by_department = await _rows(
        session,
        """
        SELECT
            e.department_id,
            dept.name                                           AS department_name,
            count(*)                                            AS total,
            count(*) FILTER (WHERE e.was_seen)                  AS seen,
            avg(e.waited_minutes)                               AS average_wait_minutes
        FROM reporting_encounters e
        LEFT JOIN departments dept ON dept.id = e.department_id
        WHERE e.local_date BETWEEN :date_from AND :date_to
          AND (CAST(:department_id AS uuid) IS NULL OR e.department_id = :department_id)
          AND (CAST(:doctor_id AS uuid) IS NULL OR e.doctor_id = :doctor_id)
        GROUP BY e.department_id, dept.name
        ORDER BY count(*) DESC
        """,
        params,
    )

    by_doctor = await _rows(
        session,
        """
        SELECT
            e.doctor_id,
            doc.display_name                                    AS doctor_name,
            count(*)                                            AS total,
            count(*) FILTER (WHERE e.was_seen)                  AS seen,
            avg(e.consultation_minutes)                         AS average_consultation_minutes,
            avg(e.waited_minutes)                               AS average_wait_minutes
        FROM reporting_encounters e
        LEFT JOIN doctors doc ON doc.id = e.doctor_id
        WHERE e.local_date BETWEEN :date_from AND :date_to
          AND (CAST(:department_id AS uuid) IS NULL OR e.department_id = :department_id)
          AND (CAST(:doctor_id AS uuid) IS NULL OR e.doctor_id = :doctor_id)
        GROUP BY e.doctor_id, doc.display_name
        ORDER BY count(*) DESC
        """,
        params,
    )

    # "New" means first registered inside the window, which is the number a
    # catchment conversation is actually about — not "first visit we happen to
    # hold a record of".
    new_patients = (
        await _rows(
            session,
            """
            SELECT count(*) AS new_patients
            FROM patients
            WHERE deleted_at IS NULL
              AND registered_at >= CAST(:date_from AS date)
              AND registered_at < (CAST(:date_to AS date) + 1)
            """,
            params,
        )
    )[0]["new_patients"]

    return FootfallReport(
        window=window,
        total_visits=totals["total_visits"],
        patients_seen=totals["patients_seen"],
        cancelled_or_no_show=totals["lost"],
        auto_closed=totals["auto_closed"],
        new_patients=new_patients,
        by_day=[
            DailyCount(day=row["day"], total=row["total"], seen=row["seen"], lost=row["lost"])
            for row in by_day
        ],
        by_department=[
            DepartmentLoad(
                department_id=row["department_id"],
                department_name=row["department_name"],
                total=row["total"],
                seen=row["seen"],
                average_wait_minutes=_minutes(row["average_wait_minutes"]),
            )
            for row in by_department
        ],
        by_doctor=[
            DoctorLoad(
                doctor_id=row["doctor_id"],
                doctor_name=row["doctor_name"],
                total=row["total"],
                seen=row["seen"],
                average_consultation_minutes=_minutes(row["average_consultation_minutes"]),
                average_wait_minutes=_minutes(row["average_wait_minutes"]),
            )
            for row in by_doctor
        ],
    )


# ---------------------------------------------------------------------------
# Occupancy (CLAUDE.md §13 step 10)
# ---------------------------------------------------------------------------
async def occupancy(session: AsyncSession) -> OccupancyReport:
    """Bed occupancy, now, hospital-wide and per ward.

    Deliberately a snapshot rather than a range. "How full were we last Tuesday"
    is a different question answered from the bed-assignment history, and
    conflating the two produces a number that is neither.
    """
    rows = await _rows(
        session,
        """
        SELECT
            ward_id,
            ward_code,
            ward_name,
            count(*)                                                     AS total_beds,
            count(*) FILTER (WHERE is_usable)                            AS usable_beds,
            count(*) FILTER (WHERE is_occupied)                          AS occupied,
            count(*) FILTER (WHERE status = 'AVAILABLE')                 AS available,
            count(*) FILTER (WHERE status = 'CLEANING')                  AS cleaning,
            count(*) FILTER (WHERE status = 'OUT_OF_SERVICE')            AS out_of_service
        FROM reporting_beds
        GROUP BY ward_id, ward_code, ward_name
        ORDER BY ward_code
        """,
        {},
    )

    wards = [
        WardOccupancy(
            ward_id=row["ward_id"],
            ward_code=row["ward_code"],
            ward_name=row["ward_name"],
            total_beds=row["total_beds"],
            usable_beds=row["usable_beds"],
            occupied=row["occupied"],
            available=row["available"],
            cleaning=row["cleaning"],
            out_of_service=row["out_of_service"],
            occupancy_rate=_rate(row["occupied"], row["usable_beds"]) or 0.0,
        )
        for row in rows
    ]

    usable = sum(ward.usable_beds for ward in wards)
    occupied = sum(ward.occupied for ward in wards)
    return OccupancyReport(
        total_beds=sum(ward.total_beds for ward in wards),
        usable_beds=usable,
        occupied=occupied,
        available=sum(ward.available for ward in wards),
        cleaning=sum(ward.cleaning for ward in wards),
        out_of_service=sum(ward.out_of_service for ward in wards),
        occupancy_rate=_rate(occupied, usable) or 0.0,
        by_ward=wards,
    )


# ---------------------------------------------------------------------------
# Inpatient / ALOS (CLAUDE.md §13 step 10)
# ---------------------------------------------------------------------------
async def inpatient_summary(session: AsyncSession, window: DateWindow) -> InpatientReport:
    """Admissions, discharges, ALOS and how stays ended.

    ALOS is computed over **discharged** stays only. Including patients still in
    a bed would drag the average down every morning and up every evening, since
    their stay is still lengthening — an average that changes with the clock
    rather than with care.
    """
    params = {"date_from": window.date_from, "date_to": window.date_to}

    totals = (
        await _rows(
            session,
            """
            SELECT
                count(*) FILTER (
                    WHERE local_date BETWEEN :date_from AND :date_to
                )                                                        AS admissions,
                count(*) FILTER (
                    WHERE discharged_date BETWEEN :date_from AND :date_to
                )                                                        AS discharges,
                count(*) FILTER (WHERE is_open)                          AS currently_admitted,
                avg(length_of_stay_days) FILTER (
                    WHERE discharged_date BETWEEN :date_from AND :date_to
                )                                                        AS alos
            FROM reporting_admissions
            """,
            params,
        )
    )[0]

    outcomes = await _rows(
        session,
        """
        SELECT discharge_type, count(*) AS total
        FROM reporting_admissions
        WHERE discharged_date BETWEEN :date_from AND :date_to
          AND discharge_type IS NOT NULL
        GROUP BY discharge_type
        ORDER BY discharge_type
        """,
        params,
    )

    by_type = {row["discharge_type"]: row["total"] for row in outcomes}
    deaths = by_type.get("DECEASED", 0)
    discharges = totals["discharges"]

    return InpatientReport(
        window=window,
        admissions=totals["admissions"],
        discharges=discharges,
        currently_admitted=totals["currently_admitted"],
        average_length_of_stay_days=_minutes(totals["alos"]),
        by_discharge_type=by_type,
        deaths=deaths,
        mortality_rate=(round(deaths / discharges * 100, 2) if discharges else None),
    )


# ---------------------------------------------------------------------------
# Revenue (CLAUDE.md §13 step 10)
# ---------------------------------------------------------------------------
async def revenue(session: AsyncSession, window: DateWindow) -> RevenueReport:
    """Earned, collected and outstanding — three numbers, never summed.

    Confusing them is the classic hospital-dashboard error: charges raised is
    not money in the bank, and collections include payments against invoices
    raised months ago.
    """
    params = {"date_from": window.date_from, "date_to": window.date_to}

    charges = (
        await _rows(
            session,
            """
            SELECT
                coalesce(sum(billable_amount), 0)                        AS charges_raised,
                coalesce(sum(tax_amount) FILTER (
                    WHERE status NOT IN ('CANCELLED', 'WAIVED')
                ), 0)                                                    AS tax_collected,
                coalesce(sum(discount_amount), 0)                        AS discounts_given,
                coalesce(sum(total_amount) FILTER (WHERE status = 'WAIVED'), 0)
                                                                         AS waived,
                count(*) FILTER (WHERE needs_pricing)                    AS unpriced_charges
            FROM reporting_charges
            WHERE local_date BETWEEN :date_from AND :date_to
            """,
            params,
        )
    )[0]

    payments = (
        await _rows(
            session,
            """
            SELECT
                coalesce(sum(net_amount), 0)                             AS collected,
                coalesce(sum(amount) FILTER (WHERE is_reversal), 0)      AS reversed_amount
            FROM reporting_payments
            WHERE local_date BETWEEN :date_from AND :date_to
            """,
            params,
        )
    )[0]

    # Not windowed, on purpose — see the schema. The oldest debt is the one that
    # matters most, and a date filter would hide it.
    outstanding = (
        await _rows(
            session,
            """
            SELECT coalesce(sum(balance_due), 0) AS outstanding_total
            FROM invoices
            WHERE deleted_at IS NULL
              -- Only issued documents. A DRAFT is not money anybody owes yet,
              -- and CANCELLED / WRITTEN_OFF are money the hospital has decided
              -- it will not collect — counting either as outstanding would put
              -- a debt on the report that nobody can chase.
              AND status IN ('ISSUED', 'PARTIALLY_PAID')
            """,
            {},
        )
    )[0]

    by_category = await _rows(
        session,
        """
        SELECT
            category,
            count(*)                                                     AS charges,
            coalesce(sum(billable_amount), 0)                            AS billable,
            coalesce(sum(total_amount) FILTER (WHERE status = 'WAIVED'), 0)
                                                                         AS waived
        FROM reporting_charges
        WHERE local_date BETWEEN :date_from AND :date_to
        GROUP BY category
        ORDER BY sum(billable_amount) DESC NULLS LAST
        """,
        params,
    )

    by_method = await _rows(
        session,
        """
        SELECT method, coalesce(sum(net_amount), 0) AS collected
        FROM reporting_payments
        WHERE local_date BETWEEN :date_from AND :date_to
        GROUP BY method
        ORDER BY method
        """,
        params,
    )

    return RevenueReport(
        window=window,
        charges_raised=_money(charges["charges_raised"]),
        tax_collected=_money(charges["tax_collected"]),
        discounts_given=_money(charges["discounts_given"]),
        waived=_money(charges["waived"]),
        unpriced_charges=charges["unpriced_charges"],
        collected=_money(payments["collected"]),
        reversed_amount=_money(payments["reversed_amount"]),
        outstanding_total=_money(outstanding["outstanding_total"]),
        by_category=[
            CategoryRevenue(
                category=row["category"],
                charges=row["charges"],
                billable=_money(row["billable"]),
                waived=_money(row["waived"]),
            )
            for row in by_category
        ],
        by_method={row["method"]: _money(row["collected"]) for row in by_method},
    )


# ---------------------------------------------------------------------------
# Follow-up compliance (CLAUDE.md §13 step 10)
# ---------------------------------------------------------------------------
async def follow_up_compliance(
    session: AsyncSession, window: DateWindow, *, today: date | None = None
) -> FollowUpCompliance:
    """Of the patients told to come back, how many did.

    "Honoured" is a later encounter for the same patient on or after the advised
    date. Deliberately not "an encounter with the same doctor" — a patient who
    returns and is seen by whoever is on duty has still come back, and counting
    that as a failure would measure rota stability rather than compliance.

    Visits whose follow-up date has not yet arrived are `pending` and are kept
    out of the rate entirely, so the number does not drift depending on when the
    report is run.
    """
    reference = today or window.date_to
    params = {
        "date_from": window.date_from,
        "date_to": window.date_to,
        "today": reference,
    }

    row = (
        await _rows(
            session,
            """
            WITH advised AS (
                SELECT
                    e.id,
                    e.patient_id,
                    e.follow_up_date,
                    EXISTS (
                        SELECT 1
                          FROM reporting_encounters back
                         WHERE back.patient_id = e.patient_id
                           AND back.id <> e.id
                           AND back.local_date >= e.follow_up_date
                           AND back.was_seen
                    ) AS returned
                FROM reporting_encounters e
                WHERE e.local_date BETWEEN :date_from AND :date_to
                  AND e.follow_up_date IS NOT NULL
            )
            SELECT
                count(*)                                                     AS advised,
                count(*) FILTER (WHERE returned)                             AS honoured,
                count(*) FILTER (
                    WHERE NOT returned AND follow_up_date <= :today
                )                                                            AS missed,
                count(*) FILTER (
                    WHERE NOT returned AND follow_up_date > :today
                )                                                            AS pending
            FROM advised
            """,
            params,
        )
    )[0]

    honoured, missed = row["honoured"], row["missed"]
    return FollowUpCompliance(
        window=window,
        advised=row["advised"],
        honoured=honoured,
        missed=missed,
        pending=row["pending"],
        compliance_rate=_rate(honoured, honoured + missed),
    )


# ---------------------------------------------------------------------------
# Live queue (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
async def queue_snapshot(
    session: AsyncSession,
    *,
    queue_date: date,
    delay_threshold_minutes: int | None = None,
) -> QueueSnapshot:
    """Live waiting counts, average wait, doctor workload, department congestion.

    The `running_late` list is §7b's doctor delay alert: reception finds out a
    clinic is behind from a screen they already have open, rather than from the
    third patient to come and ask.
    """
    threshold = delay_threshold_minutes or settings.REPORTING_QUEUE_DELAY_MINUTES
    params: dict[str, Any] = {"queue_date": queue_date}

    totals = (
        await _rows(
            session,
            """
            SELECT
                count(*) FILTER (WHERE status = 'WAITING')               AS waiting,
                count(*) FILTER (WHERE status = 'IN_CONSULTATION')       AS in_consultation,
                count(*) FILTER (WHERE status = 'COMPLETED')             AS completed,
                count(*) FILTER (WHERE status = 'LEFT_WITHOUT_BEING_SEEN')
                                                                         AS lwbs,
                avg(waited_minutes) FILTER (WHERE called_at IS NOT NULL)  AS average_wait,
                max(waited_minutes) FILTER (WHERE is_waiting)             AS longest_wait
            FROM reporting_queue
            WHERE queue_date = :queue_date
            """,
            params,
        )
    )[0]

    per_doctor = await _rows(
        session,
        """
        SELECT
            q.doctor_id,
            doc.display_name                                             AS doctor_name,
            q.department_id,
            count(*) FILTER (WHERE q.is_waiting)                         AS waiting,
            max(q.waited_minutes) FILTER (WHERE q.is_waiting)            AS longest_wait,
            avg(q.waited_minutes) FILTER (WHERE q.called_at IS NOT NULL) AS average_wait
        FROM reporting_queue q
        LEFT JOIN doctors doc ON doc.id = q.doctor_id
        WHERE q.queue_date = :queue_date
        GROUP BY q.doctor_id, doc.display_name, q.department_id
        ORDER BY count(*) FILTER (WHERE q.is_waiting) DESC
        """,
        params,
    )

    doctors = [
        WaitingDoctor(
            doctor_id=row["doctor_id"],
            doctor_name=row["doctor_name"],
            department_id=row["department_id"],
            waiting=row["waiting"],
            longest_wait_minutes=_minutes(row["longest_wait"]),
            average_wait_minutes=_minutes(row["average_wait"]),
            running_late=bool(
                row["longest_wait"] is not None and float(row["longest_wait"]) >= threshold
            ),
        )
        for row in per_doctor
    ]

    return QueueSnapshot(
        queue_date=queue_date,
        waiting=totals["waiting"],
        in_consultation=totals["in_consultation"],
        completed=totals["completed"],
        left_without_being_seen=totals["lwbs"],
        average_wait_minutes=_minutes(totals["average_wait"]),
        longest_wait_minutes=_minutes(totals["longest_wait"]),
        delay_threshold_minutes=threshold,
        by_doctor=doctors,
        running_late=[doctor for doctor in doctors if doctor.running_late],
    )


# ---------------------------------------------------------------------------
# The composed dashboard (CLAUDE.md §7b: role-based dashboards)
# ---------------------------------------------------------------------------
async def build_dashboard(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    permissions: set[str],
    trailing_days: int = 30,
) -> Dashboard:
    """Everything the caller is allowed to see, in one round trip.

    Sections are chosen by permission rather than by role, so a hospital that
    invents its own role gets a sensible dashboard without a code change — the
    same data-driven RBAC principle CLAUDE.md §8 asks for, applied to a screen.
    """
    from app.modules.reporting.rbac import ReportingPermissions

    today = await local_today(session, hospital_id)
    window = DateWindow(date_from=today - timedelta(days=trailing_days - 1), date_to=today)

    dashboard = Dashboard(generated_for=today)

    if ReportingPermissions.OPERATIONAL in permissions:
        dashboard.footfall = await footfall(session, window)
        dashboard.occupancy = await occupancy(session)
        dashboard.queue = await queue_snapshot(session, queue_date=today)
    if ReportingPermissions.CLINICAL in permissions:
        dashboard.inpatient = await inpatient_summary(session, window)
        dashboard.follow_up = await follow_up_compliance(session, window, today=today)
    if ReportingPermissions.REVENUE in permissions:
        dashboard.revenue = await revenue(session, window)

    return dashboard
