"""Clinical business logic — the module's public interface.

Every status change in here goes through `state_machine.transition`. There is no
second path, and adding one would break the invariant CLAUDE.md §14 asks us to
guard actively rather than hope for.

The other thing worth reading closely is `pending_items`. CLAUDE.md §6 makes
closure depend on whether anything is still outstanding against a visit, and
this function is the **single place** that question is answered. `diagnostics`
(Phase 6) and `billing` (Phase 7) extend it — they do not each invent their own
notion of "done", because two definitions of done is how a visit ends up closed
with an unpaid bill attached to it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.events import event_bus
from app.core.exceptions import (
    ConflictError,
    IllegalStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.clinical.events import OrderCancelled, OrderPlaced, VitalsRecorded
from app.modules.clinical.models import (
    ClinicalNote,
    Diagnosis,
    Encounter,
    EncounterEvent,
    EncounterSequence,
    EncounterStatus,
    EncounterType,
    NoteTemplate,
    Order,
    OrderStatus,
    OrderType,
    TemplateType,
    Vitals,
)
from app.modules.clinical.rbac import FULFILMENT_ROLES
from app.modules.clinical.schemas import (
    DeathRecord,
    DiagnosisCreate,
    EncounterUpdate,
    LamaRecord,
    NoteCreate,
    NoteTemplateCreate,
    NoteTemplateUpdate,
    OrderCreate,
    PendingItems,
    ReferralRecord,
    SafetyBanner,
    VitalsCreate,
)
from app.modules.clinical.state_machine import (
    open_encounter_event,
    transition,
)
from app.modules.identity.models import User
from app.modules.patients import service as patients_service

logger = logging.getLogger(__name__)

__all__ = [
    "PendingContribution",
    "admit",
    "build_chart",
    "cancel_encounter",
    "cancel_order",
    "close_stale_encounters",
    "complete_consultation",
    "complete_order",
    "discharge",
    "get_encounter",
    "get_encounter_by_appointment",
    "get_encounter_by_number",
    "get_encounters_by_appointments",
    "get_encounters_by_ids",
    "get_note",
    "get_open_encounters_by_patients",
    "get_template",
    "list_encounters",
    "open_encounter",
    "pending_items",
    "place_order",
    "record_death",
    "record_diagnosis",
    "record_lama",
    "record_referral",
    "record_vitals",
    "reevaluate_closure",
    "register_pending_provider",
    "start_consultation",
    "write_note",
]

# Statuses a visit can still move out of. Used by the work lists and by the
# auto-close sweep; derived from the transition table would be circular, so it
# is stated once here and asserted against the table in the tests.
ACTIVE_STATUSES: tuple[EncounterStatus, ...] = (
    EncounterStatus.REGISTERED,
    EncounterStatus.IN_CONSULTATION,
    EncounterStatus.AWAITING_RESULTS,
    EncounterStatus.PENDING_CLEARANCE,
)

_OPEN_ORDER_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.REQUESTED, OrderStatus.IN_PROGRESS)


# ---------------------------------------------------------------------------
# The pending-item extension seam (CLAUDE.md §6)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PendingContribution:
    """One module's answer to "is anything of yours still holding this visit?".

    `descriptions` are read by reception off a screen and said out loud to a
    waiting patient, so they are phrased as sentences, not as record ids.

    `blocks_closure=False` lets a module surface something on the screen
    without preventing the visit closing — an unsettled bill on a corporate
    credit account, say, which finance chases next month rather than at the
    counter.
    """

    source: str
    count: int
    descriptions: tuple[str, ...] = ()
    blocks_closure: bool = True


PendingProvider = Callable[[AsyncSession, Encounter], Awaitable[PendingContribution | None]]

# Registered at import time by the modules that own their own kind of pending
# work. `billing` registers "there is money outstanding on this visit"; `ipd`
# will register "the bed has not been released". Held in a registry rather than
# imported directly so that `clinical` — the core — never has to import a module
# downstream of it, and so a deployment that does not run billing simply has one
# fewer provider rather than a broken import.
_PENDING_PROVIDERS: dict[str, PendingProvider] = {}


def register_pending_provider(source: str, provider: PendingProvider) -> None:
    """Add a module's contribution to the closure gate.

    Idempotent by `source`: importing a module twice under different names (a
    reload, a test harness) must not double-count its pending work.
    """
    _PENDING_PROVIDERS[source] = provider
    logger.debug("registered pending-item provider %s", source)


def clear_pending_providers() -> None:
    """Drop every registered provider. Tests only."""
    _PENDING_PROVIDERS.clear()


async def _contributions(session: AsyncSession, encounter: Encounter) -> list[PendingContribution]:
    found: list[PendingContribution] = []
    for source, provider in _PENDING_PROVIDERS.items():
        contribution = await provider(session, encounter)
        if contribution is not None and contribution.count:
            found.append(contribution)
            logger.debug(
                "pending provider %s reports %d item(s) on %s",
                source,
                contribution.count,
                encounter.encounter_number,
            )
    return found


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
async def _next_encounter_number(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """`ENC-YY-NNNNNN`, unique per hospital per year.

    Upsert-then-lock, the same shape as the UHID and token counters: a plain
    check-then-insert races on the first allocation of the year, which is
    precisely when several counters open at once.
    """
    year = datetime.now(UTC).year
    await session.execute(
        text(
            """
            INSERT INTO encounter_sequences
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
                select(EncounterSequence)
                .where(
                    col(EncounterSequence.hospital_id) == hospital_id,
                    col(EncounterSequence.year) == year,
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
    return f"ENC-{year % 100:02d}-{row.last_value:06d}"


# ---------------------------------------------------------------------------
# Encounters
# ---------------------------------------------------------------------------
async def open_encounter(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    actor: User | None = None,
    appointment_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    encounter_type: EncounterType = EncounterType.OPD,
    chief_complaint: str | None = None,
    triage_note: str | None = None,
) -> Encounter:
    """Open a visit in `REGISTERED` (CLAUDE.md §6).

    Idempotent per appointment: a second check-in on the same booking returns
    the existing chart rather than opening a parallel one. Two open encounters
    for one patient is how orders and charges end up on the wrong visit, and the
    partial unique index on `appointments` refuses it at the database too.
    """
    patient = await patients_service.get_patient(session, patient_id)
    if patient.hospital_id != hospital_id:
        raise NotFoundError("Patient not found.", code="patient_not_found")
    if patient.is_deceased:
        # Not a validation nicety: opening a visit for a dead patient is how a
        # follow-up reminder eventually reaches their family.
        raise ConflictError(
            "This patient is recorded as deceased; a new visit cannot be opened.",
            code="patient_deceased",
        )

    if appointment_id is not None:
        existing = await get_encounter_by_appointment(
            session, appointment_id, hospital_id=hospital_id
        )
        if existing is not None:
            logger.info("reusing live encounter %s for appointment", existing.encounter_number)
            return existing

    moment = utc_now()
    encounter = Encounter(
        hospital_id=hospital_id,
        encounter_number=await _next_encounter_number(session, hospital_id),
        # Follow the merge: a visit opened on a superseded record must land on
        # the surviving chart.
        patient_id=patient.id,
        appointment_id=appointment_id,
        doctor_id=doctor_id,
        department_id=department_id,
        encounter_type=encounter_type,
        status=EncounterStatus.REGISTERED,
        chief_complaint=chief_complaint,
        triage_note=triage_note,
        started_at=moment,
    )
    session.add(encounter)
    await session.flush()
    await session.refresh(encounter)

    await open_encounter_event(session, encounter, actor=actor)
    return encounter


async def get_encounter(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Encounter:
    encounter = (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.id) == encounter_id,
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if encounter is None:
        raise NotFoundError("Visit not found.", code="encounter_not_found")
    return encounter


async def get_encounter_by_number(
    session: AsyncSession, encounter_number: str, *, hospital_id: uuid.UUID
) -> Encounter | None:
    """A visit by the number printed on its paperwork, or None.

    Returns rather than raises: its caller is the universal search box, where
    "no such visit" is an ordinary outcome of typing, not an error. Closed
    visits are included deliberately — somebody holding a discharge slip from
    last week is exactly the person who types a visit number into a search box.
    """
    number = encounter_number.strip().upper()
    if not number:
        return None

    return (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.encounter_number) == number,
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def get_encounter_by_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Encounter | None:
    """The live encounter for a booking, if there is one."""
    return (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.appointment_id) == appointment_id,
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                    col(Encounter.status).in_((*ACTIVE_STATUSES, EncounterStatus.ADMITTED)),
                )
            )
        )
        .scalars()
        .first()
    )


