"""CLI tests for the `raggd db` command group."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import tomlkit
from tomlkit.items import Table
from typer.testing import CliRunner

from raggd.cli.db import create_db_app
from raggd.cli.init import init_workspace
from raggd.core.paths import resolve_workspace
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.source.config import SourceConfigStore
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


def test_db_cli_ensure_creates_database(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    init_workspace(workspace=workspace)
    paths = resolve_workspace(workspace_override=workspace)
    store = SourceConfigStore(config_path=paths.config_file)

    source_dir = paths.source_dir("demo")
    source_dir.mkdir(parents=True, exist_ok=True)
    store.upsert(
        WorkspaceSourceConfig(
            name="demo",
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
    _write_migration(
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
    document["db"] = db_table
    paths.config_file.write_text(tomlkit.dumps(document), encoding="utf-8")

    app = create_db_app()
    result = runner.invoke(
        app,
        ["--workspace", str(workspace), "ensure", "demo"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout

    db_path = paths.source_database_path("demo")
    manifest_path = paths.source_manifest_path("demo")

    assert db_path.exists()
    assert manifest_path.exists()
