from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from raggd.modules import HealthStatus
from raggd.modules.vdb import health as health_module
from raggd.modules.vdb.health import vdb_health_hook
from raggd.modules.vdb.service import VdbInfoError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        (0, None),
        (
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        (
            datetime(2024, 1, 1),
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        (
            "2024-01-01T00:00:00",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        (
            "2024-01-01T00:00:00Z",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        ("not-a-date", None),
    ],
)
def test_parse_timestamp_handles_inputs(
    value: object,
    expected: datetime | None,
) -> None:
    result = health_module._parse_timestamp(value)
    assert result == expected


def test_counts_summary_coerces_values() -> None:
    assert health_module._counts_summary("invalid") is None

    payload = {"chunks": "4", "vectors": 5.0, "index": "NaN"}
    summary = health_module._counts_summary(payload)
    assert summary == "chunks=4, vectors=5, index=0"


def test_summaries_from_entries_normalizes_levels() -> None:
    entries = [
        {"level": "INFO", "code": "healthy", "message": "All good"},
        {
            "level": "warning",
            "code": "missing-index",
            "message": "Index missing",
            "actions": ("rebuild", "rebuild"),
        },
        {"level": " ERROR ", "message": "Broken"},
        {"level": "degraded", "message": "Minor issue"},
        {"message": "No level"},
    ]

    status, summary, actions = health_module._summaries_from_entries(entries)

    assert status is HealthStatus.ERROR
    assert "[info] healthy: All good" in summary
    assert "[warning] missing-index: Index missing" in summary
    assert "[error] Broken" in summary
    assert "[warning] Minor issue" in summary
    assert actions == ("rebuild",)


def test_build_report_combines_counts_and_health() -> None:
    payload = {
        "selector": "demo:primary",
        "counts": {"chunks": "2", "vectors": 2, "index": 2},
        "last_sync_at": "2024-01-02T12:34:56Z",
        "health": [
            {
                "level": "warning",
                "code": "missing-index",
                "message": "Rebuild index",
            },
            {"level": "info", "code": "chunks-ok", "message": "Chunks synced"},
        ],
    }

    report = health_module._build_report(payload)

    assert report.name == "demo:primary"
    assert report.status is HealthStatus.DEGRADED
    assert "chunks=2, vectors=2, index=2" in (report.summary or "")
    assert "missing-index" in (report.summary or "")
    assert report.actions == ()
    assert report.last_refresh_at == datetime(
        2024,
        1,
        2,
        12,
        34,
        56,
        tzinfo=timezone.utc,
    )


def test_vdb_health_hook_handles_info_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaisingService:
        def info(self, *, source, vdb):  # type: ignore[no-untyped-def]
            raise VdbInfoError("boom")

    monkeypatch.setattr(
        health_module,
        "_build_service",
        lambda handle: RaisingService(),  # type: ignore[arg-type]
    )

    handle = SimpleNamespace()
    reports = vdb_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "Failed to collect VDB health" in (report.summary or "")