async def record_seen_by_staff(
    session: AsyncSession, encounter: Encounter, *, actor: User | None
) -> Encounter:
    """Staff report that the doctor has seen this patient.

    Moves a visit that is still `REGISTERED` into `IN_CONSULTATION` through the
    state machine — the only way a status changes — with the receptionist as
    the actor and a reason saying so, so the timeline and audit trail show who
    reported it rather than implying a doctor opened the chart.

    Without this the end-of-day safety net (`close_stale_encounters`) would
    close the visit as NO_SHOW: never reached a consultation. That would record
    a patient reception watched walk out of the doctor's room as having never
    been seen — in footfall, in follow-up compliance, everywhere.

    It stops there. Completing the visit weighs pending labs, medicines and
    bills (CLAUDE.md §6), and that stays with the doctor's "complete visit" or
    the auto-close, which will close it as COMPLETED once nothing is pending.
    A visit already past `REGISTERED` is left exactly as it is.
    """
    if encounter.status is not EncounterStatus.REGISTERED:
        return encounter
    return await transition(
        session,
        encounter,
        EncounterStatus.IN_CONSULTATION,
        actor=actor,
        reason="Marked as seen by reception.",
    )


async def reassign_doctor(
    session: AsyncSession,
    encounter: Encounter,
    *,
    doctor_id: uuid.UUID,
    department_id: uuid.UUID | None,
) -> Encounter:
    """Point an open visit at a different doctor.

    Called by `scheduling` when reception moves a token between queues — the
    chart has to follow, or the new doctor's worklist never shows the patient
    and the old doctor's never stops. Refused once a consultation has begun:
    the note that is being written belongs to the doctor writing it, and a
    hand-over mid-visit is a referral, not a queue adjustment.

    Not a status change, so it does not go through the state machine and
    writes no `EncounterEvent`; the caller records it in the audit log.
    """
    if encounter.status is not EncounterStatus.REGISTERED:
        raise ConflictError(
            "The consultation has already started; the visit cannot be moved.",
            code="encounter_not_reassignable",
            details={"status": encounter.status.value},
        )
    encounter.doctor_id = doctor_id
    encounter.department_id = department_id
    encounter.updated_at = utc_now()
    session.add(encounter)
    await session.flush()
    return encounter


async def get_encounters_by_appointments(
    session: AsyncSession,
    appointment_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
) -> dict[uuid.UUID, Encounter]:
    """The live encounters for many bookings at once, keyed by appointment.

    The bulk form of `get_encounter_by_appointment`, and it exists for the
    same reason `patients.get_patients_by_ids` does: the OPD queue board
    renders one row per waiting patient, each row needs the encounter id to
    link into the consultation screen, and asking one row at a time makes the
    most-refreshed screen in the hospital scale with the length of the queue.

    Only live encounters are returned — the same filter the singular form
    uses. A closed visit against yesterday's appointment is not what a doctor
    clicking a waiting patient means.
    """
    if not appointment_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.appointment_id).in_(set(appointment_ids)),
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                    col(Encounter.status).in_((*ACTIVE_STATUSES, EncounterStatus.ADMITTED)),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        encounter.appointment_id: encounter
        for encounter in rows
        if encounter.appointment_id is not None
    }


