"""Patient master record.

**Identity without email.** Most patients here have no email address and many
have no reliable ID document, so the identifying pair is **name + mobile**
(CLAUDE.md §7b: registration asks for four things and no more). Uniqueness is
enforced on the *pair*, not on the phone alone — one household mobile shared by
a mother, father and three children is normal and must keep working. What is
refused is a second `Sunita Devi` on the same number, which is a duplicate
record rather than a second person.

Names are matched on a normalised form, so `  sunita   DEVI ` and `Sunita Devi`
collide as they should.

**Genuine collisions do happen** — a father and son with the same name on one
phone. Reception disambiguates exactly as they already do on paper, by
qualifying the name (`Ramesh Kumar S/O Suresh`), which changes the normalised
form. `register_patient` returns the existing UHID on a clash so the far more
common case — this patient already exists — is a one-click reuse rather than a
dead end.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "AlertSeverity",
    "AlertType",
    "BloodGroup",
    "ConsentType",
    "Gender",
    "Patient",
    "PatientAlert",
    "PatientConsent",
    "PatientIdentifier",
    "UhidSequence",
]


class Gender(enum.StrEnum):
    """Includes `OTHER`: the Transgender Persons Act 2019 makes a third option
    a legal requirement on Indian health records, not a nicety."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class BloodGroup(enum.StrEnum):
    A_POS = "A+"
    A_NEG = "A-"
    B_POS = "B+"
    B_NEG = "B-"
    AB_POS = "AB+"
    AB_NEG = "AB-"
    O_POS = "O+"
    O_NEG = "O-"
    UNKNOWN = "UNKNOWN"


class AlertType(enum.StrEnum):
    """Drives the persistent safety banner (CLAUDE.md §7b)."""

    DRUG_ALLERGY = "DRUG_ALLERGY"
    FOOD_ALLERGY = "FOOD_ALLERGY"
    HIGH_RISK_CONDITION = "HIGH_RISK_CONDITION"
    INFECTIOUS_PRECAUTION = "INFECTIOUS_PRECAUTION"
    FALL_RISK = "FALL_RISK"
    IMPLANT_DEVICE = "IMPLANT_DEVICE"
    OTHER = "OTHER"


class AlertSeverity(enum.StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    INFO = "INFO"


class ConsentType(enum.StrEnum):
    """DPDP Act 2023 §6 — consent is per purpose, not a single blanket tick."""

    TREATMENT = "TREATMENT"
    DATA_PROCESSING = "DATA_PROCESSING"
    DATA_SHARING = "DATA_SHARING"
    ABHA_LINKAGE = "ABHA_LINKAGE"
    RESEARCH = "RESEARCH"
    MARKETING = "MARKETING"


class Patient(AuditedTenantModel, table=True):
    """A person, independent of any single visit."""

    __tablename__ = "patients"
    __table_args__ = (
        # THE identity rule: one person per (name, mobile) within a hospital.
        # Partial so a merged or retired record releases the pair.
        Index(
            "uq_patients_name_phone_live",
            "hospital_id",
            "name_normalized",
            "phone",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND merged_into_id IS NULL"),
        ),
        Index(
            "uq_patients_uhid_live",
            "hospital_id",
            "uhid",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Reception's most common lookup: type a mobile, see the family.
        Index("ix_patients_hospital_id_phone", "hospital_id", "phone"),
        # Backs duplicate detection and the universal search box.
        Index("ix_patients_hospital_id_name_normalized", "hospital_id", "name_normalized"),
        # Trigram index behind fuzzy duplicate detection. Declared here as well
        # as created in the migration: an index that exists in the database but
        # not in the model metadata is one that a later `--autogenerate` will
        # cheerfully offer to DROP.
        Index(
            "ix_patients_name_normalized_trgm",
            "name_normalized",
            postgresql_using="gin",
            postgresql_ops={"name_normalized": "gin_trgm_ops"},
        ),
    )

    # --- Identifier -------------------------------------------------------
    uhid: str = Field(
        max_length=32,
        index=True,
        description="Unique Hospital ID printed on the patient's card.",
    )

    # --- The four required registration fields (CLAUDE.md §7b) ------------
    full_name: str = Field(min_length=1, max_length=200)
    # Casefolded, whitespace-collapsed, punctuation-stripped form of full_name.
    # Maintained by the service so matching never depends on how carefully a
    # busy receptionist typed.
    name_normalized: str = Field(max_length=200, index=True)
    phone: str = Field(
        max_length=15,
        index=True,
        description="Primary mobile, stored as 10 digits (India) without prefix.",
    )
    gender: Gender = Field(default=Gender.UNKNOWN, index=True)

    # Age is captured as a number when the patient does not know their date of
    # birth — which is common, and must not block registration. `birth_date` is
    # derived from it so that age arithmetic works everywhere downstream, and
    # `birth_date_is_estimated` records that it was a guess so nobody later
    # mistakes it for a documented fact.
    birth_date: date | None = Field(default=None, sa_type=Date)
    birth_date_is_estimated: bool = Field(default=False)

    # --- Everything below is optional and captured later ------------------
    alternate_phone: str | None = Field(default=None, max_length=15, index=True)
    email: str | None = Field(default=None, max_length=255)

    blood_group: BloodGroup = Field(default=BloodGroup.UNKNOWN)
    marital_status: str | None = Field(default=None, max_length=20)
    occupation: str | None = Field(default=None, max_length=100)
    # Free text: India has no closed list, and forcing one loses information.
    nationality: str = Field(default="Indian", max_length=50)

    # Guardian doubles as the disambiguator for same-name relatives and as the
    # contact for minors and the unconscious.
    guardian_name: str | None = Field(default=None, max_length=200)
    guardian_relation: str | None = Field(default=None, max_length=50)
    guardian_phone: str | None = Field(default=None, max_length=15, index=True)

    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=10, index=True)
    country: str = Field(default="IN", max_length=2)

    # --- ABDM / ABHA (CLAUDE.md §9) ---------------------------------------
    # Reserved now so linkage later is a feature, not a migration of a live
    # patient table.
    abha_id: str | None = Field(default=None, max_length=32, index=True)
    abha_address: str | None = Field(default=None, max_length=100)
    abha_linked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # --- Language & preferences -------------------------------------------
    preferred_language: str = Field(
        default="hi",
        max_length=8,
        description="ISO 639-1. Drives notification language, not just the UI.",
    )

    # --- Lifecycle --------------------------------------------------------
    is_active: bool = Field(default=True, index=True)

    # Set when this record loses a merge. Reads follow the pointer so old cards,
    # printed reports and historical encounters keep resolving.
    merged_into_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="patients.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    merged_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]

    # Denormalised from the clinical module when a death is recorded. Kept here
    # because the notification suppression rule (CLAUDE.md §6, §14 — never
    # message a deceased patient) has to be answerable from the patient record
    # alone, without a join into encounters that a dispatcher might skip.
    is_deceased: bool = Field(default=False, index=True)
    deceased_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]

    registered_at: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )


class PatientAlert(AuditedTenantModel, table=True):
    """A safety flag shown on the persistent banner (CLAUDE.md §7b).

    Deliberately patient-level rather than encounter-level: a penicillin allergy
    is true before the first consultation exists, and must be visible to the
    receptionist and nurse, not only to the doctor.
    """

    __tablename__ = "patient_alerts"
    __table_args__ = (Index("ix_patient_alerts_patient_id_is_active", "patient_id", "is_active"),)

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    alert_type: AlertType = Field(index=True)
    severity: AlertSeverity = Field(default=AlertSeverity.HIGH, index=True)

    label: str = Field(max_length=120, description="Shown on the banner, e.g. 'Penicillin'.")
    detail: str | None = Field(default=None, max_length=500)

    is_active: bool = Field(default=True, index=True)
    recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    # An allergy is never deleted, only retired — "we no longer believe this"
    # is clinically different from "this was never recorded".
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    resolved_reason: str | None = Field(default=None, max_length=255)


class PatientConsent(TenantModel, table=True):
    """A real consent record, not a buried checkbox (CLAUDE.md §12).

    Append-only in spirit: withdrawal writes `withdrawn_at` on the existing row
    rather than deleting it, because "consent was given and later withdrawn" and
    "consent was never given" are different facts and a regulator will ask which
    one applies.
    """

    __tablename__ = "patient_consents"
    __table_args__ = (
        Index("ix_patient_consents_patient_id_consent_type", "patient_id", "consent_type"),
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    consent_type: ConsentType = Field(index=True)
    granted: bool = Field(description="False records an explicit refusal.")

    # How it was taken — needed to defend the record later.
    method: str = Field(
        default="VERBAL",
        max_length=20,
        description="VERBAL, WRITTEN, DIGITAL_SIGNATURE, THUMBPRINT.",
    )
    # Who actually consented: patients who cannot consent for themselves.
    given_by: str = Field(default="PATIENT", max_length=20)
    given_by_name: str | None = Field(default=None, max_length=200)
    given_by_relation: str | None = Field(default=None, max_length=50)

    language: str = Field(
        default="hi",
        max_length=8,
        description="Language the consent was explained in — a fair-notice requirement.",
    )
    document_url: str | None = Field(default=None, max_length=500)

    recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    granted_at: datetime = Field(nullable=False, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    withdrawn_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    withdrawn_reason: str | None = Field(default=None, max_length=255)


class PatientIdentifier(AuditedTenantModel, table=True):
    """Government and scheme identifiers, one row per type.

    A separate table rather than columns on `patients`: the set grows (PMJAY,
    CGHS, ECHS, state schemes, insurer member IDs) and each new one would
    otherwise be a migration on the largest table in the system.
    """

    __tablename__ = "patient_identifiers"
    __table_args__ = (
        Index(
            "uq_patient_identifiers_type_value",
            "hospital_id",
            "identifier_type",
            "identifier_value",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    identifier_type: str = Field(
        max_length=32, index=True, description="AADHAAR, PAN, PMJAY, CGHS, ECHS, INSURER."
    )
    identifier_value: str = Field(max_length=64, index=True)
    issued_by: str | None = Field(default=None, max_length=100)
    valid_until: date | None = Field(default=None, sa_type=Date)


class UhidSequence(TenantModel, table=True):
    """Per-hospital, per-year counter behind UHID allocation.

    A row locked with `SELECT ... FOR UPDATE` rather than a Postgres SEQUENCE,
    because a sequence is global to the database and these numbers must restart
    per tenant and per year to stay short and human-readable. Sequences also
    burn numbers on rollback; a UHID with a visible gap invites "where did
    patient 41 go?" from staff who count.
    """

    __tablename__ = "uhid_sequences"
    __table_args__ = (Index("uq_uhid_sequences_hospital_year", "hospital_id", "year", unique=True),)

    year: int = Field(index=True)
    last_value: int = Field(default=0)
