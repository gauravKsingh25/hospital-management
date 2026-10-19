"""The accountant's read side: dashboard totals, lists, bill history, cost
journey and the printable bill.

Read only, and kept out of `service.py` because it answers a different
question. `service.py` moves money; this reports it. Every list is newest
first and paginated, every lookup across a module boundary goes through that
module's service in bulk — one query for a page's patients, one for its stays
— so a page costs the same whether it shows five rows or fifty.

Money here is always the server's figure. The frontend displays it and never
adds it up.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, or_
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.exceptions import NotFoundError
from app.core.models import utc_now
from app.core.pagination import MAX_PAGE_SIZE, Page, PageParams
from app.modules.billing.models import (
    Charge,
    ChargeCategory,
    ChargeStatus,
    Invoice,
    InvoiceScope,
    InvoiceStatus,
    Payment,
)
from app.modules.billing.schemas import (
    AccountsSummary,
    BillHistory,
    BillRow,
    ChargeRead,
    InvoiceSummary,
    Journey,
    JourneyLine,
    PatientBrief,
    PatientRow,
    PaymentRead,
    PaymentRow,
    PrintableBill,
    PrintableTotals,
    StayRead,
    VisitBills,
)
from app.modules.billing.stays import StayInfo, stays_for
from app.modules.billing.tax import ZERO, LineTax, money, sum_lines
from app.modules.clinical import service as clinical_service
from app.modules.identity import service as identity_service
from app.modules.patients import service as patients_service
from app.modules.patients.schemas import PatientRead
from app.modules.scheduling import service as scheduling_service
from app.modules.tenancy import service as tenancy_service

__all__ = [
    "bill_history",
    "cost_journey",
    "list_bills",
    "list_patients",
    "list_payments_received",
    "printable_invoice",
    "printable_running_bill",
    "summary",
]

_OPEN: tuple[InvoiceStatus, ...] = (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)
_NOT_A_BILL: tuple[InvoiceStatus, ...] = (InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED)
_INTERIM: Literal["INTERIM — NOT A FINAL BILL"] = "INTERIM — NOT A FINAL BILL"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
async def period(
    session: AsyncSession, hospital_id: uuid.UUID, start: date | None, end: date | None
) -> tuple[datetime, datetime]:
    """[from, to] as the hospital's own calendar days, defaulting to today."""
    zone = await tenancy_service.local_zone(session, hospital_id)
    today = utc_now().astimezone(zone).date()
    first = start or today
    last = end or first
    return (
        datetime.combine(first, time.min, tzinfo=zone),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone),
    )


async def _patient_ids_matching(
    session: AsyncSession, query: str, *, hospital_id: uuid.UUID
) -> list[uuid.UUID]:
    patients, _ = await patients_service.search_patients(
        session, query, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id
    )
    return [patient.id for patient in patients]


async def _names(
    session: AsyncSession, patient_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, PatientRead]:
    patients = await patients_service.get_patients_by_ids(
        session, set(patient_ids), hospital_id=hospital_id
    )
    return {pid: PatientRead.model_validate(patient) for pid, patient in patients.items()}


def _stay_days(stay: StayInfo) -> int:
    leaving = stay.discharged_at or utc_now()
    return max(1, (leaving.date() - stay.admitted_at.date()).days)


def _brief(patient: PatientRead) -> PatientBrief:
    return PatientBrief(
        id=patient.id,
        name=patient.full_name,
        uhid=patient.uhid,
        age_years=patient.age_years,
        gender=str(patient.gender),
        phone=patient.phone,
    )


