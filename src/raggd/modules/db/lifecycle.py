"""Database lifecycle orchestration service."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, cast

__all__ = [
    "DbLockError",
    "DbLockTimeoutError",
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
from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)

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
from .settings import DbModuleSettings


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class DbLifecycleError(RuntimeError):
    """Base error for database lifecycle operations."""


class DbLockError(DbLifecycleError):
    """Raised when database lock coordination fails."""

    def __init__(
        self,
        *,
        source: str,
        action: str,
        path: Path,
        cause: Exception | None = None,
    ) -> None:
        message = (
            f"Database lock acquisition failed for {source!r} "
            f"while performing {action!r}: {path}"
        )
        if cause is not None:
            message = f"{message} ({cause})"
        super().__init__(message)
        self.source = source
        self.action = action
        self.path = path
        self.cause = cause


class DbLockTimeoutError(DbLockError):
    """Raised when acquiring the database lock times out."""


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
        db_settings: DbModuleSettings | None = None,
        backend: DbLifecycleBackend | None = None,
        now: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        if manifest_service is not None and manifest_settings is not None:
            raise ValueError(
                "Provide either manifest_service or manifest_settings, "
                "not both."
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
        self._db_settings = db_settings or DbModuleSettings()
        self._now = now or _default_now
        self._logger = logger or get_logger(
            __name__,
            component="db-service",
        )
        self._backend = backend or build_default_backend(
            workspace=workspace,
            settings=self._db_settings,
            manifest_settings=self._manifest_settings,
            logger=self._logger,
            now=self._now,
        )

    @contextmanager
    def lock(self, source: str, *, action: str = "db") -> Iterator[None]:
        """Serialize database access for ``source`` with a filesystem lock."""

        lock_path = self.lock_path(source)
        lock = FileLock(
            path=lock_path,
            timeout=self._db_settings.lock_timeout,
            poll_interval=self._db_settings.lock_poll_interval,
        )
        log = self._logger.bind(
            source=source,
            action=action,
            lock=str(lock_path),
        )
        log.debug("db-lock-acquire")
        try:
            lock.acquire()
        except ManifestLockTimeoutError as exc:
            log.error("db-lock-timeout", error=str(exc))
            raise DbLockTimeoutError(
                source=source,
                action=action,
                path=lock_path,
                cause=exc,
            ) from exc
        except ManifestLockError as exc:
            log.error("db-lock-error", error=str(exc))
            raise DbLockError(
                source=source,
                action=action,
                path=lock_path,
                cause=exc,
            ) from exc
        try:
            yield
        finally:
            log.debug("db-lock-release")
            lock.release()

    def lock_path(self, source: str) -> Path:
        """Return the filesystem lock path for ``source``."""

        lock_root = (
            self._paths.workspace / ".locks" / self._db_settings.lock_namespace
        )
        lock_root.mkdir(parents=True, exist_ok=True)
        key = self._sanitize_lock_key(source)
        return lock_root / f"{key}{self._db_settings.lock_suffix}"

    def ensure(self, source: str) -> Path:
        """Ensure ``source`` has a database and manifest scaffolding."""

        db_path, created = self._prepare_db_path(source, touch=True)

        ensured_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            outcome = cast(
                DbEnsureOutcome,
                self._call_backend(
                    "ensure",
                    self._backend.ensure,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    now=ensured_at,
                ),
            )
            return outcome.status.replace(last_ensure_at=ensured_at)

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

        upgraded_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            outcome = cast(
                DbUpgradeOutcome,
                self._call_backend(
                    "upgrade",
                    self._backend.upgrade,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    steps=steps,
                    now=upgraded_at,
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

        downgraded_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            outcome = cast(
                DbDowngradeOutcome,
                self._call_backend(
                    "downgrade",
                    self._backend.downgrade,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    steps=steps,
                    now=downgraded_at,
                ),
            )
            return outcome.status

        self._mutate_manifest(
            source,
            operation="downgrade",
            mutator=_apply,
        )

    def info(
        self,
        source: str,
        *,
        include_schema: bool = False,
        include_counts: bool = False,
    ) -> dict[str, object]:
        """Return manifest/database info for ``source``."""

        db_path, _ = self._prepare_db_path(source)
        metadata: dict[str, object] = {}
        table_counts: dict[str, int | None] | None = None
        table_counts_skipped: list[dict[str, object]] = []
        info_outcome: DbInfoOutcome | None = None

        inspected_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            nonlocal info_outcome
            info_outcome = cast(
                DbInfoOutcome,
                self._call_backend(
                    "info",
                    self._backend.info,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    include_schema=include_schema,
                    include_counts=include_counts,
                    now=inspected_at,
                ),
            )
            return info_outcome.status

        state = self._mutate_manifest(
            source,
            operation="info",
            mutator=_apply,
        )

        if info_outcome is None:  # pragma: no cover - defensive
            raise DbLifecycleError("info outcome missing")

        schema = info_outcome.schema
        (
            metadata,
            table_counts,
            table_counts_skipped,
        ) = self._partition_info_metadata(info_outcome.metadata)
        skip_summary = self._summarize_table_count_skips(table_counts_skipped)

        payload = self._build_info_payload(
            source=source,
            db_path=db_path,
            state=state,
            include_schema=include_schema,
            schema=schema,
            metadata=metadata,
            table_counts=table_counts,
            table_counts_skipped=table_counts_skipped,
            skip_summary=skip_summary,
        )
        log_payload = self._build_info_log_payload(
            source=source,
            db_path=db_path,
            include_schema=include_schema,
            include_counts=include_counts,
            inspected_at=inspected_at,
            metadata=metadata,
            table_counts=table_counts,
            table_counts_skipped=table_counts_skipped,
            skip_summary=skip_summary,
        )

        self._logger.info("db-info", **log_payload)
        return payload

    @staticmethod
    def _partition_info_metadata(
        metadata: Mapping[str, object] | None,
    ) -> tuple[
        dict[str, object],
        dict[str, int | None] | None,
        list[dict[str, object]],
    ]:
        if not metadata:
            return {}, None, []

        raw_metadata = dict(metadata)
        counts_data = raw_metadata.pop("table_counts", None)
        table_counts: dict[str, int | None] | None = None
        if counts_data is None:
            table_counts = None
        elif isinstance(counts_data, Mapping):
            table_counts = dict(counts_data)
        else:  # pragma: no cover - defensive
            table_counts = cast(dict[str, int | None], counts_data)

        skipped_data = raw_metadata.pop("table_counts_skipped", None)
        skipped = DbLifecycleService._normalize_table_count_skips(skipped_data)
        return raw_metadata, table_counts, skipped

    @staticmethod
    def _normalize_table_count_skips(
        skipped_data: object,
    ) -> list[dict[str, object]]:
        if not skipped_data:
            return []
        if isinstance(skipped_data, Mapping):
            return [dict(skipped_data)]
        if isinstance(skipped_data, Sequence) and not isinstance(
            skipped_data, (str, bytes)
        ):
            normalized: list[dict[str, object]] = []
            for entry in skipped_data:
                if isinstance(entry, Mapping):
                    normalized.append(dict(entry))
                else:  # pragma: no cover - defensive
                    normalized.append(cast(dict[str, object], entry))
            return normalized
        return [cast(dict[str, object], skipped_data)]

    @staticmethod
    def _summarize_table_count_skips(
        skipped: Sequence[Mapping[str, object]],
    ) -> dict[str, int]:
        if not skipped:
            return {}
        skip_counter = Counter(
            str(entry.get("reason", "unknown")) for entry in skipped
        )
        return dict(skip_counter)

    @staticmethod
    def _build_info_payload(
        *,
        source: str,
        db_path: Path,
        state: DbManifestState,
        include_schema: bool,
        schema: str | None,
        metadata: dict[str, object],
        table_counts: dict[str, int | None] | None,
        table_counts_skipped: list[dict[str, object]],
        skip_summary: Mapping[str, int],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": source,
            "database": str(db_path),
            "manifest": state.to_mapping(),
        }
        if include_schema and schema is not None:
            payload["schema"] = schema
        if metadata:
            payload["metadata"] = metadata
        if table_counts is not None:
            payload["table_counts"] = table_counts
        if table_counts_skipped:
            payload["table_counts_skipped"] = table_counts_skipped
            if skip_summary:
                payload["table_counts_skipped_summary"] = dict(skip_summary)
        return payload

    @staticmethod
    def _build_info_log_payload(
        *,
        source: str,
        db_path: Path,
        include_schema: bool,
        include_counts: bool,
        inspected_at: datetime,
        metadata: Mapping[str, object],
        table_counts: Mapping[str, int | None] | None,
        table_counts_skipped: Sequence[Mapping[str, object]],
        skip_summary: Mapping[str, int],
    ) -> dict[str, object]:
        log_payload: dict[str, object] = {
            "source": source,
            "database": str(db_path),
            "include_schema": include_schema,
            "include_counts": include_counts,
            "inspected_at": inspected_at.isoformat(),
        }
        if table_counts is not None:
            log_payload["table_counts"] = table_counts
        if table_counts_skipped:
            log_payload["table_counts_skipped"] = list(table_counts_skipped)
            if skip_summary:
                log_payload["table_counts_skipped_summary"] = dict(skip_summary)
        if metadata:
            log_payload["metadata_keys"] = sorted(metadata.keys())
        return log_payload

    def vacuum(
        self,
        source: str,
        *,
        concurrency: int | str | None = None,
    ) -> None:
        """Perform vacuum maintenance for ``source``."""

        db_path, _ = self._prepare_db_path(source)

        vacuumed_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            outcome = cast(
                DbVacuumOutcome,
                self._call_backend(
                    "vacuum",
                    self._backend.vacuum,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    concurrency=concurrency,
                    now=vacuumed_at,
                ),
            )
            return outcome.status.replace(last_vacuum_at=vacuumed_at)

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

        executed_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
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
                    now=executed_at,
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

        reset_at = self._now()

        def _apply(
            _: ManifestSnapshot,
            state: DbManifestState,
        ) -> DbManifestState:
            outcome = cast(
                DbResetOutcome,
                self._call_backend(
                    "reset",
                    self._backend.reset,
                    source=source,
                    db_path=db_path,
                    manifest=state,
                    force=force,
                    now=reset_at,
                ),
            )
            return outcome.status.replace(last_ensure_at=reset_at)

        self._mutate_manifest(
            source,
            operation="reset",
            mutator=_apply,
        )

    def _prepare_db_path(
        self,
        source: str,
        *,
        touch: bool = False,
    ) -> tuple[Path, bool]:
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

    def _sanitize_lock_key(self, source: str) -> str:
        cleaned = source.strip() if source else ""
        if not cleaned:
            cleaned = "workspace"
        cleaned = (
            cleaned.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
        )
        return cleaned
