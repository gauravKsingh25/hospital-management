"""Permissions enforced by `notifications`.

Two splits carry the weight here.

**Reading a message is not reading a patient record, but it is close.** A
notification body contains "your CBC report is ready" against a named person, so
`notification:read` is a clinical-adjacent permission, not an operational one. It
is not handed to every staff role by default.

**Suppressing is separated from lifting.** `suppression:manage` covers recording
an opt-out; there is deliberately *no* permission that lifts a `DECEASED`
suppression, because no role should have it. `service.lift_suppression` refuses
that reason outright — the check is in the service, not in RBAC, precisely so
that a hospital administrator editing role rows cannot grant it back.

Template codes are prefixed `notification_template:` rather than `template:`
because `permissions.code` is unique across the whole deployment and `clinical`
already owns `template:read` for note templates. Two different things called the
same name in one permission table is how an access review ends up wrong.
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "notifications"


class NotificationPermissions:
    NOTIFICATION_READ = "notification:read"
    # Compose and send a message by hand, and retry one that failed.
    NOTIFICATION_SEND = "notification:send"
    NOTIFICATION_CANCEL = "notification:cancel"

    TEMPLATE_READ = "notification_template:read"
    TEMPLATE_MANAGE = "notification_template:manage"

    SUPPRESSION_READ = "suppression:read"
    # Record an opt-out, or lift one. Never lifts a death — see the service.
    SUPPRESSION_MANAGE = "suppression:manage"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(
        NotificationPermissions.NOTIFICATION_READ, MODULE, "View messages sent to patients."
    ),
    PermissionDef(
        NotificationPermissions.NOTIFICATION_SEND, MODULE, "Send or retry a message by hand."
    ),
    PermissionDef(NotificationPermissions.NOTIFICATION_CANCEL, MODULE, "Cancel a queued message."),
    PermissionDef(NotificationPermissions.TEMPLATE_READ, MODULE, "View message templates."),
    PermissionDef(
        NotificationPermissions.TEMPLATE_MANAGE, MODULE, "Create or edit message templates."
    ),
    PermissionDef(
        NotificationPermissions.SUPPRESSION_READ, MODULE, "View who is blocked from messaging."
    ),
    PermissionDef(
        NotificationPermissions.SUPPRESSION_MANAGE, MODULE, "Record or lift a messaging opt-out."
    ),
)

_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)

_READ = (
    NotificationPermissions.NOTIFICATION_READ,
    NotificationPermissions.TEMPLATE_READ,
    NotificationPermissions.SUPPRESSION_READ,
)

# The front desk is where "I never got the message" is said out loud, so
# reception can look, resend, and record an opt-out the patient asks for at the
# counter. It cannot rewrite the hospital's standing copy.
_RECEPTION = (
    *_READ,
    NotificationPermissions.NOTIFICATION_SEND,
    NotificationPermissions.NOTIFICATION_CANCEL,
    NotificationPermissions.SUPPRESSION_MANAGE,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    Roles.RECEPTIONIST: _RECEPTION,
    # A cashier chases payment; resending a receipt is the job.
    Roles.CASHIER: (
        NotificationPermissions.NOTIFICATION_READ,
        NotificationPermissions.NOTIFICATION_SEND,
    ),
    Roles.BILLING_STAFF: (
        NotificationPermissions.NOTIFICATION_READ,
        NotificationPermissions.NOTIFICATION_SEND,
        NotificationPermissions.SUPPRESSION_READ,
    ),
    # A doctor sees what their patient was told — a patient who acts on a
    # message the doctor has not seen is a consultation starting from behind.
    Roles.DOCTOR: (NotificationPermissions.NOTIFICATION_READ,),
    Roles.NURSE: (NotificationPermissions.NOTIFICATION_READ,),
    # The lab tells a patient their sample has to be repeated.
    Roles.LAB_TECH: (
        NotificationPermissions.NOTIFICATION_READ,
        NotificationPermissions.NOTIFICATION_SEND,
    ),
    Roles.PATHOLOGIST: (NotificationPermissions.NOTIFICATION_READ,),
    Roles.RADIOLOGIST: (NotificationPermissions.NOTIFICATION_READ,),
    Roles.PHARMACIST: (NotificationPermissions.NOTIFICATION_READ,),
    # Records officers record deaths, and are the people asked why a family
    # stopped receiving messages.
    Roles.RECORDS_OFFICER: (
        NotificationPermissions.NOTIFICATION_READ,
        NotificationPermissions.SUPPRESSION_READ,
        NotificationPermissions.SUPPRESSION_MANAGE,
    ),
    Roles.AUDITOR: _READ,
}
