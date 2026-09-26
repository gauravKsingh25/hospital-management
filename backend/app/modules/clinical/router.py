"""Clinical HTTP routes. Thin — logic lives in `service.py`.

The route layout follows CLAUDE.md §7: the doctor's surface is deliberately the
smallest here. `GET /encounters/{id}/chart` opens their whole screen in one call
and `POST /encounters/{id}/complete` closes it in one more, with the state
machine deciding where the visit goes rather than the doctor picking a status.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import Page, PageParams, page_params
from app.modules.clinical import service
from app.modules.clinical.models import (
    Encounter,
    EncounterStatus,
    OrderStatus,
    OrderType,
    TemplateType,
)
from app.modules.clinical.rbac import ClinicalPermissions
from app.modules.clinical.schemas import (
    AdmitRequest,
    CancelEncounterRequest,
    CompleteConsultationRequest,
    DeathRecord,
    DiagnosisCreate,
    DiagnosisRead,
    EncounterChart,
    EncounterEventRead,
    EncounterOpen,
    EncounterRead,
    EncounterSummary,
    EncounterUpdate,
    LamaRecord,
    NoteCreate,
    NoteRead,
    NoteTemplateCreate,
    NoteTemplateRead,
    NoteTemplateUpdate,
    OrderCancelRequest,
    OrderCreate,
    OrderRead,
    PendingItems,
    ReferralRecord,
    VitalsCreate,
    VitalsRead,
)
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext

encounters_router = APIRouter(prefix="/encounters", tags=["clinical"])
orders_router = APIRouter(prefix="/orders", tags=["clinical"])
templates_router = APIRouter(prefix="/note-templates", tags=["clinical"])

CanReadEncounter = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_READ))]
CanCreateEncounter = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_CREATE))]
CanUpdateEncounter = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_UPDATE))]
CanCompleteEncounter = Annotated[
    AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_COMPLETE))
]
CanAdmit = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_ADMIT))]
CanCancelEncounter = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_CANCEL))]
CanRecordDeath = Annotated[
    AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_RECORD_DEATH))
]
CanRecordReferral = Annotated[
    AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_RECORD_REFERRAL))
]
CanRecordLama = Annotated[AuthContext, Depends(require(ClinicalPermissions.ENCOUNTER_RECORD_LAMA))]

CanReadVitals = Annotated[AuthContext, Depends(require(ClinicalPermissions.VITALS_READ))]
CanRecordVitals = Annotated[AuthContext, Depends(require(ClinicalPermissions.VITALS_RECORD))]
CanReadNote = Annotated[AuthContext, Depends(require(ClinicalPermissions.NOTE_READ))]
CanWriteNote = Annotated[AuthContext, Depends(require(ClinicalPermissions.NOTE_WRITE))]
CanSignNote = Annotated[AuthContext, Depends(require(ClinicalPermissions.NOTE_SIGN))]
CanReadDiagnosis = Annotated[AuthContext, Depends(require(ClinicalPermissions.DIAGNOSIS_READ))]
CanRecordDiagnosis = Annotated[AuthContext, Depends(require(ClinicalPermissions.DIAGNOSIS_RECORD))]
CanReadOrder = Annotated[AuthContext, Depends(require(ClinicalPermissions.ORDER_READ))]
CanPlaceOrder = Annotated[AuthContext, Depends(require(ClinicalPermissions.ORDER_PLACE))]
CanFulfilOrder = Annotated[AuthContext, Depends(require(ClinicalPermissions.ORDER_FULFIL))]
CanCancelOrder = Annotated[AuthContext, Depends(require(ClinicalPermissions.ORDER_CANCEL))]
CanReadTemplate = Annotated[AuthContext, Depends(require(ClinicalPermissions.TEMPLATE_READ))]
CanManageTemplate = Annotated[AuthContext, Depends(require(ClinicalPermissions.TEMPLATE_MANAGE))]


async def _render_encounter(session: SessionDep, encounter: Encounter) -> EncounterRead:
    """An encounter, with the certifying doctor named if there is one.

    The lookup only happens on a deceased encounter, which is rare — so this is
    not a per-request cost, it is a cost paid on the handful of records where a
    name is not optional. A death record that says "certified by
    01a0076f-7370-…" is not a death record anybody can read, and unlike every
    other identity gap in this system that one ends up on a document a family
    is given.

    Through `identity.service`, never by joining its table (CLAUDE.md §2). A
    missing user degrades to `None` rather than failing the request: the
    certifier's account may since have been deleted, and that must not make the
    death record unreadable.
    """
    rendered = EncounterRead.model_validate(encounter)
    if encounter.death_certified_by_id is not None:
        try:
            certifier = await identity_service.get_user(session, encounter.death_certified_by_id)
        except NotFoundError:
            return rendered
        rendered.death_certified_by_name = certifier.full_name
    return rendered


# ---------------------------------------------------------------------------
# Encounters
# ---------------------------------------------------------------------------
@encounters_router.post(
    "", response_model=EncounterRead, status_code=status.HTTP_201_CREATED, summary="Open a visit"
)
async def open_encounter(
    payload: EncounterOpen,
    session: SessionDep,
    request: Request,
    context: CanCreateEncounter,
    tenant_id: TenantId,
) -> EncounterRead:
    """For casualty and direct admissions.

    The normal OPD path never calls this: check-in and quick-OPD open the
    encounter as part of the same action, because a separate step is a step
    somebody eventually forgets.
    """
    encounter = await service.open_encounter(
        session,
        hospital_id=tenant_id,
        patient_id=payload.patient_id,
        actor=context.user,
        appointment_id=payload.appointment_id,
        doctor_id=payload.doctor_id,
        department_id=payload.department_id,
        encounter_type=payload.encounter_type,
        chief_complaint=payload.chief_complaint,
        triage_note=payload.triage_note,
    )
    await identity_service.record_audit(
        session,
        action="encounter.open",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={"encounter_number": encounter.encounter_number},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, encounter)


@encounters_router.get("", response_model=Page[EncounterSummary], summary="List visits")
async def list_encounters(
    session: SessionDep,
    context: CanReadEncounter,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    patient_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
    encounter_status: EncounterStatus | None = None,
    active_only: bool = False,
    on_date: Annotated[date | None, Query(alias="date")] = None,
) -> Page[EncounterSummary]:
    encounters, total = await service.list_encounters(
        session,
        params,
        hospital_id=tenant_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        status=encounter_status,
        active_only=active_only,
        on_date=on_date,
    )
    return Page.build(
        [EncounterSummary.model_validate(item) for item in encounters], total=total, params=params
    )


@encounters_router.get(
    "/patient/{patient_id}/timeline",
    response_model=Page[EncounterEventRead],
    summary="The patient's whole timeline",
)
async def get_patient_timeline(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CanReadEncounter,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[EncounterEventRead]:
    """Registration -> OPD -> lab -> admission -> discharge -> follow-up.

    Declared before `/{encounter_id}` so the literal segment wins the match.
    """
    events, total = await service.get_patient_timeline(
        session, params, patient_id=patient_id, hospital_id=tenant_id
    )
    return Page.build(
        [EncounterEventRead.model_validate(event) for event in events], total=total, params=params
    )


@encounters_router.get("/{encounter_id}", response_model=EncounterRead, summary="Get a visit")
async def get_encounter(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadEncounter, tenant_id: TenantId
) -> EncounterRead:
    return await _render_encounter(
        session, await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    )


@encounters_router.get(
    "/{encounter_id}/chart",
    response_model=EncounterChart,
    summary="The whole consultation screen in one call",
)
async def get_chart(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadNote, tenant_id: TenantId
) -> EncounterChart:
    """Gated on `note:read`, not `encounter:read`.

    A cashier may legitimately look up a visit to bill it; the chart carries the
    examination note and the diagnoses, and that is a different question.
    """
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    chart = EncounterChart.model_validate(await service.build_chart(session, encounter))
    # The chart is what the consultation screen renders, including the outcome
    # panel on a closed visit — so the certifier has to be named here too.
    chart.encounter = await _render_encounter(session, encounter)
    return chart


@encounters_router.get(
    "/{encounter_id}/timeline",
    response_model=list[EncounterEventRead],
    summary="The visit timeline",
)
async def get_timeline(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadEncounter, tenant_id: TenantId
) -> list[EncounterEventRead]:
    """CLAUDE.md §7b — the event feed rendered chronologically."""
    await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    return [
        EncounterEventRead.model_validate(event)
        for event in await service.get_timeline(session, encounter_id)
    ]


@encounters_router.get(
    "/{encounter_id}/pending",
    response_model=PendingItems,
    summary="Why this visit will not close yet",
)
async def get_pending(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadEncounter, tenant_id: TenantId
) -> PendingItems:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    return await service.pending_items(session, encounter)


@encounters_router.patch("/{encounter_id}", response_model=EncounterRead, summary="Edit a visit")
async def update_encounter(
    encounter_id: uuid.UUID,
    payload: EncounterUpdate,
    session: SessionDep,
    context: CanUpdateEncounter,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.update_encounter(session, encounter, payload)
    await session.commit()
    return await _render_encounter(session, updated)


# --- transitions -----------------------------------------------------------
@encounters_router.post(
    "/{encounter_id}/start", response_model=EncounterRead, summary="Start the consultation"
)
async def start_consultation(
    encounter_id: uuid.UUID,
    session: SessionDep,
    context: CanCompleteEncounter,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.start_consultation(session, encounter, actor=context.user)
    await session.commit()
    return await _render_encounter(session, updated)


@encounters_router.post(
    "/{encounter_id}/complete",
    response_model=EncounterRead,
    summary="Doctor: complete the consultation",
)
async def complete_consultation(
    encounter_id: uuid.UUID,
    payload: CompleteConsultationRequest,
    session: SessionDep,
    request: Request,
    context: CanCompleteEncounter,
    tenant_id: TenantId,
) -> EncounterRead:
    """The key rule of CLAUDE.md §6.

    Whether this closes the visit or parks it in `AWAITING_RESULTS` /
    `PENDING_CLEARANCE` depends on what is still open against it — the doctor
    does not choose, and the patient does not have to walk back to reception.
    """
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.complete_consultation(
        session,
        encounter,
        actor=context.user,
        follow_up_date=payload.follow_up_date,
        follow_up_instructions=payload.follow_up_instructions,
        reason=payload.notes,
    )
    await identity_service.record_audit(
        session,
        action="encounter.complete_consultation",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={"status": updated.status.value},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


@encounters_router.post(
    "/{encounter_id}/admit", response_model=EncounterRead, summary="Convert to an admission"
)
async def admit(
    encounter_id: uuid.UUID,
    payload: AdmitRequest,
    session: SessionDep,
    request: Request,
    context: CanAdmit,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.admit(
        session,
        encounter,
        actor=context.user,
        reason=payload.reason,
        department_id=payload.department_id,
    )
    await identity_service.record_audit(
        session,
        action="encounter.admit",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


@encounters_router.post(
    "/{encounter_id}/cancel", response_model=EncounterRead, summary="Cancel or mark no-show"
)
async def cancel_encounter(
    encounter_id: uuid.UUID,
    payload: CancelEncounterRequest,
    session: SessionDep,
    request: Request,
    context: CanCancelEncounter,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.cancel_encounter(
        session, encounter, reason=payload.reason, no_show=payload.no_show, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="encounter.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={"status": updated.status.value, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


# --- the three edge cases (CLAUDE.md §6) -----------------------------------
@encounters_router.post(
    "/{encounter_id}/death", response_model=EncounterRead, summary="Record a death"
)
async def record_death(
    encounter_id: uuid.UUID,
    payload: DeathRecord,
    session: SessionDep,
    request: Request,
    context: CanRecordDeath,
    tenant_id: TenantId,
) -> EncounterRead:
    """Terminal, audited, and it flags the patient record in the same transaction.

    That last part is what stops a follow-up SMS ever reaching the family
    (CLAUDE.md §14).
    """
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)

    # The certifying doctor must be a real user in this hospital — loaded
    # through identity's service, never by joining its table (CLAUDE.md §2).
    certifier = await identity_service.get_user(session, payload.death_certified_by_id)
    if certifier.hospital_id != tenant_id:
        raise ValidationError(
            "The certifying doctor must be a clinician at this hospital.",
            code="certifier_not_found",
        )

    updated = await service.record_death(session, encounter, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="encounter.record_death",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={
            "deceased_at": payload.deceased_at.isoformat(),
            "certified_by": certifier.email,
            "cause": payload.cause_of_death,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


@encounters_router.post(
    "/{encounter_id}/referral", response_model=EncounterRead, summary="Refer the patient out"
)
async def record_referral(
    encounter_id: uuid.UUID,
    payload: ReferralRecord,
    session: SessionDep,
    request: Request,
    context: CanRecordReferral,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.record_referral(session, encounter, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="encounter.record_referral",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={"to": payload.referred_to_facility, "reason": payload.referral_reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


@encounters_router.post(
    "/{encounter_id}/lama",
    response_model=EncounterRead,
    summary="Record leaving against medical advice",
)
async def record_lama(
    encounter_id: uuid.UUID,
    payload: LamaRecord,
    session: SessionDep,
    request: Request,
    context: CanRecordLama,
    tenant_id: TenantId,
) -> EncounterRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    updated = await service.record_lama(session, encounter, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="encounter.record_lama",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="encounter",
        resource_id=encounter.id,
        changes={"reason": payload.lama_reason, "form_signed": payload.lama_form_signed},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_encounter(session, updated)


# ---------------------------------------------------------------------------
# Vitals, notes, diagnoses
# ---------------------------------------------------------------------------
@encounters_router.post(
    "/{encounter_id}/vitals",
    response_model=VitalsRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record vitals",
)
async def record_vitals(
    encounter_id: uuid.UUID,
    payload: VitalsCreate,
    session: SessionDep,
    context: CanRecordVitals,
    tenant_id: TenantId,
) -> VitalsRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    vitals = await service.record_vitals(session, encounter, payload, actor=context.user)
    await session.commit()
    return VitalsRead.model_validate(vitals)


@encounters_router.get(
    "/{encounter_id}/vitals", response_model=list[VitalsRead], summary="Vitals for a visit"
)
async def list_vitals(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadVitals, tenant_id: TenantId
) -> list[VitalsRead]:
    await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    rows = await service.list_vitals(session, encounter_id)
    return [VitalsRead.model_validate(row) for row in rows]


@encounters_router.post(
    "/{encounter_id}/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Write a clinical note",
)
async def write_note(
    encounter_id: uuid.UUID,
    payload: NoteCreate,
    session: SessionDep,
    context: CanWriteNote,
    tenant_id: TenantId,
) -> NoteRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    note = await service.write_note(session, encounter, payload, actor=context.user)
    await session.commit()
    return NoteRead.model_validate(note)


@encounters_router.get(
    "/{encounter_id}/notes", response_model=list[NoteRead], summary="Notes on a visit"
)
async def list_notes(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadNote, tenant_id: TenantId
) -> list[NoteRead]:
    await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    notes = await service.list_notes(session, encounter_id)
    return [NoteRead.model_validate(note) for note in notes]


@encounters_router.post(
    "/notes/{note_id}/sign", response_model=NoteRead, summary="e-Sign a draft note"
)
async def sign_note(
    note_id: uuid.UUID, session: SessionDep, context: CanSignNote, tenant_id: TenantId
) -> NoteRead:
    note = await service.get_note(session, note_id, hospital_id=tenant_id)
    signed = await service.sign_note(session, note, actor=context.user)
    await session.commit()
    return NoteRead.model_validate(signed)


@encounters_router.post(
    "/{encounter_id}/diagnoses",
    response_model=DiagnosisRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a diagnosis",
)
async def record_diagnosis(
    encounter_id: uuid.UUID,
    payload: DiagnosisCreate,
    session: SessionDep,
    context: CanRecordDiagnosis,
    tenant_id: TenantId,
) -> DiagnosisRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    diagnosis = await service.record_diagnosis(session, encounter, payload, actor=context.user)
    await session.commit()
    return DiagnosisRead.model_validate(diagnosis)


@encounters_router.get(
    "/{encounter_id}/diagnoses", response_model=list[DiagnosisRead], summary="Diagnoses on a visit"
)
async def list_diagnoses(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadDiagnosis, tenant_id: TenantId
) -> list[DiagnosisRead]:
    await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    return [
        DiagnosisRead.model_validate(row)
        for row in await service.list_diagnoses(session, encounter_id)
    ]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@encounters_router.post(
    "/{encounter_id}/orders",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Place an order",
)
async def place_order(
    encounter_id: uuid.UUID,
    payload: OrderCreate,
    session: SessionDep,
    context: CanPlaceOrder,
    tenant_id: TenantId,
) -> OrderRead:
    encounter = await service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    order = await service.place_order(session, encounter, payload, actor=context.user)
    await session.commit()
    return OrderRead.model_validate(order)


@orders_router.get("", response_model=Page[OrderRead], summary="Order work list")
async def list_orders(
    session: SessionDep,
    context: CanReadOrder,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    encounter_id: uuid.UUID | None = None,
    order_type: OrderType | None = None,
    order_status: OrderStatus | None = None,
    open_only: bool = False,
) -> Page[OrderRead]:
    """The lab's, radiology's and pharmacy's queues all come from here."""
    orders, total = await service.list_orders(
        session,
        params,
        hospital_id=tenant_id,
        encounter_id=encounter_id,
        order_type=order_type,
        status=order_status,
        open_only=open_only,
    )
    return Page.build(
        [OrderRead.model_validate(order) for order in orders], total=total, params=params
    )


