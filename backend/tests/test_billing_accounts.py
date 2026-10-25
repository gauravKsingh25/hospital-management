"""The accountant's side of billing: IPD bills, the final bill, the accounts view.

OPD bills are frozen the moment they are paid and belong to the counter. An IPD
stay runs for days, and its bill is the one that goes to the tax filing — so it
gets one owner, the accountant (and the hospital admin, who can do anything),
and nobody else may change it by hand.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError, ValidationError
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.core.rbac_registry import role_grants
from app.modules.billing import service
from app.modules.billing.models import (
    ChargeCategory,
    ChargeStatus,
    InvoiceScope,
    InvoiceStatus,
    PaymentMethod,
)
from app.modules.billing.schemas import (
    BillAdd,
    BillDiscount,
    BillEdit,
    BillRemove,
    ChargeAdd,
    InvoiceDraftRequest,
)
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import EncounterStatus
from app.modules.clinical.schemas import DeathRecord
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import DischargeType
from app.modules.ipd.schemas import DischargeRequest
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.models import Hospital
from app.modules.tenancy.schemas import HospitalUpdate
from tests import accounts_support as support
from tests.conftest import _make_user

D = support.D

_IPD_POWERS = {"ipd_bill:edit", "ipd_bill:finalise"}


def test_accountant_holds_exactly_the_ipd_billing_powers() -> None:
    grants = role_grants()

    accountant = grants[Roles.ACCOUNTANT]
    assert {
        "accounts:read",
        "ipd_bill:edit",
        "ipd_bill:finalise",
        "charge:read",
        "invoice:read",
        "payment:read",
    } <= accountant
    # Money is taken at the counter, not by the person who corrects the bill.
    assert "payment:record" not in accountant

    assert {"accounts:read", *_IPD_POWERS} <= grants[Roles.HOSPITAL_ADMIN]
    for role in (Roles.CASHIER, Roles.RECEPTIONIST, Roles.BILLING_STAFF, Roles.PHARMACIST):
        assert not (grants.get(role, set()) & _IPD_POWERS), role


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


@pytest.fixture
async def admin(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def stay(session: AsyncSession, tenant: Hospital, admin: User) -> support.Stay:
    return await support.admitted_stay(session, tenant.id, admin)


# ---------------------------------------------------------------------------
# Which visits are IPD stays — billing asks ipd, through the stay provider
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_stay_for_an_opd_visit_is_none(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    patient = await support.register(session, tenant.id)
    visit = await support.open_visit(session, tenant.id, patient, admin)

    assert await service.stay_for(session, visit.id, hospital_id=tenant.id) is None


@pytest.mark.integration
async def test_stay_for_an_admitted_visit_names_the_admission(
    session: AsyncSession, tenant: Hospital, stay: support.Stay
) -> None:
    info = await service.stay_for(session, stay.encounter.id, hospital_id=tenant.id)

    assert info is not None
    assert info.admission_id == stay.admission.id
    assert info.admission_number == stay.admission.admission_number
    assert info.status == "ADMITTED"
    assert info.discharged_at is None
    assert info.ward_bed is not None and info.ward_bed.startswith("General Ward, GW")


@pytest.mark.integration
async def test_collect_refuses_an_inpatient_stay(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """Reception's one-tap Paid must never issue an IPD bill behind the accountant."""
    with pytest.raises(ConflictError) as refused:
        await service.collect_balance(
            session, stay.encounter, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
        )
    assert refused.value.code == "ipd_bill_by_accounts"


@pytest.mark.integration
async def test_board_shows_no_balance_for_an_admitted_patient(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """The board's money chip is `balances_for`; a stay is absent, so the
    reception strip hides itself — there is nothing for the counter to take."""
    other = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id, name="Kavita"), admin
    )

    balances = await service.balances_for(
        session, [stay.encounter.id, other.id], hospital_id=tenant.id
    )

    assert stay.encounter.id not in balances
    assert balances[other.id].balance_due == D("500.00")


# ---------------------------------------------------------------------------
# The consultation before an admission is its own OPD bill
# ---------------------------------------------------------------------------
async def _invoices(session: AsyncSession, tenant: Hospital, encounter_id: uuid.UUID) -> list:
    rows, _ = await service.list_invoices(
        session, PageParams(), hospital_id=tenant.id, encounter_id=encounter_id
    )
    return rows


