from __future__ import annotations

from datetime import datetime, timezone

from raggd.source.health import evaluate_source_health
from raggd.source.models import (
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)


def _make_manifest(config: WorkspaceSourceConfig) -> SourceManifest:
    return SourceManifest(
        name=config.name,
        path=config.path,
        enabled=config.enabled,
        target=config.target,
        last_refresh_at=None,
    )


def test_evaluate_health_ok_when_target_and_refresh_present(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    target_dir = tmp_path / "data"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=target_dir,
    )
    manifest = _make_manifest(config).model_copy(
        update={
            "last_refresh_at": datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc),
        }
    )

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.OK
    assert snapshot.summary is None
    assert snapshot.actions == ()
    assert snapshot.checked_at == datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc)


def test_evaluate_health_reports_missing_target(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    source_dir.mkdir(parents=True)

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=None,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.DEGRADED
    assert "No target" in snapshot.summary
    assert "source target" in "".join(snapshot.actions)


def test_evaluate_health_reports_missing_directory(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=None,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.ERROR
    assert "missing" in snapshot.summary.lower()


def test_evaluate_health_reports_source_path_not_directory(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    source_dir.write_text("stub")

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=None,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.ERROR
    assert "not a directory" in snapshot.summary


def test_evaluate_health_reports_unreadable_target(monkeypatch, tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    target_dir = tmp_path / "data"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=target_dir,
    )
    manifest = _make_manifest(config)

    monkeypatch.setattr("raggd.source.health.os.access", lambda *_: False)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.ERROR
    assert "readable" in snapshot.summary


def test_evaluate_health_reports_target_as_file(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    target_file = tmp_path / "target.txt"
    source_dir.mkdir(parents=True)
    target_file.write_text("data")

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=target_file,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.DEGRADED
    assert "not a directory" in snapshot.summary


def test_evaluate_health_marks_missing_refresh_as_unknown(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    target_dir = tmp_path / "data"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=target_dir,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.UNKNOWN
    assert "not been refreshed" in snapshot.summary


def test_evaluate_health_prioritizes_highest_severity(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    source_dir.mkdir(parents=True)
    target_dir = tmp_path / "data"
    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=False,
        target=target_dir,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.ERROR
    assert "missing" in snapshot.summary.lower() or "target" in snapshot.summary.lower()
    assert snapshot.actions


def test_evaluate_health_reports_disabled_when_no_other_issues(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    target_dir = tmp_path / "data"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=False,
        target=target_dir,
    )
    manifest = _make_manifest(config).model_copy(
        update={
            "last_refresh_at": datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc),
        }
    )

    snapshot = evaluate_source_health(
        config=config,
        manifest=manifest,
        now=lambda: datetime(2025, 10, 6, 9, 30, tzinfo=timezone.utc),
    )

    assert snapshot.status == SourceHealthStatus.UNKNOWN
    assert "disabled" in snapshot.summary.lower()


def test_evaluate_health_uses_default_clock(tmp_path):
    source_dir = tmp_path / "workspace" / "sources" / "alpha"
    source_dir.mkdir(parents=True)

    config = WorkspaceSourceConfig(
        name="alpha",
        path=source_dir,
        enabled=False,
        target=None,
    )
    manifest = _make_manifest(config)

    snapshot = evaluate_source_health(config=config, manifest=manifest)

    assert snapshot.checked_at is not None
    assert snapshot.checked_at.tzinfo is not None
