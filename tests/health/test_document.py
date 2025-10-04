"""Tests for health document helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import json
import os
import pytest

from raggd.health import (
    HealthDetail,
    HealthDocument,
    HealthDocumentReadError,
    HealthDocumentStore,
    HealthModuleSnapshot,
    HealthDocumentWriteError,
    build_module_snapshot,
)
from raggd.modules.registry import HealthReport, HealthStatus


def test_build_module_snapshot_derives_highest_severity() -> None:
    reports = [
        HealthReport(name="alpha", status=HealthStatus.OK),
        HealthReport(name="beta", status=HealthStatus.DEGRADED, summary="warn"),
        HealthReport(name="gamma", status=HealthStatus.ERROR, summary="boom"),
    ]

    snapshot = build_module_snapshot(
        reports,
        checked_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    assert snapshot.status is HealthStatus.ERROR
    assert snapshot.checked_at == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert tuple(detail.name for detail in snapshot.details) == (
        "alpha",
        "beta",
        "gamma",
    )


def test_build_module_snapshot_defaults_to_ok_when_empty() -> None:
    snapshot = build_module_snapshot(
        [],
        checked_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert snapshot.status is HealthStatus.OK
    assert snapshot.details == ()


def test_document_merge_replaces_module_entries() -> None:
    original = HealthDocument.model_validate(
        {
            "sources": HealthModuleSnapshot(
                checked_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                status=HealthStatus.OK,
                details=(HealthDetail(name="alpha", status=HealthStatus.OK),),
            )
        }
    )

    updated_snapshot = HealthModuleSnapshot(
        checked_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        status=HealthStatus.DEGRADED,
        details=(
            HealthDetail(
                name="beta",
                status=HealthStatus.DEGRADED,
                summary="warn",
            ),
        ),
    )

    merged = original.merge(
        {"sources": updated_snapshot, "other": updated_snapshot}
    )

    assert merged.root["sources"].checked_at == datetime(
        2024,
        2,
        1,
        tzinfo=timezone.utc,
    )
    assert "other" in merged.root
    assert len(merged.root) == 2


def test_store_load_returns_empty_when_missing(tmp_path: Path) -> None:
    store = HealthDocumentStore(tmp_path / ".health.json")
    document = store.load()
    assert document.root == {}


def test_store_update_merges_and_persists(tmp_path: Path) -> None:
    path = tmp_path / ".health.json"
    store = HealthDocumentStore(path)

    first_snapshot = build_module_snapshot(
        [
            HealthReport(name="alpha", status=HealthStatus.OK),
        ],
        checked_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    store.write(HealthDocument.model_validate({"sources": first_snapshot}))

    second_snapshot = build_module_snapshot(
        [
            HealthReport(
                name="beta",
                status=HealthStatus.ERROR,
                summary="down",
            ),
        ],
        checked_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )

    merged = store.update({"sources": second_snapshot})

    assert merged.root["sources"].status is HealthStatus.ERROR
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["sources"]["status"] == "error"


def test_store_write_handles_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".health.json"
    store = HealthDocumentStore(path)

    def raise_replace(src: str, dst: str) -> None:
        raise OSError("boom")  # pragma: no cover - invoked in test

    monkeypatch.setattr(os, "replace", raise_replace)

    snapshot = build_module_snapshot(
        [HealthReport(name="alpha", status=HealthStatus.OK)],
        checked_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(HealthDocumentWriteError):
        store.write(HealthDocument.model_validate({"sources": snapshot}))


def test_load_raises_when_structure_invalid(tmp_path: Path) -> None:
    path = tmp_path / ".health.json"
    path.write_text("[]", encoding="utf-8")

    store = HealthDocumentStore(path)

    with pytest.raises(HealthDocumentReadError):
        store.load()
