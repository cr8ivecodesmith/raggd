"""Service layer for managing workspace sources."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from raggd.core.logging import Logger, get_logger

from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.manifest import (
    ManifestService,
    ManifestSettings,
    ManifestSnapshot,
    manifest_db_namespace,
)
from raggd.modules.manifest.migrator import MODULES_VERSION, SOURCE_MODULE_KEY
from raggd.source.config import SourceConfigStore
from raggd.source.errors import (
    SourceDisabledError,
    SourceDirectoryConflictError,
    SourceExistsError,
    SourceHealthCheckError,
    SourceNotFoundError,
)
from raggd.source.health import evaluate_source_health
from raggd.source.models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)
from raggd.source.utils import normalize_source_slug, resolve_target_path


_LEGACY_SOURCE_FIELDS = (
    "name",
    "path",
    "enabled",
    "target",
    "last_refresh_at",
    "last_health",
)


class SourceHealthEvaluator(Protocol):
    """Callable interface used to evaluate source health."""

    def __call__(
        self,
        *,
        config: WorkspaceSourceConfig,
        manifest: SourceManifest,
    ) -> SourceHealthSnapshot: ...


@dataclass(frozen=True, slots=True)
class SourceState:
    """Snapshot of a source's configuration and manifest state."""

    config: WorkspaceSourceConfig
    manifest: SourceManifest


