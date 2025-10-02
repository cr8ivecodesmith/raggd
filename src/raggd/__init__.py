"""Top-level package for :mod:`raggd` CLI utilities.

The package exposes version metadata so downstream tooling can surface the
installed build.

Example:
    >>> from raggd import __version__
    >>> __version__.split(".")[0]
    '0'
"""

from importlib import metadata

try:
    __version__ = metadata.version("raggd")
except metadata.PackageNotFoundError:  # pragma: no cover - fallback for editable installs
    __version__ = "0.0.0"

__all__ = ["__version__"]
