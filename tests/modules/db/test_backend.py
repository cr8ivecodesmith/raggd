from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path

import pytest

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.db.backend import _from_iso, _to_iso
from raggd.modules.db.migrations import (
    Migration,
    MigrationLoadError,
    MigrationPlan,
)
from raggd.modules.db.settings import DbModuleSettings
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7
from raggd.modules.manifest import ManifestService, ManifestSettings


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
) -> str:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(f"-- uuid7: {identifier}\n{up}\n", encoding="utf-8")
    if down is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(
            f"-- uuid7: {identifier}\n{down}\n",
            encoding="utf-8",
        )
    return short


def test_backend_applies_migrations_and_updates_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    bootstrap_short = _write_migration(
        migrations_dir,
        bootstrap_uuid,
        up="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    next_short = _write_migration(
        migrations_dir,
        next_uuid,
        up="ALTER TABLE example ADD COLUMN name TEXT;",
        down="ALTER TABLE example DROP COLUMN name;",
    )

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=ManifestSettings(),
        db_settings=DbModuleSettings(migrations_path=str(migrations_dir)),
    )

    db_path = service.ensure("alpha")
    assert db_path.exists()

    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load("alpha", apply_migrations=True)
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)

    assert modules["bootstrap_shortuuid7"] == bootstrap_short
    assert modules["head_migration_shortuuid7"] == next_short
    assert modules["pending_migrations"] == []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM schema_meta WHERE id = 1").fetchone()
        assert row["bootstrap_shortuuid7"] == bootstrap_short
        assert row["head_migration_shortuuid7"] == next_short
        entries = conn.execute(
            "SELECT shortuuid7, direction FROM schema_migrations "
            "ORDER BY shortuuid7"
        ).fetchall()
        status_by_short = {
            entry["shortuuid7"]: entry["direction"] for entry in entries
        }
        assert status_by_short == {
            bootstrap_short: "up",
            next_short: "up",
        }

    service.downgrade("alpha", steps=1)

    manifest = manifest_service.load("alpha", apply_migrations=True)
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)
    assert modules["head_migration_shortuuid7"] == bootstrap_short
    assert modules["pending_migrations"] == [next_short]

    service.upgrade("alpha", steps=None)
    manifest = manifest_service.load("alpha", apply_migrations=True)
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)
    assert modules["head_migration_shortuuid7"] == next_short
    assert modules["pending_migrations"] == []


def test_backend_uses_packaged_migrations(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=ManifestSettings(),
        db_settings=DbModuleSettings(),
    )

    db_path = service.ensure("alpha")
    assert db_path.exists()

    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load("alpha", apply_migrations=True)
    modules = manifest.ensure_module(manifest_service.settings.db_module_key)

    assert modules["bootstrap_shortuuid7"] == "066C4MFM01VQ"
    assert modules["head_migration_shortuuid7"] == "06CVG7EEZ5YH"
    assert modules["pending_migrations"] == []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        table_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')"
            )
        }
        expected_tables = {
            "schema_meta",
            "schema_migrations",
            "batches",
            "embedding_models",
            "vdbs",
            "files",
            "symbols",
            "chunks",
            "chunk_fts",
            "edges",
            "vectors",
            "sources",
            "migrations_audit",
            "chunk_slices",
        }
        assert expected_tables.issubset(table_names)

        trigger_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            )
        }
        assert {"chunks_ai", "chunks_ad", "chunks_au"}.issubset(trigger_names)


def test_backend_info_vacuum_run_reset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=ManifestSettings(),
        db_settings=DbModuleSettings(),
    )

    db_path = service.ensure("alpha")
    assert db_path.exists()

    info = service.info("alpha", include_schema=True)
    assert info["source"] == "alpha"
    assert "schema_meta" in info["schema"]
    metadata = info["metadata"]
    assert metadata["applied_migrations"]

    service.vacuum("alpha", concurrency=2)
    manifest_service = ManifestService(workspace=paths)
    manifest = manifest_service.load("alpha", apply_migrations=True)
    module_payload = manifest.ensure_module(
        manifest_service.settings.db_module_key
    )
    assert module_payload["last_vacuum_at"] is not None

    sql_path = tmp_path / "manual.sql"
    sql_path.write_text(
        "CREATE TABLE IF NOT EXISTS manual_runs(id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    service.run("alpha", sql_path=sql_path, autocommit=False)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'manual_runs'"
            )
        }
        assert "manual_runs" in tables

    service.reset("alpha", force=True)
    assert db_path.exists()

    manifest_after = manifest_service.load("alpha", apply_migrations=True)
    module_after = manifest_after.ensure_module(
        manifest_service.settings.db_module_key
    )
    assert module_after["pending_migrations"] == []


def test_backend_apply_upgrades_empty_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=ManifestSettings(),
        db_settings=DbModuleSettings(),
    )
    db_path = service.ensure("alpha")
    backend = service._backend  # type: ignore[attr-defined]

    with sqlite3.connect(db_path) as conn:
        plan = MigrationPlan(())
        result = backend._apply_upgrades(  # type: ignore[attr-defined]
            conn,
            plan,
            datetime.now(timezone.utc),
        )

    assert result == []


def test_backend_apply_downgrades_missing_script(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    service = DbLifecycleService(
        workspace=paths,
        manifest_settings=ManifestSettings(),
        db_settings=DbModuleSettings(),
    )
    db_path = service.ensure("alpha")
    backend = service._backend  # type: ignore[attr-defined]

    identifier = generate_uuid7(when=datetime(2024, 2, 1, tzinfo=timezone.utc))
    short = short_uuid7(identifier)
    migration = Migration(
        uuid=identifier,
        short=short,
        up_sql="SELECT 1;",
        down_sql=None,
        checksum_up="sha256:deadbeef",
        checksum_down=None,
    )
    plan = MigrationPlan((migration,))

    with sqlite3.connect(db_path) as conn, pytest.raises(MigrationLoadError):
        backend._apply_downgrades(  # type: ignore[attr-defined]
            conn,
            plan,
            datetime.now(timezone.utc),
        )


def test_backend_iso_helpers_handle_naive_values() -> None:
    assert _to_iso(None) is None

    naive = datetime(2025, 1, 1, 12, 0)
    encoded = _to_iso(naive)
    assert encoded.endswith("+00:00")

    parsed = _from_iso("2025-01-01T12:00:00")
    assert parsed.tzinfo is timezone.utc
    assert parsed.hour == 12