class SourceService:
    """Coordinate source configuration, manifests, and filesystem state."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        config_store: SourceConfigStore,
        manifest_service: ManifestService | None = None,
        manifest_settings: ManifestSettings | None = None,
        db_service: DbLifecycleService | None = None,
        health_evaluator: SourceHealthEvaluator | None = None,
        now: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._paths = workspace
        self._config_store = config_store
        if manifest_service is not None and manifest_settings is not None:
            raise ValueError(
                "Provide either manifest_service or manifest_settings, "
                "not both."
            )
        self._manifest = (
            manifest_service
            if manifest_service is not None
            else ManifestService(
                workspace=workspace,
                settings=manifest_settings,
            )
        )
        self._db = (
            db_service
            if db_service is not None
            else DbLifecycleService(
                workspace=workspace,
                manifest_service=self._manifest,
            )
        )
        modules_key, db_module_key = manifest_db_namespace(
            self._manifest.settings
        )
        self._modules_key = modules_key
        self._db_module_key = db_module_key
        self._source_module_key = SOURCE_MODULE_KEY
        self._now = now or self._default_now
        self._health_evaluator = (
            health_evaluator or self._build_default_health_evaluator()
        )
        self._logger = logger or get_logger(
            __name__,
            component="source-service",
        )

    def init(
        self,
        name: str,
        *,
        target: Path | None = None,
        force_refresh: bool = False,
    ) -> SourceState:
        """Initialize a new source in the workspace."""

        normalized = normalize_source_slug(name)
        app_config = self._config_store.load()
        if normalized in app_config.workspace_sources:
            raise SourceExistsError(f"Source {normalized!r} already exists.")

        source_dir = self._paths.source_dir(normalized)
        if source_dir.exists():
            raise SourceDirectoryConflictError(
                "Source directory already exists for "
                f"{normalized!r}: {source_dir}"
            )
        source_dir.mkdir(parents=True, exist_ok=False)

        target_path = None
        if target is not None:
            target_path = resolve_target_path(target, workspace=self._paths)

        enabled = target_path is not None
        config = WorkspaceSourceConfig(
            name=normalized,
            path=source_dir,
            enabled=enabled,
            target=target_path,
        )
        persisted = self._persist_config(config)

        manifest = self._load_manifest(persisted)
        manifest.enabled = persisted.enabled
        manifest.target = persisted.target
        manifest.path = persisted.path
        manifest.last_refresh_at = None
        manifest.last_health = SourceHealthSnapshot()
        self._write_manifest(manifest)

        self._db.ensure(normalized)

        if target_path is not None or force_refresh:
            # ``force=True`` bypasses gating during bootstrap scenarios.
            return self.refresh(normalized, force=True)

        return SourceState(config=persisted, manifest=manifest)

    def set_target(
        self,
        name: str,
        target: Path | None,
        *,
        force: bool = False,
    ) -> SourceState:
        """Update the target path for a source."""

        config = self._get_source_config(name)
        manifest = self._load_manifest(config)

        if target is not None:
            target_path = resolve_target_path(target, workspace=self._paths)
        else:
            target_path = None

        snapshot = self._guard_operation(config, manifest, force=force)

        updated = config.model_copy(update={"target": target_path})
        persisted = self._persist_config(updated)

        manifest.target = persisted.target
        manifest.enabled = persisted.enabled
        manifest.path = persisted.path
        manifest.last_health = snapshot
        self._write_manifest(manifest)

        if persisted.target is not None:
            # Perform a refresh to keep artifacts in sync with the new target.
            return self._refresh(persisted.name, force=force, skip_guard=True)

        return SourceState(config=persisted, manifest=manifest)

    def refresh(
        self,
        name: str,
        *,
        force: bool = False,
    ) -> SourceState:
        """Refresh managed artifacts for a source."""

        return self._refresh(name, force=force, skip_guard=False)

    def rename(
        self,
        current_name: str,
        new_name: str,
        *,
        force: bool = False,
    ) -> SourceState:
        """Rename a source and its managed artifacts."""

        config = self._get_source_config(current_name)
        original_dir = self._paths.source_dir(config.name)
        if not original_dir.exists():
            raise SourceDirectoryConflictError(
                "Source directory is missing for "
                f"{config.name!r}: {original_dir}"
            )

        manifest = self._load_manifest(config)

        snapshot = self._guard_operation(config, manifest, force=force)

        normalized_new = normalize_source_slug(new_name)
        if normalized_new == config.name:
            manifest.last_health = snapshot
            self._write_manifest(manifest)
            return SourceState(config=config, manifest=manifest)

        app_config = self._config_store.load()
        if normalized_new in app_config.workspace_sources:
            raise SourceExistsError(
                f"Source {normalized_new!r} already exists."
            )

        new_dir = self._paths.source_dir(normalized_new)
        if new_dir.exists():
            raise SourceDirectoryConflictError(
                "Target directory already exists for "
                f"{normalized_new!r}: {new_dir}"
            )

        original_dir.rename(new_dir)

        sources = dict(app_config.workspace_sources)
        sources.pop(config.name, None)
        renamed_config = config.model_copy(
            update={
                "name": normalized_new,
                "path": new_dir,
            }
        )
        sources[normalized_new] = renamed_config

        updated_app_config = self._config_store.replace_all(sources)
        persisted = updated_app_config.workspace_sources[normalized_new]

        manifest = self._load_manifest(persisted)
        manifest.last_health = snapshot
        manifest.path = persisted.path
        manifest.name = persisted.name
        manifest.enabled = persisted.enabled
        manifest.target = persisted.target
        self._write_manifest(manifest)

        return SourceState(config=persisted, manifest=manifest)

    def remove(
        self,
        name: str,
        *,
        force: bool = False,
    ) -> None:
        """Remove a source configuration and its filesystem artifacts."""

        config = self._get_source_config(name)
        manifest = self._load_manifest(config)

        self._guard_operation(config, manifest, force=force)

        source_dir = self._paths.source_dir(name)
        if source_dir.exists():
            shutil.rmtree(source_dir)

        self._config_store.remove(name)

    def enable(self, *names: str) -> list[SourceState]:
        """Enable one or more sources and return their state."""

        resolved = self._require_names(names)
        return self._set_enabled_state(resolved, enabled=True)

    def disable(self, *names: str) -> list[SourceState]:
        """Disable one or more sources and return their state."""

        resolved = self._require_names(names)
        return self._set_enabled_state(resolved, enabled=False)

    def list(self) -> list[SourceState]:
        """Return the current state for all sources."""

        app_config = self._config_store.load()
        states: list[SourceState] = []
        for name in sorted(app_config.workspace_sources):
            config = app_config.workspace_sources[name]
            manifest = self._load_manifest(config)
            states.append(SourceState(config=config, manifest=manifest))
        return states

    def _refresh(
        self,
        name: str,
        *,
        force: bool,
        skip_guard: bool,
    ) -> SourceState:
        config = self._get_source_config(name)
        source_dir = self._paths.source_dir(name)
        if not source_dir.exists():
            raise SourceDirectoryConflictError(
                f"Source directory is missing for {name!r}: {source_dir}"
            )

        manifest = self._load_manifest(config)

        if skip_guard:
            snapshot = self._health_evaluator(config=config, manifest=manifest)
        else:
            snapshot = self._guard_operation(config, manifest, force=force)

        self._db.ensure(name)

        manifest.last_refresh_at = self._now()
        manifest.last_health = snapshot
        manifest.enabled = config.enabled
        manifest.target = config.target
        manifest.path = config.path
        self._write_manifest(manifest)

        return SourceState(config=config, manifest=manifest)

    def _set_enabled_state(
        self,
        names: Sequence[str],
        *,
        enabled: bool,
    ) -> list[SourceState]:
        app_config = self._config_store.load()
        sources = dict(app_config.workspace_sources)

        for name in names:
            if name not in sources:
                raise SourceNotFoundError(f"Source {name!r} is not configured.")

        for name in names:
            current = sources[name]
            sources[name] = current.model_copy(update={"enabled": enabled})

        updated_app_config = self._config_store.replace_all(sources)
        result: list[SourceState] = []

        for name in names:
            config = updated_app_config.workspace_sources[name]
            manifest = self._load_manifest(config)
            manifest.enabled = config.enabled
            if enabled:
                snapshot = self._health_evaluator(
                    config=config,
                    manifest=manifest,
                )
                manifest.last_health = snapshot
            self._write_manifest(manifest)
            result.append(SourceState(config=config, manifest=manifest))

        return result

    def _get_source_config(self, name: str) -> WorkspaceSourceConfig:
        app_config = self._config_store.load()
        config = app_config.workspace_sources.get(name)
        if config is None:
            raise SourceNotFoundError(f"Source {name!r} is not configured.")
        return config

    def _persist_config(
        self,
        config: WorkspaceSourceConfig,
    ) -> WorkspaceSourceConfig:
        app_config = self._config_store.upsert(config)
        return app_config.workspace_sources[config.name]

    def _load_manifest(self, config: WorkspaceSourceConfig) -> SourceManifest:
        snapshot = self._manifest.load(
            config.name,
            apply_migrations=True,
        )
        return self._snapshot_to_manifest(snapshot, config)

    def _write_manifest(self, manifest: SourceManifest) -> None:
        def _mutate(snapshot: ManifestSnapshot) -> None:
            snapshot.ensure_module(self._db_module_key)
            module = snapshot.ensure_module(self._source_module_key)
            dump = manifest.model_dump(mode="json")
            module.update(
                {
                    "name": dump["name"],
                    "path": dump["path"],
                    "enabled": dump["enabled"],
                    "target": dump.get("target"),
                    "last_refresh_at": dump.get("last_refresh_at"),
                    "last_health": dump.get("last_health"),
                }
            )
            snapshot.data["modules_version"] = MODULES_VERSION
            for field in _LEGACY_SOURCE_FIELDS:
                snapshot.data.pop(field, None)

        self._manifest.write(manifest.name, mutate=_mutate)

    def _snapshot_to_manifest(
        self,
        snapshot: ManifestSnapshot,
        config: WorkspaceSourceConfig,
    ) -> SourceManifest:
        module = snapshot.module(self._source_module_key) or {}
        payload = {
            "name": module.get("name", config.name),
            "path": config.path,
            "enabled": config.enabled,
            "target": module.get("target", config.target),
            "last_refresh_at": module.get("last_refresh_at"),
            "last_health": module.get(
                "last_health",
                SourceHealthSnapshot(),
            ),
        }
        return SourceManifest.model_validate(payload)

    def _guard_operation(
        self,
        config: WorkspaceSourceConfig,
        manifest: SourceManifest,
        *,
        force: bool,
    ) -> SourceHealthSnapshot:
        if force:
            return self._health_evaluator(config=config, manifest=manifest)

        if not config.enabled:
            raise SourceDisabledError(f"Source {config.name!r} is disabled.")

        snapshot = self._health_evaluator(
            config=config,
            manifest=manifest,
        )
        if snapshot.status in (
            SourceHealthStatus.DEGRADED,
            SourceHealthStatus.ERROR,
        ):
            disabled = config.model_copy(update={"enabled": False})
            persisted = self._persist_config(disabled)
            manifest.enabled = persisted.enabled
            manifest.last_health = snapshot
            self._write_manifest(manifest)
            status_value = (
                snapshot.status.value
                if hasattr(snapshot.status, "value")
                else str(snapshot.status)
            )
            self._logger.warning(
                "source-auto-disabled",
                source=config.name,
                status=status_value,
                summary=snapshot.summary,
                actions=snapshot.actions,
            )
            raise SourceHealthCheckError(
                "Health check for source "
                f"{config.name!r} failed with status {snapshot.status}."
            )

        return snapshot

    def _build_default_health_evaluator(self) -> SourceHealthEvaluator:
        def _evaluate(
            *,
            config: WorkspaceSourceConfig,
            manifest: SourceManifest,
        ) -> SourceHealthSnapshot:
            return evaluate_source_health(
                config=config,
                manifest=manifest,
                now=self._now,
            )

        return _evaluate

    @staticmethod
    def _default_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _require_names(names: Iterable[str]) -> tuple[str, ...]:
        extracted = tuple(name for name in names if name)
        if not extracted:
            raise SourceNotFoundError("Provide at least one source name.")
        return extracted
