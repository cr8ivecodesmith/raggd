"""Console-script entry point for :mod:`raggd`."""

from __future__ import annotations


def main() -> None:
    """Execute the CLI application.

    Example:
        >>> from raggd.__main__ import main
        >>> main()
        Traceback (most recent call last):
        ...
        NotImplementedError: CLI wiring will be implemented during bootstrap.

    Raises:
        NotImplementedError: Until the Typer application wiring is completed.
    """

    raise NotImplementedError(
        "CLI wiring will be implemented during the bootstrap milestone."
    )


__all__ = ["main"]
