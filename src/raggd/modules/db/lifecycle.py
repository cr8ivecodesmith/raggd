"""Database lifecycle orchestration service."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast

__all__ = [
    "DbLifecycleError",
    "DbManifestSyncError",
    "DbOperationError",
    "DbLifecycleNotImplementedError",
    "DbLifecycleService",
]

from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.manifest import (
    ManifestError,
    ManifestService,
    ManifestSettings,
    ManifestSnapshot,
    manifest_db_namespace,
)
from raggd.modules.manifest.migrator import MODULES_VERSION

from .backend import (
    DbDowngradeOutcome,
    DbEnsureOutcome,
    DbInfoOutcome,
    DbLifecycleBackend,
    DbResetOutcome,
    DbRunOutcome,
    DbUpgradeOutcome,
    DbVacuumOutcome,
    build_default_backend,
)
from .models import DbManifestState


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class DbLifecycleError(RuntimeError):
    """Base error for database lifecycle operations."""


class DbManifestSyncError(DbLifecycleError):
    """Raised when manifest mirroring fails."""


class DbOperationError(DbLifecycleError):
    """Raised when a backend operation fails."""

    def __init__(self, *, action: str, source: str, cause: Exception) -> None:
        message = f"{action} failed for {source}: {cause}"
        super().__init__(message)
        self.action = action
        self.source = source
        self.cause = cause


class DbLifecycleNotImplementedError(DbLifecycleError):
    """Retained for compatibility with legacy callers."""


class DbLifecycleService:
    """Ensure per-source databases exist and mirror into manifests."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        manifest_service: ManifestService | None = None,
        manifest_settings: ManifestSettings | None = None,
        backend: DbLifecycleBackend | None = None,
        now: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        if manifest_service is not None and manifest_settings is not None:
            raise ValueError(
                "Provide either manifest_service or manifest_settings, not both."
            )

        self._paths = workspace
        self._manifest = (
            manifest_service
            if manifest_service is not None
            else ManifestService(
                workspace=workspace,
                settings=manifest_settings,
            )
        )
        self._manifest_settings = self._manifest.settings
        self._modules_key, self._db_module_key = manifest_db_namespace(
            self._manifest_settings
        )
        self._backend = backend or build_default_backend()
        self._now = now or _default_now
        self._logger = logger or get_logger(
            __name__,
            component="db-service",
        )

    def ensure(self, source: str) -> Path:
        """Ensure ``source`` has a database and manifest scaffolding."""

        db_path, created = self._prepare_db_path(source, touch=True)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbEnsureOutcome,
                self._call_backend(
                    "ensure",
                    self._backend.ensure,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                ),
            )
            return outcome.status.replace(last_ensure_at=self._now())

        updated_state = self._mutate_manifest(
            source,
            operation="ensure",
            mutator=_apply,
        )

        self._logger.debug(
            "db-ensure",
            source=source,
            path=str(db_path),
            created=created,
            pending=list(updated_state.pending_migrations),
        )
        return db_path

    def upgrade(self, source: str, *, steps: int | None = None) -> None:
        """Apply pending migrations for ``source``."""

        db_path, _ = self._prepare_db_path(source)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbUpgradeOutcome,
                self._call_backend(
                    "upgrade",
                    self._backend.upgrade,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    steps=steps,
                ),
            )
            return outcome.status

        self._mutate_manifest(
            source,
            operation="upgrade",
            mutator=_apply,
        )

    def downgrade(self, source: str, *, steps: int = 1) -> None:
        """Rollback migrations for ``source``."""

        db_path, _ = self._prepare_db_path(source)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbDowngradeOutcome,
                self._call_backend(
                    "downgrade",
                    self._backend.downgrade,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    steps=steps,
                ),
            )
            return outcome.status

        self._mutate_manifest(
            source,
            operation="downgrade",
            mutator=_apply,
        )

    def info(self, source: str, *, include_schema: bool = False) -> dict[str, object]:
        """Return manifest/database info for ``source``."""

        db_path, _ = self._prepare_db_path(source)
        schema: str | None = None
        metadata: dict[str, object] = {}

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            nonlocal schema, metadata
            outcome = cast(
                DbInfoOutcome,
                self._call_backend(
                    "info",
                    self._backend.info,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    include_schema=include_schema,
                ),
            )
            schema = outcome.schema
            metadata = dict(outcome.metadata or {})
            return outcome.status

        state = self._mutate_manifest(
            source,
            operation="info",
            mutator=_apply,
        )

        payload: dict[str, object] = {
            "source": source,
            "database": str(db_path),
            "manifest": state.to_mapping(),
        }
        if include_schema:
            payload["schema"] = schema
        if metadata:
            payload["metadata"] = metadata
        return payload

    def vacuum(
        self,
        source: str,
        *,
        concurrency: int | str | None = None,
    ) -> None:
        """Perform vacuum maintenance for ``source``."""

        db_path, _ = self._prepare_db_path(source)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbVacuumOutcome,
                self._call_backend(
                    "vacuum",
                    self._backend.vacuum,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    concurrency=concurrency,
                ),
            )
            return outcome.status.replace(last_vacuum_at=self._now())

        self._mutate_manifest(
            source,
            operation="vacuum",
            mutator=_apply,
        )

    def run(
        self,
        source: str,
        *,
        sql_path: Path,
        autocommit: bool = False,
    ) -> None:
        """Execute manual SQL for ``source``."""

        if not sql_path.exists():
            raise DbLifecycleError(
                f"SQL script not found for {source}: {sql_path}"
            )

        db_path, _ = self._prepare_db_path(source)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbRunOutcome,
                self._call_backend(
                    "run",
                    self._backend.run,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    sql_path=sql_path,
                    autocommit=autocommit,
                ),
            )
            return outcome.status

        self._mutate_manifest(
            source,
            operation="run",
            mutator=_apply,
        )

    def reset(self, source: str, *, force: bool = False) -> None:
        """Reset the database for ``source``."""

        db_path, _ = self._prepare_db_path(source)

        def _apply(_: ManifestSnapshot, state: DbManifestState) -> DbManifestState:
            outcome = cast(
                DbResetOutcome,
                self._call_backend(
                    "reset",
                    self._backend.reset,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    force=force,
                ),
            )
            return outcome.status.replace(last_ensure_at=self._now())

        self._mutate_manifest(
            source,
            operation="reset",
            mutator=_apply,
        )

    def _prepare_db_path(self, source: str, *, touch: bool = False) -> tuple[Path, bool]:
        path = self._paths.source_database_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if touch and not path.exists():
            path.touch()
            created = True
        return path, created

    def _mutate_manifest(
        self,
        source: str,
        *,
        operation: str,
        mutator: Callable[[ManifestSnapshot, DbManifestState], DbManifestState],
    ) -> DbManifestState:
        try:
            with self._manifest.with_transaction(
                source,
                backup=self._manifest_settings.backups_enabled,
            ) as txn:
                module_payload = txn.snapshot.ensure_module(self._db_module_key)
                current_state = DbManifestState.from_mapping(module_payload)
                updated_state = mutator(txn.snapshot, current_state)
                module_payload.update(updated_state.to_mapping())
                txn.snapshot.data["modules_version"] = MODULES_VERSION
                return updated_state
        except ManifestError as exc:
            raise DbManifestSyncError(
                f"{operation} manifest sync failed for {source}: {exc}"
            ) from exc

    def _call_backend(
        self,
        action: str,
        func: Callable[..., object],
        *,
        source: str,
        **kwargs: object,
    ) -> object:
        try:
            return func(source=source, **kwargs)
        except DbLifecycleError:
            raise
        except Exception as exc:  # pragma: no cover - defensive fallback
            raise DbOperationError(
                action=action,
                source=source,
                cause=exc,
            ) from exc
