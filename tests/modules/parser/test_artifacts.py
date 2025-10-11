"""Coverage for chunk slice artifact helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from raggd.modules.parser.artifacts import (
    ChunkSlice,
    _coerce_bool,
    _coerce_int,
    _parse_datetime,
    _parse_metadata,
)


def test_chunk_slice_round_trip_mapping() -> None:
    now = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    later = datetime(2024, 1, 1, 13, tzinfo=timezone.utc)
    row = {
        "batch_id": "batch-1",
        "file_id": 11,
        "symbol_id": "5",
        "parent_symbol_id": "7",
        "chunk_id": "chunk-1",
        "handler_name": "python",
        "handler_version": "1.0.0",
        "part_index": "0",
        "part_total": "1",
        "start_line": "14",
        "end_line": 20.0,
        "start_byte": 0,
        "end_byte": "120",
        "token_count": 42,
        "content_hash": "hash",
        "content_norm_hash": None,
        "content_text": "example",
        "overflow_is_truncated": "false",
        "overflow_reason": "",
        "metadata_json": '{"delegate_parent_chunk": "chunk-0"}',
        "created_at": now.isoformat(),
        "updated_at": later.isoformat(),
        "first_seen_batch": "batch-1",
        "last_seen_batch": "batch-1",
    }

    slice_ = ChunkSlice.from_row(row)

    assert slice_.symbol_id == 5
    assert slice_.parent_symbol_id == 7
    assert slice_.overflow_is_truncated is False
    assert slice_.metadata["delegate_parent_chunk"] == "chunk-0"

    mapping = slice_.to_mapping()
    assert mapping["metadata_json"] == '{"delegate_parent_chunk":"chunk-0"}'
    assert mapping["start_line"] == 14
    assert mapping["end_line"] == 20
    assert mapping["overflow_is_truncated"] == 0
    assert mapping["created_at"] == now.isoformat()
    assert mapping["updated_at"] == later.isoformat()


def test_coerce_helpers_cover_edge_cases() -> None:
    assert _coerce_int(True) == 1
    assert _coerce_int("  ") is None
    assert _coerce_int("9") == 9
    assert _coerce_int(5.9) == 5

    assert _coerce_bool(0) is False
    assert _coerce_bool("YES") is True
    assert _coerce_bool("unknown") is True

    metadata = _parse_metadata({"a": 1})
    assert metadata["a"] == 1
    assert len(_parse_metadata(None)) == 0


def test_parse_datetime_requires_isoformat() -> None:
    with pytest.raises(ValueError):
        _parse_datetime(None, field="created_at")

    dt = _parse_datetime("2024-01-01T00:00:00+00:00", field="created_at")
    assert dt == datetime(2024, 1, 1, tzinfo=timezone.utc)
