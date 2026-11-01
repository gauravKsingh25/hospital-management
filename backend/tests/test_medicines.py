"""The medicine list, and what an inpatient stay is charged from it.

Three properties carry the weight, and each guards a failure a real ward has:

* **A preset is priced where every other price lives.** Adding "NS 500 ml,
  ₹45" writes the billing service and its price on the default rate card in
  the same transaction, so the list and the bill cannot disagree — and a
  medicine missing a price lands on the exception list at zero rather than
  borrowing some other drug's price.
* **A stay is charged for what it used, once.** A dose signed as given is
  charged `units_per_dose` times the list price; a refused dose and a free-typed
  drug are not. An item recorded as used is charged, and reversed it comes
  off — unless it is already on an issued invoice, when the reversal is
  refused whole rather than leaving the ward and the invoice disagreeing.
* **Each role holds its own half.** The admission desk keeps the list and
  prices it; a nurse records what was used but cannot price it; a doctor
  prescribes from it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError
from app.core.pagination import PageParams
from app.modules.billing import service as billing_service
from app.modules.billing.models import Charge, ChargeCategory, ChargeStatus, RateCard
from app.modules.billing.schemas import (
    InvoiceDraftRequest,
    RateCardCreate,
    ServiceItemCreate,
    ServicePriceUpsert,
)
from app.modules.clinical import service as clinical_service
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service
from app.modules.ipd.models import (
    Admission,
    BedClass,
    DischargeType,
    DoseStatus,
    MedicationRoute,
    MedicineForm,
    MedicineIssueStatus,
    MedicineItem,
)
from app.modules.ipd.schemas import (
    AdmissionCreate,
    BedCreate,
    DischargeRequest,
    DoseRecord,
    MedicationOrderCreate,
    MedicineIssueCreate,
    MedicineItemCreate,
    MedicineItemUpdate,
    WardCreate,
)
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

D = Decimal
MEDICINES = "/api/v1/ipd/medicines"
ADMISSIONS = "/api/v1/ipd/admissions"
ACCOUNTS = "/api/v1/billing/accounts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def nurse_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.NURSE)


@pytest.fixture
async def rate_card(session: AsyncSession, tenant: Hospital) -> RateCard:
    card = await billing_service.create_rate_card(
        session,
        RateCardCreate(code="CASH", name="Cash counter", payer_type="CASH", is_default=True),
        hospital_id=tenant.id,
    )
    await session.commit()
    return card


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Suresh Yadav", phone="9812377001", gender=Gender.MALE, age_years=58
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def admission(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Admission:
    ward = await service.create_ward(
        session,
        WardCreate(code="MW1", name="Male Medical", bed_class=BedClass.GENERAL),
        hospital_id=tenant.id,
    )
    bed = await service.create_bed(
        session, BedCreate(ward_id=ward.id, code="MW1-01"), hospital_id=tenant.id
    )
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    record = await service.admit_patient(
        session,
        AdmissionCreate(encounter_id=encounter.id, bed_id=bed.id, provisional_diagnosis="Sepsis"),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await session.commit()
    return record


async def _add(
    session: AsyncSession,
    tenant: Hospital,
    name: str,
    price: str,
    *,
    form: MedicineForm = MedicineForm.INJECTION,
    unit: str = "vial",
    dose: str | None = None,
) -> MedicineItem:
    item = await service.create_medicine_item(
        session,
        MedicineItemCreate(
            name=name,
            form=form,
            dose=dose,
            route=MedicationRoute.IV,
            unit=unit,
            price=D(price),
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return item


@pytest.fixture
async def ceftriaxone(session: AsyncSession, tenant: Hospital, rate_card: RateCard) -> MedicineItem:
    return await _add(session, tenant, "Ceftriaxone 1 g", "85.00", dose="1 g")


@pytest.fixture
async def iv_set(session: AsyncSession, tenant: Hospital, rate_card: RateCard) -> MedicineItem:
    return await _add(
        session, tenant, "IV infusion set", "40.00", form=MedicineForm.CONSUMABLE, unit="piece"
    )


async def _pharmacy_charges(
    session: AsyncSession, tenant: Hospital, encounter_id: uuid.UUID
) -> list[Charge]:
    rows, _ = await billing_service.list_charges(
        session, PageParams(limit=100), hospital_id=tenant.id, encounter_id=encounter_id
    )
    return [row for row in rows if row.category is ChargeCategory.PHARMACY]


async def _prescribe(
    session: AsyncSession,
    admission: Admission,
    doctor: User,
    *,
    item: MedicineItem | None,
    units: str = "1",
    drug: str = "Ceftriaxone",
) -> None:
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name=drug,
            dose="1 g",
            route=MedicationRoute.IV,
            frequency="BD",
            times_per_day=2,
            dose_times=["08:00", "20:00"],
            medicine_item_id=item.id if item else None,
            units_per_dose=D(units),
        ),
        actor=doctor,
    )
    await session.commit()


async def _due_doses(session: AsyncSession, tenant: Hospital, admission: Admission):  # type: ignore[no-untyped-def]
    doses, _ = await service.list_doses(
        session, PageParams(limit=20), hospital_id=tenant.id, admission_id=admission.id
    )
    return [dose for dose in doses if dose.status is DoseStatus.DUE]


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
class TestTheList:
    async def test_adding_an_item_prices_it_on_the_default_rate_card(
        self, session: AsyncSession, tenant: Hospital, ceftriaxone: MedicineItem
    ) -> None:
        assert ceftriaxone.code == "MED-CEFTRIAXONE-1-G"
        # The priced half, in billing, joined by the code.
        service_item = await billing_service.get_service_item_by_code(
            session, ceftriaxone.code, hospital_id=tenant.id
        )
        assert service_item is not None
        assert service_item.category is ChargeCategory.PHARMACY
        assert service_item.unit == "vial"
        assert await service.medicine_prices(
            session, [ceftriaxone.code], hospital_id=tenant.id
        ) == {ceftriaxone.code: D("85.00")}

    async def test_the_same_item_cannot_be_listed_twice_at_two_prices(
        self, session: AsyncSession, tenant: Hospital, ceftriaxone: MedicineItem
    ) -> None:
        with pytest.raises(ConflictError) as excinfo:
            await _add(session, tenant, "  ceftriaxone   1 G ", "90.00")
        assert excinfo.value.code == "medicine_exists"

    async def test_a_code_billing_already_uses_is_never_reused(
        self, session: AsyncSession, tenant: Hospital, rate_card: RateCard
    ) -> None:
        """Reusing it would quietly attach the new item to somebody else's price."""
        await billing_service.create_service_item(
            session,
            ServiceItemCreate(code="MED-NS-500-ML", name="Something else", category="OTHER"),
            hospital_id=tenant.id,
        )
        await session.commit()

        saline = await _add(session, tenant, "NS 500 ml", "45.00", form=MedicineForm.IV_FLUID)
        assert saline.code == "MED-NS-500-ML-2"

    async def test_without_a_default_price_list_nothing_is_saved(
        self, session: AsyncSession, tenant: Hospital
    ) -> None:
        """Refused whole: an item on the list with no price would charge nothing."""
        hospital_id = tenant.id
        with pytest.raises(ConflictError) as excinfo:
            await _add(session, tenant, "NS 500 ml", "45.00")
        assert excinfo.value.code == "no_default_rate_card"
        await session.rollback()
        await db.set_tenant_context(session, hospital_id)

        items, total = await service.list_medicine_items(
            session, PageParams(limit=10), hospital_id=hospital_id, include_inactive=True
        )
        assert total == 0 and items == []

    async def test_a_new_price_reaches_future_charges_only(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        nurse_user: User,
    ) -> None:
        await service.issue_medicine(
            session, admission, MedicineIssueCreate(medicine_item_id=iv_set.id), actor=nurse_user
        )
        await service.update_medicine_item(session, iv_set, MedicineItemUpdate(price=D("55.00")))
        await service.issue_medicine(
            session, admission, MedicineIssueCreate(medicine_item_id=iv_set.id), actor=nurse_user
        )
        await session.commit()

        charges = await _pharmacy_charges(session, tenant, admission.encounter_id)
        assert sorted(charge.unit_price for charge in charges) == [D("40.00"), D("55.00")]

    async def test_a_switched_off_item_cannot_be_used(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        nurse_user: User,
    ) -> None:
        await service.update_medicine_item(session, iv_set, MedicineItemUpdate(is_active=False))
        await session.commit()

        listed, _ = await service.list_medicine_items(
            session, PageParams(limit=10), hospital_id=tenant.id
        )
        assert iv_set.id not in {item.id for item in listed}
        with pytest.raises(ConflictError) as excinfo:
            await service.issue_medicine(
                session,
                admission,
                MedicineIssueCreate(medicine_item_id=iv_set.id),
                actor=nurse_user,
            )
        assert excinfo.value.code == "medicine_inactive"


