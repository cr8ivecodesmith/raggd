"""Typer command group scaffolding for the parser module."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

import typer

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import ManifestService, manifest_settings_from_config
from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)
from raggd.modules.parser import ParserService
from raggd.source.config import SourceConfigError, SourceConfigStore


class ParserSessionError(RuntimeError):
    """Base error raised when acquiring a parser session fails."""


class ParserSessionTimeout(ParserSessionError):
    """Raised when acquiring a parser session lock times out."""


@dataclass(slots=True)
class ParserSessionGuard:
    """Coordinate parser CLI sessions via filesystem locks."""

    root: Path
    logger: Logger
    timeout: float = 10.0
    poll_interval: float = 0.1

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _sanitize_scope(self, scope: str | None) -> str:
        if not scope:
            return "workspace"
        cleaned = scope.strip().replace(os.sep, "_").replace("/", "_")
        return cleaned or "workspace"

    def _lock_path(self, scope: str | None) -> Path:
        name = self._sanitize_scope(scope)
        return self.root / f"{name}.lock"

    @contextmanager
    def acquire(
        self,
        *,
        scope: str | None = None,
        action: str = "parser-cli",
    ) -> Iterator[None]:
        lock_path = self._lock_path(scope)
        log = self.logger.bind(
            scope=scope or "workspace",
            action=action,
            path=str(lock_path),
        )
        lock = FileLock(
            lock_path,
            timeout=self.timeout,
            poll_interval=self.poll_interval,
        )
        log.debug("parser-session-acquire")
        try:
            with lock:
                yield
        except ManifestLockTimeoutError as exc:
            log.error("parser-session-timeout", error=str(exc))
            raise ParserSessionTimeout(str(exc)) from exc
        except ManifestLockError as exc:
            log.error("parser-session-lock-error", error=str(exc))
            raise ParserSessionError(str(exc)) from exc
        finally:
            log.debug("parser-session-release")


@dataclass(slots=True)
class ParserCLIContext:
    """Shared context persisted across parser subcommands."""

    paths: WorkspacePaths
    config: AppConfig
    store: SourceConfigStore
    settings: ParserModuleSettings
    logger: Logger
    manifest: ManifestService
    db_service: DbLifecycleService
    parser_service: ParserService
    session_guard: ParserSessionGuard


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
    parser_service = ParserService(
        workspace=paths,
        config=config,
        settings=settings,
        manifest_service=manifest_service,
        db_service=db_service,
        logger=logger.bind(component="parser-service"),
    )

    locks_root = paths.workspace / ".locks" / "parser"
    session_guard = ParserSessionGuard(
        root=locks_root,
        logger=logger.bind(component="session-guard"),
    )

    paths.sources_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "parser-cli-context-created",
        enabled=settings.enabled,
        max_concurrency=settings.max_concurrency,
        fail_fast=settings.fail_fast,
        gitignore_behavior=settings.gitignore_behavior.value,
        locks_root=str(locks_root),
    )

    ctx.obj = ParserCLIContext(
        paths=paths,
        store=store,
        config=config,
        settings=settings,
        logger=logger,
        manifest=manifest_service,
        db_service=db_service,
        parser_service=parser_service,
        session_guard=session_guard,
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
