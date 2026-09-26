"""Diagnostics service: the chain from order to a closed visit.

The test that matters most is `test_verifying_the_last_report_closes_the_visit`.
Everything else in this module exists to make that hop trustworthy — the sample
was really received, the value was really flagged against the right band, and
the person who signed was not the person who typed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.events import event_bus
from app.core.exceptions import (
    ConflictError,
    IllegalStateTransitionError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, Order, OrderStatus, OrderType
from app.modules.clinical.schemas import OrderCreate
from app.modules.diagnostics import service
from app.modules.diagnostics.events import CriticalResultFlagged, ReportAmended, ReportReady
from app.modules.diagnostics.models import (
    DiagnosticDiscipline,
    ReportStatus,
    ResultFlag,
    SpecimenStatus,
    SpecimenType,
)
from app.modules.diagnostics.models import (
    TestCatalogueItem as CatalogueItem,
)
from app.modules.diagnostics.schemas import (
    AccessionRequest,
    AmendRequest,
    AnalyteCreate,
    CatalogueItemCreate,
    CollectRequest,
    CriticalCallback,
    NarrativeEntry,
    ReferenceRangeCreate,
    ResultEntry,
    ResultsSubmission,
)
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration

D = Decimal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def tech_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.LAB_TECH)


@pytest.fixture
async def radiologist_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.RADIOLOGIST)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Sunita Devi", phone="9876543210", gender=Gender.FEMALE, age_years=34
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
    await clinical_service.start_consultation(session, record, actor=doctor_user)
    await session.commit()
    return record


@pytest.fixture
async def cbc(session: AsyncSession, tenant: Hospital) -> CatalogueItem:
    """A small panel with a sex-specific band and a panic limit."""
    item = await service.create_catalogue_item(
        session,
        CatalogueItemCreate(
            code="CBC",
            name="Complete Blood Count",
            discipline=DiagnosticDiscipline.LAB,
            section="Haematology",
            specimen_type=SpecimenType.BLOOD,
            container="EDTA (purple top)",
        ),
        hospital_id=tenant.id,
    )
    await service.add_analyte(
        session,
        item,
        AnalyteCreate(
            code="HB",
            name="Haemoglobin",
            unit="g/dL",
            display_order=1,
            ranges=[
                ReferenceRangeCreate(low=D("12.0"), high=D("17.0")),
                ReferenceRangeCreate(
                    sex="FEMALE", low=D("12.0"), high=D("15.0"), critical_low=D("7.0")
                ),
                ReferenceRangeCreate(sex="MALE", low=D("13.0"), high=D("17.0")),
            ],
        ),
    )
    await service.add_analyte(
        session,
        item,
        AnalyteCreate(
            code="WBC",
            name="Total Leucocyte Count",
            unit="/uL",
            decimal_places=0,
            display_order=2,
            ranges=[ReferenceRangeCreate(low=D("4000"), high=D("11000"))],
        ),
    )
    await session.commit()
    return item


@pytest.fixture
async def xray(session: AsyncSession, tenant: Hospital) -> CatalogueItem:
    item = await service.create_catalogue_item(
        session,
        CatalogueItemCreate(
            code="XR-CHEST",
            name="X-Ray Chest PA",
            discipline=DiagnosticDiscipline.RADIOLOGY,
            section="X-Ray",
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return item


async def _order(
    session: AsyncSession,
    encounter: Encounter,
    doctor_user: User,
    *,
    order_type: OrderType = OrderType.LAB,
    item_name: str = "CBC",
    review_in_visit: bool = False,
) -> Order:
    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=order_type, item_name=item_name, review_in_visit=review_in_visit),
        actor=doctor_user,
    )
    await session.commit()
    return order


async def _through_to_received(
    session: AsyncSession,
    order: Order,
    item: CatalogueItem,
    tech_user: User,
    tenant: Hospital,
):
    report, specimen = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=item.id),
        actor=tech_user,
        hospital_id=tenant.id,
    )
    await session.commit()
    assert specimen is not None
    await service.collect_specimen(session, specimen, CollectRequest(), actor=tech_user)
    await service.receive_specimen(session, specimen, actor=tech_user)
    await session.commit()
    return report, specimen


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
async def test_a_test_code_is_unique_per_hospital(
    session: AsyncSession, tenant: Hospital, cbc: CatalogueItem
) -> None:
    with pytest.raises(ConflictError) as error:
        await service.create_catalogue_item(
            session,
            CatalogueItemCreate(
                code="cbc",
                name="Another CBC",
                discipline=DiagnosticDiscipline.LAB,
                specimen_type=SpecimenType.BLOOD,
            ),
            hospital_id=tenant.id,
        )
    assert error.value.code == "catalogue_code_taken"


async def test_analytes_carry_their_reference_bands(
    session: AsyncSession, cbc: CatalogueItem
) -> None:
    analytes = await service.list_analytes(session, cbc.id)
    assert [a.code for a in analytes] == ["HB", "WBC"]

    bands = await service.list_reference_ranges(session, analytes[0].id)
    assert len(bands) == 3
    assert {band.sex for band in bands} == {None, "FEMALE", "MALE"}


# ---------------------------------------------------------------------------
# Accessioning
# ---------------------------------------------------------------------------
async def test_accessioning_creates_the_report_and_the_sample(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, specimen = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
        actor=tech_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    assert report.status is ReportStatus.REGISTERED
    assert report.report_number.startswith("RPT-")
    # Snapshotted: renaming the test next year must not rewrite this heading.
    assert report.test_name == "Complete Blood Count"

    assert specimen is not None
    assert specimen.accession_number.startswith("ACC-")
    assert specimen.status is SpecimenStatus.PENDING_COLLECTION
    assert specimen.container == "EDTA (purple top)"

    # The doctor's screen should now say the lab has it.
    assert order.status is OrderStatus.IN_PROGRESS


async def test_a_radiology_order_takes_no_sample(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    radiologist_user: User,
    xray: CatalogueItem,
) -> None:
    order = await _order(
        session, encounter, doctor_user, order_type=OrderType.RADIOLOGY, item_name="CXR"
    )
    report, specimen = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=xray.id),
        actor=radiologist_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    assert specimen is None
    assert report.specimen_id is None
    assert report.discipline is DiagnosticDiscipline.RADIOLOGY


async def test_a_lab_order_cannot_be_accessioned_as_a_scan(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    xray: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    with pytest.raises(ValidationError) as error:
        await service.accession_order(
            session,
            order,
            AccessionRequest(order_id=order.id, catalogue_item_id=xray.id),
            actor=tech_user,
            hospital_id=tenant.id,
        )
    assert error.value.code == "discipline_mismatch"


async def test_accessioning_twice_is_refused(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """A double-click is not a second sample."""
    order = await _order(session, encounter, doctor_user)
    await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
        actor=tech_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.accession_order(
            session,
            order,
            AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
            actor=tech_user,
            hospital_id=tenant.id,
        )
    assert error.value.code == "already_accessioned"


async def test_a_pharmacy_order_is_not_diagnostics_work(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(
        session, encounter, doctor_user, order_type=OrderType.PHARMACY, item_name="Paracetamol"
    )
    with pytest.raises(ValidationError) as error:
        await service.accession_order(
            session,
            order,
            AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
            actor=tech_user,
            hospital_id=tenant.id,
        )
    assert error.value.code == "not_a_diagnostic_order"


# ---------------------------------------------------------------------------
# The sample
# ---------------------------------------------------------------------------
async def test_results_cannot_be_entered_before_the_sample_arrives(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The accession step exists precisely so nobody types a result against a
    tube that is still in a nurse's hand."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
        actor=tech_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.enter_results(
            session,
            report,
            ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
            actor=tech_user,
        )
    assert error.value.code == "specimen_not_received"


async def test_a_rejected_sample_blocks_results_and_leaves_the_order_open(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The test was still asked for. A fresh sample is taken against the same
    order, so rejecting one must not make the work disappear."""
    order = await _order(session, encounter, doctor_user)
    report, specimen = await _through_to_received(session, order, cbc, tech_user, tenant)

    await service.reject_specimen(session, specimen, reason="Haemolysed sample.", actor=tech_user)
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.enter_results(
            session,
            report,
            ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
            actor=tech_user,
        )
    assert error.value.code == "specimen_rejected"

    refreshed = await clinical_service.get_order(session, order.id, hospital_id=tenant.id)
    assert refreshed.status is OrderStatus.IN_PROGRESS
    assert report.status is not ReportStatus.CANCELLED


