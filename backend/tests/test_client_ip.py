"""SEC-06: the audit log's IP is not whatever the caller says it is.

`X-Forwarded-For` was believed from anyone. Through the web app that was
harmless, but an insider who could reach :8000 directly could put any address
they liked against a payment or a write-off in the audit log. It is now read
only when the connection comes from a proxy the deployment has named.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.core.config import settings
from app.core.deps import get_client_ip


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 50000)})


async def test_a_direct_caller_cannot_choose_its_own_address() -> None:
    assert await get_client_ip(_request("10.1.2.3", "8.8.8.8")) == "10.1.2.3"


async def test_a_trusted_proxy_passes_on_the_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["172.16.0.0/12"])
    assert await get_client_ip(_request("172.18.0.5", "49.36.1.20")) == "49.36.1.20"


async def test_the_nearest_untrusted_hop_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client can prepend anything to the header; only what our own proxies
    appended is believed — so the address is read from the right."""
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["172.16.0.0/12", "10.0.0.1"])
    request = _request("172.18.0.5", "1.1.1.1, 49.36.1.20, 10.0.0.1")
    assert await get_client_ip(request) == "49.36.1.20"


async def test_a_malformed_header_falls_back_to_the_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["172.16.0.0/12"])
    assert await get_client_ip(_request("172.18.0.5", "not-an-ip")) == "172.18.0.5"


async def test_no_header_is_the_peer() -> None:
    assert await get_client_ip(_request("10.1.2.3")) == "10.1.2.3"