async def get_open_encounters_by_patients(
    session: AsyncSession,
    patient_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
) -> dict[uuid.UUID, Encounter]:
    """Whether each of these patients is in the middle of a visit right now.

    One query for the whole page, keyed by patient. Built for the search box:
    a receptionist who scans a card and cannot see that the patient is already
    in a visit today will register them a second time, and a split visit is a
    split bill and a chart in two halves.

    `ADMITTED` counts as open, and deliberately: a patient in a bed is very
    much in the middle of something, and it is the case where registering them
    again does the most damage.

    Newest first, so a patient with more than one open visit — rare, and a
    data-entry error when it happens — resolves to the one somebody is most
    likely working on.
    """
    if not patient_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(Encounter)
                .where(
                    col(Encounter.patient_id).in_(set(patient_ids)),
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                    col(Encounter.status).in_((*ACTIVE_STATUSES, EncounterStatus.ADMITTED)),
                )
                .order_by(col(Encounter.started_at).desc())
            )
        )
        .scalars()
        .all()
    )

    open_visits: dict[uuid.UUID, Encounter] = {}
    for encounter in rows:
        # `setdefault`, not assignment: the rows arrive newest first and the
        # first one seen per patient is the one to keep.
        open_visits.setdefault(encounter.patient_id, encounter)
    return open_visits


async def get_encounters_by_ids(
    session: AsyncSession,
    encounter_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
) -> dict[uuid.UUID, Encounter]:
    """Many visits at once, keyed by id — the bulk form of `get_encounter`.

    Unlike `get_encounters_by_appointments` this does **not** filter to live
    visits, and that is the point of having both. Its caller is the cash
    counter's board, and a bill left behind by a death or a discharge against
    advice belongs on that board precisely because the visit is closed
    (CLAUDE.md §6). Hiding closed visits here would hide the money.
    """
    if not encounter_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.id).in_(set(encounter_ids)),
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {encounter.id: encounter for encounter in rows}


