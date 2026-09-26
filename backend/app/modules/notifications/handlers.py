"""Where notifications listens to the rest of the hospital.

CLAUDE.md §13 step 8: *"triggers wired to domain events"*. This file is those
triggers, and almost all of them are the same shape — take an event, resolve who
should hear about it, hand it to `service.notify`.

---------------------------------------------------------------------------
Two channels, used for two genuinely different jobs
---------------------------------------------------------------------------

Every **outbound** handler here subscribes with `event_bus.subscribe` — the
fire-and-forget channel. That is what `core/events.py` built the channel for: a
WhatsApp gateway is a third party with its own failure domain, and a provider
having a bad afternoon must never roll back a doctor signing off a consultation.

`suppress_on_death` is the exception, and subscribes **transactionally**. It
looks inconsistent, so the reasoning is worth stating:

* It calls no gateway. It writes one row to the same database, in the same
  transaction as the death record.
* It therefore has **no independent failure domain** — the condition
  `core/events.py` names as the whole test for which channel to use. If that
  INSERT fails, the death record's own INSERT was going to fail too.
* What it writes is the enforcement of CLAUDE.md §14. A swallowed exception here
  does not lose a message; it loses the rule that stops every future message.

So: sending is isolated, and the suppression that governs sending is not.

---------------------------------------------------------------------------
The rule these handlers follow, and why
---------------------------------------------------------------------------

**Render from the event payload, never by re-reading the row the publisher just
wrote.** A fire-and-forget handler runs *before* the publisher commits, in its
own session, so the just-written row is usually invisible to it. Facts that
predate the transaction — the patient's phone number, the hospital's name, the
doctor's display name — are safe to read. The report that was just verified is
not, and nothing here reads one.

The consequence, accepted deliberately when this delivery model was chosen: if
the publisher's transaction subsequently rolls back, a message has gone out
about something that did not happen. Bounded, because every message is built only
from facts the event carried, but real.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.events import DomainEvent, event_bus
from app.modules.billing.events import InvoiceIssued, PaymentReceived, SettlementRequired
from app.modules.clinical import service as clinical_service
from app.modules.clinical.events import EncounterClosed, PatientDeceased
from app.modules.clinical.models import EncounterStatus
from app.modules.diagnostics.events import CriticalResultFlagged, ReportReady, SpecimenRejected
from app.modules.identity import service as identity_service
from app.modules.notifications import service
from app.modules.notifications.models import NotificationCategory, RecipientType
from app.modules.notifications.service import Recipient
from app.modules.notifications.templates import DEFAULT_LANGUAGE, TemplateCode
from app.modules.patients import service as patients_service
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.events import AppointmentBooked, AppointmentCancelled
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = ["register_handlers"]


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------
async def _hospital_name(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """The hospital's own name, for the message signature.

    Read through `tenancy.service` (CLAUDE.md §2). A patient receiving "your
    report is ready" from an unnamed sender has no idea which of three clinics
    they visited this month means it. A hospital whose name cannot be read is
    not a reason to withhold the message.
    """
    try:
        hospital = await tenancy_service.get_hospital(session, hospital_id)
    except Exception:  # noqa: BLE001 - a missing name must not stop the message
        logger.warning("could not resolve hospital name for %s", hospital_id)
        return "Hospital"
    return hospital.name


async def _for_patient(
    session: AsyncSession, hospital_id: uuid.UUID, patient_id: uuid.UUID
) -> tuple[Recipient, dict[str, object]]:
    """Resolve a patient once: where to reach them, and the common placeholders.

    One read rather than two. Every patient-facing template can rely on
    `hospital_name`, `patient_name` and `uhid` being present, so handlers only
    add what is specific to their event.
    """
    patient = await patients_service.get_patient(session, patient_id)
    recipient = Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        # Falls back to the guardian's number for minors and unconscious
        # admissions. Note this makes the guardian a *patient-directed* address,
        # which is exactly right for the deceased rule: CLAUDE.md §6 forbids the
        # follow-up SMS reaching the family, and this is usually the family.
        phone=patient.phone or patient.guardian_phone,
        email=patient.email,
        language=patient.preferred_language or DEFAULT_LANGUAGE,
        patient_id=patient.id,
    )
    context: dict[str, object] = {
        "hospital_name": await _hospital_name(session, hospital_id),
        "patient_name": patient.full_name,
        "uhid": patient.uhid,
    }
    return recipient, context


def _guard[EventT: DomainEvent](
    name: str,
) -> Callable[[Callable[[EventT], Awaitable[None]]], Callable[[EventT], Awaitable[None]]]:
    """Skip untenanted events, and name the failure when one happens.

    The bus already swallows and logs on this channel, but it logs the handler
    and nothing about *which* message was lost. On a fire-and-forget channel
    that is the difference between a diagnosable incident and a shrug, so the
    event id is attached before the exception is allowed to continue upwards.
    """

    def decorate(func: Callable[[EventT], Awaitable[None]]) -> Callable[[EventT], Awaitable[None]]:
        async def wrapper(event: EventT) -> None:
            if event.hospital_id is None:
                return
            try:
                await func(event)
            except Exception:
                logger.exception(
                    "notification trigger failed handler=%s event=%s event_id=%s",
                    name,
                    event.name,
                    event.event_id,
                )
                raise

        wrapper.__name__ = name
        return wrapper

    return decorate


def _when(moment: object) -> str:
    """Format a scheduled time the way an Indian patient reads it."""
    return moment.strftime("%d %b %Y, %I:%M %p") if hasattr(moment, "strftime") else str(moment)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
@_guard("notify_appointment_booked")
async def notify_appointment_booked(event: AppointmentBooked) -> None:
    """Confirm a booking — the message a patient screenshots and shows at the gate."""
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        doctor = await scheduling_service.get_doctor(
            session, event.doctor_id, hospital_id=event.hospital_id
        )
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "doctor_name": doctor.display_name,
            "appointment_time": _when(event.scheduled_start),
            "appointment_number": event.appointment_number,
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.APPOINTMENT_BOOKED,
            recipient=recipient,
            context=context,
            category=NotificationCategory.APPOINTMENT,
            source_module="scheduling",
            source_type="appointment",
            source_id=event.appointment_id,
        )
        await session.commit()


@_guard("notify_appointment_cancelled")
async def notify_appointment_cancelled(event: AppointmentCancelled) -> None:
    """Tell the patient a slot is gone.

    Sent whoever cancelled it. A patient who cancelled by phone still wants the
    confirmation, and a patient whose doctor cancelled needs it badly.
    """
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "appointment_time": _when(event.scheduled_start),
            "reason": event.reason or "",
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.APPOINTMENT_CANCELLED,
            recipient=recipient,
            context=context,
            category=NotificationCategory.APPOINTMENT,
            source_module="scheduling",
            source_type="appointment_cancel",
            source_id=event.appointment_id,
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@_guard("notify_report_ready")
async def notify_report_ready(event: ReportReady) -> None:
    """ "Your report is ready" — CLAUDE.md §13 step 6 asks for exactly this.

    `diagnostics` publishes this only on FINAL, never on a preliminary result, so
    there is no risk of announcing a number that then changes.
    """
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "test_name": event.test_name,
            "report_number": event.report_number,
            "discipline": event.discipline.title(),
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.REPORT_READY,
            recipient=recipient,
            context=context,
            category=NotificationCategory.REPORT,
            encounter_id=event.encounter_id,
            source_module="diagnostics",
            source_type="report",
            source_id=event.report_id,
        )
        await session.commit()


@_guard("notify_critical_result")
async def notify_critical_result(event: CriticalResultFlagged) -> None:
    """A panic value, to the treating doctor. Staff-directed, deliberately.

    This is the handler that shows why `RecipientType` exists. The message is
    *about* a patient and *addressed to* a clinician, so no patient-level opt-out
    and no death suppression touches it — a potassium of 7.2 reaches the doctor
    whatever the patient unsubscribed from. NABH additionally requires the
    resulting callback to be documented, which `diagnostics` records on the
    report; this message is what prompts it.
    """
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        encounter = await clinical_service.get_encounter(
            session, event.encounter_id, hospital_id=event.hospital_id
        )
        if encounter.doctor_id is None:
            # Nothing to escalate to. Logged loudly rather than swallowed: a
            # critical value with no named clinician is an operational problem,
            # not a quiet no-op.
            logger.warning(
                "critical result on encounter %s has no assigned doctor to alert",
                event.encounter_id,
            )
            return

        doctor = await scheduling_service.get_doctor(
            session, encounter.doctor_id, hospital_id=event.hospital_id
        )
        user = await identity_service.get_user(session, doctor.user_id)

        _, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "test_name": event.test_name,
            "critical_values": "; ".join(event.critical_values) or "see report",
            "doctor_name": doctor.display_name,
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.CRITICAL_RESULT,
            recipient=Recipient(
                recipient_type=RecipientType.STAFF,
                name=user.full_name,
                phone=user.phone,
                email=user.email,
                user_id=user.id,
                # Recorded so the alert appears on the patient's message history,
                # even though the patient is not the recipient.
                patient_id=event.patient_id,
            ),
            context=context,
            category=NotificationCategory.CRITICAL_ALERT,
            encounter_id=event.encounter_id,
            source_module="diagnostics",
            source_type="critical_result",
            source_id=event.report_id,
        )
        await session.commit()


@_guard("notify_specimen_rejected")
async def notify_specimen_rejected(event: SpecimenRejected) -> None:
    """The sample failed. The patient has to be stuck again, and is owed the reason."""
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {"reason": event.reason, "accession_number": event.accession_number}

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.SPECIMEN_REJECTED,
            recipient=recipient,
            context=context,
            category=NotificationCategory.REPORT,
            source_module="diagnostics",
            source_type="specimen_rejected",
            source_id=event.specimen_id,
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
@_guard("notify_payment_received")
async def notify_payment_received(event: PaymentReceived) -> None:
    """The receipt. In an Indian OPD this is the message patients actually keep."""
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "amount": f"Rs {event.amount}",
            "receipt_number": event.receipt_number,
            "method": event.method.replace("_", " ").title(),
            "balance_due": f"Rs {event.balance_due}",
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.PAYMENT_RECEIPT,
            recipient=recipient,
            context=context,
            category=NotificationCategory.BILLING,
            source_module="billing",
            source_type="payment",
            source_id=event.payment_id,
        )
        await session.commit()


@_guard("notify_invoice_issued")
async def notify_invoice_issued(event: InvoiceIssued) -> None:
    """The bill, when it is issued rather than when it is paid."""
    assert event.hospital_id is not None
    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "invoice_number": event.invoice_number,
            "grand_total": f"Rs {event.grand_total}",
            "balance_due": f"Rs {event.balance_due}",
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.INVOICE_ISSUED,
            recipient=recipient,
            context=context,
            category=NotificationCategory.BILLING,
            encounter_id=event.encounter_id,
            source_module="billing",
            source_type="invoice",
            source_id=event.invoice_id,
        )
        await session.commit()


@_guard("notify_settlement_required")
async def notify_settlement_required(event: SettlementRequired) -> None:
    """An outstanding balance on a closed visit.

    `notify_patient` is the whole handler. `billing` sets it to False for a death
    — CLAUDE.md §6 requires the settlement flow to run and the bill to be settled
    with the family *in person*, with no automated reminder attached. So when the
    flag is false, nothing is queued at all.

    Belt and braces rather than either/or: the dispatcher would suppress a
    patient-directed message to a deceased patient anyway, and this handler still
    declines to raise one. An invariant should not rest on a single layer.
    """
    assert event.hospital_id is not None
    if not event.notify_patient:
        logger.info(
            "settlement on encounter %s is not notifiable (context=%s); no message queued",
            event.encounter_id,
            event.context,
        )
        return

    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)
        context |= {
            "balance_due": f"Rs {event.balance_due}",
            "context_status": event.context.replace("_", " ").title(),
        }

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.SETTLEMENT_REQUIRED,
            recipient=recipient,
            context=context,
            category=NotificationCategory.BILLING,
            encounter_id=event.encounter_id,
            source_module="billing",
            source_type="settlement",
            source_id=event.invoice_id,
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Clinical
# ---------------------------------------------------------------------------
@_guard("notify_follow_up")
async def notify_follow_up(event: EncounterClosed) -> None:
    """The follow-up nudge — and the handler CLAUDE.md §6 warns about by name.

    Gated on `COMPLETED` alone. The dispatcher's suppression stops a deceased
    patient's message on its own, but `LAMA` and `REFERRED_OUT` are neither
    deceased nor completed, and "please book your follow-up" to a patient who
    discharged themselves against advice, or who was sent to another hospital, is
    wrong in a way no suppression rule would catch.
    """
    assert event.hospital_id is not None
    if event.final_status != EncounterStatus.COMPLETED.value:
        return

    async with tenant_session(event.hospital_id) as session:
        recipient, context = await _for_patient(session, event.hospital_id, event.patient_id)

        await service.notify(
            session,
            hospital_id=event.hospital_id,
            code=TemplateCode.FOLLOW_UP_REMINDER,
            recipient=recipient,
            context=context,
            category=NotificationCategory.FOLLOW_UP,
            encounter_id=event.encounter_id,
            source_module="clinical",
            source_type="encounter_closed",
            source_id=event.encounter_id,
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The invariant (CLAUDE.md §14)
# ---------------------------------------------------------------------------
async def suppress_on_death(event: PatientDeceased, session: AsyncSession) -> None:
    """Permanently block patient-directed messaging, in the death's own transaction.

    The **only** transactional subscriber in this module, for the reason set out
    in the module docstring: it writes to the same database in the same
    transaction and has no independent failure domain, so the fire-and-forget
    channel's swallow-and-log behaviour would silently drop the enforcement of
    the invariant itself.

    Two things happen and the second is not redundant. The suppression row stops
    anything *future*; cancelling the queue removes what is already sitting in
    it. Dispatch would refuse those anyway — that is layer two — but a pending
    "book your follow-up" row against a deceased patient is a loaded gun for the
    next person who writes a bulk-send script, and the safest row is one that
    does not exist.
    """
    if event.hospital_id is None:
        return

    await service.suppress_for_death(
        session,
        hospital_id=event.hospital_id,
        patient_id=event.patient_id,
        note=f"Death recorded on encounter {event.encounter_id}.",
    )
    cancelled = await service.cancel_pending_for_patient(
        session,
        hospital_id=event.hospital_id,
        patient_id=event.patient_id,
        reason="Patient deceased. No automated message is sent.",
    )
    logger.info(
        "death suppression applied patient=%s queued_cancelled=%d", event.patient_id, cancelled
    )


def register_handlers() -> None:
    """Wire notifications into the rest of the system. Called once, at import.

    Registered from `app.registry`, which is imported exactly once per process by
    the app factory, the worker and the test harness alike.
    """
    event_bus.subscribe(AppointmentBooked, notify_appointment_booked)
    event_bus.subscribe(AppointmentCancelled, notify_appointment_cancelled)
    event_bus.subscribe(ReportReady, notify_report_ready)
    event_bus.subscribe(CriticalResultFlagged, notify_critical_result)
    event_bus.subscribe(SpecimenRejected, notify_specimen_rejected)
    event_bus.subscribe(PaymentReceived, notify_payment_received)
    event_bus.subscribe(InvoiceIssued, notify_invoice_issued)
    event_bus.subscribe(SettlementRequired, notify_settlement_required)
    event_bus.subscribe(EncounterClosed, notify_follow_up)

    # The one exception. See the module docstring.
    event_bus.subscribe_transactional(PatientDeceased, suppress_on_death)

    logger.debug("notification handlers registered")
