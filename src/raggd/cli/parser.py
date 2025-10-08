"""Typer command group scaffolding for the parser module."""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Sequence
import uuid

import typer

from raggd.core.config import (
    AppConfig,
    ParserModuleSettings,
    ParserHandlerSettings,
    PARSER_MODULE_KEY,
)
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.manifest import (
    ManifestService,
    ManifestSnapshot,
    manifest_settings_from_config,
)
from raggd.modules.manifest.migrator import MODULES_VERSION
from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)
from raggd.modules import HealthStatus
from raggd.modules.parser import (
    FileStageOutcome,
    HandlerResult,
    ParseContext,
    ParserBatchPlan,
    ParserManifestState,
    ParserModuleDisabledError,
    ParserPlanEntry,
    ParserRunMetrics,
    ParserRunRecord,
    ParserService,
    ParserSourceNotConfiguredError,
    TokenEncoderError,
)
from raggd.modules.parser.service import ParserError
from raggd.source.config import SourceConfigError, SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


class ParserSessionError(RuntimeError):
    """Base error raised when acquiring a parser session fails."""


class ParserSessionTimeoutError(ParserSessionError):
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
            raise ParserSessionTimeoutError(str(exc)) from exc
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
    summary: str | None = None
    notes: tuple[str, ...] = ()
    manifest_state: ParserManifestState | None = None
    run_record: ParserRunRecord | None = None

    @property
    def has_failures(self) -> bool:
        return bool(self.errors or self.failed_files or self.aborted)


@dataclass(slots=True)
class _BatchSummary:
    """Lightweight summary of a persisted parser batch."""

    batch_id: str
    ref: str | None
    generated_at: datetime | None
    notes: str | None
    file_count: int
    symbol_count: int
    chunk_count: int


@dataclass(slots=True)
class _PlanProcessingState:
    """Mutable aggregation while executing a parser plan."""

    run_metrics: ParserRunMetrics
    cli_warnings: list[str] = field(default_factory=list)
    run_warnings: list[str] = field(default_factory=list)
    cli_errors: list[str] = field(default_factory=list)
    run_errors: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    results: list[tuple[ParserPlanEntry, HandlerResult]] = field(
        default_factory=list
    )
    aborted: bool = False


@dataclass(slots=True)
class _BatchRemovalStats:
    """Collected counters from batch removal operations."""

    chunks_reassigned: int = 0
    chunks_deleted: int = 0
    chunk_first_reassigned: int = 0
    chunk_last_seen_reset: int = 0
    symbols_reassigned: int = 0
    symbol_last_seen_reset: int = 0
    symbols_deleted: int = 0
    files_reassigned: int = 0
    files_deleted: int = 0


@dataclass(slots=True)
class _BatchRow:
    """Metadata snapshot captured for a batch before removal."""

    batch_id: str
    ref: str | None
    generated_at: datetime | None
    notes: str | None


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


