"""Notification HTTP routes. Thin — logic lives in `service.py`.

Three surfaces, because three different people use them: templates are written
once by an administrator, the outbox is read at the front desk when a patient
says "I never got the message", and the suppression list is where an opt-out is
recorded and — for everything except a death — released.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.notifications import service, templates
from app.modules.notifications.models import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationStatus,
    SuppressionReason,
)
from app.modules.notifications.rbac import NotificationPermissions
from app.modules.notifications.schemas import (
    AdhocSend,
    AttemptRead,
    MessageTemplateCreate,
    MessageTemplateRead,
    MessageTemplateUpdate,
    NotificationCancelRequest,
    NotificationRead,
    NotificationSummary,
    SuppressionCreate,
    SuppressionLift,
    SuppressionRead,
    TemplatePreview,
    TemplatePreviewResult,
)
from app.modules.patients import service as patients_service
from app.modules.patients.schemas import PatientRead

notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])
templates_router = APIRouter(prefix="/notifications/templates", tags=["notifications"])
suppressions_router = APIRouter(prefix="/notifications/suppressions", tags=["notifications"])

CanRead = Annotated[AuthContext, Depends(require(NotificationPermissions.NOTIFICATION_READ))]
CanSend = Annotated[AuthContext, Depends(require(NotificationPermissions.NOTIFICATION_SEND))]
CanCancel = Annotated[AuthContext, Depends(require(NotificationPermissions.NOTIFICATION_CANCEL))]
CanReadTemplate = Annotated[AuthContext, Depends(require(NotificationPermissions.TEMPLATE_READ))]
CanManageTemplate = Annotated[
    AuthContext, Depends(require(NotificationPermissions.TEMPLATE_MANAGE))
]
CanReadSuppression = Annotated[
    AuthContext, Depends(require(NotificationPermissions.SUPPRESSION_READ))
]
CanManageSuppression = Annotated[
    AuthContext, Depends(require(NotificationPermissions.SUPPRESSION_MANAGE))
]


class _NamesAPatient(Protocol):
    """A row that has somewhere to put a patient's identity."""

    patient_name: str | None
    uhid: str | None


async def _render(session: SessionDep, notification: Notification) -> NotificationRead:
    """One response carrying the message and every rung tried for it.

    The attempt log is the point of the detail view — "we sent it" is not an
    answer to a patient who did not receive anything, and "WhatsApp rejected it
    at 14:31, the SMS was accepted at 14:31" is.

    No patient lookup here: `NotificationRead` already carries
    `recipient_name`, snapshotted onto the row when the message was composed.
    That is the right value to show — it is who the message was actually
    addressed to, which is not necessarily what the patient record says today.
    """
    rendered = NotificationRead.model_validate(notification)
    rendered.attempt_log = [
        AttemptRead.model_validate(row)
        for row in await service.list_attempts(session, notification.id)
    ]
    return rendered


