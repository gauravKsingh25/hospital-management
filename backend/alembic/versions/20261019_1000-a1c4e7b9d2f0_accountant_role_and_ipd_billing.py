"""accountant role and ipd billing permissions

Revision ID: a1c4e7b9d2f0
Revises: 3b9e4c1d7a52
Create Date: 2026-10-19 10:00:00.000000+00:00

The IPD bill is the one that goes to the hospital's tax filing, so it gets an
owner. A new system role, ACCOUNTANT, and three billing permissions:

- `ipd_bill:edit`     — add, edit, discount or remove a charge on an IPD stay;
- `ipd_bill:finalise` — generate (or cancel to correct) an IPD final bill;
- `accounts:read`     — the accounts dashboard, bill history and print.

The accountant reads what billing staff read and holds the three new codes; it
takes no money (no `payment:record`). HOSPITAL_ADMIN and PLATFORM_ADMIN get the
three new codes too. Mirrors `billing/rbac.py` and the read grants in the
patients, ipd, scheduling, clinical and reporting `rbac.py` files —
`tests/test_rbac_seed.py` fails if the two disagree.

Data only: no schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b9d2f0"
down_revision: str | None = "3b9e4c1d7a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = ("ACCOUNTANT", "Accountant", "Reviews, corrects and finalises IPD bills; sees all billing.")

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("ipd_bill:edit", "billing", "Add, edit or remove charges on an IPD bill."),
    ("ipd_bill:finalise", "billing", "Generate or cancel an IPD final bill."),
    ("accounts:read", "billing", "View the accounts dashboard and bill history."),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "ACCOUNTANT": (
        # identity baseline
        "hospital:read",
        # billing
        "ratecard:read",
        "service:read",
        "charge:read",
        "invoice:read",
        "payment:read",
        "claim:read",
        *_NEW_CODES,
        # the screens the accountant works from
        "patient:read",
        "patient:read_identifiers",
        "ward:read",
        "bed:read",
        "admission:read",
        "medicine:read",
        "department:read",
        "doctor:read",
        "appointment:read",
        "queue:read",
        "encounter:read",
        "order:read",
        "report:operational",
        "report:revenue",
    ),
    "HOSPITAL_ADMIN": _NEW_CODES,
    "PLATFORM_ADMIN": _NEW_CODES,
}


def upgrade() -> None:
    connection = op.get_bind()

    for code, module, description in NEW_PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, module, description, created_at, updated_at)
                VALUES (gen_random_uuid(), :code, :module, :description, now(), now())
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "module": module, "description": description},
        )

    code, name, description = _ROLE
    connection.execute(
        sa.text(
            """
            INSERT INTO roles
                (id, hospital_id, code, name, description, is_system, created_at, updated_at)
            VALUES (gen_random_uuid(), NULL, :code, :name, :description, true, now(), now())
            ON CONFLICT DO NOTHING
            """
        ),
        {"code": code, "name": name, "description": description},
    )

    for role_code, permission_codes in ROLE_PERMISSIONS.items():
        for permission_code in permission_codes:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id, created_at)
                    SELECT r.id, p.id, now()
                      FROM roles r, permissions p
                     WHERE r.code = :role_code
                       AND r.hospital_id IS NULL
                       AND p.code = :permission_code
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"role_code": role_code, "permission_code": permission_code},
            )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE role_id IN (SELECT id FROM roles WHERE code = :role AND hospital_id IS NULL)
                OR permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"role": _ROLE[0], "codes": list(_NEW_CODES)},
    )
    # A user still holding the role blocks this, which is right: reassign them
    # first rather than strip access as a side effect of a rollback.
    connection.execute(
        sa.text("DELETE FROM roles WHERE code = :role AND hospital_id IS NULL"),
        {"role": _ROLE[0]},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_NEW_CODES)}
    )
