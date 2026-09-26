"""Seed a working demo tenant: one hospital, one of each front-line role.

The frontend cannot be opened at all without an account, and the end-to-end
tests need a tenant that looks like a small clinic on an ordinary morning.
Creating that by hand is a dozen API calls in the right order, every time
somebody sets the project up.

    python scripts/seed_demo.py

Idempotent: running it twice changes nothing and prints the same credentials,
so it is safe in a `docker compose up` hook or at the start of a test run.

    DEMO_PASSWORD=... python scripts/seed_demo.py --hospital-code DEMO

**Development and test data only.** The accounts have known passwords and
`must_change_password` unset so a browser can sign straight in. Never run this
against an environment that holds real patients — see CLAUDE.md §5 on the free
tier not being for live data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from decimal import Decimal

from app.core.database import dispose_engine, session_scope, system_context
from app.core.exceptions import DomainError
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.billing import service as billing_service
from app.modules.billing.schemas import RateCardCreate, ServiceItemCreate, ServicePriceUpsert
from app.modules.diagnostics import service as diagnostics_service
from app.modules.diagnostics.schemas import AnalyteCreate, CatalogueItemCreate
from app.modules.identity import service as identity_service
from app.modules.identity.rbac import Roles
from app.modules.identity.schemas import UserCreate
from app.modules.ipd import service as ipd_service
from app.modules.ipd.schemas import BedCreate, WardCreate
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.schemas import DoctorCreate
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.schemas import HospitalCreate

DEFAULT_PASSWORD = "demo-password-2026"  # noqa: S105 - a seed credential, not a secret

# Every role the built screens touch, plus the admin that manages them. Emails
# are deterministic so the tests can hard-code them.
STAFF = [
    ("admin@demo.hospital", "Asha Menon", Roles.HOSPITAL_ADMIN),
    ("reception@demo.hospital", "Priya Nair", Roles.RECEPTIONIST),
    ("doctor@demo.hospital", "Dr Vikram Rao", Roles.DOCTOR),
    ("nurse@demo.hospital", "Sister Lakshmi", Roles.NURSE),
    ("lab@demo.hospital", "Ravi Kulkarni", Roles.LAB_TECH),
    ("cashier@demo.hospital", "Meera Joshi", Roles.CASHIER),
    ("admission@demo.hospital", "Ritu Sharma", Roles.ADMISSION_DESK),
]

# What a clinic must have configured before it can charge for anything. Without
# a default rate card every charge lands at zero and flagged `needs_pricing`,
# which is correct behaviour (billing never blocks clinical work) and makes for
# a demo where nobody ever owes anything.
PRICES = [
    ("OPD-CONSULT", "OPD consultation", "CONSULTATION", Decimal("500.00")),
    ("CBC", "Complete Blood Count", "LAB", Decimal("350.00")),
]


async def seed(*, code: str, name: str, password: str) -> int:
    async with session_scope() as session, system_context(session):
        hospital = await tenancy_service.get_hospital_by_code(session, code)
        if hospital is None:
            hospital = await tenancy_service.create_hospital(
                session,
                HospitalCreate(code=code, name=name, timezone="Asia/Kolkata"),
            )
            print(f"Created hospital {code} ({hospital.id}).")
        else:
            print(f"Hospital {code} already exists ({hospital.id}).")
        hospital_id: uuid.UUID = hospital.id

    created: list[tuple[str, str]] = []
    doctor_user_id: uuid.UUID | None = None
    doctor_name = ""

    for email, full_name, role in STAFF:
        async with session_scope() as session, system_context(session):
            user = await identity_service.get_user_by_email(session, email)
            if user is None:
                user = await identity_service.create_user(
                    session,
                    UserCreate(
                        email=email,
                        full_name=full_name,
                        password=password,
                        role_codes=[role],
                        hospital_id=hospital_id,
                    ),
                    hospital_id=hospital_id,
                    # Unset, unlike `create_admin.py`. A forced password change
                    # is right for a real first login and wrong for a browser
                    # test, which would spend every run on that screen.
                    must_change_password=False,
                )
                created.append((email, role))
            if role == Roles.DOCTOR:
                doctor_user_id = user.id
                doctor_name = full_name

    # The doctor profile is separate from the user account: `scheduling` owns
    # who holds a clinic, `identity` owns who can sign in. A doctor without
    # one cannot be picked at the counter and has no queue of their own.
    if doctor_user_id is not None:
        async with session_scope() as session, system_context(session):
            existing = await scheduling_service.get_doctor_by_user(
                session, doctor_user_id, hospital_id=hospital_id
            )
            if existing is None:
                doctor = await scheduling_service.create_doctor(
                    session,
                    DoctorCreate(
                        user_id=doctor_user_id,
                        specialty="General Medicine",
                        qualification="MBBS, MD",
                        default_slot_minutes=10,
                    ),
                    hospital_id=hospital_id,
                    # Passed in rather than joined: `scheduling` does not read
                    # the users table (CLAUDE.md §2).
                    display_name=doctor_name,
                )
                print(f"Created doctor profile {doctor.display_name}.")

    await _seed_price_list(hospital_id)
    await _seed_catalogue(hospital_id)
    await _seed_ward(hospital_id)

    print()
    print(f"Hospital: {name} ({code})")
    print(f"Password for every account below: {password}")
    for email, _, role in STAFF:
        marker = "new" if any(email == made for made, _ in created) else "existing"
        print(f"  {role:<16} {email:<26} ({marker})")

    return 0


async def _seed_price_list(hospital_id: uuid.UUID) -> None:
    """A default cash rate card, with the two services the demo journey uses.

    Both halves matter. Without the *card* nothing resolves a price at all;
    without a card marked `is_default` a walk-in cash patient resolves nothing
    either, because that flag is what a visit with no payer falls back to.
    """
    async with session_scope() as session, system_context(session):
        card = await billing_service.get_rate_card_by_code(session, "CASH", hospital_id=hospital_id)
        if card is None:
            card = await billing_service.create_rate_card(
                session,
                RateCardCreate(
                    code="CASH",
                    name="Cash counter",
                    payer_type="CASH",
                    is_default=True,
                ),
                hospital_id=hospital_id,
            )
            print("Created the CASH rate card.")

        for code, item_name, category, price in PRICES:
            item = await billing_service.get_service_item_by_code(
                session, code, hospital_id=hospital_id
            )
            if item is None:
                item = await billing_service.create_service_item(
                    session,
                    ServiceItemCreate(code=code, name=item_name, category=category),
                    hospital_id=hospital_id,
                )
            await billing_service.set_price(
                session,
                item,
                ServicePriceUpsert(rate_card_id=card.id, price=price),
            )
            print(f"Priced {code} at {price}.")


async def _seed_catalogue(hospital_id: uuid.UUID) -> None:
    """One orderable blood test, with a reference band per sex.

    Two bands rather than one on purpose: it is what makes the demo show the
    thing that actually matters about a result screen — that 12.5 g/dL is
    normal for a woman and low for a man, and the software knows which patient
    it is looking at.
    """
    async with session_scope() as session, system_context(session):
        item = await diagnostics_service.get_catalogue_item_by_code(
            session, "CBC", hospital_id=hospital_id
        )
        if item is not None:
            return

        item = await diagnostics_service.create_catalogue_item(
            session,
            CatalogueItemCreate(
                code="CBC",
                name="Complete Blood Count",
                discipline="LAB",
                section="Haematology",
                specimen_type="BLOOD",
                container="EDTA (purple top)",
                turnaround_minutes=60,
            ),
            hospital_id=hospital_id,
        )
        await diagnostics_service.add_analyte(
            session,
            item,
            AnalyteCreate(
                code="HB",
                name="Haemoglobin",
                unit="g/dL",
                decimal_places=1,
                ranges=[
                    {"low": "13.0", "high": "17.0", "sex": "MALE", "critical_low": "7.0"},
                    {"low": "12.0", "high": "15.0", "sex": "FEMALE", "critical_low": "7.0"},
                    {"low": "12.0", "high": "17.0"},
                ],
            ),
        )
        print("Created the CBC catalogue entry.")


async def _seed_ward(hospital_id: uuid.UUID) -> None:
    """One general ward with twelve beds.

    The point is that a patient can be admitted at all — without a bed the
    whole inpatient half of the system is unreachable while every screen still
    looks like it works.

    Twelve rather than four because a ward that can be filled is a ward that
    *gets* filled: the end-to-end suite admits a patient per test and only some
    of them discharge, so a four-bed ward runs out mid-run and the failures
    look like bugs in the board.
    """
    async with session_scope() as session, system_context(session):
        # Keyed on the code, like the rate card and the catalogue — not on
        # "are there any wards at all". A tenant that has picked up a ward from
        # somewhere else still deserves the demo one.
        wards, _ = await ipd_service.list_wards(
            session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id
        )
        ward = next((row for row in wards if row.code == "GW1"), None)
        if ward is None:
            ward = await ipd_service.create_ward(
                session,
                WardCreate(code="GW1", name="General Ward", floor="1", bed_class="GENERAL"),
                hospital_id=hospital_id,
            )
            print("Created General Ward.")

        # Beds are checked separately from the ward. A ward with no beds is a
        # ward nobody can be admitted to, and it is a state that is easy to
        # reach — the reset script clears admissions, and somebody clearing up
        # test data by hand takes the beds with it.
        beds, _ = await ipd_service.list_beds(
            session, PageParams(limit=MAX_PAGE_SIZE), hospital_id=hospital_id, ward_id=ward.id
        )
        existing = {bed.code for bed in beds}
        made = 0
        for number in range(1, 13):
            # Prefixed with the ward code because a bed code is unique per
            # *hospital*, not per ward — bare numbers make the second ward
            # impossible to set up.
            code = f"GW1-{number:02d}"
            if code in existing:
                continue
            await ipd_service.create_bed(
                session, BedCreate(ward_id=ward.id, code=code), hospital_id=hospital_id
            )
            made += 1
        if made:
            print(f"Created {made} bed(s) in General Ward.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hospital-code", default="DEMO")
    parser.add_argument("--hospital-name", default="Demo Hospital")
    args = parser.parse_args()

    password = os.environ.get("DEMO_PASSWORD", DEFAULT_PASSWORD)

    async def run() -> int:
        try:
            return await seed(code=args.hospital_code, name=args.hospital_name, password=password)
        except DomainError as exc:
            print(f"{exc.code}: {exc.message}")
            return 1
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
