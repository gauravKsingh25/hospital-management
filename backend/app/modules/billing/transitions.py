"""Legal status changes for invoices, payments and claims.

Same discipline as `clinical/state_machine.py` and `diagnostics/transitions.py`:
the moves are a table, not a chain of conditionals.

The bug this prevents here is a specific one — an issued invoice slipping back
to `DRAFT` and being quietly rebuilt after the patient has walked out with a
printout of the old total. Every route back to an editable state is closed; the
only way to change an issued invoice is to cancel it (before any money arrives)
or to credit it (after).
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import IllegalStateTransitionError
from app.modules.billing.models import ClaimStatus, InvoiceStatus, PaymentStatus

__all__ = [
    "CLAIM_TRANSITIONS",
    "INVOICE_TRANSITIONS",
    "PAYMENT_TRANSITIONS",
    "TERMINAL_INVOICE_STATUSES",
    "assert_claim_transition",
    "assert_invoice_transition",
    "assert_payment_transition",
]


INVOICE_TRANSITIONS: Final[dict[InvoiceStatus, frozenset[InvoiceStatus]]] = {
    InvoiceStatus.DRAFT: frozenset(
        {
            InvoiceStatus.ISSUED,
            InvoiceStatus.CANCELLED,
        }
    ),
    InvoiceStatus.ISSUED: frozenset(
        {
            InvoiceStatus.PARTIALLY_PAID,
            # A single payment can settle it outright.
            InvoiceStatus.PAID,
            # Only while nothing has been received against it. Once money has
            # arrived the remedy is a credit note, never a cancellation.
            InvoiceStatus.CANCELLED,
            InvoiceStatus.WRITTEN_OFF,
        }
    ),
    InvoiceStatus.PARTIALLY_PAID: frozenset(
        {
            InvoiceStatus.PAID,
            # A reversal can take a part-paid invoice back to nothing received.
            InvoiceStatus.ISSUED,
            InvoiceStatus.WRITTEN_OFF,
        }
    ),
    InvoiceStatus.PAID: frozenset(
        {
            # A reversed payment reopens it. The invoice itself is untouched —
            # what changed is how much has been received against it.
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.ISSUED,
        }
    ),
    InvoiceStatus.CANCELLED: frozenset(),
    # Bad debt. Deliberately terminal: an invoice somebody decided to stop
    # chasing should be revived by raising a fresh one, with a fresh decision
    # behind it, not by quietly flipping a status back.
    InvoiceStatus.WRITTEN_OFF: frozenset(),
}

TERMINAL_INVOICE_STATUSES: Final[frozenset[InvoiceStatus]] = frozenset(
    status for status, allowed in INVOICE_TRANSITIONS.items() if not allowed
)


PAYMENT_TRANSITIONS: Final[dict[PaymentStatus, frozenset[PaymentStatus]]] = {
    PaymentStatus.RECORDED: frozenset({PaymentStatus.REVERSED}),
    # A reversal is final. Re-taking the money is a new receipt with its own
    # number, because that is what the patient is handed.
    PaymentStatus.REVERSED: frozenset(),
}


CLAIM_TRANSITIONS: Final[dict[ClaimStatus, frozenset[ClaimStatus]]] = {
    ClaimStatus.DRAFT: frozenset({ClaimStatus.SUBMITTED, ClaimStatus.REJECTED}),
    ClaimStatus.SUBMITTED: frozenset(
        {ClaimStatus.QUERIED, ClaimStatus.APPROVED, ClaimStatus.REJECTED}
    ),
    # A query is answered and the claim goes back into the queue.
    ClaimStatus.QUERIED: frozenset(
        {ClaimStatus.SUBMITTED, ClaimStatus.APPROVED, ClaimStatus.REJECTED}
    ),
    ClaimStatus.APPROVED: frozenset({ClaimStatus.SETTLED, ClaimStatus.REJECTED}),
    ClaimStatus.REJECTED: frozenset({ClaimStatus.SUBMITTED}),
    ClaimStatus.SETTLED: frozenset(),
}


def _phrase(value: str) -> str:
    return value.lower().replace("_", " ")


def _assert(current: str, target: str, allowed: frozenset[object], noun: str) -> None:
    """Shared refusal, phrased for the person reading the screen."""
    allowed_values = sorted(str(getattr(item, "value", item)) for item in allowed)

    if current == target:
        raise IllegalStateTransitionError(
            f"The {noun} is already {_phrase(current)}.",
            details={"from": current, "to": target},
        )
    if target not in allowed_values:
        detail = (
            f"An {noun} that is {_phrase(current)} cannot become {_phrase(target)}."
            if allowed_values
            else f"An {noun} that is {_phrase(current)} is closed and cannot change."
        )
        raise IllegalStateTransitionError(
            detail,
            details={"from": current, "to": target, "allowed": allowed_values},
        )


def assert_invoice_transition(current: InvoiceStatus, target: InvoiceStatus) -> None:
    _assert(current.value, target.value, INVOICE_TRANSITIONS.get(current, frozenset()), "invoice")


def assert_payment_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    _assert(current.value, target.value, PAYMENT_TRANSITIONS.get(current, frozenset()), "payment")


def assert_claim_transition(current: ClaimStatus, target: ClaimStatus) -> None:
    _assert(current.value, target.value, CLAIM_TRANSITIONS.get(current, frozenset()), "claim")
