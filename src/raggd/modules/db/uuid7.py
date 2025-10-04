"""Helpers for working with UUID7 identifiers and short representations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import uuid
from typing import Iterable

__all__ = [
    "SHORT_UUID7_LENGTH",
    "ShortUUID7",
    "generate_uuid7",
    "short_uuid7",
    "uuid7_timestamp",
    "validate_short_uuid7",
    "ensure_short_uuid7_order",
]


CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_UUID7_LENGTH = 12


@dataclass(frozen=True, slots=True)
class ShortUUID7:
    """Value object representing a normalized 12-character short UUID7."""

    value: str

    def __post_init__(self) -> None:
        validate_short_uuid7(self.value)

    def __str__(self) -> str:  # pragma: no cover - dataclass convenience
        return self.value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_uuid7(*, when: datetime | None = None) -> uuid.UUID:
    """Return a time-ordered UUIDv7 value."""

    instant = when.astimezone(timezone.utc) if when else _now()
    timestamp_ms = int(instant.timestamp() * 1000)
    if not 0 <= timestamp_ms < 1 << 48:
        raise ValueError("uuid7 timestamp out of range")

    timestamp_bytes = timestamp_ms.to_bytes(6, "big")
    random_bytes = bytearray(secrets.token_bytes(10))

    # Set version (0b0111) in high nibble of byte 6.
    random_bytes[0] &= 0x0F
    random_bytes[0] |= 0x70

    # Set variant (0b10xx) in byte 8.
    random_bytes[2] &= 0x3F
    random_bytes[2] |= 0x80

    return uuid.UUID(bytes=bytes(timestamp_bytes + random_bytes))


def short_uuid7(value: uuid.UUID) -> ShortUUID7:
    """Return the 12-character Crockford base32 prefix for ``value``."""

    high_bits = value.int >> (128 - (SHORT_UUID7_LENGTH * 5))
    encoded = _encode_crockford(high_bits, SHORT_UUID7_LENGTH)
    return ShortUUID7(encoded)


def uuid7_timestamp(value: uuid.UUID) -> datetime:
    """Return the UTC timestamp embedded in a UUIDv7."""

    ms = int.from_bytes(value.bytes[0:6], "big")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def validate_short_uuid7(value: str) -> None:
    """Validate that ``value`` is a normalized short UUID7 string."""

    if len(value) != SHORT_UUID7_LENGTH:
        raise ValueError(
            f"shortuuid7 must be {SHORT_UUID7_LENGTH} characters: {value!r}"
        )
    for char in value:
        if char not in CROCKFORD_ALPHABET:
            raise ValueError(f"Invalid shortuuid7 character: {char!r}")


def ensure_short_uuid7_order(values: Iterable[uuid.UUID]) -> bool:
    """Return ``True`` when short UUID7 ordering matches canonical order."""

    sequence = tuple(values)
    canonical = sorted(sequence, key=lambda item: item.int)
    shortened = sorted(sequence, key=lambda item: short_uuid7(item).value)
    return [item.int for item in canonical] == [item.int for item in shortened]


def _encode_crockford(value: int, length: int) -> str:
    symbols: list[str] = ["0"] * length
    for index in range(length - 1, -1, -1):
        symbols[index] = CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(symbols)
