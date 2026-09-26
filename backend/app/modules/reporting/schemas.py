"""Response DTOs for `reporting`.

All read-only, so there are no request bodies here — only a shared date window
and the shapes the dashboards render.

One convention worth stating: every count that could be zero **is** returned as
zero rather than omitted, and every average that has no denominator comes back
as `None` rather than `0.0`. A chart that silently drops empty days draws a
misleading line, and an average wait of "0 minutes" when nobody was seen is a
number somebody will quote in a meeting.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "CategoryRevenue",
    "DailyCount",
    "DateWindow",
    "DepartmentLoad",
    "DoctorLoad",
    "FollowUpCompliance",
    "FootfallReport",
    "InpatientReport",
    "OccupancyReport",
    "QueueSnapshot",
    "RevenueReport",
    "WaitingDoctor",
    "WardOccupancy",
]

MAX_WINDOW_DAYS = 731  # two years, plus a leap day


class DateWindow(BaseModel):
    """An inclusive local-date range.

    Bounded on purpose. An unbounded report is a full table scan somebody
    triggers by leaving a filter blank, and CLAUDE.md §11's "never return
    unbounded lists" applies to aggregates too — the cost is in the scan, not
    the size of the response.
    """

    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.date_to < self.date_from:
            raise ValueError("The end of the range falls before its start.")
        if (self.date_to - self.date_from).days > MAX_WINDOW_DAYS:
            raise ValueError(
                f"A reporting window may span at most {MAX_WINDOW_DAYS} days; "
                "narrow the range or ask for a monthly rollup."
            )
        return self


# ---------------------------------------------------------------------------
# Footfall
# ---------------------------------------------------------------------------
class DailyCount(BaseModel):
    """One point on a trend line. Zero-filled — see the module docstring."""

    day: date
    total: int = 0
    seen: int = 0
    lost: int = 0


class DepartmentLoad(BaseModel):
    department_id: uuid.UUID | None = None
    department_name: str | None = None
    total: int = 0
    seen: int = 0
    average_wait_minutes: float | None = None


class DoctorLoad(BaseModel):
    doctor_id: uuid.UUID | None = None
    doctor_name: str | None = None
    total: int = 0
    seen: int = 0
    average_consultation_minutes: float | None = None
    average_wait_minutes: float | None = None


class FootfallReport(BaseModel):
    """CLAUDE.md §13 step 10: footfall."""

    window: DateWindow
    total_visits: int = 0
    patients_seen: int = 0
    cancelled_or_no_show: int = 0
    new_patients: int = 0
    # Visits the auto-close sweep closed rather than a person. Not a KPI so much
    # as a process smell: a rising number means staff are not completing visits,
    # and every downstream metric built on closure times degrades with it.
    auto_closed: int = 0
    by_day: list[DailyCount] = Field(default_factory=list)
    by_department: list[DepartmentLoad] = Field(default_factory=list)
    by_doctor: list[DoctorLoad] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Occupancy and inpatient
# ---------------------------------------------------------------------------
class WardOccupancy(BaseModel):
    ward_id: uuid.UUID
    ward_code: str
    ward_name: str
    total_beds: int = 0
    usable_beds: int = 0
    occupied: int = 0
    available: int = 0
    cleaning: int = 0
    out_of_service: int = 0
    occupancy_rate: float = 0.0


class OccupancyReport(BaseModel):
    """CLAUDE.md §13 step 10: occupancy. A snapshot of now, not a range."""

    total_beds: int = 0
    usable_beds: int = 0
    occupied: int = 0
    available: int = 0
    cleaning: int = 0
    out_of_service: int = 0
    occupancy_rate: float = 0.0
    by_ward: list[WardOccupancy] = Field(default_factory=list)


class InpatientReport(BaseModel):
    """CLAUDE.md §13 step 10: ALOS, plus how stays ended."""

    window: DateWindow
    admissions: int = 0
    discharges: int = 0
    currently_admitted: int = 0
    # NULL when nobody was discharged in the window — an ALOS with no
    # denominator is not zero, it is unknown.
    average_length_of_stay_days: float | None = None
    by_discharge_type: dict[str, int] = Field(default_factory=dict)
    deaths: int = 0
    # Deaths per hundred discharges. Reported alongside the count because the
    # raw number rises with volume and tells a hospital nothing on its own.
    mortality_rate: float | None = None


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------
class CategoryRevenue(BaseModel):
    category: str
    charges: int = 0
    billable: Decimal = Decimal("0.00")
    waived: Decimal = Decimal("0.00")


class RevenueReport(BaseModel):
    """CLAUDE.md §13 step 10: revenue.

    Three different numbers that are easy to confuse and expensive to confuse:
    what was *earned* (charges raised), what was *collected* (cash in), and what
    is *outstanding* (issued and unpaid). They are reported separately and never
    summed.
    """

    window: DateWindow
    charges_raised: Decimal = Decimal("0.00")
    tax_collected: Decimal = Decimal("0.00")
    discounts_given: Decimal = Decimal("0.00")
    waived: Decimal = Decimal("0.00")
    collected: Decimal = Decimal("0.00")
    reversed_amount: Decimal = Decimal("0.00")
    # Not windowed: an outstanding balance is a fact about now, and restricting
    # it to a date range would quietly hide the oldest and most worrying debt.
    outstanding_total: Decimal = Decimal("0.00")
    # Acts captured with no price attached — the exception worklist from
    # `billing.capture_charge`. Every one is revenue the hospital has not
    # decided how to bill for yet, so it belongs on a revenue report.
    unpriced_charges: int = 0
    by_category: list[CategoryRevenue] = Field(default_factory=list)
    by_method: dict[str, Decimal] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Follow-up
# ---------------------------------------------------------------------------
class FollowUpCompliance(BaseModel):
    """CLAUDE.md §13 step 10: follow-up compliance.

    "Advised" counts visits where a doctor set a follow-up date. "Honoured"
    counts those where the patient actually came back on or after it.

    Pending visits — the date has not arrived yet — are excluded from the rate
    rather than counted as failures. Including them would make the number depend
    on when the report is run, which is how a compliance metric becomes
    something nobody trusts.
    """

    window: DateWindow
    advised: int = 0
    honoured: int = 0
    missed: int = 0
    pending: int = 0
    compliance_rate: float | None = None


# ---------------------------------------------------------------------------
# Live queue (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
class WaitingDoctor(BaseModel):
    doctor_id: uuid.UUID | None = None
    doctor_name: str | None = None
    department_id: uuid.UUID | None = None
    waiting: int = 0
    longest_wait_minutes: float | None = None
    average_wait_minutes: float | None = None
    # True when this clinic's longest wait has passed the alert threshold —
    # §7b's "doctor delay alerts to reception when a clinic runs behind".
    running_late: bool = False


class QueueSnapshot(BaseModel):
    """Live queue analytics. A snapshot of the current local day."""

    queue_date: date
    waiting: int = 0
    in_consultation: int = 0
    completed: int = 0
    left_without_being_seen: int = 0
    average_wait_minutes: float | None = None
    longest_wait_minutes: float | None = None
    delay_threshold_minutes: int = 0
    by_doctor: list[WaitingDoctor] = Field(default_factory=list)
    running_late: list[WaitingDoctor] = Field(default_factory=list)


class Dashboard(BaseModel):
    """Whatever the caller's permissions allow, in one round trip.

    Assembled server-side for the same reason the consultation chart is: a
    dashboard that opens in one request rather than five is the difference
    between a screen staff leave open and one they close.

    Sections the caller may not see are `None`, not omitted — so the frontend
    renders a stable layout instead of reflowing based on role.
    """

    generated_for: date
    footfall: FootfallReport | None = None
    occupancy: OccupancyReport | None = None
    inpatient: InpatientReport | None = None
    revenue: RevenueReport | None = None
    follow_up: FollowUpCompliance | None = None
    queue: QueueSnapshot | None = None


TrendDays = Annotated[int, Field(ge=1, le=90)]
