"""Console-script entry point for :mod:`raggd`."""

from __future__ import annotations

from raggd.cli import create_app


def main() -> None:
    """Execute the CLI application.

    Example:
        >>> from raggd.__main__ import main
        >>> main()  # doctest: +SKIP
    """

    app = create_app()
    app(prog_name="raggd")


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    main()


__all__ = ["main"]
