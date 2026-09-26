"""Request/response DTOs for `patients`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.modules.patients.matching import normalize_phone
from app.modules.patients.models import (
    AlertSeverity,
    AlertType,
    BloodGroup,
    ConsentType,
    Gender,
)

__all__ = [
    "AlertCreate",
    "ConsentCreate",
    "DuplicateCandidate",
    "IdentifierCreate",
    "PatientAlertRead",
    "PatientConsentRead",
    "PatientDetail",
    "PatientIdentifierRead",
    "PatientMergeRequest",
    "PatientRead",
    "PatientRegister",
    "PatientUpdate",
]


class PatientRegister(BaseModel):
    """Rapid registration — the whole form (CLAUDE.md §7b).

    Exactly four required fields: name, mobile, approximate age, gender. Nothing
    else gates the queue. Address, guardian and insurance are captured later by
    whoever has time, and the target is a registration under 30 seconds.
    """

    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    phone: Annotated[str, Field(min_length=6, max_length=20)]
    gender: Gender

    # Age OR birth date. Most patients here know roughly how old they are and
    # not the date; demanding a date would stall the counter.
    age_years: Annotated[int | None, Field(ge=0, le=130)] = None
    birth_date: date | None = None

    # Optional from here down.
    alternate_phone: Annotated[str | None, Field(max_length=20)] = None
    guardian_name: Annotated[str | None, Field(max_length=200)] = None
    guardian_relation: Annotated[str | None, Field(max_length=50)] = None
    guardian_phone: Annotated[str | None, Field(max_length=20)] = None
    address_line1: Annotated[str | None, Field(max_length=200)] = None
    city: Annotated[str | None, Field(max_length=100)] = None
    state: Annotated[str | None, Field(max_length=100)] = None
    pincode: Annotated[str | None, Field(max_length=10)] = None
    blood_group: BloodGroup = BloodGroup.UNKNOWN
    preferred_language: Annotated[str, Field(max_length=8)] = "hi"

    # Set deliberately by reception after reviewing the duplicate warnings, to
    # register a genuinely different person who shares a name and phone with an
    # existing record (a father and son, say). Never a default.
    confirm_not_duplicate: bool = False

    @model_validator(mode="after")
    def _require_an_age(self) -> Self:
        if self.age_years is None and self.birth_date is None:
            raise ValueError("Provide either age_years or birth_date.")
        return self

    @model_validator(mode="after")
    def _check_phone_has_digits(self) -> Self:
        if not normalize_phone(self.phone):
            raise ValueError("Phone number must contain digits.")
        return self


class PatientUpdate(BaseModel):
    """Everything optional. Name and phone are here too, but changing either
    re-checks the identity constraint — see `service.update_patient`."""

    full_name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    phone: Annotated[str | None, Field(min_length=6, max_length=20)] = None
    gender: Gender | None = None
    birth_date: date | None = None
    age_years: Annotated[int | None, Field(ge=0, le=130)] = None
    alternate_phone: Annotated[str | None, Field(max_length=20)] = None
    email: Annotated[str | None, Field(max_length=255)] = None
    blood_group: BloodGroup | None = None
    marital_status: Annotated[str | None, Field(max_length=20)] = None
    occupation: Annotated[str | None, Field(max_length=100)] = None
    guardian_name: Annotated[str | None, Field(max_length=200)] = None
    guardian_relation: Annotated[str | None, Field(max_length=50)] = None
    guardian_phone: Annotated[str | None, Field(max_length=20)] = None
    address_line1: Annotated[str | None, Field(max_length=200)] = None
    address_line2: Annotated[str | None, Field(max_length=200)] = None
    city: Annotated[str | None, Field(max_length=100)] = None
    district: Annotated[str | None, Field(max_length=100)] = None
    state: Annotated[str | None, Field(max_length=100)] = None
    pincode: Annotated[str | None, Field(max_length=10)] = None
    abha_id: Annotated[str | None, Field(max_length=32)] = None
    abha_address: Annotated[str | None, Field(max_length=100)] = None
    preferred_language: Annotated[str | None, Field(max_length=8)] = None


class PatientRead(BaseModel):
    """List/search projection. Deliberately omits identifiers and address."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    uhid: str
    full_name: str
    phone: str
    gender: Gender
    birth_date: date | None
    birth_date_is_estimated: bool
    blood_group: BloodGroup
    city: str | None
    is_active: bool
    is_deceased: bool
    merged_into_id: uuid.UUID | None
    registered_at: datetime

    @computed_field(description="Age in whole years, derived from birth_date.")  # type: ignore[prop-decorator]
    @property
    def age_years(self) -> int | None:
        """The patient's age, or None when no birth date is recorded.

        Derived rather than stored, so it cannot go stale — a stored age is
        wrong from the patient's next birthday onward, and a paediatric dose
        calculated from a stale age is a real harm.

        `computed_field`, not a bare `@property`: in Pydantic v2 a plain
        property never reaches the response or the OpenAPI schema, so this
        looked like part of the contract while every client received a record
        with no age on it at all.
        """
        if self.birth_date is None:
            return None
        today = date.today()
        return (
            today.year
            - self.birth_date.year
            - ((today.month, today.day) < (self.birth_date.month, self.birth_date.day))
        )


class PatientAlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alert_type: AlertType
    severity: AlertSeverity
    label: str
    detail: str | None
    is_active: bool
    created_at: datetime


class PatientConsentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consent_type: ConsentType
    granted: bool
    method: str
    given_by: str
    given_by_name: str | None
    language: str
    granted_at: datetime
    expires_at: datetime | None
    withdrawn_at: datetime | None


class PatientIdentifierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    identifier_type: str
    identifier_value: str
    issued_by: str | None
    valid_until: date | None


class PatientDetail(PatientRead):
    """Full record for the patient screen.

    `alerts` is always present and always first in the payload's intent: the
    safety banner must render before anything else on the page (CLAUDE.md §7b).
    """

    alternate_phone: str | None
    email: str | None
    marital_status: str | None
    occupation: str | None
    nationality: str
    guardian_name: str | None
    guardian_relation: str | None
    guardian_phone: str | None
    address_line1: str | None
    address_line2: str | None
    district: str | None
    state: str | None
    pincode: str | None
    country: str
    abha_id: str | None
    abha_address: str | None
    preferred_language: str
    deceased_at: datetime | None

    alerts: list[PatientAlertRead] = Field(default_factory=list)
    consents: list[PatientConsentRead] = Field(default_factory=list)


class DuplicateCandidate(BaseModel):
    """A possible existing record, surfaced while reception types.

    `reason` is shown verbatim to staff — "same mobile" is far more actionable
    than a bare confidence number they have no way to calibrate.
    """

    patient: PatientRead
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    reason: str
    is_exact: Annotated[
        bool,
        Field(
            description=(
                "True only for an identity collision — same normalised name AND "
                "same mobile. A high score alone is not this: two different "
                "people genuinely share a name on different numbers."
            )
        ),
    ] = False


class AlertCreate(BaseModel):
    alert_type: AlertType
    severity: AlertSeverity = AlertSeverity.HIGH
    label: Annotated[str, Field(min_length=1, max_length=120)]
    detail: Annotated[str | None, Field(max_length=500)] = None


class ConsentCreate(BaseModel):
    consent_type: ConsentType
    granted: bool
    method: Annotated[str, Field(max_length=20)] = "VERBAL"
    given_by: Annotated[str, Field(max_length=20)] = "PATIENT"
    given_by_name: Annotated[str | None, Field(max_length=200)] = None
    given_by_relation: Annotated[str | None, Field(max_length=50)] = None
    language: Annotated[str, Field(max_length=8)] = "hi"
    document_url: Annotated[str | None, Field(max_length=500)] = None
    expires_at: datetime | None = None


class IdentifierCreate(BaseModel):
    identifier_type: Annotated[str, Field(min_length=2, max_length=32)]
    identifier_value: Annotated[str, Field(min_length=1, max_length=64)]
    issued_by: Annotated[str | None, Field(max_length=100)] = None
    valid_until: date | None = None


class PatientMergeRequest(BaseModel):
    """Fold `duplicate_id` into `survivor_id`."""

    duplicate_id: uuid.UUID
    reason: Annotated[str, Field(min_length=3, max_length=255)]