async def test_an_uncollected_sample_cannot_be_received(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    _, specimen = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=cbc.id),
        actor=tech_user,
        hospital_id=tenant.id,
    )
    await session.commit()
    assert specimen is not None

    with pytest.raises(IllegalStateTransitionError):
        await service.receive_specimen(session, specimen, actor=tech_user)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
async def test_values_are_flagged_against_the_patients_own_band(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The patient is a 34-year-old woman, so 12.5 g/dL is normal — it would be
    low against the male band and against the catch-all."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    written = await service.enter_results(
        session,
        report,
        ResultsSubmission(
            results=[
                ResultEntry(analyte_code="HB", value_numeric=D("12.5")),
                ResultEntry(analyte_code="WBC", value_numeric=D("14000")),
            ]
        ),
        actor=tech_user,
    )
    await session.commit()

    by_code = {row.analyte_code: row for row in written}
    assert by_code["HB"].flag is ResultFlag.NORMAL
    assert by_code["WBC"].flag is ResultFlag.HIGH
    # The band used is copied onto the result, not looked up on read.
    assert by_code["HB"].ref_low == D("12.0000")
    assert by_code["HB"].ref_high == D("15.0000")


async def test_the_snapshotted_range_survives_the_catalogue_changing(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """A hospital that revises its ranges next year must not silently
    re-interpret a result issued today."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("12.5"))]),
        actor=tech_user,
    )
    await session.commit()

    analyte = next(a for a in await service.list_analytes(session, cbc.id) if a.code == "HB")
    for band in await service.list_reference_ranges(session, analyte.id):
        band.low = D("14.0")
        session.add(band)
    await session.commit()

    stored = await service.list_results(session, report.id)
    assert stored[0].ref_low == D("12.0000")
    assert stored[0].flag is ResultFlag.NORMAL


async def test_a_panic_value_flags_and_announces_itself(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """Fired on entry, not on verification: a haemoglobin of 5.2 does not wait
    for a signature."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    seen: list[CriticalResultFlagged] = []
    event_bus.subscribe(CriticalResultFlagged, seen.append)
    try:
        await service.enter_results(
            session,
            report,
            ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("5.2"))]),
            actor=tech_user,
        )
        await session.commit()
    finally:
        event_bus.clear()

    assert report.has_critical_result is True
    assert len(seen) == 1
    assert "Haemoglobin 5.2 g/dL" in seen[0].critical_values[0]


