"""Packaged SQL statements used by the parser module."""

from __future__ import annotations

import importlib.resources as _resources
from pathlib import Path

__all__ = ["sql_path", "load_sql"]


def sql_path(name: str) -> Path:
    """Return the filesystem path for a packaged SQL statement."""

    if not name:
        raise ValueError("name must be a non-empty string")
    return Path(_resources.files(__name__).joinpath(name))


def load_sql(name: str) -> str:
    """Return the contents of a packaged SQL statement."""

    return sql_path(name).read_text(encoding="utf-8")
