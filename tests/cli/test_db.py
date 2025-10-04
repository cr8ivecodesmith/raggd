"""CLI tests for the `raggd db` command group."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import tomlkit
import typer
from tomlkit.items import Table
from typer.testing import CliRunner, Result

from raggd.cli.db import (
    _require_context,
    _resolve_workspace_override,
    create_db_app,
)
from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import DbLifecycleError, DbLifecycleNotImplementedError
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.source.config import SourceConfigError, SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


def _write_migration(
    directory: Path,
    identifier,
    *,
    up: str,
    down: str | None = None,
) -> str:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(f"-- uuid7: {identifier}\n{up}\n", encoding="utf-8")
    if down is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(
            f"-- uuid7: {identifier}\n{down}\n",
            encoding="utf-8",
        )
    return short


@dataclass(slots=True)
class _WorkspaceContext:
    runner: CliRunner
    workspace: Path
    paths: WorkspacePaths
    next_short: str

    def invoke(self, *args: str) -> Result:
        app = create_db_app()
        return self.runner.invoke(
            app,
            ["--workspace", str(self.workspace), *args],
            catch_exceptions=False,
        )


def _prepare_workspace(
    tmp_path: Path,
    *,
    ensure_auto_upgrade: bool = True,
    sources: tuple[str, ...] = ("demo",),
) -> _WorkspaceContext:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    init_workspace(workspace=workspace)
    paths = resolve_workspace(workspace_override=workspace)
    store = SourceConfigStore(config_path=paths.config_file)

    for name in sources:
        source_dir = paths.source_dir(name)
        source_dir.mkdir(parents=True, exist_ok=True)
        store.upsert(
            WorkspaceSourceConfig(
                name=name,
                path=source_dir,
                enabled=True,
            )
        )

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    _write_migration(
        migrations_dir,
        bootstrap_uuid,
        up="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    next_short = _write_migration(
        migrations_dir,
        next_uuid,
        up="ALTER TABLE example ADD COLUMN name TEXT;",
        down="ALTER TABLE example DROP COLUMN name;",
    )

    config_text = paths.config_file.read_text(encoding="utf-8")
    document = tomlkit.parse(config_text)
    db_table = document.get("db")
    if not isinstance(db_table, Table):
        db_table = tomlkit.table()
    db_table["migrations_path"] = migrations_dir.as_posix()
    if not ensure_auto_upgrade:
        db_table["ensure_auto_upgrade"] = False
    document["db"] = db_table
    paths.config_file.write_text(tomlkit.dumps(document), encoding="utf-8")

    return _WorkspaceContext(
        runner=runner,
        workspace=workspace,
        paths=paths,
        next_short=next_short,
    )


def test_db_cli_ensure_creates_database(tmp_path: Path) -> None:
    context = _prepare_workspace(tmp_path)

    result = context.invoke("ensure", "demo")

    assert result.exit_code == 0, result.stdout

    db_path = context.paths.source_database_path("demo")
    manifest_path = context.paths.source_manifest_path("demo")

    assert db_path.exists()
    assert manifest_path.exists()


def test_db_cli_upgrade_downgrade_and_info(tmp_path: Path) -> None:
    context = _prepare_workspace(tmp_path, ensure_auto_upgrade=False)

    ensure_result = context.invoke("ensure", "demo")
    assert ensure_result.exit_code == 0, ensure_result.stdout

    upgrade_result = context.invoke("upgrade", "demo", "--steps", "1")
    assert upgrade_result.exit_code == 0, upgrade_result.stdout
    assert "Upgraded database for demo" in upgrade_result.stdout

    info_result = context.invoke("info", "demo", "--schema")
    assert info_result.exit_code == 0, info_result.stdout
    assert "Database info for demo" in info_result.stdout
    assert "schema_meta" in info_result.stdout
    assert context.next_short in info_result.stdout

    downgrade_result = context.invoke("downgrade", "demo", "--steps", "1")
    assert downgrade_result.exit_code == 0, downgrade_result.stdout
    assert "Downgraded database for demo" in downgrade_result.stdout


def test_db_cli_vacuum_run_and_reset(tmp_path: Path) -> None:
    context = _prepare_workspace(tmp_path)
    sql_path = tmp_path / "script.sql"
    sql_path.write_text(
        "INSERT INTO example(name) VALUES('from-cli');\n",
        encoding="utf-8",
    )

    ensure_result = context.invoke("ensure", "demo")
    assert ensure_result.exit_code == 0, ensure_result.stdout

    vacuum_result = context.invoke("vacuum", "demo", "--concurrency", "auto")
    assert vacuum_result.exit_code == 0, vacuum_result.stdout
    assert "Vacuum triggered for demo" in vacuum_result.stdout

    run_result = context.invoke(
        "run",
        str(sql_path),
        "demo",
        "--autocommit",
    )
    assert run_result.exit_code == 0, run_result.stdout
    assert f"Executed {sql_path}" in run_result.stdout

    reset_result = context.invoke("reset", "demo", "--force")
    assert reset_result.exit_code == 0, reset_result.stdout
    db_path = context.paths.source_database_path("demo")
    assert db_path.exists()


def test_db_cli_handles_missing_sources(tmp_path: Path) -> None:
    context = _prepare_workspace(tmp_path, sources=())

    result = context.invoke("ensure")
    assert result.exit_code == 0
    assert "No sources configured" in result.stdout


def test_db_cli_reset_prompt_cancelled(tmp_path: Path, monkeypatch) -> None:
    context = _prepare_workspace(tmp_path)

    monkeypatch.setattr("raggd.cli.db.typer.confirm", lambda *_, **__: False)

    result = context.invoke("reset", "demo")
    assert result.exit_code == 1
    assert "Operation cancelled" in result.stdout


def test_db_cli_reports_not_implemented(monkeypatch, tmp_path: Path) -> None:
    class _FailingService:
        def __init__(self, **_: object) -> None:
            pass

        def ensure(self, _: str) -> Path:
            raise DbLifecycleNotImplementedError("not available")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _FailingService)

    result = context.invoke("ensure", "demo")
    assert result.exit_code == 1
    assert "not available" in result.stdout


def test_db_cli_reports_generic_failure(monkeypatch, tmp_path: Path) -> None:
    class _FailingService:
        def __init__(self, **_: object) -> None:
            pass

        def ensure(self, _: str) -> Path:
            raise DbLifecycleError("boom")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _FailingService)

    result = context.invoke("ensure", "demo")
    assert result.exit_code == 1
    assert "boom" in result.stdout


def test_db_cli_require_context_errors() -> None:
    ctx = type("Dummy", (), {"obj": None})()

    with pytest.raises(typer.Exit) as exc:
        _require_context(ctx)

    assert exc.value.exit_code == 1


def test_db_cli_resolve_workspace_respects_env(
    monkeypatch, tmp_path: Path
) -> None:
    env_workspace = tmp_path / "env-workspace"
    monkeypatch.setenv("RAGGD_WORKSPACE", str(env_workspace))

    paths = _resolve_workspace_override(None)

    assert paths.workspace == env_workspace


def test_db_cli_workspace_override_errors(tmp_path: Path) -> None:
    app = create_db_app()
    runner = CliRunner()
    file_path = tmp_path / "workspace.txt"
    file_path.write_text("data", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--workspace", str(file_path), "ensure"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace error" in result.stdout


def test_db_cli_missing_config_file(tmp_path: Path) -> None:
    app = create_db_app()
    runner = CliRunner()
    workspace = tmp_path / "bare"
    workspace.mkdir()

    result = runner.invoke(
        app,
        ["--workspace", str(workspace), "ensure"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Run `raggd init` first" in result.stdout


def test_db_cli_source_config_error(monkeypatch, tmp_path: Path) -> None:
    app = create_db_app()
    runner = CliRunner()
    workspace = tmp_path / "broken"
    workspace.mkdir()
    config_path = workspace / "raggd.toml"
    config_path.write_text("", encoding="utf-8")

    def _raise(*_: object, **__: object):
        raise SourceConfigError("bad config")

    monkeypatch.setattr(
        "raggd.cli.db.SourceConfigStore.load",
        _raise,
        raising=False,
    )

    result = runner.invoke(
        app,
        ["--workspace", str(workspace), "ensure"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to load workspace config" in result.stdout


@pytest.mark.parametrize(
    "command, message",
    [
        ("upgrade", "nothing to upgrade"),
        ("downgrade", "nothing to downgrade"),
        ("info", "nothing to report"),
        ("vacuum", "nothing to vacuum"),
        ("reset", "nothing to reset"),
    ],
)
def test_db_cli_commands_no_sources(
    command: str, message: str, tmp_path: Path
) -> None:
    context = _prepare_workspace(tmp_path, sources=())
    result = context.invoke(command)

    assert result.exit_code == 0
    assert message in result.stdout


def test_db_cli_run_no_sources(tmp_path: Path) -> None:
    context = _prepare_workspace(tmp_path, sources=())
    sql_path = tmp_path / "noop.sql"
    sql_path.write_text("SELECT 1;\n", encoding="utf-8")

    result = context.invoke("run", str(sql_path))

    assert result.exit_code == 0
    assert "nothing to run against" in result.stdout


def test_db_cli_upgrade_failure(monkeypatch, tmp_path: Path) -> None:
    class _UpgradeFailure:
        def __init__(self, **_: object) -> None:
            pass

        def upgrade(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no upgrade")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _UpgradeFailure)

    result = context.invoke("upgrade", "demo")
    assert result.exit_code == 1
    assert "no upgrade" in result.stdout


def test_db_cli_downgrade_failure(monkeypatch, tmp_path: Path) -> None:
    class _DowngradeFailure:
        def __init__(self, **_: object) -> None:
            pass

        def downgrade(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no downgrade")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _DowngradeFailure)

    result = context.invoke("downgrade", "demo")
    assert result.exit_code == 1
    assert "no downgrade" in result.stdout


def test_db_cli_info_failure(monkeypatch, tmp_path: Path) -> None:
    class _InfoFailure:
        def __init__(self, **_: object) -> None:
            pass

        def info(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no info")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _InfoFailure)

    result = context.invoke("info", "demo")
    assert result.exit_code == 1
    assert "no info" in result.stdout


def test_db_cli_vacuum_failure(monkeypatch, tmp_path: Path) -> None:
    class _VacuumFailure:
        def __init__(self, **_: object) -> None:
            pass

        def vacuum(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no vacuum")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _VacuumFailure)

    result = context.invoke("vacuum", "demo")
    assert result.exit_code == 1
    assert "no vacuum" in result.stdout


def test_db_cli_run_failure(monkeypatch, tmp_path: Path) -> None:
    class _RunFailure:
        def __init__(self, **_: object) -> None:
            pass

        def run(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no run")

    context = _prepare_workspace(tmp_path)
    sql_path = tmp_path / "noop.sql"
    sql_path.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _RunFailure)

    result = context.invoke("run", str(sql_path), "demo")
    assert result.exit_code == 1
    assert "no run" in result.stdout


def test_db_cli_reset_failure(monkeypatch, tmp_path: Path) -> None:
    class _ResetFailure:
        def __init__(self, **_: object) -> None:
            pass

        def reset(self, *_: object, **__: object) -> None:
            raise DbLifecycleError("no reset")

    context = _prepare_workspace(tmp_path)
    monkeypatch.setattr("raggd.cli.db.DbLifecycleService", _ResetFailure)

    result = context.invoke("reset", "demo", "--force")
    assert result.exit_code == 1
    assert "no reset" in result.stdout
