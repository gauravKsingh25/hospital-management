"""The Encounter state machine — the one place a visit's status may change.

CLAUDE.md §6 and §14 make this an invariant: **no status field is ever set
directly anywhere else.** `transition()` is the only writer. It

  1. validates that the move is legal,
  2. validates that a terminal outcome carries the metadata that makes it a
     record rather than a rumour,
  3. stamps the outcome columns and the lifecycle timestamps,
  4. writes an `EncounterEvent` audit row,
  5. fires the domain events other modules react to.

Transitions are **data, not conditionals**. A table can be read by a clinician,
printed on a wall, and tested exhaustively; a chain of `if` statements spread
across a service layer cannot, and it is exactly the shape that eventually lets
a completed visit quietly reopen.

The same discipline was applied a phase early to appointments and queue tokens
(`scheduling/transitions.py`), so this file is the pattern's real home rather
than its first draft.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.core.exceptions import IllegalStateTransitionError, ValidationError
from app.core.models import utc_now
from app.modules.clinical.events import (
    EncounterAdmitted,
    EncounterClosed,
    EncounterOpened,
    EncounterStatusChanged,
    PatientDeceased,
    PatientLeftAgainstAdvice,
    PatientReferredOut,
)
from app.modules.clinical.models import Encounter, EncounterEvent, EncounterStatus
from app.modules.identity.models import User
from app.modules.patients import service as patients_service

__all__ = [
    "ENCOUNTER_TRANSITIONS",
    "REQUIRED_METADATA",
    "TERMINAL_STATUSES",
    "assert_transition",
    "is_terminal",
    "open_encounter_event",
    "transition",
]


# ---------------------------------------------------------------------------
# The table. From -> the set of statuses it may legally become.
# ---------------------------------------------------------------------------
_UNPLANNED_EXITS: Final[frozenset[EncounterStatus]] = frozenset(
    {
        # CLAUDE.md §6: death and referral "can be entered from most active
        # states by an authorized role". So can absconding — a patient can walk
        # out of the waiting room as easily as out of a ward.
        EncounterStatus.REFERRED_OUT,
        EncounterStatus.LAMA,
        EncounterStatus.DECEASED,
    }
)

ENCOUNTER_TRANSITIONS: Final[dict[EncounterStatus, frozenset[EncounterStatus]]] = {
    EncounterStatus.REGISTERED: frozenset(
        {
            EncounterStatus.IN_CONSULTATION,
            # Casualty admits directly; there is not always an OPD consultation
            # between the door and the ward.
            EncounterStatus.ADMITTED,
            EncounterStatus.CANCELLED,
            EncounterStatus.NO_SHOW,
            *_UNPLANNED_EXITS,
        }
    ),
    EncounterStatus.IN_CONSULTATION: frozenset(
        {
            EncounterStatus.AWAITING_RESULTS,
            EncounterStatus.PENDING_CLEARANCE,
            EncounterStatus.COMPLETED,
            EncounterStatus.ADMITTED,
            *_UNPLANNED_EXITS,
        }
    ),
    EncounterStatus.AWAITING_RESULTS: frozenset(
        {
            # Results are back and the doctor sees them again — the whole point
            # of distinguishing this state from PENDING_CLEARANCE.
            EncounterStatus.IN_CONSULTATION,
            EncounterStatus.PENDING_CLEARANCE,
            EncounterStatus.COMPLETED,
            EncounterStatus.ADMITTED,
            *_UNPLANNED_EXITS,
        }
    ),
    EncounterStatus.PENDING_CLEARANCE: frozenset(
        {
            EncounterStatus.COMPLETED,
            # The doctor called them back in, or a fresh test was ordered at the
            # counter. Both happen; neither should need a new encounter.
            EncounterStatus.IN_CONSULTATION,
            EncounterStatus.AWAITING_RESULTS,
            EncounterStatus.ADMITTED,
            *_UNPLANNED_EXITS,
        }
    ),
    EncounterStatus.ADMITTED: frozenset(
        {
            # Discharged alive and well. `ipd` (Phase 9) drives this.
            EncounterStatus.COMPLETED,
            # Most in-hospital deaths happen here, so DECEASED must be reachable
            # from a ward and not only from a consulting room.
            *_UNPLANNED_EXITS,
        }
    ),
    # --- terminal --------------------------------------------------------
    # Nothing reopens. A completed visit that can go back to IN_CONSULTATION is
    # a completed visit that can be silently rewritten; a correction is a new
    # encounter with a reason, which leaves a trail a regulator can follow.
    EncounterStatus.COMPLETED: frozenset(),
    EncounterStatus.CANCELLED: frozenset(),
    EncounterStatus.NO_SHOW: frozenset(),
    EncounterStatus.REFERRED_OUT: frozenset(),
    EncounterStatus.LAMA: frozenset(),
    EncounterStatus.DECEASED: frozenset(),
}

TERMINAL_STATUSES: Final[frozenset[EncounterStatus]] = frozenset(
    status for status, allowed in ENCOUNTER_TRANSITIONS.items() if not allowed
)

# What each terminal outcome must carry to be worth recording at all
# (CLAUDE.md §6: "record structured metadata").
REQUIRED_METADATA: Final[dict[EncounterStatus, tuple[str, ...]]] = {
    EncounterStatus.DECEASED: ("deceased_at", "death_certified_by_id", "cause_of_death"),
    EncounterStatus.REFERRED_OUT: ("referred_to_facility", "referral_reason"),
    EncounterStatus.LAMA: ("lama_reason",),
}

# Statuses whose `reason` argument is mandatory. A cancelled visit with no
# explanation is indistinguishable from a mis-click.
_REASON_REQUIRED: Final[frozenset[EncounterStatus]] = frozenset({EncounterStatus.CANCELLED})

# Terminal outcomes that still owe money. CLAUDE.md §6: a deceased or referred
# patient usually still has an outstanding bill, and the settlement flow must
# run rather than the visit simply vanishing from billing's view.
_SETTLEMENT_REQUIRED: Final[frozenset[EncounterStatus]] = frozenset(
    {
        EncounterStatus.COMPLETED,
        EncounterStatus.DECEASED,
        EncounterStatus.REFERRED_OUT,
        EncounterStatus.LAMA,
    }
)


def is_terminal(status: EncounterStatus) -> bool:
    return status in TERMINAL_STATUSES


def _phrase(status: EncounterStatus) -> str:
    """`AWAITING_RESULTS` -> `awaiting results`, for error messages staff read."""
    return status.value.lower().replace("_", " ")


def assert_transition(current: EncounterStatus, target: EncounterStatus) -> None:
    """Raise unless `current -> target` is allowed.

    Pure and synchronous so the whole table can be tested without a database.
    The error names both states and lists the legal alternatives, because the
    person who hits this is usually a receptionist being told "no" by a screen,
    and someone has to be able to explain why.
    """
    allowed = ENCOUNTER_TRANSITIONS.get(current, frozenset())
    if target == current:
        raise IllegalStateTransitionError(
            f"The visit is already {_phrase(current)}.",
            details={"from": current.value, "to": target.value},
        )
    if target not in allowed:
        detail = (
            f"A visit that is {_phrase(current)} cannot become {_phrase(target)}."
            if allowed
            else f"A visit that is {_phrase(current)} is closed and cannot change."
        )
        raise IllegalStateTransitionError(
            detail,
            details={
                "from": current.value,
                "to": target.value,
                "allowed": sorted(status.value for status in allowed),
            },
        )


def _assert_metadata(target: EncounterStatus, metadata: dict[str, Any]) -> None:
    required = REQUIRED_METADATA.get(target, ())
    missing = [key for key in required if metadata.get(key) in (None, "")]
    if missing:
        raise ValidationError(
            f"Recording a visit as {_phrase(target)} requires: {', '.join(missing)}.",
            code="transition_metadata_required",
            details={"status": target.value, "missing": missing},
        )


def _apply_outcome(
    encounter: Encounter,
    target: EncounterStatus,
    metadata: dict[str, Any],
    *,
    actor: User | None,
    moment: datetime,
) -> None:
    """Stamp the columns that make a terminal outcome explicable."""
    if target is EncounterStatus.DECEASED:
        encounter.deceased_at = metadata["deceased_at"]
        encounter.death_certified_by_id = metadata["death_certified_by_id"]
        encounter.cause_of_death = metadata["cause_of_death"]
        encounter.death_place = metadata.get("death_place")

    elif target is EncounterStatus.REFERRED_OUT:
        encounter.referred_at = metadata.get("referred_at") or moment
        encounter.referred_to_facility = metadata["referred_to_facility"]
        encounter.referral_reason = metadata["referral_reason"]
        encounter.referral_transport = metadata.get("referral_transport")

    elif target is EncounterStatus.LAMA:
        encounter.lama_at = metadata.get("lama_at") or moment
        # Whoever pressed the button owns this, not whoever is named in a form.
        encounter.lama_recorded_by_id = actor.id if actor else None
        encounter.lama_reason = metadata["lama_reason"]
        encounter.lama_form_signed = bool(metadata.get("lama_form_signed", False))

    elif target in (EncounterStatus.CANCELLED, EncounterStatus.NO_SHOW):
        encounter.cancellation_reason = metadata.get("cancellation_reason")

    elif target is EncounterStatus.IN_CONSULTATION and encounter.consultation_started_at is None:
        encounter.consultation_started_at = moment


async def transition(
    session: AsyncSession,
    encounter: Encounter,
    to_status: EncounterStatus,
    *,
    actor: User | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    automatic: bool = False,
) -> Encounter:
    """Move an encounter to `to_status`. The only way a status ever changes.

    `actor` is None only for the auto-close worker, and `automatic=True` records
    that on the encounter so reporting can tell "the clinic closed this visit"
    from "nobody did, and a background job tidied up". The second is a process
    problem worth surfacing, not a success.

    Side effects are deliberately inside this function rather than left to
    callers: flagging the patient record as deceased is what makes "never notify
    a deceased patient" (CLAUDE.md §14) an invariant instead of a convention
    that holds until someone adds a second code path.
    """
    payload: dict[str, Any] = dict(metadata or {})

    assert_transition(encounter.status, to_status)
    _assert_metadata(to_status, payload)
    if to_status in _REASON_REQUIRED and not reason:
        raise ValidationError(
            f"A reason is required to mark a visit {_phrase(to_status)}.",
            code="transition_reason_required",
            details={"status": to_status.value},
        )

    moment = utc_now()
    previous = encounter.status

    _apply_outcome(encounter, to_status, payload, actor=actor, moment=moment)

    encounter.status = to_status
    encounter.updated_at = moment
    if is_terminal(to_status):
        encounter.closed_at = moment
        encounter.closed_by_id = actor.id if actor else None
        encounter.closed_automatically = automatic
    session.add(encounter)

    session.add(
        EncounterEvent(
            hospital_id=encounter.hospital_id,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            from_status=previous,
            to_status=to_status,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            reason=reason,
            event_metadata=_serialisable(payload) or None,
            occurred_at=moment,
        )
    )

    # A death is denormalised onto the patient in the same transaction as the
    # transition that recorded it. Anything looser leaves a window in which the
    # encounter says DECEASED and the notification dispatcher does not know.
    if to_status is EncounterStatus.DECEASED:
        patient = await patients_service.get_patient(session, encounter.patient_id)
        await patients_service.mark_deceased(session, patient, occurred_at=payload["deceased_at"])

    await session.flush()
    await _publish(
        session, encounter, previous=previous, actor=actor, reason=reason, metadata=payload
    )
    return encounter


def _serialisable(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce a metadata dict into something JSONB will accept.

    Datetimes and UUIDs arrive here routinely (a death time, a certifying
    doctor); `json.dumps` refuses both, and a transition that fails to serialise
    its own audit row would be a spectacular way to lose a death record.
    """
    return {
        key: (value.isoformat() if isinstance(value, datetime) else str(value))
        if isinstance(value, datetime | uuid.UUID)
        else value
        for key, value in payload.items()
    }


