"""Inpatient business logic — the module's public interface.

CLAUDE.md §13 step 9. Four things live here and they are worth reading in this
order, because each depends on the one before it:

1. **Capacity** — wards, beds, and the lifecycle that stops the bed board lying.
2. **The stay** — admit, transfer, discharge, hanging off the existing Encounter.
3. **The chart** — medication orders, and the doses they generate.
4. **The document** — a discharge summary compiled from what is already recorded.

---------------------------------------------------------------------------
The rule this module exists to keep
---------------------------------------------------------------------------

**A bed's state and a patient's location are changed together or not at all.**
Every function that moves a patient writes the `bed_assignments` row and the
`beds.status` in the same transaction, and the database has a partial unique
index on each side to catch what the code misses: one live assignment per bed,
one live assignment per admission. A bed board that disagrees with reality is
not a display bug — it is a patient walked to an occupied room at 2am.

The corollary is that `beds.status` is **never assigned directly** anywhere
outside `_move_bed`, on the same principle as `Encounter.status` and
`state_machine.transition`. `transitions.py` holds the legal moves, and the one
it exists to forbid is `OCCUPIED -> AVAILABLE`: a bed a patient has just left
must pass through `CLEANING` before anybody is sent to it.

---------------------------------------------------------------------------
Where this module stops
---------------------------------------------------------------------------

`Encounter.status` is not this module's to change. `admit` and `discharge` in
`clinical.service` do that, and `ipd` calls them — the state machine belongs to
the module that owns it (CLAUDE.md §14).

A **death** or a **self-discharge** on the ward does not come through
`discharge_patient` at all. Those are `clinical.record_death` and
`record_lama`, which capture the structured metadata §6 requires; this module
then closes the admission and frees the bed by subscribing to the resulting
event. One way to record a death, not two.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.config import settings
from app.core.events import event_bus
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, EncounterType
from app.modules.diagnostics import service as diagnostics_service
from app.modules.diagnostics.models import ReportStatus
from app.modules.identity.models import User
from app.modules.ipd import summary as summary_builder
from app.modules.ipd.events import (
    BedDayAccrued,
    BedReleased,
    DischargeSummarySigned,
    MedicationAdministered,
    MedicationDoseMissed,
    PatientAdmitted,
    PatientDischarged,
    PatientTransferred,
)
from app.modules.ipd.models import (
    Admission,
    AdmissionRequest,
    AdmissionRequestStatus,
    AdmissionSequence,
    AdmissionStatus,
    Bed,
    BedAssignment,
    BedClass,
    BedStatus,
    DischargeSummary,
    DischargeType,
    DoseStatus,
    MedicationAdministration,
    MedicationOrder,
    SummaryStatus,
    Ward,
)
from app.modules.ipd.schemas import (
    AdmissionCreate,
    AdmissionUpdate,
    AdmitFromRequest,
    BedCreate,
    BedUpdate,
    DischargeRequest,
    DoseRecord,
    MedicationOrderCreate,
    SummaryUpdate,
    WardCreate,
    WardUpdate,
)
from app.modules.ipd.transitions import assert_admission_transition, assert_bed_transition
from app.modules.patients import service as patients_service
from app.modules.scheduling import service as scheduling_service

logger = logging.getLogger(__name__)

__all__ = [
    "accrual_key",
    "accrue_bed_days",
    "admit_from_request",
    "admit_patient",
    "assign_bed",
    "build_board",
    "cancel_admission",
    "cancel_admission_request",
    "close_admission_for_terminal_encounter",
    "compile_summary",
    "create_bed",
    "create_ward",
    "current_assignment",
    "discharge_patient",
    "get_admission",
    "get_admission_by_encounter",
    "get_admission_request",
    "get_bed",
    "get_current_beds",
    "get_dose",
    "get_medication_order",
    "get_summary",
    "get_summary_by_id",
    "get_ward",
    "get_wards_by_ids",
    "initiate_discharge",
    "length_of_stay",
    "list_admission_requests",
    "list_admissions",
    "list_beds",
    "list_doses",
    "list_medication_orders",
    "list_summaries",
    "list_wards",
    "mark_bed_cleaned",
    "mark_missed_doses",
    "occupancy",
    "pending_admission_request",
    "prescribe",
    "record_dose",
    "release_bed",
    "request_admission",
    "reserve_bed",
    "restore_bed",
    "sign_summary",
    "stop_medication",
    "take_bed_out_of_service",
    "top_up_charts",
    "transfer_patient",
    "update_admission",
    "update_bed",
    "update_summary",
    "update_ward",
]

# Namespace for deterministic bed-day accrual keys. Fixed forever: changing it
# would make every historical night look unbilled and the next sweep would
# charge the whole hospital twice.
_ACCRUAL_NAMESPACE = uuid.UUID("6f1d5b8a-3c2e-5a47-9f10-2b7d4e8c1a90")

_OPEN_ADMISSION_STATUSES = (AdmissionStatus.ADMITTED, AdmissionStatus.DISCHARGE_INITIATED)


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
def financial_year(on: date) -> int:
    """India's financial year runs April to March — the same rule billing uses."""
    return on.year if on.month >= 4 else on.year - 1


