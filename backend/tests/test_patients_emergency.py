"""The unidentified emergency patient (UX-01).

An unconscious road-accident patient has no name and no mobile. Before this,
registration demanded both, so casualty would have typed fake ones — breaking
duplicate detection and sending messages to made-up numbers. These tests pin
the honest path: a record that says plainly "we do not know who this is yet",
and one guarded way to say who it was later.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as SchemaError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError, ValidationError
from app.core.pagination import PageParams
from app.modules.patients import service
from app.modules.patients.models import AlertSeverity, AlertType, Gender, Patient
from app.modules.patients.schemas import (
    AlertCreate,
    PatientIdentify,
    PatientRegister,
    PatientUpdate,
)
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _unknown(
    session: AsyncSession, tenant: Hospital, gender: Gender = Gender.MALE, age: int | None = 40
) -> Patient:
    patient = await service.register_unidentified(
        session, hospital_id=tenant.id, gender=gender, age_years=age
    )
    await session.commit()
    return patient


async def _known(
    session: AsyncSession, tenant: Hospital, name: str = "Ramesh Kumar", phone: str = "9812345670"
) -> Patient:
    patient, _ = await service.register_patient(
        session,
        PatientRegister(full_name=name, phone=phone, gender=Gender.MALE, age_years=52),
        hospital_id=tenant.id,
    )
    await session.commit()
    return patient


def _as_new(name: str = "Suresh Yadav", phone: str = "9898989898") -> PatientIdentify:
    return PatientIdentify(full_name=name, phone=phone, gender=Gender.MALE, age_years=38)


# ---------------------------------------------------------------------------
# Registering someone nobody can name
# ---------------------------------------------------------------------------
class TestEmergencyRegistration:
    async def test_an_unknown_patient_needs_no_name_and_no_phone(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient = await _unknown(session, tenant)

        assert patient.full_name == "Unknown Male EM-0001"
        assert patient.emergency_tag == "EM-0001"
        assert patient.phone is None
        assert patient.is_unidentified is True
        assert patient.identified_at is None
        # A real UHID all the same: the wristband, the samples and the bill
        # need one from the first minute.
        assert patient.uhid
        assert patient.birth_date is not None
        assert patient.birth_date_is_estimated is True

    async def test_the_name_says_what_little_is_known(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        woman = await _unknown(session, tenant, Gender.FEMALE)
        unclear = await _unknown(session, tenant, Gender.UNKNOWN, age=None)

        assert woman.full_name == "Unknown Female EM-0001"
        assert unclear.full_name == "Unknown Patient EM-0002"
        assert unclear.birth_date is None

    async def test_two_unknown_patients_can_be_told_apart(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A bus accident brings several at once; casualty shouts tags, not UHIDs."""
        first = await _unknown(session, tenant)
        second = await _unknown(session, tenant)

        assert (first.emergency_tag, second.emergency_tag) == ("EM-0001", "EM-0002")
        assert first.uhid != second.uhid

    async def test_emergency_tags_do_not_use_up_uhid_numbers(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        before = await _known(session, tenant, "Asha Rani", "9811111111")
        unknown = await _unknown(session, tenant)
        after = await _known(session, tenant, "Mohan Lal", "9822222222")

        numbers = [int(p.uhid.rsplit("-", 1)[1]) for p in (before, unknown, after)]
        assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]

    async def test_the_database_refuses_a_missing_phone_on_a_known_patient(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """No phone is only ever honest on a record that says it is unidentified."""
        session.add(
            Patient(
                hospital_id=tenant.id,
                uhid=f"X-{uuid.uuid4().hex[:8]}",
                full_name="Nobody",
                name_normalized="nobody",
                phone=None,
                is_unidentified=False,
                registered_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_unknown_patients_are_never_offered_as_duplicates(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await _unknown(session, tenant)

        matches = await service.find_duplicates(
            session, hospital_id=tenant.id, full_name="Unknown Male", phone="9812345670"
        )
        assert matches == []

    async def test_the_directory_can_list_only_the_unidentified(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        unknown = await _unknown(session, tenant)
        await _known(session, tenant)

        rows, total = await service.list_patients(
            session, PageParams(limit=20), hospital_id=tenant.id, unidentified=True
        )
        assert total == 1
        assert [row.id for row in rows] == [unknown.id]


# ---------------------------------------------------------------------------
# Saying who it was: a new person
# ---------------------------------------------------------------------------
class TestIdentifyAsNewPerson:
    async def test_the_real_details_replace_the_placeholder(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient = await _unknown(session, tenant)
        uhid = patient.uhid

        await service.identify_patient(
            session,
            patient,
            _as_new(phone="+91 98989-89898"),
            hospital_id=tenant.id,
        )
        await session.commit()

        assert patient.full_name == "Suresh Yadav"
        assert patient.name_normalized == "suresh yadav"
        assert patient.phone == "9898989898"
        assert patient.is_unidentified is False
        assert patient.identified_at is not None
        # Same record, same wristband: only the identity changed.
        assert patient.uhid == uhid
        assert patient.emergency_tag == "EM-0001"

    async def test_an_exact_match_is_offered_as_the_record_to_link(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Typing the details of somebody already registered would create a
        second history. The clash names the record so the screen can offer
        "this is them" instead."""
        known = await _known(session, tenant)
        patient = await _unknown(session, tenant)

        with pytest.raises(ConflictError) as raised:
            await service.identify_patient(
                session,
                patient,
                _as_new(name="Ramesh Kumar", phone="9812345670"),
                hospital_id=tenant.id,
            )
        assert raised.value.code == "patient_already_exists"
        assert raised.value.details["patient_id"] == str(known.id)

    async def test_a_patient_is_identified_only_once(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient = await _unknown(session, tenant)
        await service.identify_patient(session, patient, _as_new(), hospital_id=tenant.id)
        await session.commit()

        with pytest.raises(ConflictError) as raised:
            await service.identify_patient(
                session, patient, _as_new(name="Someone Else"), hospital_id=tenant.id
            )
        assert raised.value.code == "already_identified"

    def test_a_new_person_needs_the_four_registration_fields(self) -> None:
        with pytest.raises(SchemaError):
            PatientIdentify(full_name="Suresh Yadav", gender=Gender.MALE, age_years=38)
        with pytest.raises(SchemaError):
            PatientIdentify(full_name="Suresh Yadav", phone="9898989898", gender=Gender.MALE)
        with pytest.raises(SchemaError):
            PatientIdentify(phone="9898989898", gender=Gender.MALE, age_years=38)

    def test_linking_and_typing_details_are_not_mixed(self) -> None:
        """Which identity wins would otherwise be a guess."""
        with pytest.raises(SchemaError):
            PatientIdentify(existing_patient_id=uuid.uuid4(), full_name="Suresh Yadav")


# ---------------------------------------------------------------------------
# Saying who it was: somebody already registered
# ---------------------------------------------------------------------------
class TestIdentifyAsExistingPatient:
    async def test_the_old_record_folds_into_the_emergency_one(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        known = await _known(session, tenant)
        known.address_line1 = "12 Lanka Road"
        session.add(known)
        await service.record_alert(
            session,
            known,
            AlertCreate(
                alert_type=AlertType.DRUG_ALLERGY,
                label="Penicillin",
                severity=AlertSeverity.CRITICAL,
            ),
        )
        await session.commit()
        patient = await _unknown(session, tenant)
        emergency_uhid, old_uhid, old_id = patient.uhid, known.uhid, known.id

        await service.identify_patient(
            session,
            patient,
            PatientIdentify(existing_patient_id=known.id),
            hospital_id=tenant.id,
        )
        await session.commit()

        # The emergency record — the one the live visit, bed and bill point
        # at — takes the identity and stays the main record.
        assert patient.full_name == "Ramesh Kumar"
        assert patient.phone == "9812345670"
        assert patient.uhid == emergency_uhid
        assert patient.is_unidentified is False
        assert patient.address_line1 == "12 Lanka Road"
        # The allergy is the reason this matters at all.
        labels = [a.label for a in await service.get_patient_alerts(session, patient.id)]
        assert labels == ["Penicillin"]
        # The old card still opens the patient.
        assert (await service.get_patient(session, old_id)).id == patient.id
        resolved = await service.get_patient_by_uhid(session, old_uhid, hospital_id=tenant.id)
        assert resolved is not None and resolved.id == patient.id

    async def test_the_identity_is_then_held_by_the_emergency_record(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        known = await _known(session, tenant)
        patient = await _unknown(session, tenant)
        await service.identify_patient(
            session,
            patient,
            PatientIdentify(existing_patient_id=known.id),
            hospital_id=tenant.id,
        )
        await session.commit()

        with pytest.raises(ConflictError) as raised:
            await _known(session, tenant)
        assert raised.value.details["patient_id"] == str(patient.id)

    async def test_a_deceased_record_is_never_linked(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The merge carries a death across. Linking a dead record would mark
        the patient alive in casualty as dead — and silence every message."""
        known = await _known(session, tenant)
        await service.mark_deceased(session, known, occurred_at=datetime.now(UTC))
        await session.commit()
        patient = await _unknown(session, tenant)

        with pytest.raises(ConflictError) as raised:
            await service.identify_patient(
                session,
                patient,
                PatientIdentify(existing_patient_id=known.id),
                hospital_id=tenant.id,
            )
        assert raised.value.code == "patient_deceased"
        assert patient.is_deceased is False

    async def test_another_unknown_patient_is_not_an_identity(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        other = await _unknown(session, tenant)
        patient = await _unknown(session, tenant)

        with pytest.raises(ValidationError) as raised:
            await service.identify_patient(
                session,
                patient,
                PatientIdentify(existing_patient_id=other.id),
                hospital_id=tenant.id,
            )
        assert raised.value.code == "invalid_identity"

    async def test_a_record_cannot_be_identified_as_itself(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient = await _unknown(session, tenant)

        with pytest.raises(ValidationError) as raised:
            await service.identify_patient(
                session,
                patient,
                PatientIdentify(existing_patient_id=patient.id),
                hospital_id=tenant.id,
            )
        assert raised.value.code == "invalid_identity"

    async def test_a_merged_away_record_is_not_an_identity(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        survivor = await _known(session, tenant)
        duplicate = await _known(session, tenant, "Ramesh K", "9700000000")
        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()
        patient = await _unknown(session, tenant)

        with pytest.raises(ValidationError) as raised:
            await service.identify_patient(
                session,
                patient,
                PatientIdentify(existing_patient_id=duplicate.id),
                hospital_id=tenant.id,
            )
        assert raised.value.code == "invalid_identity"


# ---------------------------------------------------------------------------
# The side doors stay shut
# ---------------------------------------------------------------------------
class TestGuards:
    async def test_an_ordinary_edit_cannot_name_an_unknown_patient(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Otherwise the record would carry a real name with the unidentified
        flag still set — and messages suppressed for ever."""
        patient = await _unknown(session, tenant)

        for change in (PatientUpdate(full_name="Suresh Yadav"), PatientUpdate(phone="9898989898")):
            with pytest.raises(ValidationError) as raised:
                await service.update_patient(session, patient, change)
            assert raised.value.code == "identify_first"

    async def test_other_details_can_still_be_recorded(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The police constable's name and the place found go in long before
        anybody knows who the patient is."""
        patient = await _unknown(session, tenant)

        await service.update_patient(
            session, patient, PatientUpdate(guardian_name="HC Verma (Police)", city="Varanasi")
        )
        assert patient.guardian_name == "HC Verma (Police)"
        assert patient.is_unidentified is True

    async def test_the_general_merge_refuses_an_unknown_patient(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        known = await _known(session, tenant)
        patient = await _unknown(session, tenant)

        for survivor, duplicate in ((known, patient), (patient, known)):
            with pytest.raises(ValidationError) as raised:
                await service.merge_patients(
                    session, survivor=survivor, duplicate=duplicate, reason="Same"
                )
            assert raised.value.code == "use_identify"
