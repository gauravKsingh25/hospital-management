"""Lab and radiology: the catalogue, the specimen, the report and its results.

`clinical` owns the *act of ordering* and the ledger that decides when a visit
may close. This module owns **fulfilment** — what was collected, what was
measured, who checked it, and when the report became releasable. There is
deliberately no second order table: a `DiagnosticReport` hangs off the existing
`clinical.orders` row, so "what was asked for" has exactly one answer.

Three decisions here are clinical-safety decisions rather than modelling taste:

* **Reference ranges are snapshotted onto each result.** A hospital that revises
  its haemoglobin range next year must not silently re-interpret a result issued
  today. The range that was used is part of the result, not a lookup.
* **A result is entered and then verified, by different people.** Until it is
  verified the report is `PRELIMINARY` — visible, clearly provisional, and not
  yet closing the clinical order.
* **A final report is amended, never edited.** The correction is a new report
  pointing at the old one, which is what makes the record defensible.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "AccessionSequence",
    "DiagnosticDiscipline",
    "DiagnosticReport",
    "ReferenceRange",
    "ReportStatus",
    "ResultFlag",
    "ResultValue",
    "Specimen",
    "SpecimenStatus",
    "SpecimenType",
    "TestAnalyte",
    "TestCatalogueItem",
]


class DiagnosticDiscipline(enum.StrEnum):
    """Which department does the work. Kept coarse on purpose.

    The finer split — haematology vs biochemistry, X-ray vs CT — is a free-text
    `section` on the catalogue item, because every hospital organises its
    benches differently and an enum would need a migration each time.
    """

    LAB = "LAB"
    RADIOLOGY = "RADIOLOGY"


class SpecimenType(enum.StrEnum):
    BLOOD = "BLOOD"
    SERUM = "SERUM"
    PLASMA = "PLASMA"
    URINE = "URINE"
    STOOL = "STOOL"
    SPUTUM = "SPUTUM"
    SWAB = "SWAB"
    TISSUE = "TISSUE"
    CSF = "CSF"
    FLUID = "FLUID"
    # Radiology takes nothing from the patient but the patient.
    NONE = "NONE"


class SpecimenStatus(enum.StrEnum):
    PENDING_COLLECTION = "PENDING_COLLECTION"
    COLLECTED = "COLLECTED"
    RECEIVED = "RECEIVED"
    # A haemolysed sample or a mislabelled tube. Terminal, and it must say why —
    # a rejected sample means the patient gets stuck again.
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ReportStatus(enum.StrEnum):
    REGISTERED = "REGISTERED"
    IN_PROGRESS = "IN_PROGRESS"
    # Entered but not yet verified. Released deliberately: a clinician waiting on
    # a potassium result should see it now with "provisional" attached, rather
    # than nothing at all until someone signs.
    PRELIMINARY = "PRELIMINARY"
    FINAL = "FINAL"
    # Superseded by a corrected report. Terminal on this row.
    AMENDED = "AMENDED"
    CANCELLED = "CANCELLED"


class ResultFlag(enum.StrEnum):
    NORMAL = "NORMAL"
    LOW = "LOW"
    HIGH = "HIGH"
    # Outside the panic limits: needs a phone call to the treating doctor, and
    # NABH requires that call to be documented (see `DiagnosticReport`).
    CRITICAL_LOW = "CRITICAL_LOW"
    CRITICAL_HIGH = "CRITICAL_HIGH"
    # For results that are not numbers — "Growth of E. coli", "Reactive".
    ABNORMAL = "ABNORMAL"


class TestCatalogueItem(AuditedTenantModel, table=True):
    """One orderable investigation: a single test, or a panel of them.

    Tenant-scoped because prices, panels and turnaround promises differ per
    hospital, and a shared master list would make one hospital's edit everyone
    else's problem.
    """

    __tablename__ = "test_catalogue_items"
    __table_args__ = (
        Index(
            "uq_test_catalogue_items_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_test_catalogue_items_hospital_id_discipline", "hospital_id", "discipline"),
    )

    code: str = Field(max_length=32, index=True, description="Orderable code, e.g. 'CBC'.")
    name: str = Field(max_length=200, index=True)
    discipline: DiagnosticDiscipline = Field(index=True)
    # Free text: "Haematology", "Biochemistry", "CT", "Ultrasound".
    section: str | None = Field(default=None, max_length=60, index=True)

    specimen_type: SpecimenType = Field(default=SpecimenType.NONE)
    container: str | None = Field(
        default=None, max_length=60, description="Tube or container, printed on the label."
    )
    # What the counter promises the patient. Drives the overdue worklist.
    turnaround_minutes: int | None = Field(default=None, ge=1)

    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    preparation_notes: str | None = Field(
        default=None, max_length=500, description="e.g. 'Fasting 10-12 hours'."
    )
    is_active: bool = Field(default=True, index=True)

    @property
    def requires_specimen(self) -> bool:
        return self.specimen_type is not SpecimenType.NONE


class TestAnalyte(AuditedTenantModel, table=True):
    """One measured quantity within a catalogue item.

    A CBC is one order and about a dozen analytes. Modelled as rows rather than
    a JSON blob so a single result can be trended across visits and flagged
    against its own range — which is the whole clinical point of a lab system.
    """

    __tablename__ = "test_analytes"
    __table_args__ = (
        Index(
            "uq_test_analytes_code_live",
            "catalogue_item_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    catalogue_item_id: uuid.UUID = Field(
        foreign_key="test_catalogue_items.id", ondelete="CASCADE", index=True
    )

    code: str = Field(max_length=32, index=True, description="e.g. 'HB', 'WBC'.")
    name: str = Field(max_length=120)
    unit: str | None = Field(default=None, max_length=32, description="e.g. 'g/dL'.")
    # Decimals to report to. A haemoglobin of 12.3456 is noise pretending to be
    # precision, and clinicians read the extra digits as meaningful.
    decimal_places: int = Field(default=1, ge=0, le=4)
    # Where it appears on the printed report.
    display_order: int = Field(default=0)
    # False for a text result: culture growth, an impression, "Reactive".
    is_numeric: bool = Field(default=True)
    is_active: bool = Field(default=True, index=True)


class ReferenceRange(AuditedTenantModel, table=True):
    """A normal range for one analyte, for one kind of patient.

    Sex and age bands are not a refinement — a haemoglobin of 12 g/dL is normal
    in an adult woman and anaemic in an adult man, and a neonate's ranges look
    nothing like either. A single range per analyte would mis-flag most of the
    hospital.
    """

    __tablename__ = "reference_ranges"
    __table_args__ = (Index("ix_reference_ranges_analyte_id_sex", "analyte_id", "sex"),)

    analyte_id: uuid.UUID = Field(foreign_key="test_analytes.id", ondelete="CASCADE", index=True)

    # NULL means "any" — the fallback band when nothing more specific matches.
    sex: str | None = Field(
        default=None, max_length=10, description="MALE, FEMALE, OTHER, or NULL for any."
    )
    age_min_years: int | None = Field(default=None, ge=0)
    age_max_years: int | None = Field(default=None, ge=0)

    low: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    high: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    # Panic limits. Crossing one is not "abnormal", it is a phone call.
    critical_low: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    critical_high: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]

    # Printed under the value when the numbers alone do not explain themselves.
    text_range: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=255)


class Specimen(AuditedTenantModel, table=True):
    """A sample taken for one order.

    Radiology orders have none; the column set is shared anyway because the
    collection-to-receipt worklist is the same shape whether it holds a tube or
    is empty.
    """

    __tablename__ = "specimens"
    __table_args__ = (
        Index(
            "uq_specimens_accession_live",
            "hospital_id",
            "accession_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The phlebotomy round and the lab receipt bench.
        Index("ix_specimens_hospital_id_status", "hospital_id", "status"),
    )

    accession_number: str = Field(
        max_length=32, index=True, description="Printed on the tube label, e.g. 'ACC-26-000123'."
    )

    # FK for referential integrity; the row itself is only ever read or written
    # through `clinical.service` (CLAUDE.md §2).
    order_id: uuid.UUID = Field(foreign_key="orders.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    specimen_type: SpecimenType = Field(default=SpecimenType.BLOOD, index=True)
    container: str | None = Field(default=None, max_length=60)
    status: SpecimenStatus = Field(default=SpecimenStatus.PENDING_COLLECTION, index=True)

    collected_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    collected_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    # Where the needle went in. Matters when a result looks wrong: a sample from
    # the arm with the drip running is a classic spurious potassium.
    collection_site: str | None = Field(default=None, max_length=60)

    received_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    received_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )

    rejected_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Never optional when rejecting: a rejected sample means sticking the
    # patient again, and they are owed a reason.
    rejection_reason: str | None = Field(default=None, max_length=255)

    notes: str | None = Field(default=None, max_length=500)


class DiagnosticReport(AuditedTenantModel, table=True):
    """The deliverable for one clinical order.

    One live report per order — the partial unique index below says so — because
    "which result is the current one?" must never be a judgement call. A
    correction supersedes rather than competes.
    """

    __tablename__ = "diagnostic_reports"
    __table_args__ = (
        Index(
            "uq_diagnostic_reports_number_live",
            "hospital_id",
            "report_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one live report per order. Terminal states are excluded so an
        # amendment or a cancelled study can be replaced.
        Index(
            "uq_diagnostic_reports_order_live",
            "order_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status NOT IN ('AMENDED', 'CANCELLED')"),
        ),
        # The lab's worklist, and the "what is overdue" read.
        Index("ix_diagnostic_reports_hospital_id_status", "hospital_id", "status"),
        Index("ix_diagnostic_reports_patient_id_created_at", "patient_id", "created_at"),
    )

    report_number: str = Field(max_length=32, index=True)

    order_id: uuid.UUID = Field(foreign_key="orders.id", ondelete="RESTRICT", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    catalogue_item_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="test_catalogue_items.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    specimen_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="specimens.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    discipline: DiagnosticDiscipline = Field(index=True)
    # Snapshotted from the catalogue: a test renamed next year must not rewrite
    # the heading on a report issued today.
    test_name: str = Field(max_length=200)

    # Never assigned directly — every change goes through `transitions.py`.
    status: ReportStatus = Field(default=ReportStatus.REGISTERED, index=True)

    # --- radiology, and the narrative half of a pathology report ----------
    findings: str | None = Field(default=None)
    impression: str | None = Field(
        default=None, description="The conclusion a clinician actually reads first."
    )
    technique: str | None = Field(default=None, max_length=500)

    performed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    entered_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    entered_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # --- verification: the gate between "measured" and "releasable" -------
    verified_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    verified_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Snapshotted so a report printed in five years still names its signatory
    # correctly, and carries the registration number that makes it valid.
    verified_by_name: str | None = Field(default=None, max_length=200)
    verifier_registration_number: str | None = Field(default=None, max_length=64)

    # --- critical results (NABH requires the callback to be documented) ---
    has_critical_result: bool = Field(default=False, index=True)
    critical_notified_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    critical_notified_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    # Who picked up the phone at the other end. "Called the ward" is not a
    # record; "told Dr Rao at 14:32" is.
    critical_notified_to: str | None = Field(default=None, max_length=200)

    # --- amendment chain --------------------------------------------------
    amends_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="diagnostic_reports.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    amendment_reason: str | None = Field(default=None, max_length=500)
    cancellation_reason: str | None = Field(default=None, max_length=255)

    @property
    def is_releasable(self) -> bool:
        """Whether a clinician may act on this without a caveat."""
        return self.status is ReportStatus.FINAL


class ResultValue(AuditedTenantModel, table=True):
    """One measured value on a report.

    The reference range is **copied onto this row**, not looked up when the
    report is read. A hospital that revises a range next year must not silently
    re-interpret a result issued today — the interpretation is part of what was
    reported, and it has to stay fixed.
    """

    __tablename__ = "result_values"
    __table_args__ = (
        Index("ix_result_values_report_id_display_order", "report_id", "display_order"),
        # "Show me this patient's haemoglobin over the last year."
        Index("ix_result_values_patient_id_analyte_code", "patient_id", "analyte_code"),
    )

    report_id: uuid.UUID = Field(
        foreign_key="diagnostic_reports.id", ondelete="CASCADE", index=True
    )
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    analyte_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="test_analytes.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )

    # Snapshotted from the analyte for the same reason as the range.
    analyte_code: str = Field(max_length=32, index=True)
    label: str = Field(max_length=120)
    unit: str | None = Field(default=None, max_length=32)
    display_order: int = Field(default=0)

    value_numeric: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    # Cultures, impressions, "Reactive". A result that is not a number is still
    # a result, and forcing one into a numeric column loses it.
    value_text: str | None = Field(default=None, max_length=500)

    ref_low: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    ref_high: Decimal | None = Field(default=None, sa_type=Numeric(12, 4))  # type: ignore[call-overload]
    ref_text: str | None = Field(default=None, max_length=120)

    flag: ResultFlag = Field(default=ResultFlag.NORMAL, index=True)
    is_critical: bool = Field(default=False, index=True)
    comment: str | None = Field(default=None, max_length=500)


class AccessionSequence(TenantModel, table=True):
    """Per-hospital, per-year counter for accession and report numbers.

    Same shape and same reasoning as the UHID, token and encounter counters: a
    Postgres SEQUENCE is global to the database and cannot restart per tenant
    per year, and these numbers are written on tubes by hand and read down a
    phone line.
    """

    __tablename__ = "accession_sequences"
    __table_args__ = (
        Index(
            "uq_accession_sequences_hospital_year_kind",
            "hospital_id",
            "year",
            "kind",
            unique=True,
        ),
    )

    year: int = Field(index=True)
    kind: str = Field(max_length=16, description="'ACC' for specimens, 'RPT' for reports.")
    last_value: int = Field(default=0)
