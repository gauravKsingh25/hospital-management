"""Diagnostics business logic — the module's public interface.

The chain this module completes is the one CLAUDE.md §6 is really about:

    doctor orders  ->  lab accessions  ->  sample collected and received
                   ->  results entered and flagged
                   ->  report verified  ->  clinical order cleared
                   ->  and if it was the last pending item, the visit closes.

That last hop is a **direct call** into `clinical.service`, not an event. The
bus is fire-and-forget by design, and a swallowed handler there would leave a
visit permanently open with its result already filed and nobody told. Events
carry the things whose failure is survivable — the report-ready message, the
billing capture.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.events import event_bus
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Order, OrderStatus, OrderType
from app.modules.diagnostics.events import (
    CriticalResultFlagged,
    ReportAmended,
    ReportReady,
    SpecimenCollected,
    SpecimenRejected,
)
from app.modules.diagnostics.models import (
    AccessionSequence,
    DiagnosticDiscipline,
    DiagnosticReport,
    ReferenceRange,
    ReportStatus,
    ResultFlag,
    ResultValue,
    Specimen,
    SpecimenStatus,
    TestAnalyte,
    TestCatalogueItem,
)
from app.modules.diagnostics.rbac import VERIFICATION_ROLES
from app.modules.diagnostics.schemas import (
    AccessionRequest,
    AmendRequest,
    AnalyteCreate,
    CatalogueItemCreate,
    CatalogueItemUpdate,
    CollectRequest,
    CriticalCallback,
    NarrativeEntry,
    ReferenceRangeCreate,
    ResultsSubmission,
    WorklistStage,
)
from app.modules.diagnostics.transitions import (
    TERMINAL_SPECIMEN_STATUSES,
    assert_report_transition,
    assert_specimen_transition,
)
from app.modules.identity.models import User
from app.modules.patients import service as patients_service

logger = logging.getLogger(__name__)

__all__ = [
    "accession_order",
    "acknowledge_critical",
    "add_analyte",
    "amend_report",
    "assert_may_verify",
    "cancel_report",
    "cancel_report_for_order",
    "collect_specimen",
    "create_catalogue_item",
    "enter_narrative",
    "enter_results",
    "get_catalogue_item",
    "get_catalogue_item_by_code",
    "get_report",
    "get_reports_by_orders",
    "get_specimen",
    "get_specimens_by_orders",
    "list_reports",
    "list_specimens",
    "list_worklist_orders",
    "receive_specimen",
    "reject_specimen",
    "release_preliminary",
    "verify_report",
    "worklist_stage",
]

# Report statuses whose results may still be edited. A verified report is not
# among them: it is superseded, never rewritten.
_EDITABLE = (ReportStatus.REGISTERED, ReportStatus.IN_PROGRESS, ReportStatus.PRELIMINARY)

_ORDER_TYPE_FOR: dict[DiagnosticDiscipline, OrderType] = {
    DiagnosticDiscipline.LAB: OrderType.LAB,
    DiagnosticDiscipline.RADIOLOGY: OrderType.RADIOLOGY,
}


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
async def _next_number(session: AsyncSession, hospital_id: uuid.UUID, kind: str) -> str:
    """`ACC-YY-NNNNNN` / `RPT-YY-NNNNNN`, unique per hospital per year.

    Upsert-then-lock, the same shape as every other counter in the system: a
    plain check-then-insert races on the first allocation of the year, which is
    exactly when the lab opens.
    """
    year = utc_now().year
    await session.execute(
        text(
            """
            INSERT INTO accession_sequences
                (id, hospital_id, year, kind, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :year, :kind, 0, now(), now())
            ON CONFLICT (hospital_id, year, kind) DO NOTHING
            """
        ),
        {"id": new_id(), "hospital_id": hospital_id, "year": year, "kind": kind},
    )
    row = (
        (
            await session.execute(
                select(AccessionSequence)
                .where(
                    col(AccessionSequence.hospital_id) == hospital_id,
                    col(AccessionSequence.year) == year,
                    col(AccessionSequence.kind) == kind,
                )
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )
    row.last_value += 1
    session.add(row)
    await session.flush()
    return f"{kind}-{year % 100:02d}-{row.last_value:06d}"


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
async def create_catalogue_item(
    session: AsyncSession, payload: CatalogueItemCreate, *, hospital_id: uuid.UUID
) -> TestCatalogueItem:
    code = payload.code.strip().upper()
    existing = await get_catalogue_item_by_code(session, code, hospital_id=hospital_id)
    if existing is not None:
        raise ConflictError(
            f"A test with code '{code}' already exists.", code="catalogue_code_taken"
        )

    item = TestCatalogueItem(hospital_id=hospital_id, **{**payload.model_dump(), "code": code})
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def get_catalogue_item(
    session: AsyncSession, item_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> TestCatalogueItem:
    item = (
        (
            await session.execute(
                select(TestCatalogueItem).where(
                    col(TestCatalogueItem.id) == item_id,
                    col(TestCatalogueItem.hospital_id) == hospital_id,
                    col(TestCatalogueItem.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if item is None:
        raise NotFoundError("Test not found in the catalogue.", code="catalogue_item_not_found")
    return item


async def get_catalogue_item_by_code(
    session: AsyncSession, code: str, *, hospital_id: uuid.UUID
) -> TestCatalogueItem | None:
    return (
        (
            await session.execute(
                select(TestCatalogueItem).where(
                    col(TestCatalogueItem.hospital_id) == hospital_id,
                    col(TestCatalogueItem.code) == code.strip().upper(),
                    col(TestCatalogueItem.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_catalogue_items(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    discipline: DiagnosticDiscipline | None = None,
    search: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[TestCatalogueItem], int]:
    filters: list[ColumnElement[bool]] = [
        col(TestCatalogueItem.hospital_id) == hospital_id,
        col(TestCatalogueItem.deleted_at).is_(None),
    ]
    if discipline is not None:
        filters.append(col(TestCatalogueItem.discipline) == discipline)
    if not include_inactive:
        filters.append(col(TestCatalogueItem.is_active).is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            col(TestCatalogueItem.name).ilike(pattern) | col(TestCatalogueItem.code).ilike(pattern)
        )

    total = (
        await session.execute(select(func.count()).select_from(TestCatalogueItem).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(TestCatalogueItem)
                .where(*filters)
                .order_by(col(TestCatalogueItem.name))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_catalogue_item(
    session: AsyncSession, item: TestCatalogueItem, payload: CatalogueItemUpdate
) -> TestCatalogueItem:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    item.updated_at = utc_now()
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def add_analyte(
    session: AsyncSession, item: TestCatalogueItem, payload: AnalyteCreate
) -> TestAnalyte:
    """Add a measured quantity to a test, with its reference bands."""
    code = payload.code.strip().upper()
    clash = (
        (
            await session.execute(
                select(TestAnalyte).where(
                    col(TestAnalyte.catalogue_item_id) == item.id,
                    col(TestAnalyte.code) == code,
                    col(TestAnalyte.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if clash is not None:
        raise ConflictError(
            f"'{code}' is already an analyte on this test.", code="analyte_code_taken"
        )

    analyte = TestAnalyte(
        hospital_id=item.hospital_id,
        catalogue_item_id=item.id,
        **{**payload.model_dump(exclude={"ranges"}), "code": code},
    )
    session.add(analyte)
    await session.flush()

    for range_payload in payload.ranges:
        await add_reference_range(session, analyte, range_payload)

    await session.refresh(analyte)
    return analyte


async def get_analyte(
    session: AsyncSession, analyte_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> TestAnalyte:
    analyte = (
        (
            await session.execute(
                select(TestAnalyte).where(
                    col(TestAnalyte.id) == analyte_id,
                    col(TestAnalyte.hospital_id) == hospital_id,
                    col(TestAnalyte.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if analyte is None:
        raise NotFoundError("Analyte not found.", code="analyte_not_found")
    return analyte


async def list_analytes(session: AsyncSession, catalogue_item_id: uuid.UUID) -> list[TestAnalyte]:
    rows = (
        (
            await session.execute(
                select(TestAnalyte)
                .where(
                    col(TestAnalyte.catalogue_item_id) == catalogue_item_id,
                    col(TestAnalyte.deleted_at).is_(None),
                    col(TestAnalyte.is_active).is_(True),
                )
                .order_by(col(TestAnalyte.display_order), col(TestAnalyte.name))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def add_reference_range(
    session: AsyncSession, analyte: TestAnalyte, payload: ReferenceRangeCreate
) -> ReferenceRange:
    band = ReferenceRange(
        hospital_id=analyte.hospital_id, analyte_id=analyte.id, **payload.model_dump()
    )
    session.add(band)
    await session.flush()
    await session.refresh(band)
    return band


async def list_reference_ranges(
    session: AsyncSession, analyte_id: uuid.UUID
) -> list[ReferenceRange]:
    rows = (
        (
            await session.execute(
                select(ReferenceRange).where(
                    col(ReferenceRange.analyte_id) == analyte_id,
                    col(ReferenceRange.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Range selection and flagging
# ---------------------------------------------------------------------------
def select_reference_range(
    ranges: list[ReferenceRange], *, sex: str | None, age_years: int | None
) -> ReferenceRange | None:
    """Pick the most specific band that fits this patient.

    Specificity beats order: a band that names a sex outranks one that names an
    age band, which outranks the catch-all. A band that names a sex the patient
    does not have, or an age they are not, is not a candidate at all — which is
    why the fallback band (all nulls) has to exist for anything to match a
    patient whose age we never recorded.
    """
    best: ReferenceRange | None = None
    best_score = -1

    for band in ranges:
        score = 0
        if band.sex is not None:
            if sex is None or band.sex.upper() != sex.upper():
                continue
            score += 2
        if band.age_min_years is not None or band.age_max_years is not None:
            if age_years is None:
                continue
            if band.age_min_years is not None and age_years < band.age_min_years:
                continue
            if band.age_max_years is not None and age_years > band.age_max_years:
                continue
            score += 1
        if score > best_score:
            best, best_score = band, score
    return best


def flag_for(value: Decimal, band: ReferenceRange | None) -> tuple[ResultFlag, bool]:
    """Classify a numeric value. Returns `(flag, is_critical)`.

    Panic limits are checked first: a potassium of 7.2 is both "above normal"
    and "ring the ward now", and only the second one matters.
    """
    if band is None:
        return ResultFlag.NORMAL, False
    if band.critical_low is not None and value <= band.critical_low:
        return ResultFlag.CRITICAL_LOW, True
    if band.critical_high is not None and value >= band.critical_high:
        return ResultFlag.CRITICAL_HIGH, True
    if band.low is not None and value < band.low:
        return ResultFlag.LOW, False
    if band.high is not None and value > band.high:
        return ResultFlag.HIGH, False
    return ResultFlag.NORMAL, False


def _age_years(birth_date: date | None, *, today: date | None = None) -> int | None:
    if birth_date is None:
        return None
    reference = today or utc_now().date()
    years = reference.year - birth_date.year
    if (reference.month, reference.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(years, 0)


# ---------------------------------------------------------------------------
# Accessioning
# ---------------------------------------------------------------------------
async def accession_order(
    session: AsyncSession,
    order: Order,
    payload: AccessionRequest,
    *,
    actor: User,
    hospital_id: uuid.UUID,
) -> tuple[DiagnosticReport, Specimen | None]:
    """The lab accepts a doctor's order onto its bench.

    Creates the report shell and, for a laboratory test, the specimen that has
    to be collected. The catalogue entry is chosen here rather than at ordering
    time: a doctor writes "CBC" at speed, and the lab is the one who knows which
    catalogue row that is — and can correct a typo without bouncing the order
    back to the consulting room.
    """
    if order.order_type not in (OrderType.LAB, OrderType.RADIOLOGY):
        raise ValidationError(
            "Only laboratory and radiology orders are handled by diagnostics.",
            code="not_a_diagnostic_order",
            details={"order_type": order.order_type.value},
        )
    if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        raise ConflictError(
            f"This order is already {order.status.value.lower()}.", code="order_closed"
        )

    item = await get_catalogue_item(session, payload.catalogue_item_id, hospital_id=hospital_id)
    expected = _ORDER_TYPE_FOR[item.discipline]
    if expected is not order.order_type:
        raise ValidationError(
            f"'{item.name}' is a {item.discipline.value.lower()} test, but the order is "
            f"{order.order_type.value.lower()}.",
            code="discipline_mismatch",
        )

    existing = await get_report_for_order(session, order.id, hospital_id=hospital_id)
    if existing is not None:
        # Accessioning twice is a double-click, not a second sample.
        raise ConflictError(
            "This order has already been accessioned.",
            code="already_accessioned",
            details={"report_number": existing.report_number},
        )

    specimen: Specimen | None = None
    if item.requires_specimen:
        specimen = Specimen(
            hospital_id=hospital_id,
            accession_number=await _next_number(session, hospital_id, "ACC"),
            order_id=order.id,
            patient_id=order.patient_id,
            specimen_type=item.specimen_type,
            container=item.container,
            status=SpecimenStatus.PENDING_COLLECTION,
            notes=payload.notes,
        )
        session.add(specimen)
        await session.flush()

    report = DiagnosticReport(
        hospital_id=hospital_id,
        report_number=await _next_number(session, hospital_id, "RPT"),
        order_id=order.id,
        encounter_id=order.encounter_id,
        patient_id=order.patient_id,
        catalogue_item_id=item.id,
        specimen_id=specimen.id if specimen else None,
        discipline=item.discipline,
        # Snapshotted: a test renamed next year must not rewrite the heading on
        # a report issued today.
        test_name=item.name,
        status=ReportStatus.REGISTERED,
    )
    session.add(report)

    # The clinical order is now being worked on — that is what the doctor's
    # screen and the encounter's pending list should say.
    if order.status is OrderStatus.REQUESTED:
        await clinical_service.start_order(session, order, actor=actor)

    await session.flush()
    await session.refresh(report)
    return report, specimen


async def get_report_for_order(
    session: AsyncSession, order_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> DiagnosticReport | None:
    """The live report for an order, if one has been accessioned."""
    return (
        (
            await session.execute(
                select(DiagnosticReport).where(
                    col(DiagnosticReport.order_id) == order_id,
                    col(DiagnosticReport.hospital_id) == hospital_id,
                    col(DiagnosticReport.deleted_at).is_(None),
                    col(DiagnosticReport.status).notin_(
                        [ReportStatus.AMENDED, ReportStatus.CANCELLED]
                    ),
                )
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# Specimens
# ---------------------------------------------------------------------------
async def get_specimen(
    session: AsyncSession, specimen_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Specimen:
    specimen = (
        (
            await session.execute(
                select(Specimen).where(
                    col(Specimen.id) == specimen_id,
                    col(Specimen.hospital_id) == hospital_id,
                    col(Specimen.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if specimen is None:
        raise NotFoundError("Sample not found.", code="specimen_not_found")
    return specimen


async def list_specimens(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    status: SpecimenStatus | None = None,
    patient_id: uuid.UUID | None = None,
    pending_only: bool = False,
) -> tuple[list[Specimen], int]:
    """The phlebotomy round and the receipt bench read this."""
    filters: list[ColumnElement[bool]] = [
        col(Specimen.hospital_id) == hospital_id,
        col(Specimen.deleted_at).is_(None),
    ]
    if status is not None:
        filters.append(col(Specimen.status) == status)
    if patient_id is not None:
        filters.append(col(Specimen.patient_id) == patient_id)
    if pending_only:
        filters.append(
            col(Specimen.status).in_([SpecimenStatus.PENDING_COLLECTION, SpecimenStatus.COLLECTED])
        )

    total = (
        await session.execute(select(func.count()).select_from(Specimen).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Specimen)
                .where(*filters)
                .order_by(col(Specimen.created_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def collect_specimen(
    session: AsyncSession, specimen: Specimen, payload: CollectRequest, *, actor: User
) -> Specimen:
    assert_specimen_transition(specimen.status, SpecimenStatus.COLLECTED)

    specimen.status = SpecimenStatus.COLLECTED
    specimen.collected_at = payload.collected_at or utc_now()
    specimen.collected_by_id = actor.id
    specimen.collection_site = payload.collection_site
    if payload.notes:
        specimen.notes = payload.notes
    specimen.updated_at = utc_now()
    session.add(specimen)
    await session.flush()

    await event_bus.publish(
        SpecimenCollected(
            hospital_id=specimen.hospital_id,
            actor_id=actor.id,
            specimen_id=specimen.id,
            order_id=specimen.order_id,
            patient_id=specimen.patient_id,
            accession_number=specimen.accession_number,
            specimen_type=specimen.specimen_type.value,
        ),
        session=session,
    )
    await session.refresh(specimen)
    return specimen


async def receive_specimen(session: AsyncSession, specimen: Specimen, *, actor: User) -> Specimen:
    """Booked in at the bench. Results cannot be entered before this."""
    assert_specimen_transition(specimen.status, SpecimenStatus.RECEIVED)

    specimen.status = SpecimenStatus.RECEIVED
    specimen.received_at = utc_now()
    specimen.received_by_id = actor.id
    specimen.updated_at = utc_now()
    session.add(specimen)
    await session.flush()
    await session.refresh(specimen)
    return specimen


async def reject_specimen(
    session: AsyncSession, specimen: Specimen, *, reason: str, actor: User
) -> Specimen:
    """Haemolysed, clotted, unlabelled, or the wrong tube.

    The report is *not* cancelled and the clinical order stays open: the test
    was still asked for and still has to happen. A fresh sample is collected
    against the same order, which is why this leaves the work outstanding
    rather than quietly making it disappear.
    """
    assert_specimen_transition(specimen.status, SpecimenStatus.REJECTED)

    specimen.status = SpecimenStatus.REJECTED
    specimen.rejected_at = utc_now()
    specimen.rejection_reason = reason
    specimen.updated_at = utc_now()
    session.add(specimen)
    await session.flush()

    await event_bus.publish(
        SpecimenRejected(
            hospital_id=specimen.hospital_id,
            actor_id=actor.id,
            specimen_id=specimen.id,
            order_id=specimen.order_id,
            patient_id=specimen.patient_id,
            accession_number=specimen.accession_number,
            reason=reason,
        ),
        session=session,
    )
    await session.refresh(specimen)
    return specimen


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
async def get_report(
    session: AsyncSession, report_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> DiagnosticReport:
    report = (
        (
            await session.execute(
                select(DiagnosticReport).where(
                    col(DiagnosticReport.id) == report_id,
                    col(DiagnosticReport.hospital_id) == hospital_id,
                    col(DiagnosticReport.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if report is None:
        raise NotFoundError("Report not found.", code="report_not_found")
    return report


async def list_reports(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    status: ReportStatus | None = None,
    discipline: DiagnosticDiscipline | None = None,
    patient_id: uuid.UUID | None = None,
    encounter_id: uuid.UUID | None = None,
    open_only: bool = False,
    critical_only: bool = False,
) -> tuple[list[DiagnosticReport], int]:
    """The lab worklist, the radiology worklist, and a patient's report history."""
    filters: list[ColumnElement[bool]] = [
        col(DiagnosticReport.hospital_id) == hospital_id,
        col(DiagnosticReport.deleted_at).is_(None),
    ]
    if status is not None:
        filters.append(col(DiagnosticReport.status) == status)
    if discipline is not None:
        filters.append(col(DiagnosticReport.discipline) == discipline)
    if patient_id is not None:
        filters.append(col(DiagnosticReport.patient_id) == patient_id)
    if encounter_id is not None:
        filters.append(col(DiagnosticReport.encounter_id) == encounter_id)
    if open_only:
        filters.append(col(DiagnosticReport.status).in_(_EDITABLE))
    if critical_only:
        filters.append(col(DiagnosticReport.has_critical_result).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(DiagnosticReport).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(DiagnosticReport)
                .where(*filters)
                # Criticals to the top of the worklist, then oldest first.
                .order_by(
                    col(DiagnosticReport.has_critical_result).desc(),
                    col(DiagnosticReport.created_at),
                )
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


# ---------------------------------------------------------------------------
# The bench worklist
# ---------------------------------------------------------------------------
async def get_reports_by_orders(
    session: AsyncSession, order_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, DiagnosticReport]:
    """The current report for each of these orders, in one query.

    An order can carry more than one report over its life — an amendment files a
    replacement and leaves the original standing as `AMENDED`, because somebody
    may already have treated the patient on the strength of it. The worklist
    wants the live one, so the newest wins.
    """
    if not order_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(DiagnosticReport)
                .where(
                    col(DiagnosticReport.order_id).in_(tuple(set(order_ids))),
                    col(DiagnosticReport.hospital_id) == hospital_id,
                    col(DiagnosticReport.deleted_at).is_(None),
                )
                # Oldest first, so the newest overwrites as we build the map.
                .order_by(col(DiagnosticReport.created_at))
            )
        )
        .scalars()
        .all()
    )
    return {report.order_id: report for report in rows}