@dataclass(slots=True)
class _PlanExecutor:
    """Drive parser plan execution while capturing diagnostics."""

    plan: ParserBatchPlan
    target: _ParseTarget
    parser_service: ParserService
    cli_context: ParserCLIContext
    logger: Logger
    fail_fast: bool
    stop_event: threading.Event | None = None
    handlers: dict[str, Any] = field(default_factory=dict)

    def run(self) -> _PlanProcessingState:
        state = _PlanProcessingState(
            run_metrics=self.plan.metrics.copy(),
            cli_warnings=list(self.plan.warnings),
            run_warnings=[],
            cli_errors=list(self.plan.errors),
            run_errors=[],
        )
        self._add_missing_scope_messages(state)
        if not self.plan.entries:
            self.logger.info("parser-plan-empty", source=self.target.name)
            self._adjust_metrics_for_failures(state)
            return state

        handler_context = self._create_handler_context(state)
        if handler_context is None:
            self._adjust_metrics_for_failures(state)
            return state

        for entry in self.plan.entries:
            if self._should_skip_entry(entry, state):
                break
            handler = self._resolve_handler(entry, handler_context, state)
            if handler is None:
                continue
            result = self._run_handler(entry, handler_context, handler, state)
            if result is None:
                continue
            self._record_result(entry, result, state)

        self._adjust_metrics_for_failures(state)
        return state

    def _add_missing_scope_messages(self, state: _PlanProcessingState) -> None:
        for missing in self.target.missing_scope:
            message = f"Scope path missing: {missing}"
            state.cli_warnings.append(message)
            state.run_warnings.append(message)

    def _create_handler_context(
        self, state: _PlanProcessingState
    ) -> ParseContext | None:
        try:
            return _build_handler_context(
                parser_service=self.parser_service,
                cli_context=self.cli_context,
                source=self.target,
            )
        except TokenEncoderError as exc:
            message = (
                "Failed to load token encoder for parser handlers: "
                f"{exc}. Install the parser extras or adjust configuration."
            )
            self.logger.error("parser-token-encoder-error", error=str(exc))
            state.cli_errors.append(message)
            state.run_errors.append(message)
            return None

    def _should_skip_entry(
        self, entry: ParserPlanEntry, state: _PlanProcessingState
    ) -> bool:
        if self.stop_event and self.stop_event.is_set():
            state.aborted = True
            self.logger.info(
                "parser-handler-skipped",
                path=entry.relative_path.as_posix(),
                reason="stop-signal",
            )
            return True
        return False

    def _resolve_handler(
        self,
        entry: ParserPlanEntry,
        handler_context: ParseContext,
        state: _PlanProcessingState,
    ) -> Any | None:
        handler_name = entry.handler.name
        handler = self.handlers.get(handler_name)
        if handler is not None:
            return handler

        handler_logger = handler_context.scoped_logger(handler_name)
        try:
            handler = self.parser_service.registry.create_handler(
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
            self._record_failure(
                state,
                message,
                failed_path=entry.relative_path.as_posix(),
                fatal=self.fail_fast,
            )
            return None

        self.handlers[handler_name] = handler
        return handler

    def _run_handler(
        self,
        entry: ParserPlanEntry,
        handler_context: ParseContext,
        handler: Any,
        state: _PlanProcessingState,
    ) -> HandlerResult | None:
        try:
            return handler.parse(
                path=entry.absolute_path,
                context=handler_context,
            )
        except Exception as exc:  # pragma: no cover - handler failure path
            message = (
                f"Handler {entry.handler.name!r} failed for "
                f"{entry.relative_path.as_posix()}: {exc}"
            )
            handler_logger = handler_context.scoped_logger(entry.handler.name)
            handler_logger.error(
                "parser-handler-error",
                path=entry.relative_path.as_posix(),
                error=str(exc),
            )
            self._record_failure(
                state,
                message,
                failed_path=entry.relative_path.as_posix(),
                fatal=self.fail_fast,
            )
            return None

    def _record_result(
        self,
        entry: ParserPlanEntry,
        result: HandlerResult,
        state: _PlanProcessingState,
    ) -> None:
        if result.errors:
            path = entry.relative_path.as_posix()
            for message in result.errors:
                formatted = f"{path}: {message}"
                self._record_failure(
                    state,
                    formatted,
                    failed_path=path,
                    fatal=self.fail_fast,
                )
            return

        if result.warnings:
            path = entry.relative_path.as_posix()
            for warning in result.warnings:
                formatted = f"{path}: {warning}"
                state.cli_warnings.append(formatted)
                state.run_warnings.append(formatted)

        state.results.append((entry, result))

    def _record_failure(
        self,
        state: _PlanProcessingState,
        message: str,
        *,
        failed_path: str | None = None,
        fatal: bool = False,
    ) -> None:
        state.cli_errors.append(message)
        state.run_errors.append(message)
        if failed_path:
            state.failed_files.append(failed_path)
        if fatal:
            state.aborted = True
            if self.stop_event:
                self.stop_event.set()

    def _adjust_metrics_for_failures(self, state: _PlanProcessingState) -> None:
        if not state.failed_files:
            return
        failed = len(set(state.failed_files))
        state.run_metrics.files_failed = max(
            state.run_metrics.files_failed + failed,
            failed,
        )
        state.run_metrics.files_parsed = max(
            0,
            state.run_metrics.files_parsed - failed,
        )


def _unique_messages(messages: Iterable[str]) -> tuple[str, ...]:
    """Return messages deduplicated while preserving order."""

    normalized = []
    seen: dict[str, None] = {}
    for message in messages:
        if message is None:
            continue
        text = str(message)
        if not text:
            continue
        if text in seen:
            continue
        seen[text] = None
        normalized.append(text)
    return tuple(normalized)


def _format_run_summary(
    *,
    plan: ParserBatchPlan | None,
    metrics: ParserRunMetrics | None,
    aborted: bool,
    has_failures: bool,
) -> str | None:
    """Create a human-readable summary for manifest and CLI output."""

    if metrics is None:
        return None

    no_work_planned = plan is not None and not plan.entries
    if _is_no_work_run(metrics, aborted, has_failures, no_work_planned):
        return "no changes"

    status = _select_summary_status(
        metrics,
        aborted=aborted,
        has_failures=has_failures,
        no_work_planned=no_work_planned,
    )

    file_segment = _summarize_file_metrics(metrics)
    chunk_segment = _summarize_chunk_metrics(metrics)
    return f"{status}: {file_segment}; {chunk_segment}"


def _is_no_work_run(
    metrics: ParserRunMetrics,
    aborted: bool,
    has_failures: bool,
    no_work_planned: bool,
) -> bool:
    if aborted or has_failures or not no_work_planned:
        return False
    if metrics.chunks_emitted:
        return False
    if metrics.files_parsed:
        return False
    return metrics.files_failed == 0


def _select_summary_status(
    metrics: ParserRunMetrics,
    *,
    aborted: bool,
    has_failures: bool,
    no_work_planned: bool,
) -> str:
    if aborted:
        return "aborted"
    if has_failures or metrics.files_failed > 0:
        return "completed with failures"
    if no_work_planned:
        return "no changes"
    return "completed"


def _summarize_file_metrics(metrics: ParserRunMetrics) -> str:
    details: list[str] = []
    if metrics.files_reused:
        details.append(f"reused={metrics.files_reused}")
    if metrics.files_failed:
        details.append(f"failed={metrics.files_failed}")
    if (
        metrics.files_discovered
        and metrics.files_discovered != metrics.files_parsed
    ):
        details.append(f"discovered={metrics.files_discovered}")

    segment = f"files parsed={metrics.files_parsed}"
    if details:
        segment += f" ({', '.join(details)})"
    return segment


def _summarize_chunk_metrics(metrics: ParserRunMetrics) -> str:
    details: list[str] = []
    if metrics.chunks_reused:
        details.append(f"reused={metrics.chunks_reused}")
    if metrics.fallbacks:
        details.append(f"fallbacks={metrics.fallbacks}")

    segment = f"chunks inserted={metrics.chunks_emitted}"
    if details:
        segment += f", {', '.join(details)}"
    return segment


def _vector_sync_note(source: str) -> str:
    """Return the vector sync reminder for manifest and CLI output."""

    return (
        "Vector indexes are not updated automatically; run "
        f"`raggd vdb sync {source}` to refresh embeddings."
    )


def _partition_targets(
    parse_targets: Sequence[_ParseTarget],
) -> tuple[list[_ParseTarget], list[_ParseTarget]]:
    enabled: list[_ParseTarget] = []
    disabled: list[_ParseTarget] = []
    for target in parse_targets:
        if target.config.enabled:
            enabled.append(target)
        else:
            disabled.append(target)
    return enabled, disabled


def _notify_disabled_targets(targets: Sequence[_ParseTarget]) -> None:
    for target in targets:
        typer.secho(
            f"Source {target.name} is disabled; skipping.",
            fg=typer.colors.YELLOW,
        )


def _run_parse_targets(
    *,
    context: ParserCLIContext,
    enabled_targets: Sequence[_ParseTarget],
    concurrency: int,
    fail_fast: bool,
) -> dict[str, _ParseOutcome]:
    if not enabled_targets:
        return {}

    scope_key = (
        "workspace" if len(enabled_targets) > 1 else enabled_targets[0].name
    )
    stop_event = threading.Event()
    with context.session_guard.acquire(scope=scope_key, action="parse"):
        if concurrency <= 1 or len(enabled_targets) == 1:
            return _run_targets_sequential(
                context=context,
                targets=enabled_targets,
                fail_fast=fail_fast,
                stop_event=stop_event,
            )
        return _run_targets_concurrent(
            context=context,
            targets=enabled_targets,
            concurrency=concurrency,
            fail_fast=fail_fast,
            stop_event=stop_event,
        )


def _run_targets_sequential(
    *,
    context: ParserCLIContext,
    targets: Sequence[_ParseTarget],
    fail_fast: bool,
    stop_event: threading.Event,
) -> dict[str, _ParseOutcome]:
    outcomes: dict[str, _ParseOutcome] = {}
    for target in targets:
        outcome = _parse_single_source(
            context,
            target,
            fail_fast=fail_fast,
            stop_event=stop_event,
        )
        outcomes[target.name] = outcome
        if fail_fast and outcome.has_failures:
            stop_event.set()
            break
    return outcomes


def _run_targets_concurrent(
    *,
    context: ParserCLIContext,
    targets: Sequence[_ParseTarget],
    concurrency: int,
    fail_fast: bool,
    stop_event: threading.Event,
) -> dict[str, _ParseOutcome]:
    outcomes: dict[str, _ParseOutcome] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="parser",
    ) as executor:
        future_map: dict[
            concurrent.futures.Future[_ParseOutcome], _ParseTarget
        ] = {}
        for target in targets:
            future = executor.submit(
                _parse_single_source,
                context,
                target,
                fail_fast=fail_fast,
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
            if fail_fast and outcome.has_failures:
                stop_event.set()
    return outcomes


def _render_parse_results(
    parse_targets: Sequence[_ParseTarget],
    outcomes: dict[str, _ParseOutcome],
) -> int:
    exit_code = 0
    for target in parse_targets:
        name = target.name
        outcome = outcomes.get(name)
        if outcome is None:
            continue

        _emit_outcome_messages(name, outcome)
        failed = _emit_outcome_summary(name, outcome)
        _emit_outcome_notes(name, outcome)

        if failed:
            exit_code = 1

    return exit_code


def _emit_outcome_messages(name: str, outcome: _ParseOutcome) -> None:
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


def _emit_outcome_summary(name: str, outcome: _ParseOutcome) -> bool:
    summary_text = outcome.summary
    show_summary_line = bool(summary_text)

    if outcome.has_failures:
        typer.secho(f"[{name}] Parse incomplete.", fg=typer.colors.RED)
        if show_summary_line:
            typer.secho(
                f"[{name}] Summary: {summary_text}",
                fg=typer.colors.YELLOW,
            )
        return True

    if outcome.batch_id:
        batch_ref = outcome.batch_ref or outcome.batch_id
        summary = f"batch {batch_ref}"
    else:
        summary = "no changes"
    typer.secho(
        f"[{name}] Parse completed ({summary}).",
        fg=typer.colors.GREEN,
    )
    if show_summary_line:
        typer.secho(
            f"[{name}] Summary: {summary_text}",
            fg=typer.colors.BLUE,
        )
    return False


def _emit_outcome_notes(name: str, outcome: _ParseOutcome) -> None:
    for note in outcome.notes:
        typer.secho(f"[{name}] {note}", fg=typer.colors.YELLOW)


def _ensure_target_ready(
    target: _ParseTarget,
    logger: Logger,
) -> _ParseOutcome | None:
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
            errors=(message,),
            missing_scope=target.missing_scope,
        )

    return None


