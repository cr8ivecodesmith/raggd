"""Workspace path helpers for :mod:`raggd`."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
        ... )
        >>> paths.logs_dir.name
        'logs'
    """

    workspace: Path
    config_file: Path
    logs_dir: Path
    archives_dir: Path

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
            ... )
            >>> [p.name for p in paths.iter_all()]
            ['raggd', 'raggd.toml', 'logs', 'archives']
        """

        yield from (
            self.workspace,
            self.config_file,
            self.logs_dir,
            self.archives_dir,
        )


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

    return WorkspacePaths(
        workspace=workspace,
        config_file=config_file,
        logs_dir=logs_dir,
        archives_dir=archives_dir,
    )


def archive_workspace(paths: WorkspacePaths) -> Path | None:
    """Archive the current workspace contents into a timestamped folder.

    Example:
        >>> from pathlib import Path
        >>> from raggd.core.paths import WorkspacePaths, archive_workspace
        >>> root = Path("/tmp/raggd-example")
        >>> paths = WorkspacePaths(
        ...     workspace=root,
        ...     config_file=root / "raggd.toml",
        ...     logs_dir=root / "logs",
        ...     archives_dir=root / "archives",
        ... )
        >>> root.mkdir(parents=True, exist_ok=True)
        >>> _ = (root / "logs").mkdir(exist_ok=True)
        >>> _ = (root / "raggd.toml").write_text("example = true\n")
        >>> archive = archive_workspace(paths)
        >>> archive is None or archive.parent.name == "archives"
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

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = archive_root / timestamp
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = archive_root / f"{timestamp}-{suffix:02d}"
    candidate.mkdir(parents=True, exist_ok=True)

    moved_any = False
    for entry in workspace.iterdir():
        if entry == archive_root or entry == candidate:
            continue
        target = candidate / entry.name
        entry.rename(target)
        moved_any = True

    if not moved_any:
        candidate.rmdir()
        if not any(archive_root.iterdir()):
            archive_root.rmdir()
        return None

    return candidate
