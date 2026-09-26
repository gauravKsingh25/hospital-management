"""Create the first PLATFORM_ADMIN account.

The system has no way to bootstrap itself through the API: creating a user
requires `user:create`, which requires a role, which requires a user. This
script breaks that circle exactly once.

    python scripts/create_admin.py --email you@example.com --name "Your Name"

The password is read from the ADMIN_PASSWORD environment variable, or prompted
for if absent — never passed as an argument, where it would land in shell
history and process listings. The account is created with
`must_change_password` set, so the first login forces a change.

To create a HOSPITAL_ADMIN for a specific tenant instead, pass
`--hospital-code KMC --role HOSPITAL_ADMIN`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.core.database import dispose_engine, session_scope, system_context
from app.core.exceptions import DomainError
from app.core.security import validate_password_strength
from app.modules.identity import service as identity_service
from app.modules.identity.rbac import Roles
from app.modules.identity.schemas import UserCreate
from app.modules.tenancy import service as tenancy_service


async def create_admin(
    *, email: str, full_name: str, password: str, role_code: str, hospital_code: str | None
) -> int:
    hospital_id = None
    if hospital_code:
        async with session_scope() as session, system_context(session):
            hospital = await tenancy_service.get_hospital_by_code(session, hospital_code)
            if hospital is None:
                print(f"No hospital with code {hospital_code!r}.")
                return 1
            hospital_id = hospital.id

    if role_code == Roles.PLATFORM_ADMIN and hospital_id is not None:
        print("PLATFORM_ADMIN is cross-tenant and cannot be attached to a hospital.")
        return 1

    async with session_scope() as session:
        existing = await identity_service.get_user_by_email(session, email)
        if existing is not None:
            print(f"An account already exists for {email}.")
            return 1

        user = await identity_service.create_user(
            session,
            UserCreate(
                email=email,
                full_name=full_name,
                password=password,
                role_codes=[role_code],
                hospital_id=hospital_id,
            ),
            hospital_id=hospital_id,
            must_change_password=True,
        )

    print(f"Created {role_code} {user.email} ({user.id}).")
    print("The first login will require a password change.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an administrator account.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, dest="full_name")
    parser.add_argument("--role", default=Roles.PLATFORM_ADMIN)
    parser.add_argument(
        "--hospital-code",
        default=None,
        help="Attach the account to this tenant (required for HOSPITAL_ADMIN).",
    )
    args = parser.parse_args()

    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    try:
        validate_password_strength(password)
    except DomainError as exc:
        print(exc.message)
        return 1

    async def run() -> int:
        try:
            return await create_admin(
                email=args.email,
                full_name=args.full_name,
                password=password,
                role_code=args.role,
                hospital_code=args.hospital_code,
            )
        except DomainError as exc:
            print(f"{exc.code}: {exc.message}")
            return 1
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
