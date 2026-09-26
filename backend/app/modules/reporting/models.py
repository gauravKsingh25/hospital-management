"""`reporting` owns no tables. It owns **views**, and this file is their contract.

Every other module here defines tables. This one deliberately defines none: a
KPI is a question about facts that already exist, and a reporting module that
stores its own copy of them is a reporting module that can disagree with the
system it reports on. The first time the dashboard says ₹4,80,000 and the
invoice ledger says ₹4,79,200, nobody trusts either number again.

---------------------------------------------------------------------------
The boundary problem, and how this module resolves it
---------------------------------------------------------------------------

CLAUDE.md §2 says a module never reaches into another module's tables. CLAUDE.md
§5 says *"no raw SQL except in `reporting` read-only views where performance
demands it."* Those two pull in opposite directions, and reporting is where they
meet: revenue for a quarter cannot be computed by paging through
`billing.service.list_charges`, and pretending otherwise would produce a
dashboard that times out.

The resolution is that **§5's carve-out is taken, and the §2 boundary is
replaced by a different one that is just as explicit**: every cross-module read
goes through a *named view*, declared in this module's own migration and listed
below. Never an inline join written at a call site.

That buys three things a scattering of ad-hoc joins would not:

* **One place to fix.** When `billing` renames a column, exactly one view breaks,
  and it breaks loudly at migration time rather than quietly returning zero.
* **A greppable dependency list.** What reporting depends on is this file, not
  "whatever the seventeen queries in service.py happen to touch".
* **Extraction-readiness.** These view definitions are the natural API of a read
  replica or a warehouse later (CLAUDE.md §2 names reporting as an extraction
  candidate). Views move; embedded joins get rewritten.

---------------------------------------------------------------------------
Two properties of these views that are not optional
---------------------------------------------------------------------------

**`security_invoker = true`, on every one of them.** A Postgres view executes
with the *owner's* privileges by default, and the owner here is the migration
role — which on a managed provider carries `BYPASSRLS` (see the README's note on
why the application runs as `hms_app`). A view created without
`security_invoker` would therefore read the underlying tables with row-level
security switched off, and a single unqualified query would return every
hospital's patients. That is the exact leak §3 builds RLS to prevent, reopened
by a convenience default. `tests/test_reporting_isolation.py` asserts it holds.

**Local dates, not UTC dates.** Every view exposes a `local_date` computed as
`(ts AT TIME ZONE h.timezone)::date`. A hospital's day does not end at midnight
UTC — for `Asia/Kolkata` it ends at 18:30 UTC — so grouping raw timestamps by
UTC date silently files five and a half hours of every evening's work under
tomorrow. Footfall would be wrong every single day, by an amount that looks
plausible.

---------------------------------------------------------------------------
Why plain views rather than materialised ones
---------------------------------------------------------------------------

Materialised views would be faster and would need a refresh schedule, a
staleness policy, and a story for "the dashboard is an hour behind during a
board meeting". At mid-size-hospital volumes the base tables are indexed on the
columns these views filter by, and every query the service issues is bounded by
a date range. Revisit when a single tenant's `charges` table passes a few
million rows — at which point the honest move is a rollup table refreshed by the
existing ARQ worker, not a bigger query.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "REPORTING_VIEWS",
    "ViewNames",
]


class ViewNames:
    """The views this module owns. Defined in `20260812_..._reporting_views.py`."""

    # One row per visit: local date, department, doctor, and the two durations
    # that matter — how long the patient waited, and how long they were seen.
    ENCOUNTERS = "reporting_encounters"

    # One row per inpatient stay, with length of stay computed the way the ward
    # counts it (a same-day admission and discharge is one day, not zero).
    ADMISSIONS = "reporting_admissions"

    # One row per billable act. The grain is the charge, not the invoice, so
    # "revenue by category" does not need an invoice to exist yet.
    CHARGES = "reporting_charges"

    # One row per payment, carrying `net_amount` — see the view's own comment on
    # why a reversal is two rows and what that means for "collected today".
    PAYMENTS = "reporting_payments"

    # Current bed census. A snapshot by construction: occupancy is a question
    # about now, and a historical answer needs the assignment history instead.
    BEDS = "reporting_beds"

    # Today's queue, with waiting time per token.
    QUEUE = "reporting_queue"


REPORTING_VIEWS: Final[tuple[str, ...]] = (
    ViewNames.ENCOUNTERS,
    ViewNames.ADMISSIONS,
    ViewNames.CHARGES,
    ViewNames.PAYMENTS,
    ViewNames.BEDS,
    ViewNames.QUEUE,
)
