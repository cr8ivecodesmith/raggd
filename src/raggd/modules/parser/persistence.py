"""Persistence helpers for parser chunk slices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifacts import ChunkSlice
from .handlers.base import HandlerChunk, HandlerResult
from .hashing import DEFAULT_HASH_ALGORITHM, hash_text
from .sql import load_sql
from raggd.core.logging import Logger, get_logger

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
        self._select_by_file_sql = load_sql("chunk_slices_select_by_file.sql")
        self._select_history_by_file_sql = load_sql(
            "chunk_slices_select_history_by_file.sql"
        )
        self._update_last_seen_sql = load_sql(
            "chunk_slices_update_last_seen.sql"
        )

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
        """Return ``chunk_id`` slices for ``batch_id`` ordered by part."""

        cursor = connection.execute(
            self._select_sql, {"batch_id": batch_id, "chunk_id": chunk_id}
        )
        return cursor.fetchall()

    def select_for_file(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        file_id: int,
    ) -> Sequence[sqlite3.Row]:
        """Return ``file_id`` slices in ``batch_id`` ordered by chunk/part."""

        cursor = connection.execute(
            self._select_by_file_sql,
            {"batch_id": batch_id, "file_id": file_id},
        )
        return cursor.fetchall()

    def fetch_for_file(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        file_id: int,
    ) -> tuple[ChunkSlice, ...]:
        """Return canonical chunk slices for ``file_id`` within ``batch_id``."""

        rows = self.select_for_file(
            connection,
            batch_id=batch_id,
            file_id=file_id,
        )
        return tuple(ChunkSlice.from_row(dict(row)) for row in rows)

    def select_history_for_file(
        self,
        connection: sqlite3.Connection,
        *,
        file_id: int,
    ) -> Sequence[sqlite3.Row]:
        """Return history slices for ``file_id`` ordered by chunk and part."""

        cursor = connection.execute(
            self._select_history_by_file_sql,
            {"file_id": file_id},
        )
        return cursor.fetchall()

    def fetch_history_for_file(
        self,
        connection: sqlite3.Connection,
        *,
        file_id: int,
    ) -> tuple[ChunkSlice, ...]:
        """Return canonical chunk slice history for ``file_id``."""

        rows = self.select_history_for_file(connection, file_id=file_id)
        return tuple(ChunkSlice.from_row(dict(row)) for row in rows)

    def mark_last_seen(
        self,
        connection: sqlite3.Connection,
        *,
        file_id: int,
        chunk_ids: Iterable[str],
        batch_id: str,
        updated_at: datetime,
    ) -> None:
        """Update ``last_seen_batch`` for ``chunk_ids`` within ``file_id``."""

        parameters = [
            {
                "file_id": file_id,
                "chunk_id": chunk_id,
                "last_seen_batch": batch_id,
                "updated_at": updated_at.isoformat(),
            }
            for chunk_id in chunk_ids
        ]
        if not parameters:
            return
        connection.executemany(self._update_last_seen_sql, parameters)


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


_SIGNATURE_KEYS = {
    "file_id",
    "chunk_id",
    "handler_name",
    "handler_version",
    "symbol_id",
    "parent_symbol_id",
    "part_index",
    "part_total",
    "start_line",
    "end_line",
    "start_byte",
    "end_byte",
    "token_count",
    "content_hash",
    "content_norm_hash",
    "content_text",
    "overflow_is_truncated",
    "overflow_reason",
    "metadata_json",
}


def _group_chunks(
    chunks: Sequence[HandlerChunk],
) -> dict[str, list[HandlerChunk]]:
    grouped: dict[str, list[HandlerChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.chunk_id, []).append(chunk)
    for parts in grouped.values():
        parts.sort(key=lambda item: item.part_index)
    return grouped


def _group_history_by_chunk(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        data = dict(raw)
        data["chunk_id"] = str(data["chunk_id"])
        data["part_index"] = int(data["part_index"])
        data["part_total"] = int(data["part_total"])
        data["token_count"] = int(data["token_count"])
        data["overflow_is_truncated"] = int(data["overflow_is_truncated"])
        grouped.setdefault(data["chunk_id"], []).append(data)
    return grouped


def _latest_rows_by_chunk(
    rows_by_chunk: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    latest: dict[str, dict[int, Mapping[str, Any]]] = {}
    for chunk_id, entries in rows_by_chunk.items():
        parts: dict[int, Mapping[str, Any]] = {}
        for entry in entries:
            part_index = int(entry["part_index"])
            current = parts.get(part_index)
            if current is None or (
                str(entry["updated_at"]) > str(current["updated_at"])
            ):
                parts[part_index] = entry
        latest[chunk_id] = [parts[index] for index in sorted(parts)]
    return latest


def _first_seen_for_chunk(
    rows: Sequence[Mapping[str, Any]],
    default: str,
) -> str:
    if not rows:
        return default
    earliest = min(rows, key=lambda item: str(item["created_at"]))
    batch = earliest.get("first_seen_batch")
    return str(batch) if batch is not None else default


def _row_signature_from_mapping(
    row: Mapping[str, Any],
) -> tuple[tuple[str, Any], ...]:
    signature: list[tuple[str, Any]] = []
    for key in sorted(_SIGNATURE_KEYS):
        value = row.get(key)
        if key == "overflow_is_truncated" and value is not None:
            value = int(value)
        signature.append((key, value))
    return tuple(signature)


def _row_signature_from_dataclass(
    row: ChunkSliceRow,
) -> tuple[tuple[str, Any], ...]:
    params = row.to_params()
    return _row_signature_from_mapping(params)


def _rows_equivalent(
    existing_rows: Sequence[Mapping[str, Any]],
    new_rows: Sequence[ChunkSliceRow],
) -> bool:
    if len(existing_rows) != len(new_rows):
        return False
    existing_signatures = [
        _row_signature_from_mapping(row) for row in existing_rows
    ]
    new_signatures = [_row_signature_from_dataclass(row) for row in new_rows]
    return existing_signatures == new_signatures


class ChunkWritePipeline:
    """Transform handler output into persisted ``chunk_slices`` rows."""

    def __init__(
        self,
        *,
        repository: ChunkSliceRepository,
        hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
        now: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._repository = repository
        self._hash_algorithm = hash_algorithm
        self._now = now or _default_now
        self._logger = logger or get_logger(
            __name__,
            component="parser-persistence",
        )

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
        configured_first_seen = first_seen_batch or batch_id
        configured_last_seen = last_seen_batch or batch_id

        history_rows = self._repository.select_history_for_file(
            connection,
            file_id=file_id,
        )
        history_by_chunk = _group_history_by_chunk(history_rows)
        latest_by_chunk = _latest_rows_by_chunk(history_by_chunk)

        grouped_chunks = _group_chunks(result.chunks)

        inserted_rows: list[ChunkSliceRow] = []
        reused_chunk_ids: list[str] = []

        file_path = self._normalize_path(result.file.path)

        for chunk_id, parts in grouped_chunks.items():
            existing_history = history_by_chunk.get(chunk_id, ())
            first_seen_for_chunk = _first_seen_for_chunk(
                existing_history,
                configured_first_seen,
            )

            chunk_rows = self._build_chunk_rows(
                parts=parts,
                batch_id=batch_id,
                file_id=file_id,
                handler_name=handler_name,
                handler_version=handler_version,
                handler_versions=handler_versions,
                symbol_ids=symbol_ids,
                first_seen_batch=first_seen_for_chunk,
                last_seen_batch=configured_last_seen,
                timestamp=timestamp,
                file_path=file_path,
            )

            latest_rows = latest_by_chunk.get(chunk_id)
            if latest_rows and _rows_equivalent(latest_rows, chunk_rows):
                reused_chunk_ids.append(chunk_id)
                continue

            inserted_rows.extend(chunk_rows)

        if inserted_rows:
            self._repository.upsert_many(connection, inserted_rows)

        if reused_chunk_ids:
            self._repository.mark_last_seen(
                connection,
                file_id=file_id,
                chunk_ids=reused_chunk_ids,
                batch_id=configured_last_seen,
                updated_at=timestamp,
            )

        return tuple(inserted_rows)

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

    def _build_chunk_rows(
        self,
        *,
        parts: Sequence[HandlerChunk],
        batch_id: str,
        file_id: int,
        handler_name: str,
        handler_version: str,
        handler_versions: Mapping[str, str],
        symbol_ids: Mapping[str, int],
        first_seen_batch: str,
        last_seen_batch: str,
        timestamp: datetime,
        file_path: str,
    ) -> list[ChunkSliceRow]:
        rows: list[ChunkSliceRow] = []
        for chunk in parts:
            chunk_handler = chunk.delegate or handler_name
            chunk_version = self._resolve_handler_version(
                chunk_handler,
                handler_name=handler_name,
                handler_version=handler_version,
                handler_versions=handler_versions,
            )
            row = self._build_chunk_row(
                chunk=chunk,
                batch_id=batch_id,
                file_id=file_id,
                chunk_handler=chunk_handler,
                chunk_version=chunk_version,
                symbol_ids=symbol_ids,
                first_seen_batch=first_seen_batch,
                last_seen_batch=last_seen_batch,
                timestamp=timestamp,
                file_path=file_path,
            )
            rows.append(row)
        return rows

    def _build_chunk_row(
        self,
        *,
        chunk: HandlerChunk,
        batch_id: str,
        file_id: int,
        chunk_handler: str,
        chunk_version: str,
        symbol_ids: Mapping[str, int],
        first_seen_batch: str,
        last_seen_batch: str,
        timestamp: datetime,
        file_path: str,
    ) -> ChunkSliceRow:
        token_count = chunk.token_count
        if token_count is None:
            raise ValueError(
                f"Chunk {chunk.chunk_id!r} emitted without token count."
            )

        metadata = dict(chunk.metadata or {})
        symbol_id = self._lookup_symbol(chunk.parent_symbol_id, symbol_ids)
        parent_symbol_id = self._lookup_symbol(
            metadata.get("delegate_parent_symbol"),
            symbol_ids,
        )

        part_total = _coerce_int(metadata.get("part_total")) or 1
        start_line = _coerce_int(metadata.get("start_line"))
        end_line = _coerce_int(metadata.get("end_line"))

        overflow_reason = None
        raw_reason = metadata.get("overflow_reason")
        if raw_reason is not None:
            overflow_reason = str(raw_reason)

        overflow_flag = bool(
            metadata.get("overflow") or metadata.get("overflow_is_truncated")
        )

        chunk_key = self._derive_chunk_key(
            batch_id=batch_id,
            handler=chunk_handler,
            file_path=file_path,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
            part_index=chunk.part_index,
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
            first_seen_batch=first_seen_batch,
            last_seen_batch=last_seen_batch,
        )

        if overflow_flag or overflow_reason:
            self._logger.info(
                "parser-chunk-overflow",
                chunk_key=chunk_key,
                handler=chunk_handler,
                file_path=file_path,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                part_index=chunk.part_index,
                part_total=part_total,
                token_count=token_count,
                overflow_is_truncated=overflow_flag,
                overflow_reason=overflow_reason,
            )

        return row

    @staticmethod
    def _resolve_handler_version(
        chunk_handler: str,
        *,
        handler_name: str,
        handler_version: str,
        handler_versions: Mapping[str, str],
    ) -> str:
        version = handler_versions.get(chunk_handler)
        if version is not None:
            return version
        if chunk_handler == handler_name:
            return handler_version
        raise KeyError(
            f"Missing handler version for delegate {chunk_handler!r}"
        )

    @staticmethod
    def _normalize_path(path: Path | str) -> str:
        if isinstance(path, Path):
            return path.as_posix()
        return str(path).replace("\\", "/")

    @staticmethod
    def _derive_chunk_key(
        *,
        batch_id: str,
        handler: str,
        file_path: str,
        start_offset: int,
        end_offset: int,
        part_index: int,
    ) -> str:
        return (
            f"{batch_id}:{handler}:{file_path}:{start_offset}:"
            f"{end_offset}:{part_index}"
        )