async def test_a_critical_callback_is_recorded(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """ "Called the ward" is not documentation. NABH wants to know who."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("5.2"))]),
        actor=tech_user,
    )
    await session.commit()

    updated = await service.acknowledge_critical(
        session, report, CriticalCallback(notified_to="Dr Rao, Medicine"), actor=tech_user
    )
    await session.commit()

    assert updated.critical_notified_to == "Dr Rao, Medicine"
    assert updated.critical_notified_at is not None
    assert updated.critical_notified_by_id == tech_user.id


async def test_acknowledging_a_report_with_no_critical_value_is_refused(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await session.commit()

    with pytest.raises(ValidationError) as error:
        await service.acknowledge_critical(
            session, report, CriticalCallback(notified_to="Dr Rao"), actor=tech_user
        )
    assert error.value.code == "no_critical_result"


async def test_a_partial_submission_does_not_clear_what_was_already_entered(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """A technician enters three analytes now and the rest when the machine
    finishes."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="WBC", value_numeric=D("8000"))]),
        actor=tech_user,
    )
    await session.commit()

    stored = {row.analyte_code: row for row in await service.list_results(session, report.id)}
    assert set(stored) == {"HB", "WBC"}


async def test_correcting_a_draft_value_updates_it_in_place(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("3.0"))]),
        actor=tech_user,
    )
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await session.commit()

    stored = await service.list_results(session, report.id)
    assert len(stored) == 1
    assert stored[0].value_numeric == D("13.0000")
    assert stored[0].flag is ResultFlag.NORMAL


