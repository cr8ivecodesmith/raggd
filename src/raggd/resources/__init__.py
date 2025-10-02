"""Packaged resource helpers for :mod:`raggd`."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable


def get_resource(relative_path: str) -> Traversable:
    """Return a traversable handle to a packaged resource.

    Example:
        >>> try:
        ...     get_resource("raggd.defaults.toml")
        ... except FileNotFoundError:
        ...     print("resource missing (expected during scaffolding)")
        resource missing (expected during scaffolding)
    """

    candidate = resources.files(__package__).joinpath(relative_path)
    if not candidate.exists():
        raise FileNotFoundError(relative_path)
    return candidate


__all__ = ["get_resource"]
