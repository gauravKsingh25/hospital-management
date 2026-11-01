"""No patient message while nobody knows who the patient is (UX-01).

An unidentified emergency record is called `Unknown Male EM-0034`. Anything
patient-facing raised for it — the booking confirmation quick-OPD sends, a
payment receipt — would greet a stranger by a placeholder, at whatever number
happened to be on the record (often the police constable's or a passer-by's,
entered as the guardian). So the message is held, at both layers, and the row
says why rather than failing as "no contact details".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ValidationError
from app.modules.notifications import service
from app.modules.notifications.gateways import GatewayResult, OutboundMessage
from app.modules.notifications.models import (
    NotificationChannel,
    NotificationStatus,
    RecipientType,
    SuppressionReason,
)
from app.modules.notifications.schemas import SuppressionCreate
from app.modules.notifications.service import Recipient
from app.modules.notifications.templates import TemplateCode
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientIdentify, PatientUpdate
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration

CONTEXT = {"hospital_name": "Sunrise", "patient_name": "Unknown Male EM-0001"}


class RecordingGateway:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.sent.append(message)
        return GatewayResult.ok(self.name)


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


@pytest.fixture
async def unknown(session: AsyncSession, tenant: Hospital) -> Patient:
    patient = await patients_service.register_unidentified(
        session, hospital_id=tenant.id, gender=Gender.MALE, age_years=40
    )
    # Whoever brought him in left a number. It is not his.
    await patients_service.update_patient(
        session, patient, PatientUpdate(guardian_name="Passer-by", guardian_phone="9811100000")
    )
    await session.commit()
    return patient


def _to(patient: Patient) -> Recipient:
    return Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone or patient.guardian_phone,
        patient_id=patient.id,
    )


async def test_a_message_to_an_unidentified_patient_is_held(
    session: AsyncSession, tenant: Hospital, unknown: Patient
) -> None:
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=_to(unknown),
        context=CONTEXT,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SUPPRESSED
    assert notification.suppression_reason is SuppressionReason.UNIDENTIFIED
    assert gateway.sent == []


async def test_it_is_held_again_at_send(
    session: AsyncSession, tenant: Hospital, unknown: Patient
) -> None:
    """Layer two: a row that somehow reached the queue is still refused."""
    queued = await service.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=_to(unknown),
        context=CONTEXT,
    )
    queued.status = NotificationStatus.PENDING
    queued.suppression_reason = None
    session.add(queued)
    await session.flush()

    gateway = RecordingGateway()
    result = await service.dispatch(session, queued, gateway=gateway)
    await session.commit()

    assert result.status is NotificationStatus.SUPPRESSED
    assert result.suppression_reason is SuppressionReason.UNIDENTIFIED
    assert gateway.sent == []


async def test_a_death_still_outranks_it(
    session: AsyncSession, tenant: Hospital, unknown: Patient
) -> None:
    """The reason on the row is the one that can never be lifted."""
    await patients_service.mark_deceased(session, unknown, occurred_at=datetime.now(UTC))
    await session.commit()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=_to(unknown),
        context=CONTEXT,
        gateway=RecordingGateway(),
    )
    assert notification.suppression_reason is SuppressionReason.DECEASED


async def test_messages_flow_once_the_patient_is_identified(
    session: AsyncSession, tenant: Hospital, unknown: Patient
) -> None:
    await patients_service.identify_patient(
        session,
        unknown,
        PatientIdentify(
            full_name="Suresh Yadav", phone="9898989898", gender=Gender.MALE, age_years=38
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=_to(unknown),
        context={"hospital_name": "Sunrise", "patient_name": "Suresh Yadav"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert [message.address for message in gateway.sent] == ["9898989898"]


async def test_the_reason_is_not_set_by_hand(unknown: Patient) -> None:
    """It is a fact about the record, cleared by identifying the patient —
    not a block somebody adds or lifts."""
    with pytest.raises(ValueError, match="identif"):
        SuppressionCreate(patient_id=unknown.id, reason=SuppressionReason.UNIDENTIFIED)


async def test_the_service_also_refuses_it_directly(
    session: AsyncSession, tenant: Hospital, unknown: Patient
) -> None:
    payload = SuppressionCreate(patient_id=unknown.id, reason=SuppressionReason.OPTED_OUT)
    object.__setattr__(payload, "reason", SuppressionReason.UNIDENTIFIED)

    with pytest.raises(ValidationError, match="not recorded by hand"):
        await service.suppress(session, payload, hospital_id=tenant.id)
