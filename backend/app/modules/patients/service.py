"""Patient business logic — the module's public interface."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, case, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.core.redis import get_redis
from app.modules.patients.allergy import allergy_matches
from app.modules.patients.matching import normalize_name, normalize_phone
from app.modules.patients.models import (
    AlertSeverity,
    AlertType,
    ConsentType,
    Gender,
    Patient,
    PatientAlert,
    PatientConsent,
    PatientIdentifier,
    UhidSequence,
)
from app.modules.patients.schemas import (
    AlertCreate,
    ConsentCreate,
    IdentifierCreate,
    PatientIdentify,
    PatientRegister,
    PatientSort,
    PatientUpdate,
)
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "DuplicateMatch",
    "add_identifier",
    "allergy_details",
    "allocate_emergency_tag",
    "allocate_uhid",
    "assert_no_allergy_clash",
    "clashing_allergies",
    "drug_allergies_by_patient",
    "drug_allergy_conflicts",
    "find_duplicates",
    "get_patient",
    "get_patient_alerts",
    "get_patient_by_uhid",
    "get_patients_by_ids",
    "identify_patient",
    "list_patients",
    "mask_phone",
    "merge_patients",
    "note_patient_reads",
    "record_alert",
    "record_consent",
    "record_ids",
    "record_ids_by_patient",
    "register_patient",
    "register_unidentified",
    "resolve_alert",
    "search_patients",
    "update_patient",
    "withdraw_consent",
]

# Below this trigram similarity a name match is noise. Tuned deliberately low:
# at a busy counter, one extra suggestion to glance past costs a second, while a
# missed duplicate costs a split medical history.
NAME_SIMILARITY_THRESHOLD = 0.35
MAX_DUPLICATE_CANDIDATES = 10

UHID_SERIES = "UHID"
EMERGENCY_SERIES = "EM"

# What an unidentified patient is called until somebody knows better.
_UNKNOWN_NAME = {Gender.MALE: "Unknown Male", Gender.FEMALE: "Unknown Female"}


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    patient: Patient
    score: float
    reason: str
    # Whether this is the *identity* collision — same normalised name AND same
    # mobile — rather than merely a strong resemblance. Kept as its own flag
    # instead of a score threshold because two different people can genuinely
    # share a name (similarity 1.0) on different numbers, and treating that as
    # a duplicate would refuse to register the second one.
    is_exact: bool = False


# ---------------------------------------------------------------------------
# UHID allocation
# ---------------------------------------------------------------------------
async def allocate_uhid(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """Allocate the next UHID for a hospital: `CODE-YY-NNNNNN`.

    The counter row is locked with `SELECT ... FOR UPDATE` for the rest of the
    transaction, so two receptionists registering simultaneously serialise here
    instead of colliding on the unique index. A Postgres SEQUENCE would be
    cheaper but is global to the database and gapless-on-rollback is not
    guaranteed — these numbers restart per tenant per year to stay short enough
    to read aloud over a counter, and a visible gap invites "where did patient
    41 go?".
    """
    hospital = await tenancy_service.get_hospital(session, hospital_id)
    year, number = await _next_in_series(session, hospital_id, UHID_SERIES)
    return f"{hospital.code}-{year % 100:02d}-{number:06d}"


async def allocate_emergency_tag(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """The next `EM-0034` for an unidentified arrival.

    Its own series, so tagging an unknown patient does not use up a UHID
    number (the patient still gets a UHID as well). Restarts each year like the
    UHID: the tag is a name casualty shouts across a room, so it stays short.
    """
    _, number = await _next_in_series(session, hospital_id, EMERGENCY_SERIES)
    return f"EM-{number:04d}"


async def _next_in_series(
    session: AsyncSession, hospital_id: uuid.UUID, series: str
) -> tuple[int, int]:
    """Take the next number in one of the hospital's counters: (year, number)."""
    year = datetime.now(UTC).year

    # Ensure the counter row exists before locking it. `ON CONFLICT DO NOTHING`
    # rather than "check, then insert": the check-then-insert version has a
    # window where several transactions all see no row and all try to create
    # one, and every one but the first fails on the unique index. That window is
    # exactly the first registration of a new year, when several counters are
    # most likely to be open at once.
    #
    # If a concurrent transaction is mid-insert, Postgres blocks here until it
    # resolves and then does nothing — which is the serialisation we want.
    await session.execute(
        text(
            """
            INSERT INTO uhid_sequences
                (id, hospital_id, series, year, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :series, :year, 0, now(), now())
            ON CONFLICT (hospital_id, series, year) DO NOTHING
            """
        ),
        {"id": new_id(), "hospital_id": hospital_id, "series": series, "year": year},
    )

    # Now the row is guaranteed to exist; take the lock for the rest of the
    # transaction so two receptionists serialise here rather than colliding on
    # the patients unique index.
    row = (
        (
            await session.execute(
                select(UhidSequence)
                .where(
                    UhidSequence.hospital_id == hospital_id,
                    UhidSequence.series == series,
                    UhidSequence.year == year,
                )
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )

    row.last_value += 1
    session.add(row)
    await session.flush()
    return year, row.last_value


# ---------------------------------------------------------------------------
# Duplicate detection (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
async def find_duplicates(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    full_name: str,
    phone: str,
    exclude_id: uuid.UUID | None = None,
    limit: int = MAX_DUPLICATE_CANDIDATES,
) -> list[DuplicateMatch]:
    """Suggest existing records that might be this same person.

    Three signals, strongest first:

    1. same normalised name **and** same mobile — almost certainly the same
       person, and the pair the unique index refuses outright;
    2. same mobile, different name — usually a family member on the household
       phone, occasionally a misspelling. Shown, never blocked;
    3. similar name (trigram), different mobile — a returning patient who
       changed number.
    """
    normalized_name = normalize_name(full_name)
    normalized_phone = normalize_phone(phone)

    filters: list[ColumnElement[bool]] = [
        col(Patient.hospital_id) == hospital_id,
        col(Patient.deleted_at).is_(None),
        col(Patient.merged_into_id).is_(None),
        # An unidentified record has a placeholder name and no number. It is
        # never "this person again" — it is somebody waiting to be named, and
        # is identified from its own screen rather than matched here.
        col(Patient.is_unidentified).is_(False),
    ]
    if exclude_id is not None:
        filters.append(col(Patient.id) != exclude_id)

    similarity = func.similarity(col(Patient.name_normalized), normalized_name)
    statement = (
        select(Patient, similarity.label("name_similarity"))
        .where(
            *filters,
            (col(Patient.phone) == normalized_phone)
            | (col(Patient.alternate_phone) == normalized_phone)
            | (similarity >= NAME_SIMILARITY_THRESHOLD),
        )
        .order_by(similarity.desc())
        .limit(limit)
    )

    matches: list[DuplicateMatch] = []
    for patient, name_similarity in (await session.execute(statement)).all():
        phone_matches = normalized_phone in {patient.phone, patient.alternate_phone}
        name_matches = patient.name_normalized == normalized_name

        if name_matches and phone_matches:
            matches.append(
                DuplicateMatch(patient, 1.0, "Same name and mobile number", is_exact=True)
            )
        elif phone_matches:
            matches.append(
                DuplicateMatch(
                    patient,
                    max(0.6, float(name_similarity or 0.0)),
                    "Same mobile number — may be a family member",
                )
            )
        else:
            # Capped below 1.0: an identical name on a different number is a
            # strong hint, never a certainty.
            matches.append(
                DuplicateMatch(patient, min(0.95, float(name_similarity or 0.0)), "Similar name")
            )

    matches.sort(key=lambda match: match.score, reverse=True)
    return matches


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def _birth_date_from(
    payload_age: int | None, payload_birth_date: date | None
) -> tuple[date | None, bool]:
    """Turn 'about 40' into a usable date, and remember that it was a guess.

    Storing only an age would silently rot — a record entered as 40 reads as 40
    forever. Deriving a birth date keeps age arithmetic correct downstream, and
    the estimated flag stops anyone treating it as documented fact.
    """
    if payload_birth_date is not None:
        return payload_birth_date, False
    if payload_age is None:
        return None, False
    today = datetime.now(UTC).date()
    try:
        return today.replace(year=today.year - payload_age), True
    except ValueError:  # 29 February
        return today.replace(year=today.year - payload_age, day=28), True


async def register_patient(
    session: AsyncSession,
    payload: PatientRegister,
    *,
    hospital_id: uuid.UUID,
) -> tuple[Patient, list[DuplicateMatch]]:
    """Register a patient, refusing an exact identity clash.

    Returns the new patient plus any near-duplicates worth showing reception —
    the ones that were *not* severe enough to block.

    On an exact (name, mobile) clash this raises `ConflictError` carrying the
    existing UHID, because the overwhelmingly common case is "this patient is
    already registered" and the right outcome is to reuse that record in one
    click, not to create a second history. A genuine second person on the same
    name and number — a father and son — is registered by setting
    `confirm_not_duplicate`, which is what reception already does on paper by
    qualifying the name.
    """
    normalized_name = normalize_name(payload.full_name)
    normalized_phone = normalize_phone(payload.phone)

    if not normalized_name:
        raise ValidationError("A name is required.", code="name_required")

    candidates = await find_duplicates(
        session,
        hospital_id=hospital_id,
        full_name=payload.full_name,
        phone=payload.phone,
    )
    exact = [match for match in candidates if match.is_exact]

    if exact and not payload.confirm_not_duplicate:
        existing = exact[0].patient
        raise ConflictError(
            f"{existing.full_name} is already registered on this mobile number "
            f"(UHID {existing.uhid}).",
            code="patient_already_exists",
            details={
                "patient_id": str(existing.id),
                "uhid": existing.uhid,
                "full_name": existing.full_name,
                # The client turns this into "Use existing record" next to
                # "Register anyway".
                "resolution": "reuse_or_confirm_not_duplicate",
            },
        )

    if exact and payload.confirm_not_duplicate:
        # The unique index would still refuse this. Rather than fail at the
        # database with an opaque error, say plainly what reception must do.
        raise ConflictError(
            "Another patient with this exact name and mobile already exists. "
            "Add a distinguishing detail to the name (for example a guardian's "
            "name) so the two records can be told apart.",
            code="patient_identity_collision",
            details={"uhid": exact[0].patient.uhid},
        )

    birth_date, estimated = _birth_date_from(payload.age_years, payload.birth_date)
    uhid = await allocate_uhid(session, hospital_id)

    patient = Patient(
        hospital_id=hospital_id,
        uhid=uhid,
        full_name=payload.full_name.strip(),
        name_normalized=normalized_name,
        phone=normalized_phone,
        gender=payload.gender,
        birth_date=birth_date,
        birth_date_is_estimated=estimated,
        alternate_phone=normalize_phone(payload.alternate_phone)
        if payload.alternate_phone
        else None,
        guardian_name=payload.guardian_name,
        guardian_relation=payload.guardian_relation,
        guardian_phone=normalize_phone(payload.guardian_phone) if payload.guardian_phone else None,
        address_line1=payload.address_line1,
        city=payload.city,
        state=payload.state,
        pincode=payload.pincode,
        blood_group=payload.blood_group,
        preferred_language=payload.preferred_language,
        registered_at=utc_now(),
    )
    session.add(patient)
    await session.flush()
    await session.refresh(patient)

    soft_matches = [match for match in candidates if not match.is_exact]
    return patient, soft_matches


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def get_patient(
    session: AsyncSession,
    patient_id: uuid.UUID,
    *,
    follow_merge: bool = True,
) -> Patient:
    """Fetch a patient, transparently following a merge.

    Old cards, printed reports and historical encounters keep resolving after a
    merge because reads follow `merged_into_id` rather than 404ing.
    """
    patient = (
        (
            await session.execute(
                select(Patient).where(Patient.id == patient_id, col(Patient.deleted_at).is_(None))
            )
        )
        .scalars()
        .first()
    )

    if patient is None:
        raise NotFoundError("Patient not found.", code="patient_not_found")

    if follow_merge and patient.merged_into_id is not None:
        return await get_patient(session, patient.merged_into_id, follow_merge=True)
    return patient


async def get_patient_by_uhid(
    session: AsyncSession, uhid: str, *, hospital_id: uuid.UUID
) -> Patient | None:
    """Resolve one UHID exactly, following a merge to the surviving record.

    Exact rather than the `ILIKE %term%` the search box uses: a UHID off a card
    or a scanner is a complete identifier, and substring-matching it would let
    `DEMO-26-000042` also match a longer number that happens to contain it.

    Following the merge is the point. Cards, invoices and lab reports carry a
    UHID printed months ago, and the record it names may since have lost a
    merge — with its allergies moved to the survivor. `get_patient` has always
    followed the pointer for the same reason; this is the by-UHID equivalent,
    and it is what makes a scanned card safe to trust.

    Returns `None` rather than raising: "no such patient here" is a normal
    answer for a mistyped or another hospital's UHID, and the route turns it
    into a 404.
    """
    patient = (
        (
            await session.execute(
                select(Patient).where(
                    col(Patient.hospital_id) == hospital_id,
                    col(Patient.uhid) == uhid.strip().upper(),
                    col(Patient.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )

    if patient is not None and patient.merged_into_id is not None:
        # Through `get_patient`, which walks a chain rather than one hop — a
        # record can be merged twice, and stopping at the first pointer would
        # land on another dead row.
        return await get_patient(session, patient.merged_into_id)
    return patient


async def get_patients_by_ids(
    session: AsyncSession,
    patient_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
) -> dict[uuid.UUID, Patient]:
    """Look up many patients at once, keyed by the id that was asked for.

    This exists so other modules can put patient *names* on their own lists
    without an N+1. A queue board, a ward census and an order worklist all
    hold patient ids and all need to render something a human recognises;
    fetching each one individually turns a twenty-row screen into twenty-one
    round trips, which is the difference between a queue that feels live and
    one staff stop trusting.

    It is on the service interface deliberately (CLAUDE.md §2): `scheduling`
    calls this rather than joining to the patients table, so the boundary
    survives `patients` being extracted later.

    Merges are *not* followed. The caller holds an id that appeared on its own
    row, and the honest answer is what that id points at — following the merge
    would silently show a different name than the record the row refers to.
    Callers wanting the surviving record should use `get_patient`.
    """
    if not patient_ids:
        # An empty `IN ()` is valid SQL in Postgres but still a round trip.
        return {}

    rows = (
        (
            await session.execute(
                select(Patient).where(
                    col(Patient.id).in_(set(patient_ids)),
                    col(Patient.hospital_id) == hospital_id,
                    col(Patient.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {patient.id: patient for patient in rows}


# A merge chain longer than this is a data problem, not a history to walk.
_MAX_MERGE_DEPTH = 10


async def record_ids_by_patient(
    session: AsyncSession, patient_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """For each record, every patient id whose history belongs to it — itself first.

    A merge keeps the duplicate row and points it at the survivor (see
    `merge_patients`), so the duplicate's visits, reports and invoices still
    carry the duplicate's id. A history that read only the survivor's id would
    quietly lose them — the split medical record a merge exists to repair.

    Walked level by level because merges chain: B folded into A, then A into C,
    leaves B's visits two pointers from C. One query per level for the whole
    batch, and real chains are one level deep.
    """
    groups: dict[uuid.UUID, list[uuid.UUID]] = {pid: [pid] for pid in patient_ids}
    root_of: dict[uuid.UUID, uuid.UUID] = {pid: pid for pid in patient_ids}
    frontier = list(groups)
    for _ in range(_MAX_MERGE_DEPTH):
        if not frontier:
            break
        rows = (
            await session.execute(
                select(Patient.id, Patient.merged_into_id).where(
                    col(Patient.merged_into_id).in_(frontier),
                    col(Patient.hospital_id) == hospital_id,
                    col(Patient.deleted_at).is_(None),
                )
            )
        ).all()
        frontier = []
        for child, parent in rows:
            if child in root_of or parent is None:
                continue
            root = root_of[parent]
            root_of[child] = root
            groups[root].append(child)
            frontier.append(child)
    return groups


async def record_ids(
    session: AsyncSession, patient_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every patient id whose history belongs to this one record, itself first."""
    return (await record_ids_by_patient(session, [patient_id], hospital_id=hospital_id))[patient_id]


async def list_patients(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    include_inactive: bool = False,
    gender: Gender | None = None,
    deceased: bool | None = None,
    unidentified: bool | None = None,
    registered_from: date | None = None,
    registered_to: date | None = None,
    sort: PatientSort = PatientSort.RECENT,
) -> tuple[list[Patient], int]:
    """The patient directory: every record the hospital holds, filterable.

    Registration dates are the hospital's calendar days, not UTC ones — "who
    registered on the 5th" must not lose the evening after 18:30 IST to the
    6th. Merged duplicates never appear: the surviving record carries their
    history, and listing both is the split record the merge removed.
    """
    filters: list[ColumnElement[bool]] = [
        col(Patient.hospital_id) == hospital_id,
        col(Patient.deleted_at).is_(None),
        col(Patient.merged_into_id).is_(None),
    ]
    if not include_inactive:
        filters.append(col(Patient.is_active).is_(True))
    if gender is not None:
        filters.append(col(Patient.gender) == gender)
    if deceased is not None:
        filters.append(col(Patient.is_deceased).is_(deceased))
    if unidentified is not None:
        filters.append(col(Patient.is_unidentified).is_(unidentified))
    if registered_from is not None or registered_to is not None:
        zone = await tenancy_service.local_zone(session, hospital_id)
        if registered_from is not None:
            filters.append(
                col(Patient.registered_at) >= datetime.combine(registered_from, time.min, zone)
            )
        if registered_to is not None:
            next_day = datetime.combine(registered_to + timedelta(days=1), time.min, zone)
            filters.append(col(Patient.registered_at) < next_day)

    # A unique tiebreaker on every ordering, so paging never skips or repeats
    # a row that shares a name or a registration instant with its neighbour.
    order_by = {
        PatientSort.RECENT: (col(Patient.registered_at).desc(), col(Patient.id)),
        PatientSort.NAME: (func.lower(col(Patient.full_name)), col(Patient.id)),
        PatientSort.UHID: (col(Patient.uhid), col(Patient.id)),
    }[sort]

    total = (
        await session.execute(select(func.count()).select_from(Patient).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Patient)
                .where(*filters)
                .order_by(*order_by)
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def search_patients(
    session: AsyncSession,
    query: str,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
) -> tuple[list[Patient], int]:
    """The universal search box (CLAUDE.md §7b): one field, any identifier.

    Resolves a UHID, a mobile number, or a name — reception should never have
    to decide which box to type into.

    A name search that finds nothing is retried with trigram similarity. Exact
    substring first because it is fast and precise, fuzzy only as a fallback:
    "I typed the name slightly wrong and got nothing" is a real dead end at a
    counter, and the patient standing there is not going to spell it better the
    second time.

    **Merged records are excluded.** `merge_patients` keeps the losing row and
    moves its safety alerts onto the survivor, so a search that returns the
    loser hands somebody a chart with no allergy banner on it — the exact
    failure that function exists to prevent. `find_duplicates` and
    `list_patients` have always filtered these out; this did not, which meant
    the one path a receptionist uses most was the one that could surface a
    stripped record. A scanned card makes it routine rather than theoretical:
    a card is precisely the thing that outlives a merge and keeps being
    presented afterwards.
    """
    term = query.strip()
    if not term:
        return [], 0

    base: list[ColumnElement[bool]] = [
        col(Patient.hospital_id) == hospital_id,
        col(Patient.deleted_at).is_(None),
        col(Patient.merged_into_id).is_(None),
    ]

    digits = normalize_phone(term)
    is_numeric = bool(digits) and len(digits) >= 4
    normalized = normalize_name(term)

    if is_numeric:
        match: ColumnElement[bool] = (
            col(Patient.phone).like(f"%{digits}")
            | col(Patient.alternate_phone).like(f"%{digits}")
            | col(Patient.guardian_phone).like(f"%{digits}")
            | col(Patient.uhid).ilike(f"%{term}%")
        )
    else:
        match = col(Patient.name_normalized).ilike(f"%{normalized}%") | col(Patient.uhid).ilike(
            f"%{term}%"
        )

    async def run(condition: ColumnElement[bool]) -> tuple[list[Patient], int]:
        filters = [*base, condition]
        found = (
            await session.execute(select(func.count()).select_from(Patient).where(*filters))
        ).scalar_one()
        page = (
            (
                await session.execute(
                    select(Patient)
                    .where(*filters)
                    .order_by(col(Patient.registered_at).desc())
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
        return list(page), found

    rows, total = await run(match)

    if not rows and not is_numeric and normalized:
        similarity = func.similarity(col(Patient.name_normalized), normalized)
        rows, total = await run(similarity >= NAME_SIMILARITY_THRESHOLD)

    return rows, total


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
async def update_patient(
    session: AsyncSession, patient: Patient, payload: PatientUpdate
) -> Patient:
    """Apply a partial update, re-checking identity if name or phone changed."""
    changes = payload.model_dump(exclude_unset=True)

    new_name = changes.get("full_name")
    new_phone = changes.get("phone")
    if patient.is_unidentified and (new_name is not None or new_phone is not None):
        # Naming the patient here would leave the record with a real name and
        # the unidentified flag still set — off the duplicate checks and with
        # every message held, for good. Identification is its own action.
        raise ValidationError(
            "This patient has not been identified yet. Use Identify to record who they are.",
            code="identify_first",
        )
    if new_name is not None or new_phone is not None:
        candidate_name = new_name or patient.full_name
        candidate_phone = new_phone or patient.phone or ""
        clashes = await find_duplicates(
            session,
            hospital_id=patient.hospital_id,
            full_name=candidate_name,
            phone=candidate_phone,
            exclude_id=patient.id,
        )
        if any(match.is_exact for match in clashes):
            raise ConflictError(
                "Another patient already uses that name and mobile number.",
                code="patient_identity_collision",
            )

    if "age_years" in changes or "birth_date" in changes:
        birth_date, estimated = _birth_date_from(
            changes.pop("age_years", None), changes.get("birth_date")
        )
        if birth_date is not None:
            changes["birth_date"] = birth_date
            patient.birth_date_is_estimated = estimated
    changes.pop("age_years", None)

    for field, value in changes.items():
        setattr(patient, field, value)

    if new_name is not None:
        patient.name_normalized = normalize_name(new_name)
    if new_phone is not None:
        patient.phone = normalize_phone(new_phone)
    for phone_field in ("alternate_phone", "guardian_phone"):
        raw = changes.get(phone_field)
        if raw:
            setattr(patient, phone_field, normalize_phone(raw))

    patient.updated_at = utc_now()
    session.add(patient)
    await session.flush()
    await session.refresh(patient)
    return patient


# ---------------------------------------------------------------------------
# Safety alerts (CLAUDE.md §7b — the persistent banner)
# ---------------------------------------------------------------------------
async def record_alert(
    session: AsyncSession,
    patient: Patient,
    payload: AlertCreate,
    *,
    recorded_by_id: uuid.UUID | None = None,
) -> PatientAlert:
    alert = PatientAlert(
        hospital_id=patient.hospital_id,
        patient_id=patient.id,
        alert_type=payload.alert_type,
        severity=payload.severity,
        label=payload.label.strip(),
        detail=payload.detail,
        recorded_by_id=recorded_by_id,
    )
    session.add(alert)
    await session.flush()
    await session.refresh(alert)
    return alert


async def get_patient_alerts(
    session: AsyncSession, patient_id: uuid.UUID, *, active_only: bool = True
) -> list[PatientAlert]:
    """Alerts for the safety banner, most severe first."""
    filters: list[ColumnElement[bool]] = [
        col(PatientAlert.patient_id) == patient_id,
        col(PatientAlert.deleted_at).is_(None),
    ]
    if active_only:
        filters.append(col(PatientAlert.is_active).is_(True))

    # Criticals first: the banner is read at a glance and the top line is the
    # one that gets seen.
    severity_rank = case(
        (col(PatientAlert.severity) == AlertSeverity.CRITICAL, 0),
        (col(PatientAlert.severity) == AlertSeverity.HIGH, 1),
        (col(PatientAlert.severity) == AlertSeverity.MODERATE, 2),
        else_=3,
    )
    rows = (
        (
            await session.execute(
                select(PatientAlert).where(*filters).order_by(severity_rank, PatientAlert.label)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Drug allergies against a drug (SAFE-01)
# ---------------------------------------------------------------------------
async def drug_allergy_conflicts(
    session: AsyncSession, patient_id: uuid.UUID, drug_names: Iterable[str | None]
) -> list[PatientAlert]:
    """The patient's active drug allergies that any of `drug_names` clashes with.

    Through the merge, deliberately. A stay or a visit keeps the patient id it
    was opened on, and if that record has since lost a merge its allergies
    moved to the survivor — reading the old id would find none, and the check
    would pass for exactly the patient whose record was the most confused.
    """
    patient = await get_patient(session, patient_id)
    return clashing_allergies(await get_patient_alerts(session, patient.id), drug_names)


def clashing_allergies(
    alerts: Iterable[PatientAlert], drug_names: Iterable[str | None]
) -> list[PatientAlert]:
    """Of `alerts` (already loaded), the drug allergies the drug clashes with."""
    names = [name for name in drug_names if name]
    return [
        alert
        for alert in alerts
        if alert.alert_type is AlertType.DRUG_ALLERGY and allergy_matches(alert.label, names)
    ]


def allergy_details(alerts: Iterable[PatientAlert]) -> list[dict[str, str | None]]:
    """The allergies a refusal names, in the shape the client renders."""
    return [
        {
            "id": str(alert.id),
            "label": alert.label,
            "severity": alert.severity.value,
            "detail": alert.detail,
        }
        for alert in alerts
    ]


def assert_no_allergy_clash(clashes: list[PatientAlert], *, override_reason: str | None) -> None:
    """Refuse a drug that clashes with a recorded drug allergy, unless the
    prescriber has said why it is being given anyway (SAFE-01).

    A 409 rather than a warning the client may or may not show: the API is the
    one place every route to a prescription passes through. The details name
    each allergy, so the override is a decision made with them in view.
    """
    if clashes and not override_reason:
        raise ConflictError(
            "This drug clashes with the patient's recorded drug allergy: "
            + ", ".join(alert.label for alert in clashes)
            + ". Give a reason to prescribe it anyway.",
            code="allergy_conflict",
            details={"allergies": allergy_details(clashes)},
        )


async def drug_allergies_by_patient(
    session: AsyncSession,
    patient_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
    records: Mapping[uuid.UUID, Patient] | None = None,
) -> dict[uuid.UUID, list[PatientAlert]]:
    """Active drug allergies for many patients at once, keyed by the id asked for.

    For the drug round, which shows every patient's allergies beside their
    doses: one query for the round rather than one per row. Merges are
    followed for the same reason as in `drug_allergy_conflicts`.

    `records` are patients the caller already holds (from `get_patients_by_ids`),
    so a screen that has fetched them does not fetch them twice.
    """
    if not patient_ids:
        return {}
    if records is None:
        records = await get_patients_by_ids(session, patient_ids, hospital_id=hospital_id)
    survivor_of: dict[uuid.UUID, uuid.UUID] = {}
    for patient_id in patient_ids:
        record = records.get(patient_id)
        if record is None:
            continue
        if record.merged_into_id is not None:
            # Rare: a stay on a record that lost a merge. Walked one by one.
            record = await get_patient(session, record.id)
        survivor_of[patient_id] = record.id

    rows = (
        (
            await session.execute(
                select(PatientAlert)
                .where(
                    col(PatientAlert.patient_id).in_(set(survivor_of.values())),
                    col(PatientAlert.alert_type) == AlertType.DRUG_ALLERGY,
                    col(PatientAlert.is_active).is_(True),
                    col(PatientAlert.deleted_at).is_(None),
                )
                .order_by(PatientAlert.label)
            )
        )
        .scalars()
        .all()
    )
    by_survivor: dict[uuid.UUID, list[PatientAlert]] = {}
    for alert in rows:
        by_survivor.setdefault(alert.patient_id, []).append(alert)
    return {
        patient_id: by_survivor.get(survivor, []) for patient_id, survivor in survivor_of.items()
    }


async def resolve_alert(session: AsyncSession, alert: PatientAlert, *, reason: str) -> PatientAlert:
    """Retire an alert. Never a delete — "no longer believed" and "never
    recorded" are clinically different statements."""
    alert.is_active = False
    alert.resolved_at = utc_now()
    alert.resolved_reason = reason
    alert.updated_at = utc_now()
    session.add(alert)
    await session.flush()
    await session.refresh(alert)
    return alert


# ---------------------------------------------------------------------------
# Consent (CLAUDE.md §12 / DPDP Act 2023)
# ---------------------------------------------------------------------------
async def record_consent(
    session: AsyncSession,
    patient: Patient,
    payload: ConsentCreate,
    *,
    recorded_by_id: uuid.UUID | None = None,
) -> PatientConsent:
    consent = PatientConsent(
        hospital_id=patient.hospital_id,
        patient_id=patient.id,
        consent_type=payload.consent_type,
        granted=payload.granted,
        method=payload.method,
        given_by=payload.given_by,
        given_by_name=payload.given_by_name,
        given_by_relation=payload.given_by_relation,
        language=payload.language,
        document_url=payload.document_url,
        recorded_by_id=recorded_by_id,
        granted_at=utc_now(),
        expires_at=payload.expires_at,
    )
    session.add(consent)
    await session.flush()
    await session.refresh(consent)
    return consent


async def get_consents(session: AsyncSession, patient_id: uuid.UUID) -> list[PatientConsent]:
    rows = (
        (
            await session.execute(
                select(PatientConsent)
                .where(PatientConsent.patient_id == patient_id)
                .order_by(col(PatientConsent.granted_at).desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def has_active_consent(
    session: AsyncSession, patient_id: uuid.UUID, consent_type: ConsentType
) -> bool:
    """Whether a live, unwithdrawn, unexpired consent of this type exists.

    The question other modules ask before sharing or processing data — which is
    why it lives behind the service interface rather than being re-derived from
    the table by each caller.
    """
    now = utc_now()
    consent = (
        (
            await session.execute(
                select(PatientConsent)
                .where(
                    PatientConsent.patient_id == patient_id,
                    PatientConsent.consent_type == consent_type,
                    col(PatientConsent.granted).is_(True),
                    col(PatientConsent.withdrawn_at).is_(None),
                )
                .order_by(col(PatientConsent.granted_at).desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if consent is None:
        return False
    return consent.expires_at is None or consent.expires_at > now


async def withdraw_consent(
    session: AsyncSession, consent: PatientConsent, *, reason: str
) -> PatientConsent:
    consent.withdrawn_at = utc_now()
    consent.withdrawn_reason = reason
    consent.updated_at = utc_now()
    session.add(consent)
    await session.flush()
    await session.refresh(consent)
    return consent


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
async def add_identifier(
    session: AsyncSession, patient: Patient, payload: IdentifierCreate
) -> PatientIdentifier:
    existing = (
        (
            await session.execute(
                select(PatientIdentifier).where(
                    PatientIdentifier.hospital_id == patient.hospital_id,
                    col(PatientIdentifier.identifier_type) == payload.identifier_type.upper(),
                    col(PatientIdentifier.identifier_value) == payload.identifier_value.strip(),
                    col(PatientIdentifier.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )

    if existing is not None:
        if existing.patient_id == patient.id:
            return existing
        raise ConflictError(
            "That identifier is already recorded against a different patient.",
            code="identifier_taken",
            details={"patient_id": str(existing.patient_id)},
        )

    identifier = PatientIdentifier(
        hospital_id=patient.hospital_id,
        patient_id=patient.id,
        identifier_type=payload.identifier_type.upper(),
        identifier_value=payload.identifier_value.strip(),
        issued_by=payload.issued_by,
        valid_until=payload.valid_until,
    )
    session.add(identifier)
    await session.flush()
    await session.refresh(identifier)
    return identifier


async def get_identifiers(session: AsyncSession, patient_id: uuid.UUID) -> list[PatientIdentifier]:
    rows = (
        (
            await session.execute(
                select(PatientIdentifier).where(
                    PatientIdentifier.patient_id == patient_id,
                    col(PatientIdentifier.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
async def merge_patients(
    session: AsyncSession,
    *,
    survivor: Patient,
    duplicate: Patient,
    reason: str,
) -> Patient:
    """Fold `duplicate` into `survivor`.

    The duplicate row is kept and pointed at the survivor rather than deleted:
    encounters, invoices and printed cards already reference its id, and reads
    follow the pointer. A merge is therefore reversible in principle and always
    explicable — which matters when the two records turn out to have been
    different people after all.

    Safety alerts move across. An allergy recorded on the record that loses a
    merge must not disappear from the banner; that is the failure mode this
    whole function exists to prevent.

    An unidentified emergency record is refused on either side: it is named
    through `identify_patient`, which decides which record survives and
    carries the identity across. A plain merge would leave a placeholder name
    on the live visit, or a real one with the unidentified flag still set.
    """
    if survivor.is_unidentified or duplicate.is_unidentified:
        raise ValidationError(
            "An unidentified emergency patient is linked to a record with Identify, not Merge.",
            code="use_identify",
        )
    return await _fold(session, survivor=survivor, duplicate=duplicate, reason=reason)


async def _fold(
    session: AsyncSession,
    *,
    survivor: Patient,
    duplicate: Patient,
    reason: str,
) -> Patient:
    """The merge itself, without the unidentified-record guard."""
    if survivor.id == duplicate.id:
        raise ValidationError("A patient cannot be merged into itself.", code="invalid_merge")
    if survivor.hospital_id != duplicate.hospital_id:
        raise ValidationError(
            "Patients from different hospitals cannot be merged.", code="cross_tenant_merge"
        )
    if duplicate.merged_into_id is not None:
        raise ConflictError("That record has already been merged.", code="already_merged")
    if survivor.merged_into_id is not None:
        raise ValidationError(
            "The surviving record has itself been merged elsewhere.", code="invalid_merge"
        )

    alerts = (
        (
            await session.execute(
                select(PatientAlert).where(
                    PatientAlert.patient_id == duplicate.id,
                    col(PatientAlert.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for alert in alerts:
        alert.patient_id = survivor.id
        session.add(alert)

    for table in (PatientConsent, PatientIdentifier):
        rows = (
            (await session.execute(select(table).where(col(table.patient_id) == duplicate.id)))
            .scalars()
            .all()
        )
        for row in rows:
            row.patient_id = survivor.id
            session.add(row)

    # Fill blanks on the survivor from the duplicate rather than overwriting:
    # the duplicate may hold the only address anyone ever captured.
    for field in (
        "alternate_phone",
        "email",
        "guardian_name",
        "guardian_relation",
        "guardian_phone",
        "address_line1",
        "address_line2",
        "city",
        "district",
        "state",
        "pincode",
        "abha_id",
        "abha_address",
        "occupation",
        "marital_status",
    ):
        if getattr(survivor, field, None) in (None, "") and getattr(duplicate, field, None):
            setattr(survivor, field, getattr(duplicate, field))

    if survivor.blood_group.value == "UNKNOWN" and duplicate.blood_group.value != "UNKNOWN":
        survivor.blood_group = duplicate.blood_group
    if survivor.birth_date is None and duplicate.birth_date is not None:
        survivor.birth_date = duplicate.birth_date
        survivor.birth_date_is_estimated = duplicate.birth_date_is_estimated
    # A recorded death outranks silence on either record.
    if duplicate.is_deceased and not survivor.is_deceased:
        survivor.is_deceased = True
        survivor.deceased_at = duplicate.deceased_at

    duplicate.merged_into_id = survivor.id
    duplicate.merged_at = utc_now()
    duplicate.is_active = False
    duplicate.updated_at = utc_now()
    survivor.updated_at = utc_now()

    session.add(duplicate)
    session.add(survivor)
    await session.flush()
    await session.refresh(survivor)

    logger.info("merged patient %s into %s (%s)", duplicate.uhid, survivor.uhid, reason)
    return survivor


# ---------------------------------------------------------------------------
# Emergency arrivals nobody can name (UX-01)
# ---------------------------------------------------------------------------
async def register_unidentified(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    gender: Gender,
    age_years: int | None = None,
) -> Patient:
    """Register a patient nobody can name: `Unknown Male EM-0034`, no mobile.

    The honest alternative to the name and number casualty would otherwise
    invent. The record is real from the first minute — a UHID for the
    wristband, the samples and the bill — and says plainly that its identity
    is not known, which is what keeps it out of duplicate matching, holds every
    patient message, and puts it on reception's "needs identification" list.
    """
    tag = await allocate_emergency_tag(session, hospital_id)
    full_name = f"{_UNKNOWN_NAME.get(gender, 'Unknown Patient')} {tag}"
    birth_date, estimated = _birth_date_from(age_years, None)

    patient = Patient(
        hospital_id=hospital_id,
        uhid=await allocate_uhid(session, hospital_id),
        full_name=full_name,
        name_normalized=normalize_name(full_name),
        phone=None,
        gender=gender,
        birth_date=birth_date,
        birth_date_is_estimated=estimated,
        is_unidentified=True,
        emergency_tag=tag,
        registered_at=utc_now(),
    )
    session.add(patient)
    await session.flush()
    await session.refresh(patient)
    return patient


async def identify_patient(
    session: AsyncSession,
    patient: Patient,
    payload: PatientIdentify,
    *,
    hospital_id: uuid.UUID,
) -> Patient:
    """Say who an unidentified emergency patient is. Happens once.

    **The emergency record always survives.** The live visit, the bed, the
    bill and the lab samples all point at it already; keeping it means none of
    them has to move, so none can be left behind pointing at the wrong record.

    * A new person: the four registration fields are written onto it.
    * Somebody already registered: their old record folds into this one, with
      the same mechanics as a merge — allergies, consents and identifiers move
      across, blanks are filled, and the old card still opens the patient — and
      the old record's name, mobile, gender and birth date become this one's.
    """
    if not patient.is_unidentified:
        raise ConflictError(
            f"{patient.full_name} has already been identified.", code="already_identified"
        )

    if payload.existing_patient_id is not None:
        existing = await _identity_record(
            session, payload.existing_patient_id, patient=patient, hospital_id=hospital_id
        )
        # Folded first, so the old record has left the live (name, mobile)
        # index before this one takes the same pair.
        await _fold(
            session,
            survivor=patient,
            duplicate=existing,
            reason=f"Identified emergency patient {patient.emergency_tag} as {existing.uhid}",
        )
        patient.full_name = existing.full_name
        patient.name_normalized = existing.name_normalized
        patient.phone = existing.phone
        patient.gender = existing.gender
        patient.birth_date = existing.birth_date
        patient.birth_date_is_estimated = existing.birth_date_is_estimated
        patient.preferred_language = existing.preferred_language
    else:
        # The schema guarantees all four; narrowed here for the type checker.
        assert payload.full_name and payload.phone and payload.gender
        clashes = await find_duplicates(
            session,
            hospital_id=hospital_id,
            full_name=payload.full_name,
            phone=payload.phone,
            exclude_id=patient.id,
        )
        exact = next((match.patient for match in clashes if match.is_exact), None)
        if exact is not None:
            # The client turns this into "This is them — link the records".
            raise ConflictError(
                f"{exact.full_name} is already registered on this mobile number "
                f"(UHID {exact.uhid}).",
                code="patient_already_exists",
                details={
                    "patient_id": str(exact.id),
                    "uhid": exact.uhid,
                    "full_name": exact.full_name,
                    "resolution": "link_existing",
                },
            )
        birth_date, estimated = _birth_date_from(payload.age_years, payload.birth_date)
        patient.full_name = payload.full_name.strip()
        patient.name_normalized = normalize_name(payload.full_name)
        patient.phone = normalize_phone(payload.phone)
        patient.gender = payload.gender
        patient.birth_date = birth_date
        patient.birth_date_is_estimated = estimated

    patient.is_unidentified = False
    patient.identified_at = utc_now()
    patient.updated_at = utc_now()
    session.add(patient)
    await session.flush()
    await session.refresh(patient)
    logger.info("identified emergency patient %s (%s)", patient.emergency_tag, patient.uhid)
    return patient


async def _identity_record(
    session: AsyncSession,
    patient_id: uuid.UUID,
    *,
    patient: Patient,
    hospital_id: uuid.UUID,
) -> Patient:
    """The existing record an unknown patient is being identified as — if it
    can honestly be one."""
    existing = await get_patient(session, patient_id, follow_merge=False)
    if (
        existing.id == patient.id
        or existing.hospital_id != hospital_id
        or existing.merged_into_id is not None
        or existing.is_unidentified
    ):
        raise ValidationError(
            "That record cannot be this patient's identity.",
            code="invalid_identity",
        )
    if existing.is_deceased:
        # The fold carries a death across. Linking a dead record would mark
        # the patient alive in casualty as dead, and silence every message.
        raise ConflictError(
            f"{existing.full_name} ({existing.uhid}) is recorded as deceased.",
            code="patient_deceased",
        )
    return existing


# ---------------------------------------------------------------------------
# Who sees which number, and who reads how many patients (SEC-01)
# ---------------------------------------------------------------------------
def mask_phone(phone: str | None) -> str | None:
    """`9812345670` -> `98•••••670`: enough to confirm with the patient in
    front of you, not enough to ring them later."""
    if not phone:
        return phone
    if len(phone) < 6:
        return "•" * len(phone)
    return f"{phone[:2]}•••••{phone[-3:]}"


# Two hours, so a key outlives the hour it counts by a margin.
_READ_WINDOW_SECONDS = 2 * 60 * 60


async def note_patient_reads(
    *, hospital_id: uuid.UUID, user_id: uuid.UUID, patient_ids: Collection[uuid.UUID]
) -> int | None:
    """Count the distinct patients this person has read this hour.

    Returns the count the *first* time it reaches `PATIENT_BULK_READ_ALERT` in
    the hour, and None otherwise — so one bulk read raises one alert, not one
    per page. A Redis set per person per clock hour, because the question is
    "how many different people", and re-opening one record all afternoon is
    normal work.

    Never in the way of the read itself: if Redis is down the count is
    skipped and logged, and the patient record still opens. The audit log
    still has every page.
    """
    if not patient_ids:
        return None
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    key = f"patient-reads:{hospital_id}:{user_id}:{hour}"
    try:
        redis = get_redis()
        # One round trip, applied together: add, keep the key alive, count.
        async with redis.pipeline(transaction=True) as pipe:
            pipe.sadd(key, *(str(patient_id) for patient_id in patient_ids))
            pipe.expire(key, _READ_WINDOW_SECONDS)
            pipe.scard(key)
            _, _, scard = await pipe.execute()
        count = int(scard)
        if count < settings.PATIENT_BULK_READ_ALERT:
            return None
        first = await redis.set(f"{key}:alerted", "1", nx=True, ex=_READ_WINDOW_SECONDS)
        return count if first else None
    except (RedisError, OSError):
        logger.warning("could not count patient reads for %s", user_id, exc_info=True)
        return None


async def mark_deceased(
    session: AsyncSession, patient: Patient, *, occurred_at: datetime
) -> Patient:
    """Flag the patient record as deceased.

    Called by `clinical` when a death is recorded on an encounter. Denormalised
    onto the patient so the notification dispatcher can answer "is this person
    alive?" from one row — CLAUDE.md §14 makes never messaging a deceased
    patient an invariant, and an invariant that depends on remembering to join
    is not an invariant.
    """
    patient.is_deceased = True
    patient.deceased_at = occurred_at
    patient.is_active = False
    patient.updated_at = utc_now()
    session.add(patient)
    await session.flush()
    await session.refresh(patient)
    return patient


def active_alert_summary(alerts: list[PatientAlert]) -> list[str]:
    """Compact banner labels, criticals first."""
    ordered = sorted(
        (alert for alert in alerts if alert.is_active),
        key=lambda alert: 0 if alert.severity == AlertSeverity.CRITICAL else 1,
    )
    return [f"{alert.alert_type.value}: {alert.label}" for alert in ordered]