async def _publish(
    session: AsyncSession,
    encounter: Encounter,
    *,
    previous: EncounterStatus,
    actor: User | None,
    reason: str | None,
    metadata: dict[str, Any],
) -> None:
    """Fan out the domain events for one transition.

    Every transition publishes `EncounterStatusChanged`; the notable ones also
    publish a named event, so a subscriber that only cares about deaths does not
    have to filter a firehose.

    The session travels with them because `billing` subscribes transactionally:
    the settlement flow a closed visit triggers has to be written in the same
    transaction as the closure, or a deceased patient's outstanding bill can go
    missing between the two (CLAUDE.md §6).
    """
    # Spelled out at each call rather than splatted from a shared dict: a dict
    # erases the field types, and these events are a public contract that other
    # modules will be written against.
    hospital_id = encounter.hospital_id
    actor_id = actor.id if actor else None
    encounter_id = encounter.id
    patient_id = encounter.patient_id

    await event_bus.publish(
        EncounterStatusChanged(
            hospital_id=hospital_id,
            actor_id=actor_id,
            encounter_id=encounter_id,
            patient_id=patient_id,
            from_status=previous.value,
            to_status=encounter.status.value,
            reason=reason,
        ),
        session=session,
    )

    if encounter.status is EncounterStatus.DECEASED:
        await event_bus.publish(
            PatientDeceased(
                hospital_id=hospital_id,
                actor_id=actor_id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                died_at=metadata["deceased_at"],
                cause_of_death=metadata["cause_of_death"],
                certified_by_id=metadata["death_certified_by_id"],
            ),
            session=session,
        )
    elif encounter.status is EncounterStatus.REFERRED_OUT:
        await event_bus.publish(
            PatientReferredOut(
                hospital_id=hospital_id,
                actor_id=actor_id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                referred_to_facility=metadata["referred_to_facility"],
                referral_reason=metadata["referral_reason"],
            ),
            session=session,
        )
    elif encounter.status is EncounterStatus.LAMA:
        await event_bus.publish(
            PatientLeftAgainstAdvice(
                hospital_id=hospital_id,
                actor_id=actor_id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                lama_reason=metadata["lama_reason"],
                form_signed=bool(metadata.get("lama_form_signed", False)),
            ),
            session=session,
        )
    elif encounter.status is EncounterStatus.ADMITTED:
        await event_bus.publish(
            EncounterAdmitted(
                hospital_id=hospital_id,
                actor_id=actor_id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                from_status=previous.value,
                appointment_id=encounter.appointment_id,
                was_seen=encounter.consultation_started_at is not None,
            ),
            session=session,
        )

    if is_terminal(encounter.status):
        await event_bus.publish(
            EncounterClosed(
                hospital_id=hospital_id,
                actor_id=actor_id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                final_status=encounter.status.value,
                # Deceased and referred patients still have bills. Billing keys
                # its settlement flow off this rather than off COMPLETED alone.
                requires_settlement=encounter.status in _SETTLEMENT_REQUIRED,
                closed_automatically=encounter.closed_automatically,
                appointment_id=encounter.appointment_id,
                was_seen=encounter.consultation_started_at is not None,
            ),
            session=session,
        )


async def open_encounter_event(
    session: AsyncSession, encounter: Encounter, *, actor: User | None
) -> None:
    """Write the opening timeline row and announce the new encounter.

    Separate from `transition` because an encounter is *born* in `REGISTERED`
    rather than moving into it, and the table has no `None -> REGISTERED` entry
    to validate against. The timeline would otherwise start mid-story.
    """
    session.add(
        EncounterEvent(
            hospital_id=encounter.hospital_id,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            from_status=None,
            to_status=encounter.status,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            reason="Visit opened.",
            occurred_at=encounter.started_at,
        )
    )
    await session.flush()
    await event_bus.publish(
        EncounterOpened(
            hospital_id=encounter.hospital_id,
            actor_id=actor.id if actor else None,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            encounter_number=encounter.encounter_number,
            encounter_type=encounter.encounter_type.value,
            doctor_id=encounter.doctor_id,
            appointment_id=encounter.appointment_id,
        ),
        session=session,
    )
