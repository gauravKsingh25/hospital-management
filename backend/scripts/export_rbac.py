"""Generate the frontend's permission and role unions.

Permissions are data, not schema (CLAUDE.md §8), so they do not appear in the
OpenAPI document and `openapi-typescript` cannot see them. That leaves the
frontend either hand-copying 111 strings or checking nothing — and a
hand-copied `"encouter:complete"` is not a compile error, it is a button that
never appears for anyone and a bug report six weeks later that reception
"can't close visits any more".

Emitting a union type turns every one of those typos into a build failure.

    python scripts/export_rbac.py --output ../frontend/src/types/rbac.generated.ts

Like `export_openapi.py`, this touches no database and starts no server: the
declarations are read straight from each module's `rbac.py`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "rbac-export-placeholder-not-a-secret")

from app.core.rbac_registry import (
    iter_rbac_modules,
    permission_codes,
    role_grants,
)
from app.modules.identity.rbac import SYSTEM_ROLES

HEADER = """/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Written by `backend/scripts/export_rbac.py` from each module's `rbac.py`.
 * Regenerate with `npm run codegen`; `npm run codegen:check` fails when this
 * file is stale.
 *
 * Permissions do not appear in the OpenAPI schema because they are rows, not
 * types (CLAUDE.md §8) — a hospital administrator can grant one without a
 * deploy. Generating the union anyway means the *names* are checked at build
 * time even though the *grants* are checked at runtime by FastAPI.
 *
 * Nothing here is an authorisation decision. These strings only decide which
 * controls a screen renders; every actual check happens server-side on every
 * request.
 */
"""


def _string_union(name: str, values: list[str], doc: str) -> str:
    """A `const` array plus the union type derived from it.

    An array rather than a bare union so the values exist at runtime too — an
    access-matrix screen needs to iterate them, and a TypeScript union alone
    vanishes at compile time.
    """
    entries = "".join(f'  "{value}",\n' for value in values)
    return (
        f"{doc}\nexport const {name} = [\n{entries}] as const;\n\n"
        f"export type {name[:-1].title().replace('_', '')} = (typeof {name})[number];\n\n"
    )


def render(*, permissions: list[str], roles: list[str], modules: list[str]) -> str:
    grants = {role: sorted(codes) for role, codes in sorted(role_grants().items())}
    grant_entries = "".join(
        f'  "{role}": [\n' + "".join(f'    "{code}",\n' for code in codes) + "  ],\n"
        for role, codes in grants.items()
    )

    return (
        HEADER
        + _string_union(
            "PERMISSIONS",
            permissions,
            f"\n/** Every permission code, declared across {len(modules)} modules:\n"
            f" * {', '.join(modules)}.\n */",
        )
        + _string_union(
            "ROLES",
            roles,
            "/** Seed roles from CLAUDE.md §8, plus those added since. */",
        )
        + "/**\n"
        " * Which roles hold which permission, as declared in the backend.\n"
        " *\n"
        " * Useful for rendering an access matrix. It is NOT what the server\n"
        " * enforces at request time — a hospital may have edited a role since,\n"
        " * and `/auth/me` is the only authority on what this user actually\n"
        " * holds. Never branch on this to decide whether an action is allowed.\n"
        " */\n"
        "export const DECLARED_ROLE_PERMISSIONS: Record<string, readonly Permission[]> = {\n"
        + grant_entries
        + "};\n"
    )


def export(output: Path) -> int:
    permissions = sorted(permission_codes())
    roles = sorted({role.code for role in SYSTEM_ROLES})
    modules = [module.__name__.rsplit(".", 2)[-2] for module in iter_rbac_modules()]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render(permissions=permissions, roles=roles, modules=modules), encoding="utf-8"
    )

    print(f"Wrote {output} — {len(permissions)} permissions, {len(roles)} roles.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "types"
        / "rbac.generated.ts",
        help="Where to write the TypeScript module.",
    )
    args = parser.parse_args()
    return export(args.output)


if __name__ == "__main__":
    sys.exit(main())
