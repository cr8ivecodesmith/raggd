from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

from raggd.core.paths import WorkspacePaths
from raggd.modules.manifest import (
    ManifestError,
    ManifestMigrator,
    ManifestReadError,
    ManifestService,
    ManifestSettings,
    ManifestTransactionError,
    ManifestWriteError,
    manifest_db_namespace,
    manifest_settings_from_config,
    manifest_settings_from_mapping,
)
from raggd.modules.manifest.backups import (
    ManifestBackupError,
    prune_backups,
)
from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)


def _build_workspace(tmp_path: Path) -> WorkspacePaths:
    root = tmp_path / "workspace"
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def test_snapshot_module_missing_returns_none(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    snapshot = service.load("missing")
    assert snapshot.module("db") is None


def test_load_returns_empty_snapshot_when_manifest_missing(
    tmp_path: Path,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("example")
    snapshot = service.load(ref)
    assert snapshot.data == {}
    assert snapshot.modules_key == "modules"
    assert snapshot.db_module_key == "db"


def test_write_persists_changes(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("alpha")

    result = service.write(
        ref,
        mutate=lambda snap: snap.ensure_module("source").update(
            {"enabled": True}
        ),
    )

    assert result.module("source") == {"enabled": True}
    payload = json.loads(ref.manifest_path.read_text(encoding="utf-8"))
    assert payload["modules"]["source"]["enabled"] is True


def test_write_creates_backup_on_subsequent_updates(
    tmp_path: Path,
) -> None:
    settings = ManifestSettings(backup_retention=3)
    service = ManifestService(
        workspace=_build_workspace(tmp_path),
        settings=settings,
    )
    ref = service.resolve("beta")

    service.write(ref, mutate=lambda snap: snap.ensure_module("source"))
    backups_before = list(ref.manifest_path.parent.glob("manifest.json.*.bak"))
    assert backups_before == []

    service.write(
        ref,
        mutate=lambda snap: snap.ensure_module("source").update(
            {"enabled": False}
        ),
    )

    backups_after = list(ref.manifest_path.parent.glob("manifest.json.*.bak"))
    assert len(backups_after) == 1


def test_transaction_runs_commit_callbacks(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("gamma")
    committed: list[dict[str, bool]] = []

    def _on_commit(snapshot):
        committed.append(snapshot.ensure_module("source"))

    with service.with_transaction(ref) as txn:
        txn.on_commit(_on_commit)
        txn.snapshot.ensure_module("source")["enabled"] = True

    assert committed == [{"enabled": True}]
    assert txn.result is not None
    assert txn.result.module("source") == {"enabled": True}


def test_transaction_rolls_back_on_persist_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("delta")
    attempts: list[str] = []

    def _failing_persist(*args, **kwargs):
        attempts.append("attempt")
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_persist", _failing_persist)

    with pytest.raises(ManifestTransactionError):
        with service.with_transaction(ref) as txn:
            txn.on_rollback(lambda snap: attempts.append("rollback"))
            txn.snapshot.ensure_module("source")["enabled"] = False

    assert attempts == ["attempt", "rollback"]


def test_manifest_settings_from_mapping_defaults() -> None:
    settings = manifest_settings_from_mapping({})
    assert settings.modules_key == "modules"
    assert settings.db_module_key == "db"
    assert settings.backup_retention == 5

    overrides = manifest_settings_from_mapping(
        {
            "db": {
                "manifest_modules_key": "mods",
                "manifest_db_module_key": "database",
                "manifest_backup_retention": 1,
                "manifest_lock_timeout": 1.5,
                "manifest_backups_enabled": False,
            }
        }
    )
    assert overrides.modules_key == "mods"
    assert overrides.db_module_key == "database"
    assert overrides.backup_retention == 1
    assert overrides.lock_timeout == 1.5
    assert overrides.backups_enabled is False


def test_manifest_settings_overrides_take_precedence() -> None:
    settings = manifest_settings_from_mapping(
        {"db": {"manifest_modules_key": "ignored"}},
        overrides={"manifest_modules_key": "preferred"},
    )
    assert settings.modules_key == "preferred"


def test_manifest_settings_module_key_helper() -> None:
    settings = ManifestSettings(modules_key="mods")
    assert settings.module_key("db") == ("mods", "db")


def test_manifest_db_namespace_defaults() -> None:
    modules_key, db_key = manifest_db_namespace()
    assert modules_key == "modules"
    assert db_key == "db"


def test_manifest_settings_from_config_overrides() -> None:
    config = {"db": {"manifest_modules_key": "mods"}}
    settings = manifest_settings_from_config(
        config,
        overrides={"manifest_db_module_key": "database"},
    )
    assert settings.modules_key == "mods"
    assert settings.db_module_key == "database"


def test_prune_backups_handles_retention_zero(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    prune_backups(manifest_path, retention=0)


def test_prune_backups_removes_oldest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    backups = []
    for idx in range(3):
        backup_name = f"manifest.json.2024010{idx}T000000Z.bak"
        backup = manifest_path.with_name(backup_name)
        backup.write_text(str(idx), encoding="utf-8")
        backups.append(backup)
    prune_backups(manifest_path, retention=1)
    remaining = list(manifest_path.parent.glob("manifest.json.*.bak"))
    assert remaining == [backups[-1]]


def test_filelock_behaviour(tmp_path: Path) -> None:
    lock_path = tmp_path / "lockfile.lock"
    lock = FileLock(lock_path, timeout=0.2, poll_interval=0.01)
    lock.release()  # no-op when not acquired
    lock.acquire()
    lock.acquire()  # second acquire is a no-op
    lock.release()

    with FileLock(lock_path) as guard:
        assert guard.path == lock_path


def test_filelock_timeout(tmp_path: Path) -> None:
    lock_path = tmp_path / "manifest.json.lock"
    lock_path.write_text("held", encoding="utf-8")
    lock = FileLock(lock_path, timeout=0.0, poll_interval=0.0)
    with pytest.raises(ManifestLockTimeoutError):
        lock.acquire()
    lock_path.unlink()


def test_manifest_service_load_with_migration(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("epsilon")
    ref.ensure_directories()
    ref.manifest_path.write_text('{"name": "epsilon"}', encoding="utf-8")

    backups_before = list(ref.manifest_path.parent.glob("manifest.json.*.bak"))

    snapshot = service.load(ref, apply_migrations=True)
    fresh = service.load(ref)
    assert snapshot.data["modules_version"] == 1
    assert "name" not in snapshot.data

    modules = snapshot.data["modules"]
    source_module = modules["source"]
    assert source_module["name"] == "epsilon"
    assert fresh.module("source")["name"] == "epsilon"

    db_module = modules["db"]
    assert db_module == {
        "bootstrap_shortuuid7": None,
        "head_migration_uuid7": None,
        "head_migration_shortuuid7": None,
        "ledger_checksum": None,
        "last_vacuum_at": None,
        "last_ensure_at": None,
        "pending_migrations": [],
    }

    data = json.loads(ref.manifest_path.read_text(encoding="utf-8"))
    assert data == snapshot.data

    backups_after = list(ref.manifest_path.parent.glob("manifest.json.*.bak"))
    assert len(backups_after) == len(backups_before) + 1


def test_manifest_service_load_with_migration_dry_run(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("zeta")
    ref.ensure_directories()
    ref.manifest_path.write_text('{"name": "zeta"}', encoding="utf-8")

    snapshot = service.load(ref, apply_migrations=True, dry_run=True)
    assert "modules" not in snapshot.data


def test_manifest_service_read_errors(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("eta")
    ref.ensure_directories()
    ref.manifest_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ManifestReadError):
        service.load(ref)

    ref.manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestReadError):
        service.load(ref)


def test_manifest_service_backup_failure_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("theta")
    ref.ensure_directories()
    ref.manifest_path.write_text("{}", encoding="utf-8")

    def _failing_backup(*args, **kwargs):
        raise ManifestBackupError("boom")

    monkeypatch.setattr(
        "raggd.modules.manifest.service.create_backup",
        _failing_backup,
    )

    with pytest.raises(ManifestWriteError):
        service.write(ref, mutate=lambda snap: snap.ensure_module("source"))


def test_manifest_service_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("iota")

    def _failing_named_tempfile(**kwargs):
        raise OSError("no temp")

    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        _failing_named_tempfile,
    )

    with pytest.raises(ManifestWriteError):
        service.write(ref, mutate=lambda snap: snap.ensure_module("source"))


def test_manifest_service_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("kappa")

    def _failing_replace(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
    ) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(ManifestWriteError):
        service.write(ref, mutate=lambda snap: snap.ensure_module("source"))


def test_manifest_service_lock_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("lambda")

    def _boom_acquire(self):
        raise ManifestLockError("nope")

    monkeypatch.setattr(FileLock, "acquire", _boom_acquire)

    with pytest.raises(ManifestError):
        service.write(ref)


def test_json_default_serialization(tmp_path: Path) -> None:
    class DummyModel:
        def model_dump(self, mode: str = "json") -> dict[str, str]:
            return {"mode": mode}

    class Color(Enum):
        RED = "red"

    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("mu")

    naive_dt = datetime(2024, 1, 1, 12, 0, 0)
    aware_dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def mutate(snapshot):
        module = snapshot.ensure_module("meta")
        module["path"] = Path("./example.txt")
        module["naive"] = naive_dt
        module["aware"] = aware_dt
        module["when"] = date(2024, 1, 2)
        module["tags"] = {"b", "a"}
        module["color"] = Color.RED
        module["model"] = DummyModel()
        module["other"] = object()

    service.write(ref, mutate=mutate)
    payload = json.loads(ref.manifest_path.read_text(encoding="utf-8"))
    meta = payload["modules"]["meta"]
    assert meta["path"] == "example.txt"
    assert meta["naive"].endswith("Z") or meta["naive"].endswith("+00:00")
    assert meta["aware"].endswith("Z") or meta["aware"].endswith("+00:00")
    assert meta["when"] == "2024-01-02"
    assert meta["tags"] == ["a", "b"]
    assert meta["color"] == "red"
    assert meta["model"] == {"mode": "json"}
    assert isinstance(meta["other"], str)


def test_filelock_retries_after_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "retry.lock"
    attempts: list[int] = []
    real_open = os.open

    def _flaky_open(path, flags):
        attempts.append(1)
        if len(attempts) == 1:
            raise FileExistsError
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", _flaky_open)

    lock = FileLock(lock_path, timeout=0.5, poll_interval=0.0)
    lock.acquire()
    lock.release()
    assert len(attempts) >= 2


def test_manifest_transaction_rollback_on_body_exception(
    tmp_path: Path,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("rollback")
    events: list[str] = []

    with pytest.raises(RuntimeError):
        with service.with_transaction(ref) as txn:
            txn.on_rollback(lambda snap: events.append("rolled"))
            raise RuntimeError("boom")

    assert events == ["rolled"]


def test_manifest_service_migrate_method(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("migrate")
    ref.ensure_directories()
    ref.manifest_path.write_text('{"name": "migrate"}', encoding="utf-8")

    applied = service.migrate(ref)
    assert applied is True
    applied_again = service.migrate(ref)
    assert applied_again is False


def test_manifest_service_write_with_name_coercion(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    result = service.write("coerce")
    assert result.source.name == "coerce"


def test_manifest_service_read_whitespace_returns_empty(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("whitespace")
    ref.ensure_directories()
    ref.manifest_path.write_text("   \n\t", encoding="utf-8")
    snapshot = service.load(ref)
    assert snapshot.data == {}


def test_manifest_service_read_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("readerr")
    ref.ensure_directories()
    ref.manifest_path.write_text("{}", encoding="utf-8")

    def _boom_read_text(self, encoding="utf-8"):
        raise OSError("fail")

    monkeypatch.setattr(Path, "read_text", _boom_read_text)

    with pytest.raises(ManifestReadError):
        service.load(ref)


def test_manifest_migrator_direct_application(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("mig-direct")
    migrator = ManifestMigrator(settings=ManifestSettings())
    result = migrator.migrate(source=ref, data={}, dry_run=False)
    assert result.applied is True
    assert result.data["modules_version"] == 1
    modules = result.data["modules"]
    assert modules["source"] == {}
    assert modules["db"]["pending_migrations"] == []


def test_manifest_migrator_idempotent(tmp_path: Path) -> None:
    service = ManifestService(workspace=_build_workspace(tmp_path))
    ref = service.resolve("idempotent")
    migrator = ManifestMigrator(settings=ManifestSettings())
    initial = migrator.migrate(source=ref, data={}, dry_run=False)
    assert initial.applied is True

    second = migrator.migrate(source=ref, data=initial.data, dry_run=False)
    assert second.applied is False


def test_manifest_migrator_completes_db_defaults(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    service = ManifestService(workspace=workspace)
    ref = service.resolve("delta")
    payload = {
        "modules": {
            "db": {
                "bootstrap_shortuuid7": "abc",
            }
        },
    }
    migrator = ManifestMigrator(settings=ManifestSettings())
    result = migrator.migrate(source=ref, data=payload, dry_run=False)
    assert result.applied is True
    db_module = result.data["modules"]["db"]
    for key in (
        "bootstrap_shortuuid7",
        "head_migration_uuid7",
        "head_migration_shortuuid7",
        "ledger_checksum",
        "last_vacuum_at",
        "last_ensure_at",
        "pending_migrations",
    ):
        assert key in db_module


def test_manifest_migrator_golden(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    service = ManifestService(workspace=workspace)
    ref = service.resolve("alpha")
    legacy_path = Path(__file__).parent / "data" / "legacy_manifest.json"
    expected_path = (
        Path(__file__).parent
        / "data"
        / "legacy_manifest.migrated.json"
    )

    legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    expected_payload = json.loads(
        expected_path.read_text(encoding="utf-8")
    )

    migrator = ManifestMigrator(settings=ManifestSettings())
    result = migrator.migrate(source=ref, data=legacy_payload, dry_run=True)

    assert result.applied is True
    assert result.data == expected_payload


def test_manifest_fixtures_seed_and_migrate(
    manifest_service: ManifestService,
    seed_manifest,
    legacy_manifest_payload,
) -> None:
    ref = seed_manifest("fixture", legacy_manifest_payload)
    snapshot = manifest_service.load(ref, apply_migrations=True)
    source_module = snapshot.module("source")
    assert source_module is not None
    assert source_module["name"] == "legacy"
    assert snapshot.data["modules_version"] == 1
