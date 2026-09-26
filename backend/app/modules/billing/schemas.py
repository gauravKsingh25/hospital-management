"""Request/response DTOs for `billing`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.billing.models import (
    ChargeCategory,
    ChargeStatus,
    ClaimStatus,
    InvoiceStatus,
    PayerType,
    PaymentMethod,
    PaymentStatus,
)

__all__ = [
    "AccountBoardEntry",
    "ChargeAdd",
    "ChargeRead",
    "ClaimCreate",
    "ClaimRead",
    "ClaimUpdate",
    "DiscountRequest",
    "InvoiceDraftRequest",
    "InvoiceIssueRequest",
    "InvoiceRead",
    "InvoiceSummary",
    "PaymentRead",
    "PaymentRequest",
    "RateCardCreate",
    "RateCardRead",
    "RateCardUpdate",
    "ReasonRequest",
    "ReversalRequest",
    "ServiceItemCreate",
    "ServiceItemRead",
    "ServiceItemUpdate",
    "ServicePriceRead",
    "ServicePriceUpsert",
    "VisitAccount",
]

# Money on the wire is a Decimal, never a float. Pydantic v2 serialises it as a
# JSON number with the scale intact; a float would arrive at the frontend as
# 1234.5600000000001 and be shown to a patient.
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Rate = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


# ---------------------------------------------------------------------------
# Rate cards and services
# ---------------------------------------------------------------------------
class RateCardCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    payer_type: PayerType = PayerType.CASH
    scheme_code: Annotated[str | None, Field(max_length=32)] = None
    is_default: bool = False
    valid_from: date | None = None
    valid_to: date | None = None
    notes: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to.")
        return self


class RateCardUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    scheme_code: Annotated[str | None, Field(max_length=32)] = None
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None
    notes: Annotated[str | None, Field(max_length=500)] = None


class RateCardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    payer_type: PayerType
    scheme_code: str | None
    is_default: bool
    valid_from: date | None
    valid_to: date | None
    is_active: bool


class ServiceItemCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    category: ChargeCategory = ChargeCategory.OTHER
    department_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None
    is_gst_exempt: bool = True
    gst_rate: Rate = Decimal("0.00")
    hsn_sac_code: Annotated[str | None, Field(max_length=12)] = None
    unit: Annotated[str | None, Field(max_length=24)] = None

    @model_validator(mode="after")
    def _tax_is_coherent(self) -> Self:
        # A taxable service with no rate would silently bill at 0% and look
        # correct on screen. Refuse it at the boundary instead.
        if not self.is_gst_exempt and self.gst_rate <= 0:
            raise ValueError("A taxable service needs a GST rate above zero.")
        if self.is_gst_exempt and self.gst_rate > 0:
            raise ValueError("An exempt service cannot carry a GST rate.")
        return self


class ServiceItemUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    category: ChargeCategory | None = None
    department_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None
    is_gst_exempt: bool | None = None
    gst_rate: Rate | None = None
    hsn_sac_code: Annotated[str | None, Field(max_length=12)] = None
    unit: Annotated[str | None, Field(max_length=24)] = None
    is_active: bool | None = None


class ServiceItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    category: ChargeCategory
    department_id: uuid.UUID | None
    doctor_id: uuid.UUID | None
    is_gst_exempt: bool
    gst_rate: Decimal
    hsn_sac_code: str | None
    unit: str | None
    is_active: bool


class ServicePriceUpsert(BaseModel):
    rate_card_id: uuid.UUID
    price: Money
    is_package: bool = False
    notes: Annotated[str | None, Field(max_length=255)] = None


class ServicePriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_item_id: uuid.UUID
    rate_card_id: uuid.UUID
    price: Decimal
    is_package: bool


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
class ChargeAdd(BaseModel):
    """A line added by hand — a dressing, a consumable, an ambulance.

    Most charges arrive automatically from the event bus. This is for what the
    events cannot see.
    """

    encounter_id: uuid.UUID
    service_item_code: Annotated[str | None, Field(max_length=32)] = None
    description: Annotated[str | None, Field(min_length=1, max_length=250)] = None
    category: ChargeCategory = ChargeCategory.OTHER
    quantity: Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=2)] = Decimal("1")
    # Overrides the rate card. Left null, the configured price applies.
    unit_price: Money | None = None
    discount_amount: Money = Decimal("0.00")
    discount_reason: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def _identifiable(self) -> Self:
        if not self.service_item_code and not self.description:
            raise ValueError("Give a service code or a description.")
        if self.discount_amount > 0 and not self.discount_reason:
            # An unexplained discount is the one every auditor asks about.
            raise ValueError("A discount needs a reason.")
        return self


class DiscountRequest(BaseModel):
    amount: Money
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class ReasonRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class ChargeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    source_module: str
    source_type: str
    source_id: uuid.UUID
    service_item_id: uuid.UUID | None
    category: ChargeCategory
    description: str
    quantity: Decimal
    unit_price: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_amount: Decimal
    hsn_sac_code: str | None
    status: ChargeStatus
    invoice_id: uuid.UUID | None
    needs_pricing: bool
    captured_at: datetime


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
class InvoiceDraftRequest(BaseModel):
    """Assemble every pending charge on a visit into a draft.

    `rate_card_id` is optional: left out, the hospital's default card applies,
    which is what a walk-in cash patient gets.
    """

    encounter_id: uuid.UUID
    rate_card_id: uuid.UUID | None = None
    payer_type: PayerType | None = None
    # What an insurer or scheme is expected to cover, so the counter knows what
    # to ask the patient for.
    payer_liability: Money = Decimal("0.00")
    place_of_supply: Annotated[str | None, Field(max_length=100)] = None
    is_interstate: bool = False
    notes: Annotated[str | None, Field(max_length=500)] = None


class InvoiceIssueRequest(BaseModel):
    """Issuing freezes the lines. Nothing here can change them."""

    notes: Annotated[str | None, Field(max_length=500)] = None


class PaymentRequest(BaseModel):
    amount: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    method: PaymentMethod = PaymentMethod.UPI
    reference: Annotated[str | None, Field(max_length=120)] = None
    payer_name: Annotated[str | None, Field(max_length=200)] = None
    received_at: datetime | None = None
    notes: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def _digital_needs_a_reference(self) -> Self:
        # Cash has no transaction id and never will. Everything else does, and a
        # digital payment with no reference cannot be reconciled against the
        # settlement file the next morning.
        needs_reference = {
            PaymentMethod.UPI,
            PaymentMethod.CARD,
            PaymentMethod.NETBANKING,
            PaymentMethod.WALLET,
            PaymentMethod.CHEQUE,
        }
        if self.method in needs_reference and not self.reference:
            raise ValueError(f"A {self.method.value.lower()} payment needs a reference number.")
        return self


class ReversalRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    receipt_number: str
    invoice_id: uuid.UUID
    patient_id: uuid.UUID
    amount: Decimal
    method: PaymentMethod
    reference: str | None
    payer_name: str | None
    status: PaymentStatus
    received_at: datetime
    received_by_id: uuid.UUID | None
    reversed_at: datetime | None
    reversal_reason: str | None


class InvoiceSummary(BaseModel):
    """The worklist row — a busy counter's screen is long."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_number: str
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    status: InvoiceStatus
    payer_type: PayerType
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal
    settlement_context: str | None
    issued_at: datetime | None
    created_at: datetime


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_number: str
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    rate_card_id: uuid.UUID | None
    payer_type: PayerType
    status: InvoiceStatus

    subtotal: Decimal
    discount_total: Decimal
    taxable_total: Decimal
    cgst_total: Decimal
    sgst_total: Decimal
    igst_total: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal
    payer_liability: Decimal

    hospital_gstin: str | None
    place_of_supply: str | None
    is_interstate: bool

    issued_at: datetime | None
    issued_by_id: uuid.UUID | None
    settlement_context: str | None
    settlement_note: str | None
    settled_at: datetime | None
    cancellation_reason: str | None
    write_off_reason: str | None
    credits_invoice_id: uuid.UUID | None
    notes: str | None
    created_at: datetime

    charges: list[ChargeRead] = Field(default_factory=list)
    payments: list[PaymentRead] = Field(default_factory=list)


