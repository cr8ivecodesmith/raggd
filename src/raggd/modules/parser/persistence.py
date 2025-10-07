"""Persistence helpers for parser chunk slices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .handlers.base import HandlerResult
from .hashing import DEFAULT_HASH_ALGORITHM, hash_text
from .sql import load_sql

__all__ = [
    "ChunkSliceRow",
    "ChunkSliceRepository",
    "ChunkWritePipeline",
]


@dataclass(slots=True)
class ChunkSliceRow:
    """Row payload prepared for insertion into ``chunk_slices``."""

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
    metadata_json: str | None
    created_at: datetime
    updated_at: datetime
    first_seen_batch: str
    last_seen_batch: str

    def to_params(self) -> dict[str, object]:
        """Return a dictionary suitable for SQL parameter binding."""

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
            "overflow_is_truncated": 1 if self.overflow_is_truncated else 0,
            "overflow_reason": self.overflow_reason,
            "metadata_json": self.metadata_json,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "first_seen_batch": self.first_seen_batch,
            "last_seen_batch": self.last_seen_batch,
        }


class ChunkSliceRepository:
    """Repository wrapper around packaged ``chunk_slices`` SQL statements."""

    def __init__(self) -> None:
        self._upsert_sql = load_sql("chunk_slices_upsert.sql")
        self._delete_sql = load_sql("chunk_slices_delete_by_batch.sql")
        self._select_sql = load_sql("chunk_slices_select_by_chunk.sql")

    def upsert_many(
        self,
        connection: sqlite3.Connection,
        rows: Iterable[ChunkSliceRow],
    ) -> None:
        """Persist ``rows`` into ``chunk_slices`` via upsert semantics."""

        parameters = [row.to_params() for row in rows]
        connection.executemany(self._upsert_sql, parameters)

    def delete_by_batch(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
    ) -> int:
        """Delete all slices associated with ``batch_id`` returning count."""

        cursor = connection.execute(self._delete_sql, {"batch_id": batch_id})
        return cursor.rowcount if cursor.rowcount is not None else 0

    def select_for_chunk(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        chunk_id: str,
    ) -> Sequence[sqlite3.Row]:
        """Return slices for ``chunk_id`` within ``batch_id`` ordered by part."""

        cursor = connection.execute(
            self._select_sql, {"batch_id": batch_id, "chunk_id": chunk_id}
        )
        return cursor.fetchall()


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: str) -> str:
    """Return a normalized representation suitable for hashing."""

    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


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


def _metadata_json(metadata: Mapping[str, Any] | None) -> str | None:
    if not metadata:
        return None
    try:
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    except TypeError as exc:  # pragma: no cover - defensive guard
        raise TypeError(
            f"Chunk metadata not serializable: {metadata!r}"
        ) from exc


class ChunkWritePipeline:
    """Transform handler output into persisted ``chunk_slices`` rows."""

    def __init__(
        self,
        *,
        repository: ChunkSliceRepository,
        hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._hash_algorithm = hash_algorithm
        self._now = now or _default_now

    def persist_chunks(
        self,
        *,
        connection: sqlite3.Connection,
        batch_id: str,
        file_id: int,
        handler_name: str,
        handler_version: str,
        result: HandlerResult,
        handler_versions: Mapping[str, str],
        symbol_ids: Mapping[str, int],
        first_seen_batch: str | None = None,
        last_seen_batch: str | None = None,
    ) -> tuple[ChunkSliceRow, ...]:
        """Persist ``result`` chunks and return the stored rows."""

        if not result.chunks:
            return ()

        timestamp = self._now()
        first_seen = first_seen_batch or batch_id
        last_seen = last_seen_batch or batch_id

        rows: list[ChunkSliceRow] = []
        for chunk in result.chunks:
            chunk_handler = chunk.delegate or handler_name
            chunk_version = handler_versions.get(chunk_handler)
            if chunk_version is None:
                if chunk_handler == handler_name:
                    chunk_version = handler_version
                else:
                    raise KeyError(
                        f"Missing handler version for delegate {chunk_handler!r}"
                    )

            token_count = chunk.token_count
            if token_count is None:
                raise ValueError(
                    f"Chunk {chunk.chunk_id!r} emitted without token count."
                )

            metadata = dict(chunk.metadata or {})
            symbol_id = self._lookup_symbol(chunk.parent_symbol_id, symbol_ids)
            parent_symbol_id = self._lookup_symbol(
                metadata.get("delegate_parent_symbol"), symbol_ids
            )

            part_total = _coerce_int(metadata.get("part_total")) or 1
            start_line = _coerce_int(metadata.get("start_line"))
            end_line = _coerce_int(metadata.get("end_line"))

            overflow_reason = None
            raw_reason = metadata.get("overflow_reason")
            if raw_reason is not None:
                overflow_reason = str(raw_reason)

            overflow_flag = bool(
                metadata.get("overflow")
                or metadata.get("overflow_is_truncated")
            )

            metadata_json = _metadata_json(metadata)

            content_hash = hash_text(
                chunk.text,
                handler_version=chunk_version,
                algorithm=self._hash_algorithm,
                extra=(chunk.chunk_id, chunk_handler),
            )
            normalized_text = _normalize_text(chunk.text)
            content_norm_hash = hash_text(
                normalized_text,
                handler_version=chunk_version,
                algorithm=self._hash_algorithm,
                extra=(chunk.chunk_id, chunk_handler),
            )

            row = ChunkSliceRow(
                batch_id=batch_id,
                file_id=file_id,
                symbol_id=symbol_id,
                parent_symbol_id=parent_symbol_id,
                chunk_id=chunk.chunk_id,
                handler_name=chunk_handler,
                handler_version=chunk_version,
                part_index=chunk.part_index,
                part_total=part_total,
                start_line=start_line,
                end_line=end_line,
                start_byte=chunk.start_offset,
                end_byte=chunk.end_offset,
                token_count=token_count,
                content_hash=content_hash,
                content_norm_hash=content_norm_hash,
                content_text=chunk.text,
                overflow_is_truncated=overflow_flag,
                overflow_reason=overflow_reason,
                metadata_json=metadata_json,
                created_at=timestamp,
                updated_at=timestamp,
                first_seen_batch=first_seen,
                last_seen_batch=last_seen,
            )
            rows.append(row)

        if rows:
            self._repository.upsert_many(connection, rows)
        return tuple(rows)

    @staticmethod
    def _lookup_symbol(
        symbol_key: str | None,
        symbol_ids: Mapping[str, int],
    ) -> int | None:
        if not symbol_key:
            return None
        try:
            return symbol_ids[symbol_key]
        except KeyError as exc:
            raise KeyError(
                f"Symbol mapping missing for key {symbol_key!r}"
            ) from exc