async def test_an_analyte_that_is_not_part_of_the_test_is_refused(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    with pytest.raises(ValidationError) as error:
        await service.enter_results(
            session,
            report,
            ResultsSubmission(
                results=[ResultEntry(analyte_code="POTASSIUM", value_numeric=D("4.0"))]
            ),
            actor=tech_user,
        )
    assert error.value.code == "unknown_analyte"
    assert error.value.details["unknown"] == ["POTASSIUM"]


# ---------------------------------------------------------------------------
# Verification — the hop that closes the visit
# ---------------------------------------------------------------------------
async def test_verifying_the_last_report_closes_the_visit(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The whole point of the module, end to end (CLAUDE.md §6).

    The verifier never has to know they closed a visit — they signed a report.
    """
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()
    assert encounter.status is EncounterStatus.PENDING_CLEARANCE

    seen: list[ReportReady] = []
    event_bus.subscribe(ReportReady, seen.append)
    try:
        await service.verify_report(
            session,
            report,
            actor=doctor_user,
            hospital_id=tenant.id,
            registration_number="NMC-12345",
        )
        await session.commit()
    finally:
        event_bus.clear()

    assert report.status is ReportStatus.FINAL
    assert report.verified_by_name == doctor_user.full_name
    assert report.verifier_registration_number == "NMC-12345"

    cleared = await clinical_service.get_order(session, order.id, hospital_id=tenant.id)
    assert cleared.status is OrderStatus.COMPLETED
    assert cleared.fulfilment_ref == report.report_number

    refreshed = await clinical_service.get_encounter(session, encounter.id, hospital_id=tenant.id)
    assert refreshed.status is EncounterStatus.COMPLETED
    assert len(seen) == 1


async def test_a_review_result_sends_the_patient_back_to_the_doctor(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """`review_in_visit` on the order means the doctor sees them again, so
    verifying reopens the consultation rather than closing the visit."""
    order = await _order(session, encounter, doctor_user, review_in_visit=True)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()
    assert encounter.status is EncounterStatus.AWAITING_RESULTS

    await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)
    await session.commit()

    refreshed = await clinical_service.get_encounter(session, encounter.id, hospital_id=tenant.id)
    assert refreshed.status is EncounterStatus.IN_CONSULTATION


async def test_a_technician_cannot_verify_their_own_lab_result(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The second pair of eyes is the reason verification exists."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await session.commit()

    with pytest.raises(PermissionDeniedError) as error:
        await service.verify_report(session, report, actor=tech_user, hospital_id=tenant.id)
    assert error.value.code == "self_verification"


async def test_a_radiologist_signs_their_own_report(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    radiologist_user: User,
    xray: CatalogueItem,
) -> None:
    """Not a loophole — the radiologist who dictates the report is the one who
    signs it, and that is correct practice."""
    order = await _order(
        session, encounter, doctor_user, order_type=OrderType.RADIOLOGY, item_name="CXR"
    )
    report, _ = await service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=xray.id),
        actor=radiologist_user,
        hospital_id=tenant.id,
    )
    await service.enter_narrative(
        session,
        report,
        NarrativeEntry(findings="Clear lung fields.", impression="Normal study."),
        actor=radiologist_user,
    )
    await session.commit()

    verified = await service.verify_report(
        session, report, actor=radiologist_user, hospital_id=tenant.id
    )
    await session.commit()
    assert verified.status is ReportStatus.FINAL


async def test_an_empty_report_cannot_be_released(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)

    with pytest.raises(ValidationError) as error:
        await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)
    assert error.value.code == "report_empty"


async def test_a_preliminary_release_does_not_close_the_visit(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """A provisional number is something to look at, not to close a visit on."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    await service.release_preliminary(session, report, actor=tech_user)
    await session.commit()

    assert report.status is ReportStatus.PRELIMINARY
    cleared = await clinical_service.get_order(session, order.id, hospital_id=tenant.id)
    assert cleared.status is OrderStatus.IN_PROGRESS

    refreshed = await clinical_service.get_encounter(session, encounter.id, hospital_id=tenant.id)
    assert refreshed.status is EncounterStatus.PENDING_CLEARANCE


async def test_a_verified_report_cannot_be_edited(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.enter_results(
            session,
            report,
            ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("9.0"))]),
            actor=tech_user,
        )
    assert error.value.code == "report_not_editable"


