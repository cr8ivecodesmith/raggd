"""Typer command group scaffolding for the parser module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import typer

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.source.config import SourceConfigError, SourceConfigStore


@dataclass(slots=True)
class ParserCLIContext:
    """Shared context persisted across parser subcommands."""

    paths: WorkspacePaths
    config: AppConfig
    settings: ParserModuleSettings
    logger: Logger


_parser_app = typer.Typer(
    name="parser",
    help="Manage parser workflows (parse/info/batches/remove).",
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


def _require_context(ctx: typer.Context) -> ParserCLIContext:
    context = getattr(ctx, "obj", None)
    if not isinstance(context, ParserCLIContext):
        typer.secho(
            "Internal error: parser context not initialized.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    return context


def _emit_unimplemented(
    context: ParserCLIContext,
    *,
    command: str,
    summary: str,
) -> None:
    message = (
        f"`raggd parser {command}` is not available yet — "
        "subcommand scaffolding landed in phase 1."
    )
    typer.secho(message, fg=typer.colors.YELLOW)
    context.logger.warning(
        "parser-command-unimplemented",
        command=command,
        summary=summary,
        enabled=context.settings.enabled,
    )
    raise typer.Exit(code=1)


@_parser_app.callback()
def configure_parser_commands(
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
            "Override log level for parser commands "
            "(defaults to config log_level)."
        ),
    ),
) -> None:
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

    logger = get_logger(__name__, command="parser")
    settings = config.parser
    logger.info(
        "parser-cli-context-created",
        enabled=settings.enabled,
        max_concurrency=settings.max_concurrency,
        fail_fast=settings.fail_fast,
        gitignore_behavior=settings.gitignore_behavior.value,
    )

    ctx.obj = ParserCLIContext(
        paths=paths,
        config=config,
        settings=settings,
        logger=logger,
    )


@_parser_app.command(
    "parse",
    help="Parse configured sources synchronously using parser handlers.",
)
def parse_command(
    ctx: typer.Context,
    sources: List[str] | None = typer.Argument(
        None,
        metavar="[SOURCE]...",
        help=(
            "Optional source names to parse. Defaults to all configured "
            "sources when omitted."
        ),
    ),
) -> None:
    context = _require_context(ctx)
    selected = tuple(sources or ())
    summary = ",".join(selected) or "all"
    _emit_unimplemented(context, command="parse", summary=summary)


@_parser_app.command(
    "info",
    help="Show parser module status and configuration for a source.",
)
def info_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="[SOURCE]",
        help="Optional source name to inspect (defaults to summary view).",
    ),
) -> None:
    context = _require_context(ctx)
    target = source or "*"
    _emit_unimplemented(context, command="info", summary=target)


@_parser_app.command(
    "batches",
    help="List recent parser batches with counts and health flags.",
)
def batches_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="[SOURCE]",
        help="Optional source name to list batches for (defaults to all).",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        min=1,
        help="Maximum number of batches to display.",
    ),
) -> None:
    context = _require_context(ctx)
    summary = f"source={source or '*'},limit={limit}"
    _emit_unimplemented(context, command="batches", summary=summary)


@_parser_app.command(
    "remove",
    help="Remove a parser batch, optionally forcing latest batch deletion.",
)
def remove_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="[SOURCE]",
        help="Optional source name to prune batches from.",
    ),
    batch: str | None = typer.Argument(
        None,
        metavar="[BATCH-ID]",
        help="Optional batch identifier to remove.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow removing the most recent batch without confirmation.",
    ),
) -> None:
    context = _require_context(ctx)
    summary = f"source={source or '*'},batch={batch or '*'},force={force}"
    _emit_unimplemented(context, command="remove", summary=summary)


def create_parser_app() -> typer.Typer:
    """Return the parser Typer application."""

    return _parser_app


__all__ = ["create_parser_app"]
