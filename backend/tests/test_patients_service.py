"""Patient service: identity, UHID, duplicates, alerts, consent, merge."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError, ValidationError
from app.core.pagination import PageParams
from app.modules.patients import service
from app.modules.patients.models import (
    AlertSeverity,
    AlertType,
    ConsentType,
    Gender,
)
from app.modules.patients.schemas import (
    AlertCreate,
    ConsentCreate,
    IdentifierCreate,
    PatientRegister,
    PatientUpdate,
)
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


def registration(
    name: str = "Sunita Devi",
    phone: str = "9876543210",
    *,
    age: int = 34,
    gender: Gender = Gender.FEMALE,
    **extra: object,
) -> PatientRegister:
    return PatientRegister(full_name=name, phone=phone, gender=gender, age_years=age, **extra)


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    """A hospital with the session bound to it, as a real request would be."""
    await db.set_tenant_context(session, hospital.id)
    return hospital


class TestRegistration:
    async def test_four_fields_are_enough(self, session: AsyncSession, tenant: Hospital) -> None:
        """CLAUDE.md §7b: name, mobile, approximate age, gender. Nothing else."""
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        assert patient.uhid
        assert patient.full_name == "Sunita Devi"
        assert patient.phone == "9876543210"
        assert patient.gender is Gender.FEMALE

    async def test_phone_is_stored_normalised(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(
            session, registration(phone="+91 98765-43210"), hospital_id=tenant.id
        )
        assert patient.phone == "9876543210"

    async def test_approximate_age_becomes_a_flagged_birth_date(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Storing only an age would rot; the estimate must stay marked as one."""
        patient, _ = await service.register_patient(
            session, registration(age=40), hospital_id=tenant.id
        )

        assert patient.birth_date is not None
        assert patient.birth_date_is_estimated is True
        assert patient.birth_date.year == datetime.now(UTC).year - 40

    async def test_an_exact_birth_date_is_not_flagged_as_estimated(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        from datetime import date

        payload = PatientRegister(
            full_name="Ramesh Kumar",
            phone="9000000001",
            gender=Gender.MALE,
            birth_date=date(1985, 4, 12),
        )
        patient, _ = await service.register_patient(session, payload, hospital_id=tenant.id)

        assert patient.birth_date_is_estimated is False

    async def test_registration_requires_an_age_or_a_birth_date(self) -> None:
        with pytest.raises(ValueError, match="age_years or birth_date"):
            PatientRegister(full_name="No Age", phone="9000000002", gender=Gender.MALE)


class TestIdentityRule:
    """name + mobile is the identity. Phone alone is not — families share one."""

    async def test_same_name_and_phone_is_refused_with_the_existing_uhid(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        first, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.register_patient(session, registration(), hospital_id=tenant.id)

        assert excinfo.value.code == "patient_already_exists"
        # The point of the error: reception reuses the record in one click.
        assert excinfo.value.details["uhid"] == first.uhid

    async def test_spelling_variations_of_the_same_name_still_collide(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.register_patient(
                session, registration(name="  SUNITA   devi "), hospital_id=tenant.id
            )

    async def test_honorific_variations_still_collide(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.register_patient(
                session, registration(name="Smt. Sunita Devi"), hospital_id=tenant.id
            )

    async def test_a_family_sharing_one_mobile_registers_fine(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The common case in India: one household phone, several patients."""
        household = "9812345678"
        mother, _ = await service.register_patient(
            session, registration("Sunita Devi", household), hospital_id=tenant.id
        )
        await session.commit()
        child, warnings = await service.register_patient(
            session,
            registration("Aarav Kumar", household, age=6, gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()

        assert mother.id != child.id
        assert child.uhid != mother.uhid
        # The family member is surfaced as a soft warning, never a block.
        assert any("family" in warning.reason.lower() for warning in warnings)

    async def test_the_same_name_on_a_different_phone_is_a_different_person(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await session.commit()
        second, _ = await service.register_patient(
            session, registration("Sunita Devi", "9898989898"), hospital_id=tenant.id
        )
        await session.commit()

        assert second.id is not None

    async def test_confirming_not_duplicate_explains_what_to_do_instead(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A genuine father/son collision needs a distinguishing detail, and the
        error has to say so rather than failing at the database."""
        await service.register_patient(
            session,
            registration("Ramesh Kumar", "9811111111", gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.register_patient(
                session,
                registration(
                    "Ramesh Kumar",
                    "9811111111",
                    gender=Gender.MALE,
                    confirm_not_duplicate=True,
                ),
                hospital_id=tenant.id,
            )
        assert excinfo.value.code == "patient_identity_collision"

    async def test_a_qualified_name_registers_the_second_person(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(
            session,
            registration("Ramesh Kumar", "9811111111", gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()

        junior, _ = await service.register_patient(
            session,
            registration("Ramesh Kumar S/O Suresh", "9811111111", age=19, gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()
        assert junior.uhid


class TestUhid:
    async def test_uhid_carries_the_hospital_code_and_year(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        code, year, serial = patient.uhid.split("-")

        assert code == tenant.code
        assert year == f"{datetime.now(UTC).year % 100:02d}"
        assert serial == "000001"

    async def test_uhids_increment(self, session: AsyncSession, tenant: Hospital) -> None:
        first, _ = await service.register_patient(
            session, registration("Patient One", "9000000011"), hospital_id=tenant.id
        )
        await session.commit()
        second, _ = await service.register_patient(
            session, registration("Patient Two", "9000000012"), hospital_id=tenant.id
        )
        await session.commit()

        assert first.uhid.endswith("000001")
        assert second.uhid.endswith("000002")

    async def test_concurrent_registrations_do_not_collide(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Two receptionists registering at once must not produce one UHID twice."""
        hospital_id = tenant.id

        async def register(index: int) -> str:
            async with db.session_scope() as own_session:
                await db.set_tenant_context(own_session, hospital_id)
                patient, _ = await service.register_patient(
                    own_session,
                    registration(f"Concurrent Patient {index}", f"90000001{index:02d}"),
                    hospital_id=hospital_id,
                )
                return patient.uhid

        await session.commit()
        uhids = await asyncio.gather(*(register(index) for index in range(5)))

        assert len(set(uhids)) == 5


class TestDuplicateDetection:
    async def test_a_misspelled_name_on_a_new_number_is_still_suggested(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The returning patient who changed phone — the case that silently
        creates a second medical history."""
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await session.commit()

        matches = await service.find_duplicates(
            session, hospital_id=tenant.id, full_name="Sunta Devi", phone="9999999999"
        )
        assert matches
        assert matches[0].reason == "Similar name"

    async def test_an_unrelated_name_and_number_suggests_nothing(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await session.commit()

        matches = await service.find_duplicates(
            session, hospital_id=tenant.id, full_name="Vikram Chaudhary", phone="9333333333"
        )
        assert matches == []

    async def test_matches_are_ranked_strongest_first(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await service.register_patient(
            session, registration("Sunita Sharma", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()

        matches = await service.find_duplicates(
            session, hospital_id=tenant.id, full_name="Sunita Devi", phone="9812345678"
        )
        assert matches[0].score == 1.0
        assert matches == sorted(matches, key=lambda m: m.score, reverse=True)


class TestSearch:
    async def test_search_by_uhid(self, session: AsyncSession, tenant: Hospital) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        results, total = await service.search_patients(
            session, patient.uhid, PageParams(), hospital_id=tenant.id
        )
        assert total == 1
        assert results[0].id == patient.id

    async def test_search_by_mobile(self, session: AsyncSession, tenant: Hospital) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        results, _ = await service.search_patients(
            session, "9876543210", PageParams(), hospital_id=tenant.id
        )
        assert len(results) == 1

    async def test_search_by_partial_name(self, session: AsyncSession, tenant: Hospital) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        results, _ = await service.search_patients(
            session, "sunita", PageParams(), hospital_id=tenant.id
        )
        assert len(results) == 1

    async def test_search_is_paginated(self, session: AsyncSession, tenant: Hospital) -> None:
        for index in range(4):
            await service.register_patient(
                session,
                registration(f"Ramesh Kumar {index}", f"98000000{index:02d}"),
                hospital_id=tenant.id,
            )
        await session.commit()

        page, total = await service.search_patients(
            session, "ramesh", PageParams(limit=2), hospital_id=tenant.id
        )
        assert len(page) == 2
        assert total == 4


class TestUpdates:
    async def test_changing_a_phone_number_works(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        updated = await service.update_patient(
            session, patient, PatientUpdate(phone="+91 90000 00099")
        )
        assert updated.phone == "9000000099"

    async def test_an_update_cannot_collide_with_another_patient(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        other, _ = await service.register_patient(
            session,
            registration("Vikram Singh", "9700000000", gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()

        with pytest.raises(ConflictError):
            await service.update_patient(
                session,
                other,
                PatientUpdate(full_name="Sunita Devi", phone="9812345678"),
            )

    async def test_omitted_fields_are_left_alone(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A PATCH must not silently wipe an address it never mentioned."""
        patient, _ = await service.register_patient(
            session, registration(city="Varanasi"), hospital_id=tenant.id
        )
        await session.commit()

        await service.update_patient(session, patient, PatientUpdate(occupation="Teacher"))
        assert patient.city == "Varanasi"


class TestSafetyAlerts:
    async def test_an_alert_is_recorded_and_returned(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await service.record_alert(
            session,
            patient,
            AlertCreate(
                alert_type=AlertType.DRUG_ALLERGY,
                severity=AlertSeverity.CRITICAL,
                label="Penicillin",
            ),
        )
        await session.commit()

        alerts = await service.get_patient_alerts(session, patient.id)
        assert [alert.label for alert in alerts] == ["Penicillin"]

    async def test_critical_alerts_sort_first(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The banner is read at a glance; the top line is the one that gets seen."""
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await service.record_alert(
            session,
            patient,
            AlertCreate(
                alert_type=AlertType.FALL_RISK,
                severity=AlertSeverity.MODERATE,
                label="Unsteady gait",
            ),
        )
        await service.record_alert(
            session,
            patient,
            AlertCreate(
                alert_type=AlertType.DRUG_ALLERGY,
                severity=AlertSeverity.CRITICAL,
                label="Penicillin",
            ),
        )
        await session.commit()

        alerts = await service.get_patient_alerts(session, patient.id)
        assert alerts[0].severity is AlertSeverity.CRITICAL

    async def test_resolving_an_alert_retires_it_rather_than_deleting(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """ "No longer believed" and "never recorded" are different clinical facts."""
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        alert = await service.record_alert(
            session,
            patient,
            AlertCreate(alert_type=AlertType.DRUG_ALLERGY, label="Penicillin"),
        )
        await session.commit()

        await service.resolve_alert(session, alert, reason="Allergy testing negative")
        await session.commit()

        assert await service.get_patient_alerts(session, patient.id) == []
        history = await service.get_patient_alerts(session, patient.id, active_only=False)
        assert history[0].resolved_reason == "Allergy testing negative"


class TestConsent:
    async def test_consent_is_recorded_with_language_and_method(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        consent = await service.record_consent(
            session,
            patient,
            ConsentCreate(
                consent_type=ConsentType.TREATMENT,
                granted=True,
                method="THUMBPRINT",
                language="hi",
            ),
        )
        await session.commit()

        assert consent.granted is True
        assert consent.language == "hi"
        assert await service.has_active_consent(session, patient.id, ConsentType.TREATMENT)

    async def test_an_explicit_refusal_is_recorded_not_just_absent(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await service.record_consent(
            session,
            patient,
            ConsentCreate(consent_type=ConsentType.RESEARCH, granted=False),
        )
        await session.commit()

        consents = await service.get_consents(session, patient.id)
        assert consents[0].granted is False
        assert not await service.has_active_consent(session, patient.id, ConsentType.RESEARCH)

    async def test_withdrawal_keeps_the_original_record(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A regulator will ask whether consent was never given or later withdrawn."""
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        consent = await service.record_consent(
            session,
            patient,
            ConsentCreate(consent_type=ConsentType.DATA_SHARING, granted=True),
        )
        await session.commit()

        await service.withdraw_consent(session, consent, reason="Patient asked us to stop")
        await session.commit()

        assert not await service.has_active_consent(session, patient.id, ConsentType.DATA_SHARING)
        stored = await service.get_consents(session, patient.id)
        assert stored[0].granted is True
        assert stored[0].withdrawn_at is not None


class TestIdentifiers:
    async def test_the_same_identifier_cannot_belong_to_two_patients(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        first, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        second, _ = await service.register_patient(
            session,
            registration("Vikram Singh", "9700000000", gender=Gender.MALE),
            hospital_id=tenant.id,
        )
        await session.commit()

        payload = IdentifierCreate(identifier_type="PMJAY", identifier_value="PM123456")
        await service.add_identifier(session, first, payload)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.add_identifier(session, second, payload)

    async def test_re_adding_the_same_identifier_is_idempotent(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        payload = IdentifierCreate(identifier_type="PMJAY", identifier_value="PM999")
        first = await service.add_identifier(session, patient, payload)
        await session.commit()
        second = await service.add_identifier(session, patient, payload)

        assert first.id == second.id


class TestMerge:
    async def test_merging_points_the_duplicate_at_the_survivor(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        assert duplicate.merged_into_id == survivor.id
        assert duplicate.is_active is False

    async def test_the_old_id_still_resolves_after_a_merge(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Printed cards and historical encounters still reference it."""
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()
        duplicate_id = duplicate.id

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        resolved = await service.get_patient(session, duplicate_id)
        assert resolved.id == survivor.id

    async def test_a_merged_uhid_resolves_to_the_survivor_with_its_alerts(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The card-shaped version of the merge rule.

        A UHID is printed on a card, an invoice and every lab report, months
        before anybody merges anything. When the record it names loses a merge
        its allergies move to the survivor — so resolving that UHID to the
        losing row hands somebody a chart with no safety banner. This is the
        test a scanned card depends on.
        """
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await service.record_alert(
            session,
            duplicate,
            AlertCreate(
                alert_type=AlertType.DRUG_ALLERGY,
                severity=AlertSeverity.CRITICAL,
                label="Penicillin",
            ),
        )
        await session.commit()
        old_uhid = duplicate.uhid

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        resolved = await service.get_patient_by_uhid(session, old_uhid, hospital_id=tenant.id)

        assert resolved is not None
        assert resolved.id == survivor.id
        alerts = await service.get_patient_alerts(session, resolved.id)
        assert [alert.label for alert in alerts] == ["Penicillin"]

    async def test_a_uhid_is_matched_exactly_not_as_a_substring(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A scanner sends a whole identifier, and it should mean only itself.

        The search box matches `ILIKE %term%` on purpose — somebody typing the
        last four digits should find the patient. A resolved scan must not,
        or one card would open another patient's record.
        """
        patient, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await session.commit()

        exact = await service.get_patient_by_uhid(session, patient.uhid, hospital_id=tenant.id)
        partial = await service.get_patient_by_uhid(
            session, patient.uhid[-4:], hospital_id=tenant.id
        )

        assert exact is not None and exact.id == patient.id
        assert partial is None

    async def test_a_uhid_survives_the_way_a_scanner_sends_it(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Lower case and stray whitespace both happen — a scanner suffix, a
        copy-paste, a receptionist typing it out. Neither is a wrong UHID."""
        patient, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await session.commit()

        messy = await service.get_patient_by_uhid(
            session, f"  {patient.uhid.lower()}  ", hospital_id=tenant.id
        )
        assert messy is not None and messy.id == patient.id

    async def test_a_merged_patient_is_not_offered_by_search(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Two rows for one person is how a receptionist picks the wrong one.

        `find_duplicates` and `list_patients` have always excluded merged
        records; search did not, which made the busiest path the leaky one.
        """
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        found, total = await service.search_patients(
            session, "Sunita Devi", PageParams(), hospital_id=tenant.id
        )

        assert total == 1
        assert [row.id for row in found] == [survivor.id]

    async def test_safety_alerts_survive_a_merge(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The failure mode this whole function exists to prevent: an allergy
        disappearing from the banner because it was on the losing record."""
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await service.record_alert(
            session,
            duplicate,
            AlertCreate(
                alert_type=AlertType.DRUG_ALLERGY,
                severity=AlertSeverity.CRITICAL,
                label="Penicillin",
            ),
        )
        await session.commit()

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        alerts = await service.get_patient_alerts(session, survivor.id)
        assert [alert.label for alert in alerts] == ["Penicillin"]

    async def test_blanks_on_the_survivor_are_filled_from_the_duplicate(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The losing record may hold the only address anyone ever captured."""
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session,
            registration("Sunita Devi", "9700000000", city="Varanasi"),
            hospital_id=tenant.id,
        )
        await session.commit()

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        assert survivor.city == "Varanasi"

    async def test_the_identity_pair_is_released_by_a_merge(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A merged record must not keep occupying a name+phone slot forever."""
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()
        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        reused, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()
        assert reused.id != duplicate.id

    async def test_a_patient_cannot_be_merged_into_itself(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        patient, _ = await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        with pytest.raises(ValidationError):
            await service.merge_patients(
                session, survivor=patient, duplicate=patient, reason="Oops"
            )

    async def test_merging_twice_is_refused(self, session: AsyncSession, tenant: Hospital) -> None:
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()
        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        with pytest.raises(ConflictError):
            await service.merge_patients(
                session, survivor=survivor, duplicate=duplicate, reason="Again"
            )

    async def test_a_recorded_death_survives_a_merge(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Suppressing follow-up messages depends on this flag (CLAUDE.md §14)."""
        survivor, _ = await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        duplicate, _ = await service.register_patient(
            session, registration("Sunita Devi", "9700000000"), hospital_id=tenant.id
        )
        await service.mark_deceased(session, duplicate, occurred_at=datetime.now(UTC))
        await session.commit()

        await service.merge_patients(
            session, survivor=survivor, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        assert survivor.is_deceased is True


class TestTenantIsolation:
    async def test_a_patient_is_invisible_to_another_hospital(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        async with db.system_context(session):
            other = Hospital(code=f"P{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        await db.set_tenant_context(session, other_id)
        patients, total = await service.list_patients(session, PageParams(), hospital_id=other_id)
        assert total == 0
        assert patients == []

    async def test_the_same_name_and_phone_may_exist_in_two_hospitals(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Uniqueness is per tenant: two hospitals are separate record systems."""
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        async with db.system_context(session):
            other = Hospital(code=f"Q{uuid.uuid4().hex[:5].upper()}", name="Second Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        await db.set_tenant_context(session, other_id)
        patient, _ = await service.register_patient(session, registration(), hospital_id=other_id)
        await session.commit()
        assert patient.uhid.startswith(other.code)


class TestFuzzySearchFallback:
    async def test_a_misspelled_name_still_finds_the_patient(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """A dead end at the counter is worse than an extra row to glance past."""
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        results, total = await service.search_patients(
            session, "Sunta Devi", PageParams(), hospital_id=tenant.id
        )
        assert total == 1
        assert results[0].full_name == "Sunita Devi"

    async def test_an_exact_match_does_not_pull_in_fuzzy_noise(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """The fallback only runs when the precise search found nothing."""
        await service.register_patient(
            session, registration("Sunita Devi", "9812345678"), hospital_id=tenant.id
        )
        await service.register_patient(
            session, registration("Sunita Sharma", "9700000000"), hospital_id=tenant.id
        )
        await session.commit()

        _, total = await service.search_patients(
            session, "Sunita Devi", PageParams(), hospital_id=tenant.id
        )
        assert total == 1

    async def test_a_wholly_unrelated_term_still_finds_nothing(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        await service.register_patient(session, registration(), hospital_id=tenant.id)
        await session.commit()

        _, total = await service.search_patients(
            session, "Vikram Chaudhary", PageParams(), hospital_id=tenant.id
        )
        assert total == 0
