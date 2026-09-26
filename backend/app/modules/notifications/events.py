"""Domain events published by `notifications`.

Thin, like every other module's. This module is mostly a *consumer* of events —
it is the end of most chains rather than the middle of one — so the list is
short, and the two that matter are the ones that admit a problem:

* `NotificationFailed` — every channel refused. Somebody has to phone the
  patient, and nothing will tell them unless this surfaces.
* `NotificationTemplateMissing` — the direct analogue of billing's
  `ChargeNeedsPricing`. A hospital sending bare fallback copy to its patients is
  a hospital with a gap in its configuration, and gaps that only appear in a
  boolean column are gaps nobody looks at.

`reporting` (Phase 10) is the intended consumer of all of these.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.events import DomainEvent

__all__ = [
    "NotificationFailed",
    "NotificationSent",
    "NotificationSuppressed",
    "NotificationTemplateMissing",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class NotificationSent(DomainEvent):
    notification_id: uuid.UUID
    patient_id: uuid.UUID | None
    template_code: str
    channel: str
    attempts: int


@dataclass(frozen=True, kw_only=True, slots=True)
class NotificationFailed(DomainEvent):
    """Every channel in the ladder refused it.

    Carries `channels_tried` so the person picking it up knows whether this is a
    bad number (all channels failed on the same address) or a provider outage
    (WhatsApp failed for everybody this afternoon).
    """

    notification_id: uuid.UUID
    patient_id: uuid.UUID | None
    template_code: str
    channels_tried: tuple[str, ...]
    last_error: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class NotificationSuppressed(DomainEvent):
    """A message was deliberately not sent.

    Published rather than merely recorded because a suppression is sometimes the
    system working (a death) and sometimes a hospital losing contact with a
    patient who opted out of everything (a retention problem). The two look
    identical in a status column and different in a report.
    """

    notification_id: uuid.UUID
    patient_id: uuid.UUID | None
    template_code: str
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class NotificationTemplateMissing(DomainEvent):
    """A message went out on fallback copy because nothing was configured."""

    notification_id: uuid.UUID
    template_code: str
    language: str
    channel: str
