from __future__ import annotations

import copy
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import tomlkit
from structlog import get_logger
from structlog.testing import capture_logs

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.db.settings import DbModuleSettings
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.manifest import ManifestService, ManifestSettings
from raggd.modules.manifest.migrator import MODULES_VERSION
from raggd.source import (
    SourceConfigStore,
    SourceDisabledError,
    SourceDirectoryConflictError,
    SourceExistsError,
    SourceHealthCheckError,
    SourceHealthStatus,
    SourceNotFoundError,
    SourceService,
)
from raggd.source.models import SourceHealthSnapshot, WorkspaceSourceConfig


class StubHealthEvaluator:
    """Test double for deterministic health evaluations."""

    def __init__(self) -> None:
        self.status = SourceHealthStatus.OK

    def __call__(
        self,
        *,
        config,
        manifest,
    ) -> SourceHealthSnapshot:
        return SourceHealthSnapshot(status=self.status)


class RecordingDbLifecycleService:
    """Record ``ensure`` calls while optionally delegating to a real service."""

    def __init__(
        self,
        delegate: DbLifecycleService | None = None,
        *,
        workspace: WorkspacePaths | None = None,
    ) -> None:
        self._delegate = delegate
        self._workspace = workspace
        self.calls: list[str] = []

    def ensure(self, source: str) -> Path:
        self.calls.append(source)
        if self._delegate is not None:
            return self._delegate.ensure(source)
        if self._workspace is None:
            dummy = Path(f"{source}.sqlite3")
            dummy.parent.mkdir(parents=True, exist_ok=True)
            dummy.touch(exist_ok=True)
            return dummy
        path = self._workspace.source_database_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        return path