def _plan_source_or_outcome(
    context: ParserCLIContext,
    target: _ParseTarget,
    logger: Logger,
) -> ParserBatchPlan | _ParseOutcome:
    parser_service = context.parser_service
    try:
        return parser_service.plan_source(
            source=target.name,
            scope=target.scope_paths,
        )
    except (
        ParserModuleDisabledError,
        ParserSourceNotConfiguredError,
    ) as exc:
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


def _stage_results(
    *,
    parser_service: ParserService,
    target: _ParseTarget,
    plan: ParserBatchPlan,
    state: _PlanProcessingState,
    fail_fast: bool,
    stop_event: threading.Event | None,
    logger: Logger,
) -> tuple[
    str | None,
    str | None,
    tuple[tuple[ParserPlanEntry, FileStageOutcome], ...],
]:
    if not state.results:
        return None, None, ()

    batch_uuid = generate_uuid7()
    batch_id = str(batch_uuid)
    batch_ref = short_uuid7(batch_uuid).value

    try:
        outcomes, stage_metrics = parser_service.stage_batch(
            source=target.name,
            batch_id=batch_id,
            plan=plan,
            results=tuple(state.results),
            batch_ref=batch_ref,
        )
    except ParserError as exc:
        message = f"Failed to stage parser batch: {exc}"
        logger.error("parser-stage-error", error=str(exc))
        state.cli_errors.append(message)
        state.run_errors.append(message)
        if fail_fast and stop_event:
            stop_event.set()
        return None, None, ()

    state.run_metrics = stage_metrics
    return batch_id, batch_ref, tuple(outcomes)


def _build_vector_notes(
    *,
    batch_id: str | None,
    aborted: bool,
    metrics: ParserRunMetrics,
    source: str,
) -> list[str]:
    if not batch_id or aborted:
        return []
    if metrics.chunks_emitted > 0 or metrics.chunks_reused > 0:
        return [_vector_sync_note(source)]
    return []


