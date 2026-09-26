"""notifications rls, permissions and the deceased guard

Revision ID: c58d0a3e91b7
Revises: 374aadf21718
Create Date: 2026-08-11 18:55:00.000000+00:00

Three things, on the pattern the identity, patients, scheduling, clinical,
diagnostics and billing migrations established — plus one that is new.

1. **Row-level security** on the four notification tables.
2. **The permission seed**, mirroring `notifications/rbac.py`. Template codes are
   prefixed `notification_template:` because `permissions.code` is unique across
   the whole deployment and `clinical` already owns `template:read` for note
   templates.
3. **The deceased guard** — the new one, and the reason this migration is worth
   reading.

There is deliberately **no template seed here.** The English and Hindi copy ships
in `notifications/templates.py` as `DEFAULT_TEMPLATES`, and a
`notification_templates` row is an override rather than the only source. Seeding
rows per tenant in a migration has one flaw that only shows up later: a hospital
onboarded *after* this runs gets nothing, and starts life sending bare fallback
text that nobody notices because the messages still technically go out.

---------------------------------------------------------------------------
Why a database trigger for the deceased rule
---------------------------------------------------------------------------

CLAUDE.md §14 names *never send a notification to a deceased patient* as an
invariant to actively guard. The application already checks it twice: once when
a message is queued, and again immediately before it is handed to a gateway.
Both live in `notifications/service.py`.

Both are also one careless refactor, one new module, or one well-meaning bulk
script away from being bypassed. That is precisely the argument CLAUDE.md §3
makes for putting row-level security under multi-tenancy — *"so even a buggy
query cannot leak cross-tenant data"* — and it applies here with more force,
because the failure mode is a bereaved family receiving "please book your
follow-up appointment" by SMS.

So the rule is also a `BEFORE INSERT` trigger on `notification_attempts`, which
is the exact moment a message stops being a row and becomes something a patient
will read. It refuses the insert for any attempt whose parent notification is
addressed to a deceased patient. Nothing in the application can send around it,
including code that has not been written yet.

Two implementation notes:

* **SECURITY DEFINER.** The function reads `patients`, which is under RLS. As an
  ordinary function it would see nothing when the tenant GUC is unset and would
  then wave the attempt through — a guard that fails open, which is worse than
  no guard because it looks like one. Running as the owner means it always sees
  the patient row.
* **It fails closed.** If the patient cannot be found at all, the attempt is
  refused. The FK guarantees the row exists, so "not found" means something is
  wrong, and the safe answer when you cannot prove a patient is alive is not to
  message them.

Staff-directed messages are untouched. A notification with
`recipient_type = 'STAFF'` about a deceased patient — the settlement notice
CLAUDE.md §6 requires — passes the trigger, because the family is not the
recipient.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c58d0a3e91b7"
down_revision: str | None = "374aadf21718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
_CURRENT_TENANT = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_TENANT_TABLES = (
    "notification_templates",
    "notifications",
    "notification_attempts",
    "notification_suppressions",
)

NOTIFICATIONS = "notifications"

# (code, module, description) — mirrors `notifications/rbac.py`, which is where
# route handlers import these codes from.
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("notification:read", NOTIFICATIONS, "View messages sent to patients."),
    ("notification:send", NOTIFICATIONS, "Send or retry a message by hand."),
    ("notification:cancel", NOTIFICATIONS, "Cancel a queued message."),
    ("notification_template:read", NOTIFICATIONS, "View message templates."),
    ("notification_template:manage", NOTIFICATIONS, "Create or edit message templates."),
    ("suppression:read", NOTIFICATIONS, "View who is blocked from messaging."),
    ("suppression:manage", NOTIFICATIONS, "Record or lift a messaging opt-out."),
)

_ALL = tuple(code for code, _, _ in PERMISSIONS)

_READ = ("notification:read", "notification_template:read", "suppression:read")

# The front desk is where "I never got the message" is said out loud: reception
# can look, resend, and record an opt-out asked for at the counter. It cannot
# rewrite the hospital's standing copy.
_RECEPTION = (*_READ, "notification:send", "notification:cancel", "suppression:manage")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _ALL,
    "HOSPITAL_ADMIN": _ALL,
    "RECEPTIONIST": _RECEPTION,
    "CASHIER": ("notification:read", "notification:send"),
    "BILLING_STAFF": ("notification:read", "notification:send", "suppression:read"),
    # A patient who acts on a message the doctor has not seen is a consultation
    # starting from behind.
    "DOCTOR": ("notification:read",),
    "NURSE": ("notification:read",),
    # The lab tells a patient their sample has to be repeated.
    "LAB_TECH": ("notification:read", "notification:send"),
    "RADIOLOGIST": ("notification:read",),
    "PHARMACIST": ("notification:read",),
    # Records officers record deaths, and are asked why a family stopped
    # receiving messages.
    "RECORDS_OFFICER": ("notification:read", "suppression:read", "suppression:manage"),
    "AUDITOR": _READ,
}


_DECEASED_GUARD = """
CREATE OR REPLACE FUNCTION notifications_refuse_deceased()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_recipient text;
    v_patient   uuid;
    v_deceased  boolean;
BEGIN
    SELECT n.recipient_type::text, n.patient_id
      INTO v_recipient, v_patient
      FROM notifications n
     WHERE n.id = NEW.notification_id;

    -- Staff-directed messages, and messages about nobody in particular, are
    -- none of this trigger's business. CLAUDE.md 6 requires the settlement
    -- notice for a deceased patient to reach the billing desk.
    IF v_recipient IS DISTINCT FROM 'PATIENT' OR v_patient IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.is_deceased INTO v_deceased FROM patients p WHERE p.id = v_patient;

    -- Fails closed. A patient we cannot read is a patient we cannot certify as
    -- alive, and the safe answer is not to message them. The foreign key means
    -- this should be unreachable.
    IF v_deceased IS NULL THEN
        RAISE EXCEPTION
            'notification attempt refused: patient % could not be verified', v_patient
            USING ERRCODE = 'raise_exception';
    END IF;

    IF v_deceased THEN
        RAISE EXCEPTION
            'notification attempt refused: patient % is deceased', v_patient
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN NEW;
END;
$$;
"""


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

    # --- the deceased guard (CLAUDE.md §14) --------------------------------
    op.execute(_DECEASED_GUARD)
    op.execute(
        """
        CREATE TRIGGER trg_notification_attempts_refuse_deceased
        BEFORE INSERT ON notification_attempts
        FOR EACH ROW EXECUTE FUNCTION notifications_refuse_deceased()
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

    op.execute(
        "DROP TRIGGER IF EXISTS trg_notification_attempts_refuse_deceased ON notification_attempts"
    )
    op.execute("DROP FUNCTION IF EXISTS notifications_refuse_deceased()")

    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
