"""Request/response DTOs for `ipd`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.clinical.schemas import SafetyBanner, VitalsCreate
from app.modules.ipd.models import (
    AdmissionRequestStatus,
    AdmissionStatus,
    BedClass,
    BedStatus,
    DischargeType,
    DoseStatus,
    DrugSchedule,
    MedicationRoute,
    MedicineForm,
    MedicineIssueStatus,
    SummaryStatus,
)

__all__ = [
    "AdmissionCreate",
    "AdmissionRead",
    "AdmissionRequestCancel",
    "AdmissionRequestCreate",
    "AdmissionRequestRead",
    "AdmissionSummary",
    "AdmissionUpdate",
    "AdmitFromRequest",
    "BedCreate",
    "BedRead",
    "BedUpdate",
    "BoardBed",
    "BoardWard",
    "CancelAdmission",
    "DischargeRequest",
    "DoseRecord",
    "MedicationOrderCreate",
    "MedicationOrderRead",
    "MedicationStop",
    "MedicineIssueCreate",
    "MedicineIssueRead",
    "MedicineIssueReverse",
    "MedicineItemCreate",
    "MedicineItemRead",
    "MedicineItemUpdate",
    "OccupancyStats",
    "OutOfServiceRequest",
    "ReserveRequest",
    "SignSummary",
    "SummaryRead",
    "SummaryUpdate",
    "TransferRequest",
    "WardCreate",
    "WardRead",
    "WardUpdate",
]

_TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# Rupees and paise, never a float — the same shape as billing's `Money`.
Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


# ---------------------------------------------------------------------------
# Wards and beds
# ---------------------------------------------------------------------------
class WardCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=16)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    floor: Annotated[str | None, Field(max_length=20)] = None
    bed_class: BedClass = BedClass.GENERAL
    department_id: uuid.UUID | None = None
    gender_policy: Annotated[str | None, Field(pattern=r"^(MALE|FEMALE)$")] = None
    phone_extension: Annotated[str | None, Field(max_length=16)] = None


class WardUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    floor: Annotated[str | None, Field(max_length=20)] = None
    department_id: uuid.UUID | None = None
    gender_policy: Annotated[str | None, Field(pattern=r"^(MALE|FEMALE)$")] = None
    phone_extension: Annotated[str | None, Field(max_length=16)] = None
    is_active: bool | None = None


class WardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    floor: str | None
    bed_class: BedClass
    department_id: uuid.UUID | None
    gender_policy: str | None
    phone_extension: str | None
    is_active: bool


class BedCreate(BaseModel):
    ward_id: uuid.UUID
    code: Annotated[str, Field(min_length=1, max_length=16)]
    label: Annotated[str | None, Field(max_length=60)] = None
    # Defaults to the ward's class when omitted — a bed is normally whatever its
    # ward is, and making staff retype it per bed is how a ward ends up with one
    # bed priced differently by accident.
    bed_class: BedClass | None = None
    tariff_item_code: Annotated[str | None, Field(max_length=32)] = None


class BedUpdate(BaseModel):
    label: Annotated[str | None, Field(max_length=60)] = None
    bed_class: BedClass | None = None
    tariff_item_code: Annotated[str | None, Field(max_length=32)] = None
    is_active: bool | None = None


class BedRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ward_id: uuid.UUID
    code: str
    label: str | None
    bed_class: BedClass
    status: BedStatus
    reserved_for_patient_id: uuid.UUID | None
    out_of_service_reason: str | None
    released_at: datetime | None
    tariff_item_code: str | None
    is_active: bool


class ReserveRequest(BaseModel):
    patient_id: uuid.UUID
    note: Annotated[str | None, Field(max_length=255)] = None


class OutOfServiceRequest(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class BoardBed(BaseModel):
    """One cell on the bed board."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    label: str | None
    bed_class: BedClass
    status: BedStatus
    patient_id: uuid.UUID | None = None
    patient_name: str | None = None
    uhid: str | None = None
    admission_id: uuid.UUID | None = None
    admitted_at: datetime | None = None


