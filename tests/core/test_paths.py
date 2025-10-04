"""Tests for :mod:`raggd.core.paths`."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import pytest

from raggd.core.paths import (
    WorkspacePaths,
    archive_workspace,
    resolve_workspace,
)


def test_resolve_workspace_defaults_to_home_dot_raggd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver defaults to ``$HOME/.raggd`` when overrides are absent."""

    fake_home = Path("/tmp/raggd-home")
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("USERPROFILE", fake_home.as_posix())

    paths = resolve_workspace()

    expected = (fake_home / ".raggd").expanduser().resolve(strict=False)
    assert paths.workspace == expected
    assert paths.config_file == expected / "raggd.toml"
    assert paths.logs_dir == expected / "logs"
    assert paths.archives_dir == expected / "archives"
    assert paths.sources_dir == expected / "sources"


def test_resolve_workspace_prefers_cli_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI override should take precedence over env and defaults."""

    monkeypatch.setenv("RAGGD_WORKSPACE", (tmp_path / "ignored").as_posix())
    cli_override = tmp_path / "from-cli"

    paths = resolve_workspace(
        workspace_override=cli_override,
        env_override=Path(os.environ["RAGGD_WORKSPACE"]),
    )

    assert paths.workspace == cli_override.resolve(strict=False)


def test_resolve_workspace_uses_env_override_when_cli_missing(
    tmp_path: Path,
) -> None:
    """Environment override fills in when CLI is omitted."""

    env_override = tmp_path / "from-env"

    paths = resolve_workspace(env_override=env_override)

    assert paths.workspace == env_override.resolve(strict=False)


def test_resolve_workspace_supports_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative overrides should resolve from the current working directory."""

    monkeypatch.chdir(tmp_path)
    paths = resolve_workspace(workspace_override=Path("workspaces/relative"))

    expected = (tmp_path / "workspaces/relative").resolve(strict=False)
    assert paths.workspace == expected


def test_resolve_workspace_rejects_file_path(tmp_path: Path) -> None:
    """If the target exists as a file, the resolver should fail fast."""

    file_path = tmp_path / "workspace-as-file"
    file_path.write_text("not a directory")

    with pytest.raises(ValueError):
        resolve_workspace(workspace_override=file_path)


def test_archive_workspace_moves_contents(tmp_path: Path) -> None:
    """Refreshing archives existing artifacts into timestamped ZIPs."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "logs").mkdir()
    (workspace / "raggd.toml").write_text("# sample\n")

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )

    archive_path = archive_workspace(paths)
    assert archive_path is not None
    assert archive_path.parent == paths.archives_dir
    assert archive_path.suffix == ".zip"

    with ZipFile(archive_path) as archive:
        names = sorted(archive.namelist())

    assert names == ["logs/", "raggd.toml"]
    assert list(workspace.iterdir()) == [paths.archives_dir]


def test_archive_workspace_returns_none_for_empty(tmp_path: Path) -> None:
    """When nothing exists yet, archiving should no-op."""

    workspace = tmp_path / "fresh"
    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )

    assert archive_workspace(paths) is None


def test_archive_workspace_raises_on_non_directory(tmp_path: Path) -> None:
    """Prevent archiving from an invalid workspace target."""

    workspace = tmp_path / "bad-workspace"
    workspace.write_text("oops")

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )

    with pytest.raises(ValueError):
        archive_workspace(paths)


def test_workspace_paths_iter_all() -> None:
    paths = WorkspacePaths(
        workspace=Path("/tmp/workspace"),
        config_file=Path("/tmp/workspace/raggd.toml"),
        logs_dir=Path("/tmp/workspace/logs"),
        archives_dir=Path("/tmp/workspace/archives"),
        sources_dir=Path("/tmp/workspace/sources"),
    )

    names = {path.name for path in paths.iter_all()}
    assert names == {"workspace", "raggd.toml", "logs", "archives", "sources"}


def test_workspace_paths_source_helpers() -> None:
    paths = WorkspacePaths(
        workspace=Path("/tmp/workspace"),
        config_file=Path("/tmp/workspace/raggd.toml"),
        logs_dir=Path("/tmp/workspace/logs"),
        archives_dir=Path("/tmp/workspace/archives"),
        sources_dir=Path("/tmp/workspace/sources"),
    )

    source_root = paths.source_dir("demo")
    assert source_root == Path("/tmp/workspace/sources/demo")
    assert paths.source_manifest_path("demo") == source_root / "manifest.json"
    assert paths.source_database_path("demo") == source_root / "db.sqlite3"


def test_archive_workspace_generates_unique_suffixes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    archives = workspace / "archives"
    workspace.mkdir()
    (workspace / "config.txt").write_text("a")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("raggd.core.paths.datetime", FixedDateTime)

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=archives,
        sources_dir=workspace / "sources",
    )

    first_archive = archive_workspace(paths)
    assert first_archive is not None

    (workspace / "config.txt").write_text("b")
    second_archive = archive_workspace(paths)

    assert second_archive is not None
    assert second_archive.name.endswith("-01.zip")


def test_archive_workspace_cleans_empty_archives(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    archives = workspace / "archives"
    workspace.mkdir()
    archives.mkdir()

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=archives,
        sources_dir=workspace / "sources",
    )

    result = archive_workspace(paths)

    assert result is None
    assert not archives.exists()


def test_archive_workspace_preserves_existing_archives(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    archives = workspace / "archives"
    workspace.mkdir()
    archives.mkdir()
    (archives / "old.zip").write_bytes(b"")

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=archives,
        sources_dir=workspace / "sources",
    )

    assert archive_workspace(paths) is None
    assert archives.exists()
    assert (archives / "old.zip").exists()
