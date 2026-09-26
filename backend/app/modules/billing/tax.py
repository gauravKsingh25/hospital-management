"""GST arithmetic and money rounding. Pure functions, no database.

Separated from the service so the tax rules can be read, reviewed and tested by
someone who does not want to read SQLAlchemy — and so that an accountant
disagreeing with a number has one file to point at.

Three rules, all of them CLAUDE.md §9 or Indian GST law rather than preference:

**Tax is per line, never on the total.** A hospital bill routinely mixes an
exempt consultation with a taxable pharmacy item. Applying a blended rate to the
grand total gives a number that is wrong on both lines and cannot be defended on
a GSTR-1 return.

**Intra-state splits into CGST + SGST; inter-state is IGST.** For in-person care
the place of supply is the hospital, so it is essentially always the split. The
half that does not apply must be zero rather than absent, because a tax invoice
prints all three columns.

**The document rounds; the ledger does not.** Section 170 of the CGST Act rounds
the payable amount to the nearest rupee. The rounding is carried as its own
`round_off` line so the printed total and the sum of the lines can both be true.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "InvoiceTotals",
    "LineTax",
    "compute_line",
    "money",
    "rate",
    "round_to_rupee",
    "sum_lines",
]

_PAISE = Decimal("0.01")
_RUPEE = Decimal("1")
ZERO = Decimal("0.00")


def money(value: Decimal | int | str) -> Decimal:
    """Quantise to paise, rounding half away from zero.

    `ROUND_HALF_UP` rather than Python's banker's-rounding default: a patient
    handed a bill expects 0.125 to become 0.13, and "the computer rounds to even"
    is not an explanation anybody at a cash counter wants to give.
    """
    return Decimal(value).quantize(_PAISE, rounding=ROUND_HALF_UP)


def rate(value: Decimal | int | str) -> Decimal:
    """Quantise a percentage to two places (GST rates are 0, 5, 12, 18, 28)."""
    return Decimal(value).quantize(_PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class LineTax:
    """One line's money, fully broken out."""

    taxable_amount: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_amount: Decimal

    @property
    def tax_amount(self) -> Decimal:
        return money(self.cgst_amount + self.sgst_amount + self.igst_amount)


def compute_line(
    *,
    unit_price: Decimal,
    quantity: Decimal = Decimal("1"),
    discount_amount: Decimal = ZERO,
    gst_rate: Decimal = ZERO,
    is_exempt: bool = True,
    interstate: bool = False,
) -> LineTax:
    """Price one line, discount it, then tax what is left.

    The order matters and it is the legal one: GST is charged on the transaction
    value *after* any discount shown on the invoice (CGST Act §15(3)). Taxing
    before the discount would overcharge both the patient and the exchequer.

    A discount larger than the line is clamped rather than allowed to go
    negative. A negative charge is a credit note — a different document with its
    own number — and letting one appear as a line here would let somebody build
    a refund that nothing audits.
    """
    gross = money(money(unit_price) * Decimal(quantity))
    discount = min(money(discount_amount), gross)
    taxable = money(gross - discount)

    if is_exempt or gst_rate <= 0:
        return LineTax(
            taxable_amount=taxable,
            gst_rate=ZERO,
            cgst_amount=ZERO,
            sgst_amount=ZERO,
            igst_amount=ZERO,
            total_amount=taxable,
        )

    applied = rate(gst_rate)
    tax = money(taxable * applied / Decimal(100))

    if interstate:
        cgst = sgst = ZERO
        igst = tax
    else:
        # Halve the *tax*, not the rate, then give the odd paisa to CGST so the
        # two halves always add back to the total. Halving the rate first and
        # rounding twice loses a paisa on roughly half of all lines.
        cgst = money(tax / Decimal(2))
        sgst = money(tax - cgst)
        igst = ZERO

    return LineTax(
        taxable_amount=taxable,
        gst_rate=applied,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
        total_amount=money(taxable + tax),
    )


def round_to_rupee(amount: Decimal) -> tuple[Decimal, Decimal]:
    """Round a payable amount to the nearest rupee.

    Returns `(rounded_total, round_off)` where `round_off` is what was added —
    negative when the amount was rounded down. Both are stored, because the
    printed total and the sum of the lines have to reconcile.
    """
    exact = money(amount)
    rounded = exact.quantize(_RUPEE, rounding=ROUND_HALF_UP)
    return money(rounded), money(rounded - exact)


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    """The footer of an invoice."""

    subtotal: Decimal
    discount_total: Decimal
    taxable_total: Decimal
    cgst_total: Decimal
    sgst_total: Decimal
    igst_total: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal


def sum_lines(
    lines: list[tuple[Decimal, Decimal, LineTax]], *, round_total: bool = True
) -> InvoiceTotals:
    """Add up `(gross, discount, tax)` triples into an invoice footer.

    Totals are summed from the stored per-line figures rather than recomputed
    from rates. Recomputing would give a different answer whenever rounding bit
    on an individual line, and then the invoice would not equal its own lines.
    """
    subtotal = ZERO
    discount_total = ZERO
    taxable_total = ZERO
    cgst_total = ZERO
    sgst_total = ZERO
    igst_total = ZERO

    for gross, discount, tax in lines:
        subtotal += gross
        discount_total += discount
        taxable_total += tax.taxable_amount
        cgst_total += tax.cgst_amount
        sgst_total += tax.sgst_amount
        igst_total += tax.igst_amount

    tax_total = money(cgst_total + sgst_total + igst_total)
    payable = money(money(taxable_total) + tax_total)
    grand_total, round_off = round_to_rupee(payable) if round_total else (payable, ZERO)

    return InvoiceTotals(
        subtotal=money(subtotal),
        discount_total=money(discount_total),
        taxable_total=money(taxable_total),
        cgst_total=money(cgst_total),
        sgst_total=money(sgst_total),
        igst_total=money(igst_total),
        tax_total=tax_total,
        round_off=round_off,
        grand_total=grand_total,
    )
