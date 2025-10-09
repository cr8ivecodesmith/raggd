"""Parser service orchestrating traversal, handler selection, and manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from raggd.core.config import (
    AppConfig,
    PARSER_MODULE_KEY,
    ParserModuleSettings,
)
from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthReport, HealthStatus
from raggd.modules.manifest import ManifestService, ManifestSettings
from raggd.modules.manifest.helpers import manifest_settings_from_config
from raggd.modules.manifest.migrator import MODULES_VERSION
from raggd.modules.manifest.service import ManifestSnapshot

from .handlers.base import HandlerResult
from .hashing import DEFAULT_HASH_ALGORITHM, hash_file
from .models import ParserManifestState, ParserRunMetrics, ParserRunRecord
from .registry import (
    HandlerRegistry,
    HandlerSelection,
    ParserHandlerDescriptor,
    build_default_registry,
)
from .staging import (
    FileStageOutcome,
    ParserPersistenceTransaction,
    parser_transaction,
)
from .tokenizer import DEFAULT_ENCODER, TokenEncoder, get_token_encoder
from .traversal import TraversalScope, TraversalService

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from raggd.modules.db import (
        DbLifecycleService,
        DbLockError,
        DbLockTimeoutError,
    )
else:
    from raggd.modules.db import DbLockError, DbLockTimeoutError

__all__ = [
    "ParserError",
    "ParserModuleDisabledError",
    "ParserSourceNotConfiguredError",
    "ParserPlanEntry",
    "ParserBatchPlan",
    "ParserService",
]

_DEFAULT_WORKSPACE_IGNORE_PATTERNS: tuple[str, ...] = (
    "db.sqlite3",
    "db.sqlite3-journal",
    "db.sqlite3-shm",
    "db.sqlite3-wal",
    "manifest.json",
    "manifest.json.*",
)


class ParserError(RuntimeError):
    """Base exception raised by :class:`ParserService`."""


class ParserModuleDisabledError(ParserError):
    """Raised when a parser operation is attempted while disabled."""


class ParserSourceNotConfiguredError(ParserError):
    """Raised when requesting a source that is not configured."""


@dataclass(frozen=True, slots=True)
class ParserPlanEntry:
    """Planned work item referencing a file and its associated handler."""

    absolute_path: Path
    relative_path: Path
    handler: ParserHandlerDescriptor
    selection: HandlerSelection
    file_hash: str
    shebang: str | None = None


@dataclass(slots=True)
class ParserBatchPlan:
    """Aggregate of files prepared for parsing."""

    source: str
    root: Path
    entries: tuple[ParserPlanEntry, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    metrics: ParserRunMetrics = field(default_factory=ParserRunMetrics)
    handler_versions: dict[str, str] = field(default_factory=dict)

    def has_errors(self) -> bool:
        return bool(self.errors)


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class ParserService:
    """Facade coordinating parser planning, execution, and manifest IO."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        config: AppConfig,
        settings: ParserModuleSettings | None = None,
        manifest_service: ManifestService | None = None,
        manifest_settings: ManifestSettings | None = None,
        db_service: DbLifecycleService | None = None,
        registry: HandlerRegistry | None = None,
        token_encoder: TokenEncoder | None = None,
        token_encoder_factory: Callable[
            [str], TokenEncoder
        ] = get_token_encoder,
        encoder_name: str = DEFAULT_ENCODER,
        hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
        workspace_ignore_patterns: Sequence[str] | None = None,
        now: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._paths = workspace
        self._config = config
        self._settings = settings or config.parser
        if not isinstance(self._settings, ParserModuleSettings):
            # Defensive fallback if configuration yielded a generic toggle.
            self._settings = ParserModuleSettings(**self._settings.model_dump())

        self._manifest_settings = (
            manifest_settings
            or manifest_settings_from_config(config.model_dump(mode="python"))
        )
        self._manifest = manifest_service or ManifestService(
            workspace=workspace,
            settings=self._manifest_settings,
        )
        self._db = db_service
        self._registry = registry or build_default_registry(self._settings)
        self._token_encoder = token_encoder
        self._token_encoder_factory = token_encoder_factory
        self._encoder_name = encoder_name
        self._hash_algorithm = hash_algorithm
        if workspace_ignore_patterns is None:
            self._workspace_patterns = _DEFAULT_WORKSPACE_IGNORE_PATTERNS
        else:
            self._workspace_patterns = tuple(workspace_ignore_patterns)
        self._now = now or _default_now
        self._logger = logger or get_logger(
            __name__,
            component="parser-service",
        )
        modules_key, parser_key = self._manifest_settings.module_key(
            PARSER_MODULE_KEY
        )
        self._modules_key = modules_key
        self._parser_module_key = parser_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def settings(self) -> ParserModuleSettings:
        return self._settings

    @property
    def registry(self) -> HandlerRegistry:
        return self._registry

    @property
    def manifest_service(self) -> ManifestService:
        return self._manifest

    @property
    def db_service(self) -> DbLifecycleService | None:
        return self._db

    def token_encoder(self) -> TokenEncoder:
        """Return (and lazily load) the active token encoder."""

        if self._token_encoder is None:
            self._token_encoder = self._token_encoder_factory(
                self._encoder_name
            )
        return self._token_encoder

    def plan_source(
        self,
        *,
        source: str,
        scope: TraversalScope | Sequence[Path | str] | None = None,
    ) -> ParserBatchPlan:
        """Plan parsing work for ``source`` returning discovered files."""

        if not self._settings.enabled:
            raise ParserModuleDisabledError("Parser module is disabled.")

        config = self._get_source_config(source)
        traversal_scope = self._normalize_scope(scope, root=config.path)
        traversal = self._build_traversal(root=config.path)

        metrics = ParserRunMetrics()
        warnings: list[str] = []
        errors: list[str] = []
        entries: list[ParserPlanEntry] = []

        self._capture_handler_warnings(warnings)

        fallback_logged: set[tuple[str, str]] = set()

        for result in traversal.iter_files(traversal_scope):
            metrics.files_discovered += 1
            shebang = self._read_shebang(result.absolute_path)
            try:
                selection = self._registry.resolve(
                    result.absolute_path,
                    shebang=shebang,
                )
            except KeyError as exc:
                errors.append(
                    "No handler available for "
                    f"{result.relative_path.as_posix()}: {exc}"
                )
                metrics.files_failed += 1
                continue

            handler = selection.handler
            if selection.fallback:
                metrics.record_fallback()
                warnings.append(
                    "Fallback to %s for %s via %s"
                    % (
                        handler.name,
                        result.relative_path.as_posix(),
                        selection.resolved_via,
                    )
                )
                fallback_key = (handler.name, selection.resolved_via)
                if fallback_key not in fallback_logged:
                    fallback_logged.add(fallback_key)
                    self._logger.warning(
                        "parser-handler-fallback",
                        source=source,
                        handler=handler.name,
                        resolved_via=selection.resolved_via,
                        probe_status=selection.probe.status.value,
                        probe_summary=selection.probe.summary,
                    )

            try:
                file_hash = hash_file(
                    result.absolute_path,
                    handler_version=handler.version,
                    algorithm=self._hash_algorithm,
                    extra=(result.relative_path.as_posix().encode("utf-8"),),
                )
            except OSError as exc:
                errors.append(
                    f"Failed to hash {result.relative_path.as_posix()}: {exc}"
                )
                metrics.files_failed += 1
                continue

            entry = ParserPlanEntry(
                absolute_path=result.absolute_path,
                relative_path=result.relative_path,
                handler=handler,
                selection=selection,
                file_hash=file_hash,
                shebang=shebang,
            )
            entries.append(entry)
            metrics.files_parsed += 1
            metrics.increment_handler(handler.name)

        handler_versions = {
            name: descriptor.version
            for name, descriptor in self._registry.descriptors().items()
        }

        metrics.queue_depth = len(entries)

        plan = ParserBatchPlan(
            source=source,
            root=config.path,
            entries=tuple(entries),
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(dict.fromkeys(errors)),
            metrics=metrics,
            handler_versions=handler_versions,
        )

        self._logger.debug(
            "parser-plan",
            source=source,
            files=len(entries),
            warnings=len(plan.warnings),
            errors=len(plan.errors),
            fallbacks=metrics.fallbacks,
            queue_depth=len(entries),
        )
        return plan

    def stage_batch(
        self,
        *,
        source: str,
        batch_id: str,
        plan: ParserBatchPlan,
        results: Sequence[tuple[ParserPlanEntry, HandlerResult]],
        batch_ref: str | None = None,
        batch_generated_at: datetime | None = None,
        batch_notes: str | None = None,
    ) -> tuple[
        list[tuple[ParserPlanEntry, FileStageOutcome]],
        ParserRunMetrics,
    ]:
        """Stage ``results`` for ``plan`` into the source database."""

        if self._db is None:
            raise ParserError(
                "ParserService requires a DbLifecycleService to stage batches."
            )

        if not results:
            return ([], plan.metrics.copy())

        planned_entries = {entry: entry for entry in plan.entries}
        handler_versions = dict(plan.handler_versions)

        outcomes: list[tuple[ParserPlanEntry, FileStageOutcome]] = []
        metrics = plan.metrics.copy()

        lock_wait_seconds = 0.0
        try:
            with parser_transaction(
                self._db,
                source,
                hash_algorithm=self._hash_algorithm,
                now=self._now,
            ) as transaction:
                lock_wait_seconds = getattr(
                    transaction,
                    "lock_wait_seconds",
                    0.0,
                )
                self._prepare_batch(
                    transaction=transaction,
                    batch_id=batch_id,
                    batch_ref=batch_ref,
                    batch_generated_at=batch_generated_at,
                    batch_notes=batch_notes,
                )
                outcomes, metrics = self._stage_results_for_plan(
                    transaction=transaction,
                    source=source,
                    batch_id=batch_id,
                    plan=plan,
                    results=results,
                    handler_versions=handler_versions,
                    metrics=metrics,
                    planned_entries=planned_entries,
                )
        except DbLockTimeoutError as exc:
            raise ParserError(
                f"Database lock timed out for {source!r}; "
                "retry after active runs finish."
            ) from exc
        except DbLockError as exc:
            raise ParserError(
                f"Database lock failed for {source!r}: {exc}"
            ) from exc

        if lock_wait_seconds:
            metrics.record_lock_wait(lock_wait_seconds)
            self._logger.debug(
                "parser-stage-lock-wait",
                source=source,
                seconds=lock_wait_seconds,
            )

        return outcomes, metrics

    def _prepare_batch(
        self,
        *,
        transaction: "ParserPersistenceTransaction",
        batch_id: str,
        batch_ref: str | None,
        batch_generated_at: datetime | None,
        batch_notes: str | None,
    ) -> None:
        transaction.ensure_batch(
            batch_id=batch_id,
            ref=batch_ref,
            generated_at=batch_generated_at,
            notes=batch_notes,
        )

    def _stage_results_for_plan(
        self,
        *,
        transaction: "ParserPersistenceTransaction",
        source: str,
        batch_id: str,
        plan: ParserBatchPlan,
        results: Sequence[tuple[ParserPlanEntry, HandlerResult]],
        handler_versions: dict[str, str],
        metrics: ParserRunMetrics,
        planned_entries: dict[ParserPlanEntry, ParserPlanEntry],
    ) -> tuple[
        list[tuple[ParserPlanEntry, FileStageOutcome]],
        ParserRunMetrics,
    ]:
        outcomes: list[tuple[ParserPlanEntry, FileStageOutcome]] = []
        seen_entries: set[ParserPlanEntry] = set()

        for entry, result in results:
            self._validate_result_entry(
                entry=entry,
                planned_entries=planned_entries,
                seen_entries=seen_entries,
            )

            repo_path = self._resolve_repo_path(
                result_path=Path(result.file.path),
                plan_root=plan.root,
                entry=entry,
            )

            language = result.file.language or entry.handler.name
            handler_versions.setdefault(
                entry.handler.name,
                entry.handler.version,
            )

            outcome = transaction.stage_file(
                batch_id=batch_id,
                repo_path=repo_path,
                language=language,
                file_sha=entry.file_hash,
                handler_name=entry.handler.name,
                handler_version=entry.handler.version,
                handler_versions=handler_versions,
                result=result,
                absolute_path=entry.absolute_path,
            )

            outcomes.append((entry, outcome))
            self._update_metrics_for_outcome(
                metrics=metrics,
                outcome=outcome,
                entry=entry,
                source=source,
            )

        return outcomes, metrics

    def _validate_result_entry(
        self,
        *,
        entry: ParserPlanEntry,
        planned_entries: dict[ParserPlanEntry, ParserPlanEntry],
        seen_entries: set[ParserPlanEntry],
    ) -> None:
        if entry not in planned_entries:
            raise ParserError(
                "Result entry missing from plan: "
                f"{entry.relative_path.as_posix()}"
            )
        if entry in seen_entries:
            raise ParserError(
                "Duplicate handler result provided for "
                f"{entry.relative_path.as_posix()}"
            )
        seen_entries.add(entry)

    def _resolve_repo_path(
        self,
        *,
        result_path: Path,
        plan_root: Path,
        entry: ParserPlanEntry,
    ) -> Path:
        if result_path.is_absolute():
            try:
                repo_path = result_path.relative_to(plan_root)
            except ValueError as exc:
                raise ParserError(
                    "Handler result path is outside the planned root: "
                    f"{result_path} (root {plan_root})"
                ) from exc
        else:
            repo_path = result_path

        if repo_path != entry.relative_path:
            raise ParserError(
                "Handler result path mismatch for "
                f"{entry.relative_path.as_posix()} "
                f"(got {repo_path})"
            )
        return repo_path

    def _update_metrics_for_outcome(
        self,
        *,
        metrics: ParserRunMetrics,
        outcome: FileStageOutcome,
        entry: ParserPlanEntry,
        source: str,
    ) -> None:
        metrics.chunks_emitted += outcome.chunks_inserted
        metrics.chunks_reused += outcome.chunks_reused
        if outcome.symbols_written == 0 and outcome.chunks_inserted == 0:
            metrics.files_reused += 1

        self._logger.debug(
            "parser-stage-file",
            source=source,
            repo_path=entry.relative_path.as_posix(),
            handler=entry.handler.name,
            symbols_written=outcome.symbols_written,
            symbols_reused=outcome.symbols_reused,
            chunks_inserted=outcome.chunks_inserted,
            chunks_reused=outcome.chunks_reused,
        )

    def build_run_record(
        self,
        *,
        plan: ParserBatchPlan,
        batch_id: str | None,
        status: HealthStatus | None = None,
        summary: str | None = None,
        warnings: Sequence[str] | None = None,
        errors: Sequence[str] | None = None,
        notes: Sequence[str] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        metrics: ParserRunMetrics | None = None,
    ) -> ParserRunRecord:
        """Combine planning metadata with run-time information."""

        aggregated_warnings = tuple(plan.warnings) + tuple(warnings or ())
        aggregated_errors = tuple(plan.errors) + tuple(errors or ())
        metrics_snapshot = (metrics or plan.metrics).copy()
        started = started_at or self._now()
        completed = completed_at or started

        if status is None:
            if aggregated_errors:
                status = HealthStatus.ERROR
            elif aggregated_warnings:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.OK

        record = ParserRunRecord(
            batch_id=batch_id,
            started_at=started,
            completed_at=completed,
            status=status,
            summary=summary,
            warnings=aggregated_warnings,
            errors=aggregated_errors,
            notes=tuple(notes or ()),
            handler_versions=dict(plan.handler_versions),
            metrics=metrics_snapshot,
        )
        return record

    def record_run(
        self,
        *,
        source: str,
        run: ParserRunRecord,
    ) -> ParserManifestState:
        """Persist ``run`` details into the source manifest."""

        def _mutate(snapshot: ManifestSnapshot) -> None:
            modules = snapshot.ensure_modules()
            module = modules.get(self._parser_module_key)
            state = ParserManifestState.from_mapping(module)
            updated = state.apply_run(run, enabled=self._settings.enabled)
            modules[self._parser_module_key] = updated.to_mapping()
            snapshot.data["modules_version"] = MODULES_VERSION

        snapshot = self._manifest.write(source, mutate=_mutate)
        payload = (
            snapshot.module(self._parser_module_key)
            if hasattr(snapshot, "module")
            else snapshot.data.get(self._modules_key, {}).get(
                self._parser_module_key
            )
        )
        return ParserManifestState.from_mapping(payload)

    def load_manifest_state(self, source: str) -> ParserManifestState:
        """Return the persisted parser manifest payload for ``source``."""

        snapshot = self._manifest.load(source, apply_migrations=True)
        module = snapshot.module(self._parser_module_key)
        return ParserManifestState.from_mapping(module)

    def health_report(self, source: str) -> HealthReport:
        """Compute a health report for ``source`` from manifest data."""

        state = self.load_manifest_state(source)
        return state.to_health_report(module=self._parser_module_key)

    def handler_availability(self) -> tuple[tuple[str, HealthStatus], ...]:
        """Return handler availability status pairs."""

        availability = []
        for snapshot in self._registry.availability():
            availability.append((snapshot.name, snapshot.status))
        return tuple(availability)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_source_config(self, name: str):
        config = self._config.workspace_sources.get(name)
        if config is None:
            raise ParserSourceNotConfiguredError(
                f"Source {name!r} is not configured in the workspace."
            )
        return config

    def _build_traversal(self, *, root: Path) -> TraversalService:
        return TraversalService(
            root=root,
            gitignore_behavior=self._settings.gitignore_behavior,
            workspace_patterns=self._workspace_patterns,
        )

    def _normalize_scope(
        self,
        scope: TraversalScope | Sequence[Path | str] | None,
        *,
        root: Path,
    ) -> TraversalScope | None:
        if scope is None:
            return None
        if isinstance(scope, TraversalScope):
            return scope
        candidates: list[Path] = []
        for entry in scope:
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            candidates.append(candidate)
        return TraversalScope.from_iterable(candidates)

    def _read_shebang(self, path: Path) -> str | None:
        try:
            with path.open("rb") as buffer:
                first_line = buffer.readline(256)
        except OSError:
            return None
        if not first_line.startswith(b"#!"):
            return None
        try:
            return first_line.decode("utf-8", errors="ignore").strip()
        except UnicodeDecodeError:
            return None

    def _capture_handler_warnings(self, sink: list[str]) -> None:
        for availability in self._registry.availability():
            if not availability.enabled:
                continue
            if availability.status is HealthStatus.OK:
                continue
            detail = availability.summary or availability.status.value
            self._logger.warning(
                "parser-handler-degraded",
                handler=availability.name,
                status=availability.status.value,
                summary=availability.summary,
                warnings=availability.warnings,
            )
            sink.append(f"Handler {availability.name} degraded: {detail}")
