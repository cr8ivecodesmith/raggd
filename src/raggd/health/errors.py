"""Custom exceptions for health document management."""

from __future__ import annotations


class HealthDocumentError(Exception):
    """Base error raised when reading or writing `.health.json` fails."""


class HealthDocumentReadError(HealthDocumentError):
    """Raised when the persisted health document cannot be loaded."""


class HealthDocumentWriteError(HealthDocumentError):
    """Raised when an updated health document cannot be written atomically."""


__all__ = [
    "HealthDocumentError",
    "HealthDocumentReadError",
    "HealthDocumentWriteError",
]
