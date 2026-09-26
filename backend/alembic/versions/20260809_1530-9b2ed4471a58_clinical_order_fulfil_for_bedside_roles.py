"""clinical: order:fulfil for the roles that actually clear those orders

Revision ID: 9b2ed4471a58
Revises: 7f4a1c92d6b3
Create Date: 2026-08-09 15:30:00.000000+00:00

A corrective grant, found by the Phase 6 live run.

`clinical.rbac.FULFILMENT_ROLES` names `DOCTOR` and `NURSE` as the roles that
may clear a PROCEDURE order, and `DOCTOR` and `RECORDS_OFFICER` for a REFERRAL.
None of those three roles held `order:fulfil`, so the route's permission gate
refused before the role check was ever reached — the mapping named people who
could not act on it.

The consequence was not cosmetic. A doctor who performed a procedure in the room
could order it and then not mark it done, so the encounter sat in
`PENDING_CLEARANCE` until a hospital administrator noticed. That is precisely
the "why won't this close?" failure the state machine exists to prevent, and it
would have been invisible in a permission matrix — which is why
`test_clinical_service.py` now asserts that every role named in
`FULFILMENT_ROLES` actually holds the permission.

A new migration rather than an edit to `a3f7c21d9e04`: that one has already been
applied, and rewriting applied history would let two databases claiming the same
revision disagree about what is in them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b2ed4471a58"
down_revision: str | None = "7f4a1c92d6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION = "order:fulfil"

# Exactly the roles `FULFILMENT_ROLES` names but that could not act.
GRANT_TO: tuple[str, ...] = ("DOCTOR", "NURSE", "RECORDS_OFFICER")


def upgrade() -> None:
    connection = op.get_bind()
    for role_code in GRANT_TO:
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
            {"role_code": role_code, "permission_code": PERMISSION},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE permission_id = (SELECT id FROM permissions WHERE code = :permission_code)
               AND role_id IN (
                   SELECT id FROM roles
                    WHERE code = ANY(:role_codes) AND hospital_id IS NULL
               )
            """
        ),
        {"permission_code": PERMISSION, "role_codes": list(GRANT_TO)},
    )
