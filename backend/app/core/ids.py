"""Primary-key generation.

CLAUDE.md §11 mandates UUID primary keys everywhere. We use **UUIDv7**, not v4.

Trade-off (stated per CLAUDE.md §14): v4 is uniformly random, so every insert
lands at a random point in the B-tree — index pages fragment and cache locality
collapses once the patient/encounter tables get large. v7 embeds a millisecond
timestamp in the high bits, so inserts are append-mostly and range scans by time
("today's encounters") stay on adjacent pages. Cost: creation time is inferable
from the ID. That is acceptable for internal record IDs; anything user-facing
and guessable-sensitive (UHID, invoice number) gets its own generated identifier
rather than exposing the PK.

Alternative if that leak ever matters: switch `new_id` to `uuid.uuid4`. Nothing
else in the codebase needs to change — that is the whole reason this indirection
exists.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["new_id", "uuid7"]

try:  # Python 3.14+ ships a native implementation.
    from uuid import uuid7  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised on 3.12/3.13 only

    def uuid7() -> uuid.UUID:
        """RFC 9562 UUIDv7: 48-bit big-endian ms timestamp + 74 random bits."""
        timestamp_ms = time.time_ns() // 1_000_000
        raw = bytearray(timestamp_ms.to_bytes(6, "big") + os.urandom(10))
        raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
        raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
        return uuid.UUID(bytes=bytes(raw))


def new_id() -> uuid.UUID:
    """The single entry point for generating a new primary key."""
    # Bound to a local first: on 3.14 `uuid.uuid7` is untyped to mypy, and
    # returning it directly trips --strict's no-any-return.
    value: uuid.UUID = uuid7()
    return value
