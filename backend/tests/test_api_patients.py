"""Patient endpoints over HTTP: RBAC, the 4-field form, audit, tenant scope."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients.models import Gender
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
AUDIT = "/api/v1/audit-logs"


def rapid(name: str = "Sunita Devi", phone: str = "9876543210", **extra: object) -> dict:
    """The whole registration form (CLAUDE.md §7b)."""
    return {
        "full_name": name,
        "phone": phone,
        "gender": Gender.FEMALE.value,
        "age_years": 34,
        **extra,
    }


@pytest.fixture
async def nurse(session: AsyncSession, hospital: Hospital) -> User:
    from tests.conftest import _make_user

    return await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)


@pytest.fixture
async def records_officer(session: AsyncSession, hospital: Hospital) -> User:
    """Reception does not hold `patient:merge`; records staff own the register."""
    from tests.conftest import _make_user

    return await _make_user(session, role_code=Roles.RECORDS_OFFICER, hospital_id=hospital.id)


class TestRegistrationEndpoint:
    async def test_reception_registers_with_four_fields(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        response = await api.post(PATIENTS, json=rapid(), headers=auth(token))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["uhid"]
        assert body["full_name"] == "Sunita Devi"
        # The safety banner is part of the record from the first moment.
        assert body["alerts"] == []

    async def test_a_nurse_cannot_register_a_patient(self, api: AsyncClient, nurse: User) -> None:
        """Registration is reception's job — doctors and nurses stay clinical."""
        token = await login(api, nurse)
        response = await api.post(PATIENTS, json=rapid(), headers=auth(token))
        assert response.status_code == 403

    async def test_a_nurse_can_read_and_flag_a_patient(
        self, api: AsyncClient, receptionist: User, nurse: User
    ) -> None:
        """A nurse who learns of an allergy must not have to find an admin."""
        reception_token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(reception_token))).json()

        nurse_token = await login(api, nurse)
        read = await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(nurse_token))
        assert read.status_code == 200

        flagged = await api.post(
            f"{PATIENTS}/{patient['id']}/alerts",
            json={
                "alert_type": "DRUG_ALLERGY",
                "severity": "CRITICAL",
                "label": "Penicillin",
            },
            headers=auth(nurse_token),
        )
        assert flagged.status_code == 201

    async def test_a_duplicate_returns_409_with_the_existing_uhid(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        first = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        second = await api.post(PATIENTS, json=rapid(), headers=auth(token))

        assert second.status_code == 409
        error = second.json()["error"]
        assert error["code"] == "patient_already_exists"
        # Enough for the client to offer "Use existing record" in one click.
        assert error["details"]["uhid"] == first["uhid"]

    async def test_a_family_on_one_mobile_registers_and_warns(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        await api.post(PATIENTS, json=rapid(phone="9812345678"), headers=auth(token))

        child = await api.post(
            PATIENTS,
            json=rapid("Aarav Kumar", "9812345678", age_years=6),
            headers=auth(token),
        )

        assert child.status_code == 201
        warnings = child.json()["possible_duplicates"]
        assert warnings and "family" in warnings[0]["reason"].lower()

    async def test_registration_rejects_a_missing_age(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        payload = {"full_name": "No Age", "phone": "9000000001", "gender": "MALE"}
        response = await api.post(PATIENTS, json=payload, headers=auth(token))
        assert response.status_code == 422


class TestDuplicateCheckEndpoint:
    async def test_suggestions_while_typing(self, api: AsyncClient, receptionist: User) -> None:
        token = await login(api, receptionist)
        await api.post(PATIENTS, json=rapid(), headers=auth(token))

        response = await api.get(
            f"{PATIENTS}/check-duplicates",
            params={"full_name": "Sunita Devi", "phone": "9876543210"},
            headers=auth(token),
        )

        assert response.status_code == 200
        matches = response.json()
        assert matches[0]["score"] == 1.0
        assert matches[0]["reason"] == "Same name and mobile number"

    async def test_an_identity_collision_is_flagged_as_exact(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """`is_exact` is what tells a client to block rather than to warn.

        The service has always drawn this line — `DuplicateMatch.is_exact` —
        but the API schema dropped it, so every client saw a flat list and had
        to guess. The only safe guess is "block on anything", and blocking on
        a name resemblance in a hospital full of Sharmas and Kumars turns the
        confirmation into a box staff tick by reflex. A safety check everyone
        ticks without reading is worse than no check, because it looks like one.
        """
        token = await login(api, receptionist)
        await api.post(PATIENTS, json=rapid(), headers=auth(token))

        response = await api.get(
            f"{PATIENTS}/check-duplicates",
            params={"full_name": "Sunita Devi", "phone": "9876543210"},
            headers=auth(token),
        )

        assert response.json()[0]["is_exact"] is True

    async def test_a_shared_name_on_another_number_is_not_exact(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """Two people genuinely share a name. That is a hint, not a collision."""
        token = await login(api, receptionist)
        await api.post(PATIENTS, json=rapid(), headers=auth(token))

        response = await api.get(
            f"{PATIENTS}/check-duplicates",
            # Same name, different mobile — a returning patient who changed
            # number, or simply a different Sunita Devi.
            params={"full_name": "Sunita Devi", "phone": "9800000123"},
            headers=auth(token),
        )

        assert response.status_code == 200
        matches = response.json()
        assert matches, "a shared name should still be surfaced"
        assert all(match["is_exact"] is False for match in matches)

    async def test_a_shared_mobile_is_not_exact_either(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """A household shares one phone. Warn; never block."""
        token = await login(api, receptionist)
        await api.post(PATIENTS, json=rapid(), headers=auth(token))

        response = await api.get(
            f"{PATIENTS}/check-duplicates",
            params={"full_name": "Aarav Kumar", "phone": "9876543210"},
            headers=auth(token),
        )

        matches = response.json()
        assert matches and "family" in matches[0]["reason"].lower()
        assert matches[0]["is_exact"] is False


class TestSearchEndpoint:
    async def test_one_box_resolves_uhid_mobile_and_name(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """CLAUDE.md §7b: reception should never pick which field to search."""
        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        for term in (patient["uhid"], "9876543210", "sunita"):
            response = await api.get(f"{PATIENTS}/search", params={"q": term}, headers=auth(token))
            assert response.status_code == 200, term
            assert response.json()["total"] == 1, term

    async def test_search_is_paginated(self, api: AsyncClient, receptionist: User) -> None:
        token = await login(api, receptionist)
        for index in range(3):
            await api.post(
                PATIENTS,
                json=rapid(f"Ramesh Kumar {index}", f"98000000{index:02d}"),
                headers=auth(token),
            )

        response = await api.get(
            f"{PATIENTS}/search", params={"q": "ramesh", "limit": 2}, headers=auth(token)
        )
        body = response.json()
        assert set(body) == {"items", "total", "limit", "offset", "has_more"}
        assert len(body["items"]) == 2
        assert body["has_more"] is True
        assert body["total"] == 3


class TestScannedCardLookup:
    """`GET /patients/by-uhid/{uhid}` — CLAUDE.md §7b's QR-based lookup.

    The QR on a card carries the UHID as plain text, so a counter barcode
    scanner (which is a keyboard) resolves a patient with one physical action.
    This endpoint is what the scan lands on.
    """

    async def test_a_uhid_opens_the_record(self, api: AsyncClient, receptionist: User) -> None:
        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        response = await api.get(f"{PATIENTS}/by-uhid/{patient['uhid']}", headers=auth(token))

        assert response.status_code == 200, response.text
        assert response.json()["id"] == patient["id"]

    async def test_the_literal_route_is_not_swallowed_by_the_id_route(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """FastAPI matches routes in declaration order.

        Registered after `/{patient_id}`, `by-uhid` would be parsed as a
        malformed UUID and answered with 422 — a route that exists in the code
        and is unreachable over HTTP. Cheap to get wrong, invisible in a unit
        test of the service.
        """
        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        response = await api.get(f"{PATIENTS}/by-uhid/{patient['uhid']}", headers=auth(token))
        assert response.status_code != 422

    async def test_an_unknown_uhid_is_not_found(self, api: AsyncClient, receptionist: User) -> None:
        token = await login(api, receptionist)
        response = await api.get(f"{PATIENTS}/by-uhid/NOPE-99-000001", headers=auth(token))

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "patient_not_found"

    async def test_another_hospitals_card_is_indistinguishable_from_a_bad_one(
        self, api: AsyncClient, session: AsyncSession, receptionist: User
    ) -> None:
        """404, not 403.

        Confirming that a UHID exists somewhere else is itself a cross-tenant
        leak — someone holding a card would learn which hospital issued it.
        """
        import uuid

        from app.core import database as db
        from tests.conftest import _make_user

        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        async with db.system_context(session):
            other = Hospital(code=f"Q{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        outsider = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=other_id)
        outsider_token = await login(api, outsider)

        response = await api.get(
            f"{PATIENTS}/by-uhid/{patient['uhid']}", headers=auth(outsider_token)
        )
        assert response.status_code == 404

    async def test_a_scan_is_audited_even_when_it_finds_nothing(
        self, api: AsyncClient, hospital_admin: User, receptionist: User
    ) -> None:
        """An unrecognised card is exactly the event somebody investigates.

        Audited under its own action rather than `patient.view`: "who looked
        this person up, and how" has a different answer for a card presented at
        a counter than for a click on a worklist.
        """
        reception_token = await login(api, receptionist)
        await api.get(f"{PATIENTS}/by-uhid/NOPE-99-000001", headers=auth(reception_token))

        admin_token = await login(api, hospital_admin)
        entries = (
            await api.get(
                AUDIT, params={"action": "patient.lookup.uhid"}, headers=auth(admin_token)
            )
        ).json()

        assert entries["total"] == 1
        assert entries["items"][0]["changes"] == {"uhid": "NOPE-99-000001", "found": False}

    async def test_a_card_printed_before_a_merge_opens_the_surviving_record(
        self, api: AsyncClient, receptionist: User, nurse: User, records_officer: User
    ) -> None:
        """The reason this whole slice came first.

        A card outlives a merge. The losing record keeps its UHID and loses its
        allergies to the survivor, so resolving that UHID to the loser hands
        somebody a chart with no safety banner.
        """
        reception_token = await login(api, receptionist)
        nurse_token = await login(api, nurse)
        records_token = await login(api, records_officer)

        survivor = (
            await api.post(
                PATIENTS, json=rapid("Sunita Devi", "9812345678"), headers=auth(reception_token)
            )
        ).json()
        duplicate = (
            await api.post(
                PATIENTS, json=rapid("Sunita Devi", "9700000000"), headers=auth(reception_token)
            )
        ).json()

        await api.post(
            f"{PATIENTS}/{duplicate['id']}/alerts",
            json={"alert_type": "DRUG_ALLERGY", "severity": "CRITICAL", "label": "Penicillin"},
            headers=auth(nurse_token),
        )
        await api.post(
            f"{PATIENTS}/{survivor['id']}/merge",
            json={"duplicate_id": duplicate["id"], "reason": "Same person"},
            headers=auth(records_token),
        )

        response = await api.get(
            f"{PATIENTS}/by-uhid/{duplicate['uhid']}", headers=auth(reception_token)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == survivor["id"]
        # The assertion the patient's safety depends on.
        assert [alert["label"] for alert in body["alerts"]] == ["Penicillin"]


class TestSafetyBanner:
    async def test_alerts_come_back_on_the_patient_record(
        self, api: AsyncClient, receptionist: User, nurse: User
    ) -> None:
        reception_token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(reception_token))).json()

        nurse_token = await login(api, nurse)
        await api.post(
            f"{PATIENTS}/{patient['id']}/alerts",
            json={"alert_type": "DRUG_ALLERGY", "severity": "CRITICAL", "label": "Penicillin"},
            headers=auth(nurse_token),
        )

        detail = (
            await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(reception_token))
        ).json()
        assert [alert["label"] for alert in detail["alerts"]] == ["Penicillin"]
        assert detail["alerts"][0]["severity"] == "CRITICAL"


class TestConsent:
    async def test_consent_is_captured_against_the_patient(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        response = await api.post(
            f"{PATIENTS}/{patient['id']}/consents",
            json={
                "consent_type": "TREATMENT",
                "granted": True,
                "method": "THUMBPRINT",
                "language": "hi",
            },
            headers=auth(token),
        )

        assert response.status_code == 201
        assert response.json()["granted"] is True


class TestIdentifierAccess:
    async def test_a_receptionist_cannot_read_identifiers(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """Data minimisation: adding a scheme number is not the same right as
        reading everyone's Aadhaar."""
        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        response = await api.get(f"{PATIENTS}/{patient['id']}/identifiers", headers=auth(token))
        assert response.status_code == 403


class TestAuditing:
    async def test_viewing_a_patient_is_audited(
        self, api: AsyncClient, hospital_admin: User, receptionist: User
    ) -> None:
        """DPDP Act: access to personal data must be traceable, not just changes."""
        reception_token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(reception_token))).json()
        await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(reception_token))

        admin_token = await login(api, hospital_admin)
        entries = (
            await api.get(AUDIT, params={"action": "patient.view"}, headers=auth(admin_token))
        ).json()

        assert entries["total"] >= 1
        assert entries["items"][0]["actor_email"] == receptionist.email

    async def test_searching_is_audited_with_the_query(
        self, api: AsyncClient, hospital_admin: User, receptionist: User
    ) -> None:
        reception_token = await login(api, receptionist)
        await api.get(f"{PATIENTS}/search", params={"q": "sunita"}, headers=auth(reception_token))

        admin_token = await login(api, hospital_admin)
        entries = (
            await api.get(AUDIT, params={"action": "patient.search"}, headers=auth(admin_token))
        ).json()

        assert entries["total"] == 1
        assert entries["items"][0]["changes"]["query"] == "sunita"


class TestTenantScope:
    async def test_a_patient_is_not_visible_from_another_hospital(
        self, api: AsyncClient, session: AsyncSession, receptionist: User
    ) -> None:
        import uuid

        from app.core import database as db
        from tests.conftest import _make_user

        token = await login(api, receptionist)
        patient = (await api.post(PATIENTS, json=rapid(), headers=auth(token))).json()

        async with db.system_context(session):
            other = Hospital(code=f"R{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        outsider = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=other_id)
        outsider_token = await login(api, outsider)

        response = await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(outsider_token))
        assert response.status_code == 404