def _footer(charges: list[Charge], *, paid: Decimal = ZERO) -> PrintableTotals:
    """The same footer arithmetic an invoice uses, so a printed running bill
    and the final bill it becomes add up the same way."""
    totals = sum_lines(
        [
            (
                money(charge.unit_price * charge.quantity),
                charge.discount_amount,
                LineTax(
                    taxable_amount=charge.taxable_amount,
                    gst_rate=charge.gst_rate,
                    cgst_amount=charge.cgst_amount,
                    sgst_amount=charge.sgst_amount,
                    igst_amount=charge.igst_amount,
                    total_amount=charge.total_amount,
                ),
            )
            for charge in charges
            if charge.status is not ChargeStatus.WAIVED
        ]
    )
    return PrintableTotals(
        subtotal=totals.subtotal,
        discount_total=totals.discount_total,
        taxable_total=totals.taxable_total,
        cgst_total=totals.cgst_total,
        sgst_total=totals.sgst_total,
        igst_total=totals.igst_total,
        round_off=totals.round_off,
        grand_total=totals.grand_total,
        paid=paid,
        balance=money(totals.grand_total - paid),
    )


async def _unbilled_by_encounter(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_ids: Collection[uuid.UUID] | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID, datetime, Decimal]]:
    """Charges on nobody's issued bill yet: pending, or on a draft.

    Returns (encounter, patient, captured_at, total) per charge.
    """
    filters: list[ColumnElement[bool]] = [
        col(Charge.hospital_id) == hospital_id,
        col(Charge.deleted_at).is_(None),
        or_(
            col(Charge.status) == ChargeStatus.PENDING,
            (col(Charge.status) == ChargeStatus.INVOICED)
            & (col(Invoice.status) == InvoiceStatus.DRAFT),
        ),
    ]
    if patient_ids is not None:
        filters.append(col(Charge.patient_id).in_(set(patient_ids)))
    rows = (
        await session.execute(
            sa_select(
                col(Charge.encounter_id),
                col(Charge.patient_id),
                col(Charge.captured_at),
                col(Charge.total_amount),
            )
            .select_from(Charge)
            .outerjoin(Invoice, col(Invoice.id) == col(Charge.invoice_id))
            .where(*filters)
        )
    ).all()
    return [(row[0], row[1], row[2], Decimal(row[3])) for row in rows]


# ---------------------------------------------------------------------------
# The totals strip
# ---------------------------------------------------------------------------
async def summary(
    session: AsyncSession, *, hospital_id: uuid.UUID, start: date | None, end: date | None
) -> AccountsSummary:
    since, until = await period(session, hospital_id, start, end)

    # Receipts in the period, gross: a reversal is its own row and is reported
    # as such, so the drawer can be reconciled both ways.
    by_method = (
        await session.execute(
            sa_select(col(Payment.method), func.sum(col(Payment.amount)))
            .where(
                col(Payment.hospital_id) == hospital_id,
                col(Payment.deleted_at).is_(None),
                col(Payment.reverses_payment_id).is_(None),
                col(Payment.received_at) >= since,
                col(Payment.received_at) < until,
            )
            .group_by(col(Payment.method))
        )
    ).all()
    collected_by_method = {method.value: money(Decimal(total)) for method, total in by_method}
    reversals = (
        await session.execute(
            sa_select(func.coalesce(func.sum(col(Payment.amount)), 0)).where(
                col(Payment.hospital_id) == hospital_id,
                col(Payment.deleted_at).is_(None),
                col(Payment.reverses_payment_id).is_not(None),
                col(Payment.received_at) >= since,
                col(Payment.received_at) < until,
            )
        )
    ).scalar_one()

    bills_count, billed = (
        await session.execute(
            sa_select(func.count(), func.coalesce(func.sum(col(Invoice.grand_total)), 0)).where(
                col(Invoice.hospital_id) == hospital_id,
                col(Invoice.deleted_at).is_(None),
                col(Invoice.status).not_in(_NOT_A_BILL),
                col(Invoice.issued_at) >= since,
                col(Invoice.issued_at) < until,
            )
        )
    ).one()

    outstanding, outstanding_patients = (
        await session.execute(
            sa_select(
                func.coalesce(func.sum(col(Invoice.balance_due)), 0),
                func.count(func.distinct(col(Invoice.patient_id))),
            ).where(
                col(Invoice.hospital_id) == hospital_id,
                col(Invoice.deleted_at).is_(None),
                col(Invoice.status).in_(_OPEN),
                col(Invoice.balance_due) > 0,
            )
        )
    ).one()

    # Running in wards: unbilled charges on stays, from the admission on.
    unbilled = await _unbilled_by_encounter(session, hospital_id=hospital_id)
    stays = await stays_for(session, {row[0] for row in unbilled}, hospital_id=hospital_id)
    running = ZERO
    running_patients: set[uuid.UUID] = set()
    for encounter_id, patient_id, captured_at, total in unbilled:
        stay = stays.get(encounter_id)
        if stay is not None and captured_at >= stay.admitted_at:
            running += total
            running_patients.add(patient_id)

    return AccountsSummary(
        collected=money(sum(collected_by_method.values(), start=ZERO)),
        collected_by_method=collected_by_method,
        bills_count=int(bills_count),
        billed=money(Decimal(billed)),
        outstanding=money(Decimal(outstanding)),
        outstanding_patients=int(outstanding_patients),
        running_in_wards=money(running),
        running_patients=len(running_patients),
        reversals=money(Decimal(reversals)),
    )


