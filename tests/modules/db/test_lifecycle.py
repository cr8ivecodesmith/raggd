from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import (
    DbLifecycleError,
    DbOperationError,
    DbLifecycleService,
)
from raggd.modules.db.backend import (
    DbDowngradeOutcome,
    DbEnsureOutcome,
    DbInfoOutcome,
    DbResetOutcome,
    DbRunOutcome,
    DbUpgradeOutcome,
    DbVacuumOutcome,
)
from raggd.modules.db.models import DbManifestState
from raggd.modules.manifest import ManifestService, ManifestSettings
from raggd.modules.manifest.migrator import MODULES_VERSION


def _make_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def test_db_lifecycle_rejects_conflicting_manifest_args(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)

    with pytest.raises(ValueError):
        DbLifecycleService(
            workspace=paths,
            manifest_service=manifest,
            manifest_settings=ManifestSettings(),
        )


class RecordingBackend:
    """Backend test double capturing lifecycle invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        baseline = DbManifestState()
        self.ensure_status = baseline
        self.upgrade_status = baseline
        self.downgrade_status = baseline
        self.info_status = baseline
        self.vacuum_status = baseline
        self.run_status = baseline
        self.reset_status = baseline
        self.info_schema: str | None = None
        self.info_metadata: dict[str, object] = {}
        self.raise_for: dict[str, Exception] = {}

    def _record(self, action: str, **payload: Any) -> None:
        self.calls.append((action, payload))
        if action in self.raise_for:
            raise self.raise_for[action]

    def ensure(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        now: datetime,
    ) -> DbEnsureOutcome:
        self._record("ensure", source=source, path=db_path, now=now)
        return DbEnsureOutcome(status=self.ensure_status)

    def upgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int | None,
        now: datetime,
    ) -> DbUpgradeOutcome:
        self._record("upgrade", source=source, steps=steps, now=now)
        return DbUpgradeOutcome(
            status=self.upgrade_status,
            applied_migrations=(),
        )

    def downgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int,
        now: datetime,
    ) -> DbDowngradeOutcome:
        self._record("downgrade", source=source, steps=steps, now=now)
        return DbDowngradeOutcome(
            status=self.downgrade_status,
            rolled_back_migrations=(),
        )

    def info(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        include_schema: bool,
        now: datetime,
    ) -> DbInfoOutcome:
        self._record("info", source=source, include_schema=include_schema, now=now)
        metadata = self.info_metadata if self.info_metadata else {}
        return DbInfoOutcome(
            status=self.info_status,
            schema=self.info_schema if include_schema else None,
            metadata=metadata,
        )

    def vacuum(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        concurrency: int | str | None,
        now: datetime,
    ) -> DbVacuumOutcome:
        self._record("vacuum", source=source, concurrency=concurrency, now=now)
        return DbVacuumOutcome(status=self.vacuum_status)

    def run(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        sql_path: Path,
        autocommit: bool,
        now: datetime,
    ) -> DbRunOutcome:
        self._record(
            "run",
            source=source,
            sql_path=sql_path,
            autocommit=autocommit,
            now=now,
        )
        return DbRunOutcome(status=self.run_status)

    def reset(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        force: bool,
        now: datetime,
    ) -> DbResetOutcome:
        self._record("reset", source=source, force=force, now=now)
        return DbResetOutcome(status=self.reset_status)


def test_ensure_updates_manifest_and_pending(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    now = datetime(2025, 1, 2, 12, 30, tzinfo=timezone.utc)

    backend = RecordingBackend()
    backend.ensure_status = DbManifestState(
        pending_migrations=("0001-test",),
    )

    service = DbLifecycleService(
        workspace=paths,
        backend=backend,
        now=lambda: now,
    )

    db_path = service.ensure("demo")

    assert db_path.exists()
    assert backend.calls and backend.calls[0][0] == "ensure"

    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load(
        "demo",
        apply_migrations=True,
    )
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)
    assert modules["pending_migrations"] == ["0001-test"]
    assert modules["last_ensure_at"] == now.isoformat()
    assert manifest.data["modules_version"] == MODULES_VERSION


def test_upgrade_applies_backend_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    backend = RecordingBackend()
    backend.upgrade_status = DbManifestState(
        head_migration_shortuuid7="0002-next",
        head_migration_uuid7="00000000-0000-7000-8000-000000000002",
    )

    service = DbLifecycleService(workspace=paths, backend=backend)
    service.ensure("demo")
    service.upgrade("demo", steps=None)

    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load(
        "demo",
        apply_migrations=True,
    )
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)
    assert modules["head_migration_shortuuid7"] == "0002-next"
    assert modules["head_migration_uuid7"] == "00000000-0000-7000-8000-000000000002"


def test_vacuum_tracks_timestamp(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    backend = RecordingBackend()
    now = datetime(2025, 1, 3, 8, 45, tzinfo=timezone.utc)

    service = DbLifecycleService(
        workspace=paths,
        backend=backend,
        now=lambda: now,
    )
    service.ensure("demo")
    service.vacuum("demo", concurrency="auto")

    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load(
        "demo",
        apply_migrations=True,
    )
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)
    assert modules["last_vacuum_at"] == now.isoformat()
    assert backend.calls[-1][0] == "vacuum"
    assert backend.calls[-1][1]["concurrency"] == "auto"


def test_run_missing_sql_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    backend = RecordingBackend()
    service = DbLifecycleService(workspace=paths, backend=backend)

    service.ensure("demo")

    with pytest.raises(DbLifecycleError):
        service.run("demo", sql_path=paths.workspace / "missing.sql")


def test_backend_errors_wrap_in_operation_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    backend = RecordingBackend()
    backend.raise_for["upgrade"] = RuntimeError("boom")
    service = DbLifecycleService(workspace=paths, backend=backend)
    service.ensure("demo")

    with pytest.raises(DbOperationError) as exc:
        service.upgrade("demo")

    assert "boom" in str(exc.value)