class BoardWard(BaseModel):
    """A ward and its beds, as the nursing station screen shows them."""

    id: uuid.UUID
    code: str
    name: str
    bed_class: BedClass
    gender_policy: str | None
    beds: list[BoardBed] = Field(default_factory=list)
    occupied: int = 0
    available: int = 0
    cleaning: int = 0


class OccupancyStats(BaseModel):
    """The number management asks for at 9am."""

    total_beds: int
    occupied: int
    available: int
    cleaning: int
    reserved: int
    out_of_service: int
    occupancy_rate: float = Field(description="Occupied / (total - out of service), 0-1.")


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
class AdmissionCreate(BaseModel):
    """Admit a patient. Deliberately short.

    An admission happens at 2am with a sick patient in a corridor. Everything
    that can be captured later is optional, on the same principle as CLAUDE.md
    §7b's four-field registration — the encounter and the bed are what matter,
    and a form that blocks on `expected_stay_days` is a form somebody works
    around.
    """

    encounter_id: uuid.UUID
    bed_id: uuid.UUID
    attending_doctor_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    provisional_diagnosis: Annotated[str | None, Field(max_length=500)] = None
    admission_notes: Annotated[str | None, Field(max_length=2000)] = None
    expected_stay_days: Annotated[int | None, Field(ge=0, le=365)] = None
    attendant_name: Annotated[str | None, Field(max_length=200)] = None
    attendant_phone: Annotated[str | None, Field(max_length=15)] = None
    attendant_relation: Annotated[str | None, Field(max_length=50)] = None
    admitted_at: datetime | None = None


class AdmissionRequestCreate(BaseModel):
    """Send a seen patient to the admission desk."""

    encounter_id: uuid.UUID
    note: Annotated[str | None, Field(max_length=500)] = None


class AdmissionRequestCancel(BaseModel):
    # Required: turning a patient away from admission is a decision someone
    # has to be able to explain afterwards.
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class AdmitFromRequest(BaseModel):
    """`AdmissionCreate` without the visit — the request already names it.

    `vitals` are the admission desk's set, taken at the counter before the
    patient goes up. Recorded on the stay's visit in the same transaction as
    the admission, so a stay never starts with vitals that failed to save.
    Optional, like every vitals field: a desk that could not measure something
    leaves it out rather than typing a zero.
    """

    bed_id: uuid.UUID
    attending_doctor_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    provisional_diagnosis: Annotated[str | None, Field(max_length=500)] = None
    admission_notes: Annotated[str | None, Field(max_length=2000)] = None
    expected_stay_days: Annotated[int | None, Field(ge=0, le=365)] = None
    attendant_name: Annotated[str | None, Field(max_length=200)] = None
    attendant_phone: Annotated[str | None, Field(max_length=15)] = None
    attendant_relation: Annotated[str | None, Field(max_length=50)] = None
    vitals: VitalsCreate | None = None