@pytest.mark.integration
async def test_admitting_from_an_open_visit_freezes_the_consultation_as_an_opd_bill(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    [opd] = await _invoices(session, tenant, stay.encounter.id)
    assert opd.scope is InvoiceScope.OPD
    assert opd.status is InvoiceStatus.ISSUED
    assert opd.grand_total == D("500.00")

    await service.add_charge(
        session,
        ChargeAdd(encounter_id=stay.encounter.id, service_item_code="DRESSING"),
        hospital_id=tenant.id,
        actor=admin,
    )
    draft = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=stay.encounter.id), hospital_id=tenant.id
    )
    await session.commit()

    assert draft.id != opd.id
    assert draft.scope is InvoiceScope.IPD
    lines, _ = await service.list_charges(
        session, PageParams(), hospital_id=tenant.id, invoice_id=draft.id
    )
    assert [line.description for line in lines] == ["Wound dressing"]


@pytest.mark.integration
async def test_an_already_paid_consultation_is_left_alone(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    patient = await support.register(session, tenant.id)
    visit = await support.open_visit(session, tenant.id, patient, admin)
    await service.collect_balance(
        session, visit, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
    )
    await session.commit()

    await support.admit(session, tenant.id, visit, admin)

    [paid] = await _invoices(session, tenant, visit.id)
    assert paid.status is InvoiceStatus.PAID
    assert paid.scope is InvoiceScope.OPD


@pytest.mark.integration
async def test_admitting_from_a_closed_visit_splits_nothing(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    patient = await support.register(session, tenant.id)
    visit = await support.open_visit(session, tenant.id, patient, admin)
    await service.collect_balance(
        session, visit, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
    )
    await clinical_service.complete_consultation(session, visit, actor=admin)
    await session.commit()
    assert visit.status is EncounterStatus.COMPLETED

    admission = await support.admit(session, tenant.id, visit, admin)

    assert admission.encounter_id != visit.id
    assert len(await _invoices(session, tenant, visit.id)) == 1
    assert await _invoices(session, tenant, admission.encounter_id) == []


# ---------------------------------------------------------------------------
# The IPD bill editor: every change at once, or none
# ---------------------------------------------------------------------------
def _add(code: str | None = None, **fields: object) -> BillAdd:
    return BillAdd(op="add", service_item_code=code, reason="Used on the ward", **fields)


async def _save(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay, *ops: object
) -> service.IpdBillData:
    bill = await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)
    await service.apply_bill_changes(
        session,
        stay.encounter,
        list(ops),
        expected_version=bill.version,
        actor=admin,
        hospital_id=tenant.id,
    )
    await session.commit()
    return await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)


def _line(bill: service.IpdBillData, description: str) -> uuid.UUID:
    [charge] = [charge for charge in bill.charges if charge.description == description]
    return charge.id


@pytest.mark.integration
async def test_save_applies_every_change_and_recalculates_total(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    before = await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)
    assert before.total == D("0.00")  # the consultation went to its own OPD bill

    bill = await _save(
        session,
        tenant,
        admin,
        stay,
        _add("DRESSING"),
        _add(
            description="Oxygen cylinder", category="OTHER", quantity=D("2"), unit_price=D("1000")
        ),
    )
    # 240 + 12% GST = 268.80, and 2 x 1000.
    assert bill.total == D("2268.80")

    bill = await _save(
        session,
        tenant,
        admin,
        stay,
        BillEdit(
            op="edit",
            charge_id=_line(bill, "Wound dressing"),
            quantity=D("2"),
            reason="Two dressings",
        ),
        BillDiscount(
            op="discount",
            charge_id=_line(bill, "Oxygen cylinder"),
            amount=D("100"),
            reason="Goodwill",
        ),
    )
    # 480 + 57.60 GST, and 2000 - 100.
    assert bill.total == D("2437.60")
    assert bill.version != before.version


@pytest.mark.integration
async def test_one_bad_change_applies_nothing(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    bill = await _save(session, tenant, admin, stay, _add("DRESSING"))
    dressing = _line(bill, "Wound dressing")

    with pytest.raises(ValidationError):
        await service.apply_bill_changes(
            session,
            stay.encounter,
            [
                BillEdit(op="edit", charge_id=dressing, quantity=D("3"), reason="Three dressings"),
                BillDiscount(op="discount", charge_id=dressing, amount=D("5000"), reason="Typo"),
            ],
            expected_version=bill.version,
            actor=admin,
            hospital_id=tenant.id,
        )
    hospital_id = tenant.id  # read before the rollback expires it
    await session.rollback()
    await db.set_tenant_context(session, hospital_id)
    await session.refresh(stay.encounter)

    after = await service.ipd_bill(session, stay.encounter, hospital_id=hospital_id)
    assert after.total == D("268.80")
    assert after.version == bill.version


@pytest.mark.integration
async def test_a_stale_save_is_refused(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """Two accountants on one stay: the second Save is refused, not merged."""
    loaded = await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)
    await _save(session, tenant, admin, stay, _add("DRESSING"))

    with pytest.raises(ConflictError) as refused:
        await service.apply_bill_changes(
            session,
            stay.encounter,
            [_add("DRESSING")],
            expected_version=loaded.version,
            actor=admin,
            hospital_id=tenant.id,
        )
    assert refused.value.code == "bill_changed"


@pytest.mark.integration
async def test_opd_visits_cannot_be_edited(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    visit = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id), admin
    )

    with pytest.raises(ConflictError) as refused:
        await service.apply_bill_changes(
            session,
            visit,
            [_add("DRESSING")],
            expected_version="",
            actor=admin,
            hospital_id=tenant.id,
        )
    assert refused.value.code == "opd_bill_locked"