# ---------------------------------------------------------------------------
# Bills and receipts
# ---------------------------------------------------------------------------
async def list_bills(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    query: str | None = None,
    outstanding_only: bool = False,
) -> Page[BillRow]:
    """Every bill issued, newest first. `outstanding_only` ignores the period:
    a bill unpaid since last month is still owed today."""
    filters: list[ColumnElement[bool]] = [
        col(Invoice.hospital_id) == hospital_id,
        col(Invoice.deleted_at).is_(None),
        col(Invoice.status) != InvoiceStatus.DRAFT,
    ]
    if outstanding_only:
        filters += [col(Invoice.status).in_(_OPEN), col(Invoice.balance_due) > 0]
    elif start or end:
        since, until = await period(session, hospital_id, start, end)
        filters += [col(Invoice.issued_at) >= since, col(Invoice.issued_at) < until]
    if query:
        patient_ids = await _patient_ids_matching(session, query, hospital_id=hospital_id)
        filters.append(
            or_(
                col(Invoice.invoice_number).ilike(f"%{query.strip()}%"),
                col(Invoice.patient_id).in_(patient_ids),
            )
        )

    total = (
        await session.execute(select(func.count()).select_from(Invoice).where(*filters))
    ).scalar_one()
    invoices = (
        (
            await session.execute(
                select(Invoice)
                .where(*filters)
                .order_by(
                    col(Invoice.issued_at).desc().nulls_last(), col(Invoice.created_at).desc()
                )
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    names = await _names(session, {row.patient_id for row in invoices}, hospital_id=hospital_id)
    stays = await stays_for(
        session,
        {row.encounter_id for row in invoices if row.scope is InvoiceScope.IPD},
        hospital_id=hospital_id,
    )

    rows: list[BillRow] = []
    for invoice in invoices:
        patient = names.get(invoice.patient_id)
        stay = stays.get(invoice.encounter_id) if invoice.scope is InvoiceScope.IPD else None
        rows.append(
            BillRow(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                issued_at=invoice.issued_at,
                created_at=invoice.created_at,
                status=invoice.status,
                scope=invoice.scope,
                encounter_id=invoice.encounter_id,
                patient_id=invoice.patient_id,
                patient_name=patient.full_name if patient else None,
                uhid=patient.uhid if patient else None,
                admission_number=stay.admission_number if stay else None,
                stay_days=_stay_days(stay) if stay else None,
                grand_total=invoice.grand_total,
                amount_paid=invoice.amount_paid,
                balance_due=invoice.balance_due,
            )
        )
    return Page.build(rows, total=total, params=params)


async def list_payments_received(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    query: str | None = None,
) -> Page[PaymentRow]:
    filters: list[ColumnElement[bool]] = [
        col(Payment.hospital_id) == hospital_id,
        col(Payment.deleted_at).is_(None),
    ]
    if start or end:
        since, until = await period(session, hospital_id, start, end)
        filters += [col(Payment.received_at) >= since, col(Payment.received_at) < until]
    if query:
        patient_ids = await _patient_ids_matching(session, query, hospital_id=hospital_id)
        filters.append(
            or_(
                col(Payment.receipt_number).ilike(f"%{query.strip()}%"),
                col(Payment.reference).ilike(f"%{query.strip()}%"),
                col(Payment.patient_id).in_(patient_ids),
            )
        )

    total = (
        await session.execute(select(func.count()).select_from(Payment).where(*filters))
    ).scalar_one()
    payments = (
        (
            await session.execute(
                select(Payment)
                .where(*filters)
                .order_by(col(Payment.received_at).desc(), col(Payment.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    names = await _names(session, {row.patient_id for row in payments}, hospital_id=hospital_id)
    numbers: dict[uuid.UUID, str] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                sa_select(col(Invoice.id), col(Invoice.invoice_number)).where(
                    col(Invoice.id).in_({row.invoice_id for row in payments})
                )
            )
        ).all()
    }
    staff = await identity_service.get_user_names(
        session, {row.received_by_id for row in payments if row.received_by_id}
    )

    rows: list[PaymentRow] = []
    for payment in payments:
        patient = names.get(payment.patient_id)
        rows.append(
            PaymentRow(
                id=payment.id,
                receipt_number=payment.receipt_number,
                invoice_id=payment.invoice_id,
                invoice_number=numbers.get(payment.invoice_id),
                patient_id=payment.patient_id,
                patient_name=patient.full_name if patient else None,
                uhid=patient.uhid if patient else None,
                amount=payment.amount,
                method=payment.method,
                reference=payment.reference,
                status=payment.status,
                received_at=payment.received_at,
                received_by_name=(
                    staff.get(payment.received_by_id) if payment.received_by_id else None
                ),
                reverses_payment_id=payment.reverses_payment_id,
            )
        )
    return Page.build(rows, total=total, params=params)


# ---------------------------------------------------------------------------
# Patients, latest activity first
# ---------------------------------------------------------------------------
async def list_patients(
    session: AsyncSession, params: PageParams, *, hospital_id: uuid.UUID, query: str | None
) -> Page[PatientRow]:
    filters: list[ColumnElement[bool]] = [
        col(Charge.hospital_id) == hospital_id,
        col(Charge.deleted_at).is_(None),
        col(Charge.status) != ChargeStatus.CANCELLED,
    ]
    if query:
        filters.append(
            col(Charge.patient_id).in_(
                await _patient_ids_matching(session, query, hospital_id=hospital_id)
            )
        )
    total = (
        await session.execute(
            sa_select(func.count(func.distinct(col(Charge.patient_id)))).where(*filters)
        )
    ).scalar_one()
    activity = (
        await session.execute(
            sa_select(
                col(Charge.patient_id),
                func.max(col(Charge.captured_at)).label("last"),
                func.count(func.distinct(col(Charge.encounter_id))),
            )
            .where(*filters)
            .group_by(col(Charge.patient_id))
            .order_by(func.max(col(Charge.captured_at)).desc())
            .limit(params.limit)
            .offset(params.offset)
        )
    ).all()
    patient_ids = [row[0] for row in activity]
    if not patient_ids:
        return Page.build([], total=total, params=params)

    issued = {
        row[0]: (Decimal(row[1]), Decimal(row[2] or 0), Decimal(row[3] or 0))
        for row in (
            await session.execute(
                sa_select(
                    col(Invoice.patient_id),
                    func.sum(col(Invoice.grand_total)),
                    func.sum(col(Invoice.amount_paid)),
                    func.sum(col(Invoice.balance_due)).filter(col(Invoice.status).in_(_OPEN)),
                )
                .where(
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.deleted_at).is_(None),
                    col(Invoice.status).not_in(_NOT_A_BILL),
                    col(Invoice.patient_id).in_(patient_ids),
                )
                .group_by(col(Invoice.patient_id))
            )
        ).all()
        if row[1] is not None
    }
    unbilled: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for _, patient_id, _, amount in await _unbilled_by_encounter(
        session, hospital_id=hospital_id, patient_ids=patient_ids
    ):
        unbilled[patient_id] += amount
    names = await _names(session, patient_ids, hospital_id=hospital_id)

    rows: list[PatientRow] = []
    for patient_id, last, visits in activity:
        grand, paid, open_balance = issued.get(patient_id, (ZERO, ZERO, ZERO))
        extra = unbilled.get(patient_id, ZERO)
        patient = names.get(patient_id)
        rows.append(
            PatientRow(
                patient_id=patient_id,
                name=patient.full_name if patient else None,
                uhid=patient.uhid if patient else None,
                last_activity_at=last,
                visits=int(visits),
                billed=money(grand + extra),
                paid=money(paid),
                due=money(open_balance + extra),
            )
        )
    return Page.build(rows, total=total, params=params)


# ---------------------------------------------------------------------------
# One patient: bill history and cost journey
# ---------------------------------------------------------------------------
def _is_ipd(stay: StayInfo | None, captured_at: datetime) -> bool:
    return stay is not None and captured_at >= stay.admitted_at


async def bill_history(
    session: AsyncSession, patient_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> BillHistory:
    """Every bill-bearing visit, newest first. A stay admitted from a still-open
    OPD visit shows as two: the consultation and the stay."""
    names = await _names(session, [patient_id], hospital_id=hospital_id)
    patient = names.get(patient_id)
    if patient is None:
        raise NotFoundError("Patient not found.", code="patient_not_found")

    invoices = (
        (
            await session.execute(
                select(Invoice)
                .where(
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.patient_id) == patient_id,
                    col(Invoice.deleted_at).is_(None),
                    col(Invoice.status) != InvoiceStatus.DRAFT,
                )
                .order_by(col(Invoice.created_at).desc())
            )
        )
        .scalars()
        .all()
    )
    unbilled = await _unbilled_by_encounter(
        session, hospital_id=hospital_id, patient_ids=[patient_id]
    )
    # Every visit of the patient, so a stay admitted a minute ago — nothing
    # charged on it yet — is still listed: it is a bill the accountant will make.
    visits_page, _ = await clinical_service.list_encounters(
        session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id, patient_id=patient_id
    )
    encounters = {encounter.id: encounter for encounter in visits_page}
    encounter_ids = (
        {row.encounter_id for row in invoices} | {row[0] for row in unbilled} | set(encounters)
    )
    missing = encounter_ids - set(encounters)
    if missing:
        encounters.update(
            await clinical_service.get_encounters_by_ids(session, missing, hospital_id=hospital_id)
        )
    stays = await stays_for(session, encounter_ids, hospital_id=hospital_id)
    doctors = await scheduling_service.get_doctors_by_ids(
        session,
        {e.doctor_id for e in encounters.values() if e.doctor_id},
        hospital_id=hospital_id,
    )

    # (encounter, kind) -> invoices, unbilled amount
    grouped: dict[tuple[uuid.UUID, str], list[Invoice]] = defaultdict(list)
    pending: dict[tuple[uuid.UUID, str], Decimal] = defaultdict(lambda: ZERO)
    for invoice in invoices:
        grouped[(invoice.encounter_id, invoice.scope.value)].append(invoice)
    for encounter_id, _, captured_at, amount in unbilled:
        kind = "IPD" if _is_ipd(stays.get(encounter_id), captured_at) else "OPD"
        pending[(encounter_id, kind)] += amount
        grouped.setdefault((encounter_id, kind), [])
    for encounter_id in stays:
        grouped.setdefault((encounter_id, "IPD"), [])

    visits: list[VisitBills] = []
    for (encounter_id, kind), bills in grouped.items():
        encounter = encounters.get(encounter_id)
        if encounter is None:
            continue
        stay = stays.get(encounter_id)
        live = [bill for bill in bills if bill.status is not InvoiceStatus.CANCELLED]
        extra = pending[(encounter_id, kind)]
        total = money(sum((bill.grand_total for bill in live), start=ZERO) + extra)
        paid = money(sum((bill.amount_paid for bill in live), start=ZERO))
        due = money(
            sum((bill.balance_due for bill in live if bill.status in _OPEN), start=ZERO) + extra
        )
        doctor = doctors.get(encounter.doctor_id) if encounter.doctor_id else None

        if kind == "IPD" and stay is not None:
            if live:  # an issued IPD invoice is the final bill
                label = "Paid" if due == ZERO else "Final"
            elif encounter.is_closed or stay.status == "DISCHARGED":
                label = "Awaiting final bill"
            else:
                label = "Running"
            visits.append(
                VisitBills(
                    encounter_id=encounter_id,
                    kind="IPD",
                    started_at=stay.admitted_at,
                    ended_at=stay.discharged_at,
                    doctor_name=stay.attending_doctor or (doctor.display_name if doctor else None),
                    admission_number=stay.admission_number,
                    ward_bed=stay.ward_bed,
                    status_label=label,
                    total=total,
                    paid=paid,
                    due=due,
                    invoices=[InvoiceSummary.model_validate(bill) for bill in bills],
                )
            )
        else:
            label = "No charge" if total == ZERO else ("Paid" if due == ZERO else "Due")
            visits.append(
                VisitBills(
                    encounter_id=encounter_id,
                    kind="OPD",
                    started_at=encounter.started_at,
                    ended_at=stay.admitted_at if stay else encounter.closed_at,
                    doctor_name=doctor.display_name if doctor else None,
                    status_label=label,
                    total=total,
                    paid=paid,
                    due=due,
                    invoices=[InvoiceSummary.model_validate(bill) for bill in bills],
                )
            )

    visits.sort(key=lambda visit: visit.started_at, reverse=True)
    return BillHistory(
        patient=_brief(patient),
        lifetime_billed=money(sum((v.total for v in visits), start=ZERO)),
        lifetime_paid=money(sum((v.paid for v in visits), start=ZERO)),
        lifetime_due=money(sum((v.due for v in visits), start=ZERO)),
        visits=visits,
    )


async def cost_journey(
    session: AsyncSession,
    patient_id: uuid.UUID,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    category: ChargeCategory | None = None,
    encounter_id: uuid.UUID | None = None,
) -> Journey:
    """Everything charged to the patient across every visit, latest first —
    medicines, consumables, tests, room-days — with totals per category for
    the same filter. Read only; edits happen in the IPD bill editor."""
    filters: list[ColumnElement[bool]] = [
        col(Charge.hospital_id) == hospital_id,
        col(Charge.patient_id) == patient_id,
        col(Charge.deleted_at).is_(None),
        col(Charge.status) != ChargeStatus.CANCELLED,
    ]
    if category is not None:
        filters.append(col(Charge.category) == category)
    if encounter_id is not None:
        filters.append(col(Charge.encounter_id) == encounter_id)

    total = (
        await session.execute(select(func.count()).select_from(Charge).where(*filters))
    ).scalar_one()
    charges = (
        (
            await session.execute(
                select(Charge)
                .where(*filters)
                .order_by(col(Charge.captured_at).desc(), col(Charge.id).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    totals = (
        await session.execute(
            sa_select(col(Charge.category), func.sum(col(Charge.total_amount)))
            .where(*filters, col(Charge.status) != ChargeStatus.WAIVED)
            .group_by(col(Charge.category))
        )
    ).all()
    stays = await stays_for(session, {row.encounter_id for row in charges}, hospital_id=hospital_id)

    lines = [
        JourneyLine(
            charge_id=charge.id,
            captured_at=charge.captured_at,
            encounter_id=charge.encounter_id,
            kind="IPD" if _is_ipd(stays.get(charge.encounter_id), charge.captured_at) else "OPD",
            category=charge.category,
            description=charge.description,
            quantity=charge.quantity,
            unit_price=charge.unit_price,
            total_amount=charge.total_amount,
            status=charge.status,
            source_module=charge.source_module,
        )
        for charge in charges
    ]
    return Journey(
        lines=Page.build(lines, total=total, params=params),
        totals_by_category={cat.value: money(Decimal(amount)) for cat, amount in totals},
    )


# ---------------------------------------------------------------------------
# The printed bill
# ---------------------------------------------------------------------------
def _title(
    charges: list[Charge], *, interim: bool
) -> Literal["TAX INVOICE", "BILL OF SUPPLY", "INTERIM — NOT A FINAL BILL"]:
    """Usual GST practice — the hospital's CA should confirm: a tax invoice when
    any line carries GST, otherwise a bill of supply."""
    if interim:
        return _INTERIM
    taxed = any(c.gst_rate > 0 for c in charges if c.status is not ChargeStatus.WAIVED)
    return "TAX INVOICE" if taxed else "BILL OF SUPPLY"


async def _patient_brief(
    session: AsyncSession, patient_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> PatientBrief:
    names = await _names(session, [patient_id], hospital_id=hospital_id)
    patient = names.get(patient_id)
    if patient is None:
        raise NotFoundError("Patient not found.", code="patient_not_found")
    return _brief(patient)


async def printable_invoice(
    session: AsyncSession,
    invoice: Invoice,
    *,
    charges: list[Charge],
    payments: list[Payment],
    hospital_id: uuid.UUID,
) -> PrintableBill:
    """The issued bill exactly as it was issued: its own lines and totals, and
    the letterhead snapshotted that day. A draft prints as interim, with the
    hospital's details as they are now."""
    interim = invoice.status is InvoiceStatus.DRAFT
    stay = None
    if invoice.scope is InvoiceScope.IPD:
        stay = (await stays_for(session, [invoice.encounter_id], hospital_id=hospital_id)).get(
            invoice.encounter_id
        )
    letterhead: dict[str, Any] = invoice.letterhead or await tenancy_service.letterhead_for(
        session, hospital_id
    )
    return PrintableBill(
        invoice_id=invoice.id,
        number=None if interim else invoice.invoice_number,
        date=invoice.issued_at or utc_now(),
        title=_title(charges, interim=interim),
        scope=invoice.scope,
        status=invoice.status,
        place_of_supply=invoice.place_of_supply,
        letterhead=letterhead,
        patient=await _patient_brief(session, invoice.patient_id, hospital_id=hospital_id),
        stay=StayRead.model_validate(stay) if stay else None,
        lines=[ChargeRead.model_validate(charge) for charge in charges],
        totals=PrintableTotals(
            subtotal=invoice.subtotal,
            discount_total=invoice.discount_total,
            taxable_total=invoice.taxable_total,
            cgst_total=invoice.cgst_total,
            sgst_total=invoice.sgst_total,
            igst_total=invoice.igst_total,
            round_off=invoice.round_off,
            grand_total=invoice.grand_total,
            paid=invoice.amount_paid,
            balance=invoice.balance_due,
        ),
        payments=[PaymentRead.model_validate(payment) for payment in payments],
    )


async def printable_running_bill(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    stay: StayInfo,
    charges: list[Charge],
    hospital_id: uuid.UUID,
) -> PrintableBill:
    """A stay's bill as last saved, marked interim: not a document, no number."""
    hospital = await tenancy_service.get_hospital(session, hospital_id)
    live = [c for c in charges if c.status in (ChargeStatus.PENDING, ChargeStatus.INVOICED)]
    shown = [c for c in charges if c.status is not ChargeStatus.CANCELLED]
    return PrintableBill(
        invoice_id=None,
        number=None,
        date=utc_now(),
        title=_INTERIM,
        scope=InvoiceScope.IPD,
        status=None,
        place_of_supply=hospital.state,
        letterhead=await tenancy_service.letterhead_for(session, hospital_id),
        patient=await _patient_brief(session, patient_id, hospital_id=hospital_id),
        stay=StayRead.model_validate(stay),
        lines=[ChargeRead.model_validate(charge) for charge in shown],
        totals=_footer(live),
        payments=[],
    )
