"""Money: the price list, the running bill, the document, and what was paid.

Four ideas, in the order they happen.

**Rate cards** (CLAUDE.md §9). A service does not have *a* price; it has a price
per payer. The cash rate, the rate negotiated with an insurer, and the PMJAY or
CGHS package rate are three different numbers for the same act, and a hospital
that stores one price per service ends up maintaining the other two in a
spreadsheet. So price lives on `service_prices`, keyed by (service, rate card),
and the invoice records which card it was raised on.

**Charges** are the running bill: one row per billable act, captured
automatically as the act happens (CLAUDE.md §13.7). This is the ledger, and two
properties make it trustworthy:

  * It is **idempotent by source.** A partial unique index on
    (source_module, source_type, source_id) means a re-published event, a
    double-click or a retried request cannot bill a patient twice for one CBC.
  * It is **total.** An act with no price in the rate card is still captured —
    at zero, flagged `needs_pricing` — because a charge that silently fails to
    record is revenue the hospital never learns it lost. A visible zero on a
    billing exception list is recoverable; a missing row is not.

**Invoices** are documents, not views. Once issued, an invoice does not change
under the person holding the printout: the lines are frozen, and a correction is
a credit note that references it. This is also where GST lives, per line, because
CLAUDE.md §9 is right that clinical services are largely exempt while pharmacy,
diagnostics and room rent may not be — tax is an attribute of the line, never a
rate applied to the total.

**Payments** are append-only in spirit. A wrong payment is reversed by a second
row, never edited away, because "we took ₹2,000 and gave it back" and "we never
took it" are different facts and a cash drawer has to reconcile against the first.

One deliberate omission worth naming: there is **no separate invoice-line
table.** A charge already snapshots its description, price and tax; an invoice
owns its charges and freezes them. A second table would be a second place for
the same line to live, and the two would eventually disagree. The cost of this
choice is that a charge belongs to exactly one invoice — which is what a credit
note is for.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Numeric, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "Charge",
    "ChargeCategory",
    "ChargeStatus",
    "ClaimStatus",
    "InsuranceClaim",
    "Invoice",
    "InvoiceSequence",
    "InvoiceStatus",
    "PayerType",
    "Payment",
    "PaymentMethod",
    "PaymentStatus",
    "RateCard",
    "ServiceItem",
    "ServicePrice",
]

# Money is `Numeric(12, 2)` throughout — never a float. A float rupee is a
# rounding error waiting to be argued about at a cash counter, and Python's
# `0.1 + 0.2` is not a conversation anybody wants to have with a patient.
_MONEY = Numeric(12, 2)
_RATE = Numeric(5, 2)


class PayerType(enum.StrEnum):
    """Who ultimately settles. Drives which rate card applies."""

    CASH = "CASH"
    INSURANCE = "INSURANCE"
    # PMJAY, CGHS, ECHS, state schemes — packaged rates, not per-item pricing.
    SCHEME = "SCHEME"
    CORPORATE = "CORPORATE"


class ChargeCategory(enum.StrEnum):
    """What kind of act was billed.

    Matters beyond reporting: GST treatment differs by category, and so does
    who is allowed to waive it.
    """

    CONSULTATION = "CONSULTATION"
    LAB = "LAB"
    RADIOLOGY = "RADIOLOGY"
    PROCEDURE = "PROCEDURE"
    PHARMACY = "PHARMACY"
    ROOM = "ROOM"
    REGISTRATION = "REGISTRATION"
    OTHER = "OTHER"


class ChargeStatus(enum.StrEnum):
    # Captured, on the running bill, not yet on a document.
    PENDING = "PENDING"
    INVOICED = "INVOICED"
    # The underlying act was cancelled before it was invoiced.
    CANCELLED = "CANCELLED"
    # Deliberately not charged — a concession, a staff discount, a goodwill
    # write-off. Distinct from CANCELLED: the act happened, the money did not.
    WAIVED = "WAIVED"


class InvoiceStatus(enum.StrEnum):
    # Assembled from charges, still editable, not a document yet.
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    # Given up on: bad debt, an absconded patient, a concession after issue.
    WRITTEN_OFF = "WRITTEN_OFF"


class PaymentMethod(enum.StrEnum):
    """UPI first, because in an Indian hospital in 2026 it usually is
    (CLAUDE.md §9)."""

    UPI = "UPI"
    CARD = "CARD"
    CASH = "CASH"
    NETBANKING = "NETBANKING"
    WALLET = "WALLET"
    CHEQUE = "CHEQUE"
    # Settled by the insurer or the scheme rather than by the patient.
    INSURANCE = "INSURANCE"
    SCHEME = "SCHEME"
    # A correcting entry, e.g. an advance moved between invoices.
    ADJUSTMENT = "ADJUSTMENT"


class PaymentStatus(enum.StrEnum):
    RECORDED = "RECORDED"
    # Reversed by a later entry. The original row stays.
    REVERSED = "REVERSED"


class ClaimStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    # The TPA has asked a question and the clock is on the hospital.
    QUERIED = "QUERIED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SETTLED = "SETTLED"


class RateCard(AuditedTenantModel, table=True):
    """A named price list for one payer.

    Exactly one card per hospital may be the default; it is the one a walk-in
    cash patient gets, and the fallback whenever a patient's scheme has no card
    configured. Enforced in the service rather than by a unique index for the
    same reason as the primary diagnosis: "make *that* one the default" has to
    be a single call, not a delete-then-insert someone can leave half done.
    """

    __tablename__ = "rate_cards"
    __table_args__ = (
        Index(
            "uq_rate_cards_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_rate_cards_hospital_id_payer_type", "hospital_id", "payer_type"),
    )

    code: str = Field(max_length=32, index=True, description="e.g. 'CASH', 'PMJAY', 'STAR-TPA'.")
    name: str = Field(max_length=120)
    payer_type: PayerType = Field(default=PayerType.CASH, index=True)
    # PMJAY / CGHS / ECHS / an insurer's short code. Free text because the list
    # grows with every state scheme and an enum would need a migration each time.
    scheme_code: str | None = Field(default=None, max_length=32, index=True)

    is_default: bool = Field(default=False, index=True)
    valid_from: date | None = Field(default=None, sa_type=Date)
    valid_to: date | None = Field(default=None, sa_type=Date)
    is_active: bool = Field(default=True, index=True)
    notes: str | None = Field(default=None, max_length=500)


class ServiceItem(AuditedTenantModel, table=True):
    """One billable thing, independent of what it costs.

    The link to the rest of the system is `code`. A diagnostics catalogue item
    with code `CBC` prices against the service item with code `CBC`; a doctor's
    order for `CBC` does the same. That is a deliberately loose coupling —
    billing never reads another module's tables (CLAUDE.md §2), and a hospital
    that has not yet mapped a test still gets the charge captured, flagged for
    pricing.

    `doctor_id` exists because consultation fees are per consultant far more
    often than not. Null means the hospital-wide default for that category,
    which is what an unmapped doctor falls back to.
    """

    __tablename__ = "service_items"
    __table_args__ = (
        Index(
            "uq_service_items_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_service_items_hospital_id_category", "hospital_id", "category"),
        # Resolving "what does this doctor's consultation cost?".
        Index("ix_service_items_doctor_id_category", "doctor_id", "category"),
    )

    code: str = Field(max_length=32, index=True)
    name: str = Field(max_length=200, index=True)
    category: ChargeCategory = Field(default=ChargeCategory.OTHER, index=True)

    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    doctor_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="doctors.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )

    # --- GST (CLAUDE.md §9: tax is a per-line-item attribute) --------------
    # Healthcare services by a clinical establishment are exempt under
    # Notification 12/2017 Sl. 74; pharmacy sales and room rent above the
    # threshold are not. Defaulting to exempt is the safe direction: an
    # accidentally taxed exempt service overcharges a patient, which is worse
    # than an untaxed line an accountant catches at filing.
    is_gst_exempt: bool = Field(default=True)
    gst_rate: Decimal = Field(default=Decimal("0.00"), sa_type=_RATE)  # type: ignore[call-overload]
    hsn_sac_code: str | None = Field(
        default=None,
        max_length=12,
        description="HSN (goods) or SAC (services), printed on the tax invoice.",
    )

    unit: str | None = Field(default=None, max_length=24, description="e.g. 'per day', 'per test'.")
    is_active: bool = Field(default=True, index=True)


class ServicePrice(AuditedTenantModel, table=True):
    """What one service costs on one rate card.

    The row that makes multi-rate billing real rather than aspirational.
    """

    __tablename__ = "service_prices"
    __table_args__ = (
        Index(
            "uq_service_prices_item_card_live",
            "service_item_id",
            "rate_card_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    service_item_id: uuid.UUID = Field(
        foreign_key="service_items.id", ondelete="CASCADE", index=True
    )
    rate_card_id: uuid.UUID = Field(foreign_key="rate_cards.id", ondelete="CASCADE", index=True)

    price: Decimal = Field(sa_type=_MONEY)  # type: ignore[call-overload]
    # A scheme package price covers a bundle and is not further itemised. Worth
    # recording because it changes what the claim form says, not just the number.
    is_package: bool = Field(default=False)
    notes: str | None = Field(default=None, max_length=255)


class Charge(AuditedTenantModel, table=True):
    """One billable line on a visit's running bill.

    Captured by a transactional event subscriber the moment the act happens, so
    reception reviews an invoice rather than reconstructing one (CLAUDE.md §7b).
    """

    __tablename__ = "charges"
    __table_args__ = (
        # THE idempotency rule. One charge per source act, ever. A re-delivered
        # event, a double-click or a retried request all collapse onto the same
        # row instead of billing the patient twice. Cancelled charges are
        # excluded so a cancelled-then-reordered test can be charged again.
        Index(
            "uq_charges_source_live",
            "hospital_id",
            "source_module",
            "source_type",
            "source_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status <> 'CANCELLED'"),
        ),
        # The running bill for a visit, and the cashier's screen.
        Index("ix_charges_encounter_id_status", "encounter_id", "status"),
        Index("ix_charges_invoice_id", "invoice_id"),
        # The billing exception worklist: acts nobody has priced.
        Index("ix_charges_hospital_id_needs_pricing", "hospital_id", "needs_pricing"),
    )

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    # --- provenance: which act produced this line -------------------------
    source_module: str = Field(max_length=32, index=True, description="'clinical', 'diagnostics'.")
    source_type: str = Field(max_length=32, description="'order', 'encounter', 'admission'.")
    source_id: uuid.UUID = Field(index=True)

    service_item_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="service_items.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    rate_card_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="rate_cards.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    category: ChargeCategory = Field(default=ChargeCategory.OTHER, index=True)
    # Snapshotted, like every other name in this system: renaming a service next
    # year must not rewrite an invoice already handed to a patient.
    description: str = Field(max_length=250)

    quantity: Decimal = Field(default=Decimal("1.00"), sa_type=Numeric(9, 2))  # type: ignore[call-overload]
    unit_price: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    discount_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    discount_reason: str | None = Field(default=None, max_length=255)

    # --- GST, computed per line and stored, never recomputed on read ------
    taxable_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    gst_rate: Decimal = Field(default=Decimal("0.00"), sa_type=_RATE)  # type: ignore[call-overload]
    cgst_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    sgst_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    igst_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    total_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    hsn_sac_code: str | None = Field(default=None, max_length=12)

    status: ChargeStatus = Field(default=ChargeStatus.PENDING, index=True)
    invoice_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="invoices.id",
        ondelete="RESTRICT",
        nullable=True,
    )

    # True when no price could be resolved. The act is still on the bill at
    # zero, and on somebody's exception list — which is the whole point.
    needs_pricing: bool = Field(default=False, index=True)

    captured_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    captured_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    waived_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    waiver_reason: str | None = Field(default=None, max_length=255)
    cancellation_reason: str | None = Field(default=None, max_length=255)


class Invoice(AuditedTenantModel, table=True):
    """A bill as a document.

    `DRAFT` is a working total. From `ISSUED` onwards the numbers are frozen:
    the charges it owns can no longer be edited, and a correction is a credit
    note. That is not bureaucracy — a patient walks away with a printout, and a
    document that can be changed afterwards is not evidence of anything.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        Index(
            "uq_invoices_number_live",
            "hospital_id",
            "invoice_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one live draft per visit, so two staff members cannot each
        # build half a bill. Issued invoices are excluded: a long stay may
        # legitimately produce several.
        Index(
            "uq_invoices_encounter_draft",
            "encounter_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status = 'DRAFT'"),
        ),
        Index("ix_invoices_hospital_id_status", "hospital_id", "status"),
        Index("ix_invoices_patient_id_created_at", "patient_id", "created_at"),
        # The outstanding-money read, which is also the closure gate.
        Index("ix_invoices_encounter_id_status", "encounter_id", "status"),
    )

    invoice_number: str = Field(max_length=32, index=True)

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    rate_card_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="rate_cards.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    payer_type: PayerType = Field(default=PayerType.CASH, index=True)

    status: InvoiceStatus = Field(default=InvoiceStatus.DRAFT, index=True)

    # --- totals, all derived from the charges and stored ------------------
    subtotal: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    discount_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    taxable_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    cgst_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    sgst_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    igst_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    tax_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    # A GST invoice is presented to the nearest rupee, with the difference shown
    # as its own line. Storing both means the printed total and the ledger agree.
    round_off: Decimal = Field(default=Decimal("0.00"), sa_type=Numeric(6, 2))  # type: ignore[call-overload]
    grand_total: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]

    amount_paid: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    balance_due: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    # What an insurer or scheme is expected to cover. The patient owes the rest,
    # and the counter needs that split before they can take a payment.
    payer_liability: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]

    # --- statutory snapshot -----------------------------------------------
    # Copied at issue: a hospital that changes its registration must not rewrite
    # the GSTIN on invoices already filed under the old one.
    hospital_gstin: str | None = Field(default=None, max_length=15)
    place_of_supply: str | None = Field(default=None, max_length=100)
    # Intra-state is CGST + SGST; inter-state is IGST. Almost always false for
    # in-person care, and it has to be recorded rather than assumed.
    is_interstate: bool = Field(default=False)

    issued_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    issued_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )

    # --- settlement (CLAUDE.md §6: deceased and referred patients still owe) --
    # Why this bill needs a person: 'DECEASED', 'REFERRED_OUT', 'LAMA'. Set when
    # a visit closes on one of the edge cases, so the outstanding amount lands on
    # a worklist instead of leaving with the patient.
    settlement_context: str | None = Field(default=None, max_length=32, index=True)
    settlement_note: str | None = Field(default=None, max_length=500)
    settled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    cancellation_reason: str | None = Field(default=None, max_length=255)
    write_off_reason: str | None = Field(default=None, max_length=255)
    write_off_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    # Set on a credit note, pointing at the invoice it corrects.
    credits_invoice_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="invoices.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    notes: str | None = Field(default=None, max_length=500)

    @property
    def is_open(self) -> bool:
        """Whether money is still expected against this invoice."""
        return self.status in (
            InvoiceStatus.DRAFT,
            InvoiceStatus.ISSUED,
            InvoiceStatus.PARTIALLY_PAID,
        )

    @property
    def is_frozen(self) -> bool:
        """Whether the lines may still change. Everything past DRAFT is a document."""
        return self.status is not InvoiceStatus.DRAFT


