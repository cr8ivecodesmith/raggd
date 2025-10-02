"""Logging helpers for :mod:`raggd`."""

from __future__ import annotations

from typing import Any

import structlog

Logger = structlog.stdlib.BoundLogger


def configure_logging(*, level: str = "INFO", workspace_path: str | None = None) -> None:
    """Configure structlog and stdlib logging.

    Args:
        level: Log level name to apply to the root logger.
        workspace_path: Optional workspace path for file handlers.

    Raises:
        NotImplementedError: Until logging configuration is implemented.
    """

    raise NotImplementedError(
        "Logging configuration will be implemented in a subsequent step."
    )


def get_logger(name: str | None = None, **initial_context: Any) -> Logger:
    """Return a structured logger bound to an optional context.

    Example:
        >>> logger = get_logger(__name__, feature="bootstrap")
        >>> isinstance(logger, structlog.stdlib.BoundLogger)
        True
    """

    return structlog.get_logger(name).bind(**initial_context)


__all__ = ["Logger", "configure_logging", "get_logger"]
