"""Domain events published by `billing`.

Thin, like every other module's: identifiers and the few facts a subscriber
cannot cheaply re-derive.

`notifications` (Phase 8) is the intended consumer of all of these — the payment
receipt on WhatsApp, the outstanding-balance reminder, and the one that needs
care: `SettlementRequired`. A bill left owing by a deceased patient still has to
reach somebody, and that somebody is never the patient (CLAUDE.md §14). The
event therefore carries `notify_patient=False` for a death, so the suppression
rule does not depend on a dispatcher remembering to check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.core.events import DomainEvent

__all__ = [
    "ChargeCaptured",
    "ChargeNeedsPricing",
    "InvoiceIssued",
    "InvoicePaid",
    "PaymentReceived",
    "PaymentReversed",
    "SettlementRequired",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class ChargeCaptured(DomainEvent):
    """A billable act landed on a visit's running bill."""

    charge_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    category: str
    description: str
    total_amount: Decimal


@dataclass(frozen=True, kw_only=True, slots=True)
class ChargeNeedsPricing(DomainEvent):
    """An act was captured with no price, because none is configured.

    Its own event rather than a flag on `ChargeCaptured` so that it can be
    routed somewhere a human looks. A hospital delivering care it cannot price
    is losing money quietly, and quietly is the problem.
    """

    charge_id: uuid.UUID
    encounter_id: uuid.UUID
    description: str
    source_module: str
    item_code: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class InvoiceIssued(DomainEvent):
    invoice_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    invoice_number: str
    grand_total: Decimal
    balance_due: Decimal


@dataclass(frozen=True, kw_only=True, slots=True)
class PaymentReceived(DomainEvent):
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    patient_id: uuid.UUID
    receipt_number: str
    amount: Decimal
    method: str
    balance_due: Decimal


@dataclass(frozen=True, kw_only=True, slots=True)
class PaymentReversed(DomainEvent):
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    patient_id: uuid.UUID
    amount: Decimal
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class InvoicePaid(DomainEvent):
    invoice_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    invoice_number: str
    grand_total: Decimal


@dataclass(frozen=True, kw_only=True, slots=True)
class SettlementRequired(DomainEvent):
    """A visit closed owing money (CLAUDE.md §6).

    `context` is the terminal status that triggered it — COMPLETED,
    REFERRED_OUT, LAMA or DECEASED. `notify_patient` is false for a death: the
    bill is real and has to be settled with the family in person, but no
    automated message goes out on it, ever.
    """

    invoice_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    context: str
    balance_due: Decimal
    notify_patient: bool