async def list_encounters(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
    status: EncounterStatus | None = None,
    active_only: bool = False,
    on_date: date | None = None,
) -> tuple[list[Encounter], int]:
    filters: list[ColumnElement[bool]] = [
        col(Encounter.hospital_id) == hospital_id,
        col(Encounter.deleted_at).is_(None),
    ]
    if patient_id is not None:
        filters.append(col(Encounter.patient_id) == patient_id)
    if doctor_id is not None:
        filters.append(col(Encounter.doctor_id) == doctor_id)
    if status is not None:
        filters.append(col(Encounter.status) == status)
    if active_only:
        filters.append(col(Encounter.status).in_(ACTIVE_STATUSES))
    if on_date is not None:
        day_start = datetime.combine(on_date, datetime.min.time(), tzinfo=UTC)
        filters.append(col(Encounter.started_at) >= day_start)
        filters.append(col(Encounter.started_at) < day_start + timedelta(days=1))

    total = (
        await session.execute(select(func.count()).select_from(Encounter).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Encounter)
                .where(*filters)
                .order_by(col(Encounter.started_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_encounter(
    session: AsyncSession, encounter: Encounter, payload: EncounterUpdate
) -> Encounter:
    """Edit the non-status fields. Status is not among them, by design."""
    if encounter.is_closed:
        raise ConflictError(
            "This visit is closed and cannot be edited.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(encounter, field, value)
    encounter.updated_at = utc_now()
    session.add(encounter)
    await session.flush()
    await session.refresh(encounter)
    return encounter


async def start_consultation(
    session: AsyncSession, encounter: Encounter, *, actor: User | None = None
) -> Encounter:
    """The doctor has the patient in front of them."""
    return await transition(session, encounter, EncounterStatus.IN_CONSULTATION, actor=actor)


async def complete_consultation(
    session: AsyncSession,
    encounter: Encounter,
    *,
    actor: User | None = None,
    follow_up_date: date | None = None,
    follow_up_instructions: str | None = None,
    reason: str | None = None,
) -> Encounter:
    """The doctor's "I am finished" action — the key rule of CLAUDE.md §6.

    Where the visit goes is decided by what is still open against it, never by
    the doctor picking a status:

    * nothing outstanding                       -> `COMPLETED`
    * something the doctor will review today    -> `AWAITING_RESULTS`
    * something to be cleared at a counter      -> `PENDING_CLEARANCE`

    The patient does not have to walk back to reception for any of this, which
    is the entire reason the state machine exists rather than a "closed" flag
    someone remembers to tick.
    """
    if encounter.is_closed:
        raise ConflictError(
            "This visit is already closed.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )

    moment = utc_now()
    encounter.consultation_completed_at = moment
    if follow_up_date is not None:
        encounter.follow_up_date = follow_up_date
    if follow_up_instructions is not None:
        encounter.follow_up_instructions = follow_up_instructions
    encounter.updated_at = moment
    session.add(encounter)

    open_orders = await _open_orders(session, encounter.id)
    blocking = [order for order in open_orders if order.blocks_closure]
    awaiting_review = [order for order in blocking if order.review_in_visit]
    # Money counts as a pending item too — the counter is one of the clearance
    # stations CLAUDE.md §6 names explicitly.
    other_blocking = sum(
        contribution.count
        for contribution in await _contributions(session, encounter)
        if contribution.blocks_closure
    )

    if awaiting_review:
        target = EncounterStatus.AWAITING_RESULTS
    elif blocking or other_blocking:
        target = EncounterStatus.PENDING_CLEARANCE
    else:
        target = EncounterStatus.COMPLETED

    if target is encounter.status:
        # The doctor said "finished" twice while the same items are still open.
        # Nothing has changed, so there is nothing to record — and re-running the
        # transition would raise "the visit is already awaiting results", which
        # is a true statement and a useless error.
        await session.flush()
        await session.refresh(encounter)
        return encounter

    return await transition(
        session,
        encounter,
        target,
        actor=actor,
        reason=reason,
        metadata={
            "pending_orders": len(blocking),
            "awaiting_review": len(awaiting_review),
            "pending_other": other_blocking,
        },
    )


async def admit(
    session: AsyncSession,
    encounter: Encounter,
    *,
    actor: User | None = None,
    reason: str | None = None,
    department_id: uuid.UUID | None = None,
) -> Encounter:
    """Convert the visit to an inpatient stay. `ipd` takes it from here."""
    if department_id is not None:
        encounter.department_id = department_id
        session.add(encounter)
    return await transition(
        session, encounter, EncounterStatus.ADMITTED, actor=actor, reason=reason
    )


async def discharge(
    session: AsyncSession,
    encounter: Encounter,
    *,
    actor: User | None = None,
    reason: str | None = None,
) -> Encounter:
    """Close an inpatient stay: `ADMITTED` -> `COMPLETED`.

    The counterpart to `admit`, and it exists here rather than in `ipd` for the
    reason CLAUDE.md §14 gives: `Encounter.status` is only ever changed by the
    state machine, and the state machine belongs to this module. `ipd` owns the
    bed, the summary and the paperwork, and calls this for the one thing it must
    not do itself.

    A death or a self-discharge on the ward does **not** come through here —
    those are `record_death` and `record_lama`, which capture the structured
    metadata CLAUDE.md §6 requires and are reachable directly from `ADMITTED`.
    """
    if encounter.status is not EncounterStatus.ADMITTED:
        raise IllegalStateTransitionError(
            "Only an admitted patient can be discharged.",
            details={"from": encounter.status.value, "to": EncounterStatus.COMPLETED.value},
        )
    return await transition(
        session,
        encounter,
        EncounterStatus.COMPLETED,
        actor=actor,
        reason=reason or "Discharged from the ward.",
    )


async def cancel_encounter(
    session: AsyncSession,
    encounter: Encounter,
    *,
    reason: str,
    no_show: bool = False,
    actor: User | None = None,
) -> Encounter:
    """Call the visit off, or record that the patient never arrived."""
    target = EncounterStatus.NO_SHOW if no_show else EncounterStatus.CANCELLED
    return await transition(
        session,
        encounter,
        target,
        actor=actor,
        reason=reason,
        metadata={"cancellation_reason": reason},
    )


# --- the three edge cases (CLAUDE.md §6) -----------------------------------
async def record_death(
    session: AsyncSession,
    encounter: Encounter,
    payload: DeathRecord,
    *,
    actor: User | None = None,
) -> Encounter:
    """Record a death.

    The transition flags the patient record in the same transaction, which is
    what makes "never notify a deceased patient" (CLAUDE.md §14) an invariant
    rather than a convention. It also emits `EncounterClosed` with
    `requires_settlement=True` — a deceased patient usually still has a bill,
    and it must be settled with the family, not silently written off.
    """
    return await transition(
        session,
        encounter,
        EncounterStatus.DECEASED,
        actor=actor,
        reason=payload.reason,
        metadata={
            "deceased_at": payload.deceased_at,
            "death_certified_by_id": payload.death_certified_by_id,
            "cause_of_death": payload.cause_of_death,
            "death_place": payload.death_place,
        },
    )


async def record_referral(
    session: AsyncSession,
    encounter: Encounter,
    payload: ReferralRecord,
    *,
    actor: User | None = None,
) -> Encounter:
    return await transition(
        session,
        encounter,
        EncounterStatus.REFERRED_OUT,
        actor=actor,
        reason=payload.referral_reason,
        metadata={
            "referred_to_facility": payload.referred_to_facility,
            "referral_reason": payload.referral_reason,
            "referral_transport": payload.referral_transport,
            "referred_at": payload.referred_at,
        },
    )


async def record_lama(
    session: AsyncSession,
    encounter: Encounter,
    payload: LamaRecord,
    *,
    actor: User | None = None,
) -> Encounter:
    """Left against medical advice, or absconded.

    Terminal, and still billable: the patient consumed care before walking out.
    """
    return await transition(
        session,
        encounter,
        EncounterStatus.LAMA,
        actor=actor,
        reason=payload.lama_reason,
        metadata={
            "lama_reason": payload.lama_reason,
            "lama_form_signed": payload.lama_form_signed,
            "lama_at": payload.lama_at,
        },
    )


# ---------------------------------------------------------------------------
# Pending items — the closure gate (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def _open_orders(session: AsyncSession, encounter_id: uuid.UUID) -> list[Order]:
    rows = (
        (
            await session.execute(
                select(Order).where(
                    col(Order.encounter_id) == encounter_id,
                    col(Order.status).in_(_OPEN_ORDER_STATUSES),
                    col(Order.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def pending_items(session: AsyncSession, encounter: Encounter) -> PendingItems:
    """Everything still holding this visit open.

    **This is the extension seam**, and as of Phase 7 it is genuinely extended
    rather than merely advertised as extensible. The clinical answer is "open
    orders"; `billing` contributes "there is money outstanding", and `ipd` will
    contribute "the bed has not been released". They register a provider; they
    do not each grow their own idea of whether a visit is finished, because two
    definitions of done is how a visit gets closed with an unpaid bill still
    attached to it.
    """
    orders = [order for order in await _open_orders(session, encounter.id) if order.blocks_closure]

    by_type: dict[str, int] = {}
    for order in orders:
        by_type[order.order_type.value] = by_type.get(order.order_type.value, 0) + 1

    # Phrased for the screen: reception needs to tell a waiting patient what is
    # left, not read an order id back to them.
    descriptions = [f"{order.order_type.value.title()}: {order.item_name}" for order in orders]

    total = len(orders)
    for contribution in await _contributions(session, encounter):
        by_type[contribution.source] = by_type.get(contribution.source, 0) + contribution.count
        descriptions.extend(contribution.descriptions)
        total += contribution.count

    return PendingItems(
        total=total,
        awaiting_review=sum(1 for order in orders if order.review_in_visit),
        by_type=by_type,
        descriptions=descriptions,
    )


async def _blocking_count(session: AsyncSession, encounter: Encounter) -> int:
    """How many things — from any module — still prevent this visit closing.

    The single question every closure decision asks. Kept in one function so
    `complete_consultation`, the clearance advance and the auto-close sweep can
    never disagree about what "nothing outstanding" means.
    """
    blocking = sum(1 for order in await _open_orders(session, encounter.id) if order.blocks_closure)
    blocking += sum(
        contribution.count
        for contribution in await _contributions(session, encounter)
        if contribution.blocks_closure
    )
    return blocking


async def reevaluate_closure(
    session: AsyncSession, encounter: Encounter, *, actor: User | None = None
) -> Encounter:
    """Move a visit on when the last thing holding it open is cleared.

    CLAUDE.md §6: the visit becomes `COMPLETED` when the final pending item is
    cleared by the relevant staff — the cashier, the lab tech, the pharmacist —
    with nobody having to remember to go back and close it.

    Public because clearing a pending item is not something only `clinical` can
    do. `diagnostics` clears an order by verifying a report; `billing` clears
    one by taking the last rupee. Both call this directly rather than publishing
    an event, for the reason set out in `diagnostics/service.py`: a swallowed
    handler would leave the visit open forever with its work already done.
    """
    if encounter.is_closed:
        return encounter

    open_blocking = [
        order for order in await _open_orders(session, encounter.id) if order.blocks_closure
    ]

    if encounter.status is EncounterStatus.AWAITING_RESULTS:
        if any(order.review_in_visit for order in open_blocking):
            return encounter
        # Results are in and the doctor asked to see them. Back onto their
        # active list rather than closed — the visit is not over until they say.
        return await transition(
            session,
            encounter,
            EncounterStatus.IN_CONSULTATION,
            actor=actor,
            reason="Results available for review.",
        )

    if encounter.status is EncounterStatus.PENDING_CLEARANCE:
        if open_blocking or await _blocking_count(session, encounter):
            return encounter
        return await transition(
            session,
            encounter,
            EncounterStatus.COMPLETED,
            actor=actor,
            reason="Last pending item cleared.",
        )

    return encounter


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
async def place_order(
    session: AsyncSession,
    encounter: Encounter,
    payload: OrderCreate,
    *,
    actor: User,
) -> Order:
    if encounter.is_closed:
        raise ConflictError(
            "This visit is closed; orders cannot be added.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )

    order = Order(
        hospital_id=encounter.hospital_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        ordered_by_id=actor.id,
        ordered_at=utc_now(),
        **payload.model_dump(),
    )
    session.add(order)
    await session.flush()
    await session.refresh(order)

    await event_bus.publish(
        OrderPlaced(
            hospital_id=encounter.hospital_id,
            actor_id=actor.id,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            order_id=order.id,
            order_type=order.order_type.value,
            item_code=order.item_code,
            item_name=order.item_name,
            priority=order.priority.value,
        ),
        session=session,
    )
    return order


async def get_order(session: AsyncSession, order_id: uuid.UUID, *, hospital_id: uuid.UUID) -> Order:
    order = (
        (
            await session.execute(
                select(Order).where(
                    col(Order.id) == order_id,
                    col(Order.hospital_id) == hospital_id,
                    col(Order.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if order is None:
        raise NotFoundError("Order not found.", code="order_not_found")
    return order


async def list_orders(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID | None = None,
    order_type: OrderType | None = None,
    order_types: Collection[OrderType] | None = None,
    status: OrderStatus | None = None,
    open_only: bool = False,
) -> tuple[list[Order], int]:
    """The lab's, radiology's and pharmacy's work lists all come from here.

    `order_types` is the plural of `order_type`, for a desk that works more than
    one kind. The diagnostics bench is the case in point: one technician handles
    both lab and radiology requests, and paging two lists separately would show
    them a routine X-ray above a STAT troponin — the priority ordering only
    means anything across the whole set.
    """
    filters: list[ColumnElement[bool]] = [
        col(Order.hospital_id) == hospital_id,
        col(Order.deleted_at).is_(None),
    ]
    if encounter_id is not None:
        filters.append(col(Order.encounter_id) == encounter_id)
    if order_type is not None:
        filters.append(col(Order.order_type) == order_type)
    if order_types is not None:
        filters.append(col(Order.order_type).in_(tuple(order_types)))
    if status is not None:
        filters.append(col(Order.status) == status)
    if open_only:
        filters.append(col(Order.status).in_(_OPEN_ORDER_STATUSES))

    total = (
        await session.execute(select(func.count()).select_from(Order).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Order)
                # STAT before urgent before routine, then oldest first. A work
                # list in insertion order is a work list nobody trusts.
                .where(*filters)
                .order_by(col(Order.priority), col(Order.ordered_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


def assert_may_fulfil(order: Order, roles: frozenset[str]) -> None:
    """Second gate on fulfilment: the right *kind* of staff, not just any staff.

    `order:fulfil` says a user may clear orders at all. This says which. Without
    it the permission would let a cashier mark a blood test resulted — obvious
    in hindsight, invisible in a permission matrix.
    """
    allowed = FULFILMENT_ROLES.get(order.order_type, frozenset())
    if not (roles & allowed):
        raise PermissionDeniedError(
            f"{order.order_type.value.title()} orders are cleared by "
            f"{', '.join(sorted(role.replace('_', ' ').title() for role in allowed))}.",
            code="wrong_fulfilment_role",
            details={"order_type": order.order_type.value, "allowed_roles": sorted(allowed)},
        )


async def start_order(session: AsyncSession, order: Order, *, actor: User) -> Order:
    """Sample taken, patient in the scanner, prescription being dispensed."""
    if order.status is not OrderStatus.REQUESTED:
        raise ConflictError(
            f"This order is already {order.status.value.lower().replace('_', ' ')}.",
            code="order_not_open",
        )
    order.status = OrderStatus.IN_PROGRESS
    order.started_at = utc_now()
    order.updated_at = utc_now()
    session.add(order)
    await session.flush()
    await session.refresh(order)
    return order


async def complete_order(
    session: AsyncSession,
    order: Order,
    *,
    actor: User,
    hospital_id: uuid.UUID,
    fulfilment_ref: str | None = None,
) -> tuple[Order, Encounter]:
    """Clear an order and let the visit move on if it was the last one.

    Returns the encounter too, because the caller almost always wants to tell
    the user what just happened to the visit — "that was the last item, the
    visit is now closed" is the message that stops people chasing it.
    """
    if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        raise ConflictError(
            f"This order is already {order.status.value.lower()}.", code="order_closed"
        )

    order.status = OrderStatus.COMPLETED
    order.completed_at = utc_now()
    order.completed_by_id = actor.id
    if fulfilment_ref:
        order.fulfilment_ref = fulfilment_ref
    order.updated_at = utc_now()
    session.add(order)
    await session.flush()

    encounter = await get_encounter(session, order.encounter_id, hospital_id=hospital_id)
    encounter = await reevaluate_closure(session, encounter, actor=actor)
    await session.refresh(order)
    return order, encounter


async def cancel_order(
    session: AsyncSession,
    order: Order,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> tuple[Order, Encounter]:
    """Withdraw an order. A cancelled order stops holding the visit open."""
    if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        raise ConflictError(
            f"This order is already {order.status.value.lower()}.", code="order_closed"
        )

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utc_now()
    order.cancellation_reason = reason
    order.updated_at = utc_now()
    session.add(order)
    await session.flush()

    await event_bus.publish(
        OrderCancelled(
            hospital_id=order.hospital_id,
            actor_id=actor.id,
            encounter_id=order.encounter_id,
            patient_id=order.patient_id,
            order_id=order.id,
            order_type=order.order_type.value,
            reason=reason,
        ),
        session=session,
    )

    encounter = await get_encounter(session, order.encounter_id, hospital_id=hospital_id)
    encounter = await reevaluate_closure(session, encounter, actor=actor)
    await session.refresh(order)
    return order, encounter


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------
# Adult thresholds for the "abnormal" flag. Deliberately wide: this drives a
# highlight on a screen, not a clinical decision, and a flag that fires on every
# second patient is a flag everybody learns to ignore. Paediatric and obstetric
# ranges differ enough that they need their own table, not a fudge of this one.
_ABNORMAL_ADULT: dict[str, tuple[float, float]] = {
    "temperature_c": (35.0, 37.8),
    "pulse_bpm": (50, 110),
    "respiratory_rate": (10, 24),
    "systolic_bp": (90, 160),
    "diastolic_bp": (55, 100),
    "spo2_percent": (94, 100),
    "blood_glucose_mgdl": (60, 200),
}


def _is_abnormal(values: dict[str, Any]) -> bool:
    for field, (low, high) in _ABNORMAL_ADULT.items():
        value = values.get(field)
        if value is None:
            continue
        if not (Decimal(str(low)) <= Decimal(str(value)) <= Decimal(str(high))):
            return True
    # Moderate pain and above is worth a clinician's eye.
    score = values.get("pain_score")
    return score is not None and int(score) >= 4


def _bmi(height_cm: Decimal | None, weight_kg: Decimal | None) -> Decimal | None:
    if not height_cm or not weight_kg or height_cm <= 0:
        return None
    metres = height_cm / Decimal(100)
    return (weight_kg / (metres * metres)).quantize(Decimal("0.1"))


async def record_vitals(
    session: AsyncSession,
    encounter: Encounter,
    payload: VitalsCreate,
    *,
    actor: User | None = None,
) -> Vitals:
    """One-click vitals entry (CLAUDE.md §7b: a nurse action in under 15s)."""
    if encounter.is_closed:
        raise ConflictError(
            "This visit is closed; vitals cannot be added.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )

    values = payload.model_dump(exclude={"recorded_at"})
    vitals = Vitals(
        hospital_id=encounter.hospital_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        recorded_by_id=actor.id if actor else None,
        recorded_at=payload.recorded_at or utc_now(),
        bmi=_bmi(payload.height_cm, payload.weight_kg),
        is_abnormal=_is_abnormal(values),
        **values,
    )
    session.add(vitals)
    await session.flush()
    await session.refresh(vitals)

    await event_bus.publish(
        VitalsRecorded(
            hospital_id=encounter.hospital_id,
            actor_id=actor.id if actor else None,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            vitals_id=vitals.id,
            is_abnormal=vitals.is_abnormal,
        ),
        session=session,
    )
    return vitals


async def list_vitals(session: AsyncSession, encounter_id: uuid.UUID) -> list[Vitals]:
    """Newest first — the trend is read from the top of the screen down."""
    rows = (
        (
            await session.execute(
                select(Vitals)
                .where(
                    col(Vitals.encounter_id) == encounter_id,
                    col(Vitals.deleted_at).is_(None),
                )
                .order_by(col(Vitals.recorded_at).desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Clinical notes
# ---------------------------------------------------------------------------
async def write_note(
    session: AsyncSession,
    encounter: Encounter,
    payload: NoteCreate,
    *,
    actor: User,
) -> ClinicalNote:
    """Write (and usually sign) a note.

    A signed note is never edited. An amendment is a new note pointing at the
    original — which is what makes the record defensible, and what a court would
    expect to see.
    """
    if encounter.is_closed:
        raise ConflictError(
            "This visit is closed; notes cannot be added.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )

    if payload.amends_id is not None:
        original = await get_note(session, payload.amends_id, hospital_id=encounter.hospital_id)
        if original.encounter_id != encounter.id:
            raise ValidationError(
                "A note can only amend another note on the same visit.",
                code="amendment_cross_encounter",
            )

    moment = utc_now()
    note = ClinicalNote(
        hospital_id=encounter.hospital_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        note_type=payload.note_type,
        content=payload.content.strip(),
        authored_by_id=actor.id,
        author_name=actor.full_name,
        template_id=payload.template_id,
        amends_id=payload.amends_id,
        is_signed=payload.sign,
        signed_at=moment if payload.sign else None,
    )
    session.add(note)

    if payload.template_id is not None:
        # Sorts the picker by what clinicians actually reach for, which is how
        # the library stays short enough to stay useful.
        template = await get_template(
            session, payload.template_id, hospital_id=encounter.hospital_id
        )
        template.usage_count += 1
        session.add(template)

    await session.flush()
    await session.refresh(note)
    return note


async def get_note(
    session: AsyncSession, note_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> ClinicalNote:
    note = (
        (
            await session.execute(
                select(ClinicalNote).where(
                    col(ClinicalNote.id) == note_id,
                    col(ClinicalNote.hospital_id) == hospital_id,
                    col(ClinicalNote.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if note is None:
        raise NotFoundError("Note not found.", code="note_not_found")
    return note


async def sign_note(session: AsyncSession, note: ClinicalNote, *, actor: User) -> ClinicalNote:
    """e-Sign a draft. Only its author may sign it."""
    if note.is_signed:
        raise ConflictError("This note is already signed.", code="note_already_signed")
    if note.authored_by_id != actor.id:
        # An e-signature that anyone can apply on anyone's behalf is not a
        # signature; it is a timestamp with someone else's name on it.
        raise PermissionDeniedError(
            "A note can only be signed by the clinician who wrote it.",
            code="not_note_author",
        )
    note.is_signed = True
    note.signed_at = utc_now()
    note.updated_at = utc_now()
    session.add(note)
    await session.flush()
    await session.refresh(note)
    return note


async def list_notes(session: AsyncSession, encounter_id: uuid.UUID) -> list[ClinicalNote]:
    rows = (
        (
            await session.execute(
                select(ClinicalNote)
                .where(
                    col(ClinicalNote.encounter_id) == encounter_id,
                    col(ClinicalNote.deleted_at).is_(None),
                )
                .order_by(col(ClinicalNote.created_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Templates (CLAUDE.md §7b — the fastest note is one already written)
# ---------------------------------------------------------------------------
async def create_template(
    session: AsyncSession,
    payload: NoteTemplateCreate,
    *,
    hospital_id: uuid.UUID,
    actor: User,
) -> NoteTemplate:
    template = NoteTemplate(
        hospital_id=hospital_id,
        # NULL owner = hospital-wide. A doctor's own shortcuts stay theirs.
        owner_id=None if payload.shared else actor.id,
        department_id=payload.department_id,
        template_type=payload.template_type,
        note_type=payload.note_type,
        title=payload.title.strip(),
        body=payload.body.strip(),
    )
    session.add(template)
    await session.flush()
    await session.refresh(template)
    return template


async def get_template(
    session: AsyncSession, template_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> NoteTemplate:
    template = (
        (
            await session.execute(
                select(NoteTemplate).where(
                    col(NoteTemplate.id) == template_id,
                    col(NoteTemplate.hospital_id) == hospital_id,
                    col(NoteTemplate.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if template is None:
        raise NotFoundError("Template not found.", code="template_not_found")
    return template


async def list_templates(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    owner_id: uuid.UUID | None = None,
    template_type: TemplateType | None = None,
) -> list[NoteTemplate]:
    """A clinician's own templates plus the hospital's shared ones, most-used first."""
    filters: list[ColumnElement[bool]] = [
        col(NoteTemplate.hospital_id) == hospital_id,
        col(NoteTemplate.deleted_at).is_(None),
        col(NoteTemplate.is_active).is_(True),
    ]
    if owner_id is not None:
        filters.append(
            col(NoteTemplate.owner_id).is_(None) | (col(NoteTemplate.owner_id) == owner_id)
        )
    if template_type is not None:
        filters.append(col(NoteTemplate.template_type) == template_type)

    rows = (
        (
            await session.execute(
                select(NoteTemplate)
                .where(*filters)
                .order_by(col(NoteTemplate.usage_count).desc(), col(NoteTemplate.title))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def update_template(
    session: AsyncSession, template: NoteTemplate, payload: NoteTemplateUpdate
) -> NoteTemplate:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    template.updated_at = utc_now()
    session.add(template)
    await session.flush()
    await session.refresh(template)
    return template


# ---------------------------------------------------------------------------
# Diagnoses
# ---------------------------------------------------------------------------
async def record_diagnosis(
    session: AsyncSession,
    encounter: Encounter,
    payload: DiagnosisCreate,
    *,
    actor: User,
) -> Diagnosis:
    """Record a diagnosis, keeping "exactly one primary" true.

    Enforced here rather than by a constraint because the correction — "no,
    *that* one is primary" — has to be a single call. A unique index would force
    the caller into delete-then-insert, which is a sequence a user can leave
    half finished.
    """
    if encounter.is_closed:
        raise ConflictError(
            "This visit is closed; a diagnosis cannot be added.",
            code="encounter_closed",
            details={"status": encounter.status.value},
        )

    if payload.is_primary:
        for existing in await list_diagnoses(session, encounter.id):
            if existing.is_primary:
                existing.is_primary = False
                existing.updated_at = utc_now()
                session.add(existing)

    diagnosis = Diagnosis(
        hospital_id=encounter.hospital_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        diagnosed_by_id=actor.id,
        diagnosed_at=utc_now(),
        **payload.model_dump(),
    )
    session.add(diagnosis)
    await session.flush()
    await session.refresh(diagnosis)
    return diagnosis


async def list_diagnoses(session: AsyncSession, encounter_id: uuid.UUID) -> list[Diagnosis]:
    rows = (
        (
            await session.execute(
                select(Diagnosis)
                .where(
                    col(Diagnosis.encounter_id) == encounter_id,
                    col(Diagnosis.deleted_at).is_(None),
                )
                .order_by(col(Diagnosis.is_primary).desc(), col(Diagnosis.diagnosed_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Timeline and chart (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
async def get_timeline(session: AsyncSession, encounter_id: uuid.UUID) -> list[EncounterEvent]:
    rows = (
        (
            await session.execute(
                select(EncounterEvent)
                .where(
                    col(EncounterEvent.encounter_id) == encounter_id,
                    col(EncounterEvent.deleted_at).is_(None),
                )
                .order_by(col(EncounterEvent.occurred_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_patient_timeline(
    session: AsyncSession,
    params: PageParams,
    *,
    patient_id: uuid.UUID,
    hospital_id: uuid.UUID,
) -> tuple[list[EncounterEvent], int]:
    """Registration -> OPD -> lab -> admission -> discharge, in order.

    Paginated like every other list (CLAUDE.md §11): a patient with a chronic
    condition accumulates thousands of these.
    """
    filters: list[ColumnElement[bool]] = [
        col(EncounterEvent.patient_id) == patient_id,
        col(EncounterEvent.hospital_id) == hospital_id,
        col(EncounterEvent.deleted_at).is_(None),
    ]
    total = (
        await session.execute(select(func.count()).select_from(EncounterEvent).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(EncounterEvent)
                .where(*filters)
                .order_by(col(EncounterEvent.occurred_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


def _age_years(birth_date: date | None, *, today: date | None = None) -> int | None:
    if birth_date is None:
        return None
    reference = today or utc_now().date()
    years = reference.year - birth_date.year
    if (reference.month, reference.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(years, 0)


async def build_safety_banner(session: AsyncSession, patient_id: uuid.UUID) -> SafetyBanner:
    """The persistent patient-safety header (CLAUDE.md §7b).

    Patient facts come through `patients.service`, never by selecting from that
    module's tables (CLAUDE.md §2).
    """
    patient = await patients_service.get_patient(session, patient_id)
    alerts = await patients_service.get_patient_alerts(session, patient.id)

    return SafetyBanner(
        patient_id=patient.id,
        uhid=patient.uhid,
        full_name=patient.full_name,
        age_years=_age_years(patient.birth_date),
        gender=patient.gender.value,
        blood_group=patient.blood_group.value,
        is_deceased=patient.is_deceased,
        alerts=patients_service.active_alert_summary(alerts),
        has_critical_alert=any(alert.severity.value == "CRITICAL" for alert in alerts),
    )


async def build_chart(session: AsyncSession, encounter: Encounter) -> dict[str, Any]:
    """Everything the consultation screen needs, in one round trip.

    Assembled server-side on purpose: the doctor's screen opening in one request
    rather than six is most of the difference between hitting the §7b 60-second
    target and missing it.
    """
    orders, _ = await list_orders(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=encounter.hospital_id,
        encounter_id=encounter.id,
    )
    return {
        "encounter": encounter,
        "banner": await build_safety_banner(session, encounter.patient_id),
        "vitals": await list_vitals(session, encounter.id),
        "notes": await list_notes(session, encounter.id),
        "diagnoses": await list_diagnoses(session, encounter.id),
        "orders": orders,
        "pending": await pending_items(session, encounter),
    }


# ---------------------------------------------------------------------------
# Auto-close safety net (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def close_stale_encounters(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    older_than: timedelta,
    now: datetime | None = None,
) -> dict[str, int]:
    """Close visits that a forgotten click left open. Returns what it did.

    CLAUDE.md §6 asks for a safety net so a missed click does not leave an
    encounter open forever. Two details make this net honest rather than tidy:

    * A visit that never reached a consultation is closed as **NO_SHOW**, not
      COMPLETED. Recording a patient as seen because a job ran at midnight would
      be a fabricated clinical fact, and it would corrupt every footfall number
      built on top of it.
    * A visit with pending items is **left open** and counted. Those need a
      person — a lab result to enter, a bill to settle — and closing them would
      hide exactly the work this net exists to surface.

    `ADMITTED` is excluded entirely: an inpatient on day five is not stale.
    """
    moment = now or utc_now()
    cutoff = moment - older_than

    stale = (
        (
            await session.execute(
                select(Encounter).where(
                    col(Encounter.hospital_id) == hospital_id,
                    col(Encounter.status).in_(ACTIVE_STATUSES),
                    col(Encounter.started_at) < cutoff,
                    col(Encounter.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    closed = 0
    no_shows = 0
    held = 0
    for encounter in stale:
        # Asks every module, not just this one: a visit whose only outstanding
        # item is an unpaid bill is exactly the case this net exists to surface,
        # and closing it would hide the money.
        if await _blocking_count(session, encounter):
            held += 1
            continue

        if encounter.status is EncounterStatus.REGISTERED:
            await transition(
                session,
                encounter,
                EncounterStatus.NO_SHOW,
                actor=None,
                reason="Automatically closed: never seen.",
                automatic=True,
                metadata={"cancellation_reason": "Auto-closed: consultation never started."},
            )
            no_shows += 1
        else:
            await transition(
                session,
                encounter,
                EncounterStatus.COMPLETED,
                actor=None,
                reason="Automatically closed: nothing outstanding.",
                automatic=True,
            )
            closed += 1

    if closed or no_shows or held:
        logger.info(
            "auto-close hospital=%s completed=%d no_show=%d held=%d",
            hospital_id,
            closed,
            no_shows,
            held,
        )
    return {"completed": closed, "no_show": no_shows, "held_for_staff": held}