async def get_specimens_by_orders(
    session: AsyncSession, order_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, Specimen]:
    """The sample currently standing against each order, in one query.

    Newest wins here too, and for a sharper reason than reports: a rejected
    sample leaves the order open and a fresh tube is drawn against it. Showing
    the rejected one would tell the round the sample is already at the bench.
    """
    if not order_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(Specimen)
                .where(
                    col(Specimen.order_id).in_(tuple(set(order_ids))),
                    col(Specimen.hospital_id) == hospital_id,
                    col(Specimen.deleted_at).is_(None),
                )
                .order_by(col(Specimen.created_at))
            )
        )
        .scalars()
        .all()
    )
    return {specimen.order_id: specimen for specimen in rows}


def worklist_stage(
    order: Order, report: DiagnosticReport | None, specimen: Specimen | None
) -> WorklistStage:
    """What this request is waiting for — one answer, from three records.

    The bench screen exists to answer "what do I do with this one?", and the
    honest answer is spread across an `Order`, a `Specimen` and a
    `DiagnosticReport`, each with its own status enum. Reading three enums and
    combining them in the head is exactly the sort of work a technician should
    not be doing forty times a morning, so the combination is made here, once,
    where it can be tested — rather than in a template where each client would
    reinvent it slightly differently.
    """
    if report is None:
        return WorklistStage.AWAITING_ACCESSION

    # The sample gate comes first, and it outranks the report's own status: a
    # report sits at `REGISTERED` from the moment it is accessioned, long before
    # there is anything in a tube to measure. Reading the report alone would put
    # "enter results" against a patient nobody has drawn blood from yet.
    if specimen is not None:
        if specimen.status is SpecimenStatus.PENDING_COLLECTION:
            return WorklistStage.AWAITING_COLLECTION
        if specimen.status is SpecimenStatus.COLLECTED:
            return WorklistStage.AWAITING_RECEIPT
        if specimen.status is SpecimenStatus.REJECTED:
            # A rejected sample sends the round back out for another tube. The
            # order was never withdrawn, so the work restarts at the patient's
            # arm — this is the row that must not look finished.
            return WorklistStage.AWAITING_COLLECTION

    if report.status is ReportStatus.REGISTERED:
        return WorklistStage.AWAITING_RESULTS
    # `IN_PROGRESS` means values are filed and unsigned; `PRELIMINARY` means they
    # are filed, released provisionally and still unsigned. Both want the same
    # next act, and neither clears the order until somebody verifies.
    return WorklistStage.AWAITING_VERIFICATION


