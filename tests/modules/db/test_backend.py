from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
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


def _write_migration(directory: Path, identifier, *, up: str, down: str | None = None) -> str:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(f"-- uuid7: {identifier}\n{up}\n", encoding="utf-8")
    if down is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(f"-- uuid7: {identifier}\n{down}\n", encoding="utf-8")
    return short


def test_backend_applies_migrations_and_updates_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    bootstrap_uuid = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
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
            "SELECT shortuuid7, direction FROM schema_migrations ORDER BY shortuuid7"
        ).fetchall()
        assert {entry["shortuuid7"]: entry["direction"] for entry in entries} == {
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
    assert modules["head_migration_shortuuid7"] == "066CEY2G01SG"
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
        }
        assert expected_tables.issubset(table_names)

        trigger_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            )
        }
        assert {"chunks_ai", "chunks_ad", "chunks_au"}.issubset(trigger_names)