@pytest.mark.integration
async def test_remove_waives_and_keeps_the_line(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    bill = await _save(session, tenant, admin, stay, _add("DRESSING"))

    bill = await _save(
        session,
        tenant,
        admin,
        stay,
        BillRemove(op="remove", charge_id=_line(bill, "Wound dressing"), reason="Not used"),
    )

    [line] = [charge for charge in bill.charges if charge.description == "Wound dressing"]
    assert line.status is ChargeStatus.WAIVED
    assert bill.total == D("0.00")


def test_every_change_needs_a_reason() -> None:
    with pytest.raises(PydanticValidationError):
        BillRemove(op="remove", charge_id=uuid.uuid4(), reason="")


# ---------------------------------------------------------------------------
# The final bill, after the patient has left
# ---------------------------------------------------------------------------
async def _discharge(session: AsyncSession, stay: support.Stay, admin: User) -> None:
    await ipd_service.discharge_patient(
        session,
        stay.admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=admin,
    )
    await session.commit()
    await session.refresh(stay.encounter)


@pytest.mark.integration
async def test_final_bill_is_refused_while_admitted(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    await _save(session, tenant, admin, stay, _add("DRESSING"))

    with pytest.raises(ConflictError) as refused:
        await service.finalise_ipd_bill(session, stay.encounter, actor=admin, hospital_id=tenant.id)
    assert refused.value.code == "stay_not_discharged"


@pytest.mark.integration
async def test_final_bill_after_discharge_issues_and_freezes(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    bill = await _save(session, tenant, admin, stay, _add("DRESSING"))
    await _discharge(session, stay, admin)
    assert (await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)).can_finalise

    invoice = await service.finalise_ipd_bill(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.ISSUED
    assert invoice.scope is InvoiceScope.IPD
    after = await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)
    assert after.final_invoice is not None and after.final_invoice.id == invoice.id
    assert not after.can_finalise

    with pytest.raises(ConflictError):
        await service.apply_bill_changes(
            session,
            stay.encounter,
            [BillRemove(op="remove", charge_id=_line(bill, "Wound dressing"), reason="Late")],
            expected_version=after.version,
            actor=admin,
            hospital_id=tenant.id,
        )


@pytest.mark.integration
async def test_final_bill_after_death_is_allowed(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """A death closes the visit without a discharge; the family still gets a bill."""
    await _save(session, tenant, admin, stay, _add("DRESSING"))
    doctor = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    await clinical_service.record_death(
        session,
        stay.encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor.id,
            cause_of_death="Cardiac arrest.",
        ),
        actor=doctor,
    )
    await session.commit()

    invoice = await service.finalise_ipd_bill(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )

    assert invoice.status is InvoiceStatus.ISSUED
    # The dressing, and the night the patient died in (accrued as the stay
    # closed): 268.80 + 2000, rounded to the rupee on the bill.
    assert invoice.grand_total == D("2269.00")


@pytest.mark.integration
async def test_final_bill_includes_the_last_night(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """Discharge accrues the room before the visit closes, so it is on the bill."""
    await _discharge(session, stay, admin)

    invoice = await service.finalise_ipd_bill(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )

    lines, _ = await service.list_charges(
        session, PageParams(), hospital_id=tenant.id, invoice_id=invoice.id
    )
    assert [line.category for line in lines] == [ChargeCategory.ROOM]
    assert invoice.grand_total == D("2000.00")


# ---------------------------------------------------------------------------
# The letterhead, as it was on the day of issue
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_reprint_uses_the_letterhead_of_the_issue_day(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    from tests.test_api_hospital_logo import JPEG, PNG

    await tenancy_service.save_logo(session, tenant.id, data=PNG, content_type="image/png")
    await tenancy_service.update_hospital(
        session, tenant.id, HospitalUpdate(address_line1="12 MG Road", gstin="09AAACH7409R1ZZ")
    )
    await session.commit()
    visit = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id), admin
    )
    await service.collect_balance(
        session, visit, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
    )
    await session.commit()
    [invoice] = await _invoices(session, tenant, visit.id)

    await tenancy_service.update_hospital(
        session, tenant.id, HospitalUpdate(address_line1="Plot 4, New Campus")
    )
    await tenancy_service.save_logo(session, tenant.id, data=JPEG, content_type="image/jpeg")
    await session.commit()
    await session.refresh(invoice)

    assert invoice.letterhead is not None
    assert invoice.letterhead["address_line1"] == "12 MG Road"
    assert invoice.letterhead["gstin"] == "09AAACH7409R1ZZ"
    assert invoice.letterhead["logo_version"] == 1
    live = await tenancy_service.letterhead_for(session, tenant.id)
    assert live["address_line1"] == "Plot 4, New Campus"
    assert live["logo_version"] == 2


# ---------------------------------------------------------------------------
# One live draft per visit, whatever its scope; the pre-admission OPD bill
# can always be billed again (final review #1, #2)
# ---------------------------------------------------------------------------
async def _cancel_pre_admission_bill(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    [opd] = await _invoices(session, tenant, stay.encounter.id)
    await service.cancel_invoice(
        session, opd, reason="Wrong consultation fee", actor=admin, hospital_id=tenant.id
    )
    await session.commit()


async def _pending(session: AsyncSession, tenant: Hospital, encounter_id: uuid.UUID) -> list:
    rows, _ = await service.list_charges(
        session,
        PageParams(),
        hospital_id=tenant.id,
        encounter_id=encounter_id,
        status=ChargeStatus.PENDING,
    )
    return rows


@pytest.mark.integration
async def test_a_cancelled_pre_admission_bill_can_be_issued_again(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    await _cancel_pre_admission_bill(session, tenant, admin, stay)

    reissued = await service.rebill_pre_admission(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )
    await session.commit()

    assert reissued.scope is InvoiceScope.OPD
    assert reissued.status is InvoiceStatus.ISSUED
    assert reissued.grand_total == D("500.00")
    assert await _pending(session, tenant, stay.encounter.id) == []


@pytest.mark.integration
async def test_rebilling_with_nothing_before_the_admission_is_refused(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    with pytest.raises(ConflictError) as refused:
        await service.rebill_pre_admission(
            session, stay.encounter, actor=admin, hospital_id=tenant.id
        )
    assert refused.value.code == "nothing_before_admission"


@pytest.mark.integration
async def test_the_final_bill_does_not_strand_a_cancelled_consultation(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """Cancelled and never re-issued: the stay's bill re-issues it as its own
    OPD bill rather than leaving it pending forever or folding it into IPD."""
    await _cancel_pre_admission_bill(session, tenant, admin, stay)
    await _discharge(session, stay, admin)

    final = await service.finalise_ipd_bill(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )
    await session.commit()

    assert final.grand_total == D("2000.00")
    issued_opd = [
        invoice
        for invoice in await _invoices(session, tenant, stay.encounter.id)
        if invoice.scope is InvoiceScope.OPD and invoice.status is InvoiceStatus.ISSUED
    ]
    assert [invoice.grand_total for invoice in issued_opd] == [D("500.00")]
    assert await _pending(session, tenant, stay.encounter.id) == []


@pytest.mark.integration
async def test_an_opd_draft_left_on_a_stay_does_not_block_its_bill(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """A stay admitted before this release: its draft was made before the
    admission, so it is OPD, and it may hold lines from after it too."""
    await _cancel_pre_admission_bill(session, tenant, admin, stay)
    await service.add_charge(
        session,
        ChargeAdd(encounter_id=stay.encounter.id, service_item_code="DRESSING"),
        hospital_id=tenant.id,
        actor=admin,
    )
    legacy = await service._new_draft(
        session,
        stay.encounter,
        InvoiceDraftRequest(encounter_id=stay.encounter.id),
        scope=InvoiceScope.OPD,
    )
    await service._gather_pending(session, legacy)
    await session.commit()

    draft = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=stay.encounter.id), hospital_id=tenant.id
    )
    await session.commit()

    assert draft.scope is InvoiceScope.IPD
    # The dressing alone, rounded to the rupee.
    assert draft.grand_total == D("269.00")
    await session.refresh(legacy)
    assert legacy.status is InvoiceStatus.ISSUED
    assert legacy.grand_total == D("500.00")


@pytest.mark.integration
async def test_a_cancelled_admission_turns_its_draft_back_into_the_opd_draft(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    await service.add_charge(
        session,
        ChargeAdd(encounter_id=stay.encounter.id, service_item_code="DRESSING"),
        hospital_id=tenant.id,
        actor=admin,
    )
    ipd_draft = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=stay.encounter.id), hospital_id=tenant.id
    )
    await session.commit()
    await ipd_service.cancel_admission(
        session, stay.admission, reason="Admitted the wrong patient", actor=admin
    )
    await session.commit()

    draft = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=stay.encounter.id), hospital_id=tenant.id
    )

    assert draft.id == ipd_draft.id
    assert draft.scope is InvoiceScope.OPD
