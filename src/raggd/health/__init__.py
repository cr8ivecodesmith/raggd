"""Health document helpers for :mod:`raggd`."""

from __future__ import annotations

from .document import (
    HealthDetail,
    HealthDocument,
    HealthDocumentStore,
    HealthModuleSnapshot,
    build_module_snapshot,
    dump_health_document,
    load_health_document,
)
from .errors import (
    HealthDocumentError,
    HealthDocumentReadError,
    HealthDocumentWriteError,
)

__all__ = [
    "HealthDetail",
    "HealthDocument",
    "HealthDocumentError",
    "HealthDocumentReadError",
    "HealthDocumentStore",
    "HealthDocumentWriteError",
    "HealthModuleSnapshot",
    "build_module_snapshot",
    "dump_health_document",
    "load_health_document",
]
