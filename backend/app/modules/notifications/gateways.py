"""The provider seam: where a rendered message stops being our problem.

CLAUDE.md §9 asks for a **channel-agnostic** dispatcher. That word does the work
here. `service.py` knows about a ladder of channels and a message; it does not
know that WhatsApp needs a pre-approved template id, that Indian SMS needs a DLT
entity registration, or that email needs a MIME part. Each of those lives behind
`Gateway.send`, which takes an `OutboundMessage` and returns a `GatewayResult`.

Nothing here talks to a third party yet, and that is deliberate rather than
unfinished. A real WhatsApp Business adapter needs a Meta WABA account and
per-message template approval; Indian SMS needs DLT registration of the entity
and every template. Both are procurement, not programming, and neither should
block the hospital's own logic from being finished and tested. `ConsoleGateway`
records exactly what would have gone out; swapping in `WhatsAppCloudGateway`
later is a new class in this file and one environment variable.

The failure contract is the important part, and it is the opposite of the rest
of the codebase's: a gateway **returns** failure, it does not raise. A provider
being down is an expected Tuesday, not an exception — and the whole point of the
ladder is that WhatsApp refusing is the normal path to SMS succeeding, not an
error anybody should see. `send` still guards against an adapter that raises
anyway, because a third-party SDK will eventually do so.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from app.core.config import settings
from app.modules.notifications.models import NotificationChannel

logger = logging.getLogger(__name__)

__all__ = [
    "ConsoleGateway",
    "Gateway",
    "GatewayResult",
    "NullGateway",
    "OutboundMessage",
    "available_gateways",
    "get_gateway",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class OutboundMessage:
    """Everything a provider needs, and nothing it does not.

    No ORM object crosses this boundary on purpose: an adapter that can reach a
    `Notification` can reach the whole database, and this is the one place in
    the system where third-party code will eventually run.
    """

    notification_id: uuid.UUID
    channel: NotificationChannel
    address: str
    body: str
    subject: str | None = None
    language: str = "en"
    # The template code doubles as the provider-side template name for WhatsApp,
    # where free-form messages outside a 24-hour session window are refused.
    template_code: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class GatewayResult:
    """The outcome of one attempt on one channel."""

    succeeded: bool
    gateway: str
    provider_message_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    latency_ms: int | None = None

    @classmethod
    def ok(
        cls, gateway: str, *, provider_message_id: str | None = None, latency_ms: int | None = None
    ) -> GatewayResult:
        return cls(
            succeeded=True,
            gateway=gateway,
            provider_message_id=provider_message_id,
            latency_ms=latency_ms,
        )

    @classmethod
    def failure(
        cls,
        gateway: str,
        *,
        code: str,
        detail: str | None = None,
        latency_ms: int | None = None,
    ) -> GatewayResult:
        return cls(
            succeeded=False,
            gateway=gateway,
            error_code=code,
            error_detail=(detail or "")[:500] or None,
            latency_ms=latency_ms,
        )


class Gateway(Protocol):
    """One provider adapter.

    `supports` exists so the ladder can skip a channel the configured provider
    cannot serve at all, rather than recording a guaranteed failure against it.
    A hospital on an SMS-only provider should not accumulate a WhatsApp rejection
    for every message it ever sends.
    """

    name: str

    def supports(self, channel: NotificationChannel) -> bool: ...

    async def send(self, message: OutboundMessage) -> GatewayResult: ...


class ConsoleGateway:
    """Logs the message instead of sending it. The default outside production.

    Useful beyond development: it is what makes the whole pipeline — trigger,
    suppression, template resolution, ladder, attempt records — verifiable
    end-to-end without a provider contract, which is how Phase 7 gets tested at
    all. The log line is deliberately readable by a person, not JSON.
    """

    name = "console"

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        started = time.perf_counter()
        logger.info(
            "[%s -> %s] %s%s",
            message.channel.value,
            message.address,
            f"({message.subject}) " if message.subject else "",
            message.body,
        )
        latency = int((time.perf_counter() - started) * 1000)
        return GatewayResult.ok(
            self.name,
            provider_message_id=f"console-{message.notification_id.hex[:12]}",
            latency_ms=latency,
        )


class NullGateway:
    """Accepts everything and does nothing. No log line.

    For test runs that assert on the notification rows rather than on delivery,
    and for a deployment that wants the pipeline exercised with the outside
    world switched off.
    """

    name = "null"

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        return GatewayResult.ok(
            self.name, provider_message_id=f"null-{message.notification_id.hex}"
        )


# A future `WhatsAppCloudGateway` / `Msg91Gateway` / `SmtpGateway` registers here.
# Keeping the registry a plain dict rather than an entry-point mechanism is
# deliberate: three adapters do not need a plugin system, and a plugin system is
# a place for a misconfiguration to hide.
_GATEWAYS: Final[dict[str, type[Gateway]]] = {
    ConsoleGateway.name: ConsoleGateway,
    NullGateway.name: NullGateway,
}


def available_gateways() -> tuple[str, ...]:
    return tuple(sorted(_GATEWAYS))


def get_gateway(name: str | None = None) -> Gateway:
    """Resolve the configured adapter.

    An unknown name falls back to the console rather than raising. The
    alternative is an API that boots fine and then fails on the first
    notification, at which point the person who mistyped the variable is not the
    person reading the traceback.
    """
    key = (name or settings.NOTIFICATION_GATEWAY or ConsoleGateway.name).strip().lower()
    gateway_type = _GATEWAYS.get(key)
    if gateway_type is None:
        logger.warning(
            "unknown NOTIFICATION_GATEWAY=%r; falling back to console. known=%s",
            key,
            available_gateways(),
        )
        gateway_type = ConsoleGateway
    return gateway_type()