def _record_manifest_update(
    *,
    parser_service: ParserService,
    target: _ParseTarget,
    plan: ParserBatchPlan,
    batch_id: str | None,
    summary: str | None,
    state: _PlanProcessingState,
    notes: Sequence[str],
    logger: Logger,
    fail_fast: bool,
    stop_event: threading.Event | None,
) -> tuple[ParserRunRecord | None, ParserManifestState | None]:
    try:
        run_record = parser_service.build_run_record(
            plan=plan,
            batch_id=batch_id,
            summary=summary,
            warnings=tuple(state.run_warnings),
            errors=tuple(state.run_errors),
            notes=tuple(notes),
            metrics=state.run_metrics,
        )
        manifest_state = parser_service.record_run(
            source=target.name,
            run=run_record,
        )
        return run_record, manifest_state
    except Exception as exc:  # pragma: no cover - manifest failure path
        message = f"Failed to update parser manifest: {exc}"
        logger.error("parser-manifest-error", error=str(exc))
        state.cli_errors.append(message)
        state.run_errors.append(message)
        if fail_fast and stop_event:
            stop_event.set()
        return None, None


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

    ready = _ensure_target_ready(target, logger)
    if ready is not None:
        return ready

    planned = _plan_source_or_outcome(context, target, logger)
    if isinstance(planned, _ParseOutcome):
        return planned

    plan = planned
    scope_display = _scope_summary(target.scope_paths)
    logger.info(
        "parser-plan-created",
        files=len(plan.entries),
        warnings=len(plan.warnings),
        errors=len(plan.errors),
        scope=scope_display,
    )

    executor = _PlanExecutor(
        plan=plan,
        target=target,
        parser_service=context.parser_service,
        cli_context=context,
        logger=logger,
        fail_fast=fail_fast,
        stop_event=stop_event,
    )
    state = executor.run()

    batch_id, batch_ref, staged_outcomes = _stage_results(
        parser_service=context.parser_service,
        target=target,
        plan=plan,
        state=state,
        fail_fast=fail_fast,
        stop_event=stop_event,
        logger=logger,
    )

    notes = _build_vector_notes(
        batch_id=batch_id,
        aborted=state.aborted,
        metrics=state.run_metrics,
        source=target.name,
    )

    manifest_failures = bool(
        plan.errors or state.run_errors or state.failed_files or state.aborted
    )
    manifest_summary = _format_run_summary(
        plan=plan,
        metrics=state.run_metrics,
        aborted=state.aborted,
        has_failures=manifest_failures,
    )

    run_record, manifest_state = _record_manifest_update(
        parser_service=context.parser_service,
        target=target,
        plan=plan,
        batch_id=batch_id,
        summary=manifest_summary,
        state=state,
        notes=notes,
        logger=logger,
        fail_fast=fail_fast,
        stop_event=stop_event,
    )

    has_failures = bool(
        plan.errors or state.run_errors or state.failed_files or state.aborted
    )
    summary_text = _format_run_summary(
        plan=plan,
        metrics=state.run_metrics,
        aborted=state.aborted,
        has_failures=has_failures,
    )

    warnings_out = _unique_messages(state.cli_warnings)
    errors_out = _unique_messages(state.cli_errors)
    notes_out = _unique_messages(notes)
    failed_files_out = tuple(sorted(set(state.failed_files)))

    logger.info(
        "parser-parse-finished",
        batch_id=batch_id,
        warnings=len(warnings_out),
        errors=len(errors_out),
        failed_files=len(failed_files_out),
        aborted=state.aborted,
        summary=summary_text,
    )

    return _ParseOutcome(
        source=target.name,
        batch_id=batch_id,
        batch_ref=batch_ref,
        plan=plan,
        metrics=state.run_metrics,
        staged=staged_outcomes,
        warnings=warnings_out,
        errors=errors_out,
        failed_files=failed_files_out,
        missing_scope=target.missing_scope,
        aborted=state.aborted,
        summary=summary_text,
        notes=notes_out,
        manifest_state=manifest_state,
        run_record=run_record,
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


def _format_setting_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "inherit"
    if isinstance(value, tuple):
        if not value:
            return "none"
        return ", ".join(str(item) for item in value)
    if isinstance(value, HealthStatus):
        return value.value
    if hasattr(value, "value"):
        try:
            return str(value.value)
        except Exception:  # pragma: no cover - defensive fallback
            pass
    return str(value)


def _coerce_health_status(value: object) -> HealthStatus:
    if isinstance(value, HealthStatus):
        return value
    try:
        return HealthStatus(str(value))
    except Exception:  # pragma: no cover - defensive fallback
        return HealthStatus.UNKNOWN


def _compute_config_overrides(
    settings: ParserModuleSettings,
) -> tuple[str, ...]:
    baseline = ParserModuleSettings()
    overrides: list[str] = []

    def _maybe_add(label: str, actual: object, default: object) -> None:
        if actual == default:
            return
        overrides.append(
            f"{label}: {_format_setting_value(actual)} "
            f"(default {_format_setting_value(default)})"
        )

    _maybe_add("enabled", settings.enabled, baseline.enabled)
    _maybe_add("extras", settings.extras, baseline.extras)
    _maybe_add(
        "fail_fast",
        settings.fail_fast,
        baseline.fail_fast,
    )
    _maybe_add(
        "max_concurrency",
        settings.max_concurrency,
        baseline.max_concurrency,
    )
    _maybe_add(
        "general_max_tokens",
        settings.general_max_tokens,
        baseline.general_max_tokens,
    )
    _maybe_add(
        "gitignore_behavior",
        settings.gitignore_behavior,
        baseline.gitignore_behavior,
    )

    handler_names = set(baseline.handlers) | set(settings.handlers)
    for name in sorted(handler_names):
        actual = settings.handlers.get(name)
        default = baseline.handlers.get(name, ParserHandlerSettings())
        if actual is None:
            actual = ParserHandlerSettings()
        _maybe_add(
            f"handlers.{name}.enabled",
            actual.enabled,
            default.enabled,
        )
        _maybe_add(
            f"handlers.{name}.max_tokens",
            actual.max_tokens,
            default.max_tokens,
        )

    return tuple(overrides)


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _shorten_batch_id(batch_id: str | None) -> str:
    if not batch_id:
        return "none"
    try:
        shortened = short_uuid7(uuid.UUID(batch_id))
    except (ValueError, AttributeError):
        return batch_id
    return shortened.value


def _render_handler_coverage(
    state: ParserManifestState,
) -> tuple[str, ...]:
    coverage: list[str] = []
    names = set(state.handler_versions) | set(state.metrics.handlers_invoked)
    for name in sorted(names):
        count = state.metrics.handlers_invoked.get(name, 0)
        version = state.handler_versions.get(name)
        details = f"{name}: count={count}"
        if version:
            details += f", version={version}"
        coverage.append(details)
    return tuple(coverage)


def _render_dependency_gaps(
    availability: Sequence[Any],
) -> tuple[tuple[str, tuple[str, ...]]]:
    gaps: list[tuple[str, tuple[str, ...]]] = []
    for snapshot in availability:
        name = getattr(snapshot, "name", "handler")
        enabled = getattr(snapshot, "enabled", True)
        status = _coerce_health_status(
            getattr(snapshot, "status", HealthStatus.UNKNOWN)
        )
        summary = getattr(snapshot, "summary", None)
        warnings = tuple(getattr(snapshot, "warnings", ()) or ())
        reasons: list[str] = []
        if not enabled:
            reasons.append("disabled by configuration")
        if enabled and status != HealthStatus.OK:
            reasons.append(summary or f"status={status.value}")
        if warnings:
            reasons.extend(warnings)
        if reasons:
            gaps.append((name, tuple(reasons)))
    return tuple(gaps)


def _render_handler_availability(
    availability: Sequence[Any],
) -> tuple[str, ...]:
    lines: list[str] = []
    for snapshot in sorted(
        availability,
        key=lambda item: getattr(item, "name", ""),
    ):
        name = getattr(snapshot, "name", "handler")
        status = _coerce_health_status(
            getattr(snapshot, "status", HealthStatus.UNKNOWN)
        )
        enabled = getattr(snapshot, "enabled", True)
        summary = getattr(snapshot, "summary", None)
        label = "enabled" if enabled else "disabled"
        if enabled:
            line = f"{name}: {label} (status={status.value})"
        else:
            line = f"{name}: {label}"
        if summary:
            line += f" - {summary}"
        lines.append(line)
    return tuple(lines)


def _resolve_info_sources(
    context: ParserCLIContext,
    source: str | None,
) -> tuple[list[str], list[str]]:
    sources = [name for name, _ in _sorted_workspace_sources(context.config)]
    if source is None:
        return sources, []
    normalized = source.strip()
    if normalized in context.config.workspace_sources:
        return [normalized], []
    return [], [normalized]


_BATCH_SUMMARY_QUERY = """
    SELECT
        b.id AS batch_id,
        b.ref AS ref,
        b.generated_at AS generated_at,
        b.notes AS notes,
        COALESCE(f.file_count, 0) AS file_count,
        COALESCE(s.symbol_count, 0) AS symbol_count,
        COALESCE(c.chunk_count, 0) AS chunk_count
    FROM batches AS b
    LEFT JOIN (
        SELECT batch_id, COUNT(*) AS file_count
        FROM files
        GROUP BY batch_id
    ) AS f ON f.batch_id = b.id
    LEFT JOIN (
        SELECT last_seen_batch AS batch_id, COUNT(*) AS symbol_count
        FROM symbols
        GROUP BY last_seen_batch
    ) AS s ON s.batch_id = b.id
    LEFT JOIN (
        SELECT last_seen_batch AS batch_id, COUNT(*) AS chunk_count
        FROM chunk_slices
        GROUP BY last_seen_batch
    ) AS c ON c.batch_id = b.id
    ORDER BY b.generated_at DESC, b.id DESC
    LIMIT ?
"""


def _parse_generated_at(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _format_generated_at(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return _format_timestamp(value)


def _load_batch_summaries(
    context: ParserCLIContext,
    *,
    source: str,
    limit: int,
) -> tuple[tuple[_BatchSummary, ...], str | None]:
    db_path = context.paths.source_database_path(source)
    if not db_path.exists():
        context.logger.debug("parser-batches-db-missing", source=source)
        return (), None

    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        context.logger.error(
            "parser-batches-open-error",
            source=source,
            error=str(exc),
        )
        return (), f"Failed to open database for {source}: {exc}"

    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(_BATCH_SUMMARY_QUERY, (limit,))
        rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        context.logger.error(
            "parser-batches-query-error",
            source=source,
            error=str(exc),
        )
        connection.close()
        return (), f"Failed to query batches for {source}: {exc}"

    connection.close()

    summaries = [
        _BatchSummary(
            batch_id=str(row["batch_id"]),
            ref=(row["ref"] if row["ref"] is not None else None),
            generated_at=_parse_generated_at(row["generated_at"]),
            notes=(row["notes"] if row["notes"] else None),
            file_count=int(row["file_count"] or 0),
            symbol_count=int(row["symbol_count"] or 0),
            chunk_count=int(row["chunk_count"] or 0),
        )
        for row in rows
    ]
    return tuple(summaries), None


def _determine_batch_status(
    summary: _BatchSummary,
    state: ParserManifestState,
) -> HealthStatus:
    if summary.batch_id == (state.last_batch_id or ""):
        return _coerce_health_status(state.last_run_status)
    return HealthStatus.UNKNOWN


def _display_batches_for_source(
    *,
    name: str,
    summaries: Sequence[_BatchSummary],
    limit: int,
    state: ParserManifestState,
) -> None:
    typer.secho(
        f"Parser batches for {name} (showing up to {limit})",
        fg=typer.colors.CYAN,
        bold=True,
    )

    if not summaries:
        typer.echo("  No batches recorded.")
        typer.echo("")
        return

    for summary in summaries:
        status = _determine_batch_status(summary, state)
        status_color = {
            HealthStatus.OK: typer.colors.GREEN,
            HealthStatus.DEGRADED: typer.colors.YELLOW,
            HealthStatus.ERROR: typer.colors.RED,
            HealthStatus.UNKNOWN: typer.colors.BLUE,
        }.get(status, typer.colors.WHITE)

        timestamp = _format_generated_at(summary.generated_at)
        batch_ref = summary.ref or _shorten_batch_id(summary.batch_id)
        latest_suffix = (
            " · latest" if summary.batch_id == (state.last_batch_id or "") else ""
        )
        typer.secho(
            (
                f"  - {timestamp} · batch {_shorten_batch_id(summary.batch_id)} "
                f"(ref={batch_ref}) · status={status.value}{latest_suffix}"
            ),
            fg=status_color,
        )
        typer.echo(
            "    files: {files}  symbols: {symbols}  chunks: {chunks}".format(
                files=summary.file_count,
                symbols=summary.symbol_count,
                chunks=summary.chunk_count,
            )
        )
        if summary.notes:
            typer.echo(f"    notes: {summary.notes}")

    typer.echo("")


def _emit_basic_info(
    *,
    state: ParserManifestState,
    settings_enabled: bool,
) -> None:
    typer.echo(f"  Module enabled: {'yes' if settings_enabled else 'no'}")
    typer.echo(f"  Last batch id: {_shorten_batch_id(state.last_batch_id)}")
    status = _coerce_health_status(state.last_run_status)
    status_color = {
        HealthStatus.OK: typer.colors.GREEN,
        HealthStatus.DEGRADED: typer.colors.YELLOW,
        HealthStatus.ERROR: typer.colors.RED,
        HealthStatus.UNKNOWN: typer.colors.BLUE,
    }.get(status, typer.colors.WHITE)
    typer.secho(
        f"  Last run status: {status.value}",
        fg=status_color,
    )
    typer.echo(
        f"  Last run started: {_format_timestamp(state.last_run_started_at)}"
    )
    typer.echo(
        "  Last run completed: "
        f"{_format_timestamp(state.last_run_completed_at)}"
    )
    typer.echo(f"  Last run summary: {state.last_run_summary or 'none'}")


def _emit_run_messages(state: ParserManifestState) -> None:
    if state.warning_count:
        typer.echo("  Last run warnings:")
        for warning in state.last_run_warnings:
            typer.secho(f"    - {warning}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Last run warnings: none")

    if state.error_count:
        typer.echo("  Last run errors:")
        for error in state.last_run_errors:
            typer.secho(f"    - {error}", fg=typer.colors.RED)
    else:
        typer.echo("  Last run errors: none")


def _emit_metrics_summary(state: ParserManifestState) -> None:
    metrics = state.metrics
    typer.echo(
        (
            "  Last run metrics: parsed={parsed} reused={reused} "
            "chunks={chunks} reused_chunks={chunks_reused}"
        ).format(
            parsed=metrics.files_parsed,
            reused=metrics.files_reused,
            chunks=metrics.chunks_emitted,
            chunks_reused=metrics.chunks_reused,
        )
    )


def _emit_handler_sections(
    *,
    state: ParserManifestState,
    availability: tuple[Any, ...],
) -> None:
    coverage = _render_handler_coverage(state)
    if coverage:
        typer.echo("  Handler coverage:")
        for line in coverage:
            typer.echo(f"    - {line}")
    else:
        typer.echo("  Handler coverage: none recorded")

    availability_lines = _render_handler_availability(availability)
    if availability_lines:
        typer.echo("  Handler availability:")
        for line in availability_lines:
            typer.echo(f"    - {line}")
    else:
        typer.echo("  Handler availability: none")


def _emit_dependency_section(availability: tuple[Any, ...]) -> None:
    gaps = _render_dependency_gaps(availability)
    if gaps:
        typer.echo("  Dependency gaps:")
        for name, reasons in gaps:
            typer.echo(f"    - {name}:")
            for reason in reasons:
                typer.secho(f"      * {reason}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Dependency gaps: none")


def _emit_overrides_summary(overrides: tuple[str, ...]) -> None:
    if overrides:
        typer.echo("  Configuration overrides:")
        for entry in overrides:
            typer.echo(f"    - {entry}")
    else:
        typer.echo("  Configuration overrides: none")


class ParserRemoveError(RuntimeError):
    """Raised when parser batch removal cannot proceed."""


class ParserRemoveBlocked(ParserRemoveError):
    """Raised when removal is blocked by dependency checks."""


def _handle_remove_command(
    *,
    context: ParserCLIContext,
    source: str,
    batch_id: str,
    manifest_state: ParserManifestState,
    force: bool,
) -> int:
    db_path = context.paths.source_database_path(source)
    if not db_path.exists():
        typer.secho(
            f"Parser database not found for {source}; nothing to remove.",
            fg=typer.colors.RED,
        )
        context.logger.error(
            "parser-remove-missing-db",
            source=source,
            batch=batch_id,
        )
        return 1

    try:
        stats, removed_row, next_row, remaining = _perform_batch_removal(
            context=context,
            source=source,
            batch_id=batch_id,
        )
    except ParserRemoveBlocked as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        context.logger.warning(
            "parser-remove-blocked",
            source=source,
            batch=batch_id,
            reason=str(exc),
        )
        return 1
    except ParserRemoveError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        context.logger.error(
            "parser-remove-error",
            source=source,
            batch=batch_id,
            error=str(exc),
        )
        return 1

    manifest_updated = _sync_manifest_after_removal(
        context=context,
        source=source,
        manifest_state=manifest_state,
        removed_batch=batch_id,
        next_batch=next_row,
    )

    short_removed = _shorten_batch_id(removed_row.batch_id)
    typer.secho(
        f"Removed parser batch {short_removed} from {source}.",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        "  removed ref: %s  generated_at: %s"
        % (
            removed_row.ref or "none",
            _format_generated_at(removed_row.generated_at),
        )
    )
    typer.echo(
        "  chunks: reassigned=%d  deleted=%d  first_seen_resets=%d  last_seen_resets=%d"
        % (
            stats.chunks_reassigned,
            stats.chunks_deleted,
            stats.chunk_first_reassigned,
            stats.chunk_last_seen_reset,
        )
    )
    typer.echo(
        "  symbols: first_seen_resets=%d  last_seen_resets=%d  deleted=%d"
        % (
            stats.symbols_reassigned,
            stats.symbol_last_seen_reset,
            stats.symbols_deleted,
        )
    )
    typer.echo(
        "  files: reassigned=%d  deleted=%d"
        % (stats.files_reassigned, stats.files_deleted)
    )
    typer.echo(f"  remaining batches: {remaining}")

    typer.secho(_vector_sync_note(source), fg=typer.colors.YELLOW)

    if manifest_updated:
        typer.secho(
            (
                "Parser manifest reset; run `raggd parser parse %s` to "
                "re-establish baseline data." % source
            ),
            fg=typer.colors.YELLOW,
        )

    context.logger.info(
        "parser-remove",
        source=source,
        batch=batch_id,
        short_batch=short_removed,
        force=force,
        chunks_reassigned=stats.chunks_reassigned,
        chunks_deleted=stats.chunks_deleted,
        chunk_first_seen_resets=stats.chunk_first_reassigned,
        chunk_last_seen_resets=stats.chunk_last_seen_reset,
        symbols_reassigned=stats.symbols_reassigned,
        symbol_last_seen_resets=stats.symbol_last_seen_reset,
        symbols_deleted=stats.symbols_deleted,
        files_reassigned=stats.files_reassigned,
        files_deleted=stats.files_deleted,
        remaining_batches=remaining,
        manifest_updated=manifest_updated,
    )
    return 0


def _perform_batch_removal(
    *,
    context: ParserCLIContext,
    source: str,
    batch_id: str,
) -> tuple[_BatchRemovalStats, _BatchRow, _BatchRow | None, int]:
    db_path = context.paths.source_database_path(source)
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:
        raise ParserRemoveError(
            f"Failed to open database for {source}: {exc}"
        ) from exc

    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        batch_cursor = connection.execute(
            (
                "SELECT id, ref, generated_at, notes "
                "FROM batches WHERE id = :batch"
            ),
            {"batch": batch_id},
        )
        batch_row = batch_cursor.fetchone()
        if batch_row is None:
            raise ParserRemoveError(
                f"Batch {batch_id} not found for source {source}."
            )

        vector_count = connection.execute(
            "SELECT COUNT(*) FROM vdbs WHERE batch_id = :batch",
            {"batch": batch_id},
        ).fetchone()[0]
        if vector_count:
            raise ParserRemoveBlocked(
                (
                    f"Batch {batch_id} is referenced by {vector_count} vector "
                    "index(es); run `raggd vdb reset` or detach them before "
                    "removing the parser batch."
                )
            )

        stats = _BatchRemovalStats()

        with connection:
            reassigned = connection.execute(
                (
                    "UPDATE chunk_slices\n"
                    "   SET batch_id = last_seen_batch,\n"
                    "       first_seen_batch = CASE\n"
                    "           WHEN first_seen_batch = :batch\n"
                    "           THEN last_seen_batch\n"
                    "           ELSE first_seen_batch\n"
                    "       END\n"
                    " WHERE batch_id = :batch\n"
                    "   AND last_seen_batch <> :batch"
                ),
                {"batch": batch_id},
            )
            stats.chunks_reassigned = max(reassigned.rowcount or 0, 0)

            deleted_chunks = connection.execute(
                "DELETE FROM chunk_slices WHERE batch_id = :batch",
                {"batch": batch_id},
            )
            stats.chunks_deleted = max(deleted_chunks.rowcount or 0, 0)

            first_seen_resets = connection.execute(
                (
                    "UPDATE chunk_slices\n"
                    "   SET first_seen_batch = batch_id\n"
                    " WHERE first_seen_batch = :batch\n"
                    "   AND batch_id <> :batch"
                ),
                {"batch": batch_id},
            )
            stats.chunk_first_reassigned = max(
                first_seen_resets.rowcount or 0, 0
            )

            last_seen_resets = connection.execute(
                (
                    "UPDATE chunk_slices\n"
                    "   SET last_seen_batch = batch_id\n"
                    " WHERE last_seen_batch = :batch\n"
                    "   AND batch_id <> :batch"
                ),
                {"batch": batch_id},
            )
            stats.chunk_last_seen_reset = max(
                last_seen_resets.rowcount or 0, 0
            )

            symbol_reassign = connection.execute(
                (
                    "UPDATE symbols\n"
                    "   SET first_seen_batch = last_seen_batch\n"
                    " WHERE first_seen_batch = :batch\n"
                    "   AND last_seen_batch <> :batch"
                ),
                {"batch": batch_id},
            )
            stats.symbols_reassigned = max(
                symbol_reassign.rowcount or 0, 0
            )

            symbol_last_seen_reset = connection.execute(
                (
                    "UPDATE symbols\n"
                    "   SET last_seen_batch = COALESCE(\n"
                    "       (SELECT MAX(last_seen_batch)\n"
                    "          FROM chunk_slices\n"
                    "         WHERE chunk_slices.symbol_id = symbols.id\n"
                    "           AND chunk_slices.last_seen_batch <> :batch),\n"
                    "       first_seen_batch\n"
                    "   )\n"
                    " WHERE last_seen_batch = :batch\n"
                    "   AND first_seen_batch <> :batch"
                ),
                {"batch": batch_id},
            )
            stats.symbol_last_seen_reset = max(
                symbol_last_seen_reset.rowcount or 0, 0
            )

            symbol_deleted = connection.execute(
                "DELETE FROM symbols WHERE last_seen_batch = :batch",
                {"batch": batch_id},
            )
            stats.symbols_deleted = max(symbol_deleted.rowcount or 0, 0)

            files_reassigned = connection.execute(
                (
                    "UPDATE files\n"
                    "   SET batch_id = (\n"
                    "       SELECT MAX(last_seen_batch)\n"
                    "         FROM chunk_slices\n"
                    "        WHERE chunk_slices.file_id = files.id\n"
                    "          AND chunk_slices.last_seen_batch <> :batch\n"
                    "   )\n"
                    " WHERE batch_id = :batch\n"
                    "   AND EXISTS (\n"
                    "       SELECT 1\n"
                    "         FROM chunk_slices\n"
                    "        WHERE chunk_slices.file_id = files.id\n"
                    "          AND chunk_slices.last_seen_batch <> :batch\n"
                    "   )"
                ),
                {"batch": batch_id},
            )
            stats.files_reassigned = max(
                files_reassigned.rowcount or 0, 0
            )

            files_deleted = connection.execute(
                "DELETE FROM files WHERE batch_id = :batch",
                {"batch": batch_id},
            )
            stats.files_deleted = max(files_deleted.rowcount or 0, 0)

            connection.execute(
                "DELETE FROM batches WHERE id = :batch",
                {"batch": batch_id},
            )

        next_cursor = connection.execute(
            (
                "SELECT id, ref, generated_at, notes\n"
                "  FROM batches\n"
                " ORDER BY generated_at DESC, id DESC\n"
                " LIMIT 1"
            )
        )
        next_row = next_cursor.fetchone()

        remaining = connection.execute(
            "SELECT COUNT(*) FROM batches",
        ).fetchone()[0]

    finally:
        connection.close()

    removed = _BatchRow(
        batch_id=str(batch_row["id"]),
        ref=batch_row["ref"] if batch_row["ref"] else None,
        generated_at=_parse_generated_at(batch_row["generated_at"]),
        notes=batch_row["notes"] if batch_row["notes"] else None,
    )

    if next_row is None:
        next_batch: _BatchRow | None = None
    else:
        next_batch = _BatchRow(
            batch_id=str(next_row["id"]),
            ref=next_row["ref"] if next_row["ref"] else None,
            generated_at=_parse_generated_at(next_row["generated_at"]),
            notes=next_row["notes"] if next_row["notes"] else None,
        )

    return stats, removed, next_batch, int(remaining)


def _sync_manifest_after_removal(
    *,
    context: ParserCLIContext,
    source: str,
    manifest_state: ParserManifestState,
    removed_batch: str,
    next_batch: _BatchRow | None,
) -> bool:
    if manifest_state.last_batch_id != removed_batch:
        return False

    manifest_service = context.manifest
    modules_key, module_key = manifest_service.settings.module_key(
        PARSER_MODULE_KEY
    )
    short_removed = _shorten_batch_id(removed_batch)
    next_note = None
    if next_batch is not None:
        next_note = (
            "Next available batch is %s (generated_at: %s)"
            % (
                _shorten_batch_id(next_batch.batch_id),
                _format_generated_at(next_batch.generated_at),
            )
        )

    def _mutate(snapshot: ManifestSnapshot) -> None:
        modules = snapshot.ensure_modules()
        current_payload = modules.get(module_key)
        current_state = ParserManifestState.from_mapping(current_payload)
        notes: list[str] = [
            f"Removed parser batch {short_removed} via CLI.",
            _vector_sync_note(source),
            f"Run `raggd parser parse {source}` to regenerate parser data.",
        ]
        if next_note:
            notes.insert(1, next_note)

        replacement = ParserManifestState(
            enabled=current_state.enabled,
            last_batch_id=None,
            last_run_started_at=None,
            last_run_completed_at=None,
            last_run_status=HealthStatus.DEGRADED,
            last_run_summary=f"Manual removal of parser batch {short_removed}.",
            last_run_warnings=(notes[0],),
            last_run_errors=(),
            last_run_notes=tuple(notes),
            handler_versions={},
            metrics=ParserRunMetrics(),
        )
        modules[module_key] = replacement.to_mapping()
        snapshot.data["modules_version"] = MODULES_VERSION

    manifest_service.write(source, mutate=_mutate)
    return True


def _display_source_info(
    *,
    name: str,
    state: ParserManifestState,
    availability: tuple[Any, ...],
    overrides: tuple[str, ...],
    settings_enabled: bool,
) -> None:
    typer.secho(
        f"Parser info for {name}",
        fg=typer.colors.CYAN,
        bold=True,
    )
    _emit_basic_info(state=state, settings_enabled=settings_enabled)
    _emit_run_messages(state)
    _emit_metrics_summary(state)
    _emit_handler_sections(state=state, availability=availability)
    _emit_dependency_section(availability)
    _emit_overrides_summary(overrides)
    typer.echo("")


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
        help=("Override configured fail-fast behavior for this parse run."),
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

    enabled_targets, disabled_targets = _partition_targets(parse_targets)
    _notify_disabled_targets(disabled_targets)

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

    outcomes = _run_parse_targets(
        context=context,
        enabled_targets=enabled_targets,
        concurrency=concurrency,
        fail_fast=resolved_fail_fast,
    )

    exit_code = _render_parse_results(parse_targets, outcomes)
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
    sources, invalid = _resolve_info_sources(context, source)

    if invalid:
        for name in invalid:
            typer.secho(f"Unknown source: {name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if not sources:
        typer.secho(
            "No sources configured; nothing to report.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    availability = context.parser_service.handler_availability()
    overrides = _compute_config_overrides(context.settings)

    exit_code = 0
    for name in sources:
        try:
            state = context.parser_service.load_manifest_state(name)
        except Exception as exc:  # pragma: no cover - manifest failure path
            typer.secho(
                f"Failed to load parser manifest for {name}: {exc}",
                fg=typer.colors.RED,
            )
            context.logger.error(
                "parser-info-error",
                source=name,
                error=str(exc),
            )
            exit_code = 1
            continue
        _display_source_info(
            name=name,
            state=state,
            availability=availability,
            overrides=overrides,
            settings_enabled=context.settings.enabled,
        )

    context.logger.info(
        "parser-info",
        sources=sources,
        module_enabled=context.settings.enabled,
        overrides=len(overrides),
    )
    raise typer.Exit(code=exit_code)


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
    sources, invalid = _resolve_info_sources(context, source)

    if invalid:
        for name in invalid:
            typer.secho(f"Unknown source: {name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if not sources:
        typer.secho(
            "No sources configured; nothing to list.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    exit_code = 0
    listed_counts: dict[str, int] = {}

    for name in sources:
        try:
            state = context.parser_service.load_manifest_state(name)
        except Exception as exc:  # pragma: no cover - manifest failure path
            typer.secho(
                f"Failed to load parser manifest for {name}: {exc}",
                fg=typer.colors.RED,
            )
            context.logger.error(
                "parser-batches-manifest-error",
                source=name,
                error=str(exc),
            )
            exit_code = 1
            continue

        summaries, error = _load_batch_summaries(
            context,
            source=name,
            limit=limit,
        )

        if error is not None:
            typer.secho(error, fg=typer.colors.RED)
            context.logger.error(
                "parser-batches-load-error",
                source=name,
                error=error,
            )
            exit_code = 1
            continue

        listed_counts[name] = len(summaries)
        _display_batches_for_source(
            name=name,
            summaries=summaries,
            limit=limit,
            state=state,
        )

    context.logger.info(
        "parser-batches",
        sources=sources,
        limit=limit,
        listed=listed_counts,
    )
    raise typer.Exit(code=exit_code)


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
    if source is None or not source.strip():
        typer.secho(
            "Provide a source name to remove parser batches from.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    normalized_source = source.strip()
    if normalized_source not in context.config.workspace_sources:
        typer.secho(f"Unknown source: {normalized_source}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if batch is None or not str(batch).strip():
        typer.secho(
            "Provide a parser batch identifier to remove.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    normalized_batch = str(batch).strip()

    try:
        manifest_state = context.parser_service.load_manifest_state(
            normalized_source
        )
    except Exception as exc:  # pragma: no cover - manifest failure path
        typer.secho(
            f"Failed to load parser manifest for {normalized_source}: {exc}",
            fg=typer.colors.RED,
        )
        context.logger.error(
            "parser-remove-manifest-error",
            source=normalized_source,
            batch=normalized_batch,
            error=str(exc),
        )
        raise typer.Exit(code=1)

    if (
        not force
        and manifest_state.last_batch_id
        and manifest_state.last_batch_id == normalized_batch
    ):
        typer.secho(
            (
                f"Batch {normalized_batch} is the latest parser run for "
                f"{normalized_source}; rerun with --force to remove it."
            ),
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    with context.session_guard.acquire(
        scope=normalized_source,
        action="remove",
    ):
        exit_code = _handle_remove_command(
            context=context,
            source=normalized_source,
            batch_id=normalized_batch,
            manifest_state=manifest_state,
            force=force,
        )

    raise typer.Exit(code=exit_code)


def create_parser_app() -> typer.Typer:
    """Return the parser Typer application."""

    return _parser_app


__all__ = ["create_parser_app"]