@orders_router.post("/{order_id}/start", response_model=OrderRead, summary="Begin work on an order")
async def start_order(
    order_id: uuid.UUID, session: SessionDep, context: CanFulfilOrder, tenant_id: TenantId
) -> OrderRead:
    order = await service.get_order(session, order_id, hospital_id=tenant_id)
    service.assert_may_fulfil(order, context.roles)
    updated = await service.start_order(session, order, actor=context.user)
    await session.commit()
    return OrderRead.model_validate(updated)


@orders_router.post(
    "/{order_id}/complete",
    response_model=OrderRead,
    summary="Clear an order (and close the visit if it was the last)",
)
async def complete_order(
    order_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanFulfilOrder,
    tenant_id: TenantId,
) -> OrderRead:
    """CLAUDE.md §6 — the last cleared item is what closes the visit.

    The staff member who clears it never has to know that; they mark their own
    work done and the encounter follows.
    """
    order = await service.get_order(session, order_id, hospital_id=tenant_id)
    service.assert_may_fulfil(order, context.roles)
    updated, encounter = await service.complete_order(
        session, order, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="order.complete",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="order",
        resource_id=order.id,
        changes={"item": order.item_name, "encounter_status": encounter.status.value},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return OrderRead.model_validate(updated)


@orders_router.post("/{order_id}/cancel", response_model=OrderRead, summary="Cancel an order")
async def cancel_order(
    order_id: uuid.UUID,
    payload: OrderCancelRequest,
    session: SessionDep,
    context: CanCancelOrder,
    tenant_id: TenantId,
) -> OrderRead:
    order = await service.get_order(session, order_id, hospital_id=tenant_id)
    updated, _ = await service.cancel_order(
        session, order, reason=payload.reason, actor=context.user, hospital_id=tenant_id
    )
    await session.commit()
    return OrderRead.model_validate(updated)


# ---------------------------------------------------------------------------
# Templates (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
@templates_router.post(
    "",
    response_model=NoteTemplateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a note template",
)
async def create_template(
    payload: NoteTemplateCreate,
    session: SessionDep,
    context: CanReadTemplate,
    tenant_id: TenantId,
) -> NoteTemplateRead:
    """Personal templates need only `template:read`; sharing one hospital-wide
    needs `template:manage`, because it lands on everyone else's screen."""
    if payload.shared and not context.has_permission(ClinicalPermissions.TEMPLATE_MANAGE):
        raise ValidationError(
            "You can create your own templates, but not hospital-wide ones.",
            code="template_share_denied",
        )
    template = await service.create_template(
        session, payload, hospital_id=tenant_id, actor=context.user
    )
    await session.commit()
    return NoteTemplateRead.model_validate(template)


@templates_router.get(
    "", response_model=list[NoteTemplateRead], summary="Templates available to me"
)
async def list_templates(
    session: SessionDep,
    context: CanReadTemplate,
    tenant_id: TenantId,
    template_type: TemplateType | None = None,
) -> list[NoteTemplateRead]:
    rows = await service.list_templates(
        session, hospital_id=tenant_id, owner_id=context.user.id, template_type=template_type
    )
    return [NoteTemplateRead.model_validate(row) for row in rows]


@templates_router.patch(
    "/{template_id}", response_model=NoteTemplateRead, summary="Edit a note template"
)
async def update_template(
    template_id: uuid.UUID,
    payload: NoteTemplateUpdate,
    session: SessionDep,
    context: CanReadTemplate,
    tenant_id: TenantId,
) -> NoteTemplateRead:
    template = await service.get_template(session, template_id, hospital_id=tenant_id)
    may_edit = template.owner_id == context.user.id or context.has_permission(
        ClinicalPermissions.TEMPLATE_MANAGE
    )
    if not may_edit:
        raise NotFoundError("Template not found.", code="template_not_found")

    updated = await service.update_template(session, template, payload)
    await session.commit()
    return NoteTemplateRead.model_validate(updated)
