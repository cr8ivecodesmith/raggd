"""Tests covering the parser CLI scaffolding."""

from __future__ import annotations

from pathlib import Path

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
from raggd.modules.parser import ParserService
from raggd.source.config import SourceConfigError, SourceConfigStore


def _workspace_env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    return {
        "HOME": str(tmp_path),
        "RAGGD_WORKSPACE": str(workspace),
    }


def test_parser_parse_reports_unimplemented(tmp_path: Path) -> None:
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

    assert result.exit_code == 1
    assert "not available yet" in result.stdout


def test_legacy_parse_command_is_deprecated() -> None:
    runner = CliRunner()
    app = create_app()

    result = runner.invoke(app, ["parse"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "has moved" in result.stdout


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
