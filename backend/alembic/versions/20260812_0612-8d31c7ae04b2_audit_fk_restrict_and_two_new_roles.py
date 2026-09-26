"""audit fk restrict and two new roles

Revision ID: 8d31c7ae04b2
Revises: 4c8e2b71fa93
Create Date: 2026-08-12 06:12:00.000000+00:00

Two corrections carried forward from earlier phases.

---------------------------------------------------------------------------
1. `audit_logs.actor_user_id` becomes ON DELETE RESTRICT
---------------------------------------------------------------------------

It was `ON DELETE SET NULL`, which contradicted the append-only trigger added
in `5e46114656c0`. `SET NULL` is implemented as an **UPDATE** of the referencing
row, and that trigger refuses UPDATE on `audit_logs`. The declared behaviour was
therefore unreachable: deleting a staff account would not have anonymised the
trail, it would have failed with

    audit_logs is append-only; UPDATE is not permitted

at the one moment nobody wants a surprise — offboarding somebody who has left.

RESTRICT states the real rule instead of a decorative one: an account that has
done anything cannot be erased, because an audit trail whose actor can be
deleted is not an audit trail (CLAUDE.md §8, §12). Offboarding sets
`users.deleted_at`; `actor_email` is already snapshotted on every row, so the
history stays readable afterwards. This also matches the sibling `hospital_id`
foreign key, which was RESTRICT from the start.

No data migration is needed — the constraint is redefined, existing rows are
untouched, and nothing in the application ever hard-deleted a user.

---------------------------------------------------------------------------
2. `PATHOLOGIST` and `HOUSEKEEPING` join the seeded roles
---------------------------------------------------------------------------

CLAUDE.md §8 calls its role list "extensible — data-driven, not hardcoded in
logic", and both of these are added exactly the way a hospital would add its
own: rows in `roles`, rows in `role_permissions`. No authorisation check
changed to accommodate either.

* **PATHOLOGIST** — the lab's counterpart to `RADIOLOGIST`. `diagnostics`
  separates *entering* a result from *verifying* one, and until now the lab
  signature had nowhere clinically correct to sit, so it rested with `DOCTOR`.
  `DOCTOR` keeps it: a mid-size hospital with no resident pathologist still has
  to release results, and a permission model that makes that impossible is one
  the hospital routes around by sharing the administrator password.

* **HOUSEKEEPING** — owns `bed:clean`, the rung of the bed lifecycle that is
  somebody's actual job. Nursing keeps the permission too, because a ward at 3am
  with no housekeeping shift on should not be stuck with an unreleasable bed.
  What housekeeping gets is *only* beds: it holds nothing that touches a patient
  record, because a porter who can read a diagnosis is a data-protection finding
  waiting to happen (CLAUDE.md §12).

Both grants are `ON CONFLICT DO NOTHING`, so re-running is harmless, and the
downgrade removes the roles and their grants without touching the permissions
themselves — those belong to the module migrations that created them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d31c7ae04b2"
down_revision: str | None = "4c8e2b71fa93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FK = "fk_audit_logs_actor_user_id_users"

# (code, name, description) — mirrors `identity/rbac.py::SYSTEM_ROLES`.
NEW_ROLES: tuple[tuple[str, str, str], ...] = (
    ("PATHOLOGIST", "Pathologist", "Verifies and signs laboratory reports."),
    ("HOUSEKEEPING", "Housekeeping", "Turns discharged beds around; no access to patient data."),
)

# Mirrors each module's `MODULE_ROLE_PERMISSIONS`. A permission granted here and
# not held in code — or the reverse — is caught by the drift test in
# `tests/test_rbac_seed.py`, which compares the two sides directly.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PATHOLOGIST": (
        # patients — the identity on the report
        "patient:read",
        # scheduling — who ordered it, and from which department
        "department:read",
        "doctor:read",
        # clinical — the clinical question behind the specimen
        "encounter:read",
        "order:read",
        "order:fulfil",
        "note:read",
        "diagnosis:read",
        # diagnostics — reports and signs, does not touch the pre-analytical bench
        "catalogue:read",
        "specimen:read",
        "report:read",
        "result:enter",
        "result:verify",
        "result:amend",
        "report:cancel",
        "result:acknowledge_critical",
        # billing — quotes a price at the window, does not collect
        "service:read",
        "ratecard:read",
        # notifications — reads what went out
        "notification:read",
        # ipd — which ward the report goes to
        "ward:read",
        "admission:read",
    ),
    # Beds, and nothing else. No patient-facing permission appears here, and
    # that omission is the point rather than an oversight.
    "HOUSEKEEPING": (
        "ward:read",
        "bed:read",
        "bed:clean",
    ),
}


def upgrade() -> None:
    connection = op.get_bind()

    # --- 1. the audit foreign key -----------------------------------------
    op.drop_constraint(_FK, "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        _FK, "audit_logs", "users", ["actor_user_id"], ["id"], ondelete="RESTRICT"
    )

    # --- 2. the two roles --------------------------------------------------
    for code, name, description in NEW_ROLES:
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

    codes = [code for code, _, _ in NEW_ROLES]
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE role_id IN (
                 SELECT id FROM roles WHERE code = ANY(:codes) AND hospital_id IS NULL
             )
            """
        ),
        {"codes": codes},
    )
    # Users still holding one of these roles would block the delete, which is
    # the correct outcome: reassign them first rather than silently stripping
    # somebody's access as a side effect of a schema rollback.
    connection.execute(
        sa.text("DELETE FROM roles WHERE code = ANY(:codes) AND hospital_id IS NULL"),
        {"codes": codes},
    )

    op.drop_constraint(_FK, "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        _FK, "audit_logs", "users", ["actor_user_id"], ["id"], ondelete="SET NULL"
    )
