"""row level security and audit append only

Revision ID: 5e46114656c0
Revises: 2106661f1238
Create Date: 2026-08-01 16:42:36.595778+00:00

Two hard database-level guarantees that the application cannot talk its way
around:

**Row-Level Security (CLAUDE.md §3).** Every tenant-scoped table gets a policy
keyed on the `app.hospital_id` GUC the request sets. This is a backstop, not
the primary filter — services still scope their own queries — but it means a
buggy or forgotten `WHERE hospital_id = ...` cannot leak another hospital's
patients.

`FORCE ROW LEVEL SECURITY` is essential here: without it, policies are skipped
for the table's owner, and the application connects as the database owner on
both Neon and the local container. Without FORCE this migration would look
correct and protect nothing.

The `app.bypass_rls` escape hatch exists for the operations CLAUDE.md §3
explicitly permits to cross tenants: authenticating a user before their tenant
is known, and platform-admin work. It is set transaction-locally by
`app.core.database.system_context`.

**Append-only audit log (CLAUDE.md §8, §12).** A trigger refuses UPDATE and
DELETE on `audit_logs`, so the trail is tamper-evident against the application
itself, not merely by convention.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "5e46114656c0"
down_revision: str | None = "2106661f1238"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Reusable SQL fragments -----------------------------------------------------

# True when the caller is running a sanctioned cross-tenant operation.
_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
# The tenant bound to the current transaction; NULL when unset, and NULL never
# equals anything — so an unbound session sees no tenant rows at all.
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

# Tables carrying their own hospital_id.
#   (table, allow_null_tenant)
# `allow_null_tenant` marks tables where a NULL hospital_id means "shared by
# every tenant" — the seeded system roles. It is deliberately NOT set for
# `users`: a platform admin's account must not be visible to every hospital.
_TENANT_TABLES: tuple[tuple[str, bool], ...] = (
    ("users", False),
    ("roles", True),
    ("refresh_tokens", False),
    ("audit_logs", False),
)

# Link tables with no tenant column of their own; they inherit isolation from
# the row they point at.
_DERIVED_POLICIES: tuple[tuple[str, str], ...] = (
    (
        "user_roles",
        """
        EXISTS (
            SELECT 1 FROM users u
             WHERE u.id = user_roles.user_id
               AND u.hospital_id IS NOT DISTINCT FROM {tenant}
        )
        """,
    ),
    (
        "role_permissions",
        """
        EXISTS (
            SELECT 1 FROM roles r
             WHERE r.id = role_permissions.role_id
               AND (r.hospital_id IS NULL OR r.hospital_id = {tenant})
        )
        """,
    ),
)


def upgrade() -> None:
    # --- hospitals: a tenant may see only itself --------------------------
    op.execute("ALTER TABLE hospitals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hospitals FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON hospitals
        USING ({_BYPASS} OR id = {_CURRENT_TENANT})
        WITH CHECK ({_BYPASS} OR id = {_CURRENT_TENANT})
        """
    )

    # --- tables with a hospital_id column ---------------------------------
    for table, allow_null_tenant in _TENANT_TABLES:
        null_clause = "hospital_id IS NULL OR " if allow_null_tenant else ""
        predicate = f"{_BYPASS} OR {null_clause}hospital_id = {_CURRENT_TENANT}"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # --- link tables ------------------------------------------------------
    for table, template in _DERIVED_POLICIES:
        predicate = f"{_BYPASS} OR " + template.format(tenant=_CURRENT_TENANT)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    # `permissions` is deliberately left without RLS: it is a global catalogue
    # of what the software can do, identical for every tenant and containing
    # nothing patient-related.

    # --- append-only audit log --------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_logs_reject_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_logs is append-only; % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()
        """
    )
    # Row-level triggers do not fire for TRUNCATE, so blocking UPDATE/DELETE
    # alone would leave "wipe the whole trail in one statement" available.
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_reject_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_reject_mutation()")

    tables = (
        ["hospitals"]
        + [table for table, _ in _TENANT_TABLES]
        + [table for table, _ in _DERIVED_POLICIES]
    )
    for table in tables:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