class AdmissionRequestRead(BaseModel):
    """A row on the admission desk, with the patient already named.

    Patient identity is attached by the router through `patients.service`, once
    for the whole page, for the same reason the queue board does it: a list of
    ids is not a worklist.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    encounter_id: uuid.UUID
    doctor_id: uuid.UUID | None
    department_id: uuid.UUID | None
    doctor_name: str | None
    status: AdmissionRequestStatus
    note: str | None
    requested_at: datetime
    requested_by_id: uuid.UUID | None
    requested_by_name: str | None
    handled_at: datetime | None
    handled_by_id: uuid.UUID | None
    admission_id: uuid.UUID | None
    cancellation_reason: str | None

    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    patient_phone: str | None = None


class AdmissionUpdate(BaseModel):
    attending_doctor_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    provisional_diagnosis: Annotated[str | None, Field(max_length=500)] = None
    admission_notes: Annotated[str | None, Field(max_length=2000)] = None
    expected_stay_days: Annotated[int | None, Field(ge=0, le=365)] = None
    attendant_name: Annotated[str | None, Field(max_length=200)] = None
    attendant_phone: Annotated[str | None, Field(max_length=15)] = None
    attendant_relation: Annotated[str | None, Field(max_length=50)] = None


class TransferRequest(BaseModel):
    to_bed_id: uuid.UUID
    reason: Annotated[str | None, Field(max_length=255)] = None


class DischargeRequest(BaseModel):
    """End the stay.

    `discharge_type` is required and has no default. A default would be
    `RECOVERED`, and a system that quietly records a death as a recovery because
    somebody did not change a dropdown is worse than one that asks.
    """

    discharge_type: DischargeType
    condition_at_discharge: Annotated[str | None, Field(max_length=1000)] = None
    follow_up_date: date | None = None
    follow_up_instructions: Annotated[str | None, Field(max_length=2000)] = None
    notes: Annotated[str | None, Field(max_length=1000)] = None

    @model_validator(mode="after")
    def _terminal_types_go_through_clinical(self) -> Self:
        """A death or a self-discharge is not recorded here.

        Both need the structured metadata CLAUDE.md §6 requires — who certified,
        the cause, whether the LAMA form was signed — and both are recorded
        against the Encounter through `clinical`. `ipd` then follows. Accepting
        them on this endpoint would create a second, thinner way to record a
        death, which is exactly the kind of second path §14 warns about.
        """
        if self.discharge_type in (DischargeType.DECEASED, DischargeType.LAMA):
            raise ValueError(
                f"A {self.discharge_type.value} outcome is recorded against the visit "
                "(death or LAMA entry), which discharges the admission automatically."
            )
        return self


class CancelAdmission(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class AdmissionSummary(BaseModel):
    """The census row — a ward list is long.

    Patient identity and the bed are attached by the router, in bulk. Without
    them the census is a list of UUIDs, which is not a census: the question a
    charge nurse opens it to answer is "who is in my ward and where", and both
    halves of that answer live in other tables.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admission_number: str
    patient_id: uuid.UUID
    encounter_id: uuid.UUID
    status: AdmissionStatus
    admitted_at: datetime
    discharged_at: datetime | None
    discharge_type: DischargeType | None
    attending_doctor_id: uuid.UUID | None
    bed_days_charged: int

    patient_name: str | None = None
    uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None

    bed_code: str | None = None
    ward_name: str | None = None


class AdmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admission_number: str
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    attending_doctor_id: uuid.UUID | None
    department_id: uuid.UUID | None
    status: AdmissionStatus

    admitted_at: datetime
    provisional_diagnosis: str | None
    admission_notes: str | None
    expected_stay_days: int | None

    attendant_name: str | None
    attendant_phone: str | None
    attendant_relation: str | None

    discharge_initiated_at: datetime | None
    discharged_at: datetime | None
    discharge_type: DischargeType | None
    cancellation_reason: str | None
    bed_days_charged: int

    # Filled by the router from the live assignment.
    bed: BedRead | None = None
    ward: WardRead | None = None
    length_of_stay_days: int = 0

    # And who this is. Carried for the same reason the diagnostic report
    # carries it: a screen where somebody transfers a bed or signs a discharge
    # has to name the patient it is about, on the screen, at the moment of the
    # act — not one request later.
    patient_name: str | None = None
    uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    patient_is_deceased: bool = False

    # The stay screen's header, in the same response: allergies and other
    # alerts (CLAUDE.md §7b), who is responsible, and whether the discharge
    # summary is written (DRAFT) or signed (SIGNED) — null when not started.
    safety_banner: SafetyBanner | None = None
    attending_doctor_name: str | None = None
    discharge_summary_status: str | None = None
    # "Ward, bed" of the latest assignment, released or not — what the printed
    # discharge summary names after the bed has been freed and `bed` is null.
    last_ward_bed: str | None = None


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The medicine list
# ---------------------------------------------------------------------------
class MedicineItemCreate(BaseModel):
    """A preset for the medicine list: what it is, how it is given, what it costs.

    Only the name, the form, the unit and the price are required. The price is
    required because an item on this list exists to be charged for; one with
    no price would charge the patient nothing and look as if it worked.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)]
    form: MedicineForm
    dose: Annotated[str | None, Field(max_length=60)] = None
    route: MedicationRoute | None = None
    unit: Annotated[str, Field(min_length=1, max_length=24)]
    drug_schedule: DrugSchedule = DrugSchedule.NONE
    price: Price


class MedicineItemUpdate(BaseModel):
    """Every field optional; `price` changes the default rate card's price."""

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    form: MedicineForm | None = None
    dose: Annotated[str | None, Field(max_length=60)] = None
    route: MedicationRoute | None = None
    unit: Annotated[str | None, Field(min_length=1, max_length=24)] = None
    drug_schedule: DrugSchedule | None = None
    price: Price | None = None
    is_active: bool | None = None


class MedicineItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    form: MedicineForm
    dose: str | None
    route: MedicationRoute | None
    unit: str
    drug_schedule: DrugSchedule
    is_active: bool
    # From billing's default rate card, attached by the router. None means the
    # item has no price there — shown as "not priced", never as zero.
    price: Decimal | None = None


class MedicineIssueCreate(BaseModel):
    medicine_item_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=999)] = 1
    note: Annotated[str | None, Field(max_length=255)] = None


class MedicineIssueReverse(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class MedicineIssueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admission_id: uuid.UUID
    medicine_item_id: uuid.UUID
    item_code: str
    item_name: str
    unit: str
    quantity: int
    note: str | None
    status: MedicineIssueStatus
    issued_at: datetime
    issued_by_name: str | None
    reversed_at: datetime | None
    reversal_reason: str | None


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
class MedicationOrderCreate(BaseModel):
    drug_name: Annotated[str, Field(min_length=1, max_length=200)]
    dose: Annotated[str, Field(min_length=1, max_length=60)]
    route: MedicationRoute = MedicationRoute.ORAL
    frequency: Annotated[str, Field(min_length=1, max_length=40)]
    times_per_day: Annotated[int, Field(ge=0, le=24)] = 1
    dose_times: list[Annotated[str, Field(pattern=_TIME_PATTERN)]] = Field(default_factory=list)
    is_prn: bool = False
    prn_indication: Annotated[str | None, Field(max_length=200)] = None
    drug_schedule: DrugSchedule = DrugSchedule.NONE
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    instructions: Annotated[str | None, Field(max_length=500)] = None
    # Picked from the medicine list: every dose given is then billed as
    # `units_per_dose` of that item. Left out, the drug is charted but not billed.
    medicine_item_id: uuid.UUID | None = None
    units_per_dose: Annotated[Decimal, Field(gt=0, le=100, max_digits=9, decimal_places=2)] = (
        Decimal("1")
    )

    @model_validator(mode="after")
    def _schedule_must_be_chartable(self) -> Self:
        if self.is_prn:
            if not self.prn_indication:
                raise ValueError("An as-needed drug needs an indication saying when to give it.")
            return self
        if self.times_per_day < 1:
            raise ValueError("A scheduled drug needs at least one dose time per day.")
        if self.dose_times and len(self.dose_times) != self.times_per_day:
            raise ValueError(
                f"{self.times_per_day} dose(s) per day were specified but "
                f"{len(self.dose_times)} time(s) were given."
            )
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValueError("The course cannot end before it starts.")
        return self


class MedicationStop(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class MedicationOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admission_id: uuid.UUID
    patient_id: uuid.UUID
    drug_name: str
    dose: str
    route: MedicationRoute
    frequency: str
    times_per_day: int
    dose_times: list[str]
    is_prn: bool
    prn_indication: str | None
    drug_schedule: DrugSchedule
    starts_at: datetime
    ends_at: datetime | None
    instructions: str | None
    prescribed_by_id: uuid.UUID | None
    is_active: bool
    stopped_at: datetime | None
    stop_reason: str | None
    medicine_item_id: uuid.UUID | None = None
    item_code: str | None = None
    units_per_dose: Decimal = Decimal("1")
    # What one unit costs and what the unit is, attached by the router from the
    # medicine list, so the chart can say "₹85 a vial, 1 vial a dose" beside
    # the drug. Both None for a drug that is not billed.
    unit_price: Decimal | None = None
    unit: str | None = None


class DoseRecord(BaseModel):
    """A nurse saying what happened to one dose.

    `reason` is required for everything except `GIVEN`. A blank reason on a
    missed antibiotic is precisely the gap an incident review cannot close, so
    it is refused at the boundary rather than nagged about later.
    """

    status: DoseStatus = DoseStatus.GIVEN
    administered_at: datetime | None = None
    reason: Annotated[str | None, Field(max_length=255)] = None
    notes: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def _not_given_needs_a_reason(self) -> Self:
        if self.status is DoseStatus.DUE:
            raise ValueError("Recording a dose means saying what happened to it.")
        if self.status is not DoseStatus.GIVEN and not self.reason:
            raise ValueError(f"Recording a dose as {self.status.value.lower()} needs a reason.")
        return self


class DoseRead(BaseModel):
    """One slot on the chart, and enough to give it safely.

    Patient identity is attached by the router, and it is not a convenience.
    `GET /ipd/doses` without an admission filter is the ward's medication round
    — its own docstring says "what is due, **on whom**" — and a round that
    cannot say whose dose this is, in which bed, is the single worst list in
    the hospital to render as identifiers. Checking the patient against the
    chart before giving a drug is the check; the screen has to make it
    possible.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    medication_order_id: uuid.UUID
    admission_id: uuid.UUID
    patient_id: uuid.UUID
    drug_name: str
    dose: str
    route: MedicationRoute
    due_at: datetime
    status: DoseStatus
    administered_at: datetime | None
    administered_by_id: uuid.UUID | None
    administered_by_name: str | None
    reason: str | None
    notes: str | None
    auto_missed: bool

    patient_name: str | None = None
    uhid: str | None = None
    bed_code: str | None = None
    ward_name: str | None = None
    # A dose must never be given to a patient recorded as deceased, and the
    # ward may learn of a death before the chart is stopped.
    patient_is_deceased: bool = False


# ---------------------------------------------------------------------------
# Discharge summary
# ---------------------------------------------------------------------------
class SummaryUpdate(BaseModel):
    """Every section is optional: the doctor edits what is wrong, not everything."""

    presenting_complaint: str | None = None
    diagnoses: str | None = None
    history: str | None = None
    examination: str | None = None
    course_in_hospital: str | None = None
    investigations: str | None = None
    procedures: str | None = None
    treatment_given: str | None = None
    condition_at_discharge: Annotated[str | None, Field(max_length=1000)] = None
    discharge_medications: str | None = None
    follow_up_instructions: str | None = None
    diet_and_activity: Annotated[str | None, Field(max_length=1000)] = None
    warning_signs: Annotated[str | None, Field(max_length=1000)] = None
    follow_up_date: date | None = None


class SignSummary(BaseModel):
    registration_number: Annotated[str | None, Field(max_length=64)] = None


class SummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admission_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    status: SummaryStatus

    presenting_complaint: str | None
    diagnoses: str | None
    history: str | None
    examination: str | None
    course_in_hospital: str | None
    investigations: str | None
    procedures: str | None
    treatment_given: str | None
    condition_at_discharge: str | None
    discharge_medications: str | None
    follow_up_instructions: str | None
    diet_and_activity: str | None
    warning_signs: str | None
    follow_up_date: date | None

    compiled: dict[str, Any]
    compiled_at: datetime | None
    signed_by_id: uuid.UUID | None
    signed_by_name: str | None
    signatory_registration_number: str | None
    signed_at: datetime | None
    amends_id: uuid.UUID | None
    amendment_reason: str | None
    created_at: datetime
