"""Tests for the database module health hook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sqlite3

import pytest

import raggd.modules.db.health as db_health
from raggd.core.config import (
    AppConfig,
    DbSettings,
    ModuleToggle,
    WorkspaceSettings,
)
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.modules.db import (
    DbLifecycleService,
    db_health_hook,
    db_settings_from_mapping,
)
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.db.migrations import MigrationRunner
from raggd.modules.db.models import DbManifestState
from raggd.modules.db.settings import DbModuleSettings
from raggd.modules.manifest import (
    ManifestError,
    ManifestService,
    ManifestSettings,
    manifest_settings_from_mapping,
)
from raggd.source.models import WorkspaceSourceConfig


def _make_paths(root: Path) -> WorkspacePaths:
    workspace = root / "workspace"
    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )
    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.archives_dir.mkdir(parents=True, exist_ok=True)
    paths.sources_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _make_config(
    paths: WorkspacePaths,
    *,
    include_source: bool = True,
    db_enabled: bool = True,
    db_overrides: dict[str, object] | None = None,
) -> AppConfig:
    sources: dict[str, WorkspaceSourceConfig] = {}
    if include_source:
        source_dir = paths.source_dir("alpha")
        source_dir.mkdir(parents=True, exist_ok=True)
        target = paths.workspace / "data"
        target.mkdir(parents=True, exist_ok=True)
        sources["alpha"] = WorkspaceSourceConfig(
            name="alpha",
            path=source_dir,
            enabled=True,
            target=target,
        )

    modules = {
        "source": ModuleToggle(enabled=True),
        "db": ModuleToggle(enabled=db_enabled, extras=("db",)),
    }

    settings = WorkspaceSettings(root=paths.workspace, sources=sources)
    db_settings = DbSettings(**(db_overrides or {}))
    return AppConfig(
        workspace_settings=settings,
        modules=modules,
        db=db_settings,
    )


def _write_migration(
    directory: Path,
    *,
    identifier,
    up: str,
    down: str | None = None,
) -> str:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(
        f"-- uuid7: {identifier}\n{up}\n",
        encoding="utf-8",
    )
    if down is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(
            f"-- uuid7: {identifier}\n{down}\n",
            encoding="utf-8",
        )
    return short


def test_db_health_hook_reports_disabled_module(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths, include_source=False, db_enabled=False)
    handle = SimpleNamespace(paths=paths, config=config)

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.UNKNOWN
    assert "disabled" in (report.summary or "")


def test_db_health_hook_reports_migration_loader_error(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(
        paths,
        include_source=False,
        db_overrides={"migrations_path": str(tmp_path / "missing")},
    )
    handle = SimpleNamespace(paths=paths, config=config)

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "Failed to load migrations" in (report.summary or "")


def test_db_health_hook_reports_healthy_state(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.OK
    assert report.summary == "database healthy"
    assert report.actions == ()


def test_db_health_hook_flags_missing_database(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    db_path = paths.source_database_path("alpha")
    db_path.unlink()

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.ERROR
    assert "missing" in (report.summary or "")


def test_db_health_hook_detects_pending_and_drift(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    bootstrap_short = _write_migration(
        migrations_dir,
        identifier=bootstrap_uuid,
        up="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        migrations_dir,
        identifier=next_uuid,
        up="ALTER TABLE example ADD COLUMN name TEXT;",
        down="ALTER TABLE example DROP COLUMN name;",
    )

    config = _make_config(
        paths,
        db_overrides={
            "migrations_path": migrations_dir.as_posix(),
            "drift_warning_seconds": 0,
        },
    )
    handle = SimpleNamespace(paths=paths, config=config)

    payload = config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_mapping(payload)
    module_settings = db_settings_from_mapping(payload)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=manifest_settings,
        db_settings=module_settings,
    )
    service.ensure("alpha")
    service.vacuum("alpha")

    manifest_service = ManifestService(
        workspace=paths,
        settings=manifest_settings,
    )

    pending_uuid = generate_uuid7(
        when=datetime(2024, 1, 3, tzinfo=timezone.utc)
    )
    pending_short = _write_migration(
        migrations_dir,
        identifier=pending_uuid,
        up="ALTER TABLE example ADD COLUMN extra TEXT;",
        down="ALTER TABLE example DROP COLUMN extra;",
    )

    with manifest_service.with_transaction("alpha", backup=False) as txn:
        module = txn.snapshot.ensure_module(
            manifest_service.settings.db_module_key
        )
        module["head_migration_shortuuid7"] = bootstrap_short
        module["head_migration_uuid7"] = "00000000-0000-0000-0000-000000000000"
        module["pending_migrations"] = []

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.DEGRADED
    summary = report.summary or ""
    assert pending_short in summary or "pending migrations" in summary
    assert "manifest drift" in summary
    assert any(
        "ensure" in action or "upgrade" in action for action in report.actions
    )


def test_db_health_hook_detects_stale_vacuum(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")
    service.vacuum("alpha")

    db_path = paths.source_database_path("alpha")
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_meta SET last_vacuum_at = ? WHERE id = 1",
            (stale.isoformat(),),
        )

    reports = db_health_hook(handle)

    assert len(reports) == 1
    report = reports[0]
    assert report.status is HealthStatus.DEGRADED
    assert "vacuum" in (report.summary or "")
    assert any("vacuum" in action for action in report.actions)


def test_db_health_parse_iso_variants() -> None:
    assert db_health._parse_iso(None) is None
    assert db_health._parse_iso("") is None

    naive = datetime(2025, 1, 1, 0, 0)
    parsed_naive = db_health._parse_iso(naive)
    assert parsed_naive.tzinfo is timezone.utc

    parsed_str = db_health._parse_iso("2025-01-01T00:00:00")
    assert parsed_str.tzinfo is timezone.utc

    with pytest.raises(TypeError):
        db_health._parse_iso(123)


def test_db_health_compute_checksum_unknown_migration(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    first = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    _write_migration(migrations_dir, identifier=first, up="SELECT 1;")

    runner = MigrationRunner.from_path(migrations_dir)

    with pytest.raises(db_health._DbInspectionError):
        db_health._compute_ledger_checksum(("missing",), runner=runner)


def test_db_health_within_drift_window_behaviour() -> None:
    state = DbManifestState(
        last_ensure_at=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    )
    now = state.last_ensure_at + timedelta(seconds=30)

    assert db_health._within_drift_window(state, now=now, threshold_seconds=60)
    assert not db_health._within_drift_window(
        state, now=now, threshold_seconds=0
    )

    assert not db_health._within_drift_window(
        DbManifestState(),
        now=datetime.now(timezone.utc),
        threshold_seconds=60,
    )


def test_db_health_load_manifest_state_error() -> None:
    class FailingManifest:
        def __init__(self) -> None:
            self.settings = ManifestSettings()

        def load(self, _: str) -> None:
            raise ManifestError("boom")

    state, report = db_health._load_manifest_state(
        name="alpha",
        manifest_service=FailingManifest(),
        manifest_actions=("retry",),
    )

    assert state is None
    assert report is not None
    assert report.status is HealthStatus.ERROR


def test_db_health_load_manifest_state_missing_module() -> None:
    class Snapshot:
        def __init__(self) -> None:
            self.settings = ManifestSettings()

        def load(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(
                module=lambda __: None,
                data={},
            )

    state, report = db_health._load_manifest_state(
        name="alpha",
        manifest_service=Snapshot(),
        manifest_actions=("ensure",),
    )

    assert state is None
    assert report is not None
    assert "missing" in (report.summary or "")


def test_db_health_observe_state_missing_database(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    config = _make_config(paths)
    handle = SimpleNamespace(paths=paths, config=config)

    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")

    db_path = paths.source_database_path("alpha")
    db_path.unlink()

    manifest_state = DbManifestState()
    runner = db_health._load_runner(DbModuleSettings())

    observed, report = db_health._observe_state(
        name="alpha",
        handle=handle,
        manifest_state=manifest_state,
        runner=runner,
        manifest_actions=("ensure",),
    )

    assert observed is None
    assert report is not None
    assert report.status is HealthStatus.ERROR


def test_db_health_observe_state_defaults_to_manifest_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = SimpleNamespace(
        paths=SimpleNamespace(
            source_database_path=lambda name: Path.cwd() / f"{name}.sqlite3",
        )
    )
    manifest_state = DbManifestState()
    runner = SimpleNamespace()

    def _raise(_: Path, *, runner: object) -> None:
        raise db_health._DbInspectionError("boom")

    monkeypatch.setattr(db_health, "_inspect_database", _raise)

    observed, report = db_health._observe_state(
        name="alpha",
        handle=handle,
        manifest_state=manifest_state,
        runner=runner,
        manifest_actions=("ensure", "check"),
    )

    assert observed is None
    assert report is not None
    assert report.actions == ("ensure", "check")


def test_db_health_assess_pending_migrations(tmp_path: Path) -> None:
    observed = db_health._ObservedState(
        bootstrap_shortuuid7="boot",
        head_migration_uuid7="uuid",
        head_migration_shortuuid7="head",
        ledger_checksum="sha256:1",
        pending_migrations=("001",),
        applied_migrations=("000",),
        last_vacuum_at=None,
    )
    manifest_state = DbManifestState(pending_migrations=())
    settings = DbModuleSettings(drift_warning_seconds=0)
    issues: list[str] = []
    actions: set[str] = set()

    severity = db_health._assess_pending_migrations(
        name="alpha",
        manifest_state=manifest_state,
        observed=observed,
        db_settings=settings,
        now=datetime.now(timezone.utc),
        issues=issues,
        actions=actions,
    )

    assert severity is HealthStatus.DEGRADED
    assert any("upgrade" in action for action in actions)


def test_db_health_assess_manifest_sync_drift() -> None:
    observed = db_health._ObservedState(
        bootstrap_shortuuid7="boot",
        head_migration_uuid7="uuid",
        head_migration_shortuuid7="head",
        ledger_checksum="sha256:1",
        pending_migrations=(),
        applied_migrations=(),
        last_vacuum_at=None,
    )
    manifest_state = DbManifestState(
        bootstrap_shortuuid7="other",
        head_migration_uuid7="other",
        head_migration_shortuuid7="other",
        ledger_checksum="sha256:2",
        last_ensure_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    issues: list[str] = []
    actions: set[str] = set()

    severity = db_health._assess_manifest_sync(
        name="alpha",
        manifest_state=manifest_state,
        observed=observed,
        db_settings=DbModuleSettings(drift_warning_seconds=0),
        now=datetime.now(timezone.utc),
        issues=issues,
        actions=actions,
    )

    assert severity is HealthStatus.DEGRADED
    assert any("ensure" in action for action in actions)

    severity_with_window = db_health._assess_manifest_sync(
        name="alpha",
        manifest_state=manifest_state.replace(
            last_ensure_at=datetime.now(timezone.utc)
        ),
        observed=observed,
        db_settings=DbModuleSettings(drift_warning_seconds=3600),
        now=datetime.now(timezone.utc),
        issues=[],
        actions=set(),
    )
    assert severity_with_window is None


def test_db_health_assess_vacuum_status_branches() -> None:
    observed = db_health._ObservedState(
        bootstrap_shortuuid7="boot",
        head_migration_uuid7="uuid",
        head_migration_shortuuid7="head",
        ledger_checksum="sha256:1",
        pending_migrations=(),
        applied_migrations=(),
        last_vacuum_at=None,
    )
    actions: set[str] = set()
    issues: list[str] = []

    assert (
        db_health._assess_vacuum_status(
            name="alpha",
            observed=observed,
            db_settings=DbModuleSettings(vacuum_max_stale_days=-1),
            now=datetime.now(timezone.utc),
            issues=issues,
            actions=actions,
        )
        is None
    )

    actions.clear()
    issues.clear()
    severity = db_health._assess_vacuum_status(
        name="alpha",
        observed=observed,
        db_settings=DbModuleSettings(),
        now=datetime.now(timezone.utc),
        issues=issues,
        actions=actions,
    )
    assert severity is HealthStatus.DEGRADED
    assert any("vacuum" in issue for issue in issues)

    recent = db_health._ObservedState(
        bootstrap_shortuuid7="boot",
        head_migration_uuid7="uuid",
        head_migration_shortuuid7="head",
        ledger_checksum="sha256:1",
        pending_migrations=(),
        applied_migrations=(),
        last_vacuum_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert (
        db_health._assess_vacuum_status(
            name="alpha",
            observed=recent,
            db_settings=DbModuleSettings(),
            now=datetime.now(timezone.utc),
            issues=[],
            actions=set(),
        )
        is None
    )

    stale = db_health._ObservedState(
        bootstrap_shortuuid7="boot",
        head_migration_uuid7="uuid",
        head_migration_shortuuid7="head",
        ledger_checksum="sha256:1",
        pending_migrations=(),
        applied_migrations=(),
        last_vacuum_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    severity_stale = db_health._assess_vacuum_status(
        name="alpha",
        observed=stale,
        db_settings=DbModuleSettings(),
        now=datetime.now(timezone.utc),
        issues=[],
        actions=set(),
    )
    assert severity_stale is HealthStatus.DEGRADED


def test_db_health_inspect_database_metadata_missing(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")

    db_path = paths.source_database_path("alpha")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM schema_meta")

    runner = db_health._load_runner(DbModuleSettings())

    with pytest.raises(db_health._DbInspectionError):
        db_health._inspect_database(db_path, runner=runner)


def test_db_health_inspect_database_schema_table_missing(
    tmp_path: Path,
) -> None:
    paths = _make_paths(tmp_path)
    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")

    db_path = paths.source_database_path("alpha")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE schema_meta")

    runner = db_health._load_runner(DbModuleSettings())

    with pytest.raises(db_health._DbInspectionError):
        db_health._inspect_database(db_path, runner=runner)


def test_db_health_inspect_database_checksum_mismatch(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    service = DbLifecycleService(workspace=paths)
    service.ensure("alpha")

    db_path = paths.source_database_path("alpha")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_meta SET ledger_checksum = 'sha256:bad' WHERE id = 1"
        )

    runner = db_health._load_runner(DbModuleSettings())

    with pytest.raises(db_health._DbInspectionError):
        db_health._inspect_database(db_path, runner=runner)
