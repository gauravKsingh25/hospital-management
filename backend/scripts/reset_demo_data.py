"""Clear the operational data from a development tenant, keeping its setup.

    python scripts/reset_demo_data.py

**Development and test databases only.** It deletes patients, visits, orders,
reports, invoices and payments. There is a guard below and you should not
remove it.

## Why this exists

The end-to-end suite runs against a persistent database and every spec creates
a patient, opens a visit, and — for the specs that stop halfway on purpose —
leaves it open. Nothing ever cleans up, so after a few dozen runs the cash
counter's board and the diagnostics bench are hundreds of rows deep in
abandoned test visits.

That is not a cosmetic problem for the suite. Both boards are ordered
oldest-first (which is correct: the person waiting longest goes first), so a
visit created two seconds ago sits on the last page, and specs start failing
for reasons that have nothing to do with the code. Worse, it hides real
regressions behind slow, flaky assertions.

## What it keeps

Staff accounts, roles, departments, doctors, wards, beds, rate cards, service
prices and the test catalogue — everything `seed_demo.py` sets up and everything
an administrator would configure. Only the day's work is removed, so a reset is
followed by a working hospital with nobody in it.

**And the audit log**, which is not an oversight. `audit_logs` is append-only,
enforced by a database trigger that refuses UPDATE, DELETE *and* TRUNCATE
(CLAUDE.md §12). This script does not try to work around that and must not be
changed to: a trail that a maintenance script can erase is not a trail. The
consequence is that the log keeps growing across resets, which is correct — and
which is also how you can tell afterwards that a reset happened.

## Which connection this uses, and why it is not the application's

`DIRECT_URL`, not `DATABASE_URL` — the same split CLAUDE.md §5 draws for
Alembic, for the same reason. The application role (`hms_app`) deliberately
holds no TRUNCATE privilege and cannot bypass row-level security; both are
properties worth having, and neither should be relaxed to let a maintenance
script run. Truncating thirty-three tables in one statement is schema-level
work, so it goes through the schema-level connection.

Deleting them one at a time as the application role is not the alternative it
looks like: `insurance_claims` references `invoices` with `ON DELETE RESTRICT`,
so a row-by-row teardown has to be ordered against every foreign key in the
system and re-ordered whenever one is added. `TRUNCATE ... CASCADE` over the
whole set is one atomic statement with no ordering to get wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

# Ordered child-first so the deletes work even where a foreign key has no
# cascade. Sequences are included: leaving them alone would keep issuing UHIDs
# from wherever the last run stopped, which is harmless but makes a reset look
# like it did not happen.
OPERATIONAL_TABLES = (
    "notification_attempts",
    "notifications",
    "notification_suppressions",
    "medication_administrations",
    "medication_orders",
    "discharge_summaries",
    "bed_assignments",
    "admissions",
    "insurance_claims",
    "payments",
    "invoices",
    "charges",
    "result_values",
    "diagnostic_reports",
    "specimens",
    "vitals",
    "diagnoses",
    "clinical_notes",
    "orders",
    "encounter_events",
    "encounters",
    "queue_entries",
    "appointments",
    "patient_consents",
    "patient_identifiers",
    "patient_alerts",
    # Numbering counters, so UHIDs and invoice numbers restart.
    "uhid_sequences",
    "encounter_sequences",
    "appointment_sequences",
    "token_sequences",
    "accession_sequences",
    "invoice_sequences",
    "admission_sequences",
)


async def reset() -> int:
    # The maintenance connection, not the application's — see the module
    # docstring. It is also the URL the guard below has to check, because it is
    # the one this script actually points a TRUNCATE at.
    url = settings.DIRECT_URL

    # The guard. A production database is not local, and this script has no
    # business anywhere near one.
    if not any(host in url for host in ("localhost", "127.0.0.1")) and not os.environ.get(
        "ALLOW_REMOTE_RESET"
    ):
        print(
            "Refusing to reset a non-local database.\n"
            f"  DIRECT_URL points at: {url.split('@')[-1].split('/')[0]}\n"
            "Set ALLOW_REMOTE_RESET=1 only if you are certain."
        )
        return 1

    engine = create_async_engine(settings.async_direct_url)
    try:
        async with engine.begin() as connection:
            beds_before = (await connection.execute(text("SELECT count(*) FROM beds"))).scalar_one()

            # One statement, so the foreign keys between these tables never see
            # a half-deleted graph. RESTART IDENTITY is harmless here (every key
            # is a UUID) and CASCADE catches anything the ordering above missed.
            await connection.execute(
                text(f"TRUNCATE TABLE {', '.join(OPERATIONAL_TABLES)} RESTART IDENTITY CASCADE")
            )

            # Beds are configuration and survive — but their *status* is the
            # day's work, and `reserved_for_patient_id` points at a patient who
            # is about to stop existing. Cleared before the patients go, because
            # the foreign key demands it and because a ward left OCCUPIED by
            # patients who no longer exist reads as a full hospital with nobody
            # in it.
            await connection.execute(
                text(
                    "UPDATE beds SET status = 'AVAILABLE', "
                    "reserved_for_patient_id = NULL, out_of_service_reason = NULL"
                )
            )

            # `patients` is deleted rather than truncated, and that is the whole
            # reason this is three statements instead of one. `beds` has a
            # foreign key to `patients`, so `TRUNCATE patients CASCADE` takes
            # every bed in the hospital with it — silently, while the script
            # prints that configuration was untouched. DELETE respects the same
            # constraints without following them destructively.
            await connection.execute(text("DELETE FROM patients"))

            beds_after = (await connection.execute(text("SELECT count(*) FROM beds"))).scalar_one()
            if beds_after != beds_before:
                # Belt and braces against exactly the bug this script once had.
                # `tests/test_reset_scope.py` catches it from the metadata; this
                # catches it from the database, and rolls back rather than
                # leaving a hospital with no beds.
                raise RuntimeError(
                    f"reset destroyed configuration: beds went from {beds_before} to {beds_after}"
                )
    finally:
        await engine.dispose()

    print(
        f"Cleared {len(OPERATIONAL_TABLES)} operational tables, every patient, and freed every bed."
    )
    print("Staff, roles, departments, doctors, wards, prices and the catalogue are untouched.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for scripts and CI).",
    )
    args = parser.parse_args()

    if not args.yes and sys.stdin.isatty():
        answer = input("Delete all patients and visits from this database? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    # The application engine is never opened here, so there is nothing of its
    # to dispose — `reset` owns and disposes the maintenance engine itself.
    return asyncio.run(reset())


if __name__ == "__main__":
    sys.exit(main())