def test_a_radiologist_cannot_countersign_a_blood_test() -> None:
    """Gated twice: the permission, then the discipline."""
    from app.modules.diagnostics.models import DiagnosticReport

    report = DiagnosticReport(
        hospital_id=uuid.uuid4(),
        report_number="RPT-26-000001",
        order_id=uuid.uuid4(),
        encounter_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        discipline=DiagnosticDiscipline.LAB,
        test_name="Complete Blood Count",
    )
    with pytest.raises(PermissionDeniedError) as error:
        service.assert_may_verify(report, frozenset({Roles.RADIOLOGIST}))
    assert error.value.code == "wrong_verification_role"

    service.assert_may_verify(report, frozenset({Roles.DOCTOR}))


# ---------------------------------------------------------------------------
# Amendment and cancellation
# ---------------------------------------------------------------------------
async def test_amending_supersedes_rather_than_rewrites(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """Somebody may already have treated the patient on the first version."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)
    await session.commit()

    seen: list[ReportAmended] = []
    event_bus.subscribe(ReportAmended, seen.append)
    try:
        replacement = await service.amend_report(
            session, report, AmendRequest(reason="Sample mix-up; re-run."), actor=doctor_user
        )
        await session.commit()
    finally:
        event_bus.clear()

    assert report.status is ReportStatus.AMENDED
    assert report.amendment_reason == "Sample mix-up; re-run."
    assert replacement.id != report.id
    assert replacement.amends_id == report.id
    assert replacement.status is ReportStatus.IN_PROGRESS
    # Values carried across so one number is corrected, not twelve retyped.
    carried = await service.list_results(session, replacement.id)
    assert [row.analyte_code for row in carried] == ["HB"]
    assert len(seen) == 1


async def test_the_live_report_for_an_order_follows_the_amendment(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """ "Which result is current?" must never be a judgement call."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)
    replacement = await service.amend_report(
        session, report, AmendRequest(reason="Re-run."), actor=doctor_user
    )
    await session.commit()

    live = await service.get_report_for_order(session, order.id, hospital_id=tenant.id)
    assert live is not None
    assert live.id == replacement.id


async def test_cancelling_a_study_releases_the_visit(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """An abandoned study that leaves its order outstanding is how an encounter
    sits in PENDING_CLEARANCE forever with nobody able to say why."""
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()
    assert encounter.status is EncounterStatus.PENDING_CLEARANCE

    await service.cancel_report(
        session,
        report,
        reason="Patient went home before the sample was run.",
        actor=doctor_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    cleared = await clinical_service.get_order(session, order.id, hospital_id=tenant.id)
    assert cleared.status is OrderStatus.CANCELLED

    refreshed = await clinical_service.get_encounter(session, encounter.id, hospital_id=tenant.id)
    assert refreshed.status is EncounterStatus.COMPLETED


# ---------------------------------------------------------------------------
# Worklists and isolation
# ---------------------------------------------------------------------------
async def test_critical_reports_sort_to_the_top_of_the_worklist(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    normal_order = await _order(session, encounter, doctor_user)
    normal, _ = await _through_to_received(session, normal_order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        normal,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )

    critical_order = await _order(session, encounter, doctor_user)
    critical, _ = await _through_to_received(session, critical_order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        critical,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("5.0"))]),
        actor=tech_user,
    )
    await session.commit()

    rows, total = await service.list_reports(session, PageParams(limit=50), hospital_id=tenant.id)
    assert total == 2
    assert rows[0].id == critical.id


async def test_another_hospitals_report_is_invisible(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """Row-level security, not a WHERE clause someone can forget."""
    from app.core.exceptions import NotFoundError

    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await session.commit()

    other = uuid.uuid4()
    await db.set_tenant_context(session, other)
    with pytest.raises(NotFoundError):
        await service.get_report(session, report.id, hospital_id=other)

    await db.set_tenant_context(session, tenant.id)
    assert (await service.get_report(session, report.id, hospital_id=tenant.id)).id == report.id


async def test_a_report_cannot_be_signed_against_a_cancelled_order(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    tech_user: User,
    cbc: CatalogueItem,
) -> None:
    """The doctor called the investigation off while it was on the bench.

    Phase 5 could only refuse the signature — the report stayed on the lab
    worklist and somebody had to spot it. Phase 6 gave the event bus a
    transactional channel, so `diagnostics.handlers` now withdraws the report
    with the order and the stale row never appears.

    The `order_cancelled` guard inside `verify_report` stays as defence in
    depth: it catches any future path that closes an order without publishing
    `OrderCancelled`, which is exactly the kind of thing a later phase adds
    without noticing.
    """
    order = await _order(session, encounter, doctor_user)
    report, _ = await _through_to_received(session, order, cbc, tech_user, tenant)
    await service.enter_results(
        session,
        report,
        ResultsSubmission(results=[ResultEntry(analyte_code="HB", value_numeric=D("13.0"))]),
        actor=tech_user,
    )
    await session.commit()

    await clinical_service.cancel_order(
        session, order, reason="Patient declined.", actor=doctor_user, hospital_id=tenant.id
    )
    await session.commit()

    withdrawn = await service.get_report(session, report.id, hospital_id=tenant.id)
    assert withdrawn.status is ReportStatus.CANCELLED
    assert withdrawn.cancellation_reason == "Patient declined."

    # And it is off the lab's open worklist entirely.
    _, still_open = await service.list_reports(
        session, PageParams(limit=50), hospital_id=tenant.id, open_only=True
    )
    assert still_open == 0

    # Signing is refused, now because the report itself is closed.
    with pytest.raises(IllegalStateTransitionError):
        await service.verify_report(session, report, actor=doctor_user, hospital_id=tenant.id)


def test_whoever_may_sign_a_report_may_also_correct_it() -> None:
    """A correction that needs an administrator is a correction that waits.

    Asserted against the role matrix rather than a live request, so the two
    permissions cannot drift apart unnoticed.
    """
    from app.modules.diagnostics.rbac import (
        MODULE_ROLE_PERMISSIONS,
        VERIFICATION_ROLES,
        DiagnosticsPermissions,
    )

    signers = {role for roles in VERIFICATION_ROLES.values() for role in roles}
    for role in signers:
        held = MODULE_ROLE_PERMISSIONS[role]
        assert DiagnosticsPermissions.RESULT_AMEND in held, role
        assert DiagnosticsPermissions.REPORT_CANCEL in held, role