async def _with_patients[TRow: _NamesAPatient](
    session: SessionDep,
    rows: Sequence[TRow],
    *,
    hospital_id: uuid.UUID,
    patient_id_of: Callable[[TRow], uuid.UUID | None],
) -> list[TRow]:
    """Attach patient name and UHID to a page of rows, in one lookup.

    One query for the page rather than one per row — the same shape as the
    queue, lab, counter and census boards, and for the same reason: these lists
    are read by somebody with a person in front of them, and a list that costs
    a request per row gets slower exactly as the day gets busier.

    Reached through `patients.service` rather than by joining the table, which
    is the boundary CLAUDE.md §2 draws. `tests/test_api_notifications.py`
    counts the statements so a later "just add a join" cannot pass unnoticed.
    """
    patients = await patients_service.get_patients_by_ids(
        session,
        [pid for row in rows if (pid := patient_id_of(row)) is not None],
        hospital_id=hospital_id,
    )
    for row in rows:
        patient_id = patient_id_of(row)
        record = patients.get(patient_id) if patient_id is not None else None
        if record is not None:
            # Through `PatientRead` so the display name comes from the one
            # implementation that owns it, not a second copy here.
            view = PatientRead.model_validate(record)
            row.patient_name = view.full_name
            row.uhid = view.uhid
    return list(rows)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@templates_router.post(
    "",
    response_model=MessageTemplateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a message template",
)
async def create_template(
    payload: MessageTemplateCreate,
    session: SessionDep,
    request: Request,
    context: CanManageTemplate,
    tenant_id: TenantId,
) -> MessageTemplateRead:
    template = await service.create_template(session, payload, hospital_id=tenant_id)
    await identity_service.record_audit(
        session,
        action="notifications.template.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification_template",
        resource_id=template.id,
        changes={
            "code": template.code,
            "channel": template.channel.value,
            "language": template.language,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return MessageTemplateRead.model_validate(template)


@templates_router.get(
    "", response_model=Page[MessageTemplateRead], summary="List message templates"
)
async def list_templates(
    session: SessionDep,
    context: CanReadTemplate,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    code: str | None = None,
    channel: NotificationChannel | None = None,
    language: str | None = None,
    include_inactive: bool = False,
) -> Page[MessageTemplateRead]:
    rows, total = await service.list_templates(
        session,
        params,
        hospital_id=tenant_id,
        code=code,
        channel=channel,
        language=language,
        include_inactive=include_inactive,
    )
    return Page.build(
        [MessageTemplateRead.model_validate(row) for row in rows], total=total, params=params
    )


@templates_router.get("/codes", summary="Every template code the system can send")
async def list_template_codes(context: CanReadTemplate) -> dict[str, list[str]]:
    """What an administrator needs before they can write anything useful.

    Returns the codes and the placeholders each one's fallback copy expects, so
    a template can be authored without reading the source.
    """
    codes = sorted(
        value
        for name, value in vars(templates.TemplateCode).items()
        if not name.startswith("_") and isinstance(value, str)
    )
    return {code: list(templates.placeholders_in(templates.fallback_body(code))) for code in codes}


@templates_router.get(
    "/{template_id}", response_model=MessageTemplateRead, summary="Get a template"
)
async def get_template(
    template_id: uuid.UUID, session: SessionDep, context: CanReadTemplate, tenant_id: TenantId
) -> MessageTemplateRead:
    return MessageTemplateRead.model_validate(
        await service.get_template(session, template_id, hospital_id=tenant_id)
    )


@templates_router.patch(
    "/{template_id}", response_model=MessageTemplateRead, summary="Edit a template"
)
async def update_template(
    template_id: uuid.UUID,
    payload: MessageTemplateUpdate,
    session: SessionDep,
    request: Request,
    context: CanManageTemplate,
    tenant_id: TenantId,
) -> MessageTemplateRead:
    template = await service.get_template(session, template_id, hospital_id=tenant_id)
    updated = await service.update_template(session, template, payload)
    await identity_service.record_audit(
        session,
        action="notifications.template.update",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification_template",
        resource_id=template.id,
        changes=payload.model_dump(exclude_unset=True, mode="json"),
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return MessageTemplateRead.model_validate(updated)


@templates_router.post(
    "/{template_id}/preview",
    response_model=TemplatePreviewResult,
    summary="Render a template without sending it",
)
async def preview_template(
    template_id: uuid.UUID,
    payload: TemplatePreview,
    session: SessionDep,
    context: CanReadTemplate,
    tenant_id: TenantId,
) -> TemplatePreviewResult:
    """The alternative to this endpoint is discovering a typo on a patient's phone."""
    template = await service.get_template(session, template_id, hospital_id=tenant_id)
    result = templates.render(template.body, payload.context, subject=template.subject)
    return TemplatePreviewResult(
        subject=result.subject,
        body=result.body,
        missing=list(result.missing),
        placeholders=list(templates.placeholders_in(template.body)),
    )


# ---------------------------------------------------------------------------
# The outbox
# ---------------------------------------------------------------------------
@notifications_router.get(
    "", response_model=Page[NotificationSummary], summary="Messages sent and queued"
)
async def list_notifications(
    session: SessionDep,
    context: CanRead,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    patient_id: uuid.UUID | None = None,
    encounter_id: uuid.UUID | None = None,
    message_status: NotificationStatus | None = None,
    category: NotificationCategory | None = None,
    channel: NotificationChannel | None = None,
    needs_template: bool | None = None,
) -> Page[NotificationSummary]:
    """The outbox, and — via `needs_template` — the missing-copy worklist."""
    rows, total = await service.list_notifications(
        session,
        params,
        hospital_id=tenant_id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        status=message_status,
        category=category,
        channel=channel,
        needs_template=needs_template,
    )
    return Page.build(
        await _with_patients(
            session,
            [NotificationSummary.model_validate(row) for row in rows],
            hospital_id=tenant_id,
            patient_id_of=lambda row: row.patient_id,
        ),
        total=total,
        params=params,
    )


@notifications_router.post(
    "",
    response_model=NotificationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message by hand",
)
async def send_message(
    payload: AdhocSend,
    session: SessionDep,
    request: Request,
    context: CanSend,
    tenant_id: TenantId,
) -> NotificationRead:
    """Compose and send from the counter.

    Goes through the same dispatcher as every triggered message, so the same
    suppression checks apply. There is deliberately no route that sends without
    them.
    """
    notification = await service.send_adhoc(
        session, payload, hospital_id=tenant_id, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="notifications.send",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification",
        resource_id=notification.id,
        changes={
            "template_code": notification.template_code,
            "recipient_type": notification.recipient_type.value,
            "status": notification.status.value,
            "patient_id": str(notification.patient_id) if notification.patient_id else None,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, notification)


@notifications_router.get(
    "/{notification_id}", response_model=NotificationRead, summary="A message and its attempts"
)
async def get_notification(
    notification_id: uuid.UUID, session: SessionDep, context: CanRead, tenant_id: TenantId
) -> NotificationRead:
    notification = await service.get_notification(session, notification_id, hospital_id=tenant_id)
    return await _render(session, notification)


@notifications_router.post(
    "/{notification_id}/retry",
    response_model=NotificationRead,
    summary="Try a failed message again",
)
async def retry_notification(
    notification_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanSend,
    tenant_id: TenantId,
) -> NotificationRead:
    notification = await service.get_notification(session, notification_id, hospital_id=tenant_id)
    updated = await service.retry(session, notification, actor=context.user)
    await identity_service.record_audit(
        session,
        action="notifications.retry",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification",
        resource_id=notification.id,
        changes={"status": updated.status.value, "attempts": updated.attempts},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, updated)


@notifications_router.post(
    "/{notification_id}/cancel",
    response_model=NotificationRead,
    summary="Withdraw a queued message",
)
async def cancel_notification(
    notification_id: uuid.UUID,
    payload: NotificationCancelRequest,
    session: SessionDep,
    request: Request,
    context: CanCancel,
    tenant_id: TenantId,
) -> NotificationRead:
    notification = await service.get_notification(session, notification_id, hospital_id=tenant_id)
    updated = await service.cancel(session, notification, reason=payload.reason, actor=context.user)
    await identity_service.record_audit(
        session,
        action="notifications.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification",
        resource_id=notification.id,
        changes={"reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, updated)


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------
@suppressions_router.get(
    "", response_model=Page[SuppressionRead], summary="Who is blocked from messaging"
)
async def list_suppressions(
    session: SessionDep,
    context: CanReadSuppression,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    patient_id: uuid.UUID | None = None,
    reason: SuppressionReason | None = None,
    include_lifted: bool = False,
) -> Page[SuppressionRead]:
    rows, total = await service.list_suppressions(
        session,
        params,
        hospital_id=tenant_id,
        patient_id=patient_id,
        reason=reason,
        include_lifted=include_lifted,
    )
    return Page.build(
        await _with_patients(
            session,
            [SuppressionRead.model_validate(row) for row in rows],
            hospital_id=tenant_id,
            patient_id_of=lambda row: row.patient_id,
        ),
        total=total,
        params=params,
    )


@suppressions_router.post(
    "",
    response_model=SuppressionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record an opt-out",
)
async def create_suppression(
    payload: SuppressionCreate,
    session: SessionDep,
    request: Request,
    context: CanManageSuppression,
    tenant_id: TenantId,
) -> SuppressionRead:
    """Record a block a patient asked for, or a number that bounces.

    A death is not recordable here — it is written by the clinical death entry,
    inside that transaction. See `service.suppress`.
    """
    suppression = await service.suppress(
        session, payload, hospital_id=tenant_id, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="notifications.suppression.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification_suppression",
        resource_id=suppression.id,
        changes={
            "patient_id": str(payload.patient_id),
            "reason": payload.reason.value,
            "channel": payload.channel.value if payload.channel else None,
            "category": payload.category.value if payload.category else None,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    # Through the same helper as the list, so a suppression never has a name in
    # one response and a bare id in the next.
    return (
        await _with_patients(
            session,
            [SuppressionRead.model_validate(suppression)],
            hospital_id=tenant_id,
            patient_id_of=lambda row: row.patient_id,
        )
    )[0]


@suppressions_router.post(
    "/{suppression_id}/lift", response_model=SuppressionRead, summary="Release a block"
)
async def lift_suppression(
    suppression_id: uuid.UUID,
    payload: SuppressionLift,
    session: SessionDep,
    request: Request,
    context: CanManageSuppression,
    tenant_id: TenantId,
) -> SuppressionRead:
    """Release a block. A death suppression is refused, whatever the caller holds.

    The refusal is in the service, not in RBAC: permissions are data-driven rows
    an administrator can edit (CLAUDE.md §8), and this is the one rule that must
    not be grantable.
    """
    suppression = await service.get_suppression(session, suppression_id, hospital_id=tenant_id)
    lifted = await service.lift_suppression(
        session, suppression, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="notifications.suppression.lift",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="notification_suppression",
        resource_id=suppression.id,
        changes={"reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return (
        await _with_patients(
            session,
            [SuppressionRead.model_validate(lifted)],
            hospital_id=tenant_id,
            patient_id_of=lambda row: row.patient_id,
        )
    )[0]
