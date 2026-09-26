"""billing rls and permissions

Revision ID: 7f4a1c92d6b3
Revises: 2e6914300484
Create Date: 2026-08-08 22:25:00.000000+00:00

Row-level security for the billing tables, on the pattern the identity,
patients, scheduling, clinical and diagnostics migrations established, plus this
module's permission seed.

The permission split here is deliberately finer than elsewhere, because this is
the module where an access-control mistake becomes theft rather than
inconvenience. Taking money (`payment:record`) is separated from reducing what
is owed (`charge:waive`, `invoice:write_off`, `payment:reverse`); a cashier gets
the first and not the others.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f4a1c92d6b3"
down_revision: str | None = "2e6914300484"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "rate_cards",
    "service_items",
    "service_prices",
    "charges",
    "invoices",
    "payments",
    "insurance_claims",
    "invoice_sequences",
)

BILLING = "billing"

# (code, module, description) — mirrors `billing/rbac.py`, which is where route
# handlers import these codes from.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("ratecard:read", BILLING, "View rate cards and prices."),
    ("ratecard:manage", BILLING, "Create or edit rate cards."),
    ("service:read", BILLING, "View the billable service list."),
    ("service:manage", BILLING, "Edit billable services."),
    ("charge:read", BILLING, "View charges on a visit."),
    ("charge:add", BILLING, "Add a charge by hand."),
    ("charge:waive", BILLING, "Waive a charge."),
    ("charge:discount", BILLING, "Discount a charge."),
    ("invoice:read", BILLING, "View invoices."),
    ("invoice:create", BILLING, "Assemble a draft invoice."),
    ("invoice:issue", BILLING, "Issue an invoice to a patient."),
    ("invoice:cancel", BILLING, "Cancel an invoice."),
    ("invoice:write_off", BILLING, "Write off an unpaid invoice."),
    ("payment:read", BILLING, "View payments and receipts."),
    ("payment:record", BILLING, "Record a payment."),
    ("payment:reverse", BILLING, "Reverse a payment."),
    ("claim:read", BILLING, "View insurance and scheme claims."),
    ("claim:manage", BILLING, "Raise and progress a claim."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

_READ = ("ratecard:read", "service:read", "charge:read", "invoice:read", "payment:read")

# The counter: everything needed to turn a finished visit into a paid one, and
# nothing that reduces what is owed. A cashier who could waive a charge and take
# the cash is a cashier the hospital cannot audit.
_CASHIER = (
    *_READ,
    "charge:add",
    "invoice:create",
    "invoice:issue",
    "payment:record",
)

# The back office: claims, concessions, corrections. Reverses payments and
# writes off bad debt; does not set the price list.
_BILLING_STAFF = (
    *_CASHIER,
    "charge:discount",
    "charge:waive",
    "invoice:cancel",
    "invoice:write_off",
    "payment:reverse",
    "claim:read",
    "claim:manage",
)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "CASHIER": _CASHIER,
    "BILLING_STAFF": _BILLING_STAFF,
    # CLAUDE.md §8: the cashier role "can be merged with receptionist", and in a
    # mid-size hospital it usually is.
    "RECEPTIONIST": _CASHIER,
    # A doctor sees whether the patient in front of them owes money — it changes
    # the conversation — without being able to alter it.
    "DOCTOR": ("charge:read", "invoice:read"),
    "NURSE": ("charge:read",),
    # The pharmacy counter takes money for what it dispenses.
    "PHARMACIST": (*_READ, "charge:add", "payment:record"),
    # The lab quotes a price at the window; it does not collect.
    "LAB_TECH": ("service:read", "ratecard:read"),
    "RADIOLOGIST": ("service:read", "ratecard:read"),
    "RECORDS_OFFICER": ("invoice:read", "charge:read"),
    "AUDITOR": (*_READ, "claim:read"),
}


def upgrade() -> None:
    connection = op.get_bind()

    # --- row-level security ------------------------------------------------
    predicate = f"{_BYPASS} OR hospital_id = {_CURRENT_TENANT}"
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # --- permissions -------------------------------------------------------
    for code, module, description in PERMISSIONS:
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
             WHERE permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"codes": list(_ALL)},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_ALL)}
    )

    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
