"""Tests covering the parser CLI scaffolding."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest
import typer
from typer.testing import CliRunner

from raggd.cli import create_app
from raggd.cli.parser import (
    ParserCLIContext,
    ParserSessionGuard,
    _require_context,
    _parser_app,
    configure_parser_commands,
)
import raggd.modules.parser  # noqa: F401 - ensure module import coverage
from raggd.modules.db import DbLifecycleService
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.manifest import ManifestService
from raggd.modules import HealthStatus
from raggd.modules.parser import (
    FileStageOutcome,
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSelection,
    HandlerProbeResult,
    ParserBatchPlan,
    ParserManifestState,
    ParserPlanEntry,
    ParserRunMetrics,
    ParserService,
)
from raggd.source.config import SourceConfigError, SourceConfigStore
from raggd.core.paths import resolve_workspace


def _workspace_env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    return {
        "HOME": str(tmp_path),
        "RAGGD_WORKSPACE": str(workspace),
    }


def test_parser_parse_no_sources_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No sources configured" in result.stdout


def test_parser_parse_scope_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "sample.txt").write_text("hello", encoding="utf-8")

    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    seen_scopes: dict[str, tuple[Path, ...]] = {}
    source_dir = workspace / "sources" / "demo"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_file = source_dir / "sample.txt"
    source_file.write_text("hello", encoding="utf-8")
    assert source_file.exists()

    def fake_plan(
        self: ParserService,
        *,
        source: str,
        scope: Sequence[Path] | None = None,
    ) -> ParserBatchPlan:
        config = self._config.workspace_sources[source]
        resolved_scope = tuple(scope or ())
        seen_scopes[source] = resolved_scope
        return ParserBatchPlan(
            source=source,
            root=config.path,
            entries=(),
            warnings=(),
            errors=(),
            metrics=ParserRunMetrics(),
        )

    monkeypatch.setattr("raggd.cli.parser.ParserService.plan_source", fake_plan)

    result = runner.invoke(
        app,
        ["parser", "parse", "demo", "sample.txt", "docs/missing"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Scope filter missing" in result.stdout
    scope_paths = seen_scopes["demo"]
    assert scope_paths == (source_file.resolve(),)


def test_parser_parse_records_manifest_without_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    def fake_plan(
        self: ParserService,
        *,
        source: str,
        scope: Sequence[Path] | None = None,
    ) -> ParserBatchPlan:
        config = self._config.workspace_sources[source]
        return ParserBatchPlan(
            source=source,
            root=config.path,
            entries=(),
            warnings=("planner warning",),
            errors=(),
            metrics=ParserRunMetrics(),
        )

    monkeypatch.setattr("raggd.cli.parser.ParserService.plan_source", fake_plan)

    build_calls: list[dict[str, object]] = []
    original_build = ParserService.build_run_record

    def spy_build(self: ParserService, **kwargs: object):
        build_calls.append(kwargs)
        return original_build(self, **kwargs)

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.build_run_record",
        spy_build,
    )

    record_calls: list[tuple[str, object]] = []

    def fake_record(
        self: ParserService,
        *,
        source: str,
        run,
    ) -> ParserManifestState:
        record_calls.append((source, run))
        return ParserManifestState(
            last_batch_id=run.batch_id,
            last_run_summary=run.summary,
            last_run_status=run.status,
            handler_versions=run.handler_versions,
            metrics=run.metrics.copy(),
        )

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.record_run",
        fake_record,
    )

    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "planner warning" in result.stdout
    assert "Summary: no changes" in result.stdout

    assert len(build_calls) == 1
    assert build_calls[0]["batch_id"] is None
    assert len(record_calls) == 1
    recorded_source, recorded_run = record_calls[0]
    assert recorded_source == "demo"
    assert recorded_run.summary == "no changes"


def test_parser_parse_emits_vector_sync_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    source_dir = workspace / "sources" / "demo"

    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    source_dir.mkdir(parents=True, exist_ok=True)
    file_path = source_dir / "sample.txt"
    file_path.write_text("vector testing", encoding="utf-8")

    def fake_plan(
        self: ParserService,
        *,
        source: str,
        scope: Sequence[Path] | None = None,
    ) -> ParserBatchPlan:
        config = self._config.workspace_sources[source]
        descriptor = self.registry.descriptors()["text"]
        selection = HandlerSelection(
            handler=descriptor,
            resolved_via="extension",
            fallback=False,
            probe=HandlerProbeResult(status=HealthStatus.OK),
        )
        metrics = ParserRunMetrics(files_discovered=1, files_parsed=1)
        return ParserBatchPlan(
            source=source,
            root=config.path,
            entries=(
                ParserPlanEntry(
                    absolute_path=file_path,
                    relative_path=Path("sample.txt"),
                    handler=descriptor,
                    selection=selection,
                    file_hash="hash-sample",
                ),
            ),
            warnings=(),
            errors=(),
            metrics=metrics,
            handler_versions={descriptor.name: descriptor.version},
        )

    monkeypatch.setattr("raggd.cli.parser.ParserService.plan_source", fake_plan)

    class _DummyEncoder:
        def encode(self, value: str) -> list[int]:
            return [0] * len(value)

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.token_encoder",
        lambda self: _DummyEncoder(),
    )

    def fake_create_handler(self, handler_name: str, *, context):
        descriptor = self.descriptors()[handler_name]

        class _Handler:
            name = descriptor.name
            version = descriptor.version
            display_name = descriptor.display_name

            def parse(self, *, path: Path, context) -> HandlerResult:
                text = path.read_text(encoding="utf-8")
                handler_file = HandlerFile(path=path, language=descriptor.name)
                chunk = HandlerChunk(
                    chunk_id="chunk-1",
                    text=text,
                    token_count=len(text.split()),
                    start_offset=0,
                    end_offset=len(text),
                )
                return HandlerResult(
                    file=handler_file,
                    chunks=(chunk,),
                    warnings=(),
                    errors=(),
                )

        return _Handler()

    monkeypatch.setattr(
        "raggd.modules.parser.registry.HandlerRegistry.create_handler",
        fake_create_handler,
    )

    def fake_stage(
        self: ParserService,
        *,
        source: str,
        batch_id: str,
        plan: ParserBatchPlan,
        results,
        batch_ref: str | None = None,
        **_: object,
    ) -> tuple[
        list[tuple[ParserPlanEntry, FileStageOutcome]],
        ParserRunMetrics,
    ]:
        metrics = plan.metrics.copy()
        metrics.chunks_emitted = len(results)
        outcomes: list[tuple[ParserPlanEntry, FileStageOutcome]] = []
        for idx, (entry, _result) in enumerate(results, start=1):
            outcomes.append(
                (
                    entry,
                    FileStageOutcome(
                        file_id=idx,
                        symbols_written=1,
                        symbols_reused=0,
                        chunks_inserted=1,
                        chunks_reused=0,
                    ),
                )
            )
        return outcomes, metrics

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.stage_batch",
        fake_stage,
    )

    build_calls: list[dict[str, object]] = []
    original_build = ParserService.build_run_record

    def spy_build(self: ParserService, **kwargs: object):
        build_calls.append(kwargs)
        return original_build(self, **kwargs)

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.build_run_record",
        spy_build,
    )

    record_calls: list[tuple[str, object]] = []

    def fake_record(
        self: ParserService,
        *,
        source: str,
        run,
    ) -> ParserManifestState:
        record_calls.append((source, run))
        return ParserManifestState(
            last_batch_id=run.batch_id,
            last_run_summary=run.summary,
            last_run_status=run.status,
            handler_versions=run.handler_versions,
            metrics=run.metrics.copy(),
        )

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.record_run",
        fake_record,
    )

    result = runner.invoke(
        app,
        ["parser", "parse", "demo"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "Parse completed (batch" in result.stdout
    assert (
        "Summary: completed: files parsed=1; chunks inserted=1" in result.stdout
    )

    expected_note = (
        "Vector indexes are not updated automatically; run "
        "`raggd vdb sync demo` to refresh embeddings."
    )
    assert expected_note in result.stdout

    assert len(build_calls) == 1
    assert build_calls[0]["batch_id"] is not None
    assert len(record_calls) == 1
    recorded_source, recorded_run = record_calls[0]
    assert recorded_source == "demo"
    assert expected_note in recorded_run.notes


def test_parser_parse_module_disabled(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(
        app,
        ["init", "--disable-module", "parser"],
        env=env,
        catch_exceptions=False,
    )
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Parser module is disabled" in result.stdout


def test_parser_info_no_sources_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    info_result = runner.invoke(
        app,
        ["parser", "info"],
        env=env,
        catch_exceptions=False,
    )

    assert info_result.exit_code == 0
    assert "No sources configured" in info_result.stdout


def test_parser_info_unknown_source(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    info_result = runner.invoke(
        app,
        ["parser", "info", "missing"],
        env=env,
        catch_exceptions=False,
    )

    assert info_result.exit_code == 1
    assert "Unknown source: missing" in info_result.stdout


def test_parser_info_reports_manifest_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    app = create_app()

    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    from datetime import datetime, timezone

    from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
    from raggd.modules.parser.registry import HandlerAvailability

    run_timestamp = datetime(2025, 10, 8, 8, 15, tzinfo=timezone.utc)
    batch_id = generate_uuid7(when=run_timestamp)
    metrics = ParserRunMetrics(
        files_parsed=2,
        files_reused=1,
        chunks_emitted=3,
        chunks_reused=1,
        handlers_invoked={"text": 2, "python": 1},
    )
    manifest_state = ParserManifestState(
        last_batch_id=str(batch_id),
        last_run_status=HealthStatus.DEGRADED,
        last_run_started_at=datetime(2025, 10, 8, 8, 15, tzinfo=timezone.utc),
        last_run_completed_at=datetime(2025, 10, 8, 8, 17, tzinfo=timezone.utc),
        last_run_summary="parse completed",
        last_run_warnings=("install tree-sitter runtime",),
        handler_versions={"text": "1.0.0", "python": "3.11"},
        metrics=metrics,
    )

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.load_manifest_state",
        lambda self, source: manifest_state,
    )

    handler_availability = (
        HandlerAvailability(
            name="text",
            enabled=True,
            status=HealthStatus.OK,
            summary=None,
            warnings=(),
        ),
        HandlerAvailability(
            name="markdown",
            enabled=True,
            status=HealthStatus.DEGRADED,
            summary="tree-sitter missing",
            warnings=("install tree-sitter runtime",),
        ),
        HandlerAvailability(
            name="python",
            enabled=False,
            status=HealthStatus.UNKNOWN,
            summary=None,
            warnings=(),
        ),
    )

    monkeypatch.setattr(
        "raggd.cli.parser.ParserService.handler_availability",
        lambda self: handler_availability,
    )

    info_result = runner.invoke(
        app,
        ["parser", "info"],
        env=env,
        catch_exceptions=False,
    )

    assert info_result.exit_code == 0, info_result.stdout
    stdout = info_result.stdout
    short_id = short_uuid7(batch_id).value
    assert "Parser info for demo" in stdout
    assert f"Last batch id: {short_id}" in stdout
    assert "Last run status: degraded" in stdout
    assert "Last run summary: parse completed" in stdout
    assert "Last run warnings" in stdout
    assert "install tree-sitter runtime" in stdout
    assert (
        "Last run metrics: parsed=2 reused=1 chunks=3 reused_chunks=1" in stdout
    )
    assert "Handler coverage:" in stdout
    assert "text: count=2, version=1.0.0" in stdout
    assert "Handler availability:" in stdout
    assert "markdown: enabled (status=degraded" in stdout
    assert "python: disabled" in stdout
    assert "Dependency gaps:" in stdout
    assert "tree-sitter runtime" in stdout
    assert "Configuration overrides: none" in stdout


def test_parser_batches_no_sources_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    batches_result = runner.invoke(
        app,
        ["parser", "batches"],
        env=env,
        catch_exceptions=False,
    )

    assert batches_result.exit_code == 0
    assert "No sources configured" in batches_result.stdout


def test_parser_batches_unknown_source(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["parser", "batches", "mystery"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Unknown source: mystery" in result.stdout


def test_parser_batches_lists_recent_batches(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    target_dir = tmp_path / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    init_source = runner.invoke(
        app,
        [
            "source",
            "init",
            "demo",
            "--target",
            str(target_dir),
            "--force-refresh",
        ],
        env=env,
        catch_exceptions=False,
    )
    assert init_source.exit_code == 0, init_source.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    paths = resolve_workspace(workspace_override=workspace)
    store = SourceConfigStore(config_path=paths.config_file)
    config = store.load()

    manifest_service = ManifestService(workspace=paths)
    db_service = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest_service,
    )

    db_path = db_service.ensure("demo")

    first_generated = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    second_generated = datetime(2024, 1, 2, 12, tzinfo=timezone.utc)
    batch_a = str(generate_uuid7(when=first_generated))
    batch_b = str(generate_uuid7(when=second_generated))

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO batches (id, ref, generated_at, notes) VALUES (?, ?, ?, ?)",
            (batch_a, "ref-a", first_generated.isoformat(), "initial sync"),
        )
        connection.execute(
            "INSERT INTO batches (id, ref, generated_at, notes) VALUES (?, ?, ?, ?)",
            (batch_b, "ref-b", second_generated.isoformat(), "follow-up sync"),
        )

        file_a = connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, mtime_ns, "
                "size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (batch_a, "src/alpha.py", "python", "sha-alpha", 111, 100),
        ).lastrowid

        file_b1 = connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, mtime_ns, "
                "size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (batch_b, "src/beta.py", "python", "sha-beta", 222, 200),
        ).lastrowid

        file_b2 = connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, mtime_ns, "
                "size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (batch_b, "src/gamma.py", "markdown", "sha-gamma", 333, 300),
        ).lastrowid

        symbol_a = connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_a,
                "function",
                "alpha:main",
                1,
                5,
                "sym-alpha",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                15,
                batch_a,
                batch_a,
            ),
        ).lastrowid

        symbol_b1 = connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_b1,
                "class",
                "beta:Widget",
                10,
                40,
                "sym-beta-1",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                30,
                batch_b,
                batch_b,
            ),
        ).lastrowid

        symbol_b2 = connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_b2,
                "heading",
                "gamma:section",
                1,
                8,
                "sym-beta-2",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                12,
                batch_b,
                batch_b,
            ),
        ).lastrowid

        connection.execute(
            (
                "INSERT INTO chunk_slices (batch_id, file_id, symbol_id, "
                "parent_symbol_id, chunk_id, handler_name, handler_version, "
                "part_index, part_total, start_line, end_line, start_byte, "
                "end_byte, token_count, content_hash, content_norm_hash, "
                "content_text, overflow_is_truncated, overflow_reason, "
                "metadata_json, created_at, updated_at, first_seen_batch, "
                "last_seen_batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                batch_a,
                file_a,
                symbol_a,
                None,
                "chunk-alpha-1",
                "python",
                "1.0.0",
                0,
                1,
                1,
                5,
                0,
                120,
                42,
                "chunk-alpha-hash",
                None,
                "alpha body",
                0,
                None,
                None,
                first_generated.isoformat(),
                first_generated.isoformat(),
                batch_a,
                batch_a,
            ),
        )

        for index, (file_id, symbol_id, chunk_id, tokens) in enumerate(
            (
                (file_b1, symbol_b1, "chunk-beta-1", 58),
                (file_b1, symbol_b1, "chunk-beta-2", 31),
                (file_b2, symbol_b2, "chunk-gamma-1", 17),
            )
        ):
            connection.execute(
                (
                    "INSERT INTO chunk_slices (batch_id, file_id, symbol_id, "
                    "parent_symbol_id, chunk_id, handler_name, handler_version, "
                    "part_index, part_total, start_line, end_line, start_byte, "
                    "end_byte, token_count, content_hash, content_norm_hash, "
                    "content_text, overflow_is_truncated, overflow_reason, "
                    "metadata_json, created_at, updated_at, first_seen_batch, "
                    "last_seen_batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    batch_b,
                    file_id,
                    symbol_id,
                    None,
                    chunk_id,
                    "python" if file_id == file_b1 else "markdown",
                    "1.0.0",
                    0,
                    1,
                    1 + index,
                    10 + index,
                    0,
                    200 + index,
                    tokens,
                    f"hash-{chunk_id}",
                    None,
                    f"body-{chunk_id}",
                    0,
                    None,
                    None,
                    second_generated.isoformat(),
                    second_generated.isoformat(),
                    batch_b,
                    batch_b,
                ),
            )

    parser_service = ParserService(
        workspace=paths,
        config=config,
        manifest_service=manifest_service,
        db_service=db_service,
    )

    plan = ParserBatchPlan(
        source="demo",
        root=paths.source_dir("demo"),
        entries=(),
        warnings=(),
        errors=(),
        metrics=ParserRunMetrics(files_discovered=2, files_parsed=2, chunks_emitted=3),
    )
    run = parser_service.build_run_record(
        plan=plan,
        batch_id=batch_b,
        status=HealthStatus.DEGRADED,
        summary="follow-up sync",
        warnings=("handler fallback",),
        errors=(),
        notes=("vector sync required",),
        started_at=second_generated,
        completed_at=second_generated,
    )
    parser_service.record_run(source="demo", run=run)

    batches_result = runner.invoke(
        app,
        ["parser", "batches", "--limit", "5"],
        env=env,
        catch_exceptions=False,
    )

    assert batches_result.exit_code == 0, batches_result.stdout
    stdout = batches_result.stdout
    short_latest = short_uuid7(uuid.UUID(batch_b)).value

    assert "Parser batches for demo (showing up to 5)" in stdout
    assert "status=degraded" in stdout
    assert "latest" in stdout
    assert "files: 2  symbols: 2  chunks: 3" in stdout
    assert "notes: follow-up sync" in stdout
    assert f"batch {short_latest}" in stdout
    assert "status=unknown" in stdout
    assert "notes: initial sync" in stdout


def test_parser_remove_unimplemented(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    wildcard_result = runner.invoke(
        app,
        ["parser", "remove", "--force"],
        env=env,
        catch_exceptions=False,
    )
    assert wildcard_result.exit_code == 1
    assert "not available yet" in wildcard_result.stdout

    explicit_result = runner.invoke(
        app,
        ["parser", "remove", "demo", "batch-1"],
        env=env,
        catch_exceptions=False,
    )
    assert explicit_result.exit_code == 1
    assert "not available yet" in explicit_result.stdout


def test_parser_workspace_missing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Run `raggd init` first" in result.stdout


def test_parser_workspace_file_override(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    workspace_path = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_path.write_text("", encoding="utf-8")

    app = create_app()
    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace error" in result.stdout


def test_parser_workspace_config_load_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)
    workspace_path = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    workspace_path.mkdir(parents=True, exist_ok=True)
    config_path = workspace_path / "raggd.toml"
    config_path.write_text("", encoding="utf-8")

    def boom(self: object) -> object:
        raise SourceConfigError("boom")

    monkeypatch.setattr("raggd.cli.parser.SourceConfigStore.load", boom)

    app = create_app()
    result = runner.invoke(
        app,
        ["parser", "parse"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to load workspace config" in result.stdout


def test_parser_require_context_guard() -> None:
    click_command = typer.main.get_command(_parser_app)
    ctx = typer.Context(click_command)
    with pytest.raises(typer.Exit):
        _require_context(ctx)


def test_parser_configure_context_initializes_services(tmp_path: Path) -> None:
    runner = CliRunner()
    env = _workspace_env(tmp_path)

    app = create_app()
    init_result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert init_result.exit_code == 0, init_result.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    click_command = typer.main.get_command(_parser_app)
    ctx = typer.Context(click_command)

    configure_parser_commands(
        ctx,
        workspace=workspace,
        log_level=None,
    )

    context = _require_context(ctx)
    assert isinstance(context, ParserCLIContext)
    assert context.paths.workspace == workspace
    assert isinstance(context.store, SourceConfigStore)
    assert isinstance(context.manifest, ManifestService)
    assert isinstance(context.db_service, DbLifecycleService)
    assert isinstance(context.parser_service, ParserService)
    assert isinstance(context.session_guard, ParserSessionGuard)
    locks_root = workspace / ".locks" / "parser"
    assert locks_root.exists()
    assert context.session_guard.root == locks_root
