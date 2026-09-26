"""Create the least-privilege application database role.

**Why this exists.** Neon's `neondb_owner` (like the default superuser-ish role
on most managed Postgres) carries the `BYPASSRLS` attribute. That attribute
overrides even `FORCE ROW LEVEL SECURITY`, so an application connecting as the
owner silently ignores every tenant-isolation policy — the policies look right
in `pg_policies` and protect nothing. The role cannot revoke the attribute from
itself.

So the application connects as a separate role that has no such attribute:

    DIRECT_URL    -> owner        : runs migrations, owns the tables
    DATABASE_URL  -> app role     : runs the application, subject to RLS

Run this once per environment (and again if you rotate the password):

    python scripts/bootstrap_db_role.py

It is idempotent. `ALTER DEFAULT PRIVILEGES` means tables created by future
migrations are automatically usable by the app role, so this does not need
re-running as modules are added.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.core.config import settings
from app.core.database import create_engine


async def bootstrap() -> int:
    role = settings.APP_DB_ROLE
    password = settings.APP_DB_PASSWORD

    if not password:
        print("APP_DB_PASSWORD is not set. Add it to .env before running this.")
        return 1

    engine = create_engine(direct=True)
    try:
        async with engine.connect() as conn:
            owner = (await conn.execute(text("SELECT current_user"))).scalar_one()
            print(f"connected as {owner} (owner) on {engine.url.host}")

            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
                )
            ).scalar_one_or_none()

            # Identifiers cannot be parameterised; the role name comes from our
            # own settings and is validated below rather than from user input.
            if not role.replace("_", "").isalnum():
                print(f"refusing unsafe role name: {role!r}")
                return 1

            # CREATE/ALTER ROLE are utility statements: Postgres does not accept
            # bind parameters in them, so the password has to be inlined. Rather
            # than hand-roll escaping, the charset is restricted to what the
            # generator produces — anything else is rejected outright.
            if not password.isalnum() or len(password) < 24:
                print(
                    "APP_DB_PASSWORD must be at least 24 alphanumeric characters "
                    '(generate with: python -c "import secrets,string; '
                    "print(''.join(secrets.choice(string.ascii_letters+string.digits) "
                    'for _ in range(32)))")'
                )
                return 1
            literal = f"'{password}'"

            if exists:
                await conn.execute(text(f"ALTER ROLE {role} WITH LOGIN PASSWORD {literal}"))
                print(f"role {role} already existed — password updated")
            else:
                await conn.execute(text(f"CREATE ROLE {role} WITH LOGIN PASSWORD {literal}"))
                print(f"created role {role}")

            # A freshly created role is NOBYPASSRLS/NOSUPERUSER by default, and
            # only a superuser may change those attributes — which the managed
            # owner is not. So rather than assert them here, the check at the
            # end of this script verifies the result and fails loudly if the
            # provider ever hands out something different.
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
                )
            )
            await conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
            )
            # Tables created by future migrations are covered automatically.
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
                )
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
                )
            )
            # DDL stays with the owner: the application must never be able to
            # drop a table it merely reads.
            await conn.commit()

            bypass = (
                await conn.execute(
                    text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :role"),
                    {"role": role},
                )
            ).scalar_one()
            print(f"{role}: bypassrls={bypass} (must be False)")
            if bypass:
                print("role can still bypass RLS — isolation would not be enforced")
                return 1

        print("\nPoint DATABASE_URL at this role, keeping DIRECT_URL on the owner:")
        host = settings.async_database_url.host
        database = settings.async_database_url.database
        print(f'  DATABASE_URL="postgresql://{role}:<password>@{host}/{database}?sslmode=require"')
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(bootstrap()))
