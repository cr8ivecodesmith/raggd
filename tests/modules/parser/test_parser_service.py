"""Tests for :mod:`raggd.modules.parser.service`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from raggd.core.config import AppConfig, ParserModuleSettings, WorkspaceSettings
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules import HealthStatus
from raggd.modules.parser import (
    ParserBatchPlan,
    ParserModuleDisabledError,
    ParserPlanEntry,
    ParserService,
)
from raggd.modules.parser.handlers.base import (
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSymbol,
)
from raggd.modules.parser.models import ParserRunMetrics
from raggd.modules.parser.registry import (
    HandlerProbeResult,
    HandlerRegistry,
    ParserHandlerDescriptor,
)
from raggd.source.models import WorkspaceSourceConfig
from structlog.testing import capture_logs


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


def _build_handler_result(
    entry: ParserPlanEntry,
    *,
    text: str,
    token_count: int,
) -> HandlerResult:
    handler_file = HandlerFile(
        path=entry.relative_path,
        language=entry.handler.name,
        metadata={"size_bytes": len(text)},
    )

    symbol = HandlerSymbol(
        symbol_id="module:artifact",
        name="artifact",
        kind="module",
        start_offset=0,
        end_offset=len(text),
        metadata={"start_line": 1, "end_line": 1},
    )

    chunk = HandlerChunk(
        chunk_id=f"{entry.handler.name}:artifact:0:{len(text)}",
        text=text,
        token_count=token_count,
        start_offset=0,
        end_offset=len(text),
        part_index=0,
        parent_symbol_id=symbol.symbol_id,
        metadata={"start_line": 1, "end_line": 1, "part_total": 1},
    )

    return HandlerResult(
        file=handler_file,
        symbols=(symbol,),
        chunks=(chunk,),
    )


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
    (root / "db.sqlite3").write_text("", encoding="utf-8")
    (root / "db.sqlite3-wal").write_text("", encoding="utf-8")
    (root / "manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "manifest.json.20250101T000000Z.bak").write_text(
        "{}\n",
        encoding="utf-8",
    )

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


def test_plan_source_logs_fallback_and_queue_depth(tmp_path: Path) -> None:
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

    with capture_logs() as captured:
        plan = service.plan_source(source="beta")

    assert plan.metrics.queue_depth == len(plan.entries)

    fallback_events = [
        event
        for event in captured
        if event["event"] == "parser-handler-fallback"
    ]
    assert fallback_events
    assert fallback_events[0]["handler"] == "text"

    degraded_events = [
        event
        for event in captured
        if event["event"] == "parser-handler-degraded"
    ]
    assert any(event["handler"] == "python" for event in degraded_events)


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


def test_stage_batch_persists_results(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "alpha")
    settings = config.parser
    registry = _make_registry(settings)
    db_service = DbLifecycleService(workspace=paths)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
        db_service=db_service,
    )

    root = paths.source_dir("alpha")
    file_path = root / "sample.py"
    content = "print('hello')\n"
    file_path.write_text(content, encoding="utf-8")

    plan = service.plan_source(source="alpha")
    assert len(plan.entries) == 1
    entry = plan.entries[0]

    result = _build_handler_result(entry, text=content, token_count=3)

    outcomes, metrics = service.stage_batch(
        source="alpha",
        batch_id="batch-1",
        plan=plan,
        results=((entry, result),),
        batch_ref="ref-1",
    )

    assert len(outcomes) == 1
    staged_entry, outcome = outcomes[0]
    assert staged_entry == entry
    assert outcome.symbols_written == 1
    assert outcome.symbols_reused == 0
    assert outcome.chunks_inserted == 1
    assert outcome.chunks_reused == 0

    assert metrics.chunks_emitted == 1
    assert metrics.chunks_reused == 0
    assert metrics.files_reused == 0

    db_path = db_service.ensure("alpha")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        file_row = connection.execute(
            "SELECT repo_path, batch_id FROM files"
        ).fetchone()
        assert file_row["repo_path"] == entry.relative_path.as_posix()
        assert file_row["batch_id"] == "batch-1"

        chunk_row = connection.execute(
            (
                "SELECT handler_name, chunk_id,\n"
                "       first_seen_batch, last_seen_batch\n"
                "FROM chunk_slices"
            )
        ).fetchone()
        assert chunk_row["handler_name"] == entry.handler.name
        assert chunk_row["chunk_id"] == result.chunks[0].chunk_id
        assert chunk_row["first_seen_batch"] == "batch-1"
        assert chunk_row["last_seen_batch"] == "batch-1"


def test_stage_batch_marks_reused_files(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, "alpha")
    settings = config.parser
    registry = _make_registry(settings)
    db_service = DbLifecycleService(workspace=paths)
    service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        registry=registry,
        db_service=db_service,
    )

    root = paths.source_dir("alpha")
    file_path = root / "sample.py"
    content = "print('hello again')\n"
    file_path.write_text(content, encoding="utf-8")

    first_plan = service.plan_source(source="alpha", scope=(file_path,))
    first_entry = first_plan.entries[0]
    first_result = _build_handler_result(
        first_entry,
        text=content,
        token_count=4,
    )

    service.stage_batch(
        source="alpha",
        batch_id="batch-1",
        plan=first_plan,
        results=((first_entry, first_result),),
    )

    second_plan = service.plan_source(source="alpha", scope=(file_path,))
    second_entry = second_plan.entries[0]
    second_result = _build_handler_result(
        second_entry,
        text=content,
        token_count=4,
    )

    outcomes, metrics = service.stage_batch(
        source="alpha",
        batch_id="batch-2",
        plan=second_plan,
        results=((second_entry, second_result),),
    )

    assert metrics.files_reused == 1
    assert metrics.chunks_emitted == 0
    assert metrics.chunks_reused == len(second_result.chunks)

    _, outcome = outcomes[0]
    assert outcome.chunks_inserted == 0
    assert outcome.chunks_reused == len(second_result.chunks)

    db_path = db_service.ensure("alpha")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        chunk_rows = connection.execute(
            "SELECT first_seen_batch, last_seen_batch FROM chunk_slices"
        ).fetchall()
        assert {row["first_seen_batch"] for row in chunk_rows} == {"batch-1"}
        assert {row["last_seen_batch"] for row in chunk_rows} == {"batch-2"}
