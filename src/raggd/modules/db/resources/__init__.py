"""Bundled resources for the database module."""

from __future__ import annotations

import importlib.resources as _resources
from pathlib import Path
from typing import Union

__all__ = ["resource_path"]


def resource_path(relative: str) -> Path:
    """Return a filesystem path for a packaged resource."""

    package = __name__
    return Path(_resources.files(package).joinpath(relative))

