"""Workspace path helpers for :mod:`raggd`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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
        NotImplementedError: Until the resolver is implemented.
    """

    raise NotImplementedError(
        "Workspace resolution will be implemented in a subsequent step."
    )


__all__ = ["WorkspacePaths", "resolve_workspace"]
