"""Tests covering the VDB CLI scaffold."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raggd.cli.init import init_workspace
from raggd.cli.vdb import create_vdb_app


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
        (("create", "docs@latest", "base", "--model", "openai:test"), "create"),
        (("sync", "docs"), "sync"),
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
    assert (
        "Invalid value for --missing-only/--recompute" in result.output
    )
