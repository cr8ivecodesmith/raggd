"""Typed representations of database lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DbManifestState",
]


def _parse_datetime(value: object) -> datetime | None:
    """Return ``datetime`` parsed from ``value`` when possible."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"Invalid ISO datetime: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _normalize_pending(values: Iterable[object] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized: list[str] = []
    for item in values:
        if item is None:
            continue
        normalized.append(str(item))
    return tuple(normalized)


@dataclass(slots=True)
class DbManifestState:
    """Canonical snapshot of the ``modules.db`` manifest payload."""

    bootstrap_shortuuid7: str | None = None
    head_migration_uuid7: str | None = None
    head_migration_shortuuid7: str | None = None
    ledger_checksum: str | None = None
    last_vacuum_at: datetime | None = None
    last_ensure_at: datetime | None = None
    pending_migrations: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "DbManifestState":
        if payload is None:
            return cls()
        return cls(
            bootstrap_shortuuid7=str(payload.get("bootstrap_shortuuid7"))
            if payload.get("bootstrap_shortuuid7") is not None
            else None,
            head_migration_uuid7=str(payload.get("head_migration_uuid7"))
            if payload.get("head_migration_uuid7") is not None
            else None,
            head_migration_shortuuid7=str(
                payload.get("head_migration_shortuuid7")
            )
            if payload.get("head_migration_shortuuid7") is not None
            else None,
            ledger_checksum=str(payload.get("ledger_checksum"))
            if payload.get("ledger_checksum") is not None
            else None,
            last_vacuum_at=_parse_datetime(payload.get("last_vacuum_at"))
            if payload.get("last_vacuum_at") is not None
            else None,
            last_ensure_at=_parse_datetime(payload.get("last_ensure_at"))
            if payload.get("last_ensure_at") is not None
            else None,
            pending_migrations=_normalize_pending(
                payload.get("pending_migrations")
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "bootstrap_shortuuid7": self.bootstrap_shortuuid7,
            "head_migration_uuid7": self.head_migration_uuid7,
            "head_migration_shortuuid7": self.head_migration_shortuuid7,
            "ledger_checksum": self.ledger_checksum,
            "last_vacuum_at": _serialize_datetime(self.last_vacuum_at),
            "last_ensure_at": _serialize_datetime(self.last_ensure_at),
            "pending_migrations": list(self.pending_migrations),
        }

    def replace(self, **updates: Any) -> "DbManifestState":
        """Return a copy with ``updates`` applied."""

        pending = updates.pop("pending_migrations", None)
        if pending is not None:
            updates["pending_migrations"] = _normalize_pending(pending)
        return replace(self, **updates)

    def with_pending(self, values: Sequence[object]) -> "DbManifestState":
        """Return a copy with ``pending_migrations`` set to ``values``."""

        return self.replace(pending_migrations=values)

