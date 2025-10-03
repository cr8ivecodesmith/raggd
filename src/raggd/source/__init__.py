"""Source management package for :mod:`raggd`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
    source_manifest_schema,
    workspace_source_config_schema,
)

if TYPE_CHECKING:  # pragma: no cover - imports only used for typing
    from .config import (
        SourceConfigError,
        SourceConfigSnapshot,
        SourceConfigStore,
        SourceConfigWriteError,
    )


__all__ = [
    "SourceHealthSnapshot",
    "SourceHealthStatus",
    "SourceManifest",
    "WorkspaceSourceConfig",
    "source_manifest_schema",
    "workspace_source_config_schema",
    "SourceConfigError",
    "SourceConfigSnapshot",
    "SourceConfigStore",
    "SourceConfigWriteError",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial delegation
    if name in {
        "SourceConfigError",
        "SourceConfigSnapshot",
        "SourceConfigStore",
        "SourceConfigWriteError",
    }:
        from . import config

        return getattr(config, name)
    raise AttributeError(f"module 'raggd.source' has no attribute {name!r}")
