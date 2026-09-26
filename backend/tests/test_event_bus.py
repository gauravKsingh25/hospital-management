"""The event bus, and specifically its two channels.

The distinction these tests pin down is the one Phase 7 added and the one most
likely to be undone by someone tidying up later: fire-and-forget subscribers
have their failures swallowed, and transactional subscribers do not.

Swallowing is right for a handler with its own failure domain — a flaky SMS
gateway must not fail a patient registration. It is wrong for a handler writing
a row to the same database in the same transaction, because there is no
independent failure to absorb and the row it drops is a charge for care that was
delivered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.core.events import DomainEvent, EventBus, MissingSessionError


@dataclass(frozen=True, kw_only=True, slots=True)
class Thing(DomainEvent):
    label: str


@dataclass(frozen=True, kw_only=True, slots=True)
class SpecificThing(Thing):
    extra: str


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


# ---------------------------------------------------------------------------
# Fire-and-forget
# ---------------------------------------------------------------------------
async def test_a_failing_handler_does_not_starve_its_peers(bus: EventBus) -> None:
    seen: list[str] = []

    async def explodes(_: Thing) -> None:
        raise RuntimeError("the SMS gateway is down")

    async def records(event: Thing) -> None:
        seen.append(event.label)

    bus.subscribe(Thing, explodes)
    bus.subscribe(Thing, records)

    await bus.publish(Thing(label="registration"))

    assert seen == ["registration"]


async def test_a_synchronous_handler_is_accepted(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe(Thing, lambda event: seen.append(event.label))

    await bus.publish(Thing(label="sync"))

    assert seen == ["sync"]


async def test_a_handler_on_a_base_event_sees_its_subclasses(bus: EventBus) -> None:
    seen: list[str] = []

    async def records(event: Thing) -> None:
        seen.append(event.label)

    bus.subscribe(Thing, records)
    await bus.publish(SpecificThing(label="derived", extra="x"))

    assert seen == ["derived"]


async def test_publishing_with_no_subscribers_is_harmless(bus: EventBus) -> None:
    await bus.publish(Thing(label="nobody is listening"))


# ---------------------------------------------------------------------------
# Transactional
# ---------------------------------------------------------------------------
async def test_a_transactional_handler_receives_the_session(bus: EventBus) -> None:
    seen: list[object] = []
    sentinel = object()

    async def records(_: Thing, session: object) -> None:
        seen.append(session)

    bus.subscribe_transactional(Thing, records)
    await bus.publish(Thing(label="charge"), session=sentinel)  # type: ignore[arg-type]

    assert seen == [sentinel]


async def test_a_failing_transactional_handler_propagates(bus: EventBus) -> None:
    """The whole point. A charge that cannot be written must fail the act that
    would have been billed, because they are the same transaction anyway."""

    async def explodes(_: Thing, __: object) -> None:
        raise RuntimeError("constraint violation")

    bus.subscribe_transactional(Thing, explodes)

    with pytest.raises(RuntimeError, match="constraint violation"):
        await bus.publish(Thing(label="charge"), session=object())  # type: ignore[arg-type]


async def test_transactional_handlers_run_in_registration_order(bus: EventBus) -> None:
    """Sequentially, not concurrently: `AsyncSession` is not safe for concurrent
    use and they all share one."""
    order: list[int] = []

    async def first(_: Thing, __: object) -> None:
        order.append(1)

    async def second(_: Thing, __: object) -> None:
        order.append(2)

    bus.subscribe_transactional(Thing, first)
    bus.subscribe_transactional(Thing, second)
    await bus.publish(Thing(label="x"), session=object())  # type: ignore[arg-type]

    assert order == [1, 2]


async def test_publishing_without_a_session_to_a_transactional_subscriber_raises(
    bus: EventBus,
) -> None:
    """A programming error at the publish site, and one that would otherwise
    lose a charge in silence. Better a loud failure the tests catch."""

    async def records(_: Thing, __: object) -> None:
        return None

    bus.subscribe_transactional(Thing, records)

    with pytest.raises(MissingSessionError, match="published without a session"):
        await bus.publish(Thing(label="charge"))


async def test_transactional_handlers_run_before_fire_and_forget_ones(bus: EventBus) -> None:
    """The durable write lands first; the notification goes out after."""
    order: list[str] = []

    async def durable(_: Thing, __: object) -> None:
        order.append("transactional")

    async def notify(_: Thing) -> None:
        order.append("fire-and-forget")

    bus.subscribe(Thing, notify)
    bus.subscribe_transactional(Thing, durable)
    await bus.publish(Thing(label="x"), session=object())  # type: ignore[arg-type]

    assert order == ["transactional", "fire-and-forget"]


async def test_clear_drops_both_channels(bus: EventBus) -> None:
    async def durable(_: Thing, __: object) -> None:
        raise AssertionError("should not run")

    bus.subscribe(Thing, lambda _: None)
    bus.subscribe_transactional(Thing, durable)
    bus.clear()

    # No session, and no complaint — proof the transactional registry is empty.
    await bus.publish(Thing(label="x"))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_every_event_carries_its_own_id_and_name() -> None:
    event = Thing(label="x", hospital_id=uuid.uuid4())
    assert event.name == "Thing"
    assert event.event_id != Thing(label="x").event_id
