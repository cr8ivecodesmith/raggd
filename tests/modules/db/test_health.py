"""Tests for the database module health hook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sqlite3


from raggd.core.config import AppConfig, DbSettings, ModuleToggle, WorkspaceSettings
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.modules.db import DbLifecycleService, db_health_hook
from raggd.source.models import WorkspaceSourceConfig


def _make_paths(root: Path) -> WorkspacePaths:
    workspace = root / "workspace"
    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )
    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.archives_dir.mkdir(parents=True, exist_ok=True)
    paths.sources_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _make_config(
    paths: WorkspacePaths,
    *,
    include_source: bool = True,
    db_enabled: bool = True,
) -> AppConfig:
    sources: dict[str, WorkspaceSourceConfig] = {}
    if include_source:
        source_dir = paths.source_dir("alpha")
        source_dir.mkdir(parents=True, exist_ok=True)
        target = paths.workspace / "data"
        target.mkdir(parents=True, exist_ok=True)
        sources["alpha"] = WorkspaceSourceConfig(
            name="alpha",
            path=source_dir,
            enabled=True,
            target=target,
        )

    modules = {
        "source": ModuleToggle(enabled=True),
        "db": ModuleToggle(enabled=db_enabled, extras=("db",)),
    }

    settings = WorkspaceSettings(root=paths.workspace, sources=sources)
    return AppConfig(
        workspace_settings=settings,
        modules=modules,
        db=DbSettings(),
    )


def test_db_health_hook_reports_disabled_module(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths, include_source=False, db_enabled=False)
    handle = SimpleNamespace(paths=paths, config=config)

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.UNKNOWN
    assert "disabled" in (report.summary or "")


def test_db_health_hook_reports_healthy_state(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.OK
    assert report.summary == "database healthy"
    assert report.actions == ()


def test_db_health_hook_flags_missing_database(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    db_path = paths.source_database_path("alpha")
    db_path.unlink()

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "missing" in (report.summary or "")


def test_db_health_hook_detects_stale_vacuum(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    db_path = paths.source_database_path("alpha")
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_meta SET last_vacuum_at = ? WHERE id = 1",
            (stale.isoformat(),),
        )

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.DEGRADED
    assert "vacuum" in (report.summary or "")
    assert any("vacuum" in action for action in report.actions)
