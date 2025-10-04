from __future__ import annotations

from datetime import datetime, timedelta, timezone

import uuid

import pytest

from raggd.modules.db.uuid7 import (
    SHORT_UUID7_LENGTH,
    ShortUUID7,
    ensure_short_uuid7_order,
    generate_uuid7,
    short_uuid7,
    uuid7_timestamp,
    validate_short_uuid7,
)


def test_generate_uuid7_short_form_roundtrip() -> None:
    instant = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    value = generate_uuid7(when=instant)
    short = short_uuid7(value)

    assert isinstance(short, ShortUUID7)
    assert len(short.value) == SHORT_UUID7_LENGTH
    assert uuid7_timestamp(value) == instant


def test_generate_uuid7_uses_current_time() -> None:
    value = generate_uuid7()
    assert isinstance(value, uuid.UUID)


def test_validate_short_uuid7_rejects_invalid_length() -> None:
    with pytest.raises(ValueError):
        validate_short_uuid7("abc")


def test_validate_short_uuid7_rejects_invalid_characters() -> None:
    with pytest.raises(ValueError):
        validate_short_uuid7("0" * (SHORT_UUID7_LENGTH - 1) + "$")


def test_short_uuid7_preserves_ordering() -> None:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    uuids = [
        generate_uuid7(when=base + timedelta(milliseconds=index))
        for index in range(10)
    ]
    assert ensure_short_uuid7_order(uuids)


def test_generate_uuid7_rejects_negative_timestamp() -> None:
    before_epoch = datetime(1960, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        generate_uuid7(when=before_epoch)
