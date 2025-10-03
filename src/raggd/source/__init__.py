"""Source management package for :mod:`raggd`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import (
    SourceDisabledError,
    SourceDirectoryConflictError,
    SourceError,
    SourceExistsError,
    SourceHealthCheckError,
    SourceNotFoundError,
)
from .models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
    source_manifest_schema,
    workspace_source_config_schema,
)
from .utils import (
    SourcePathError,
    SourceSlugError,
    ensure_workspace_path,
    normalize_source_slug,
    resolve_target_path,
)

if TYPE_CHECKING:  # pragma: no cover - imports only used for typing
    from .config import (
        SourceConfigError,
        SourceConfigSnapshot,
        SourceConfigStore,
        SourceConfigWriteError,
    )
    from .service import SourceService, SourceState


__all__ = [
    "SourceDisabledError",
    "SourceDirectoryConflictError",
    "SourceError",
    "SourceExistsError",
    "SourceHealthCheckError",
    "SourceNotFoundError",
    "SourceHealthSnapshot",
    "SourceHealthStatus",
    "SourceManifest",
    "WorkspaceSourceConfig",
    "SourceService",
    "SourceState",
    "source_manifest_schema",
    "workspace_source_config_schema",
    "SourceSlugError",
    "SourcePathError",
    "normalize_source_slug",
    "ensure_workspace_path",
    "resolve_target_path",
    "SourceConfigError",
    "SourceConfigSnapshot",
    "SourceConfigStore",
    "SourceConfigWriteError",
    "SourceHealthIssue",
    "evaluate_source_health",
]


_LAZY_IMPORTS = {
    "SourceConfigError": "config",
    "SourceConfigSnapshot": "config",
    "SourceConfigStore": "config",
    "SourceConfigWriteError": "config",
    "SourceService": "service",
    "SourceState": "service",
    "SourceHealthIssue": "health",
    "evaluate_source_health": "health",
}


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial delegation
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'raggd.source' has no attribute {name!r}")

    module = __import__(f"raggd.source.{module_name}", fromlist=[name])
    return getattr(module, name)