async def _next_admission_number(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """`IPD-YYYY-NNNNNN`, unique per hospital per financial year.

    Upsert-then-lock, the same shape as every other counter in the system: a
    check-then-insert races on the first allocation of the year, which is
    precisely when several admissions desks open at once.
    """
    year = financial_year(datetime.now(UTC).date())
    await session.execute(
        text(
            """
            INSERT INTO admission_sequences
                (id, hospital_id, year, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :year, 0, now(), now())
            ON CONFLICT (hospital_id, year) DO NOTHING
            """
        ),
        {"id": new_id(), "hospital_id": hospital_id, "year": year},
    )
    row = (
        (
            await session.execute(
                select(AdmissionSequence)
                .where(
                    col(AdmissionSequence.hospital_id) == hospital_id,
                    col(AdmissionSequence.year) == year,
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
    return f"IPD-{year % 100:02d}{(year + 1) % 100:02d}-{row.last_value:06d}"


# ---------------------------------------------------------------------------
# Wards
# ---------------------------------------------------------------------------
async def create_ward(
    session: AsyncSession, payload: WardCreate, *, hospital_id: uuid.UUID
) -> Ward:
    code = payload.code.strip().upper()
    if await _ward_by_code(session, hospital_id=hospital_id, code=code) is not None:
        raise ConflictError(f"Ward {code} already exists.", code="ward_exists")

    ward = Ward(
        hospital_id=hospital_id,
        code=code,
        name=payload.name.strip(),
        floor=payload.floor,
        bed_class=payload.bed_class,
        department_id=payload.department_id,
        gender_policy=payload.gender_policy,
        phone_extension=payload.phone_extension,
    )
    session.add(ward)
    await session.flush()
    await session.refresh(ward)
    return ward


async def _ward_by_code(session: AsyncSession, *, hospital_id: uuid.UUID, code: str) -> Ward | None:
    return (
        (
            await session.execute(
                select(Ward).where(
                    col(Ward.hospital_id) == hospital_id,
                    col(Ward.code) == code,
                    col(Ward.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def get_ward(session: AsyncSession, ward_id: uuid.UUID, *, hospital_id: uuid.UUID) -> Ward:
    ward = (
        (
            await session.execute(
                select(Ward).where(
                    col(Ward.id) == ward_id,
                    col(Ward.hospital_id) == hospital_id,
                    col(Ward.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if ward is None:
        raise NotFoundError("Ward not found.", code="ward_not_found")
    return ward


async def update_ward(session: AsyncSession, ward: Ward, payload: WardUpdate) -> Ward:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ward, field, value)
    session.add(ward)
    await session.flush()
    await session.refresh(ward)
    return ward


async def list_wards(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    include_inactive: bool = False,
) -> tuple[list[Ward], int]:
    filters: list[ColumnElement[bool]] = [
        col(Ward.hospital_id) == hospital_id,
        col(Ward.deleted_at).is_(None),
    ]
    if not include_inactive:
        filters.append(col(Ward.is_active).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(Ward).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Ward)
                .where(*filters)
                .order_by(col(Ward.code))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


# ---------------------------------------------------------------------------
# Beds
# ---------------------------------------------------------------------------
async def create_bed(session: AsyncSession, payload: BedCreate, *, hospital_id: uuid.UUID) -> Bed:
    ward = await get_ward(session, payload.ward_id, hospital_id=hospital_id)
    code = payload.code.strip().upper()

    existing = (
        (
            await session.execute(
                select(Bed).where(
                    col(Bed.hospital_id) == hospital_id,
                    col(Bed.code) == code,
                    col(Bed.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise ConflictError(f"Bed {code} already exists.", code="bed_exists")

    bed = Bed(
        hospital_id=hospital_id,
        ward_id=ward.id,
        code=code,
        label=payload.label,
        # A bed is normally whatever its ward is. Making staff retype it per bed
        # is how a ward ends up with one bed priced differently by accident.
        bed_class=payload.bed_class or ward.bed_class,
        tariff_item_code=payload.tariff_item_code,
    )
    session.add(bed)
    await session.flush()
    await session.refresh(bed)
    return bed


async def get_bed(session: AsyncSession, bed_id: uuid.UUID, *, hospital_id: uuid.UUID) -> Bed:
    bed = (
        (
            await session.execute(
                select(Bed).where(
                    col(Bed.id) == bed_id,
                    col(Bed.hospital_id) == hospital_id,
                    col(Bed.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if bed is None:
        raise NotFoundError("Bed not found.", code="bed_not_found")
    return bed


async def update_bed(session: AsyncSession, bed: Bed, payload: BedUpdate) -> Bed:
    """Edit a bed's label, class or tariff. Never its status — that is a move."""
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(bed, field, value)
    session.add(bed)
    await session.flush()
    await session.refresh(bed)
    return bed


async def list_beds(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    ward_id: uuid.UUID | None = None,
    status: BedStatus | None = None,
    bed_class: BedClass | None = None,
    include_inactive: bool = False,
) -> tuple[list[Bed], int]:
    filters: list[ColumnElement[bool]] = [
        col(Bed.hospital_id) == hospital_id,
        col(Bed.deleted_at).is_(None),
    ]
    if ward_id is not None:
        filters.append(col(Bed.ward_id) == ward_id)
    if status is not None:
        filters.append(col(Bed.status) == status)
    if bed_class is not None:
        filters.append(col(Bed.bed_class) == bed_class)
    if not include_inactive:
        filters.append(col(Bed.is_active).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(Bed).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Bed)
                .where(*filters)
                .order_by(col(Bed.ward_id), col(Bed.code))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def _move_bed(session: AsyncSession, bed: Bed, target: BedStatus) -> Bed:
    """The only place `beds.status` is ever assigned.

    Same discipline as `Encounter.status` and `state_machine.transition`, for the
    same reason: a second assignment site is a second place for the
    `OCCUPIED -> AVAILABLE` shortcut to reappear, and that shortcut is a patient
    sent to an unmade bed.
    """
    assert_bed_transition(bed.status, target)
    bed.status = target
    if target is not BedStatus.RESERVED:
        bed.reserved_for_patient_id = None
    if target is not BedStatus.OUT_OF_SERVICE:
        bed.out_of_service_reason = None
    session.add(bed)
    await session.flush()
    return bed


async def reserve_bed(
    session: AsyncSession, bed: Bed, *, patient_id: uuid.UUID, hospital_id: uuid.UUID
) -> Bed:
    """Hold a bed for a named patient — a theatre case coming back, a planned admission.

    Named rather than merely blocked, so a ward asking "why is 12 unavailable?"
    gets an answer instead of a mystery.
    """
    await patients_service.get_patient(session, patient_id)
    bed = await _move_bed(session, bed, BedStatus.RESERVED)
    bed.reserved_for_patient_id = patient_id
    session.add(bed)
    await session.flush()
    return bed


async def mark_bed_cleaned(session: AsyncSession, bed: Bed) -> Bed:
    """Housekeeping's action: the bed is turned around and can be offered again."""
    bed = await _move_bed(session, bed, BedStatus.AVAILABLE)
    bed.released_at = None
    session.add(bed)
    await session.flush()
    return bed


async def take_bed_out_of_service(session: AsyncSession, bed: Bed, *, reason: str) -> Bed:
    bed = await _move_bed(session, bed, BedStatus.OUT_OF_SERVICE)
    bed.out_of_service_reason = reason
    session.add(bed)
    await session.flush()
    return bed


async def restore_bed(session: AsyncSession, bed: Bed) -> Bed:
    """Back from maintenance — into cleaning, not straight into service."""
    return await _move_bed(session, bed, BedStatus.CLEANING)


# ---------------------------------------------------------------------------
# The bed board
# ---------------------------------------------------------------------------
async def current_assignment(
    session: AsyncSession, admission_id: uuid.UUID
) -> BedAssignment | None:
    return (
        (
            await session.execute(
                select(BedAssignment).where(
                    col(BedAssignment.admission_id) == admission_id,
                    col(BedAssignment.released_at).is_(None),
                    col(BedAssignment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def get_current_beds(
    session: AsyncSession, admission_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, Bed]:
    """Where each of these admissions currently is, keyed by admission.

    Two queries for a whole census page — the live assignments, then the beds
    they point at — rather than the two-per-row `current_assignment` +
    `get_bed` pair. A ward census is read on every round, and it is the list
    that grows precisely when the ward is busiest.
    """
    if not admission_ids:
        return {}

    assignments = (
        (
            await session.execute(
                select(BedAssignment).where(
                    col(BedAssignment.admission_id).in_(set(admission_ids)),
                    col(BedAssignment.hospital_id) == hospital_id,
                    col(BedAssignment.released_at).is_(None),
                    col(BedAssignment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not assignments:
        return {}

    beds = (
        (
            await session.execute(
                select(Bed).where(
                    col(Bed.id).in_({row.bed_id for row in assignments}),
                    col(Bed.hospital_id) == hospital_id,
                    col(Bed.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {bed.id: bed for bed in beds}
    return {
        assignment.admission_id: by_id[assignment.bed_id]
        for assignment in assignments
        if assignment.bed_id in by_id
    }


async def get_wards_by_ids(
    session: AsyncSession, ward_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, Ward]:
    """Many wards at once, keyed by id — the bulk form of `get_ward`."""
    if not ward_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(Ward).where(
                    col(Ward.id).in_(set(ward_ids)),
                    col(Ward.hospital_id) == hospital_id,
                    col(Ward.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {ward.id: ward for ward in rows}


async def _live_assignments(
    session: AsyncSession, *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, BedAssignment]:
    rows = (
        (
            await session.execute(
                select(BedAssignment).where(
                    col(BedAssignment.hospital_id) == hospital_id,
                    col(BedAssignment.released_at).is_(None),
                    col(BedAssignment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.bed_id: row for row in rows}


async def build_board(
    session: AsyncSession, *, hospital_id: uuid.UUID, ward_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """The nursing-station screen: every ward, every bed, who is in it.

    Assembled in one pass rather than a query per bed. A 300-bed hospital
    refreshing a board that costs 300 round trips is a board nobody leaves open,
    and a bed board nobody leaves open is a bed board that is always stale.
    """
    wards, _ = await list_wards(session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id)
    if ward_id is not None:
        wards = [ward for ward in wards if ward.id == ward_id]

    beds, _ = await list_beds(
        session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id, ward_id=ward_id
    )
    assignments = await _live_assignments(session, hospital_id=hospital_id)

    # One lookup for every occupant, rather than one per occupied bed.
    admission_ids = {row.admission_id for row in assignments.values()}
    admissions: dict[uuid.UUID, Admission] = {}
    patients: dict[uuid.UUID, Any] = {}
    if admission_ids:
        found = (
            (await session.execute(select(Admission).where(col(Admission.id).in_(admission_ids))))
            .scalars()
            .all()
        )
        admissions = {row.id: row for row in found}
        for admission in found:
            if admission.patient_id not in patients:
                patients[admission.patient_id] = await patients_service.get_patient(
                    session, admission.patient_id
                )

    board: list[dict[str, Any]] = []
    for ward in wards:
        ward_beds = [bed for bed in beds if bed.ward_id == ward.id]
        cells: list[dict[str, Any]] = []
        for bed in sorted(ward_beds, key=lambda item: item.code):
            cell: dict[str, Any] = {
                "id": bed.id,
                "code": bed.code,
                "label": bed.label,
                "bed_class": bed.bed_class,
                "status": bed.status,
            }
            assignment = assignments.get(bed.id)
            if assignment is not None:
                occupant = admissions.get(assignment.admission_id)
                patient = patients.get(assignment.patient_id)
                cell |= {
                    "patient_id": assignment.patient_id,
                    "patient_name": patient.full_name if patient else None,
                    "uhid": patient.uhid if patient else None,
                    "admission_id": assignment.admission_id,
                    "admitted_at": occupant.admitted_at if occupant else None,
                }
            cells.append(cell)

        board.append(
            {
                "id": ward.id,
                "code": ward.code,
                "name": ward.name,
                "bed_class": ward.bed_class,
                "gender_policy": ward.gender_policy,
                "beds": cells,
                "occupied": sum(1 for bed in ward_beds if bed.status is BedStatus.OCCUPIED),
                "available": sum(1 for bed in ward_beds if bed.status is BedStatus.AVAILABLE),
                "cleaning": sum(1 for bed in ward_beds if bed.status is BedStatus.CLEANING),
            }
        )
    return board


async def occupancy(session: AsyncSession, *, hospital_id: uuid.UUID) -> dict[str, Any]:
    """The number management asks for at 9am.

    Out-of-service beds are excluded from the denominator: a hospital that
    closes a ward for renovation has not become 100% occupied, and a rate that
    says so sends people looking for capacity that was never there.
    """
    beds, _ = await list_beds(session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id)
    counts = {status: sum(1 for bed in beds if bed.status is status) for status in BedStatus}
    usable = len(beds) - counts[BedStatus.OUT_OF_SERVICE]
    return {
        "total_beds": len(beds),
        "occupied": counts[BedStatus.OCCUPIED],
        "available": counts[BedStatus.AVAILABLE],
        "cleaning": counts[BedStatus.CLEANING],
        "reserved": counts[BedStatus.RESERVED],
        "out_of_service": counts[BedStatus.OUT_OF_SERVICE],
        "occupancy_rate": round(counts[BedStatus.OCCUPIED] / usable, 4) if usable else 0.0,
    }


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
async def assign_bed(
    session: AsyncSession,
    admission: Admission,
    bed: Bed,
    *,
    actor: User | None = None,
    reason: str | None = None,
) -> BedAssignment:
    """Put a patient in a bed: the assignment row and the bed status, together.

    Never one without the other. The partial unique indexes on
    `bed_assignments` are the hard guarantee — one live assignment per bed, one
    per admission — and this function is what normally gets there first with a
    sentence a human can act on.
    """
    if not bed.is_active:
        raise ConflictError(f"Bed {bed.code} is not in use.", code="bed_inactive")
    if not bed.is_occupiable:
        raise ConflictError(
            f"Bed {bed.code} is {bed.status.value.lower().replace('_', ' ')}.",
            code="bed_unavailable",
            details={"bed_status": bed.status.value},
        )
    if bed.status is BedStatus.RESERVED and bed.reserved_for_patient_id not in (
        None,
        admission.patient_id,
    ):
        raise ConflictError(
            f"Bed {bed.code} is reserved for another patient.",
            code="bed_reserved_for_other",
        )

    assignment = BedAssignment(
        hospital_id=admission.hospital_id,
        admission_id=admission.id,
        bed_id=bed.id,
        patient_id=admission.patient_id,
        # Snapshotted: a bed reclassified next year must not retrospectively
        # change what this patient was charged for.
        bed_class=bed.bed_class,
        tariff_item_code=bed.tariff_item_code,
        assigned_at=utc_now(),
        transfer_reason=reason,
        assigned_by_id=actor.id if actor else None,
    )
    session.add(assignment)
    await _move_bed(session, bed, BedStatus.OCCUPIED)
    await session.flush()
    await session.refresh(assignment)
    return assignment


async def _release_assignment(
    session: AsyncSession, assignment: BedAssignment, *, at: datetime | None = None
) -> Bed:
    """End an occupancy and send the bed to housekeeping.

    `CLEANING`, never `AVAILABLE` — see `transitions.py`.
    """
    released = at or utc_now()
    assignment.released_at = released
    session.add(assignment)

    bed = (
        (await session.execute(select(Bed).where(col(Bed.id) == assignment.bed_id))).scalars().one()
    )
    if bed.status is BedStatus.OCCUPIED:
        await _move_bed(session, bed, BedStatus.CLEANING)
        bed.released_at = released
        session.add(bed)
    await session.flush()
    return bed


async def release_bed(
    session: AsyncSession, admission: Admission, *, at: datetime | None = None
) -> Bed | None:
    """Free whatever bed this admission currently holds."""
    assignment = await current_assignment(session, admission.id)
    if assignment is None:
        return None
    bed = await _release_assignment(session, assignment, at=at)
    await event_bus.publish(
        BedReleased(
            hospital_id=admission.hospital_id,
            bed_id=bed.id,
            ward_id=bed.ward_id,
            admission_id=admission.id,
            bed_code=bed.code,
        ),
        session=session,
    )
    return bed


async def admit_patient(
    session: AsyncSession,
    payload: AdmissionCreate,
    *,
    hospital_id: uuid.UUID,
    actor: User | None = None,
) -> Admission:
    """Admit a patient: the Encounter becomes an inpatient stay and takes a bed.

    Order matters. The bed is taken *before* the encounter transitions, because
    the encounter transition publishes `EncounterAdmitted` and a subscriber that
    reads the ward should not see an admitted patient with nowhere to be.
    """
    encounter = await clinical_service.get_encounter(
        session, payload.encounter_id, hospital_id=hospital_id
    )
    if encounter.status is EncounterStatus.ADMITTED:
        existing = await get_admission_by_encounter(session, encounter.id, hospital_id=hospital_id)
        if existing is not None:
            raise ConflictError(
                f"This visit is already admitted as {existing.admission_number}.",
                code="already_admitted",
                details={"admission_id": str(existing.id)},
            )

    patient = await patients_service.get_patient(session, encounter.patient_id)
    if patient.is_deceased:
        raise ConflictError(
            "This patient is recorded as deceased.",
            code="patient_deceased",
        )

    bed = await get_bed(session, payload.bed_id, hospital_id=hospital_id)
    ward = await get_ward(session, bed.ward_id, hospital_id=hospital_id)
    _assert_ward_accepts(ward, patient.gender.value if patient.gender else None)

    admission = Admission(
        hospital_id=hospital_id,
        admission_number=await _next_admission_number(session, hospital_id),
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        attending_doctor_id=payload.attending_doctor_id,
        department_id=payload.department_id or ward.department_id or encounter.department_id,
        admitted_at=payload.admitted_at or utc_now(),
        provisional_diagnosis=payload.provisional_diagnosis,
        admission_notes=payload.admission_notes,
        expected_stay_days=payload.expected_stay_days,
        attendant_name=payload.attendant_name,
        attendant_phone=payload.attendant_phone,
        attendant_relation=payload.attendant_relation,
    )
    session.add(admission)
    await session.flush()

    await assign_bed(session, admission, bed, actor=actor, reason="Admission")
    # Whichever way the patient got here — the desk working its list, or a
    # doctor admitting straight from the consultation screen — a request left
    # waiting for them would keep them on the desk's list after they are in a
    # bed.
    await _fulfil_pending_request(session, admission, actor=actor)

    # The encounter's own transition, through the module that owns the state
    # machine. `admit` is idempotent-ish here: a casualty patient may already be
    # ADMITTED if the ward moved first.
    if encounter.status is not EncounterStatus.ADMITTED:
        await clinical_service.admit(
            session,
            encounter,
            actor=actor,
            reason=f"Admitted to {ward.code} bed {bed.code}.",
            department_id=admission.department_id,
        )
    if encounter.encounter_type is not EncounterType.IPD:
        encounter.encounter_type = EncounterType.IPD
        session.add(encounter)

    await session.flush()
    await session.refresh(admission)

    await event_bus.publish(
        PatientAdmitted(
            hospital_id=hospital_id,
            actor_id=actor.id if actor else None,
            admission_id=admission.id,
            encounter_id=encounter.id,
            patient_id=admission.patient_id,
            admission_number=admission.admission_number,
            bed_id=bed.id,
            ward_id=ward.id,
            bed_class=bed.bed_class.value,
            attending_doctor_id=admission.attending_doctor_id,
        ),
        session=session,
    )
    logger.info("admitted %s to %s bed %s", admission.admission_number, ward.code, bed.code)
    return admission


# ---------------------------------------------------------------------------
# Admission requests — the OPD counter handing a patient to the admission desk
# ---------------------------------------------------------------------------
# A visit in any of these can still become an admission (see the encounter
# transition table in `clinical.state_machine`). COMPLETED is accepted too and
# handled at admission time: the OPD visit is over, so the stay opens a new
# IPD visit rather than reopening a closed one.
_ADMITTABLE_VISITS: frozenset[EncounterStatus] = frozenset(
    {
        EncounterStatus.REGISTERED,
        EncounterStatus.IN_CONSULTATION,
        EncounterStatus.AWAITING_RESULTS,
        EncounterStatus.PENDING_CLEARANCE,
    }
)
_IN_A_BED: tuple[AdmissionStatus, ...] = (
    AdmissionStatus.ADMITTED,
    AdmissionStatus.DISCHARGE_INITIATED,
)


async def _pending_request_for_patient(
    session: AsyncSession, patient_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> AdmissionRequest | None:
    return (
        (
            await session.execute(
                select(AdmissionRequest).where(
                    col(AdmissionRequest.hospital_id) == hospital_id,
                    col(AdmissionRequest.patient_id) == patient_id,
                    col(AdmissionRequest.status) == AdmissionRequestStatus.PENDING,
                    col(AdmissionRequest.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def request_admission(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID,
    note: str | None = None,
    actor: User | None = None,
) -> AdmissionRequest:
    """Send a seen patient to the admission desk.

    Refused where the desk could only refuse it later: a visit that ended in
    death, referral or LAMA, a patient already in a bed, a patient already
    waiting at the desk. Everything else is the desk's call to make.
    """
    encounter = await clinical_service.get_encounter(session, encounter_id, hospital_id=hospital_id)
    if encounter.status is EncounterStatus.ADMITTED:
        raise ConflictError("This visit is already an admission.", code="already_admitted")
    if (
        encounter.status not in _ADMITTABLE_VISITS
        and encounter.status is not EncounterStatus.COMPLETED
    ):
        raise ConflictError(
            f"A visit that ended as {encounter.status.value.lower().replace('_', ' ')} "
            "cannot be sent for admission.",
            code="encounter_not_admittable",
            details={"status": encounter.status.value},
        )

    patient = await patients_service.get_patient(session, encounter.patient_id)
    if patient.is_deceased:
        raise ConflictError("This patient is recorded as deceased.", code="patient_deceased")

    in_bed = (
        (
            await session.execute(
                select(Admission).where(
                    col(Admission.hospital_id) == hospital_id,
                    col(Admission.patient_id) == patient.id,
                    col(Admission.status).in_(_IN_A_BED),
                    col(Admission.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if in_bed is not None:
        raise ConflictError(
            f"This patient is already admitted as {in_bed.admission_number}.",
            code="already_inpatient",
            details={"admission_id": str(in_bed.id)},
        )

    waiting = await _pending_request_for_patient(session, patient.id, hospital_id=hospital_id)
    if waiting is not None:
        raise ConflictError(
            "This patient is already waiting at the admission desk.",
            code="admission_already_requested",
            details={"admission_request_id": str(waiting.id)},
        )

    doctor_name: str | None = None
    if encounter.doctor_id is not None:
        try:
            doctor = await scheduling_service.get_doctor(
                session, encounter.doctor_id, hospital_id=hospital_id
            )
            doctor_name = doctor.display_name
        except NotFoundError:
            # A doctor profile retired since the visit. The request still
            # stands; the desk just sees no name.
            doctor_name = None

    request = AdmissionRequest(
        hospital_id=hospital_id,
        patient_id=patient.id,
        encounter_id=encounter.id,
        doctor_id=encounter.doctor_id,
        department_id=encounter.department_id,
        doctor_name=doctor_name,
        status=AdmissionRequestStatus.PENDING,
        note=note,
        requested_at=utc_now(),
        requested_by_id=actor.id if actor else None,
        requested_by_name=actor.full_name if actor else None,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two counters sent the same patient at the same instant. The check
        # above let both through; the partial unique index did not.
        raise ConflictError(
            "This patient is already waiting at the admission desk.",
            code="admission_already_requested",
        ) from exc
    await session.refresh(request)
    logger.info("admission requested for patient %s", patient.id)
    return request


async def get_admission_request(
    session: AsyncSession, request_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> AdmissionRequest:
    request = (
        (
            await session.execute(
                select(AdmissionRequest).where(
                    col(AdmissionRequest.id) == request_id,
                    col(AdmissionRequest.hospital_id) == hospital_id,
                    col(AdmissionRequest.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if request is None:
        raise NotFoundError("Admission request not found.", code="admission_request_not_found")
    return request


async def list_admission_requests(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    statuses: Collection[AdmissionRequestStatus] | None = None,
    since: datetime | None = None,
    newest_first: bool = False,
) -> tuple[list[AdmissionRequest], int]:
    """The desk's list. Oldest first by default — first come, first admitted."""
    filters: list[ColumnElement[bool]] = [
        col(AdmissionRequest.hospital_id) == hospital_id,
        col(AdmissionRequest.deleted_at).is_(None),
    ]
    if statuses:
        filters.append(col(AdmissionRequest.status).in_(list(statuses)))
    if since is not None:
        filters.append(col(AdmissionRequest.requested_at) >= since)

    total = (
        await session.execute(select(func.count()).select_from(AdmissionRequest).where(*filters))
    ).scalar_one()
    order = col(AdmissionRequest.requested_at)
    rows = (
        (
            await session.execute(
                select(AdmissionRequest)
                .where(*filters)
                .order_by(order.desc() if newest_first else order.asc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def _assert_pending(request: AdmissionRequest) -> None:
    if request.status is not AdmissionRequestStatus.PENDING:
        raise ConflictError(
            f"This request has already been {request.status.value.lower()}.",
            code="admission_request_closed",
            details={"status": request.status.value},
        )


async def cancel_admission_request(
    session: AsyncSession,
    request: AdmissionRequest,
    *,
    reason: str,
    actor: User | None = None,
) -> AdmissionRequest:
    """Turn a request away — sent in error, the patient went elsewhere.

    The pending request was holding the OPD visit open (it counts as pending
    work); once it goes, the visit is given the chance to close, exactly as
    when a lab result or a payment clears.
    """
    _assert_pending(request)
    moment = utc_now()
    request.status = AdmissionRequestStatus.CANCELLED
    request.cancellation_reason = reason
    request.handled_at = moment
    request.handled_by_id = actor.id if actor else None
    request.updated_at = moment
    session.add(request)
    await session.flush()

    encounter = await clinical_service.get_encounter(
        session, request.encounter_id, hospital_id=request.hospital_id
    )
    if encounter.status is EncounterStatus.PENDING_CLEARANCE:
        await clinical_service.reevaluate_closure(session, encounter, actor=actor)

    await session.refresh(request)
    return request


async def admit_from_request(
    session: AsyncSession,
    request: AdmissionRequest,
    payload: AdmitFromRequest,
    *,
    actor: User | None = None,
) -> Admission:
    """The desk admits a patient it was sent. The IPD flow starts here.

    Uses the visit the patient was seen on while it is still open — the chart,
    the diagnosis and the bill carry straight on into the stay. If the doctor
    has already closed that visit, a new IPD visit is opened for the stay
    instead of reopening a closed one (CLAUDE.md §6: terminal is terminal).
    IPD visits carry no consultation charge, so this does not bill the
    patient twice.
    """
    _assert_pending(request)
    encounter = await clinical_service.get_encounter(
        session, request.encounter_id, hospital_id=request.hospital_id
    )
    attending = payload.attending_doctor_id or request.doctor_id
    department = payload.department_id or request.department_id

    if encounter.status in _ADMITTABLE_VISITS:
        target = encounter
    elif encounter.status is EncounterStatus.COMPLETED:
        target = await clinical_service.open_encounter(
            session,
            hospital_id=request.hospital_id,
            patient_id=request.patient_id,
            actor=actor,
            doctor_id=attending,
            department_id=department,
            encounter_type=EncounterType.IPD,
            chief_complaint=request.note,
        )
    else:
        raise ConflictError(
            f"The visit ended as {encounter.status.value.lower().replace('_', ' ')}; "
            "the patient cannot be admitted from it.",
            code="encounter_not_admittable",
            details={"status": encounter.status.value},
        )

    return await admit_patient(
        session,
        AdmissionCreate(
            encounter_id=target.id,
            bed_id=payload.bed_id,
            attending_doctor_id=attending,
            department_id=department,
            provisional_diagnosis=payload.provisional_diagnosis,
            admission_notes=payload.admission_notes,
            expected_stay_days=payload.expected_stay_days,
            attendant_name=payload.attendant_name,
            attendant_phone=payload.attendant_phone,
            attendant_relation=payload.attendant_relation,
        ),
        hospital_id=request.hospital_id,
        actor=actor,
    )


async def _fulfil_pending_request(
    session: AsyncSession, admission: Admission, *, actor: User | None
) -> None:
    request = await _pending_request_for_patient(
        session, admission.patient_id, hospital_id=admission.hospital_id
    )
    if request is None:
        return
    moment = utc_now()
    request.status = AdmissionRequestStatus.ADMITTED
    request.admission_id = admission.id
    request.handled_at = moment
    request.handled_by_id = actor.id if actor else None
    request.updated_at = moment
    session.add(request)


async def pending_admission_request(
    session: AsyncSession, encounter: Encounter
) -> clinical_service.PendingContribution | None:
    """`ipd`'s answer to "is anything of yours still holding this visit open?".

    A patient waiting at the admission desk is not a finished OPD visit. Without
    this, the doctor's "complete visit" would close it — and the night's
    auto-close certainly would — and the desk would then be admitting a patient
    whose visit had ended, which the state machine rightly refuses. Held open,
    the visit stays admittable until the desk either admits or turns it away.
    """
    waiting = (
        (
            await session.execute(
                select(AdmissionRequest.id).where(
                    col(AdmissionRequest.encounter_id) == encounter.id,
                    col(AdmissionRequest.status) == AdmissionRequestStatus.PENDING,
                    col(AdmissionRequest.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if waiting is None:
        return None
    return clinical_service.PendingContribution(
        source="ADMISSION",
        count=1,
        descriptions=("Admission: waiting at the admission desk",),
        blocks_closure=True,
    )


def _assert_ward_accepts(ward: Ward, gender: str | None) -> None:
    """Enforce a single-sex ward rather than merely displaying its policy.

    Putting a man in a female ward is a complaint, and sometimes a serious
    incident. `UNKNOWN` is allowed through: an unconscious admission with no
    recorded gender must not be blocked at the door over a data-entry field.
    """
    if ward.gender_policy is None or gender in (None, "UNKNOWN", "OTHER"):
        return
    if gender != ward.gender_policy:
        raise ConflictError(
            f"{ward.name} admits {ward.gender_policy.lower()} patients only.",
            code="ward_gender_policy",
            details={"ward_policy": ward.gender_policy, "patient_gender": gender},
        )


async def get_admission(
    session: AsyncSession, admission_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Admission:
    admission = (
        (
            await session.execute(
                select(Admission).where(
                    col(Admission.id) == admission_id,
                    col(Admission.hospital_id) == hospital_id,
                    col(Admission.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if admission is None:
        raise NotFoundError("Admission not found.", code="admission_not_found")
    return admission


async def get_admission_by_encounter(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Admission | None:
    return (
        (
            await session.execute(
                select(Admission).where(
                    col(Admission.encounter_id) == encounter_id,
                    col(Admission.hospital_id) == hospital_id,
                    col(Admission.status) != AdmissionStatus.CANCELLED,
                    col(Admission.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_admissions(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    status: AdmissionStatus | None = None,
    ward_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    attending_doctor_id: uuid.UUID | None = None,
    open_only: bool = False,
) -> tuple[list[Admission], int]:
    """The ward census, a patient's stay history, and a consultant's list."""
    filters: list[ColumnElement[bool]] = [
        col(Admission.hospital_id) == hospital_id,
        col(Admission.deleted_at).is_(None),
    ]
    if status is not None:
        filters.append(col(Admission.status) == status)
    if open_only:
        filters.append(col(Admission.status).in_(_OPEN_ADMISSION_STATUSES))
    if patient_id is not None:
        filters.append(col(Admission.patient_id) == patient_id)
    if attending_doctor_id is not None:
        filters.append(col(Admission.attending_doctor_id) == attending_doctor_id)
    if ward_id is not None:
        # Through the live assignment: a patient's ward is where they are now,
        # not where they were admitted.
        bed_ids = select(col(Bed.id)).where(col(Bed.ward_id) == ward_id)
        occupied = select(col(BedAssignment.admission_id)).where(
            col(BedAssignment.bed_id).in_(bed_ids),
            col(BedAssignment.released_at).is_(None),
            col(BedAssignment.deleted_at).is_(None),
        )
        filters.append(col(Admission.id).in_(occupied))

    total = (
        await session.execute(select(func.count()).select_from(Admission).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Admission)
                .where(*filters)
                .order_by(col(Admission.admitted_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_admission(
    session: AsyncSession, admission: Admission, payload: AdmissionUpdate
) -> Admission:
    _assert_open(admission)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(admission, field, value)
    session.add(admission)
    await session.flush()
    await session.refresh(admission)
    return admission


def _assert_open(admission: Admission) -> None:
    if not admission.is_open:
        raise ConflictError(
            f"This admission is {admission.status.value.lower().replace('_', ' ')}.",
            code="admission_closed",
            details={"status": admission.status.value},
        )


async def transfer_patient(
    session: AsyncSession,
    admission: Admission,
    *,
    to_bed: Bed,
    reason: str | None = None,
    actor: User | None = None,
) -> BedAssignment:
    """Move a patient between beds, closing one occupancy and opening the next.

    The old bed goes to `CLEANING`, exactly as it would on discharge. A bed
    somebody has just been moved out of is not a bed the next patient can be
    walked to, and a transfer is the case where that is easiest to forget.
    """
    _assert_open(admission)
    current = await current_assignment(session, admission.id)
    if current is not None and current.bed_id == to_bed.id:
        raise ConflictError("The patient is already in that bed.", code="same_bed")

    ward = await get_ward(session, to_bed.ward_id, hospital_id=admission.hospital_id)
    patient = await patients_service.get_patient(session, admission.patient_id)
    _assert_ward_accepts(ward, patient.gender.value if patient.gender else None)

    from_bed_id = current.bed_id if current else None
    from_class = current.bed_class.value if current else ""

    if current is not None:
        await _release_assignment(session, current)

    assignment = await assign_bed(session, admission, to_bed, actor=actor, reason=reason)

    if from_bed_id is not None:
        await event_bus.publish(
            PatientTransferred(
                hospital_id=admission.hospital_id,
                actor_id=actor.id if actor else None,
                admission_id=admission.id,
                patient_id=admission.patient_id,
                from_bed_id=from_bed_id,
                to_bed_id=to_bed.id,
                from_bed_class=from_class,
                to_bed_class=to_bed.bed_class.value,
                reason=reason,
            ),
            session=session,
        )
    return assignment


async def _set_admission_status(
    session: AsyncSession, admission: Admission, target: AdmissionStatus
) -> Admission:
    assert_admission_transition(admission.status, target)
    admission.status = target
    session.add(admission)
    await session.flush()
    return admission


async def initiate_discharge(
    session: AsyncSession, admission: Admission, *, actor: User | None = None
) -> Admission:
    """The doctor's "this patient can go" — but the bed stays theirs.

    Between this and the patient actually leaving there is a bill to settle and
    medicines to collect, often hours. A bed board that frees the bed at the
    doctor's signature is a bed board that double-books it.

    Compiles the discharge summary as a side effect if there is none. That is
    the §7 move: by the time anybody asks the consultant for a summary, a draft
    assembled from what they already wrote is waiting to be edited.
    """
    _assert_open(admission)
    await _set_admission_status(session, admission, AdmissionStatus.DISCHARGE_INITIATED)
    admission.discharge_initiated_at = utc_now()
    session.add(admission)

    if await get_summary(session, admission.id) is None:
        await compile_summary(session, admission, actor=actor)
    await session.flush()
    return admission


async def discharge_patient(
    session: AsyncSession,
    admission: Admission,
    payload: DischargeRequest,
    *,
    actor: User | None = None,
) -> Admission:
    """End the stay: free the bed, close the admission, close the Encounter.

    A death or a self-discharge never arrives here — the schema refuses those
    discharge types, because both are recorded against the Encounter through
    `clinical` with the structured metadata CLAUDE.md §6 requires, and this
    module follows via `close_admission_for_terminal_encounter`.
    """
    _assert_open(admission)

    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=admission.hospital_id
    )
    now = utc_now()

    await release_bed(session, admission, at=now)
    await _cancel_outstanding_doses(session, admission, reason="Patient discharged.")

    await _set_admission_status(session, admission, AdmissionStatus.DISCHARGED)

    admission.discharged_at = now
    admission.discharge_type = payload.discharge_type
    admission.discharged_by_id = actor.id if actor else None
    session.add(admission)
    await session.flush()

    summary = await get_summary(session, admission.id)
    if summary is None:
        summary = await compile_summary(session, admission, actor=actor)
    if payload.condition_at_discharge:
        summary.condition_at_discharge = payload.condition_at_discharge
    if payload.follow_up_instructions:
        summary.follow_up_instructions = payload.follow_up_instructions
    if payload.follow_up_date:
        summary.follow_up_date = payload.follow_up_date
    session.add(summary)

    # The Encounter's own closure, through the module that owns the state
    # machine. `billing` settles and `notifications` sends the follow-up off the
    # back of the resulting `EncounterClosed` — neither is called from here.
    await clinical_service.discharge(
        session,
        encounter,
        actor=actor,
        reason=payload.notes or f"Discharged: {payload.discharge_type.value.lower()}.",
    )

    await session.flush()
    await session.refresh(admission)
    await event_bus.publish(
        PatientDischarged(
            hospital_id=admission.hospital_id,
            actor_id=actor.id if actor else None,
            admission_id=admission.id,
            encounter_id=admission.encounter_id,
            patient_id=admission.patient_id,
            admission_number=admission.admission_number,
            discharge_type=payload.discharge_type.value,
            length_of_stay_days=length_of_stay(admission),
        ),
        session=session,
    )
    return admission


def length_of_stay(admission: Admission, *, now: datetime | None = None) -> int:
    """Nights, counted the way a hospital counts them.

    Day-case admissions and discharges are one day, not zero: a patient who came
    in and went home the same afternoon still occupied a bed, and an ALOS that
    records them as zero understates every ward's workload.
    """
    end = admission.discharged_at or now or utc_now()
    return max((end.date() - admission.admitted_at.date()).days, 1)


async def cancel_admission(
    session: AsyncSession, admission: Admission, *, reason: str, actor: User | None = None
) -> Admission:
    """Admitted in error — wrong patient, duplicate record.

    Distinct from a discharge: nothing happened, so no bed-day is owed and the
    stay does not appear in occupancy statistics. The encounter is deliberately
    left where it is, because unpicking a clinical status is a decision for the
    people who made it rather than a side effect of a correction here.
    """
    _assert_open(admission)
    await release_bed(session, admission)
    await _cancel_outstanding_doses(session, admission, reason="Admission cancelled.")
    await _set_admission_status(session, admission, AdmissionStatus.CANCELLED)
    admission.cancellation_reason = reason
    session.add(admission)
    await session.flush()
    await session.refresh(admission)
    return admission


async def close_admission_for_terminal_encounter(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID,
    final_status: str,
    actor_id: uuid.UUID | None = None,
) -> Admission | None:
    """Close a stay whose Encounter ended somewhere else.

    A death on the ward, a self-discharge, a transfer out: all three are
    recorded against the Encounter by `clinical`, which is the only place that
    captures the metadata CLAUDE.md §6 requires. This is `ipd` following, so the
    bed is freed and the chart stopped without a second way to record a death.
    """
    admission = await get_admission_by_encounter(session, encounter_id, hospital_id=hospital_id)
    if admission is None or not admission.is_open:
        return None

    mapping = {
        EncounterStatus.DECEASED.value: DischargeType.DECEASED,
        EncounterStatus.LAMA.value: DischargeType.LAMA,
        EncounterStatus.REFERRED_OUT.value: DischargeType.REFERRED,
        EncounterStatus.COMPLETED.value: DischargeType.RECOVERED,
    }
    discharge_type = mapping.get(final_status, DischargeType.RECOVERED)
    now = utc_now()

    await release_bed(session, admission, at=now)
    await _cancel_outstanding_doses(
        session, admission, reason=f"Stay ended: {final_status.lower().replace('_', ' ')}."
    )
    await _set_admission_status(session, admission, AdmissionStatus.DISCHARGED)
    admission.discharged_at = now
    admission.discharge_type = discharge_type
    admission.discharged_by_id = actor_id
    session.add(admission)
    await session.flush()

    if await get_summary(session, admission.id) is None:
        # A death still needs a summary — it is what the family and the
        # certifying process are given.
        await compile_summary(session, admission)

    await event_bus.publish(
        PatientDischarged(
            hospital_id=hospital_id,
            actor_id=actor_id,
            admission_id=admission.id,
            encounter_id=encounter_id,
            patient_id=admission.patient_id,
            admission_number=admission.admission_number,
            discharge_type=discharge_type.value,
            length_of_stay_days=length_of_stay(admission),
        ),
        session=session,
    )
    logger.info("closed admission %s following %s", admission.admission_number, final_status)
    return admission


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
async def prescribe(
    session: AsyncSession,
    admission: Admission,
    payload: MedicationOrderCreate,
    *,
    actor: User | None = None,
) -> MedicationOrder:
    """Write a drug onto the chart, and materialise the doses it is due.

    The materialisation is the point. A `MedicationOrder` alone says what the
    patient is on; the `MedicationAdministration` rows say what is due at 08:00
    and whether anybody gave it. Without them a missed dose has no row to be
    missing from.
    """
    _assert_open(admission)
    starts = payload.starts_at or utc_now()

    order = MedicationOrder(
        hospital_id=admission.hospital_id,
        admission_id=admission.id,
        encounter_id=admission.encounter_id,
        patient_id=admission.patient_id,
        drug_name=payload.drug_name.strip(),
        dose=payload.dose.strip(),
        route=payload.route,
        frequency=payload.frequency.strip(),
        times_per_day=0 if payload.is_prn else payload.times_per_day,
        dose_times=list(payload.dose_times),
        is_prn=payload.is_prn,
        prn_indication=payload.prn_indication,
        drug_schedule=payload.drug_schedule,
        starts_at=starts,
        ends_at=payload.ends_at,
        instructions=payload.instructions,
        prescribed_by_id=actor.id if actor else None,
    )
    session.add(order)
    await session.flush()

    await materialise_doses(session, order)
    await session.refresh(order)
    return order


def _slot_times(order: MedicationOrder) -> list[time]:
    """The clock times a scheduled drug is due at.

    An explicit `dose_times` wins. Otherwise the day is divided evenly starting
    at 08:00 — a defensible default that a ward can override, rather than a
    guess that pretends to be a policy.
    """
    if order.dose_times:
        parsed: list[time] = []
        for entry in order.dose_times:
            hour, _, minute = entry.partition(":")
            parsed.append(time(hour=int(hour), minute=int(minute)))
        return sorted(parsed)

    count = max(order.times_per_day, 1)
    step = 24 // count
    return [time(hour=(8 + step * index) % 24) for index in range(count)]


async def materialise_doses(
    session: AsyncSession,
    order: MedicationOrder,
    *,
    through: datetime | None = None,
) -> int:
    """Create the DUE slots for a scheduled drug. Returns how many were added.

    Idempotent: the partial unique index on (order, due_at) means a re-run tops
    up rather than doubles the chart, which is what lets the daily sweep call
    this for every active order without checking first.

    Horizon is deliberately short — a couple of days, not the whole course.
    A chart materialised weeks ahead is a chart full of doses for orders that
    will be stopped tomorrow, and every one of them would age into a false
    "missed".
    """
    if not order.generates_slots:
        return 0

    horizon = through or (utc_now() + timedelta(days=settings.IPD_CHART_HORIZON_DAYS))
    if order.ends_at is not None:
        horizon = min(horizon, order.ends_at)

    existing = set(
        (
            await session.execute(
                select(col(MedicationAdministration.due_at)).where(
                    col(MedicationAdministration.medication_order_id) == order.id,
                    col(MedicationAdministration.status) != DoseStatus.CANCELLED,
                    col(MedicationAdministration.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    day = order.starts_at.date()
    while day <= horizon.date():
        for slot in _slot_times(order):
            due = datetime.combine(day, slot, tzinfo=UTC)
            if due < order.starts_at or due > horizon:
                continue
            if any(abs((due - seen).total_seconds()) < 1 for seen in existing):
                continue
            session.add(
                MedicationAdministration(
                    hospital_id=order.hospital_id,
                    medication_order_id=order.id,
                    admission_id=order.admission_id,
                    patient_id=order.patient_id,
                    # Snapshotted, for the same reason a result snapshots its
                    # reference range: a drug re-dosed next week must not rewrite
                    # what this patient was given today.
                    drug_name=order.drug_name,
                    dose=order.dose,
                    route=order.route,
                    due_at=due,
                )
            )
            existing.add(due)
            created += 1
        day += timedelta(days=1)

    if created:
        await session.flush()
    return created


async def get_medication_order(
    session: AsyncSession, order_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> MedicationOrder:
    order = (
        (
            await session.execute(
                select(MedicationOrder).where(
                    col(MedicationOrder.id) == order_id,
                    col(MedicationOrder.hospital_id) == hospital_id,
                    col(MedicationOrder.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if order is None:
        raise NotFoundError("Medication order not found.", code="medication_order_not_found")
    return order


async def list_medication_orders(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    admission_id: uuid.UUID | None = None,
    active_only: bool = True,
) -> tuple[list[MedicationOrder], int]:
    filters: list[ColumnElement[bool]] = [
        col(MedicationOrder.hospital_id) == hospital_id,
        col(MedicationOrder.deleted_at).is_(None),
    ]
    if admission_id is not None:
        filters.append(col(MedicationOrder.admission_id) == admission_id)
    if active_only:
        filters.append(col(MedicationOrder.is_active).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(MedicationOrder).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(MedicationOrder)
                .where(*filters)
                .order_by(col(MedicationOrder.created_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def stop_medication(
    session: AsyncSession, order: MedicationOrder, *, reason: str, actor: User | None = None
) -> MedicationOrder:
    """Stop a drug, and cancel the doses it has not reached yet.

    Future slots are `CANCELLED`, not deleted. "This was scheduled and then
    stopped" and "this was never prescribed" are different facts, and a chart
    that quietly loses the first cannot explain a gap in a course.
    """
    if not order.is_active:
        raise ConflictError("That drug is already stopped.", code="medication_stopped")

    order.is_active = False
    order.stopped_at = utc_now()
    order.stopped_by_id = actor.id if actor else None
    order.stop_reason = reason
    session.add(order)

    await _cancel_future_slots(
        session,
        col(MedicationAdministration.medication_order_id) == order.id,
        reason=reason,
    )
    await session.flush()
    await session.refresh(order)
    return order


async def _cancel_future_slots(session: AsyncSession, *conditions: Any, reason: str) -> None:
    """Cancel not-yet-due slots matching `conditions`.

    An ORM `update()` with `synchronize_session="fetch"` rather than raw SQL, on
    purpose. A textual UPDATE bypasses the identity map, so anything already
    loaded in this session keeps its old status and the very next read returns a
    row that no longer matches the database. That is a subtle enough trap to be
    worth the slightly heavier statement.

    Only *future* slots. A dose that was due at 06:00 and never recorded stays
    outstanding and ages into `MISSED`: stopping the drug at noon does not
    unmake the fact that nobody gave the morning one.
    """
    await session.execute(
        sa_update(MedicationAdministration)
        .where(
            *conditions,
            col(MedicationAdministration.status) == DoseStatus.DUE,
            col(MedicationAdministration.due_at) > utc_now(),
            col(MedicationAdministration.deleted_at).is_(None),
        )
        .values(status=DoseStatus.CANCELLED, reason=reason[:255], updated_at=utc_now())
        .execution_options(synchronize_session="fetch")
    )


async def _cancel_outstanding_doses(
    session: AsyncSession, admission: Admission, *, reason: str
) -> None:
    """Close the chart when the patient leaves.

    Only future slots. A dose that was due at 06:00 and never recorded stays
    outstanding and ages into `MISSED` — the patient having gone home at noon
    does not unmake the fact that nobody gave their morning antibiotic.
    """
    await _cancel_future_slots(
        session,
        col(MedicationAdministration.admission_id) == admission.id,
        reason=reason,
    )
    await session.execute(
        sa_update(MedicationOrder)
        .where(
            col(MedicationOrder.admission_id) == admission.id,
            col(MedicationOrder.is_active).is_(True),
            col(MedicationOrder.deleted_at).is_(None),
        )
        .values(
            is_active=False,
            stopped_at=utc_now(),
            stop_reason=reason[:255],
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()


async def get_dose(
    session: AsyncSession, dose_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> MedicationAdministration:
    dose = (
        (
            await session.execute(
                select(MedicationAdministration).where(
                    col(MedicationAdministration.id) == dose_id,
                    col(MedicationAdministration.hospital_id) == hospital_id,
                    col(MedicationAdministration.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if dose is None:
        raise NotFoundError("Dose not found.", code="dose_not_found")
    return dose


async def list_doses(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    admission_id: uuid.UUID | None = None,
    status: DoseStatus | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> tuple[list[MedicationAdministration], int]:
    """The nurse's screen: what is due on this patient, in this window."""
    filters: list[ColumnElement[bool]] = [
        col(MedicationAdministration.hospital_id) == hospital_id,
        col(MedicationAdministration.deleted_at).is_(None),
    ]
    if admission_id is not None:
        filters.append(col(MedicationAdministration.admission_id) == admission_id)
    if status is not None:
        filters.append(col(MedicationAdministration.status) == status)
    if due_from is not None:
        filters.append(col(MedicationAdministration.due_at) >= due_from)
    if due_to is not None:
        filters.append(col(MedicationAdministration.due_at) <= due_to)

    total = (
        await session.execute(
            select(func.count()).select_from(MedicationAdministration).where(*filters)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(MedicationAdministration)
                .where(*filters)
                .order_by(col(MedicationAdministration.due_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def record_dose(
    session: AsyncSession,
    dose: MedicationAdministration,
    payload: DoseRecord,
    *,
    actor: User | None = None,
) -> MedicationAdministration:
    """A nurse signing for one dose — given, refused, held, or missed.

    A recorded dose is not re-recordable. The chart is a clinical record, and
    "it says given but it was actually held" is a correction with a trail, not
    an edit — which is why a settled slot refuses rather than overwriting.
    """
    if dose.status is not DoseStatus.DUE:
        raise ConflictError(
            f"That dose is already recorded as {dose.status.value.lower()}.",
            code="dose_already_recorded",
            details={"status": dose.status.value},
        )

    dose.status = payload.status
    dose.reason = payload.reason
    dose.notes = payload.notes
    dose.auto_missed = False
    if payload.status is DoseStatus.GIVEN:
        dose.administered_at = payload.administered_at or utc_now()
        dose.administered_by_id = actor.id if actor else None
        dose.administered_by_name = actor.full_name if actor else None
    session.add(dose)
    await session.flush()
    await session.refresh(dose)

    if payload.status is DoseStatus.GIVEN:
        order = await get_medication_order(
            session, dose.medication_order_id, hospital_id=dose.hospital_id
        )
        await event_bus.publish(
            MedicationAdministered(
                hospital_id=dose.hospital_id,
                actor_id=actor.id if actor else None,
                administration_id=dose.id,
                admission_id=dose.admission_id,
                patient_id=dose.patient_id,
                encounter_id=order.encounter_id,
                drug_name=dose.drug_name,
                dose=dose.dose,
                route=dose.route.value,
                administered_by_id=dose.administered_by_id,
            ),
            session=session,
        )
    elif payload.status in (DoseStatus.MISSED, DoseStatus.REFUSED):
        await event_bus.publish(
            MedicationDoseMissed(
                hospital_id=dose.hospital_id,
                actor_id=actor.id if actor else None,
                administration_id=dose.id,
                admission_id=dose.admission_id,
                patient_id=dose.patient_id,
                drug_name=dose.drug_name,
                due_at=dose.due_at,
                auto=False,
            ),
            session=session,
        )
    return dose


async def mark_missed_doses(
    session: AsyncSession, *, hospital_id: uuid.UUID, grace: timedelta | None = None
) -> int:
    """The sweep that turns silence into a record. Returns how many it marked.

    This is the safety net that makes the chart honest. A dose nobody recorded
    is not evidence that nothing happened — it is evidence that nobody wrote
    anything, and after the window closes that is itself the finding. `auto_missed`
    keeps the two apart: a nurse recording a miss is documented care, and the
    sweep noticing an absence is a process failure worth escalating.
    """
    window = grace or timedelta(hours=2)
    cutoff = utc_now() - window

    rows = (
        (
            await session.execute(
                select(MedicationAdministration).where(
                    col(MedicationAdministration.hospital_id) == hospital_id,
                    col(MedicationAdministration.status) == DoseStatus.DUE,
                    col(MedicationAdministration.due_at) < cutoff,
                    col(MedicationAdministration.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for dose in rows:
        dose.status = DoseStatus.MISSED
        dose.auto_missed = True
        dose.reason = "No administration recorded within the window."
        session.add(dose)
        await event_bus.publish(
            MedicationDoseMissed(
                hospital_id=hospital_id,
                administration_id=dose.id,
                admission_id=dose.admission_id,
                patient_id=dose.patient_id,
                drug_name=dose.drug_name,
                due_at=dose.due_at,
                auto=True,
            ),
            session=session,
        )
    if rows:
        await session.flush()
        logger.info("marked %d dose(s) missed for hospital %s", len(rows), hospital_id)
    return len(rows)


async def top_up_charts(session: AsyncSession, *, hospital_id: uuid.UUID) -> int:
    """Extend every active chart to the materialisation horizon. Returns slots added."""
    orders, _ = await list_medication_orders(
        session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id, active_only=True
    )
    added = 0
    for order in orders:
        added += await materialise_doses(session, order)
    return added


# ---------------------------------------------------------------------------
# Bed-day accrual
# ---------------------------------------------------------------------------
def accrual_key(admission_id: uuid.UUID, service_date: date) -> uuid.UUID:
    """A deterministic id for one admission-night.

    UUIDv5 rather than a stored row: it makes the charge idempotent against a
    sweep that restarts mid-pass or runs twice after a deploy, without needing a
    table whose only job is to remember what was already billed. `billing`
    already keys charges on (source module, type, id), so this slots straight
    into a guarantee that exists.
    """
    return uuid.uuid5(_ACCRUAL_NAMESPACE, f"{admission_id}:{service_date.isoformat()}")


async def accrue_bed_days(
    session: AsyncSession, *, hospital_id: uuid.UUID, upto: date | None = None
) -> dict[str, int]:
    """Emit one `BedDayAccrued` per admission per night not yet accrued.

    Nightly rather than at discharge, so the running bill is live — a family
    asking on day four what the stay has cost gets an answer, which is most of
    what CLAUDE.md §7b's "automatic billing" is worth in an inpatient setting.

    The bed class charged for a night is the one the patient was in at the
    *end* of it. A patient stepped down from ICU at 4pm is charged the ward rate
    for that night, which is both the common convention and the one that does
    not reward a late transfer.
    """
    through = upto or utc_now().date()
    admissions, _ = await list_admissions(
        session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id, open_only=True
    )
    # Stays that closed today still owe their final nights.
    closed, _ = await list_admissions(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        status=AdmissionStatus.DISCHARGED,
    )
    candidates = admissions + [
        row
        for row in closed
        if row.discharged_at is not None and row.discharged_at.date() >= through - timedelta(days=2)
    ]

    emitted = 0
    for admission in candidates:
        last_billed = admission.admitted_at.date() + timedelta(days=admission.bed_days_charged)
        end = min(through, (admission.discharged_at or utc_now()).date())
        day = last_billed
        while day <= end:
            assignment = await _assignment_on(session, admission.id, day)
            bed_class = assignment.bed_class if assignment else BedClass.GENERAL
            tariff = assignment.tariff_item_code if assignment else None

            await event_bus.publish(
                BedDayAccrued(
                    hospital_id=hospital_id,
                    admission_id=admission.id,
                    encounter_id=admission.encounter_id,
                    patient_id=admission.patient_id,
                    accrual_key=accrual_key(admission.id, day),
                    service_date=day,
                    bed_class=bed_class.value,
                    tariff_item_code=tariff,
                    nights=1,
                    description=(
                        f"{bed_class.value.replace('_', ' ').title()} bed — {day.isoformat()}"
                    ),
                ),
                session=session,
            )
            admission.bed_days_charged += 1
            emitted += 1
            day += timedelta(days=1)
        session.add(admission)

    if emitted:
        await session.flush()
        logger.info("accrued %d bed-day(s) for hospital %s", emitted, hospital_id)
    return {"bed_days_accrued": emitted}


async def _assignment_on(
    session: AsyncSession, admission_id: uuid.UUID, day: date
) -> BedAssignment | None:
    """Where the patient was at the end of `day`."""
    end_of_day = datetime.combine(day, time(23, 59, 59), tzinfo=UTC)
    return (
        (
            await session.execute(
                select(BedAssignment)
                .where(
                    col(BedAssignment.admission_id) == admission_id,
                    col(BedAssignment.assigned_at) <= end_of_day,
                    col(BedAssignment.deleted_at).is_(None),
                )
                .order_by(col(BedAssignment.assigned_at).desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# The discharge summary
# ---------------------------------------------------------------------------
async def get_summary(session: AsyncSession, admission_id: uuid.UUID) -> DischargeSummary | None:
    return (
        (
            await session.execute(
                select(DischargeSummary).where(
                    col(DischargeSummary.admission_id) == admission_id,
                    col(DischargeSummary.status) != SummaryStatus.AMENDED,
                    col(DischargeSummary.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def get_summary_by_id(
    session: AsyncSession, summary_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> DischargeSummary:
    summary = (
        (
            await session.execute(
                select(DischargeSummary).where(
                    col(DischargeSummary.id) == summary_id,
                    col(DischargeSummary.hospital_id) == hospital_id,
                    col(DischargeSummary.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if summary is None:
        raise NotFoundError("Discharge summary not found.", code="summary_not_found")
    return summary


async def compile_summary(
    session: AsyncSession, admission: Admission, *, actor: User | None = None
) -> DischargeSummary:
    """Assemble a draft out of what the hospital already recorded.

    The §7 move, and the reason this module exists in the shape it does: the
    consultant's surface is *review and sign*, not *write*. Every section comes
    from a row somebody already entered — diagnoses and notes from `clinical`,
    reports from `diagnostics`, drugs from the chart here — and nothing is
    invented to fill a gap. A blank section is a blank section, because a
    plausible sentence nobody wrote is worse than an obvious hole.

    Refuses to overwrite a signed summary: that is an amendment, with a reason.
    """
    existing = await get_summary(session, admission.id)
    if existing is not None and existing.is_signed:
        raise ConflictError(
            "This summary is signed. Amend it rather than recompiling.",
            code="summary_signed",
        )

    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=admission.hospital_id
    )
    notes = await clinical_service.list_notes(session, encounter.id)
    diagnoses = await clinical_service.list_diagnoses(session, encounter.id)
    reports, _ = await diagnostics_service.list_reports(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=admission.hospital_id,
        encounter_id=encounter.id,
        status=ReportStatus.FINAL,
    )
    medications, _ = await list_medication_orders(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=admission.hospital_id,
        admission_id=admission.id,
        active_only=False,
    )

    sections = summary_builder.compile_sections(
        notes=notes,
        diagnoses=diagnoses,
        reports=reports,
        medications=medications,
        chief_complaint=encounter.chief_complaint,
    )
    provenance = summary_builder.provenance(
        notes=notes, diagnoses=diagnoses, reports=reports, medications=medications
    )

    summary = existing or DischargeSummary(
        hospital_id=admission.hospital_id,
        admission_id=admission.id,
        encounter_id=encounter.id,
        patient_id=admission.patient_id,
    )
    for field, value in sections.items():
        # Never clobber something a human typed with an empty compiled section.
        if value or not getattr(summary, field, None):
            setattr(summary, field, value or None)
    summary.compiled = {**sections, "_provenance": provenance}
    summary.compiled_at = utc_now()
    summary.status = SummaryStatus.DRAFT

    session.add(summary)
    await session.flush()
    await session.refresh(summary)
    logger.info("compiled discharge summary for %s from %s", admission.admission_number, provenance)
    return summary


async def update_summary(
    session: AsyncSession, summary: DischargeSummary, payload: SummaryUpdate
) -> DischargeSummary:
    if summary.is_signed:
        raise ConflictError(
            "A signed summary cannot be edited. Amend it instead.", code="summary_signed"
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(summary, field, value)
    session.add(summary)
    await session.flush()
    await session.refresh(summary)
    return summary


async def sign_summary(
    session: AsyncSession,
    summary: DischargeSummary,
    *,
    actor: User,
    registration_number: str | None = None,
) -> DischargeSummary:
    """The clinical signature. Freezes the document.

    The signatory's name and registration number are snapshotted onto the row so
    a summary reprinted in five years still names its author correctly and
    carries the number that makes it valid — the same reasoning as a verified
    diagnostic report.
    """
    if summary.is_signed:
        raise ConflictError("This summary is already signed.", code="summary_signed")
    if not summary.diagnoses:
        raise ValidationError(
            "A discharge summary needs at least one diagnosis before it can be signed.",
            code="summary_incomplete",
        )

    summary.status = SummaryStatus.FINAL
    summary.signed_by_id = actor.id
    summary.signed_by_name = actor.full_name
    summary.signatory_registration_number = registration_number
    summary.signed_at = utc_now()
    session.add(summary)
    await session.flush()
    await session.refresh(summary)

    await event_bus.publish(
        DischargeSummarySigned(
            hospital_id=summary.hospital_id,
            actor_id=actor.id,
            summary_id=summary.id,
            admission_id=summary.admission_id,
            patient_id=summary.patient_id,
            signed_by_id=actor.id,
        ),
        session=session,
    )
    return summary


async def list_summaries(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    status: SummaryStatus | None = None,
    patient_id: uuid.UUID | None = None,
) -> tuple[list[DischargeSummary], int]:
    """Also the unsigned worklist — `status=DRAFT` is what a consultant owes."""
    filters: list[ColumnElement[bool]] = [
        col(DischargeSummary.hospital_id) == hospital_id,
        col(DischargeSummary.deleted_at).is_(None),
    ]
    if status is not None:
        filters.append(col(DischargeSummary.status) == status)
    if patient_id is not None:
        filters.append(col(DischargeSummary.patient_id) == patient_id)

    total = (
        await session.execute(select(func.count()).select_from(DischargeSummary).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(DischargeSummary)
                .where(*filters)
                .order_by(col(DischargeSummary.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total
