"""Typer command group for managing workspace sources."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import typer

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.source import (
    SourceConfigError,
    SourceConfigStore,
    SourceError,
    SourceHealthCheckError,
    SourceHealthStatus,
    SourcePathError,
    SourceService,
    SourceSlugError,
    SourceState,
)


@dataclass(slots=True)
class SourceCLIContext:
    """Shared context object carried across `raggd source` commands."""

    paths: WorkspacePaths
    config: AppConfig
    store: SourceConfigStore
    service: SourceService
    logger: Logger


_source_app = typer.Typer(
    name="source",
    help="Manage workspace sources (init, target, refresh, list, enable, disable).",
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


def _require_context(ctx: typer.Context) -> SourceCLIContext:
    context = getattr(ctx, "obj", None)
    if not isinstance(context, SourceCLIContext):
        typer.secho("Internal error: source context not initialized.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return context


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "never"
    return value.isoformat()


def _status_color(status: SourceHealthStatus) -> str | None:
    if status is SourceHealthStatus.OK:
        return typer.colors.GREEN
    if status is SourceHealthStatus.UNKNOWN:
        return typer.colors.YELLOW
    if status is SourceHealthStatus.DEGRADED:
        return typer.colors.BRIGHT_YELLOW
    if status is SourceHealthStatus.ERROR:
        return typer.colors.RED
    return None


def _emit_state_summary(state: SourceState, *, prefix: str = "") -> None:
    manifest = state.manifest
    status = manifest.last_health.status
    color = _status_color(status)
    header = f"{prefix}source: {state.config.name}"
    typer.secho(header, fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  enabled: {'yes' if state.config.enabled else 'no'}")
    typer.echo(f"  path: {state.config.path}")
    if state.config.target is not None:
        typer.echo(f"  target: {state.config.target}")
    else:
        typer.echo("  target: <unset>")
    typer.echo(f"  last refresh: {_format_timestamp(manifest.last_refresh_at)}")
    status_line = f"  health: {status.value}"
    if color:
        typer.secho(status_line, fg=color)
    else:
        typer.echo(status_line)
    if manifest.last_health.summary:
        typer.echo(f"  summary: {manifest.last_health.summary}")
    if manifest.last_health.actions:
        typer.echo("  actions:")
        for action in manifest.last_health.actions:
            typer.echo(f"    - {action}")


def _emit_health_guidance(
    context: SourceCLIContext,
    name: str,
) -> None:
    try:
        states = context.service.list()
    except SourceError:
        return

    for state in states:
        if state.config.name == name:
            typer.echo()
            typer.secho(
                f"Latest health snapshot for {name}:",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            _emit_state_summary(state, prefix="")
            break


def _handle_failure(
    context: SourceCLIContext,
    *,
    action: str,
    error: Exception,
    source: str | Sequence[str] | None = None,
) -> None:
    message = f"{action} failed: {error}"
    typer.secho(message, fg=typer.colors.RED)
    log = context.logger.bind(action=action)
    payload: dict[str, object] = {"error": str(error)}
    if source is not None:
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            payload["sources"] = list(source)
        else:
            payload["source"] = source
    log.error("source-command-failed", **payload)
    if isinstance(error, SourceHealthCheckError) and isinstance(source, str):
        _emit_health_guidance(context, source)
    raise typer.Exit(code=1) from error


def _confirm(prompt: str) -> None:
    if not typer.confirm(prompt, default=False):
        typer.echo("Operation cancelled.")
        raise typer.Exit(code=1)


@_source_app.callback()
def configure_source_commands(
    ctx: typer.Context,
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Override workspace directory (defaults to RAGGD_WORKSPACE or ~/.raggd)."
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
            f"Workspace config not found at {paths.config_file}. Run `raggd init` first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    store = SourceConfigStore(config_path=paths.config_file)
    try:
        config = store.load()
    except SourceConfigError as exc:
        typer.secho(f"Failed to load workspace config: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    configure_logging(level=config.log_level, workspace_path=config.workspace)
    logger = get_logger(__name__, command="source")
    service = SourceService(workspace=paths, config_store=store)
    paths.sources_dir.mkdir(parents=True, exist_ok=True)

    ctx.obj = SourceCLIContext(
        paths=paths,
        config=config,
        store=store,
        service=service,
        logger=logger,
    )


@_source_app.command(
    "init",
    help="Create a new source and optionally seed it from a target directory.",
)
def init_source(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="NAME", help="Name for the new source."),
    target: Path | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Optional target directory to associate with the source.",
    ),
    force_refresh: bool = typer.Option(
        False,
        "--force-refresh",
        help="Skip confirmation before the initial refresh when --target is set.",
    ),
) -> None:
    context = _require_context(ctx)
    should_refresh = target is not None or force_refresh
    if should_refresh and not force_refresh:
        target_display = str(target) if target is not None else "configured target"
        _confirm(
            f"This will refresh source {name!r} from {target_display}. Continue?",
        )

    try:
        state = context.service.init(name, target=target, force_refresh=force_refresh)
    except (SourceError, SourceSlugError, SourcePathError) as exc:
        _handle_failure(context, action="init", error=exc, source=name)
    else:
        context.logger.bind(action="init").info(
            "source-init",
            source=state.config.name,
            target=str(state.config.target) if state.config.target else None,
            enabled=state.config.enabled,
            force_refresh=force_refresh,
        )
        _emit_state_summary(state)


@_source_app.command(
    "target",
    help="Set or clear the target directory for a source and trigger refresh handling.",
)
def update_target(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    directory: Path | None = typer.Argument(
        None,
        metavar="[DIR]",
        help="New target directory (omit when using --clear).",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Clear the configured target for the source.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass health gating and skip confirmation prompts.",
    ),
) -> None:
    context = _require_context(ctx)

    if clear and directory is not None:
        typer.secho("--clear cannot be combined with a directory argument.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if not clear and directory is None:
        typer.secho("A target directory is required unless --clear is provided.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    target_arg: Path | None = None
    if clear:
        if not force:
            _confirm(f"Clear the target for source {name!r}?")
    else:
        target_arg = directory
        if not force:
            _confirm(
                f"This will refresh source {name!r} after updating the target. Continue?",
            )

    try:
        state = context.service.set_target(name, target_arg, force=force)
    except (SourceError, SourcePathError) as exc:
        _handle_failure(context, action="target", error=exc, source=name)
    else:
        context.logger.bind(action="target").info(
            "source-target-updated",
            source=state.config.name,
            target=str(state.config.target) if state.config.target else None,
            cleared=target_arg is None,
            forced=force,
        )
        _emit_state_summary(state)


@_source_app.command(
    "refresh",
    help="Refresh cached artifacts for a source, respecting health gating unless forced.",
)
def refresh_source(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass health gating and confirmation prompts.",
    ),
) -> None:
    context = _require_context(ctx)
    if not force:
        _confirm(f"Refresh source {name!r}? This will reset cached artifacts.")

    try:
        state = context.service.refresh(name, force=force)
    except SourceError as exc:
        _handle_failure(context, action="refresh", error=exc, source=name)
    else:
        context.logger.bind(action="refresh").info(
            "source-refresh",
            source=state.config.name,
            forced=force,
        )
        _emit_state_summary(state)


@_source_app.command(
    "rename",
    help="Rename an existing source and update its configuration and manifests.",
)
def rename_source(
    ctx: typer.Context,
    current: str = typer.Argument(..., metavar="CURRENT"),
    new: str = typer.Argument(..., metavar="NEW"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass health gating when the source is degraded or disabled.",
    ),
) -> None:
    context = _require_context(ctx)

    try:
        state = context.service.rename(current, new, force=force)
    except (SourceError, SourceSlugError) as exc:
        _handle_failure(context, action="rename", error=exc, source=current)
    else:
        context.logger.bind(action="rename").info(
            "source-rename",
            source=current,
            renamed_to=state.config.name,
            forced=force,
        )
        _emit_state_summary(state)


@_source_app.command(
    "remove",
    help="Delete a source and its managed artifacts, bypassing health gating with --force.",
)
def remove_source(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove the source even if health checks fail or it is disabled.",
    ),
) -> None:
    context = _require_context(ctx)
    if not force:
        _confirm(f"Remove source {name!r}? This deletes managed artifacts.")

    try:
        context.service.remove(name, force=force)
    except SourceError as exc:
        _handle_failure(context, action="remove", error=exc, source=name)
    else:
        context.logger.bind(action="remove").info(
            "source-remove",
            source=name,
            forced=force,
        )
        typer.secho(f"Removed source {name}", fg=typer.colors.GREEN)


@_source_app.command(
    "enable",
    help="Enable one or more sources after running their health checks.",
)
def enable_sources(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., metavar="NAME...", min=1),
) -> None:
    context = _require_context(ctx)

    try:
        states = context.service.enable(*names)
    except SourceError as exc:
        _handle_failure(context, action="enable", error=exc, source=names)
    else:
        context.logger.bind(action="enable").info(
            "source-enable",
            sources=names,
        )
        for index, state in enumerate(states):
            if index:
                typer.echo()
            _emit_state_summary(state)


@_source_app.command(
    "disable",
    help="Disable one or more sources to prevent guarded operations.",
)
def disable_sources(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., metavar="NAME...", min=1),
) -> None:
    context = _require_context(ctx)

    try:
        states = context.service.disable(*names)
    except SourceError as exc:
        _handle_failure(context, action="disable", error=exc, source=names)
    else:
        context.logger.bind(action="disable").info(
            "source-disable",
            sources=names,
        )
        for index, state in enumerate(states):
            if index:
                typer.echo()
            _emit_state_summary(state)


@_source_app.command(
    "list",
    help="List configured sources with their health summaries and exit codes.",
)
def list_sources(ctx: typer.Context) -> None:
    context = _require_context(ctx)

    try:
        states = context.service.list()
    except SourceError as exc:
        _handle_failure(context, action="list", error=exc)

    if not states:
        typer.secho("No sources are configured.", fg=typer.colors.YELLOW)
        return

    exit_code = 0
    typer.secho("Configured sources:", fg=typer.colors.CYAN, bold=True)
    for index, state in enumerate(states):
        if index:
            typer.echo()
        _emit_state_summary(state)
        status = state.manifest.last_health.status
        if status is not SourceHealthStatus.OK:
            exit_code = 1

    context.logger.bind(action="list").info(
        "source-list",
        sources=[state.config.name for state in states],
        exit_code=exit_code,
    )

    if exit_code:
        raise typer.Exit(code=exit_code)


def create_source_app() -> typer.Typer:
    """Return the Typer app handling `raggd source` subcommands."""

    return _source_app


__all__ = ["create_source_app"]