class Payment(AuditedTenantModel, table=True):
    """Money received against an invoice.

    Never edited. A mistake is corrected by recording the reversal, because the
    cash drawer at the end of the shift has to reconcile against what actually
    happened, not against what somebody wishes had.
    """

    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "uq_payments_receipt_live",
            "hospital_id",
            "receipt_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_payments_invoice_id_status", "invoice_id", "status"),
        # The day's collection report, by method.
        Index("ix_payments_hospital_id_received_at", "hospital_id", "received_at"),
    )

    receipt_number: str = Field(max_length=32, index=True)

    invoice_id: uuid.UUID = Field(foreign_key="invoices.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    amount: Decimal = Field(sa_type=_MONEY)  # type: ignore[call-overload]
    method: PaymentMethod = Field(default=PaymentMethod.UPI, index=True)
    # The UPI transaction id, the card's approval code, the cheque number. What
    # a dispute is resolved with.
    reference: str | None = Field(default=None, max_length=120, index=True)
    payer_name: str | None = Field(
        default=None, max_length=200, description="When somebody other than the patient pays."
    )

    status: PaymentStatus = Field(default=PaymentStatus.RECORDED, index=True)

    received_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    received_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )

    reversed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    reversal_reason: str | None = Field(default=None, max_length=255)
    reverses_payment_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="payments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    notes: str | None = Field(default=None, max_length=500)


