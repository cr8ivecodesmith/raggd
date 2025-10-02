"""Command-line interface primitives for :mod:`raggd`.

This package will expose the Typer application used by the ``raggd`` console
script while keeping command registration modular.

Example:
    >>> from raggd.cli import create_app
    >>> create_app()
    Traceback (most recent call last):
    ...
    NotImplementedError: CLI wiring will be provided during bootstrap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported solely for type checking
    import typer


def create_app() -> "typer.Typer":
    """Return the Typer application powering the ``raggd`` CLI.

    Example:
        >>> from raggd.cli import create_app
        >>> create_app()
        Traceback (most recent call last):
        ...
        NotImplementedError: CLI wiring will be provided during bootstrap.

    Returns:
        The fully wired Typer application once bootstrap work lands.

    Raises:
        NotImplementedError: Until the CLI wiring step is implemented.
    """

    raise NotImplementedError(
        "CLI wiring will be provided during the bootstrap implementation."
    )


__all__ = ["create_app"]
