"""Tests covering the parser CLI scaffolding."""

from __future__ import annotations

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


def test_parser_parse_scope_filters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    ) -> tuple[list[tuple[ParserPlanEntry, FileStageOutcome]], ParserRunMetrics]:
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
    assert "Summary: completed: files parsed=1; chunks inserted=1" in result.stdout

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
def test_parser_info_and_batches_unimplemented(tmp_path: Path) -> None:
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
    assert info_result.exit_code == 1
    assert "not available yet" in info_result.stdout

    batches_result = runner.invoke(
        app,
        ["parser", "batches", "--limit", "5"],
        env=env,
        catch_exceptions=False,
    )
    assert batches_result.exit_code == 1
    assert "not available yet" in batches_result.stdout


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
