"""Is this visit an IPD stay? Billing asks `ipd`, without importing it.

`ipd.service` already imports `billing.service` — bed-days and medicines are
charged through it — so billing importing `ipd` back would be a cycle. The
answer comes through the same seam `clinical` uses for its closure gate: `ipd`
registers a provider at startup (`ipd/handlers.py`), and billing calls it.

The question matters because an IPD bill has rules an OPD bill does not: only
the accountant and the hospital admin may change it, reception's one-tap Paid
may not issue it, and it holds only what was charged from the admission on.

With no provider registered — a billing test that never wires `ipd` — every
visit reads as OPD, which is the behaviour billing had before stays existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["StayInfo", "StayProvider", "register_stay_provider", "stay_for", "stays_for"]


@dataclass(frozen=True, slots=True)
class StayInfo:
    """What billing needs to know about an admission, and nothing clinical."""

    admission_id: uuid.UUID
    admission_number: str
    # ADMITTED | DISCHARGE_INITIATED | DISCHARGED — never CANCELLED: a
    # cancelled admission is not a stay, and the provider leaves it out.
    status: str
    admitted_at: datetime
    discharged_at: datetime | None
    ward_bed: str | None
    attending_doctor: str | None


StayProvider = Callable[
    [AsyncSession, Collection[uuid.UUID], uuid.UUID], Awaitable[dict[uuid.UUID, StayInfo]]
]

_provider: StayProvider | None = None


def register_stay_provider(provider: StayProvider) -> None:
    """Called once by `ipd` at startup. Re-registering replaces, never stacks."""
    global _provider
    _provider = provider


async def stays_for(
    session: AsyncSession, encounter_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, StayInfo]:
    """The stays among these visits, keyed by encounter — one lookup for a list."""
    if _provider is None or not encounter_ids:
        return {}
    return await _provider(session, encounter_ids, hospital_id)


async def stay_for(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> StayInfo | None:
    return (await stays_for(session, [encounter_id], hospital_id=hospital_id)).get(encounter_id)
