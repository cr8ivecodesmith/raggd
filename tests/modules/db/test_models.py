from __future__ import annotations

from datetime import datetime, timezone

import pytest

import raggd.modules.db.models as models
from raggd.modules.db.models import DbManifestState


def test_manifest_state_roundtrip_and_normalization() -> None:
    recorded = datetime(2025, 1, 5, 12, 30, tzinfo=timezone.utc)
    payload = {
        "bootstrap_shortuuid7": "0001-boot",
        "head_migration_uuid7": "00000000-0000-7000-8000-000000000001",
        "head_migration_shortuuid7": "0001-boot",
        "ledger_checksum": "sha256:deadbeef",
        "last_vacuum_at": recorded.isoformat(),
        "last_ensure_at": recorded.replace(tzinfo=None).isoformat(),
        "last_sql_run_at": recorded.isoformat(),
        "pending_migrations": ["0002-next", None, 123],
    }

    state = DbManifestState.from_mapping(payload)
    assert state.bootstrap_shortuuid7 == "0001-boot"
    assert state.last_vacuum_at == recorded
    # Naive datetime values should coerce to UTC.
    assert state.last_ensure_at.tzinfo is timezone.utc
    assert state.last_sql_run_at == recorded
    assert state.pending_migrations == ("0002-next", "123")

    mapping = state.to_mapping()
    assert mapping["last_vacuum_at"] == recorded.isoformat()
    assert mapping["last_sql_run_at"] == recorded.isoformat()
    assert mapping["pending_migrations"] == ["0002-next", "123"]

    assert DbManifestState.from_mapping(None) == DbManifestState()


def test_manifest_state_replace_and_with_pending() -> None:
    state = DbManifestState(pending_migrations=("0001",))

    updated = state.replace(pending_migrations=["0002", 3])
    assert updated.pending_migrations == ("0002", "3")
    assert state.pending_migrations == ("0001",)

    pending = state.with_pending([None, "0004"])
    assert pending.pending_migrations == ("0004",)


@pytest.mark.parametrize("value", [123, object()])
def test_manifest_state_invalid_datetime(value: object) -> None:
    with pytest.raises(TypeError):
        DbManifestState.from_mapping({"last_vacuum_at": value})


def test_manifest_state_parse_datetime_inputs() -> None:
    state = DbManifestState.from_mapping(
        {"last_vacuum_at": datetime(2025, 1, 1)}
    )
    assert state.last_vacuum_at.tzinfo is timezone.utc

    none_state = DbManifestState.from_mapping({"last_vacuum_at": None})
    assert none_state.last_vacuum_at is None

    aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
    aware_state = DbManifestState.from_mapping({"last_vacuum_at": aware})
    assert aware_state.last_vacuum_at is aware

    assert models._parse_datetime(None) is None

    mapping = DbManifestState(last_vacuum_at=datetime(2025, 1, 1)).to_mapping()
    assert mapping["last_vacuum_at"].endswith("+00:00")
