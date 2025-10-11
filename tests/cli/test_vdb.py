"""Tests covering the VDB CLI surface."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raggd.cli.init import init_workspace
from raggd.cli.vdb import create_vdb_app
from raggd.core.paths import resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import (
    ManifestService,
    manifest_settings_from_config,
)
from raggd.source.config import SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Materialize a minimal workspace for CLI exercises."""

    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    return workspace


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a Typer CLI runner instance."""

    return CliRunner()


@pytest.mark.parametrize(
    ("args", "action"),
    [
        (("info",), "info"),
        (("reset", "docs"), "reset"),
    ],
)
def test_vdb_cli_stub_actions(
    workspace: Path,
    runner: CliRunner,
    args: tuple[str, ...],
    action: str,
) -> None:
    """Ensure stubbed service actions emit a helpful notice."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        ["--workspace", workspace.as_posix(), *args],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert (
        f"VDB {action} is not implemented yet; CLI scaffold is in place."
        in result.stdout
    )


def test_vdb_cli_sync_requires_configured_source(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """`vdb sync` should fail fast when the source is missing."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        ["--workspace", workspace.as_posix(), "sync", "docs"],
    )

    assert result.exit_code == 1
    assert "Source 'docs' is not configured in this workspace." in result.stdout


def _configure_docs_source(workspace: Path) -> None:
    """Attach a `docs` source entry to the workspace configuration."""

    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    docs_dir = resolve_workspace(
        workspace_override=workspace,
    ).source_dir("docs")
    docs_dir.mkdir(parents=True, exist_ok=True)

    source = WorkspaceSourceConfig(
        name="docs",
        path=docs_dir,
        enabled=True,
    )
    store.upsert(source)


def _seed_docs_database(workspace: Path) -> Path:
    """Ensure the docs database exists with a baseline batch/model."""

    paths = resolve_workspace(workspace_override=workspace)
    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    config = store.load()
    payload = config.model_dump(mode="python")

    manifest_service = ManifestService(
        workspace=paths,
        settings=manifest_settings_from_config(payload),
    )
    db_service = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest_service,
        db_settings=db_settings_from_mapping(payload),
    )

    db_path = db_service.ensure("docs")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            (
                "batch-001",
                None,
                datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
                None,
            ),
        )
        connection.execute(
            (
                "INSERT INTO embedding_models (provider, name, dim) "
                "VALUES (?, ?, ?)"
            ),
            ("openai", "test", 1536),
        )
    return db_path


def test_vdb_cli_create_success(workspace: Path, runner: CliRunner) -> None:
    """`raggd vdb create` succeeds when the source and batch exist."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@latest",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert (
        "Created VDB base for docs@latest using model openai:test"
        in result.stdout
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM vdbs WHERE name = ?",
            ("base",),
        ).fetchone()
    assert row is not None and row[0] == 1


def test_vdb_cli_sync_conflicting_flags(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """Mutually exclusive sync flags should trigger a CLI error."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "sync",
            "docs",
            "--missing-only",
            "--recompute",
        ],
    )

    assert result.exit_code == 2
    assert "Invalid value for --missing-only/--recompute" in result.output