async def list_worklist_orders(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    discipline: DiagnosticDiscipline | None = None,
) -> tuple[list[Order], int]:
    """Open lab and radiology requests, in the order the bench should work them.

    Keyed on the *order* rather than the report on purpose. A request that has
    not been accessioned yet has no report at all, and it is precisely the one
    most at risk of being forgotten — a worklist drawn from reports is a
    worklist of work that somebody already remembered to start.
    """
    types = (
        (_ORDER_TYPE_FOR[discipline],)
        if discipline is not None
        else tuple(_ORDER_TYPE_FOR.values())
    )
    return await clinical_service.list_orders(
        session, params, hospital_id=hospital_id, order_types=types, open_only=True
    )


async def list_results(session: AsyncSession, report_id: uuid.UUID) -> list[ResultValue]:
    rows = (
        (
            await session.execute(
                select(ResultValue)
                .where(
                    col(ResultValue.report_id) == report_id,
                    col(ResultValue.deleted_at).is_(None),
                )
                .order_by(col(ResultValue.display_order), col(ResultValue.label))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _assert_ready_for_results(session: AsyncSession, report: DiagnosticReport) -> None:
    if report.status not in _EDITABLE:
        raise ConflictError(
            f"This report is {report.status.value.lower()} and cannot be edited. Amend it instead.",
            code="report_not_editable",
            details={"status": report.status.value},
        )
    if report.specimen_id is None:
        return

    specimen = await get_specimen(session, report.specimen_id, hospital_id=report.hospital_id)
    if specimen.status is SpecimenStatus.REJECTED:
        raise ConflictError(
            "The sample was rejected; collect a fresh one before entering results.",
            code="specimen_rejected",
            details={"reason": specimen.rejection_reason},
        )
    if specimen.status is not SpecimenStatus.RECEIVED:
        # The accession step exists precisely so nobody types a result against a
        # tube that is still in a nurse's hand.
        raise ConflictError(
            "The sample has not been received at the lab yet.",
            code="specimen_not_received",
            details={"specimen_status": specimen.status.value},
        )


async def _begin_work(session: AsyncSession, report: DiagnosticReport, *, actor: User) -> None:
    if report.status is ReportStatus.REGISTERED:
        assert_report_transition(report.status, ReportStatus.IN_PROGRESS)
        report.status = ReportStatus.IN_PROGRESS
    report.entered_by_id = actor.id
    report.entered_at = utc_now()
    report.updated_at = utc_now()
    session.add(report)


async def enter_results(
    session: AsyncSession,
    report: DiagnosticReport,
    payload: ResultsSubmission,
    *,
    actor: User,
) -> list[ResultValue]:
    """Enter or correct measured values, flagging each against its range.

    Submissions are partial by design — a technician enters three analytes now
    and the rest when the machine finishes — so values are matched by analyte
    code and updated in place. Nothing is deleted, and nothing already entered
    is cleared by a later partial submission.
    """
    await _assert_ready_for_results(session, report)
    if report.catalogue_item_id is None:
        raise ValidationError(
            "This report has no catalogue entry, so its analytes are unknown.",
            code="report_has_no_catalogue_item",
        )

    analytes = {a.code: a for a in await list_analytes(session, report.catalogue_item_id)}
    unknown = [entry.analyte_code.strip().upper() for entry in payload.results]
    unknown = [code for code in unknown if code not in analytes]
    if unknown:
        raise ValidationError(
            f"Not part of this test: {', '.join(unknown)}.",
            code="unknown_analyte",
            details={"unknown": unknown, "expected": sorted(analytes)},
        )

    patient = await patients_service.get_patient(session, report.patient_id)
    sex = patient.gender.value
    age = _age_years(patient.birth_date)

    existing = {row.analyte_code: row for row in await list_results(session, report.id)}
    newly_critical: list[str] = []
    written: list[ResultValue] = []

    for entry in payload.results:
        analyte = analytes[entry.analyte_code.strip().upper()]
        band = select_reference_range(
            await list_reference_ranges(session, analyte.id), sex=sex, age_years=age
        )

        if entry.value_numeric is not None:
            flag, is_critical = flag_for(entry.value_numeric, band)
        else:
            # No arithmetic will tell you "Growth of E. coli" is abnormal.
            flag = ResultFlag.ABNORMAL if entry.abnormal else ResultFlag.NORMAL
            is_critical = False

        row = existing.get(analyte.code)
        was_critical = row.is_critical if row else False
        if row is None:
            row = ResultValue(
                hospital_id=report.hospital_id,
                report_id=report.id,
                patient_id=report.patient_id,
                analyte_id=analyte.id,
                analyte_code=analyte.code,
                label=analyte.name,
                unit=analyte.unit,
                display_order=analyte.display_order,
            )

        row.value_numeric = entry.value_numeric
        row.value_text = entry.value_text
        row.comment = entry.comment
        # The range is COPIED onto the result, never looked up on read: a
        # hospital that revises its ranges next year must not silently
        # re-interpret a result issued today.
        row.ref_low = band.low if band else None
        row.ref_high = band.high if band else None
        row.ref_text = band.text_range if band else None
        row.flag = flag
        row.is_critical = is_critical
        row.updated_at = utc_now()
        session.add(row)
        written.append(row)

        if is_critical and not was_critical:
            unit = f" {analyte.unit}" if analyte.unit else ""
            newly_critical.append(f"{analyte.name} {entry.value_numeric}{unit} ({flag.value})")

    await _begin_work(session, report, actor=actor)
    if payload.performed_at is not None:
        report.performed_at = payload.performed_at
    elif report.performed_at is None:
        report.performed_at = utc_now()
    await session.flush()

    stored = await list_results(session, report.id)
    report.has_critical_result = any(row.is_critical for row in stored)
    session.add(report)
    await session.flush()

    if newly_critical:
        # Fired on entry, not on verification: a potassium of 7.2 does not wait
        # for a signature.
        logger.warning(
            "critical result on report %s: %s", report.report_number, "; ".join(newly_critical)
        )
        await event_bus.publish(
            CriticalResultFlagged(
                hospital_id=report.hospital_id,
                actor_id=actor.id,
                report_id=report.id,
                order_id=report.order_id,
                encounter_id=report.encounter_id,
                patient_id=report.patient_id,
                test_name=report.test_name,
                critical_values=tuple(newly_critical),
            ),
            session=session,
        )
    return written


async def enter_narrative(
    session: AsyncSession,
    report: DiagnosticReport,
    payload: NarrativeEntry,
    *,
    actor: User,
) -> DiagnosticReport:
    """The radiologist's findings and impression, or a pathology comment."""
    await _assert_ready_for_results(session, report)

    if payload.findings is not None:
        report.findings = payload.findings
    if payload.impression is not None:
        report.impression = payload.impression
    if payload.technique is not None:
        report.technique = payload.technique
    if payload.performed_at is not None:
        report.performed_at = payload.performed_at
    elif report.performed_at is None:
        report.performed_at = utc_now()

    await _begin_work(session, report, actor=actor)
    await session.flush()
    await session.refresh(report)
    return report


async def release_preliminary(
    session: AsyncSession, report: DiagnosticReport, *, actor: User
) -> DiagnosticReport:
    """Release before verification, clearly labelled provisional.

    Deliberately does **not** clear the clinical order: a provisional number is
    something a clinician may look at, not something a visit may close on.
    """
    assert_report_transition(report.status, ReportStatus.PRELIMINARY)
    await _assert_has_content(session, report)

    report.status = ReportStatus.PRELIMINARY
    report.updated_at = utc_now()
    session.add(report)
    await session.flush()
    await session.refresh(report)
    return report


async def _assert_has_content(session: AsyncSession, report: DiagnosticReport) -> None:
    if report.findings or report.impression:
        return
    if await list_results(session, report.id):
        return
    raise ValidationError(
        "There is nothing to release — enter results or findings first.",
        code="report_empty",
    )


def assert_may_verify(report: DiagnosticReport, roles: frozenset[str]) -> None:
    """Second gate on verification: the right *kind* of clinician.

    `result:verify` says a user may sign reports off; this says which discipline.
    A radiologist countersigning a blood culture is the same class of mistake as
    a cashier resulting one.
    """
    allowed = VERIFICATION_ROLES.get(report.discipline, frozenset())
    if not (roles & allowed):
        raise PermissionDeniedError(
            f"{report.discipline.value.title()} reports are verified by "
            f"{', '.join(sorted(role.replace('_', ' ').title() for role in allowed))}.",
            code="wrong_verification_role",
            details={"discipline": report.discipline.value, "allowed_roles": sorted(allowed)},
        )


async def verify_report(
    session: AsyncSession,
    report: DiagnosticReport,
    *,
    actor: User,
    hospital_id: uuid.UUID,
    registration_number: str | None = None,
) -> DiagnosticReport:
    """Sign the report off, release it, and clear the clinical order.

    That last step is the whole point of the module: verification is what turns
    a measurement into something the encounter may close on. It is a direct call
    into `clinical.service` rather than an event, because a swallowed handler
    would leave the visit open forever with its result already filed.

    Lab work is verified by someone other than whoever entered it — the second
    pair of eyes is the reason verification exists. Radiology is exempt: the
    radiologist who dictates the report is the one who signs it, and that is
    correct practice rather than a loophole.
    """
    # Emptiness is checked first on purpose. A REGISTERED report is also an
    # illegal transition to FINAL, but "there is nothing to release yet" tells
    # the technician what to do next and "a report that is registered cannot
    # become final" does not.
    await _assert_has_content(session, report)
    assert_report_transition(report.status, ReportStatus.FINAL)

    if (
        report.discipline is DiagnosticDiscipline.LAB
        and report.entered_by_id is not None
        and report.entered_by_id == actor.id
    ):
        raise PermissionDeniedError(
            "A laboratory result must be verified by someone other than the person who entered it.",
            code="self_verification",
        )

    moment = utc_now()
    report.status = ReportStatus.FINAL
    report.verified_by_id = actor.id
    report.verified_by_name = actor.full_name
    report.verifier_registration_number = registration_number
    report.verified_at = moment
    report.updated_at = moment
    session.add(report)
    await session.flush()

    order = await clinical_service.get_order(session, report.order_id, hospital_id=hospital_id)
    if order.status is OrderStatus.CANCELLED:
        # The doctor called the investigation off while it was on the bench.
        # Signing it now would file a result against work nobody asked for any
        # more, and it would never clear anything. Cancel the report instead.
        raise ConflictError(
            "The order for this report was cancelled; it cannot be signed off.",
            code="order_cancelled",
            details={"order_id": str(order.id)},
        )
    if order.status is not OrderStatus.COMPLETED:
        # Clearing the last outstanding item is what closes the visit
        # (CLAUDE.md §6). The verifier never has to know that.
        await clinical_service.complete_order(
            session,
            order,
            actor=actor,
            hospital_id=hospital_id,
            fulfilment_ref=report.report_number,
        )

    await event_bus.publish(
        ReportReady(
            hospital_id=report.hospital_id,
            actor_id=actor.id,
            report_id=report.id,
            order_id=report.order_id,
            encounter_id=report.encounter_id,
            patient_id=report.patient_id,
            report_number=report.report_number,
            test_name=report.test_name,
            discipline=report.discipline.value,
            has_critical_result=report.has_critical_result,
        ),
        session=session,
    )
    await session.refresh(report)
    return report


async def amend_report(
    session: AsyncSession,
    report: DiagnosticReport,
    payload: AmendRequest,
    *,
    actor: User,
) -> DiagnosticReport:
    """Supersede a verified report with a corrected one.

    The original is never edited. It becomes `AMENDED` and the replacement
    points back at it, so both versions survive — somebody may already have
    treated the patient on the strength of the first one, and erasing it would
    erase why.
    """
    assert_report_transition(report.status, ReportStatus.AMENDED)

    report.status = ReportStatus.AMENDED
    report.amendment_reason = payload.reason
    report.updated_at = utc_now()
    session.add(report)
    # Flushed before the replacement is inserted: the partial unique index
    # allows only one live report per order, and the old one has to step aside
    # first.
    await session.flush()

    replacement = DiagnosticReport(
        hospital_id=report.hospital_id,
        report_number=await _next_number(session, report.hospital_id, "RPT"),
        order_id=report.order_id,
        encounter_id=report.encounter_id,
        patient_id=report.patient_id,
        catalogue_item_id=report.catalogue_item_id,
        specimen_id=report.specimen_id,
        discipline=report.discipline,
        test_name=report.test_name,
        status=ReportStatus.IN_PROGRESS,
        findings=report.findings,
        impression=report.impression,
        technique=report.technique,
        performed_at=report.performed_at,
        entered_by_id=actor.id,
        entered_at=utc_now(),
        amends_id=report.id,
        amendment_reason=payload.reason,
    )
    session.add(replacement)
    await session.flush()

    # Carry the values across so the amender corrects one number rather than
    # retyping twelve.
    for row in await list_results(session, report.id):
        session.add(
            ResultValue(
                hospital_id=row.hospital_id,
                report_id=replacement.id,
                patient_id=row.patient_id,
                analyte_id=row.analyte_id,
                analyte_code=row.analyte_code,
                label=row.label,
                unit=row.unit,
                display_order=row.display_order,
                value_numeric=row.value_numeric,
                value_text=row.value_text,
                ref_low=row.ref_low,
                ref_high=row.ref_high,
                ref_text=row.ref_text,
                flag=row.flag,
                is_critical=row.is_critical,
                comment=row.comment,
            )
        )
    replacement.has_critical_result = report.has_critical_result
    session.add(replacement)
    await session.flush()

    await event_bus.publish(
        ReportAmended(
            hospital_id=report.hospital_id,
            actor_id=actor.id,
            report_id=replacement.id,
            superseded_report_id=report.id,
            order_id=report.order_id,
            patient_id=report.patient_id,
            reason=payload.reason,
        ),
        session=session,
    )
    await session.refresh(replacement)
    return replacement


async def cancel_report(
    session: AsyncSession,
    report: DiagnosticReport,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> DiagnosticReport:
    """Call the investigation off, and stop it holding the visit open.

    The clinical order is cancelled with it: an abandoned study that leaves its
    order outstanding is exactly how an encounter sits in `PENDING_CLEARANCE`
    forever with nobody able to say why.
    """
    assert_report_transition(report.status, ReportStatus.CANCELLED)

    report.status = ReportStatus.CANCELLED
    report.cancellation_reason = reason
    report.updated_at = utc_now()
    session.add(report)
    await session.flush()

    order = await clinical_service.get_order(session, report.order_id, hospital_id=hospital_id)
    if order.status not in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        await clinical_service.cancel_order(
            session, order, reason=reason, actor=actor, hospital_id=hospital_id
        )

    await session.refresh(report)
    return report


async def cancel_report_for_order(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    hospital_id: uuid.UUID,
    reason: str,
) -> DiagnosticReport | None:
    """Take a report off the lab worklist because its order was called off.

    The mirror image of `cancel_report`, and deliberately **not** the same
    function: this one does not touch the clinical order, because the order is
    what triggered it. Calling `cancel_order` from here would publish
    `OrderCancelled` again and recurse.

    A report that has already been signed is left alone. Somebody may have acted
    on that result, and unpicking it is an amendment with a reason, not a
    silent cancellation triggered by a subscriber.
    """
    report = await get_report_for_order(session, order_id, hospital_id=hospital_id)
    if report is None:
        return None
    if report.status in (ReportStatus.FINAL, ReportStatus.AMENDED, ReportStatus.CANCELLED):
        logger.info(
            "not cancelling report %s: already %s",
            report.report_number,
            report.status.value.lower(),
        )
        return report

    assert_report_transition(report.status, ReportStatus.CANCELLED)
    report.status = ReportStatus.CANCELLED
    report.cancellation_reason = reason
    report.updated_at = utc_now()
    session.add(report)

    if report.specimen_id is not None:
        specimen = await get_specimen(session, report.specimen_id, hospital_id=hospital_id)
        if specimen.status not in TERMINAL_SPECIMEN_STATUSES:
            specimen.status = SpecimenStatus.CANCELLED
            specimen.updated_at = utc_now()
            session.add(specimen)

    await session.flush()
    await session.refresh(report)
    return report


async def acknowledge_critical(
    session: AsyncSession,
    report: DiagnosticReport,
    payload: CriticalCallback,
    *,
    actor: User,
) -> DiagnosticReport:
    """Record the phone call a panic value demands.

    NABH wants the callback documented, and "called the ward" is not
    documentation — who was told, and when.
    """
    if not report.has_critical_result:
        raise ValidationError(
            "This report has no critical result to acknowledge.",
            code="no_critical_result",
        )

    report.critical_notified_at = payload.notified_at or utc_now()
    report.critical_notified_by_id = actor.id
    report.critical_notified_to = payload.notified_to
    report.updated_at = utc_now()
    session.add(report)
    await session.flush()
    await session.refresh(report)
    return report
