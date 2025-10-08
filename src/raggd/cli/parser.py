"""Typer command group scaffolding for the parser module."""

from __future__ import annotations

import concurrent.futures
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Sequence

import typer

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.manifest import ManifestService, manifest_settings_from_config
from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)
from raggd.modules.parser import (
    FileStageOutcome,
    HandlerResult,
    ParseContext,
    ParserBatchPlan,
    ParserModuleDisabledError,
    ParserPlanEntry,
    ParserService,
    ParserSourceNotConfiguredError,
    ParserRunMetrics,
    TokenEncoderError,
)
from raggd.modules.parser.service import ParserError
from raggd.source.config import SourceConfigError, SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


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


@dataclass(slots=True)
class _ParseTarget:
    """Normalized representation of a parser invocation target."""

    name: str
    config: WorkspaceSourceConfig
    scope_paths: tuple[Path, ...] = ()
    missing_scope: tuple[str, ...] = ()


@dataclass(slots=True)
class _ParseOutcome:
    """Aggregate result returned by individual source parses."""

    source: str
    batch_id: str | None
    batch_ref: str | None
    plan: ParserBatchPlan | None = None
    metrics: ParserRunMetrics | None = None
    staged: tuple[tuple[ParserPlanEntry, FileStageOutcome], ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    failed_files: tuple[str, ...] = ()
    missing_scope: tuple[str, ...] = ()
    aborted: bool = False

    @property
    def has_failures(self) -> bool:
        return bool(self.errors or self.failed_files or self.aborted)


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


def _sorted_workspace_sources(
    config: AppConfig,
) -> tuple[tuple[str, WorkspaceSourceConfig], ...]:
    items: list[tuple[str, WorkspaceSourceConfig]] = list(
        config.iter_workspace_sources()
    )
    items.sort(key=lambda item: item[0])
    return tuple(items)


def _split_target_tokens(
    tokens: Sequence[str] | None,
    available: Iterable[str],
) -> tuple[list[str], list[str]]:
    known = set(available)
    selected: list[str] = []
    scope_tokens: list[str] = []
    unknown_mode = False
    for token in tokens or ():
        value = token.strip()
        if not value:
            continue
        if not unknown_mode and value in known:
            selected.append(value)
            continue
        unknown_mode = True
        scope_tokens.append(value)
    return selected, scope_tokens


def _resolve_scope_paths(
    *,
    config: WorkspaceSourceConfig,
    tokens: Sequence[str],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    if not tokens:
        return (), ()

    resolved: list[Path] = []
    missing: list[str] = []
    root = config.path
    for token in tokens:
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            missing.append(token)
            continue
        if not candidate.exists():
            missing.append(token)
            continue
        resolved.append(candidate)
    return tuple(resolved), tuple(missing)


def _resolve_parse_targets(
    context: ParserCLIContext,
    tokens: Sequence[str] | None,
) -> tuple[list[_ParseTarget], list[str]]:
    sources = _sorted_workspace_sources(context.config)
    available_names = tuple(name for name, _ in sources)
    selected, scope_tokens = _split_target_tokens(tokens, available_names)

    invalid: list[str] = []
    resolved_targets: list[_ParseTarget] = []

    if selected:
        name_lookup = {name: cfg for name, cfg in sources}
        for name in selected:
            config = name_lookup.get(name)
            if config is None:
                invalid.append(name)
                continue
            scope_paths, missing = _resolve_scope_paths(
                config=config,
                tokens=scope_tokens,
            )
            resolved_targets.append(
                _ParseTarget(
                    name=name,
                    config=config,
                    scope_paths=scope_paths,
                    missing_scope=missing,
                )
            )
    else:
        for name, config in sources:
            scope_paths, missing = _resolve_scope_paths(
                config=config,
                tokens=scope_tokens,
            )
            resolved_targets.append(
                _ParseTarget(
                    name=name,
                    config=config,
                    scope_paths=scope_paths,
                    missing_scope=missing,
                )
            )

    return resolved_targets, invalid


def _resolve_concurrency(
    setting: int | str,
    *,
    target_count: int,
) -> int:
    if target_count <= 0:
        return 0
    if isinstance(setting, int):
        return max(1, min(setting, target_count))
    if setting != "auto":  # Defensive guard; validation should prevent this.
        return max(1, target_count)
    cpu_count = os.cpu_count() or 1
    return max(1, min(cpu_count, target_count))


def _determine_fail_fast(
    *,
    override: bool | None,
    settings: ParserModuleSettings,
) -> bool:
    if override is None:
        return settings.fail_fast
    return bool(override)


def _scope_summary(paths: Sequence[Path]) -> str | None:
    if not paths:
        return None
    return ",".join(sorted(path.as_posix() for path in paths))


def _build_handler_context(
    *,
    parser_service: ParserService,
    cli_context: ParserCLIContext,
    source: _ParseTarget,
) -> ParseContext:
    token_encoder = parser_service.token_encoder()
    return ParseContext(
        source=source.name,
        root=source.config.path,
        workspace=cli_context.paths,
        config=cli_context.config,
        settings=cli_context.settings,
        token_encoder=token_encoder,
        logger=cli_context.logger.bind(source=source.name, component="handler"),
    )


def _parse_single_source(
    context: ParserCLIContext,
    target: _ParseTarget,
    *,
    fail_fast: bool,
    stop_event: threading.Event | None = None,
) -> _ParseOutcome:
    logger = context.logger.bind(source=target.name)

    if stop_event and stop_event.is_set():
        logger.info("parser-parse-skipped", reason="stop-signal")
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            missing_scope=target.missing_scope,
            aborted=True,
        )

    if not target.config.enabled:
        message = (
            f"Source {target.name!r} is disabled. Enable it with "
            "`raggd source enable` before parsing."
        )
        logger.warning("parser-source-disabled")
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            warnings=(),
            errors=(message,),
            missing_scope=target.missing_scope,
        )

    root = target.config.path
    if not root.exists() or not root.is_dir():
        message = (
            f"Source directory not found for {target.name!r}: {root}. "
            "Run `raggd source refresh` first."
        )
        logger.error("parser-source-missing", path=str(root))
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            warnings=(),
            errors=(message,),
            missing_scope=target.missing_scope,
        )

    parser_service = context.parser_service

    try:
        plan = parser_service.plan_source(
            source=target.name,
            scope=target.scope_paths,
        )
    except (ParserModuleDisabledError, ParserSourceNotConfiguredError) as exc:
        logger.error("parser-plan-error", error=str(exc))
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            errors=(str(exc),),
            missing_scope=target.missing_scope,
        )
    except Exception as exc:  # pragma: no cover - unexpected propagation
        logger.exception("parser-plan-unhandled", error=str(exc))
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            errors=(f"Planning failed: {exc}",),
            missing_scope=target.missing_scope,
        )

    scope_display = _scope_summary(target.scope_paths)
    logger.info(
        "parser-plan-created",
        files=len(plan.entries),
        warnings=len(plan.warnings),
        errors=len(plan.errors),
        scope=scope_display,
    )

    warnings: list[str] = []
    errors: list[str] = list(plan.errors)
    failed_files: list[str] = []

    for missing in target.missing_scope:
        warnings.append(f"Scope path missing: {missing}")

    for warning in plan.warnings:
        warnings.append(warning)

    if not plan.entries:
        logger.info("parser-plan-empty", source=target.name)
        run_metrics = plan.metrics.copy()
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            plan=plan,
            metrics=run_metrics,
            warnings=tuple(warnings),
            errors=tuple(errors),
            missing_scope=target.missing_scope,
        )

    try:
        handler_context = _build_handler_context(
            parser_service=parser_service,
            cli_context=context,
            source=target,
        )
    except TokenEncoderError as exc:
        message = (
            "Failed to load token encoder for parser handlers: "
            f"{exc}. Install the parser extras or adjust configuration."
        )
        logger.error("parser-token-encoder-error", error=str(exc))
        return _ParseOutcome(
            source=target.name,
            batch_id=None,
            batch_ref=None,
            plan=plan,
            errors=(message,),
            warnings=tuple(warnings),
            missing_scope=target.missing_scope,
        )

    registry = parser_service.registry
    handlers_cache: dict[str, Any] = {}
    results: list[tuple[ParserPlanEntry, HandlerResult]] = []
    aborted = False

    for entry in plan.entries:
        if stop_event and stop_event.is_set():
            aborted = True
            logger.info(
                "parser-handler-skipped",
                path=entry.relative_path.as_posix(),
                reason="stop-signal",
            )
            break

        handler_name = entry.handler.name
        handler_logger = handler_context.scoped_logger(handler_name)
        handler = handlers_cache.get(handler_name)
        if handler is None:
            try:
                handler = registry.create_handler(
                    handler_name,
                    context=handler_context,
                )
            except Exception as exc:
                message = (
                    f"Failed to initialize handler {handler_name!r} for "
                    f"{entry.relative_path.as_posix()}: {exc}"
                )
                handler_logger.error(
                    "parser-handler-init-error",
                    path=entry.relative_path.as_posix(),
                    error=str(exc),
                )
                errors.append(message)
                failed_files.append(entry.relative_path.as_posix())
                if fail_fast:
                    aborted = True
                    if stop_event:
                        stop_event.set()
                    break
                continue
            handlers_cache[handler_name] = handler

        try:
            result = handler.parse(
                path=entry.absolute_path,
                context=handler_context,
            )
        except Exception as exc:  # pragma: no cover - handler failure path
            message = (
                f"Handler {handler_name!r} failed for "
                f"{entry.relative_path.as_posix()}: {exc}"
            )
            handler_logger.error(
                "parser-handler-error",
                path=entry.relative_path.as_posix(),
                error=str(exc),
            )
            errors.append(message)
            failed_files.append(entry.relative_path.as_posix())
            if fail_fast:
                aborted = True
                if stop_event:
                    stop_event.set()
                break
            continue

        if result.errors:
            for message in result.errors:
                formatted = (
                    f"{entry.relative_path.as_posix()}: {message}"
                )
                errors.append(formatted)
                failed_files.append(entry.relative_path.as_posix())
            if fail_fast:
                aborted = True
                if stop_event:
                    stop_event.set()
                break
            continue

        if result.warnings:
            for warning in result.warnings:
                warnings.append(
                    f"{entry.relative_path.as_posix()}: {warning}"
                )

        results.append((entry, result))

    batch_id: str | None = None
    batch_ref: str | None = None
    staged_outcomes: tuple[tuple[ParserPlanEntry, FileStageOutcome], ...] = ()

    run_metrics = plan.metrics.copy()

    if failed_files:
        failed = len(set(failed_files))
        run_metrics.files_failed = max(run_metrics.files_failed + failed, failed)
        run_metrics.files_parsed = max(0, run_metrics.files_parsed - failed)

    if results:
        batch_uuid = generate_uuid7()
        batch_id = str(batch_uuid)
        batch_ref = short_uuid7(batch_uuid).value
        try:
            outcomes, stage_metrics = parser_service.stage_batch(
                source=target.name,
                batch_id=batch_id,
                plan=plan,
                results=tuple(results),
                batch_ref=batch_ref,
            )
        except ParserError as exc:
            message = f"Failed to stage parser batch: {exc}"
            logger.error("parser-stage-error", error=str(exc))
            errors.append(message)
            if fail_fast and stop_event:
                stop_event.set()
            staged_outcomes = ()
        else:
            staged_outcomes = tuple(outcomes)
            run_metrics = stage_metrics

    logger.info(
        "parser-parse-finished",
        batch_id=batch_id,
        warnings=len(warnings),
        errors=len(errors),
        failed_files=len(set(failed_files)),
        aborted=aborted,
    )

    return _ParseOutcome(
        source=target.name,
        batch_id=batch_id,
        batch_ref=batch_ref,
        plan=plan,
        metrics=run_metrics,
        staged=staged_outcomes,
        warnings=tuple(warnings),
        errors=tuple(errors),
        failed_files=tuple(sorted(set(failed_files))),
        missing_scope=target.missing_scope,
        aborted=aborted,
    )


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
    targets: List[str] | None = typer.Argument(
        None,
        metavar="[SOURCE|PATH]...",
        help=(
            "Optional source names followed by path filters. Defaults to "
            "all configured sources when omitted."
        ),
    ),
    fail_fast: bool | None = typer.Option(
        None,
        "--fail-fast/--no-fail-fast",
        help=(
            "Override configured fail-fast behavior for this parse run."
        ),
    ),
) -> None:
    context = _require_context(ctx)
    if not context.settings.enabled:
        typer.secho(
            "Parser module is disabled in this workspace.",
            fg=typer.colors.RED,
        )
        context.logger.warning("parser-disabled")
        raise typer.Exit(code=1)

    parse_targets, invalid = _resolve_parse_targets(context, targets)

    if invalid:
        for name in invalid:
            typer.secho(f"Unknown source: {name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if not parse_targets:
        typer.secho(
            "No sources configured. Add one with `raggd source init`.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    enabled_targets = [target for target in parse_targets if target.config.enabled]
    disabled_targets = [target for target in parse_targets if not target.config.enabled]

    for target in disabled_targets:
        typer.secho(
            f"Source {target.name} is disabled; skipping.",
            fg=typer.colors.YELLOW,
        )

    if not enabled_targets:
        raise typer.Exit(code=1)

    concurrency = _resolve_concurrency(
        context.settings.max_concurrency,
        target_count=len(enabled_targets),
    )
    resolved_fail_fast = _determine_fail_fast(
        override=fail_fast,
        settings=context.settings,
    )

    context.logger.info(
        "parser-parse-start",
        sources=[target.name for target in enabled_targets],
        concurrency=concurrency,
        fail_fast=resolved_fail_fast,
    )

    scope_key = (
        "workspace"
        if len(enabled_targets) > 1
        else enabled_targets[0].name
    )

    stop_event = threading.Event()
    outcomes: dict[str, _ParseOutcome] = {}

    with context.session_guard.acquire(scope=scope_key, action="parse"):
        if concurrency <= 1 or len(enabled_targets) == 1:
            for target in enabled_targets:
                outcome = _parse_single_source(
                    context,
                    target,
                    fail_fast=resolved_fail_fast,
                    stop_event=stop_event,
                )
                outcomes[target.name] = outcome
                if resolved_fail_fast and outcome.has_failures:
                    stop_event.set()
                    break
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="parser",
            ) as executor:
                future_map: dict[
                    concurrent.futures.Future[_ParseOutcome], _ParseTarget
                ] = {}
                for target in enabled_targets:
                    future = executor.submit(
                        _parse_single_source,
                        context,
                        target,
                        fail_fast=resolved_fail_fast,
                        stop_event=stop_event,
                    )
                    future_map[future] = target

                for future in concurrent.futures.as_completed(future_map):
                    target = future_map[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # pragma: no cover - executor path
                        context.logger.exception(
                            "parser-parse-thread-error",
                            source=target.name,
                            error=str(exc),
                        )
                        outcome = _ParseOutcome(
                            source=target.name,
                            batch_id=None,
                            batch_ref=None,
                            errors=(f"Unhandled error: {exc}",),
                        )
                    outcomes[target.name] = outcome
                    if resolved_fail_fast and outcome.has_failures:
                        stop_event.set()

    exit_code = 0

    for target in parse_targets:
        name = target.name
        outcome = outcomes.get(name)
        if outcome is None:
            continue

        for warning in outcome.warnings:
            typer.secho(f"[{name}] {warning}", fg=typer.colors.YELLOW)
        for missing in outcome.missing_scope:
            typer.secho(
                f"[{name}] Scope filter missing: {missing}",
                fg=typer.colors.YELLOW,
            )
        for failure in outcome.failed_files:
            typer.secho(
                f"[{name}] Failed to parse {failure}",
                fg=typer.colors.RED,
            )
        for error in outcome.errors:
            typer.secho(f"[{name}] {error}", fg=typer.colors.RED)

        if outcome.has_failures:
            exit_code = 1
            typer.secho(
                f"[{name}] Parse incomplete.",
                fg=typer.colors.RED,
            )
        else:
            summary = (
                f"batch {outcome.batch_ref or outcome.batch_id}"
                if outcome.batch_id
                else "no changes"
            )
            typer.secho(
                f"[{name}] Parse completed ({summary}).",
                fg=typer.colors.GREEN,
            )

    raise typer.Exit(code=exit_code)


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
