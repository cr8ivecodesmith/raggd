"""Domain-specific exceptions for source management."""

from __future__ import annotations


class SourceError(RuntimeError):
    """Base error for source management failures."""


class SourceExistsError(SourceError):
    """Raised when attempting to create a source that already exists."""


class SourceNotFoundError(SourceError):
    """Raised when a requested source cannot be located."""


class SourceDirectoryConflictError(SourceError):
    """Raised when filesystem artifacts conflict with expected source layout."""


class SourceDisabledError(SourceError):
    """Raised when an operation requires an enabled source."""


class SourceHealthCheckError(SourceError):
    """Raised when a health check blocks an operation."""


__all__ = [
    "SourceError",
    "SourceExistsError",
    "SourceNotFoundError",
    "SourceDirectoryConflictError",
    "SourceDisabledError",
    "SourceHealthCheckError",
]
