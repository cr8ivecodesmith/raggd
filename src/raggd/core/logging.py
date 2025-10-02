"""Logging helpers for :mod:`raggd`."""

from __future__ import annotations

import gzip
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import shutil
from typing import Any, Iterable

from rich.console import Console
from rich.logging import RichHandler
import structlog

Logger = structlog.stdlib.BoundLogger

_CONSOLE_PROCESSOR = structlog.dev.ConsoleRenderer(colors=False)
_FILE_PROCESSOR = structlog.processors.JSONRenderer(sort_keys=True)
_TIMESTAMPER = structlog.processors.TimeStamper(fmt="iso", utc=True)

# Default retention keeps a week of history, balancing insight with footprint.
_ROTATION_BACKUP_COUNT = 7
_DEFAULT_LOG_FILENAME = "raggd.log"


def _normalize_level(level: str) -> int:
    """Return the logging module level constant for ``level``.

    Raises:
        ValueError: If the level name is not recognized.
    """

    normalized = level.strip().upper()
    value = logging.getLevelName(normalized)
    if isinstance(value, str):  # ``getLevelName`` echoes unknown names.
        raise ValueError(f"Unsupported log level: {level!r}")
    return value


def _reset_root_logger(
    root: logging.Logger,
    handlers: Iterable[logging.Handler],
) -> None:
    """Replace root handlers with the provided ones."""

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive close
            pass
    for handler in handlers:
        root.addHandler(handler)


def _configure_structlog() -> None:
    """Apply the structlog configuration shared by console/file handlers."""

    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _TIMESTAMPER,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _gzip_rotator(source: str, dest: str) -> None:
    """Compress rotated log file ``source`` into ``dest`` using gzip."""

    with open(source, "rb") as src, gzip.open(dest, "wb") as target:
        shutil.copyfileobj(src, target)
    Path(source).unlink(missing_ok=True)


def _build_file_handler(log_file: Path, level: int) -> TimedRotatingFileHandler:
    """Return a rotating handler that compresses archived log files."""

    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=_ROTATION_BACKUP_COUNT,
        utc=True,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.suffix = "%Y-%m-%d"
    handler.namer = lambda name: f"{name}.gz"
    handler.rotator = _gzip_rotator

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=_FILE_PROCESSOR,
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _TIMESTAMPER,
        ],
    )
    handler.setFormatter(formatter)
    return handler


def _build_console_handler(
    level: int,
    console: Console | None = None,
) -> RichHandler:
    """Return a Rich-backed console handler for structured logging."""

    handler = RichHandler(
        console=console or Console(),
        rich_tracebacks=True,
        show_path=False,
        markup=False,
        enable_link_path=False,
        log_time_format="%Y-%m-%d %H:%M:%S",
    )
    handler.setLevel(level)
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=_CONSOLE_PROCESSOR,
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _TIMESTAMPER,
        ],
    )
    handler.setFormatter(formatter)
    return handler


def configure_logging(
    *,
    level: str = "INFO",
    workspace_path: str | Path | None = None,
    console: Console | None = None,
) -> None:
    """Configure structlog alongside stdlib logging.

    Args:
        level: Log level name to apply to the root logger (case-insensitive).
        workspace_path: Optional workspace root used for the log directory.
        console: Optional Rich console override, primarily for testing.

    Raises:
        ValueError: If ``level`` is not a recognized log level name.

    Example:
        >>> from pathlib import Path
        >>> path = Path("/tmp/raggd-log-example")
        >>> configure_logging(level="debug", workspace_path=path)
        >>> logger = get_logger(__name__)
        >>> logger.info("configured", example=True)  # doctest: +ELLIPSIS
        >>> (path / "logs" / "raggd.log").exists()
        True
    """

    log_level = _normalize_level(level)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    _configure_structlog()

    handlers: list[logging.Handler] = [
        _build_console_handler(log_level, console=console)
    ]

    log_dir: Path | None = None
    if workspace_path is not None:
        workspace = Path(workspace_path).expanduser().resolve(strict=False)
        log_dir = workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / _DEFAULT_LOG_FILENAME
        handlers.append(_build_file_handler(log_file, log_level))

    _reset_root_logger(root_logger, handlers)

    logging.captureWarnings(True)


def get_logger(name: str | None = None, **initial_context: Any) -> Logger:
    """Return a structured logger bound to an optional context.

    Example:
        >>> logger = get_logger(__name__, feature="bootstrap")
        >>> isinstance(logger, structlog.stdlib.BoundLogger)
        True
    """

    return structlog.get_logger(name).bind(**initial_context)


__all__ = ["Logger", "configure_logging", "get_logger"]
