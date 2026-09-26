"""The event bus is the module extraction seam — its failure isolation is a
correctness property, not a nicety."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.core.events import DomainEvent, EventBus


@dataclass(frozen=True, kw_only=True, slots=True)
class _ThingHappened(DomainEvent):
    thing_id: uuid.UUID


@dataclass(frozen=True, kw_only=True, slots=True)
class _SpecificThingHappened(_ThingHappened):
    pass


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_handler_receives_published_event(bus: EventBus) -> None:
    seen: list[DomainEvent] = []

    @bus.on(_ThingHappened)
    async def _handler(event: _ThingHappened) -> None:
        seen.append(event)

    event = _ThingHappened(thing_id=uuid.uuid4())
    await bus.publish(event)

    assert seen == [event]


async def test_sync_and_async_handlers_both_work(bus: EventBus) -> None:
    calls: list[str] = []

    bus.subscribe(_ThingHappened, lambda _: calls.append("sync"))

    async def _async_handler(_: _ThingHappened) -> None:
        calls.append("async")

    bus.subscribe(_ThingHappened, _async_handler)
    await bus.publish(_ThingHappened(thing_id=uuid.uuid4()))

    assert sorted(calls) == ["async", "sync"]


async def test_a_failing_handler_does_not_starve_its_peers(bus: EventBus) -> None:
    """A flaky SMS gateway must not undo a committed patient registration."""
    survived: list[str] = []

    def _explodes(_: _ThingHappened) -> None:
        raise RuntimeError("gateway down")

    bus.subscribe(_ThingHappened, _explodes)
    bus.subscribe(_ThingHappened, lambda _: survived.append("ok"))

    await bus.publish(_ThingHappened(thing_id=uuid.uuid4()))

    assert survived == ["ok"]


async def test_base_type_handler_sees_subclassed_events(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe(_ThingHappened, lambda event: seen.append(event.name))

    await bus.publish(_SpecificThingHappened(thing_id=uuid.uuid4()))

    assert seen == ["_SpecificThingHappened"]


async def test_publishing_without_subscribers_is_not_an_error(bus: EventBus) -> None:
    await bus.publish(_ThingHappened(thing_id=uuid.uuid4()))


def test_events_carry_tenant_and_actor_context() -> None:
    hospital_id, actor_id = uuid.uuid4(), uuid.uuid4()
    event = _ThingHappened(thing_id=uuid.uuid4(), hospital_id=hospital_id, actor_id=actor_id)

    assert event.hospital_id == hospital_id
    assert event.actor_id == actor_id
    assert event.occurred_at.tzinfo is not None
