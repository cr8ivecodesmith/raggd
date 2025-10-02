"""Helpers for the ``raggd init`` command."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from raggd.core.config import (
    AppConfig,
    DEFAULTS_RESOURCE_NAME,
    load_config,
    load_packaged_defaults,
    render_user_config,
    read_packaged_defaults_text,
)
from raggd.core.paths import WorkspacePaths, archive_workspace, resolve_workspace


def _ensure_directories(paths: WorkspacePaths) -> None:
    """Create the workspace directories if they are missing."""

    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.archives_dir.mkdir(parents=True, exist_ok=True)


def _normalize_module_overrides(
    overrides: Mapping[str, bool] | None,
) -> Mapping[str, dict[str, bool]] | None:
    """Convert CLI-style module overrides into config-friendly values."""

    if not overrides:
        return None

    return {name: {"enabled": bool(enabled)} for name, enabled in overrides.items()}


def init_workspace(
    *,
    workspace: Path,
    refresh: bool = False,
    log_level: str | None = None,
    module_overrides: Mapping[str, bool] | None = None,
    extra_messages: Iterable[str] | None = None,
) -> AppConfig:
    """Bootstrap the workspace directory and supporting artifacts.

    Example:
        >>> from pathlib import Path
        >>> config = init_workspace(workspace=Path("/tmp/raggd-example"))
        >>> str(config.workspace).endswith("raggd-example")
        True

    Args:
        workspace: Target directory for the workspace.
        refresh: Whether to archive/refresh an existing workspace.
        log_level: Optional override for the configured logging level.
        module_overrides: Optional mapping that forces module enablement state.
        extra_messages: Additional log lines to emit after success.

    Returns:
        The resolved configuration after applying overrides.
    """

    # Materialize the tuple for validation even if we do not log yet.
    _ = tuple(extra_messages or ())

    paths = resolve_workspace(workspace_override=workspace)

    if refresh:
        archive_workspace(paths)

    _ensure_directories(paths)

    defaults = load_packaged_defaults()
    cli_overrides: dict[str, object] = {"workspace": str(paths.workspace)}
    if log_level:
        cli_overrides["log_level"] = log_level

    normalized_module_overrides = _normalize_module_overrides(module_overrides)

    config = load_config(
        defaults=defaults,
        cli_overrides=cli_overrides,
        module_overrides=normalized_module_overrides,
    )

    defaults_path = paths.workspace / DEFAULTS_RESOURCE_NAME
    if refresh or not defaults_path.exists():
        defaults_text = read_packaged_defaults_text()
        defaults_path.write_text(defaults_text, encoding="utf-8")

    config_path = paths.config_file
    if refresh or not config_path.exists():
        rendered = render_user_config(config)
        config_path.write_text(rendered, encoding="utf-8")

    return config


__all__ = ["init_workspace"]
