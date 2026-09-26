"""GST arithmetic and money rounding.

No database. These are the numbers a patient reads off a printout and an
accountant defends on a return, so they are tested against worked examples
rather than against the implementation's own idea of itself.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.billing.tax import (
    compute_line,
    money,
    round_to_rupee,
    sum_lines,
)


def D(value: str) -> Decimal:  # noqa: N802 - reads better than `dec` in assertions
    return Decimal(value)


# ---------------------------------------------------------------------------
# The exempt case, which is most of a hospital bill
# ---------------------------------------------------------------------------
def test_a_consultation_is_exempt_and_carries_no_tax() -> None:
    """Healthcare by a clinical establishment is exempt (Notification 12/2017)."""
    line = compute_line(unit_price=D("500.00"), is_exempt=True)

    assert line.taxable_amount == D("500.00")
    assert line.gst_rate == D("0.00")
    assert line.cgst_amount == D("0.00")
    assert line.sgst_amount == D("0.00")
    assert line.igst_amount == D("0.00")
    assert line.total_amount == D("500.00")


def test_a_taxable_rate_of_zero_is_treated_as_exempt() -> None:
    """Belt and braces: a service marked taxable but left at 0% must not
    produce a tax line of zero rupees, which would print as a real tax."""
    line = compute_line(unit_price=D("100.00"), is_exempt=False, gst_rate=D("0"))
    assert line.total_amount == D("100.00")
    assert line.gst_rate == D("0.00")


# ---------------------------------------------------------------------------
# The taxable case
# ---------------------------------------------------------------------------
def test_intra_state_supply_splits_into_cgst_and_sgst() -> None:
    """A ₹1,000 pharmacy item at 12% is ₹60 CGST + ₹60 SGST, no IGST."""
    line = compute_line(unit_price=D("1000.00"), is_exempt=False, gst_rate=D("12.00"))

    assert line.taxable_amount == D("1000.00")
    assert line.cgst_amount == D("60.00")
    assert line.sgst_amount == D("60.00")
    assert line.igst_amount == D("0.00")
    assert line.total_amount == D("1120.00")
    assert line.tax_amount == D("120.00")


def test_inter_state_supply_is_all_igst() -> None:
    line = compute_line(
        unit_price=D("1000.00"), is_exempt=False, gst_rate=D("18.00"), interstate=True
    )

    assert line.igst_amount == D("180.00")
    assert line.cgst_amount == D("0.00")
    assert line.sgst_amount == D("0.00")
    assert line.total_amount == D("1180.00")


def test_the_two_halves_always_add_back_to_the_whole_tax() -> None:
    """The odd-paisa case.

    ₹333.33 at 5% is ₹16.6665 of tax, which quantises to ₹16.67. Halving the
    *rate* first and rounding twice would give 8.33 + 8.33 = 16.66 and lose a
    paisa; halving the tax and giving the remainder to CGST cannot.
    """
    line = compute_line(unit_price=D("333.33"), is_exempt=False, gst_rate=D("5.00"))

    assert line.cgst_amount + line.sgst_amount == line.tax_amount
    assert line.tax_amount == D("16.67")
    assert line.cgst_amount == D("8.34")
    assert line.sgst_amount == D("8.33")


def test_quantity_multiplies_before_tax() -> None:
    line = compute_line(
        unit_price=D("250.00"), quantity=D("3"), is_exempt=False, gst_rate=D("18.00")
    )
    assert line.taxable_amount == D("750.00")
    assert line.total_amount == D("885.00")


# ---------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------
def test_tax_is_charged_after_the_discount_not_before() -> None:
    """CGST Act §15(3): GST applies to the transaction value net of a discount
    shown on the invoice. Taxing first would overcharge patient and exchequer."""
    line = compute_line(
        unit_price=D("1000.00"),
        discount_amount=D("100.00"),
        is_exempt=False,
        gst_rate=D("12.00"),
    )

    assert line.taxable_amount == D("900.00")
    assert line.tax_amount == D("108.00")
    assert line.total_amount == D("1008.00")


def test_a_discount_larger_than_the_line_cannot_make_it_negative() -> None:
    """A negative charge is a credit note — a different document with its own
    number — not a line that quietly appears on a bill."""
    line = compute_line(unit_price=D("500.00"), discount_amount=D("900.00"))

    assert line.taxable_amount == D("0.00")
    assert line.total_amount == D("0.00")


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("amount", "expected_total", "expected_round_off"),
    [
        ("1120.00", "1120.00", "0.00"),
        # Rounded up: the hospital collects 40 paise more.
        ("1119.60", "1120.00", "0.40"),
        # Rounded down: the hospital forgoes 30 paise.
        ("1120.30", "1120.00", "-0.30"),
        # Exactly half rounds away from zero, as a patient expects.
        ("1120.50", "1121.00", "0.50"),
    ],
)
def test_the_payable_total_rounds_to_the_nearest_rupee(
    amount: str, expected_total: str, expected_round_off: str
) -> None:
    total, round_off = round_to_rupee(D(amount))
    assert total == D(expected_total)
    assert round_off == D(expected_round_off)
    # The stored pair must always reconcile back to the exact amount.
    assert total - round_off == money(D(amount))


def test_money_rounds_half_up_not_to_even() -> None:
    """Python's default is banker's rounding, which would make 0.125 -> 0.12.
    "The computer rounds to even" is not an explanation for a cash counter."""
    assert money(D("0.125")) == D("0.13")
    assert money(D("0.135")) == D("0.14")


# ---------------------------------------------------------------------------
# Invoice footers
# ---------------------------------------------------------------------------
def test_an_invoice_footer_adds_up_its_own_lines() -> None:
    """A realistic OPD bill: an exempt consultation, an exempt lab test, and a
    taxable pharmacy item. Applying one blended rate to the total would be
    wrong on every line."""
    consultation = compute_line(unit_price=D("500.00"), is_exempt=True)
    lab = compute_line(unit_price=D("350.00"), is_exempt=True)
    pharmacy = compute_line(unit_price=D("240.00"), is_exempt=False, gst_rate=D("12.00"))

    totals = sum_lines(
        [
            (D("500.00"), D("0.00"), consultation),
            (D("350.00"), D("0.00"), lab),
            (D("240.00"), D("0.00"), pharmacy),
        ]
    )

    assert totals.subtotal == D("1090.00")
    assert totals.taxable_total == D("1090.00")
    assert totals.cgst_total == D("14.40")
    assert totals.sgst_total == D("14.40")
    assert totals.tax_total == D("28.80")
    # 1118.80 payable -> 1119 presented, with 20 paise carried as round-off.
    assert totals.grand_total == D("1119.00")
    assert totals.round_off == D("0.20")


def test_an_empty_invoice_totals_to_zero_rather_than_failing() -> None:
    totals = sum_lines([])
    assert totals.grand_total == D("0.00")
    assert totals.tax_total == D("0.00")


def test_discounts_are_carried_into_the_footer() -> None:
    line = compute_line(unit_price=D("1000.00"), discount_amount=D("250.00"))
    totals = sum_lines([(D("1000.00"), D("250.00"), line)])

    assert totals.subtotal == D("1000.00")
    assert totals.discount_total == D("250.00")
    assert totals.taxable_total == D("750.00")
    assert totals.grand_total == D("750.00")