# ---------------------------------------------------------------------------
# The chart: a dose given is a dose charged
# ---------------------------------------------------------------------------
class TestDosesAreCharged:
    async def test_a_given_dose_is_charged_at_the_list_price(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        ceftriaxone: MedicineItem,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        """A 2 g dose of a 1 g vial is two vials."""
        await _prescribe(session, admission, doctor_user, item=ceftriaxone, units="2")
        [first, *_] = await _due_doses(session, tenant, admission)

        await service.record_dose(
            session, first, DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
        )
        await session.commit()

        [charge] = await _pharmacy_charges(session, tenant, admission.encounter_id)
        assert charge.source_type == "dose" and charge.source_id == first.id
        assert charge.description == "Ceftriaxone 1 g"
        assert charge.quantity == D("2.00")
        assert charge.unit_price == D("85.00")
        assert charge.total_amount == D("170.00")
        assert not charge.needs_pricing

    async def test_a_refused_dose_is_not_charged(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        ceftriaxone: MedicineItem,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        await _prescribe(session, admission, doctor_user, item=ceftriaxone)
        [first, *_] = await _due_doses(session, tenant, admission)

        await service.record_dose(
            session,
            first,
            DoseRecord(status=DoseStatus.REFUSED, reason="Patient declined"),
            actor=nurse_user,
        )
        await session.commit()

        assert await _pharmacy_charges(session, tenant, admission.encounter_id) == []

    async def test_a_free_typed_drug_is_charted_but_not_charged(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        rate_card: RateCard,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        await _prescribe(session, admission, doctor_user, item=None, drug="Something unlisted")
        [first, *_] = await _due_doses(session, tenant, admission)

        await service.record_dose(
            session, first, DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
        )
        await session.commit()

        assert await _pharmacy_charges(session, tenant, admission.encounter_id) == []

    async def test_a_missing_price_is_flagged_rather_than_borrowed(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        ceftriaxone: MedicineItem,
        rate_card: RateCard,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        """The first pharmacy service on file is some other drug. Its price is
        not this one's — the charge goes to the exception list at zero.

        The hospital moves to a new default price list on which ceftriaxone has
        not been priced yet, while another drug has. (This used to switch the
        item off instead; a switched-off item now keeps its price for courses
        already running — see test_billing_service's
        `test_a_medicine_switched_off_mid_course_is_still_charged_at_its_price`.)
        """
        new_card = await billing_service.create_rate_card(
            session,
            RateCardCreate(code="CASH-2027", name="Cash 2027", payer_type="CASH", is_default=True),
            hospital_id=tenant.id,
        )
        other = await billing_service.create_service_item(
            session,
            ServiceItemCreate(code="AAA-FIRST", name="Another drug", category="PHARMACY"),
            hospital_id=tenant.id,
        )
        await billing_service.set_price(
            session, other, ServicePriceUpsert(rate_card_id=new_card.id, price=D("999.00"))
        )
        await session.commit()

        await _prescribe(session, admission, doctor_user, item=ceftriaxone)
        [first, *_] = await _due_doses(session, tenant, admission)
        await service.record_dose(
            session, first, DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
        )
        await session.commit()

        [charge] = await _pharmacy_charges(session, tenant, admission.encounter_id)
        assert charge.needs_pricing
        assert charge.unit_price == D("0.00")


# ---------------------------------------------------------------------------
# Items used outside the chart
# ---------------------------------------------------------------------------
class TestItemsUsed:
    async def test_an_item_used_goes_on_the_bill(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        nurse_user: User,
    ) -> None:
        issue = await service.issue_medicine(
            session,
            admission,
            MedicineIssueCreate(medicine_item_id=iv_set.id, quantity=2, note="Line re-sited"),
            actor=nurse_user,
        )
        await session.commit()

        assert issue.issued_by_name == nurse_user.full_name
        [charge] = await _pharmacy_charges(session, tenant, admission.encounter_id)
        assert (charge.source_type, charge.source_id) == ("issue", issue.id)
        assert charge.total_amount == D("80.00")

    async def test_reversing_an_entry_takes_it_off_the_bill(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        nurse_user: User,
    ) -> None:
        issue = await service.issue_medicine(
            session, admission, MedicineIssueCreate(medicine_item_id=iv_set.id), actor=nurse_user
        )
        reversed_issue = await service.reverse_medicine_issue(
            session, issue, reason="Wrong patient", actor=nurse_user
        )
        await session.commit()

        assert reversed_issue.status is MedicineIssueStatus.REVERSED
        [charge] = await _pharmacy_charges(session, tenant, admission.encounter_id)
        assert charge.status is ChargeStatus.CANCELLED

        with pytest.raises(ConflictError) as excinfo:
            await service.reverse_medicine_issue(session, issue, reason="Again")
        assert excinfo.value.code == "issue_already_reversed"

    async def test_an_invoiced_entry_cannot_be_reversed(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        nurse_user: User,
    ) -> None:
        """The ward's record and the patient's printout must tell one story."""
        cashier = await _user(session, tenant, Roles.CASHIER)
        issue = await service.issue_medicine(
            session, admission, MedicineIssueCreate(medicine_item_id=iv_set.id), actor=nurse_user
        )
        invoice = await billing_service.assemble_draft(
            session, InvoiceDraftRequest(encounter_id=admission.encounter_id), hospital_id=tenant.id
        )
        await billing_service.issue_invoice(session, invoice, actor=cashier, hospital_id=tenant.id)
        await session.commit()
        hospital_id, issue_id = tenant.id, issue.id

        with pytest.raises(ConflictError) as excinfo:
            await service.reverse_medicine_issue(
                session, issue, reason="Not used", actor=nurse_user
            )
        assert excinfo.value.code == "charge_invoiced"
        await session.rollback()
        await db.set_tenant_context(session, hospital_id)

        kept = await service.get_medicine_issue(session, issue_id, hospital_id=hospital_id)
        assert kept.status is MedicineIssueStatus.ISSUED

    async def test_nothing_is_recorded_against_a_finished_stay(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        iv_set: MedicineItem,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        await service.discharge_patient(
            session,
            admission,
            DischargeRequest(discharge_type=DischargeType.RECOVERED),
            actor=doctor_user,
        )
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.issue_medicine(
                session,
                admission,
                MedicineIssueCreate(medicine_item_id=iv_set.id),
                actor=nurse_user,
            )
        assert excinfo.value.code == "admission_closed"


# ---------------------------------------------------------------------------
# The cost of a stay
# ---------------------------------------------------------------------------
class TestTheCostOfAStay:
    async def test_costs_are_grouped_by_item_and_skip_what_nobody_owes(
        self,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        ceftriaxone: MedicineItem,
        iv_set: MedicineItem,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        await _prescribe(session, admission, doctor_user, item=ceftriaxone)
        for dose in (await _due_doses(session, tenant, admission))[:2]:
            await service.record_dose(
                session, dose, DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
            )
        await service.issue_medicine(
            session, admission, MedicineIssueCreate(medicine_item_id=iv_set.id), actor=nurse_user
        )
        mistake = await service.issue_medicine(
            session,
            admission,
            MedicineIssueCreate(medicine_item_id=iv_set.id, quantity=5),
            actor=nurse_user,
        )
        await service.reverse_medicine_issue(session, mistake, reason="Typed 5, meant 1")
        await session.commit()

        breakdown = await billing_service.cost_breakdown(
            session, admission.encounter_id, hospital_id=tenant.id
        )
        medicines = {
            line.description: (line.quantity, line.total_amount)
            for line in breakdown.lines
            if line.category is ChargeCategory.PHARMACY
        }
        # Two doses are one line of two; the reversed five are not a cost.
        assert medicines == {
            "Ceftriaxone 1 g": (D("2.00"), D("170.00")),
            "IV infusion set": (D("1.00"), D("40.00")),
        }
        assert breakdown.total == sum((line.total_amount for line in breakdown.lines), D("0"))


# ---------------------------------------------------------------------------
# Over HTTP: who may do what
# ---------------------------------------------------------------------------
class TestOverHttp:
    async def test_the_desk_keeps_the_list_and_the_ward_records_what_was_used(
        self,
        api: AsyncClient,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        rate_card: RateCard,
        doctor_user: User,
        nurse_user: User,
    ) -> None:
        desk = await _user(session, tenant, Roles.ADMISSION_DESK)
        desk_token = await login(api, desk)
        nurse_token = await login(api, nurse_user)
        doctor_token = await login(api, doctor_user)

        # The desk adds an IV fluid with its price…
        created = await api.post(
            MEDICINES,
            json={
                "name": "RL 500 ml",
                "form": "IV_FLUID",
                "dose": "500 ml",
                "route": "IV",
                "unit": "bottle",
                "price": "55.00",
            },
            headers=auth(desk_token),
        )
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["price"] == "55.00"
        assert item["code"] == "MED-RL-500-ML"

        # …and changes it. A nurse can do neither.
        repriced = await api.patch(
            f"{MEDICINES}/{item['id']}", json={"price": "60.00"}, headers=auth(desk_token)
        )
        assert repriced.status_code == 200, repriced.text
        assert repriced.json()["price"] == "60.00"
        refused = await api.post(
            MEDICINES,
            json={"name": "X", "form": "OTHER", "unit": "piece", "price": "1.00"},
            headers=auth(nurse_token),
        )
        assert refused.status_code == 403, refused.text

        # Everybody on the ward reads the list, with prices.
        listed = await api.get(MEDICINES, headers=auth(doctor_token))
        assert listed.status_code == 200, listed.text
        assert [(row["name"], row["price"]) for row in listed.json()["items"]] == [
            ("RL 500 ml", "60.00")
        ]

        # The nurse records two bottles used; the doctor cannot.
        issues = f"{ADMISSIONS}/{admission.id}/medicine-issues"
        issued = await api.post(
            issues, json={"medicine_item_id": item["id"], "quantity": 2}, headers=auth(nurse_token)
        )
        assert issued.status_code == 201, issued.text
        assert issued.json()["status"] == "ISSUED"
        assert (
            await api.post(
                issues, json={"medicine_item_id": item["id"]}, headers=auth(doctor_token)
            )
        ).status_code == 403

        # The desk sees it on the stay, and in what the stay has cost.
        used = await api.get(issues, headers=auth(desk_token))
        assert [row["item_name"] for row in used.json()["items"]] == ["RL 500 ml"]
        costs = await api.get(
            f"{ACCOUNTS}/{admission.encounter_id}/costs", headers=auth(desk_token)
        )
        assert costs.status_code == 200, costs.text
        [line] = [row for row in costs.json()["lines"] if row["category"] == "PHARMACY"]
        assert (line["description"], line["quantity"], line["total_amount"]) == (
            "RL 500 ml",
            "2.00",
            "120.00",
        )

        # Reversed by the nurse, with a reason, and off the bill.
        reversed_ = await api.post(
            f"/api/v1/ipd/medicine-issues/{issued.json()['id']}/reverse",
            json={"reason": "Recorded against the wrong bed"},
            headers=auth(nurse_token),
        )
        assert reversed_.status_code == 200, reversed_.text
        costs = await api.get(
            f"{ACCOUNTS}/{admission.encounter_id}/costs", headers=auth(desk_token)
        )
        assert [row for row in costs.json()["lines"] if row["category"] == "PHARMACY"] == []

    async def test_the_chart_says_what_a_dose_costs(
        self,
        api: AsyncClient,
        session: AsyncSession,
        tenant: Hospital,
        admission: Admission,
        ceftriaxone: MedicineItem,
        doctor_user: User,
    ) -> None:
        token = await login(api, doctor_user)
        prescribed = await api.post(
            f"{ADMISSIONS}/{admission.id}/medications",
            json={
                "drug_name": "Ceftriaxone",
                "dose": "1 g",
                "route": "IV",
                "frequency": "BD",
                "times_per_day": 2,
                "medicine_item_id": str(ceftriaxone.id),
            },
            headers=auth(token),
        )
        assert prescribed.status_code == 201, prescribed.text
        body = prescribed.json()
        assert (body["item_code"], body["unit_price"], body["unit"], body["units_per_dose"]) == (
            ceftriaxone.code,
            "85.00",
            "vial",
            "1.00",
        )

        free_typed = await api.post(
            f"{ADMISSIONS}/{admission.id}/medications",
            json={"drug_name": "Unlisted", "dose": "5 mg", "frequency": "OD", "times_per_day": 1},
            headers=auth(token),
        )
        assert free_typed.status_code == 201, free_typed.text
        assert free_typed.json()["unit_price"] is None
        assert free_typed.json()["item_code"] is None

    async def test_an_unknown_encounter_has_no_costs(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, admission: Admission
    ) -> None:
        desk = await _user(session, tenant, Roles.ADMISSION_DESK)
        token = await login(api, desk)
        missing = await api.get(f"{ACCOUNTS}/{uuid.uuid4()}/costs", headers=auth(token))
        assert missing.status_code == 404, missing.text
