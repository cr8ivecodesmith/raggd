"""Helpers for the ``raggd init`` command."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping


def init_workspace(
    *,
    workspace: Path,
    refresh: bool = False,
    log_level: str | None = None,
    module_overrides: Mapping[str, bool] | None = None,
    extra_messages: Iterable[str] | None = None,
) -> None:
    """Bootstrap the workspace directory and supporting artifacts.

    Example:
        >>> from pathlib import Path
        >>> from raggd.cli.init import init_workspace
        >>> init_workspace(workspace=Path("/tmp/raggd"))
        Traceback (most recent call last):
        ...
        NotImplementedError: Workspace bootstrap will be implemented shortly.

    Args:
        workspace: Target directory for the workspace.
        refresh: Whether to archive/refresh an existing workspace.
        log_level: Optional override for the configured logging level.
        module_overrides: Optional mapping that forces module enablement state.
        extra_messages: Additional log lines to emit after success.

    Raises:
        NotImplementedError: Until the bootstrap logic is implemented.
    """

    raise NotImplementedError(
        "Workspace bootstrap will be implemented in a subsequent step."
    )


__all__ = ["init_workspace"]
