"""Typer command group for vector database (VDB) operations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import typer

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import (
    ManifestService,
    manifest_settings_from_config,
)
from raggd.modules.vdb.providers import create_default_provider_registry
from raggd.modules.vdb.service import VdbService
from raggd.source.config import SourceConfigError, SourceConfigStore


@dataclass(slots=True)
class VdbCLIContext:
    """Shared context carried across `raggd vdb` commands."""

    paths: WorkspacePaths
    config: AppConfig
    store: SourceConfigStore
    service: VdbService
    logger: Logger


_vdb_app = typer.Typer(
    name="vdb",
    help=(
        "Manage per-source vector databases: create, sync, inspect, and "
        "reset.\n\n"
        "Materialize embeddings, review health signals, and repair FAISS "
        "artifacts for each configured source."
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


def _build_vdb_service(
    *,
    paths: WorkspacePaths,
    config: AppConfig,
    logger: Logger,
) -> VdbService:
    """Return a configured VDB service instance."""

    config_payload = config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(config_payload)
    db_settings = db_settings_from_mapping(config_payload)

    manifest_service = ManifestService(
        workspace=paths,
        settings=manifest_settings,
        logger=logger.bind(component="manifest"),
    )
    db_service = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest_service,
        db_settings=db_settings,
        logger=logger.bind(component="db-service"),
    )

    providers = create_default_provider_registry()

    service = VdbService(
        workspace=paths,
        config=config,
        db_service=db_service,
        providers=providers,
        logger=logger.bind(component="vdb-service"),
    )

    logger.debug(
        "vdb-service-configured",
        providers=tuple(sorted(providers.snapshot().keys())),
    )
    return service


def _handle_not_implemented(action: str, *, logger: Logger) -> None:
    message = f"VDB {action} is not implemented yet; CLI scaffold is in place."
    typer.secho(message, fg=typer.colors.YELLOW)
    logger.warning("vdb-action-not-implemented", action=action)


def _handle_service_failure(
    action: str,
    error: Exception,
    *,
    logger: Logger,
) -> None:
    typer.secho(
        f"VDB {action} failed: {error}",
        fg=typer.colors.RED,
    )
    logger.error("vdb-action-failed", action=action, error=str(error))
    raise typer.Exit(code=1) from error


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
            "Override log level for vdb commands "
            "(defaults to config log_level)."
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

    paths.sources_dir.mkdir(parents=True, exist_ok=True)

    service = _build_vdb_service(
        paths=paths,
        config=config,
        logger=logger,
    )

    ctx.obj = VdbCLIContext(
        paths=paths,
        config=config,
        store=store,
        service=service,
        logger=logger,
    )


@_vdb_app.command(
    "info",
    help=(
        "List VDB metadata, counts, and health findings for one source or all "
        "configured sources. Use --json to emit the structured payload "
        "described in the implementation spec."
    ),
)
def info_vdb(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="[SOURCE]",
        help="Optional source name to filter results (defaults to all).",
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
        help=(
            "Emit machine-readable JSON including selector, batch, embedding "
            "model, counts, paths, timestamps, and health entries."
        ),
    ),
) -> None:
    """Render VDB summaries including counts, paths, timestamps, and health."""

    context = _require_context(ctx)
    try:
        records = context.service.info(source=source, vdb=vdb)
    except NotImplementedError as exc:
        _handle_not_implemented("info", logger=context.logger)
        context.logger.debug(
            "vdb-info-not-implemented",
            source=source,
            vdb=vdb,
            error=str(exc),
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        _handle_service_failure("info", exc, logger=context.logger)
        return

    if json_output:
        typer.echo(json.dumps(list(records), indent=2, sort_keys=True))
        context.logger.info(
            "vdb-info",
            source=source,
            vdb=vdb,
            json=True,
            count=len(records),
        )
        return

    if not records:
        typer.secho("No VDBs found.", fg=typer.colors.YELLOW)
    else:
        for record in records:
            name = record.get("selector") or record.get("name")
            typer.secho(
                f"VDB {name}",
                fg=typer.colors.CYAN,
                bold=True,
            )
            for key, value in sorted(record.items()):
                typer.echo(f"  {key}: {value}")

    context.logger.info(
        "vdb-info",
        source=source,
        vdb=vdb,
        json=False,
        count=len(records),
    )


@_vdb_app.command(
    "create",
    help=(
        "Create a VDB bound to a parser batch and embedding model. Existing "
        "names must refer to the same batch/model pairing, otherwise the "
        "command fails with remediation guidance."
    ),
)
def create_vdb(
    ctx: typer.Context,
    selector: str = typer.Argument(
        ...,
        metavar="SOURCE@BATCH",
        help=(
            "Source and batch selector (accepts the `latest` alias for the most"
            " recent batch). The referenced batch must already exist."
        ),
    ),
    name: str = typer.Argument(
        ...,
        metavar="NAME",
        help="Human-friendly VDB name; must be unique per source.",
    ),
    model: str = typer.Option(
        ...,
        "--model",
        "-m",
        metavar="PROVIDER:MODEL",
        help=(
            "Embedding model identifier (provider:name or provider:id), e.g."
            " `openai:text-embedding-3-small`."
        ),
    ),
) -> None:
    """Create or verify the requested VDB and confirm the bound model."""

    context = _require_context(ctx)
    try:
        context.service.create(selector=selector, name=name, model=model)
    except NotImplementedError as exc:
        _handle_not_implemented("create", logger=context.logger)
        context.logger.debug(
            "vdb-create-not-implemented",
            selector=selector,
            name=name,
            model=model,
            error=str(exc),
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        _handle_service_failure("create", exc, logger=context.logger)
        return

    typer.secho(
        f"Created VDB {name} for {selector} using model {model}",
        fg=typer.colors.GREEN,
    )
    context.logger.info(
        "vdb-create",
        selector=selector,
        name=name,
        model=model,
    )


@_vdb_app.command(
    "sync",
    help=(
        "Materialize chunks, generate embeddings, and update the FAISS index. "
        "Supports incremental refresh via --missing-only, atomic rebuilds via "
        "--recompute, and dry-run planning."
    ),
)
def sync_vdb(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        metavar="SOURCE",
        help="Source name to synchronize.",
    ),
    vdb: str | None = typer.Option(
        None,
        "--vdb",
        metavar="NAME",
        help="Optional VDB name to target (defaults to all for the source).",
    ),
    missing_only: bool = typer.Option(
        False,
        "--missing-only",
        help=(
            "Embed only chunks without vectors while validating existing rows."
        ),
    ),
    recompute: bool = typer.Option(
        False,
        "--recompute",
        help="Rebuild embeddings and index atomically before swapping them in.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        min=1,
        help=(
            "Optional limit on chunks to process; pair with --dry-run or "
            "targeted repairs."
        ),
    ),
    concurrency: str | None = typer.Option(
        None,
        "--concurrency",
        "-c",
        help=(
            "Override concurrency (integer or 'auto'); defaults to provider "
            "and workspace configuration."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Plan actions without performing writes; summary reports "
            "`vectors_planned` instead of persisted counts."
        ),
    ),
) -> None:
    """Synchronize vectors and report chunk/vector counts plus run metadata."""

    if missing_only and recompute:
        raise typer.BadParameter(
            "--missing-only and --recompute are mutually exclusive",
            param_hint="--missing-only/--recompute",
        )

    context = _require_context(ctx)
    try:
        summary = context.service.sync(
            source=source,
            vdb=vdb,
            missing_only=missing_only,
            recompute=recompute,
            limit=limit,
            concurrency=concurrency,
            dry_run=dry_run,
        )
    except NotImplementedError as exc:
        _handle_not_implemented("sync", logger=context.logger)
        context.logger.debug(
            "vdb-sync-not-implemented",
            source=source,
            vdb=vdb,
            missing_only=missing_only,
            recompute=recompute,
            limit=limit,
            concurrency=concurrency,
            dry_run=dry_run,
            error=str(exc),
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        _handle_service_failure("sync", exc, logger=context.logger)
        return

    typer.secho("VDB sync complete", fg=typer.colors.GREEN, bold=True)
    for key, value in sorted(summary.items()):
        typer.echo(f"  {key}: {value}")

    context.logger.info(
        "vdb-sync",
        source=source,
        vdb=vdb,
        missing_only=missing_only,
        recompute=recompute,
        limit=limit,
        concurrency=concurrency,
        dry_run=dry_run,
        summary=summary,
    )


@_vdb_app.command(
    "reset",
    help=(
        "Remove vector artifacts and optionally drop the VDB entry after "
        "confirmation. Summaries include deleted row counts and removed files."
    ),
)
def reset_vdb(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        metavar="SOURCE",
        help="Source name containing the VDB(s).",
    ),
    vdb: str | None = typer.Option(
        None,
        "--vdb",
        metavar="NAME",
        help="Optional VDB name to reset (defaults to all for the source).",
    ),
    drop: bool = typer.Option(
        False,
        "--drop",
        help="Remove the VDB record after clearing artifacts from disk.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass interactive confirmation prompts for destructive actions.",
    ),
) -> None:
    """Purge vectors, artifacts, and optionally the VDB metadata entry."""

    context = _require_context(ctx)
    try:
        summary = context.service.reset(
            source=source,
            vdb=vdb,
            drop=drop,
            force=force,
        )
    except NotImplementedError as exc:
        _handle_not_implemented("reset", logger=context.logger)
        context.logger.debug(
            "vdb-reset-not-implemented",
            source=source,
            vdb=vdb,
            drop=drop,
            force=force,
            error=str(exc),
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        _handle_service_failure("reset", exc, logger=context.logger)
        return

    typer.secho("VDB reset complete", fg=typer.colors.GREEN, bold=True)
    for key, value in sorted(summary.items()):
        typer.echo(f"  {key}: {value}")

    context.logger.info(
        "vdb-reset",
        source=source,
        vdb=vdb,
        drop=drop,
        force=force,
        summary=summary,
    )


def create_vdb_app() -> "typer.Typer":
    """Return the Typer application for `raggd vdb`."""

    return _vdb_app


__all__ = ["create_vdb_app"]
