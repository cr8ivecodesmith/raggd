"""Helpers to recompose chunk slice rows into chunk trees."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .persistence import ChunkSliceRepository

__all__ = [
    "ChunkSlicePart",
    "RecomposedChunk",
    "ChunkRecomposer",
    "recompose_chunk_slices",
]


@dataclass(frozen=True, slots=True)
class ChunkSlicePart:
    """Single persisted chunk slice prepared for recomposition."""

    part_index: int
    part_total: int
    token_count: int
    text: str
    start_line: int | None
    end_line: int | None
    start_byte: int | None
    end_byte: int | None
    overflow_is_truncated: bool
    overflow_reason: str | None
    metadata: Mapping[str, Any]
    content_hash: str
    content_norm_hash: str | None


@dataclass(frozen=True, slots=True)
class RecomposedChunk:
    """Chunk reconstructed from one or more slice parts."""

    chunk_id: str
    batch_id: str
    file_id: int
    handler_name: str
    handler_version: str
    symbol_id: int | None
    parent_symbol_id: int | None
    token_count: int
    metadata: Mapping[str, Any]
    parts: tuple[ChunkSlicePart, ...]
    text: str
    start_line: int | None
    end_line: int | None
    start_byte: int | None
    end_byte: int | None
    first_seen_batch: str
    last_seen_batch: str
    delegate_parent_chunk_id: str | None
    delegate_children: tuple["RecomposedChunk", ...]


class ChunkRecomposer:
    """Facade for reconstructing chunk trees from persisted slices."""

    def __init__(self, repository: ChunkSliceRepository) -> None:
        self._repository = repository

    def for_file(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        file_id: int,
    ) -> tuple[RecomposedChunk, ...]:
        """Return recomposed chunks for ``file_id`` within ``batch_id``."""

        rows = self._repository.select_for_file(
            connection, batch_id=batch_id, file_id=file_id
        )
        return recompose_chunk_slices(dict(row) for row in rows)


def recompose_chunk_slices(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[RecomposedChunk, ...]:
    """Recompose chunk slices emitted by the persistence pipeline."""

    grouped: dict[str, list[_SliceRecord]] = {}
    for raw in rows:
        record = _record_from_row(raw)
        grouped.setdefault(record.chunk_id, []).append(record)

    if not grouped:
        return ()

    chunk_map: dict[str, RecomposedChunk] = {}
    for chunk_id, records in grouped.items():
        ordered = sorted(records, key=lambda item: item.part.part_index)
        parts = tuple(item.part for item in ordered)
        metadata = _chunk_metadata(parts)
        chunk = RecomposedChunk(
            chunk_id=chunk_id,
            batch_id=ordered[0].batch_id,
            file_id=ordered[0].file_id,
            handler_name=ordered[0].handler_name,
            handler_version=ordered[0].handler_version,
            symbol_id=ordered[0].symbol_id,
            parent_symbol_id=ordered[0].parent_symbol_id,
            token_count=sum(part.token_count for part in parts),
            metadata=metadata,
            parts=parts,
            text="".join(part.text for part in parts),
            start_line=_min_optional(part.start_line for part in parts),
            end_line=_max_optional(part.end_line for part in parts),
            start_byte=_min_optional(part.start_byte for part in parts),
            end_byte=_max_optional(part.end_byte for part in parts),
            first_seen_batch=ordered[0].first_seen_batch,
            last_seen_batch=ordered[0].last_seen_batch,
            delegate_parent_chunk_id=_as_str_or_none(
                metadata.get("delegate_parent_chunk")
            ),
            delegate_children=(),
        )
        chunk_map[chunk_id] = chunk

    children_map: defaultdict[str, list[RecomposedChunk]] = defaultdict(list)
    for chunk in chunk_map.values():
        parent_id = chunk.delegate_parent_chunk_id
        if parent_id is None:
            continue
        if parent_id not in chunk_map:
            raise ValueError(
                f"Delegate chunk {chunk.chunk_id!r} references missing parent {parent_id!r}"
            )
        children_map[parent_id].append(chunk)

    for parent_id in sorted(
        children_map,
        key=lambda cid: _chunk_sort_key(chunk_map[cid]),
    ):
        parent = chunk_map[parent_id]
        sorted_children = tuple(
            sorted(children_map[parent_id], key=_chunk_sort_key)
        )
        chunk_map[parent_id] = replace(parent, delegate_children=sorted_children)

    top_level = [
        chunk for chunk in chunk_map.values() if chunk.delegate_parent_chunk_id is None
    ]
    top_level.sort(key=_chunk_sort_key)
    return tuple(top_level)


@dataclass(frozen=True, slots=True)
class _SliceRecord:
    batch_id: str
    file_id: int
    chunk_id: str
    handler_name: str
    handler_version: str
    symbol_id: int | None
    parent_symbol_id: int | None
    first_seen_batch: str
    last_seen_batch: str
    part: ChunkSlicePart


def _record_from_row(row: Mapping[str, Any]) -> _SliceRecord:
    metadata = _parse_metadata(row.get("metadata_json"))
    part = ChunkSlicePart(
        part_index=int(row["part_index"]),
        part_total=int(row["part_total"]),
        token_count=int(row["token_count"]),
        text=str(row["content_text"]),
        start_line=_coerce_optional_int(row.get("start_line")),
        end_line=_coerce_optional_int(row.get("end_line")),
        start_byte=_coerce_optional_int(row.get("start_byte")),
        end_byte=_coerce_optional_int(row.get("end_byte")),
        overflow_is_truncated=bool(row["overflow_is_truncated"]),
        overflow_reason=_as_str_or_none(row.get("overflow_reason")),
        metadata=_freeze_mapping(metadata),
        content_hash=str(row["content_hash"]),
        content_norm_hash=_as_str_or_none(row.get("content_norm_hash")),
    )
    return _SliceRecord(
        batch_id=str(row["batch_id"]),
        file_id=int(row["file_id"]),
        chunk_id=str(row["chunk_id"]),
        handler_name=str(row["handler_name"]),
        handler_version=str(row["handler_version"]),
        symbol_id=_coerce_optional_int(row.get("symbol_id")),
        parent_symbol_id=_coerce_optional_int(row.get("parent_symbol_id")),
        first_seen_batch=str(row["first_seen_batch"]),
        last_seen_batch=str(row["last_seen_batch"]),
        part=part,
    )


def _chunk_metadata(parts: tuple[ChunkSlicePart, ...]) -> Mapping[str, Any]:
    if not parts:
        return MappingProxyType({})

    base = dict(parts[0].metadata)
    base.pop("part_index", None)
    totals = {part.part_total for part in parts if part.part_total > 0}
    aggregate_total = len(parts)
    if len(totals) == 1:
        aggregate_total = max(aggregate_total, totals.pop())
    base["part_total"] = aggregate_total
    return _freeze_mapping(base)


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        return json.loads(raw)
    return dict(raw)


def _freeze_mapping(data: Mapping[str, Any] | dict[str, Any]) -> Mapping[str, Any]:
    if not data:
        return MappingProxyType({})
    return MappingProxyType(dict(data))


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _as_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _min_optional(values: Iterable[int | None]) -> int | None:
    filtered = [item for item in values if item is not None]
    if not filtered:
        return None
    return min(filtered)


def _max_optional(values: Iterable[int | None]) -> int | None:
    filtered = [item for item in values if item is not None]
    if not filtered:
        return None
    return max(filtered)


def _chunk_sort_key(chunk: RecomposedChunk) -> tuple[int, int, str]:
    position = _first_position(chunk)
    return (chunk.file_id, position, chunk.chunk_id)


def _first_position(chunk: RecomposedChunk) -> int:
    if chunk.start_byte is not None:
        return chunk.start_byte
    if chunk.start_line is not None:
        return chunk.start_line
    if chunk.parts:
        return chunk.parts[0].part_index
    return 0