class InsuranceClaim(AuditedTenantModel, table=True):
    """A claim against an insurer or a government scheme.

    Kept deliberately small. This records the hospital's side of the
    conversation — what was claimed, what came back, and what was finally paid —
    without pretending to be a TPA portal integration. The gap between claimed
    and settled is the number a finance officer actually chases.
    """

    __tablename__ = "insurance_claims"
    __table_args__ = (
        Index("ix_insurance_claims_hospital_id_status", "hospital_id", "status"),
        Index("ix_insurance_claims_invoice_id", "invoice_id"),
    )

    claim_number: str = Field(max_length=48, index=True)

    invoice_id: uuid.UUID = Field(foreign_key="invoices.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    payer_name: str = Field(
        max_length=200, description="Insurer or scheme, as it appears on the card."
    )
    scheme_code: str | None = Field(default=None, max_length=32, index=True)
    policy_number: str | None = Field(default=None, max_length=64)
    tpa_name: str | None = Field(default=None, max_length=200)

    status: ClaimStatus = Field(default=ClaimStatus.DRAFT, index=True)

    claimed_amount: Decimal = Field(default=Decimal("0.00"), sa_type=_MONEY)  # type: ignore[call-overload]
    approved_amount: Decimal | None = Field(default=None, sa_type=_MONEY)  # type: ignore[call-overload]
    settled_amount: Decimal | None = Field(default=None, sa_type=_MONEY)  # type: ignore[call-overload]

    submitted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    responded_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    settled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Why it was rejected, or what the TPA asked. The thing somebody has to act
    # on, so it is a column rather than a note nobody reads.
    remarks: str | None = Field(default=None, max_length=1000)


class InvoiceSequence(TenantModel, table=True):
    """Per-hospital counter for invoice and receipt numbers.

    Unlike every other counter in this system it restarts on the **financial**
    year, not the calendar year. That is not a stylistic choice: rule 46 of the
    CGST Rules requires a consecutive serial number unique for a financial year,
    and India's financial year runs April to March. A calendar-year series would
    be a compliance defect discovered at the worst possible moment — during an
    audit, retroactively, across every invoice already issued.
    """

    __tablename__ = "invoice_sequences"
    __table_args__ = (
        Index(
            "uq_invoice_sequences_hospital_fy_kind",
            "hospital_id",
            "financial_year",
            "kind",
            unique=True,
        ),
    )

    # The starting calendar year: 2026 means FY 2026-27 (April 2026 - March 2027).
    financial_year: int = Field(index=True)
    kind: str = Field(max_length=16, description="'INV' for invoices, 'RCP' for receipts.")
    last_value: int = Field(default=0)