class FailingDbLifecycleService:
    """Raise an error after recording ``ensure`` invocations."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._exc = exc or RuntimeError("db ensure failed")

    def ensure(self, source: str) -> Path:
        self.calls.append(source)
        raise self._exc


def _make_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def _write_migration(
    directory: Path,
    identifier,
    *,
    up: str,
    down: str | None = None,
) -> None:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(f"-- uuid7: {identifier}\n{up}\n", encoding="utf-8")
    if down is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(
            f"-- uuid7: {identifier}\n{down}\n",
            encoding="utf-8",
        )


def _prepare_db_settings(tmp_path: Path) -> DbModuleSettings:
    migrations_dir = (tmp_path / "migrations").resolve()
    migrations_dir.mkdir(exist_ok=True)

    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    _write_migration(
        migrations_dir,
        bootstrap_uuid,
        up="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        migrations_dir,
        next_uuid,
        up="ALTER TABLE example ADD COLUMN name TEXT;",
        down="ALTER TABLE example DROP COLUMN name;",
    )

    return DbModuleSettings(migrations_path=str(migrations_dir))


def _make_service(
    tmp_path: Path,
    health: StubHealthEvaluator,
    *,
    workspace_paths: WorkspacePaths | None = None,
    manifest_service: ManifestService | None = None,
    db_service: DbLifecycleService | None = None,
    logger=None,
) -> tuple[SourceService, WorkspacePaths]:
    if workspace_paths is None:
        workspace = tmp_path / "workspace"
        init_workspace(workspace=workspace)
        paths = _make_paths(workspace)
    else:
        init_workspace(workspace=workspace_paths.workspace)
        paths = workspace_paths
    store = SourceConfigStore(config_path=paths.config_file)
    fixed_now = datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)
    if db_service is None:
        db_service = RecordingDbLifecycleService(workspace=paths)
    service = SourceService(
        workspace=paths,
        config_store=store,
        manifest_service=manifest_service,
        db_service=db_service,
        health_evaluator=health,
        now=lambda: fixed_now,
        logger=logger,
    )
    return service, paths


def _load_source_module(
    paths: WorkspacePaths,
    name: str,
) -> dict[str, object]:
    manifest_service = ManifestService(workspace=paths)
    snapshot = manifest_service.load(name, apply_migrations=True)
    modules = snapshot.module("source")
    assert modules is not None
    return modules


def test_source_service_rejects_conflicting_manifest_args(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    store = SourceConfigStore(config_path=paths.config_file)
    manifest_service = ManifestService(workspace=paths)

    with pytest.raises(ValueError):
        SourceService(
            workspace=paths,
            config_store=store,
            manifest_service=manifest_service,
            manifest_settings=ManifestSettings(),
        )


def test_source_service_uses_config_manifest_settings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    config_path = workspace / "raggd.toml"

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    db_table = tomlkit.table()
    db_table["manifest_modules_key"] = "custom_mods"
    db_table["manifest_db_module_key"] = "custom_db"
    db_table["manifest_lock_suffix"] = ".lock.custom"
    db_table["manifest_backups_enabled"] = False
    document["db"] = db_table
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    health = StubHealthEvaluator()
    paths = _make_paths(workspace)
    service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
    )

    assert service._modules_key == "custom_mods"
    assert service._db_module_key == "custom_db"
    manifest_settings = service._manifest.settings
    assert manifest_settings.modules_key == "custom_mods"
    assert manifest_settings.db_module_key == "custom_db"
    assert manifest_settings.lock_suffix == ".lock.custom"
    assert manifest_settings.backups_enabled is False


def test_init_creates_source_without_target(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    state = service.init("Demo")

    assert state.config.name == "demo"
    assert state.config.enabled is False
    manifest_path = paths.source_manifest_path("demo")
    assert manifest_path.exists()
    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False
    assert source_module["target"] is None
    assert source_module["last_refresh_at"] is None
    assert snapshot.data["modules_version"] == MODULES_VERSION


def test_init_invokes_db_lifecycle(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)
    db_settings = _prepare_db_settings(tmp_path)
    delegate = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest,
        db_settings=db_settings,
    )
    recorder = RecordingDbLifecycleService(delegate=delegate, workspace=paths)

    service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
        manifest_service=manifest,
        db_service=recorder,
    )

    service.init("demo")

    assert recorder.calls == ["demo"]


def test_init_with_target_enables_and_refreshes(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)

    state = service.init("demo", target=target_dir)

    assert state.config.enabled is True
    assert state.config.target == target_dir
    assert state.manifest.last_refresh_at == datetime(
        2025,
        10,
        5,
        12,
        0,
        tzinfo=timezone.utc,
    )
    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is True
    assert source_module["target"] == str(target_dir)


def test_refresh_invokes_db_lifecycle(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)
    db_settings = _prepare_db_settings(tmp_path)
    delegate = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest,
        db_settings=db_settings,
    )
    recorder = RecordingDbLifecycleService(delegate=delegate, workspace=paths)

    service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
        manifest_service=manifest,
        db_service=recorder,
    )

    service.init("demo")
    recorder.calls.clear()

    service.refresh("demo", force=True)

    assert recorder.calls == ["demo"]


def test_set_target_invokes_db_lifecycle(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)
    db_settings = _prepare_db_settings(tmp_path)
    delegate = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest,
        db_settings=db_settings,
    )
    recorder = RecordingDbLifecycleService(delegate=delegate, workspace=paths)

    service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
        manifest_service=manifest,
        db_service=recorder,
    )

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)

    service.init("demo")
    service.enable("demo")
    recorder.calls.clear()

    service.set_target("demo", target_dir)

    assert recorder.calls == ["demo"]


def test_refresh_migrates_legacy_manifest(
    manifest_workspace: WorkspacePaths,
    manifest_service: ManifestService,
    seed_manifest,
    legacy_manifest_payload,
) -> None:
    init_workspace(workspace=manifest_workspace.workspace)
    seed_manifest("legacy", legacy_manifest_payload)
    store = SourceConfigStore(config_path=manifest_workspace.config_file)
    store.upsert(
        WorkspaceSourceConfig(
            name="legacy",
            path=manifest_workspace.source_dir("legacy"),
            enabled=True,
            target=None,
        )
    )
    health = StubHealthEvaluator()
    db_settings = _prepare_db_settings(manifest_workspace.workspace)
    delegate = DbLifecycleService(
        workspace=manifest_workspace,
        manifest_service=manifest_service,
        db_settings=db_settings,
    )
    recorder = RecordingDbLifecycleService(
        delegate=delegate,
        workspace=manifest_workspace,
    )
    fixed_now = datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)
    service = SourceService(
        workspace=manifest_workspace,
        config_store=store,
        manifest_service=manifest_service,
        db_service=recorder,
        health_evaluator=health,
        now=lambda: fixed_now,
    )

    state = service.refresh("legacy", force=True)

    assert recorder.calls == ["legacy"]
    assert state.manifest.last_refresh_at == fixed_now
    snapshot = manifest_service.load("legacy", apply_migrations=True)
    assert snapshot.data["modules_version"] == MODULES_VERSION
    modules = snapshot.data[manifest_service.settings.modules_key]
    assert isinstance(modules, dict)
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["name"] == "legacy"
    assert source_module["path"] == str(manifest_workspace.source_dir("legacy"))
    assert source_module["last_health"]["status"] == "ok"
    db_module = snapshot.module(manifest_service.settings.db_module_key)
    assert db_module is not None
    assert set(db_module) >= {
        "bootstrap_shortuuid7",
        "head_migration_uuid7",
        "head_migration_shortuuid7",
        "ledger_checksum",
        "last_vacuum_at",
        "last_ensure_at",
        "pending_migrations",
    }
    for legacy_field in (
        "name",
        "path",
        "enabled",
        "target",
        "last_refresh_at",
        "last_health",
    ):
        assert legacy_field not in snapshot.data


def test_refresh_rolls_back_on_db_failure(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)
    db_settings = _prepare_db_settings(tmp_path)
    delegate = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest,
        db_settings=db_settings,
    )
    service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
        manifest_service=manifest,
        db_service=delegate,
    )
    service.init("demo")
    baseline_snapshot = manifest.load("demo", apply_migrations=True)
    baseline_data = copy.deepcopy(baseline_snapshot.data)
    failing = FailingDbLifecycleService()
    failing_service, _ = _make_service(
        tmp_path,
        health,
        workspace_paths=paths,
        manifest_service=manifest,
        db_service=failing,
    )

    with pytest.raises(RuntimeError, match="db ensure failed"):
        failing_service.refresh("demo", force=True)

    assert failing.calls == ["demo"]
    updated_snapshot = manifest.load("demo", apply_migrations=True)
    assert updated_snapshot.data == baseline_data


def test_init_rejects_duplicate_name(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    service.init("demo")

    with pytest.raises(SourceExistsError):
        service.init("demo")


def test_init_detects_directory_conflict(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    orphan_dir = paths.source_dir("demo")
    orphan_dir.mkdir()

    with pytest.raises(SourceDirectoryConflictError):
        service.init("demo")


def test_set_target_requires_enabled_or_force(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("demo")

    target_dir = paths.workspace / "sources-data"
    target_dir.mkdir(parents=True)

    with pytest.raises(SourceDisabledError):
        service.set_target("demo", target_dir)

    service.enable("demo")
    result = service.set_target("demo", target_dir)

    assert result.config.target == target_dir
    assert result.manifest.target == target_dir
    assert result.manifest.last_refresh_at == datetime(
        2025,
        10,
        5,
        12,
        0,
        tzinfo=timezone.utc,
    )


def test_set_target_can_clear_target(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    cleared = service.set_target("demo", None)

    assert cleared.config.target is None
    assert cleared.manifest.target is None


def test_refresh_disables_source_on_failed_health(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.DEGRADED
    with pytest.raises(SourceHealthCheckError):
        service.refresh("demo")

    config = service.list()[0].config
    assert config.enabled is False
    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False
    assert source_module["last_health"]["status"] == "degraded"

    # Forced refresh proceeds even when disabled/unhealthy.
    state = service.refresh("demo", force=True)
    assert state.manifest.last_refresh_at == datetime(
        2025,
        10,
        5,
        12,
        0,
        tzinfo=timezone.utc,
    )


def test_set_target_blocks_when_health_fails_without_force(
    tmp_path: Path,
) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    replacement = paths.workspace / "data" / "replacement"
    replacement.mkdir(parents=True)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.set_target("demo", replacement)

    [state] = service.list()
    assert state.config.enabled is False
    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False
    assert source_module["last_health"]["status"] == "error"


def test_refresh_logs_auto_disable_event(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    with capture_logs() as logs:
        service, paths = _make_service(
            tmp_path,
            health,
            logger=get_logger(__name__),
        )

        target_dir = paths.workspace / "data" / "demo"
        target_dir.mkdir(parents=True)
        service.init("demo", target=target_dir)

        health.status = SourceHealthStatus.ERROR

        with pytest.raises(SourceHealthCheckError):
            service.refresh("demo")

    events = [
        entry for entry in logs if entry.get("event") == "source-auto-disabled"
    ]
    assert len(events) == 1
    payload = events[0]
    assert payload["source"] == "demo"
    assert payload["status"] == "error"


def test_set_target_force_allows_remediation_after_health_failure(
    tmp_path: Path,
) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    replacement = paths.workspace / "data" / "replacement"
    replacement.mkdir(parents=True)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.refresh("demo")

    health.status = SourceHealthStatus.OK

    state = service.set_target("demo", replacement, force=True)

    assert state.config.target == replacement
    assert state.manifest.target == replacement
    assert state.manifest.last_refresh_at == datetime(
        2025,
        10,
        5,
        12,
        0,
        tzinfo=timezone.utc,
    )
    assert state.manifest.last_health.status == SourceHealthStatus.OK
    assert state.config.enabled is False


def test_rename_updates_configuration_and_filesystem(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    service.rename("demo", "renamed", force=True)

    names = [state.config.name for state in service.list()]
    assert names == ["renamed"]
    assert (paths.sources_dir / "renamed").exists()
    assert not (paths.sources_dir / "demo").exists()


def test_rename_same_name_is_noop(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)
    service.init("demo")
    service.enable("demo")

    state = service.rename("demo", "demo")

    assert state.config.name == "demo"


def test_rename_missing_directory_raises(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    shutil.rmtree(paths.source_dir("demo"))

    with pytest.raises(SourceDirectoryConflictError):
        service.rename("demo", "renamed", force=True)


def test_rename_target_directory_conflict(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    orphan_dir = paths.source_dir("renamed")
    orphan_dir.mkdir()

    with pytest.raises(SourceDirectoryConflictError):
        service.rename("demo", "renamed", force=True)


def test_rename_rejects_existing_name(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)
    service.init("demo")
    service.init("second")
    service.enable("demo")

    with pytest.raises(SourceExistsError):
        service.rename("demo", "second")


def test_remove_prunes_config_and_directory(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("demo")
    service.enable("demo")

    directory = paths.source_dir("demo")
    assert directory.exists()

    service.remove("demo", force=True)

    assert directory.exists() is False
    assert service.list() == []


def test_rename_blocks_when_health_fails_without_force(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.rename("demo", "renamed")

    [state] = service.list()
    assert state.config.enabled is False
    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False
    assert source_module["last_health"]["status"] == "error"


def test_remove_requires_force_when_health_fails(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.remove("demo")

    snapshot = ManifestService(workspace=paths).load(
        "demo",
        apply_migrations=True,
    )
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False
    assert source_module["last_health"]["status"] == "error"
    assert (paths.sources_dir / "demo").exists()


def test_remove_blocks_when_disabled(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    service.init("demo")

    with pytest.raises(SourceDisabledError):
        service.remove("demo")


def test_enable_and_disable_update_state(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("alpha")
    service.init("bravo")

    enabled_states = service.enable("alpha", "bravo")
    assert [state.config.enabled for state in enabled_states] == [True, True]
    manifest_snapshot = ManifestService(workspace=paths).load(
        "alpha",
        apply_migrations=True,
    )
    source_module = manifest_snapshot.module("source")
    assert source_module is not None
    assert source_module["last_health"]["status"] == "ok"

    disabled_states = service.disable("alpha")
    assert disabled_states[0].config.enabled is False
    manifest_snapshot = ManifestService(workspace=paths).load(
        "alpha",
        apply_migrations=True,
    )
    source_module = manifest_snapshot.module("source")
    assert source_module is not None
    assert source_module["enabled"] is False


def test_refresh_requires_existing_directory(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    shutil.rmtree(paths.source_dir("demo"))

    with pytest.raises(SourceDirectoryConflictError):
        service.refresh("demo", force=True)


def test_enable_requires_existing_source(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.enable("missing")


def test_enable_requires_names(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.enable()


def test_default_health_evaluator_reports_degraded_when_target_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    store = SourceConfigStore(config_path=paths.config_file)
    db_service = RecordingDbLifecycleService(workspace=paths)
    service = SourceService(
        workspace=paths,
        config_store=store,
        db_service=db_service,
    )

    service.init("demo")
    [state] = service.enable("demo")

    assert state.manifest.last_health.status == SourceHealthStatus.DEGRADED

    refreshed = service.refresh("demo", force=True)
    assert refreshed.manifest.last_refresh_at is not None


def test_refresh_missing_source_raises(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.refresh("missing", force=True)
