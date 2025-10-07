"""Tests for :mod:`raggd.modules.parser.service`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from raggd.core.config import AppConfig, ParserModuleSettings, WorkspaceSettings
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.modules.parser import (
    ParserBatchPlan,
    ParserModuleDisabledError,
    ParserService,
)
from raggd.modules.parser.models import ParserRunMetrics
from raggd.modules.parser.registry import (
    HandlerProbeResult,
    HandlerRegistry,
    ParserHandlerDescriptor,
)
from raggd.source.models import WorkspaceSourceConfig


def _make_workspace(tmp_path: Path) -> WorkspacePaths:
    root = tmp_path / "workspace"
    config_file = root / "raggd.toml"
    logs_dir = root / "logs"
    archives_dir = root / "archives"
    sources_dir = root / "sources"
    paths = WorkspacePaths(
        workspace=root,
        config_file=config_file,
        logs_dir=logs_dir,
        archives_dir=archives_dir,
        sources_dir=sources_dir,
    )
    for entry in paths.iter_all():
        if entry.suffix:
            entry.parent.mkdir(parents=True, exist_ok=True)
        else:
            entry.mkdir(parents=True, exist_ok=True)
    return paths


def _make_config(paths: WorkspacePaths, source: str) -> AppConfig:
    source_dir = paths.source_dir(source)
    source_dir.mkdir(parents=True, exist_ok=True)
    workspace_settings = WorkspaceSettings(
        root=paths.workspace,
        sources={
            source: WorkspaceSourceConfig(
                name=source,
                path=source_dir,
                enabled=True,
            )
        },
    )
    settings = ParserModuleSettings()
    return AppConfig(
        workspace_settings=workspace_settings,
        modules={"parser": settings},
    )


def _make_registry(
    settings: ParserModuleSettings,
    *,
    python_status: HealthStatus = HealthStatus.OK,
) -> HandlerRegistry:
    descriptors = (
        ParserHandlerDescriptor(
            name="text",
            version="test-text",
            display_name="Text",
        ),
        ParserHandlerDescriptor(
            name="python",
            version="test-python",
            display_name="Python",
            extensions=("py",),
            probe=lambda: HandlerProbeResult(
                status=python_status,
                summary=(
                    "dependency missing"
                    if python_status is HealthStatus.ERROR
                    else None
                ),
            ),
        ),
    )
    return HandlerRegistry(descriptors=descriptors, settings=settings)


def test_plan_source_collects_entries(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "alpha")
    settings = config.parser
    registry = _make_registry(settings)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
    )

    root = paths.source_dir("alpha")
    (root / "alpha.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "README.txt").write_text("hello world\n", encoding="utf-8")

    plan = service.plan_source(source="alpha")

    assert isinstance(plan, ParserBatchPlan)
    assert {entry.relative_path.as_posix() for entry in plan.entries} == {
        "alpha.py",
        "README.txt",
    }
    handler_map = {
        entry.relative_path.as_posix(): entry.handler.name
        for entry in plan.entries
    }
    assert handler_map["alpha.py"] == "python"
    assert handler_map["README.txt"] == "text"

    assert plan.metrics.files_discovered == 2
    assert plan.metrics.files_parsed == 2
    assert plan.metrics.files_failed == 0
    assert plan.metrics.fallbacks == 0
    assert plan.metrics.handlers_invoked["python"] == 1
    assert plan.metrics.handlers_invoked["text"] == 1
    assert plan.warnings == ()
    assert plan.errors == ()
    assert all(entry.file_hash for entry in plan.entries)


def test_plan_source_fallback_when_handler_unhealthy(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "beta")
    settings = config.parser
    registry = _make_registry(settings, python_status=HealthStatus.ERROR)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
    )

    root = paths.source_dir("beta")
    (root / "module.py").write_text("print('fallback')\n", encoding="utf-8")

    plan = service.plan_source(source="beta")

    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.handler.name == "text"
    assert plan.metrics.fallbacks == 1
    assert any("Fallback" in warning for warning in plan.warnings)


def test_plan_source_raises_when_disabled(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "gamma")
    settings = ParserModuleSettings(enabled=False)
    registry = _make_registry(settings)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
    )

    with pytest.raises(ParserModuleDisabledError):
        service.plan_source(source="gamma")


def test_record_run_updates_manifest_and_health(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "omega")
    settings = config.parser
    registry = _make_registry(settings)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
    )

    root = paths.source_dir("omega")
    (root / "sample.py").write_text("print('ok')\n", encoding="utf-8")

    plan = service.plan_source(source="omega")
    now = datetime.now(timezone.utc)
    run = service.build_run_record(
        plan=plan,
        batch_id="batch-123",
        summary="completed",
        warnings=None,
        errors=None,
        notes=("stay hydrated",),
        started_at=now,
        completed_at=now,
        metrics=ParserRunMetrics(files_discovered=1, files_parsed=1),
    )

    state = service.record_run(source="omega", run=run)
    assert state.last_batch_id == "batch-123"
    assert state.last_run_status is run.status
    assert state.last_run_summary == "completed"
    assert state.last_run_notes == ("stay hydrated",)
    assert state.metrics.files_parsed == 1
    assert state.handler_versions == plan.handler_versions

    manifest_path = paths.source_manifest_path("omega")
    assert manifest_path.exists()

    report = service.health_report("omega")
    assert report.status is run.status
    assert report.summary == "completed"
    assert report.last_refresh_at == run.completed_at
    assert tuple(report.actions) == state.last_run_notes
