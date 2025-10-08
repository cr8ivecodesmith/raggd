"""Tests for parser module health integration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from raggd.core.config import (
    AppConfig,
    ParserModuleSettings,
    WorkspaceSettings,
    PARSER_MODULE_KEY,
)
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.modules.db import DbLifecycleService
from raggd.modules.manifest import ManifestService
from raggd.modules.parser import parser_health_hook
from raggd.modules.parser.models import ParserManifestState, ParserRunMetrics
from raggd.modules.parser.persistence import ChunkSliceRepository, ChunkSliceRow
from raggd.source.models import WorkspaceSourceConfig


def _make_workspace(tmp_path: Path) -> WorkspacePaths:
    root = tmp_path / "workspace"
    paths = WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )
    for entry in paths.iter_all():
        if entry.suffix:
            entry.parent.mkdir(parents=True, exist_ok=True)
        else:
            entry.mkdir(parents=True, exist_ok=True)
    return paths


def _make_config(paths: WorkspacePaths, *, enabled: bool = True) -> AppConfig:
    source_dir = paths.source_dir("alpha")
    source_dir.mkdir(parents=True, exist_ok=True)
    workspace_settings = WorkspaceSettings(
        root=paths.workspace,
        sources={
            "alpha": WorkspaceSourceConfig(
                name="alpha",
                path=source_dir,
                enabled=True,
            )
        },
    )
    parser_settings = ParserModuleSettings(enabled=enabled)
    return AppConfig(
        workspace_settings=workspace_settings,
        modules={"parser": parser_settings},
    )


def _write_parser_manifest(
    *,
    paths: WorkspacePaths,
    batch_id: str | None,
    status: HealthStatus,
    summary: str | None = None,
    metrics: ParserRunMetrics | None = None,
) -> ParserManifestState:
    manifest_service = ManifestService(workspace=paths)
    _, parser_key = manifest_service.settings.module_key(PARSER_MODULE_KEY)
    completed = None
    if batch_id is not None:
        completed = datetime.now(timezone.utc)
    state = ParserManifestState(
        enabled=True,
        last_batch_id=batch_id,
        last_run_status=status,
        last_run_summary=summary,
        last_run_completed_at=completed,
        metrics=metrics or ParserRunMetrics(),
    )

    def _mutate(snapshot):
        modules = snapshot.ensure_modules()
        modules[parser_key] = state.to_mapping()

    manifest_service.write("alpha", mutate=_mutate)
    return state


def _insert_chunk_slices(
    *,
    paths: WorkspacePaths,
    batch_id: str,
    part_total: int,
    part_indices: tuple[int, ...],
) -> None:
    db_service = DbLifecycleService(workspace=paths)
    db_path = db_service.ensure("alpha")
    timestamp = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        now = timestamp.isoformat()
        connection.execute(
            "INSERT INTO batches (id, ref, generated_at, notes)"
            " VALUES (?, ?, ?, ?)",
            (batch_id, None, now, None),
        )
        cursor = connection.execute(
            "INSERT INTO files (batch_id, repo_path, lang, file_sha,"
            " mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, "docs/readme.md", "markdown", "deadbeef", 0, 10),
        )
        file_id = cursor.lastrowid
        repository = ChunkSliceRepository()
        rows = []
        for index in part_indices:
            rows.append(
                ChunkSliceRow(
                    batch_id=batch_id,
                    file_id=file_id,
                    symbol_id=None,
                    parent_symbol_id=None,
                    chunk_id="chunk-1",
                    handler_name="markdown",
                    handler_version="1.0.0",
                    part_index=index,
                    part_total=part_total,
                    start_line=None,
                    end_line=None,
                    start_byte=0,
                    end_byte=0,
                    token_count=1,
                    content_hash="hash",
                    content_norm_hash="hash",
                    content_text="content",
                    overflow_is_truncated=False,
                    overflow_reason=None,
                    metadata_json=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                    first_seen_batch=batch_id,
                    last_seen_batch=batch_id,
                )
            )
        repository.upsert_many(connection, rows)


def test_parser_health_hook_reports_disabled_module(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths, enabled=False)
    handle = SimpleNamespace(paths=paths, config=config)

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.name == "parser-module"
    assert report.status == HealthStatus.UNKNOWN
    assert "disabled" in (report.summary or "")


def test_parser_health_hook_reports_pending_parse(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == HealthStatus.UNKNOWN
    assert "has not completed" in (report.summary or "")
    assert report.actions == (
        "Run `raggd parser parse alpha` to rebuild parser data.",
    )


def test_parser_health_hook_reports_chunk_integrity_error(
    tmp_path: Path,
) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    _write_parser_manifest(
        paths=paths,
        batch_id="batch-err",
        status=HealthStatus.OK,
        summary="complete",
    )
    _insert_chunk_slices(
        paths=paths,
        batch_id="batch-err",
        part_total=2,
        part_indices=(0,),
    )

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == HealthStatus.ERROR
    assert "not contiguous" in (report.summary or "")
    assert "parser parse" in " ".join(report.actions)


def test_parser_health_hook_reports_lock_wait_warning(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    metrics = ParserRunMetrics(lock_wait_seconds=7.5)
    _write_parser_manifest(
        paths=paths,
        batch_id="batch-lock",
        status=HealthStatus.OK,
        summary="parse complete",
        metrics=metrics,
    )
    _insert_chunk_slices(
        paths=paths,
        batch_id="batch-lock",
        part_total=1,
        part_indices=(0,),
    )

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == HealthStatus.DEGRADED
    assert report.summary is not None
    assert "lock waits" in report.summary
    assert any(
        "parser concurrency telemetry" in action for action in report.actions
    )


def test_parser_health_hook_reports_contention_error(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    metrics = ParserRunMetrics(lock_contention_events=12)
    _write_parser_manifest(
        paths=paths,
        batch_id="batch-contention",
        status=HealthStatus.OK,
        summary="parse complete",
        metrics=metrics,
    )
    _insert_chunk_slices(
        paths=paths,
        batch_id="batch-contention",
        part_total=1,
        part_indices=(0,),
    )

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == HealthStatus.ERROR
    assert report.summary is not None
    assert "lock contention" in report.summary
    assert any(
        "parser concurrency telemetry" in action for action in report.actions
    )


def test_parser_health_hook_reports_healthy_state(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    state = _write_parser_manifest(
        paths=paths,
        batch_id="batch-ok",
        status=HealthStatus.OK,
        summary="parse completed",
    )
    _insert_chunk_slices(
        paths=paths,
        batch_id="batch-ok",
        part_total=1,
        part_indices=(0,),
    )

    reports = parser_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == HealthStatus.OK
    assert report.summary == "parse completed"
    assert report.actions == ()
    assert report.last_refresh_at == state.last_run_completed_at
