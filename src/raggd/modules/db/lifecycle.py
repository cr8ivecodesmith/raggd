"""Database lifecycle orchestration service."""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "DbLifecycleError",
    "DbLifecycleNotImplementedError",
    "DbLifecycleService",
]

from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.manifest import (
    ManifestService,
    ManifestSettings,
    ManifestSnapshot,
    manifest_db_namespace,
)
from raggd.modules.manifest.migrator import MODULES_VERSION


class DbLifecycleError(RuntimeError):
    """Base error for database lifecycle operations."""


class DbLifecycleNotImplementedError(DbLifecycleError):
    """Raised when a lifecycle command has not been implemented yet."""


class DbLifecycleService:
    """Ensure per-source databases exist and mirror into manifests."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        manifest_service: ManifestService | None = None,
        manifest_settings: ManifestSettings | None = None,
        logger: Logger | None = None,
    ) -> None:
        if manifest_service is not None and manifest_settings is not None:
            raise ValueError(
                "Provide either manifest_service or "
                "manifest_settings, not both."
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
        self._modules_key, self._db_module_key = manifest_db_namespace(
            self._manifest.settings
        )
        self._logger = logger or get_logger(
            __name__,
            component="db-service",
        )

    def ensure(self, source: str) -> Path:
        """Ensure ``source`` has a database and manifest scaffolding."""

        db_path = self._paths.source_database_path(source)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if not db_path.exists():
            db_path.touch()
            created = True

        def _mutate(snapshot: ManifestSnapshot) -> None:
            snapshot.ensure_module(self._db_module_key)
            snapshot.data["modules_version"] = MODULES_VERSION

        self._manifest.write(source, mutate=_mutate)
        self._logger.debug(
            "db-ensure",
            source=source,
            path=str(db_path),
            created=created,
        )
        return db_path

    def upgrade(self, source: str, *, steps: int | None = None) -> None:
        """Apply pending migrations for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            f"upgrade not yet implemented for {source!r} (steps={steps!r})"
        )

    def downgrade(self, source: str, *, steps: int = 1) -> None:
        """Rollback migrations for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            f"downgrade not yet implemented for {source!r} (steps={steps})"
        )

    def info(self, source: str, *, include_schema: bool = False) -> dict[str, object]:
        """Return manifest/database info for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            (
                "info not yet implemented for "
                f"{source!r} (include_schema={include_schema})"
            )
        )

    def vacuum(
        self,
        source: str,
        *,
        concurrency: int | str | None = None,
    ) -> None:
        """Perform vacuum maintenance for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            (
                "vacuum not yet implemented for "
                f"{source!r} (concurrency={concurrency!r})"
            )
        )

    def run(
        self,
        source: str,
        *,
        sql_path: Path,
        autocommit: bool = False,
    ) -> None:
        """Execute manual SQL for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            (
                "run not yet implemented for "
                f"{source!r} (sql_path={sql_path}, autocommit={autocommit})"
            )
        )

    def reset(self, source: str, *, force: bool = False) -> None:
        """Reset the database for ``source`` (placeholder)."""

        raise DbLifecycleNotImplementedError(
            f"reset not yet implemented for {source!r} (force={force})"
        )
