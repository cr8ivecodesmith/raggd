"""Core utilities shared across :mod:`raggd` modules.

The core namespace provides cohesive seams for configuration loading, logging
setup, and workspace path resolution so feature modules remain lightweight.

Example:
    >>> from raggd.core import get_logger
    >>> logger = get_logger(__name__)
    >>> isinstance(logger, object)
    True
"""

from __future__ import annotations

from .config import AppConfig, ModuleToggle, load_config
from .logging import configure_logging, get_logger
from .paths import WorkspacePaths, resolve_workspace

__all__ = [
    "AppConfig",
    "ModuleToggle",
    "configure_logging",
    "get_logger",
    "load_config",
    "WorkspacePaths",
    "resolve_workspace",
]
