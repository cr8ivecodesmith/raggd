"""Workspace path helpers for :mod:`raggd`."""

from __future__ import annotations

import shutil

from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile

__all__ = [
    "WorkspacePaths",
    "resolve_workspace",
    "archive_workspace",
]


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Resolved locations for a workspace instance.

    Example:
        >>> from pathlib import Path
        >>> from raggd.core.paths import WorkspacePaths
        >>> paths = WorkspacePaths(
        ...     workspace=Path("/tmp/raggd"),
        ...     config_file=Path("/tmp/raggd/raggd.toml"),
        ...     logs_dir=Path("/tmp/raggd/logs"),
        ...     archives_dir=Path("/tmp/raggd/archives"),
        ...     sources_dir=Path("/tmp/raggd/sources"),
        ... )
        >>> paths.logs_dir.name
        'logs'
    """

    workspace: Path
    config_file: Path
    logs_dir: Path
    archives_dir: Path
    sources_dir: Path

    def iter_all(self) -> Iterable[Path]:
        """Yield every path managed within the workspace.

        Example:
            >>> from pathlib import Path
            >>> from raggd.core.paths import WorkspacePaths
            >>> paths = WorkspacePaths(
            ...     workspace=Path("/tmp/raggd"),
            ...     config_file=Path("/tmp/raggd/raggd.toml"),
            ...     logs_dir=Path("/tmp/raggd/logs"),
            ...     archives_dir=Path("/tmp/raggd/archives"),
            ...     sources_dir=Path("/tmp/raggd/sources"),
            ... )
            >>> [p.name for p in paths.iter_all()]
            ['raggd', 'raggd.toml', 'logs', 'archives', 'sources']
        """

        yield from (
            self.workspace,
            self.config_file,
            self.logs_dir,
            self.archives_dir,
            self.sources_dir,
        )

    def source_dir(self, name: str) -> Path:
        """Return the directory for a named source."""

        return self.sources_dir / name

    def source_manifest_path(self, name: str) -> Path:
        """Return the manifest path for a named source."""

        return self.source_dir(name) / "manifest.json"

    def source_database_path(self, name: str) -> Path:
        """Return the SQLite database path for a named source."""

        return self.source_dir(name) / "db.sqlite3"


def resolve_workspace(
    *,
    workspace_override: Path | None = None,
    env_override: Path | None = None,
) -> WorkspacePaths:
    """Resolve canonical workspace locations.

    Args:
        workspace_override: Optional override provided by CLI flags.
        env_override: Optional override from environment variables.

    Returns:
        Resolved workspace paths after precedence rules are applied.

    Raises:
        ValueError: If the resolved workspace points to a regular file.
    """

    def _normalize(candidate: Path) -> Path:
        raw = Path(candidate).expanduser()
        if raw.is_absolute():
            resolved = raw.resolve(strict=False)
        else:
            resolved = (Path.cwd() / raw).resolve(strict=False)
        return resolved

    base = workspace_override or env_override or Path.home() / ".raggd"
    workspace = _normalize(base)

    if workspace.exists() and workspace.is_file():
        raise ValueError(f"Workspace file path not allowed: {workspace}")

    config_file = workspace / "raggd.toml"
    logs_dir = workspace / "logs"
    archives_dir = workspace / "archives"
    sources_dir = workspace / "sources"

    return WorkspacePaths(
        workspace=workspace,
        config_file=config_file,
        logs_dir=logs_dir,
        archives_dir=archives_dir,
        sources_dir=sources_dir,
    )


def _gather_archive_candidates(
    workspace: Path,
    archive_root: Path,
) -> list[Path]:
    return [entry for entry in workspace.iterdir() if entry != archive_root]


def _generate_archive_name(archive_root: Path, timestamp: str) -> Path:
    suffix = 0
    while True:
        suffix_part = "" if suffix == 0 else f"-{suffix:02d}"
        candidate = archive_root / f"{timestamp}{suffix_part}.zip"
        if not candidate.exists():
            return candidate
        suffix += 1


def _write_path_to_zip(root: Path, path: Path, zf: ZipFile) -> None:
    relative = path.relative_to(root).as_posix()
    if path.is_dir():
        directory_name = relative.rstrip("/") + "/"
        zf.writestr(directory_name, "")
        for child in sorted(path.iterdir()):
            _write_path_to_zip(root, child, zf)
    else:
        zf.write(path, relative)


def _zip_workspace_entries(
    workspace: Path,
    entries: Iterable[Path],
    destination: Path,
) -> None:
    with ZipFile(destination, mode="w", compression=ZIP_DEFLATED) as zf:
        for entry in sorted(entries):
            _write_path_to_zip(workspace, entry, zf)


def _remove_archived_entries(entries: Iterable[Path]) -> None:
    for entry in entries:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _prune_archive_root_if_empty(archive_root: Path) -> None:
    if any(archive_root.iterdir()):
        return
    archive_root.rmdir()


def archive_workspace(paths: WorkspacePaths) -> Path | None:
    """Archive the current workspace contents into a timestamped ZIP file.

    Example:
        >>> from pathlib import Path
        >>> from raggd.core.paths import WorkspacePaths, archive_workspace
        >>> root = Path("/tmp/raggd-example")
        >>> paths = WorkspacePaths(
        ...     workspace=root,
        ...     config_file=root / "raggd.toml",
        ...     logs_dir=root / "logs",
        ...     archives_dir=root / "archives",
        ...     sources_dir=root / "sources",
        ... )
        >>> root.mkdir(parents=True, exist_ok=True)
        >>> _ = (root / "logs").mkdir(exist_ok=True)
        >>> _ = (root / "raggd.toml").write_text("example = true\n")
        >>> archive = archive_workspace(paths)
        >>> archive is None or archive.suffix == ".zip"
        True

    Args:
        paths: Workspace paths describing the current workspace layout.

    Returns:
        Archive directory path when contents were moved, otherwise ``None``.
    """

    workspace = paths.workspace
    if not workspace.exists():
        return None

    if not workspace.is_dir():
        raise ValueError(
            f"Workspace path '{workspace}' exists but is not a directory."
        )

    archive_root = paths.archives_dir
    archive_root.mkdir(parents=True, exist_ok=True)

    to_archive = _gather_archive_candidates(workspace, archive_root)

    if not to_archive:
        _prune_archive_root_if_empty(archive_root)
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive_path = _generate_archive_name(archive_root, timestamp)

    _zip_workspace_entries(workspace, to_archive, archive_path)
    _remove_archived_entries(to_archive)

    return archive_path
