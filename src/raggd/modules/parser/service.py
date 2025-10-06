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

from .hashing import DEFAULT_HASH_ALGORITHM, hash_file
from .models import ParserManifestState, ParserRunMetrics, ParserRunRecord
from .registry import (
    HandlerRegistry,
    HandlerSelection,
    ParserHandlerDescriptor,
    build_default_registry,
)
from .tokenizer import DEFAULT_ENCODER, TokenEncoder, get_token_encoder
from .traversal import TraversalScope, TraversalService

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from raggd.modules.db import DbLifecycleService

__all__ = [
    "ParserError",
    "ParserModuleDisabledError",
    "ParserSourceNotConfiguredError",
    "ParserPlanEntry",
    "ParserBatchPlan",
    "ParserService",
]


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
        token_encoder_factory: Callable[[str], TokenEncoder] = get_token_encoder,
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

        self._manifest_settings = manifest_settings or manifest_settings_from_config(
            config.model_dump(mode="python")
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
        self._workspace_patterns = tuple(workspace_ignore_patterns or ())
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
            self._token_encoder = self._token_encoder_factory(self._encoder_name)
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
                    f"No handler available for {result.relative_path.as_posix()}: {exc}"
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
        )
        return plan

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
            sink.append(
                f"Handler {availability.name} degraded: {detail}"
            )
