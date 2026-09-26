"""In-process domain event bus — the module extraction seam (CLAUDE.md §2).

Modules must not reach into each other's tables. Where a module needs to react
to something that happened elsewhere (billing capturing a charge when a lab
order completes), it subscribes to an event instead of being called directly.

Why this matters beyond tidiness: this bus is the boundary along which billing
and notifications get lifted into their own services later. Publishing today is
an in-process fan-out; swapping the dispatcher for Redis Streams or a broker is
a change to `EventBus.publish` alone, because nothing else knows how delivery
happens.

---------------------------------------------------------------------------
Two channels, because "a subscriber" is not one kind of thing
---------------------------------------------------------------------------

`subscribe` is **fire-and-forget**. Handlers run concurrently, get no database
session, and an exception in one is logged and swallowed. That is right for a
subscriber with its own failure domain — an SMS gateway, a webhook, a cache.
A flaky WhatsApp provider must not fail a patient registration.

`subscribe_transactional` is the opposite, and it exists because Phase 7
(`billing`) needed something the first channel cannot safely provide. Its
handlers receive the publisher's `AsyncSession`, run **inside the publisher's
transaction**, sequentially, and their exceptions **propagate**.

The reasoning is worth stating, because "swallow the error" looks like the safe
choice and here it is the dangerous one. A handler that writes a row to the same
database, in the same transaction, has **no independent failure domain**. There
is no flaky third party to protect the publisher from: if that INSERT fails, the
publisher's own INSERT was going to fail too. Swallowing the exception buys
nothing and loses the row — and the row in question is a charge for care that
was actually delivered. Unbilled care is silent revenue loss, and it is silent
precisely because nobody gets an error.

So the contract for a transactional handler is strict:

  * It must be **total with respect to configuration.** Missing rate card, no
    price for the item, catalogue never set up — none of these may raise. Record
    the fact and flag it for a human (see `billing.service.capture_charge`,
    which captures at zero with `needs_pricing` rather than refusing). A
    hospital that has not finished configuring billing must still be able to
    place a clinical order.
  * It may only fail the way the publisher's own write would fail: a genuine
    database error. Then rolling the whole thing back is correct.

Handlers run sequentially rather than concurrently for a mundane but absolute
reason: `AsyncSession` is not safe for concurrent use, and they all share one.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True, slots=True)
class DomainEvent:
    """Base class for every domain event.

    Subclasses add their own payload fields and are published by name, so the
    event type is part of a module's public contract — treat it like an API.
    """

    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=utc_now)
    # Tenant that the event belongs to. None only for platform-level events.
    hospital_id: uuid.UUID | None = None
    # Who caused it — carried through to the audit log (CLAUDE.md §8).
    actor_id: uuid.UUID | None = None

    @property
    def name(self) -> str:
        return type(self).__name__


EventT = TypeVar("EventT", bound=DomainEvent)
Handler = Callable[[Any], Awaitable[None] | None]
TransactionalHandler = Callable[[Any, AsyncSession], Awaitable[None]]


class MissingSessionError(RuntimeError):
    """A transactional subscriber exists but `publish` was given no session.

    A programming error, not a runtime condition: the publish site has to be
    fixed. Raised rather than logged because the alternative is dropping a
    charge quietly, which is the exact failure this channel exists to prevent.
    """


class EventBus:
    """Synchronous-registration, two-channel dispatch."""

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = defaultdict(list)
        self._transactional: dict[type[DomainEvent], list[TransactionalHandler]] = defaultdict(list)

    # --- registration ------------------------------------------------------
    def subscribe(self, event_type: type[EventT], handler: Handler) -> None:
        """Fire-and-forget. Failures are logged and swallowed."""
        self._handlers[event_type].append(handler)
        logger.debug(
            "subscribed %s -> %s",
            event_type.__name__,
            getattr(handler, "__name__", handler),
        )

    def subscribe_transactional(
        self, event_type: type[EventT], handler: TransactionalHandler
    ) -> None:
        """Runs inside the publisher's transaction. Failures propagate.

        For subscribers that write to the same database as the publisher and
        whose work must not be lost. See the module docstring for the contract
        this places on the handler.
        """
        self._transactional[event_type].append(handler)
        logger.debug(
            "subscribed (transactional) %s -> %s",
            event_type.__name__,
            getattr(handler, "__name__", handler),
        )

    def on(self, event_type: type[EventT]) -> Callable[[Handler], Handler]:
        """Decorator form: `@bus.on(LabResultReady)`."""

        def decorator(handler: Handler) -> Handler:
            self.subscribe(event_type, handler)
            return handler

        return decorator

    def on_transactional(
        self, event_type: type[EventT]
    ) -> Callable[[TransactionalHandler], TransactionalHandler]:
        """Decorator form: `@bus.on_transactional(OrderPlaced)`."""

        def decorator(handler: TransactionalHandler) -> TransactionalHandler:
            self.subscribe_transactional(event_type, handler)
            return handler

        return decorator

    # --- dispatch ----------------------------------------------------------
    def _matching[T](
        self, registry: dict[type[DomainEvent], list[T]], event: DomainEvent
    ) -> list[T]:
        # Walk the MRO so a handler on a base event type sees subclasses too.
        matched: list[T] = []
        for klass in type(event).__mro__:
            if klass in registry:
                matched.extend(registry[klass])
        return matched

    async def publish(self, event: DomainEvent, *, session: AsyncSession | None = None) -> None:
        """Deliver an event to both channels.

        Transactional subscribers go first, sequentially, sharing `session` and
        free to raise. Fire-and-forget subscribers follow, concurrently, with
        their failures isolated.
        """
        transactional = self._matching(self._transactional, event)
        if transactional:
            if session is None:
                raise MissingSessionError(
                    f"{event.name} has {len(transactional)} transactional subscriber(s) but was "
                    "published without a session. Pass session=... at the publish site."
                )
            logger.debug(
                "publishing %s to %d transactional handler(s)", event.name, len(transactional)
            )
            for handler in transactional:
                # Deliberately unguarded. A failure here means the publisher's
                # own transaction should not commit either.
                await handler(event, session)

        handlers = self._matching(self._handlers, event)
        if not handlers:
            if not transactional:
                logger.debug("event %s published with no subscribers", event.name)
            return

        async def _invoke(handler: Handler) -> None:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "event handler failed event=%s handler=%s event_id=%s",
                    event.name,
                    getattr(handler, "__name__", handler),
                    event.event_id,
                )

        logger.debug("publishing %s to %d handler(s)", event.name, len(handlers))
        await asyncio.gather(*(_invoke(handler) for handler in handlers))

    def clear(self) -> None:
        """Drop all subscriptions. Tests only."""
        self._handlers.clear()
        self._transactional.clear()


# Application-wide bus. Modules register their handlers at import time.
event_bus = EventBus()
