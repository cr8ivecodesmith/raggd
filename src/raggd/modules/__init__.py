"""Optional module registry for :mod:`raggd`."""

from __future__ import annotations

from .registry import (
    HealthRegistry,
    HealthReport,
    HealthStatus,
    ModuleDescriptor,
    ModuleHealthHook,
    ModuleRegistry,
    WorkspaceHandle,
)

__all__ = [
    "HealthRegistry",
    "HealthReport",
    "HealthStatus",
    "ModuleDescriptor",
    "ModuleHealthHook",
    "ModuleRegistry",
    "WorkspaceHandle",
]
