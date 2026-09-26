"""Request/response DTOs for `tenancy`. ORM objects never leave a router."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.modules.tenancy.models import DepartmentType

_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,16}$")

__all__ = [
    "DepartmentCreate",
    "DepartmentRead",
    "DepartmentUpdate",
    "HospitalCreate",
    "HospitalRead",
    "HospitalUpdate",
]


class HospitalBase(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=200)]
    legal_name: Annotated[str | None, Field(max_length=200)] = None
    hfr_facility_id: Annotated[str | None, Field(max_length=64)] = None
    address_line1: Annotated[str | None, Field(max_length=200)] = None
    address_line2: Annotated[str | None, Field(max_length=200)] = None
    city: Annotated[str | None, Field(max_length=100)] = None
    state: Annotated[str | None, Field(max_length=100)] = None
    pincode: Annotated[str | None, Field(max_length=10)] = None
    country: Annotated[str, Field(min_length=2, max_length=2)] = "IN"
    phone: Annotated[str | None, Field(max_length=20)] = None
    email: EmailStr | None = None
    gstin: Annotated[str | None, Field(max_length=15)] = None
    timezone: Annotated[str, Field(max_length=64)] = "Asia/Kolkata"


class HospitalCreate(HospitalBase):
    code: Annotated[
        str,
        Field(
            min_length=2,
            max_length=16,
            description="Short uppercase tenant code, e.g. 'KMC'. Cannot be changed later.",
        ),
    ]

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        code = value.strip().upper()
        if not _CODE_PATTERN.match(code):
            raise ValueError("Code must be 2-16 uppercase letters or digits.")
        return code


class HospitalUpdate(BaseModel):
    """Every field optional — `code` is deliberately absent.

    The code is printed on patient cards and embedded in identifiers; letting
    it change would orphan records that are already on paper.
    """

    name: Annotated[str | None, Field(min_length=2, max_length=200)] = None
    legal_name: Annotated[str | None, Field(max_length=200)] = None
    hfr_facility_id: Annotated[str | None, Field(max_length=64)] = None
    address_line1: Annotated[str | None, Field(max_length=200)] = None
    address_line2: Annotated[str | None, Field(max_length=200)] = None
    city: Annotated[str | None, Field(max_length=100)] = None
    state: Annotated[str | None, Field(max_length=100)] = None
    pincode: Annotated[str | None, Field(max_length=10)] = None
    country: Annotated[str | None, Field(min_length=2, max_length=2)] = None
    phone: Annotated[str | None, Field(max_length=20)] = None
    email: EmailStr | None = None
    gstin: Annotated[str | None, Field(max_length=15)] = None
    timezone: Annotated[str | None, Field(max_length=64)] = None


class HospitalRead(HospitalBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DepartmentCreate(BaseModel):
    code: Annotated[str, Field(min_length=2, max_length=24)]
    name: Annotated[str, Field(min_length=2, max_length=120)]
    department_type: DepartmentType = DepartmentType.OPD
    description: Annotated[str | None, Field(max_length=500)] = None
    location: Annotated[str | None, Field(max_length=120)] = None
    phone_extension: Annotated[str | None, Field(max_length=10)] = None
    head_doctor_id: uuid.UUID | None = None

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper().replace(" ", "_")


class DepartmentUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=2, max_length=120)] = None
    department_type: DepartmentType | None = None
    description: Annotated[str | None, Field(max_length=500)] = None
    location: Annotated[str | None, Field(max_length=120)] = None
    phone_extension: Annotated[str | None, Field(max_length=10)] = None
    head_doctor_id: uuid.UUID | None = None
    is_active: bool | None = None


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    department_type: DepartmentType
    description: str | None
    location: str | None
    phone_extension: str | None
    head_doctor_id: uuid.UUID | None
    is_active: bool
