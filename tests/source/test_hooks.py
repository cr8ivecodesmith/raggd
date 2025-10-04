from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from raggd.core.config import AppConfig, load_config, load_packaged_defaults
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.source.hooks import _convert_status, source_health_hook
from raggd.source.models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
)


@dataclass(slots=True)
class _Handle:
    paths: WorkspacePaths
    config: AppConfig


def _make_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def _make_config(root: Path) -> AppConfig:
    defaults = load_packaged_defaults()
    user_config = {
        "workspace": {
            "root": str(root),
            "sources": {
                "alpha": {
                    "enabled": True,
                    "path": str(root / "sources" / "alpha"),
                    "target": str(root / "data"),
                }
            },
        }
    }
    return load_config(defaults=defaults, user_config=user_config)


def test_source_health_hook_reports_manifest_status(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = _make_paths(workspace)
    paths.sources_dir.mkdir()
    source_dir = paths.sources_dir / "alpha"
    source_dir.mkdir()
    (workspace / "data").mkdir()

    manifest = SourceManifest(
        name="alpha",
        path=source_dir,
        enabled=True,
        target=workspace / "data",
        last_refresh_at=datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc),
        last_health=SourceHealthSnapshot(
            status=SourceHealthStatus.DEGRADED,
            checked_at=datetime(2025, 10, 5, 12, 5, tzinfo=timezone.utc),
            summary="Target index missing",
            actions=("Run raggd source refresh alpha",),
        ),
    )

    manifest_path = paths.source_manifest_path("alpha")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )

    config = _make_config(workspace)
    handle = _Handle(paths=paths, config=config)

    reports = source_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.name == "alpha"
    assert report.status is HealthStatus.DEGRADED
    assert report.summary == "Target index missing"
    assert report.actions == ("Run raggd source refresh alpha",)
    assert report.last_refresh_at == manifest.last_refresh_at


def test_source_health_hook_handles_missing_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = _make_paths(workspace)
    paths.sources_dir.mkdir()
    config = _make_config(workspace)
    handle = _Handle(paths=paths, config=config)

    reports = source_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.UNKNOWN
    assert report.summary is not None
    assert "Manifest missing" in report.summary


def test_source_health_hook_handles_manifest_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = _make_paths(workspace)
    paths.sources_dir.mkdir()
    config = _make_config(workspace)
    handle = _Handle(paths=paths, config=config)

    manifest_path = paths.source_manifest_path("alpha")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{}", encoding="utf-8")

    original = Path.read_text

    def _raising(self: Path, *args: object, **kwargs: object) -> str:
        if self == manifest_path:
            raise OSError("boom")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raising)

    reports = source_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "Failed to read manifest" in report.summary


def test_source_health_hook_handles_invalid_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = _make_paths(workspace)
    paths.sources_dir.mkdir()
    config = _make_config(workspace)
    handle = _Handle(paths=paths, config=config)

    manifest_path = paths.source_manifest_path("alpha")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{\"name\": 123}", encoding="utf-8")

    reports = source_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "is invalid" in report.summary


def test_convert_status_handles_none_and_unknown() -> None:
    assert _convert_status(SourceHealthStatus.OK) is HealthStatus.OK
    assert _convert_status("error") is HealthStatus.ERROR
    assert _convert_status(None) is HealthStatus.UNKNOWN
    assert _convert_status("unexpected") is HealthStatus.UNKNOWN
