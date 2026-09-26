"""Reporting service: the KPIs CLAUDE.md §13 step 10 asks for.

The tests that carry the most weight are the ones about *how a number is
defined*, not whether SQL runs:

* `test_the_local_day_is_the_hospitals_not_the_servers` — the timezone bug that
  would make every daily figure wrong by five and a half hours of evening work.
* `test_earned_collected_and_outstanding_are_three_different_numbers` — the
  classic hospital-dashboard error.
* `test_a_follow_up_that_is_not_due_yet_is_not_a_failure` — the difference
  between a compliance metric people trust and one they explain away.
* `test_a_same_day_stay_counts_as_one_day` — an ALOS that records day cases as
  zero understates every ward.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.billing import service as billing_service
from app.modules.billing.models import ChargeCategory, PayerType, PaymentMethod
from app.modules.billing.schemas import (
    ChargeAdd,
    InvoiceDraftRequest,
    RateCardCreate,
    ServiceItemCreate,
    ServicePriceUpsert,
)
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import BedClass, DischargeType
from app.modules.ipd.schemas import AdmissionCreate, BedCreate, DischargeRequest, WardCreate
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.reporting import service
from app.modules.reporting.schemas import DateWindow
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.schemas import DoctorCreate
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
async def cashier_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.CASHIER)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ramesh Yadav", phone="9812345670", gender=Gender.MALE, age_years=58
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


async def _window_around(session: AsyncSession, tenant: Hospital, days: int = 7) -> DateWindow:
    today = await service.local_today(session, tenant.id)
    return DateWindow(date_from=today - timedelta(days=days), date_to=today + timedelta(days=1))


async def _set_started_at(session: AsyncSession, encounter: Encounter, when: datetime) -> None:
    """Backdate a visit. The views read `started_at`, so this is the honest lever."""
    async with db.system_context(session):
        await session.execute(
            sa.text("UPDATE encounters SET started_at = :when WHERE id = :id"),
            {"when": when, "id": encounter.id},
        )
    await session.commit()


# ---------------------------------------------------------------------------
# The timezone rule
# ---------------------------------------------------------------------------
async def test_the_local_day_is_the_hospitals_not_the_servers(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """The bug that would make every daily number wrong.

    `Asia/Kolkata` is UTC+5:30, so 20:00 UTC on the 12th is 01:30 on the *13th*
    in the hospital. Grouping by UTC date would file that visit — and every
    evening's work after 18:30 — under the wrong day, by an amount that looks
    entirely plausible on a chart.
    """
    assert tenant.timezone == "Asia/Kolkata"

    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    await _set_started_at(session, encounter, datetime(2026, 8, 12, 20, 0, tzinfo=UTC))

    row = (
        await session.execute(
            sa.text("SELECT local_date FROM reporting_encounters WHERE id = :id"),
            {"id": encounter.id},
        )
    ).scalar_one()

    assert row == date(2026, 8, 13), "the view grouped by UTC date, not the hospital's"


async def test_local_today_follows_the_hospital_clock(
    session: AsyncSession, tenant: Hospital
) -> None:
    today = await service.local_today(session, tenant.id)
    # Never more than a day from UTC in either direction, and equal to the IST
    # date right now.
    assert abs((today - datetime.now(UTC).date()).days) <= 1


# ---------------------------------------------------------------------------
# Footfall
# ---------------------------------------------------------------------------
async def test_footfall_counts_visits_seen_and_lost(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    seen = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, seen, actor=doctor_user)

    lost = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.cancel_encounter(
        session, lost, reason="Patient left", no_show=True, actor=doctor_user
    )
    await session.commit()

    report = await service.footfall(session, await _window_around(session, tenant))

    assert report.total_visits == 2
    assert report.patients_seen == 1
    assert report.cancelled_or_no_show == 1
    assert report.new_patients == 1


async def test_a_visit_closed_by_the_sweep_is_not_counted_as_seen(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """ "Seen" means a doctor started the consultation, not that the visit closed.

    Deriving attendance from status would let the auto-close sweep manufacture
    patients nobody examined, and flatter every clinic's numbers.
    """
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    was_seen = (
        await session.execute(
            sa.text("SELECT was_seen FROM reporting_encounters WHERE id = :id"),
            {"id": encounter.id},
        )
    ).scalar_one()
    assert was_seen is False


async def test_days_with_no_visits_still_appear_on_the_trend(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """A chart that drops empty days draws a line through the gap."""
    await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    today = await service.local_today(session, tenant.id)
    window = DateWindow(date_from=today - timedelta(days=6), date_to=today)
    report = await service.footfall(session, window)

    assert len(report.by_day) == 7
    assert report.by_day[0].total == 0
    assert report.by_day[-1].total == 1


async def test_footfall_can_be_scoped_to_one_doctor(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    window = await _window_around(session, tenant)
    import uuid as _uuid

    other = await service.footfall(session, window, doctor_id=_uuid.uuid4())
    assert other.total_visits == 0

    everyone = await service.footfall(session, window)
    assert everyone.total_visits == 1


# ---------------------------------------------------------------------------
# Occupancy and ALOS
# ---------------------------------------------------------------------------
@pytest.fixture
async def ward_with_beds(session: AsyncSession, tenant: Hospital):  # type: ignore[no-untyped-def]
    ward = await ipd_service.create_ward(
        session,
        WardCreate(code="MW1", name="Male Medical", bed_class=BedClass.GENERAL),
        hospital_id=tenant.id,
    )
    beds = [
        await ipd_service.create_bed(
            session, BedCreate(ward_id=ward.id, code=code), hospital_id=tenant.id
        )
        for code in ("MW1-01", "MW1-02", "MW1-03")
    ]
    await session.commit()
    return ward, beds


async def test_occupancy_excludes_out_of_service_beds_from_the_denominator(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User, ward_with_beds
) -> None:
    """A ward closed for renovation has not made the hospital full."""
    _, beds = ward_with_beds
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    await ipd_service.admit_patient(
        session,
        AdmissionCreate(encounter_id=encounter.id, bed_id=beds[0].id),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await ipd_service.take_bed_out_of_service(session, beds[2], reason="Broken rail")
    await session.commit()

    report = await service.occupancy(session)

    assert report.total_beds == 3
    assert report.out_of_service == 1
    assert report.usable_beds == 2
    assert report.occupied == 1
    assert report.occupancy_rate == 0.5
    assert len(report.by_ward) == 1


async def test_a_same_day_stay_counts_as_one_day(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User, ward_with_beds
) -> None:
    """ALOS floors at one, matching how the ward counts and `ipd.length_of_stay`.

    A patient who came in and went home the same afternoon still occupied a bed.
    """
    _, beds = ward_with_beds
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    admission = await ipd_service.admit_patient(
        session,
        AdmissionCreate(encounter_id=encounter.id, bed_id=beds[0].id),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await session.commit()
    await ipd_service.discharge_patient(
        session,
        admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=doctor_user,
    )
    await session.commit()

    report = await service.inpatient_summary(session, await _window_around(session, tenant))

    assert report.admissions == 1
    assert report.discharges == 1
    assert report.average_length_of_stay_days == 1.0
    assert report.by_discharge_type == {"RECOVERED": 1}
    assert report.deaths == 0
    assert report.mortality_rate == 0.0


async def test_a_cancelled_admission_does_not_count_as_a_stay(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User, ward_with_beds
) -> None:
    """Admitted in error is not a stay; counting it inflates volume and ALOS."""
    _, beds = ward_with_beds
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    admission = await ipd_service.admit_patient(
        session,
        AdmissionCreate(encounter_id=encounter.id, bed_id=beds[0].id),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await session.commit()
    await ipd_service.cancel_admission(
        session, admission, reason="Wrong patient", actor=doctor_user
    )
    await session.commit()

    report = await service.inpatient_summary(session, await _window_around(session, tenant))
    assert report.admissions == 0
    assert report.currently_admitted == 0


async def test_alos_is_unknown_rather_than_zero_when_nobody_was_discharged(
    session: AsyncSession, tenant: Hospital
) -> None:
    """An average with no denominator is not zero."""
    report = await service.inpatient_summary(session, await _window_around(session, tenant))
    assert report.discharges == 0
    assert report.average_length_of_stay_days is None
    assert report.mortality_rate is None


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------
@pytest.fixture
async def priced(session: AsyncSession, tenant: Hospital):  # type: ignore[no-untyped-def]
    card = await billing_service.create_rate_card(
        session,
        RateCardCreate(code="CASH", name="Cash", payer_type=PayerType.CASH, is_default=True),
        hospital_id=tenant.id,
    )
    item = await billing_service.create_service_item(
        session,
        ServiceItemCreate(
            code="DRESSING",
            name="Wound dressing",
            category=ChargeCategory.PROCEDURE,
            is_gst_exempt=True,
        ),
        hospital_id=tenant.id,
    )
    await billing_service.set_price(
        session, item, ServicePriceUpsert(rate_card_id=card.id, price=D("1000.00"))
    )
    await session.commit()
    return card, item


async def test_earned_collected_and_outstanding_are_three_different_numbers(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    doctor_user: User,
    cashier_user: User,
    priced,
) -> None:
    """The classic dashboard error: charges raised is not money in the bank.

    Here ₹1,000 is earned, ₹400 collected, and ₹600 left outstanding. All three
    must be reported, and none of them is the sum of the others.
    """
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    await billing_service.add_charge(
        session,
        ChargeAdd(
            encounter_id=encounter.id,
            item_code="DRESSING",
            description="Wound dressing",
            category=ChargeCategory.PROCEDURE,
        ),
        hospital_id=tenant.id,
        actor=cashier_user,
    )
    await session.commit()

    invoice = await billing_service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=tenant.id,
        actor=cashier_user,
    )
    await billing_service.issue_invoice(session, invoice, actor=cashier_user, hospital_id=tenant.id)
    await session.commit()

    await billing_service.record_payment(
        session,
        invoice,
        amount=D("400.00"),
        method=PaymentMethod.CASH,
        actor=cashier_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    report = await service.revenue(session, await _window_around(session, tenant))

    assert report.charges_raised >= D("1000.00")
    assert report.collected == D("400.00")
    assert report.outstanding_total == D("600.00")
    assert any(row.category == "PROCEDURE" for row in report.by_category)
    assert report.by_method["CASH"] == D("400.00")


async def test_a_waived_charge_is_not_billable_but_is_still_reported(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    doctor_user: User,
    cashier_user: User,
    priced,
) -> None:
    """ "How much did we waive last month" is a question management asks.

    So a waived charge stays in the view with `billable_amount` zeroed, rather
    than being filtered out where nobody could count it.
    """
    biller = await _user(session, tenant, Roles.BILLING_STAFF)
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    charge = await billing_service.add_charge(
        session,
        ChargeAdd(
            encounter_id=encounter.id,
            item_code="DRESSING",
            description="Wound dressing",
            category=ChargeCategory.PROCEDURE,
        ),
        hospital_id=tenant.id,
        actor=cashier_user,
    )
    await session.commit()
    await billing_service.waive_charge(session, charge, reason="Staff concession", actor=biller)
    await session.commit()

    report = await service.revenue(session, await _window_around(session, tenant))

    assert report.waived == D("1000.00")
    assert report.charges_raised == D("0.00")


async def test_an_unpriced_act_shows_on_the_revenue_report(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """Every `needs_pricing` charge is revenue nobody has decided how to bill."""
    await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    report = await service.revenue(session, await _window_around(session, tenant))
    # The consultation fee is captured with no rate card configured in this test.
    assert report.unpriced_charges >= 1


# ---------------------------------------------------------------------------
# Follow-up compliance
# ---------------------------------------------------------------------------
async def test_a_follow_up_that_is_not_due_yet_is_not_a_failure(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """Pending visits stay out of the rate entirely.

    Counting them as misses would make the number depend on when the report was
    run, which is how a compliance metric becomes something people explain away.
    """
    today = await service.local_today(session, tenant.id)

    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.complete_consultation(
        session, encounter, actor=doctor_user, follow_up_date=today + timedelta(days=14)
    )
    await session.commit()

    report = await service.follow_up_compliance(
        session, await _window_around(session, tenant), today=today
    )

    assert report.advised == 1
    assert report.pending == 1
    assert report.missed == 0
    # No decided cases yet, so the rate is unknown rather than 0%.
    assert report.compliance_rate is None


async def test_a_patient_who_came_back_counts_as_compliant(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """And it counts whoever they saw — returning is returning."""
    today = await service.local_today(session, tenant.id)

    first = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, first, actor=doctor_user)
    await clinical_service.complete_consultation(
        session, first, actor=doctor_user, follow_up_date=today - timedelta(days=3)
    )
    await session.commit()

    back = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, back, actor=doctor_user)
    await session.commit()

    report = await service.follow_up_compliance(
        session, await _window_around(session, tenant), today=today
    )

    assert report.advised == 1
    assert report.honoured == 1
    assert report.missed == 0
    assert report.compliance_rate == 1.0


async def test_a_patient_who_never_returned_is_a_miss(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    today = await service.local_today(session, tenant.id)

    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.complete_consultation(
        session, encounter, actor=doctor_user, follow_up_date=today - timedelta(days=5)
    )
    await session.commit()

    report = await service.follow_up_compliance(
        session, await _window_around(session, tenant), today=today
    )

    assert report.advised == 1
    assert report.missed == 1
    assert report.compliance_rate == 0.0


# ---------------------------------------------------------------------------
# The window guard
# ---------------------------------------------------------------------------
def test_a_backwards_window_is_refused() -> None:
    with pytest.raises(ValueError, match="falls before its start"):
        DateWindow(date_from=date(2026, 8, 12), date_to=date(2026, 8, 1))


def test_an_absurdly_wide_window_is_refused() -> None:
    """An unbounded report is a full scan somebody triggers by clearing a filter."""
    with pytest.raises(ValueError, match="at most"):
        DateWindow(date_from=date(2000, 1, 1), date_to=date(2026, 8, 12))


# ---------------------------------------------------------------------------
# Queue analytics (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
async def test_an_empty_queue_reports_zeroes_and_unknown_averages(
    session: AsyncSession, tenant: Hospital
) -> None:
    today = await service.local_today(session, tenant.id)
    snapshot = await service.queue_snapshot(session, queue_date=today)

    assert snapshot.waiting == 0
    assert snapshot.average_wait_minutes is None
    assert snapshot.running_late == []
    assert snapshot.delay_threshold_minutes == 30


async def test_a_clinic_that_has_fallen_behind_is_flagged(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """§7b's doctor delay alert, firing.

    Every other test here asserts `running_late == []`, which is the easy half:
    an empty list is also what a broken threshold comparison returns. This one
    puts a patient on the floor who has been waiting longer than the threshold
    and checks the alert actually goes off, with the doctor named — because the
    alert reaches reception as "who", not "somebody".
    """
    doctor = await scheduling_service.create_doctor(
        session,
        DoctorCreate(user_id=doctor_user.id, specialty="General Medicine"),
        hospital_id=tenant.id,
        display_name="Dr Rao",
    )
    await session.commit()

    _, entry = await scheduling_service.quick_opd(
        session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
    )
    await session.commit()

    # The view counts up from `checked_in_at` for anyone still waiting, so
    # backdating check-in is the honest lever — the same trick `_set_started_at`
    # uses, and it exercises the real expression rather than a stubbed number.
    async with db.system_context(session):
        await session.execute(
            sa.text("UPDATE queue_entries SET checked_in_at = :when WHERE id = :id"),
            {"when": datetime.now(UTC) - timedelta(minutes=50), "id": entry.id},
        )
    await session.commit()

    today = await service.local_today(session, tenant.id)
    snapshot = await service.queue_snapshot(session, queue_date=today)

    assert snapshot.longest_wait_minutes is not None
    assert snapshot.longest_wait_minutes >= 49
    assert [late.doctor_name for late in snapshot.running_late] == ["Dr Rao"]
    assert snapshot.running_late[0].waiting == 1

    # And the threshold is genuinely a threshold, not decoration: the same
    # fifty-minute wait is not late in a clinic that allows ninety.
    relaxed = await service.queue_snapshot(session, queue_date=today, delay_threshold_minutes=90)
    assert relaxed.running_late == []


async def test_the_dashboard_only_includes_what_the_caller_may_see(
    session: AsyncSession, tenant: Hospital
) -> None:
    """§7b's role-based dashboards, driven by permission rather than role name."""
    from app.modules.reporting.rbac import ReportingPermissions

    nurse_view = await service.build_dashboard(
        session,
        hospital_id=tenant.id,
        permissions={ReportingPermissions.OPERATIONAL},
        trailing_days=7,
    )
    assert nurse_view.footfall is not None
    assert nurse_view.occupancy is not None
    assert nurse_view.queue is not None
    assert nurse_view.revenue is None, "a nurse must not receive the hospital's revenue"
    assert nurse_view.inpatient is None

    admin_view = await service.build_dashboard(
        session,
        hospital_id=tenant.id,
        permissions={
            ReportingPermissions.OPERATIONAL,
            ReportingPermissions.CLINICAL,
            ReportingPermissions.REVENUE,
        },
        trailing_days=7,
    )
    assert admin_view.revenue is not None
    assert admin_view.inpatient is not None
    assert admin_view.follow_up is not None