class AccountBoardEntry(BaseModel):
    """One row on the cash counter's board.

    Carries who the patient is, not just which id they are. It also carries
    `encounter_status`, because that is what tells the cashier whether the
    person is standing in front of them or whether this is a bill left behind by
    a death, a referral or a patient who walked out — three situations that need
    the same money collected in three very different conversations.
    """

    encounter_id: uuid.UUID
    encounter_number: str | None = None
    encounter_status: str | None = None

    patient_id: uuid.UUID | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    # Drives suppression of every "collect from the patient" prompt on the
    # screen. The bill is still owed; the person to ask is not the patient.
    patient_is_deceased: bool = False

    pending_total: Decimal
    invoiced_balance: Decimal
    balance_due: Decimal
    has_unpriced_items: bool
    outstanding_since: datetime


class VisitAccount(BaseModel):
    """Everything the cash counter needs about one visit, in one response.

    Assembled server-side for the same reason as the doctor's chart: a screen
    that opens in one request rather than four is most of the difference between
    hitting the §7b speed targets and missing them.
    """

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    uhid: str
    patient_name: str
    encounter_number: str
    encounter_status: str

    pending_charges: list[ChargeRead]
    invoices: list[InvoiceSummary]

    pending_total: Decimal
    invoiced_total: Decimal
    paid_total: Decimal
    balance_due: Decimal
    # True when something on this visit could not be priced. Shown to the
    # cashier, because they are the last person who can catch it.
    has_unpriced_items: bool


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
class ClaimCreate(BaseModel):
    invoice_id: uuid.UUID
    payer_name: Annotated[str, Field(min_length=1, max_length=200)]
    scheme_code: Annotated[str | None, Field(max_length=32)] = None
    policy_number: Annotated[str | None, Field(max_length=64)] = None
    tpa_name: Annotated[str | None, Field(max_length=200)] = None
    claimed_amount: Money | None = None
    remarks: Annotated[str | None, Field(max_length=1000)] = None


class ClaimUpdate(BaseModel):
    status: ClaimStatus
    approved_amount: Money | None = None
    settled_amount: Money | None = None
    remarks: Annotated[str | None, Field(max_length=1000)] = None

    @model_validator(mode="after")
    def _rejection_needs_a_reason(self) -> Self:
        if self.status is ClaimStatus.REJECTED and not self.remarks:
            # Somebody has to appeal this, and they need to know what to answer.
            raise ValueError("A rejected claim needs the payer's reason in remarks.")
        return self


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_number: str
    invoice_id: uuid.UUID
    patient_id: uuid.UUID
    payer_name: str
    scheme_code: str | None
    policy_number: str | None
    tpa_name: str | None
    status: ClaimStatus
    claimed_amount: Decimal
    approved_amount: Decimal | None
    settled_amount: Decimal | None
    submitted_at: datetime | None
    responded_at: datetime | None
    settled_at: datetime | None
    remarks: str | None
