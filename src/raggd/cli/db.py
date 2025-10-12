"""Typer command group handling database lifecycle operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence

import typer

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import (
    DbLifecycleError,
    DbLifecycleNotImplementedError,
    DbLifecycleService,
    db_settings_from_mapping,
)
from raggd.modules.manifest import manifest_settings_from_config
from raggd.source.config import SourceConfigError, SourceConfigStore


@dataclass(slots=True)
class DbCLIContext:
    """Shared context carried across `raggd db` commands."""

    paths: WorkspacePaths
    config: AppConfig
    store: SourceConfigStore
    service: DbLifecycleService
    logger: Logger


_db_app = typer.Typer(
    name="db",
    help="Manage per-source database lifecycle (ensure/upgrade/etc).",
    no_args_is_help=True,
    invoke_without_command=False,
)


def _echo_table_counts(counts: Mapping[str, object]) -> None:
    """Render table count details with nested indentation."""

    typer.echo("  table_counts:")
    for table, total in sorted(counts.items()):
        display = "skipped" if total is None else str(total)
        typer.echo(f"    {table}: {display}")


def _echo_table_counts_skipped(value: object) -> bool:
    """Render skipped table count entries and return True when any exist."""

    entries: list[Mapping[str, object]] = []
    extras: list[object] = []

    if isinstance(value, Mapping):
        entries.append(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, Mapping):
                entries.append(item)
            else:
                extras.append(item)
    elif value:
        extras.append(value)

    if not entries and not extras:
        return False

    typer.echo("  table_counts_skipped:")
    for entry in entries:
        table = entry.get("table", "<unknown>")
        reason = entry.get("reason")
        details = [
            f"{key}={entry[key]}"
            for key in sorted(entry)
            if key not in {"table", "reason"}
        ]
        fragments: list[str] = []
        if reason is not None:
            fragments.append(str(reason))
        if details:
            fragments.append(", ".join(details))
        if fragments:
            typer.echo(f"    - {table} ({'; '.join(fragments)})")
        else:
            typer.echo(f"    - {table}")
    for extra in extras:
        typer.echo(f"    - {extra}")
    return True


def _echo_table_counts_skip_summary(value: object) -> str | None:
    """Render skip summary details and return a condensed summary string."""

    if not value:
        return None

    if isinstance(value, Mapping):
        typer.echo("  table_counts_skipped_summary:")
        parts: list[str] = []
        for reason, count in sorted(value.items()):
            typer.echo(f"    {reason}: {count}")
            parts.append(f"{reason}: {count}")
        return ", ".join(parts)

    typer.echo(f"  table_counts_skipped_summary: {value}")
    return str(value)


def _echo_info_payload(info: Mapping[str, object]) -> None:
    """Display info payload with enriched formatting for table counts."""

    skipped_present = False
    skip_summary: str | None = None

    for key, value in sorted(info.items()):
        if key == "table_counts" and isinstance(value, Mapping):
            _echo_table_counts(value)
        elif key == "table_counts_skipped":
            skipped_present = _echo_table_counts_skipped(value)
        elif key == "table_counts_skipped_summary":
            skip_summary = _echo_table_counts_skip_summary(value)
        else:
            typer.echo(f"  {key}: {value}")

    if skipped_present:
        note = "Some table counts were skipped"
        if skip_summary:
            note = f"{note} ({skip_summary})"
        typer.secho(f"  counts note: {note}", fg=typer.colors.YELLOW)


def _resolve_workspace_override(workspace: Path | None) -> WorkspacePaths:
    env_workspace = os.environ.get("RAGGD_WORKSPACE")
    env_override = Path(env_workspace).expanduser() if env_workspace else None
    return resolve_workspace(
        workspace_override=workspace,
        env_override=env_override,
    )


def _require_context(ctx: typer.Context) -> DbCLIContext:
    context = getattr(ctx, "obj", None)
    if not isinstance(context, DbCLIContext):
        typer.secho(
            "Internal error: db context not initialized.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    return context


def _resolve_targets(
    context: DbCLIContext,
    names: Sequence[str] | None,
) -> tuple[str, ...]:
    if names:
        normalized = [name.strip() for name in names if name and name.strip()]
        return tuple(dict.fromkeys(normalized))
    return tuple(sorted(context.config.workspace_sources))


def _handle_failure(
    context: DbCLIContext,
    *,
    action: str,
    error: Exception,
    source: str | None = None,
) -> None:
    message = f"{action} failed"
    if source:
        message = f"{message} for {source}"
    message = f"{message}: {error}"
    color = typer.colors.RED
    if isinstance(error, DbLifecycleNotImplementedError):
        color = typer.colors.YELLOW
    typer.secho(message, fg=color)
    payload: dict[str, object] = {"action": action, "error": str(error)}
    if source is not None:
        payload["source"] = source
    context.logger.error("db-command-failed", **payload)
    raise typer.Exit(code=1) from error


@_db_app.callback()
def configure_db_commands(
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
            "Override log level for db commands (defaults to config log_level)."
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

    logger = get_logger(__name__, command="db")
    config_payload = config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(config_payload)
    db_settings = db_settings_from_mapping(config_payload)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=manifest_settings,
        db_settings=db_settings,
        logger=logger.bind(component="service"),
    )

    paths.sources_dir.mkdir(parents=True, exist_ok=True)

    ctx.obj = DbCLIContext(
        paths=paths,
        config=config,
        store=store,
        service=service,
        logger=logger,
    )


@_db_app.command(
    "ensure",
    help="Ensure databases exist for the provided sources (or all configured).",
)
def ensure_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to ensure.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            path = context.service.ensure(name)
        except DbLifecycleError as exc:
            _handle_failure(context, action="ensure", error=exc, source=name)
        else:
            typer.secho(
                f"Ensured database for {name}: {path}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-ensure",
                source=name,
                path=str(path),
            )


@_db_app.command(
    "upgrade",
    help="Apply pending migrations for the provided sources (or all).",
)
def upgrade_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    steps: int | None = typer.Option(
        None,
        "--steps",
        "-s",
        min=1,
        help="Limit the number of migrations applied (defaults to all).",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to upgrade.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            context.service.upgrade(name, steps=steps)
        except DbLifecycleError as exc:
            _handle_failure(context, action="upgrade", error=exc, source=name)
        else:
            typer.secho(
                f"Upgraded database for {name}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-upgrade",
                source=name,
                steps=steps,
            )


@_db_app.command(
    "downgrade",
    help="Rollback migrations for the provided sources (defaults to head-1).",
)
def downgrade_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    steps: int = typer.Option(
        1,
        "--steps",
        "-s",
        min=1,
        help="Number of migrations to rollback (defaults to 1).",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to downgrade.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            context.service.downgrade(name, steps=steps)
        except DbLifecycleError as exc:
            _handle_failure(context, action="downgrade", error=exc, source=name)
        else:
            typer.secho(
                f"Downgraded database for {name}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-downgrade",
                source=name,
                steps=steps,
            )


@_db_app.command(
    "info",
    help="Display database status for the provided sources (or all).",
)
def info_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    include_schema: bool = typer.Option(
        False,
        "--schema",
        help="Include schema information in the output.",
    ),
    counts: bool = typer.Option(
        True,
        "--counts/--no-counts",
        help="Toggle inclusion of per-table row counts.",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to report.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            info = context.service.info(
                name,
                include_schema=include_schema,
                include_counts=counts,
            )
        except DbLifecycleError as exc:
            _handle_failure(context, action="info", error=exc, source=name)
        else:
            typer.secho(
                f"Database info for {name}",
                fg=typer.colors.CYAN,
                bold=True,
            )
            _echo_info_payload(info)
            context.logger.info(
                "db-info",
                source=name,
                include_schema=include_schema,
                include_counts=counts,
            )


@_db_app.command(
    "vacuum",
    help="Run vacuum maintenance for the provided sources (or all).",
)
def vacuum_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    concurrency: str | None = typer.Option(
        None,
        "--concurrency",
        "-c",
        help="Override vacuum concurrency (int or 'auto').",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to vacuum.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            context.service.vacuum(name, concurrency=concurrency)
        except DbLifecycleError as exc:
            _handle_failure(context, action="vacuum", error=exc, source=name)
        else:
            typer.secho(
                f"Vacuum triggered for {name}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-vacuum",
                source=name,
                concurrency=concurrency,
            )


@_db_app.command(
    "run",
    help="Execute a SQL file against the provided sources (or all).",
)
def run_sql(
    ctx: typer.Context,
    sql_file: Path = typer.Argument(
        ...,
        metavar="SQL_FILE",
        exists=True,
        readable=True,
        resolve_path=True,
        help="Path to a .sql file to execute.",
    ),
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    autocommit: bool = typer.Option(
        False,
        "--autocommit/--transaction",
        help=(
            "Execute without wrapping in a transaction "
            "(defaults to transaction)."
        ),
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to run against.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    for name in targets:
        try:
            context.service.run(name, sql_path=sql_file, autocommit=autocommit)
        except DbLifecycleError as exc:
            _handle_failure(context, action="run", error=exc, source=name)
        else:
            typer.secho(
                f"Executed {sql_file} for {name}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-run",
                source=name,
                sql_path=str(sql_file),
                autocommit=autocommit,
            )


@_db_app.command(
    "reset",
    help="Reset (drop and reinitialize) databases for the provided sources.",
)
def reset_databases(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        None,
        metavar="[NAME]...",
        help="Optional source names to operate on (defaults to all).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass confirmation prompts when resetting.",
    ),
) -> None:
    context = _require_context(ctx)
    targets = list(_resolve_targets(context, names))
    if not targets:
        typer.secho(
            "No sources configured; nothing to reset.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    if not force:
        names_display = ", ".join(targets)
        if not typer.confirm(
            f"Reset databases for {names_display or 'all sources'}?",
            default=False,
        ):
            typer.echo("Operation cancelled.")
            raise typer.Exit(code=1)

    for name in targets:
        try:
            context.service.reset(name, force=True)
        except DbLifecycleError as exc:
            _handle_failure(context, action="reset", error=exc, source=name)
        else:
            typer.secho(
                f"Reset database for {name}",
                fg=typer.colors.GREEN,
            )
            context.logger.info(
                "db-reset",
                source=name,
            )


def create_db_app() -> typer.Typer:
    """Return the Typer app managing `raggd db` subcommands."""

    return _db_app


__all__ = ["create_db_app"]
