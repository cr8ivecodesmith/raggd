"""Typer command group for vector database (VDB) operations.

This CLI group manages VDB lifecycle bound to parser batches and embedding
models. MVP subcommands include `info`, `create`, `sync`, and `reset`. The
initial scaffold wires the group into the main CLI and prepares a context for
subcommands to build on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import typer

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.source.config import SourceConfigError, SourceConfigStore


@dataclass(slots=True)
class VdbCLIContext:
    """Shared context carried across `raggd vdb` commands."""

    paths: WorkspacePaths
    config: AppConfig
    store: SourceConfigStore
    logger: Logger


_vdb_app = typer.Typer(
    name="vdb",
    help=(
        "Manage per-source vector databases (create/sync/info/reset).\n\n"
        "Note: This is an initial scaffold; subcommands will be completed in"
        " subsequent steps as per the implementation plan."
    ),
    no_args_is_help=True,
    invoke_without_command=False,
)


def _resolve_workspace_override(workspace: Path | None) -> WorkspacePaths:
    env_workspace = os.environ.get("RAGGD_WORKSPACE")
    env_override = Path(env_workspace).expanduser() if env_workspace else None
    return resolve_workspace(
        workspace_override=workspace,
        env_override=env_override,
    )


def _require_context(ctx: typer.Context) -> VdbCLIContext:
    context = getattr(ctx, "obj", None)
    if not isinstance(context, VdbCLIContext):
        typer.secho(
            "Internal error: vdb context not initialized.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    return context


@_vdb_app.callback()
def configure_vdb_commands(
    ctx: typer.Context,
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Override workspace directory (defaults to "
            "RAGGD_WORKSPACE or ~/.raggd)."
        ),
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        "-l",
        help=(
            "Override log level for vdb commands (defaults to config log_level)."
        ),
    ),
) -> None:
    """Initialize common VDB CLI context."""

    try:
        paths = _resolve_workspace_override(workspace)
    except ValueError as exc:
        typer.secho(f"Workspace error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if not paths.config_file.exists():
        typer.secho(
            (
                "Workspace config not found at "
                f"{paths.config_file}. Run `raggd init` first."
            ),
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    store = SourceConfigStore(config_path=paths.config_file)
    try:
        config = store.load()
    except SourceConfigError as exc:
        typer.secho(
            f"Failed to load workspace config: {exc}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc

    configure_logging(
        level=log_level or config.log_level,
        workspace_path=config.workspace,
    )

    logger = get_logger(__name__, command="vdb")

    ctx.obj = VdbCLIContext(
        paths=paths,
        config=config,
        store=store,
        logger=logger,
    )


@_vdb_app.command(
    "info",
    help=(
        "Display VDB status for a source (scaffold placeholder).\n\n"
        "This placeholder will be replaced with the full implementation that "
        "emits a structured JSON summary as per the spec."
    ),
)
def info_vdb(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="[SOURCE]",
        help="Optional source name to filter info (defaults to all).",
    ),
    vdb: str | None = typer.Option(
        None,
        "--vdb",
        metavar="NAME",
        help="Optional VDB name to filter results.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON output (not implemented yet).",
    ),
) -> None:
    """Report basic status while the full feature is under construction."""

    context = _require_context(ctx)
    note = (
        "VDB info is not implemented yet; CLI scaffold is in place."
    )
    if json_output:
        # Keep output stable and machine-friendly even during scaffold.
        typer.echo(
            {
                "status": "not-implemented",
                "message": note,
                "source": source,
                "vdb": vdb,
            }
        )
    else:
        target = source or "all configured sources"
        typer.secho(
            f"VDB info for {target}",
            fg=typer.colors.CYAN,
            bold=True,
        )
        typer.secho(note, fg=typer.colors.YELLOW)
    context.logger.info(
        "vdb-info-skeleton",
        source=source,
        vdb=vdb,
        json=json_output,
    )


def create_vdb_app() -> "typer.Typer":
    """Return the Typer application for `raggd vdb`."""

    return _vdb_app


__all__ = ["create_vdb_app"]

