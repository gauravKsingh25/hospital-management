"""Request/response DTOs for `diagnostics`."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.clinical.models import OrderPriority, OrderStatus, OrderType
from app.modules.diagnostics.models import (
    DiagnosticDiscipline,
    ReportStatus,
    ResultFlag,
    SpecimenStatus,
    SpecimenType,
)

__all__ = [
    "AccessionRequest",
    "AmendRequest",
    "AnalyteCreate",
    "AnalyteRead",
    "CatalogueItemCreate",
    "CatalogueItemRead",
    "CatalogueItemUpdate",
    "CollectRequest",
    "CriticalCallback",
    "NarrativeEntry",
    "ReferenceRangeCreate",
    "ReferenceRangeRead",
    "RejectRequest",
    "ReportRead",
    "ReportSummary",
    "ResultEntry",
    "ResultValueRead",
    "ResultsSubmission",
    "SpecimenRead",
    "WorklistEntry",
    "WorklistStage",
]


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
class CatalogueItemCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    discipline: DiagnosticDiscipline
    section: Annotated[str | None, Field(max_length=60)] = None
    specimen_type: SpecimenType = SpecimenType.NONE
    container: Annotated[str | None, Field(max_length=60)] = None
    turnaround_minutes: Annotated[int | None, Field(ge=1)] = None
    department_id: uuid.UUID | None = None
    preparation_notes: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def _radiology_takes_no_sample(self) -> Self:
        if (
            self.discipline is DiagnosticDiscipline.RADIOLOGY
            and self.specimen_type is not SpecimenType.NONE
        ):
            raise ValueError("A radiology investigation does not collect a specimen.")
        if self.discipline is DiagnosticDiscipline.LAB and self.specimen_type is SpecimenType.NONE:
            raise ValueError("A laboratory test needs a specimen type.")
        return self


class CatalogueItemUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    section: Annotated[str | None, Field(max_length=60)] = None
    container: Annotated[str | None, Field(max_length=60)] = None
    turnaround_minutes: Annotated[int | None, Field(ge=1)] = None
    department_id: uuid.UUID | None = None
    preparation_notes: Annotated[str | None, Field(max_length=500)] = None
    is_active: bool | None = None


class ReferenceRangeCreate(BaseModel):
    """A normal band for one kind of patient.

    Leave `sex` and the age bounds null for the catch-all band; the service
    picks the most specific match and falls back to it.
    """

    sex: Annotated[str | None, Field(max_length=10)] = None
    age_min_years: Annotated[int | None, Field(ge=0)] = None
    age_max_years: Annotated[int | None, Field(ge=0)] = None
    low: Decimal | None = None
    high: Decimal | None = None
    # Panic limits: crossing one is a phone call, not a highlight.
    critical_low: Decimal | None = None
    critical_high: Decimal | None = None
    text_range: Annotated[str | None, Field(max_length=120)] = None
    notes: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.low is not None and self.high is not None and self.low >= self.high:
            raise ValueError("low must be below high.")
        if (
            self.age_min_years is not None
            and self.age_max_years is not None
            and self.age_min_years > self.age_max_years
        ):
            raise ValueError("age_min_years must not exceed age_max_years.")
        if self.critical_low is not None and self.low is not None and self.critical_low > self.low:
            raise ValueError("critical_low must be at or below low.")
        if (
            self.critical_high is not None
            and self.high is not None
            and self.critical_high < self.high
        ):
            raise ValueError("critical_high must be at or above high.")
        return self


class ReferenceRangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    analyte_id: uuid.UUID
    sex: str | None
    age_min_years: int | None
    age_max_years: int | None
    low: Decimal | None
    high: Decimal | None
    critical_low: Decimal | None
    critical_high: Decimal | None
    text_range: str | None


class AnalyteCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    unit: Annotated[str | None, Field(max_length=32)] = None
    decimal_places: Annotated[int, Field(ge=0, le=4)] = 1
    display_order: int = 0
    is_numeric: bool = True
    ranges: list[ReferenceRangeCreate] = Field(default_factory=list)


class AnalyteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    catalogue_item_id: uuid.UUID
    code: str
    name: str
    unit: str | None
    decimal_places: int
    display_order: int
    is_numeric: bool
    is_active: bool


class CatalogueItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    discipline: DiagnosticDiscipline
    section: str | None
    specimen_type: SpecimenType
    container: str | None
    turnaround_minutes: int | None
    department_id: uuid.UUID | None
    preparation_notes: str | None
    is_active: bool


# ---------------------------------------------------------------------------
# Specimens
# ---------------------------------------------------------------------------
class AccessionRequest(BaseModel):
    """The lab accepting a doctor's order onto its bench.

    `catalogue_item_id` is chosen here rather than at ordering time on purpose:
    a doctor writes "CBC" at speed, and the lab is the one that knows which
    catalogue entry that is — and can correct a typo without bouncing the order
    back to the consulting room.
    """

    order_id: uuid.UUID
    catalogue_item_id: uuid.UUID
    notes: Annotated[str | None, Field(max_length=500)] = None


class CollectRequest(BaseModel):
    collection_site: Annotated[str | None, Field(max_length=60)] = None
    collected_at: datetime | None = None
    notes: Annotated[str | None, Field(max_length=500)] = None


class RejectRequest(BaseModel):
    # Never optional: a rejected sample means sticking the patient again, and
    # they are owed a reason.
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class SpecimenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    accession_number: str
    order_id: uuid.UUID
    patient_id: uuid.UUID
    specimen_type: SpecimenType
    container: str | None
    status: SpecimenStatus
    collected_at: datetime | None
    collected_by_id: uuid.UUID | None
    collection_site: str | None
    received_at: datetime | None
    rejected_at: datetime | None
    rejection_reason: str | None
    notes: str | None


# ---------------------------------------------------------------------------
# Results and reports
# ---------------------------------------------------------------------------
class ResultEntry(BaseModel):
    """One measured value.

    Identified by `analyte_code` rather than id: a technician typing from a
    machine printout works in codes, and the codes are stable while ids are not
    anything a human ever sees.
    """

    analyte_code: Annotated[str, Field(min_length=1, max_length=32)]
    value_numeric: Decimal | None = None
    value_text: Annotated[str | None, Field(max_length=500)] = None
    comment: Annotated[str | None, Field(max_length=500)] = None
    # Only consulted for text results — "Growth of E. coli" is abnormal and no
    # arithmetic will tell you so. A numeric value is always flagged against its
    # reference range, and this field is ignored: letting a technician overrule
    # the range by hand would defeat having ranges at all.
    abnormal: bool = False

    @model_validator(mode="after")
    def _needs_a_value(self) -> Self:
        if self.value_numeric is None and not self.value_text:
            raise ValueError("Give either a numeric value or a text value.")
        return self


class ResultsSubmission(BaseModel):
    """A batch of values. Entering a CBC is one action, not twelve."""

    results: Annotated[list[ResultEntry], Field(min_length=1)]
    performed_at: datetime | None = None


class NarrativeEntry(BaseModel):
    """A radiologist's report, and the prose half of a pathology one."""

    findings: str | None = None
    impression: Annotated[str | None, Field(max_length=2000)] = None
    technique: Annotated[str | None, Field(max_length=500)] = None
    performed_at: datetime | None = None

    @model_validator(mode="after")
    def _needs_something(self) -> Self:
        if not (self.findings or self.impression):
            raise ValueError("A report needs findings or an impression.")
        return self


class AmendRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=500)]


class CriticalCallback(BaseModel):
    """Recording the phone call a panic value demands.

    "Called the ward" is not a record. NABH wants to know who was told.
    """

    notified_to: Annotated[str, Field(min_length=2, max_length=200)]
    notified_at: datetime | None = None


class ResultValueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    analyte_code: str
    label: str
    unit: str | None
    display_order: int
    value_numeric: Decimal | None
    value_text: str | None
    ref_low: Decimal | None
    ref_high: Decimal | None
    ref_text: str | None
    flag: ResultFlag
    is_critical: bool
    comment: str | None


class ReportSummary(BaseModel):
    """The worklist row — the lab's screen is long."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_number: str
    order_id: uuid.UUID
    patient_id: uuid.UUID
    discipline: DiagnosticDiscipline
    test_name: str
    status: ReportStatus
    has_critical_result: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# The bench worklist
# ---------------------------------------------------------------------------
class WorklistStage(enum.StrEnum):
    """What one diagnostic request is waiting for.

    Derived, never stored — it is the combination of an order's status, its
    sample's status and its report's status, which is three enums the person at
    the bench should not have to hold in their head at once. Derived on the
    server so the rule has one implementation rather than one per client.
    """

    AWAITING_ACCESSION = "AWAITING_ACCESSION"
    AWAITING_COLLECTION = "AWAITING_COLLECTION"
    AWAITING_RECEIPT = "AWAITING_RECEIPT"
    AWAITING_RESULTS = "AWAITING_RESULTS"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"


class WorklistEntry(BaseModel):
    """One open request, with everything needed to work it and to be sure who
    it belongs to.

    Patient identity is on the row rather than a `patient_id` for the client to
    resolve. A worklist of UUIDs is not a worklist, and a technician who has to
    open each row to find out whose blood this is will batch them — which is how
    a STAT troponin ends up processed fourth.
    """

    order_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID

    order_type: OrderType
    order_status: OrderStatus
    priority: OrderPriority
    item_name: str
    item_code: str | None
    instructions: str | None
    review_in_visit: bool
    ordered_at: datetime

    stage: WorklistStage

    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    patient_is_deceased: bool = False

    report_id: uuid.UUID | None = None
    report_number: str | None = None
    report_status: ReportStatus | None = None
    catalogue_item_id: uuid.UUID | None = None
    has_critical_result: bool = False

    specimen_id: uuid.UUID | None = None
    accession_number: str | None = None
    specimen_status: SpecimenStatus | None = None


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_number: str
    order_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    catalogue_item_id: uuid.UUID | None
    specimen_id: uuid.UUID | None
    discipline: DiagnosticDiscipline
    test_name: str
    status: ReportStatus

    # Not decoration. A numeric result means nothing without the band it was
    # judged against, and the band is chosen by sex and age — so a report that
    # does not carry them is not a document anyone can act on, on screen or on
    # paper. Filled by the router; absent only if the patient record has gone.
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    patient_is_deceased: bool = False

    findings: str | None
    impression: str | None
    technique: str | None

    performed_at: datetime | None
    entered_by_id: uuid.UUID | None
    entered_at: datetime | None
    verified_by_id: uuid.UUID | None
    verified_by_name: str | None
    verifier_registration_number: str | None
    verified_at: datetime | None

    has_critical_result: bool
    critical_notified_at: datetime | None
    critical_notified_to: str | None

    amends_id: uuid.UUID | None
    amendment_reason: str | None
    cancellation_reason: str | None
    created_at: datetime

    results: list[ResultValueRead] = Field(default_factory=list)
    specimen: SpecimenRead | None = None
