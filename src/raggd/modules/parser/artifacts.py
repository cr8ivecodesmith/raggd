"""Canonical parser artifacts derived from chunk slice persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["ChunkSlice"]


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"1", "true", "yes", "on"}:
            return True
        if stripped in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _parse_metadata(value: Any) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if isinstance(value, Mapping):
        return MappingProxyType(dict(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return MappingProxyType({})
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"Invalid chunk metadata JSON: {value!r}") from exc
        if isinstance(payload, Mapping):
            return MappingProxyType(dict(payload))
        return MappingProxyType({})
    return MappingProxyType({})


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        raise ValueError(f"Chunk slice {field} is required")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise ValueError(
                f"Chunk slice {field} must be ISO-8601, got {value!r}"
            ) from exc
    raise TypeError(f"Chunk slice {field} must be str or datetime, got {type(value)}")


@dataclass(frozen=True, slots=True)
class ChunkSlice:
    """Domain representation of a persisted chunk slice."""

    batch_id: str
    file_id: int
    symbol_id: int | None
    parent_symbol_id: int | None
    chunk_id: str
    handler_name: str
    handler_version: str
    part_index: int
    part_total: int
    start_line: int | None
    end_line: int | None
    start_byte: int | None
    end_byte: int | None
    token_count: int
    content_hash: str
    content_norm_hash: str | None
    content_text: str
    overflow_is_truncated: bool
    overflow_reason: str | None
    metadata: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    first_seen_batch: str
    last_seen_batch: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChunkSlice":
        """Instantiate a canonical slice from a DB row mapping."""

        metadata = _parse_metadata(row.get("metadata_json"))
        created_at = _parse_datetime(row.get("created_at"), field="created_at")
        updated_at = _parse_datetime(row.get("updated_at"), field="updated_at")
        return cls(
            batch_id=str(row["batch_id"]),
            file_id=int(row["file_id"]),
            symbol_id=_coerce_int(row.get("symbol_id")),
            parent_symbol_id=_coerce_int(row.get("parent_symbol_id")),
            chunk_id=str(row["chunk_id"]),
            handler_name=str(row["handler_name"]),
            handler_version=str(row["handler_version"]),
            part_index=int(row["part_index"]),
            part_total=int(row["part_total"]),
            start_line=_coerce_int(row.get("start_line")),
            end_line=_coerce_int(row.get("end_line")),
            start_byte=_coerce_int(row.get("start_byte")),
            end_byte=_coerce_int(row.get("end_byte")),
            token_count=int(row["token_count"]),
            content_hash=str(row["content_hash"]),
            content_norm_hash=(
                None
                if row.get("content_norm_hash") is None
                else str(row["content_norm_hash"])
            ),
            content_text=str(row["content_text"]),
            overflow_is_truncated=_coerce_bool(row.get("overflow_is_truncated")),
            overflow_reason=(
                None
                if row.get("overflow_reason") is None
                else str(row["overflow_reason"])
            ),
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
            first_seen_batch=str(row["first_seen_batch"]),
            last_seen_batch=str(row["last_seen_batch"]),
        )

    def to_mapping(self) -> Mapping[str, Any]:
        """Return a lightweight mapping compatible with recomposition helpers."""

        payload = dict(self.metadata)
        metadata_json = None
        if payload:
            metadata_json = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )

        return {
            "batch_id": self.batch_id,
            "file_id": self.file_id,
            "symbol_id": self.symbol_id,
            "parent_symbol_id": self.parent_symbol_id,
            "chunk_id": self.chunk_id,
            "handler_name": self.handler_name,
            "handler_version": self.handler_version,
            "part_index": self.part_index,
            "part_total": self.part_total,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "token_count": self.token_count,
            "content_hash": self.content_hash,
            "content_norm_hash": self.content_norm_hash,
            "content_text": self.content_text,
            "overflow_is_truncated": int(self.overflow_is_truncated),
            "overflow_reason": self.overflow_reason,
            "metadata_json": metadata_json,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "first_seen_batch": self.first_seen_batch,
            "last_seen_batch": self.last_seen_batch,
        }
