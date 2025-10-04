"""CLI tests for the `raggd db` command group."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from raggd.cli.db import create_db_app
from raggd.cli.init import init_workspace
from raggd.core.paths import resolve_workspace
from raggd.source.config import SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


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

