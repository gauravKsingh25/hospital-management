"""The follow-up date the doctor set, carried through to the patient.

Before this, the only message a completed visit produced was "please book a
follow-up if your doctor has advised one" — at the moment the visit closed, with
no date in it, and nothing at all as the date approached. The doctor's date was
recorded and then never reached the one person who had to act on it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter
from app.modules.clinical.schemas import DeathRecord, LamaRecord
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.notifications import service
from app.modules.notifications.gateways import GatewayResult, OutboundMessage
from app.modules.notifications.models import (
    NotificationChannel,
    NotificationStatus,
    SuppressionReason,
)
from app.modules.notifications.templates import TemplateCode
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


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
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ram Prasad", phone="9812345670", gender=Gender.MALE, age_years=71
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    return record


async def _local_today(session: AsyncSession, tenant: Hospital) -> date:
    return await tenancy_service.local_today(session, tenant.id)


async def _at(session: AsyncSession, tenant: Hospital, day: date, hour: int) -> datetime:
    """A moment on the hospital's own clock."""
    zone = await tenancy_service.local_zone(session, tenant.id)
    return datetime.combine(day, time(hour), tzinfo=zone)


async def _complete(
    session: AsyncSession, encounter: Encounter, doctor: User, follow_up: date | None
) -> None:
    await clinical_service.start_consultation(session, encounter, actor=doctor)
    await clinical_service.complete_consultation(
        session, encounter, actor=doctor, follow_up_date=follow_up
    )
    await session.commit()


async def _messages(session: AsyncSession, tenant: Hospital, patient: Patient, code: str):  # type: ignore[no-untyped-def]
    rows, _ = await service.list_notifications(
        session, PageParams(limit=50), hospital_id=tenant.id, patient_id=patient.id
    )
    return [row for row in rows if row.template_code == code]


# ---------------------------------------------------------------------------
# At the close of the visit
# ---------------------------------------------------------------------------
async def test_the_closing_message_names_the_follow_up_date(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    follow_up = await _local_today(session, tenant) + timedelta(days=8)
    await _complete(session, encounter, doctor_user, follow_up)

    scheduled = await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_SCHEDULED)
    assert len(scheduled) == 1
    assert follow_up.strftime("%d %b %Y") in scheduled[0].body
    # The dateless nudge is not sent as well.
    assert await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_REMINDER) == []


async def test_without_a_date_the_closing_message_is_the_general_nudge(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    await _complete(session, encounter, doctor_user, None)

    assert len(await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_REMINDER)) == 1
    assert await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_SCHEDULED) == []


# ---------------------------------------------------------------------------
# The day before
# ---------------------------------------------------------------------------
async def test_the_sweep_reminds_the_patient_the_day_before(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    today = await _local_today(session, tenant)
    follow_up = today + timedelta(days=5)
    await _complete(session, encounter, doctor_user, follow_up)
    gateway = RecordingGateway()

    # Two days out: nothing yet.
    early = await _at(session, tenant, follow_up - timedelta(days=2), 10)
    assert (
        await service.send_follow_up_reminders(
            session, hospital_id=tenant.id, now=early, gateway=gateway
        )
        == 0
    )

    eve = await _at(session, tenant, follow_up - timedelta(days=1), 10)
    sent = await service.send_follow_up_reminders(
        session, hospital_id=tenant.id, now=eve, gateway=gateway
    )
    await session.commit()

    assert sent == 1
    due = await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_DUE)
    assert len(due) == 1
    assert due[0].status is NotificationStatus.SENT
    assert follow_up.strftime("%d %b %Y") in due[0].body
    assert len(gateway.sent) == 1


async def test_the_reminder_is_sent_once_however_often_the_sweep_runs(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    follow_up = await _local_today(session, tenant) + timedelta(days=3)
    await _complete(session, encounter, doctor_user, follow_up)
    gateway = RecordingGateway()

    for hour in (10, 13, 18):
        moment = await _at(session, tenant, follow_up - timedelta(days=1), hour)
        await service.send_follow_up_reminders(
            session, hospital_id=tenant.id, now=moment, gateway=gateway
        )
        await session.commit()

    assert len(await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_DUE)) == 1
    assert len(gateway.sent) == 1


async def test_no_reminder_goes_out_in_the_small_hours(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    """The sweep runs all night. A hospital message at 00:10 wakes a household."""
    follow_up = await _local_today(session, tenant) + timedelta(days=3)
    await _complete(session, encounter, doctor_user, follow_up)

    night = await _at(session, tenant, follow_up - timedelta(days=1), 2)
    assert (
        await service.send_follow_up_reminders(
            session, hospital_id=tenant.id, now=night, gateway=RecordingGateway()
        )
        == 0
    )


async def test_a_visit_that_ended_against_advice_is_not_reminded(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    """Only a COMPLETED visit has a follow-up to keep — the same gate as the
    closing message."""
    follow_up = await _local_today(session, tenant) + timedelta(days=3)
    encounter.follow_up_date = follow_up
    session.add(encounter)
    await clinical_service.record_lama(
        session, encounter, LamaRecord(lama_reason="Went home."), actor=doctor_user
    )
    await session.commit()

    eve = await _at(session, tenant, follow_up - timedelta(days=1), 10)
    assert (
        await service.send_follow_up_reminders(
            session, hospital_id=tenant.id, now=eve, gateway=RecordingGateway()
        )
        == 0
    )


async def test_a_patient_who_has_since_died_is_not_reminded(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    """CLAUDE.md §14. The visit completed cleanly; the death came later, on
    another visit. The reminder is held by the suppression at send time and the
    row says why — nothing reaches the family."""
    follow_up = await _local_today(session, tenant) + timedelta(days=3)
    await _complete(session, encounter, doctor_user, follow_up)

    later = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    await clinical_service.record_death(
        session,
        later,
        DeathRecord(
            deceased_at=datetime.now(later.started_at.tzinfo),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest.",
        ),
        actor=doctor_user,
    )
    await session.commit()

    gateway = RecordingGateway()
    eve = await _at(session, tenant, follow_up - timedelta(days=1), 10)
    await service.send_follow_up_reminders(session, hospital_id=tenant.id, now=eve, gateway=gateway)
    await session.commit()

    assert gateway.sent == []
    due = await _messages(session, tenant, patient, TemplateCode.FOLLOW_UP_DUE)
    assert all(row.status is NotificationStatus.SUPPRESSED for row in due)
    assert all(row.suppression_reason is SuppressionReason.DECEASED for row in due)
