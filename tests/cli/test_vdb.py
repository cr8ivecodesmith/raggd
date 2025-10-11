"""Tests covering the VDB CLI surface."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raggd.cli.init import init_workspace
from raggd.cli.vdb import create_vdb_app
from raggd.core.paths import resolve_workspace
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import (
    ManifestService,
    manifest_settings_from_config,
)
from raggd.source.config import SourceConfigStore
from raggd.source.models import WorkspaceSourceConfig


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Materialize a minimal workspace for CLI exercises."""

    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    return workspace


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a Typer CLI runner instance."""

    return CliRunner()


def test_vdb_cli_info_reports_no_vdbs(
        workspace: Path,
        runner: CliRunner
) -> None:
    """`raggd vdb info` should indicate when no VDBs exist."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        ["--workspace", workspace.as_posix(), "info"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "No VDBs found." in result.stdout


def test_vdb_cli_sync_requires_configured_source(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """`vdb sync` should fail fast when the source is missing."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        ["--workspace", workspace.as_posix(), "sync", "docs"],
    )

    assert result.exit_code == 1
    assert "Source 'docs' is not configured in this workspace." in result.stdout


def _configure_docs_source(workspace: Path) -> None:
    """Attach a `docs` source entry to the workspace configuration."""

    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    docs_dir = resolve_workspace(
        workspace_override=workspace,
    ).source_dir("docs")
    docs_dir.mkdir(parents=True, exist_ok=True)

    source = WorkspaceSourceConfig(
        name="docs",
        path=docs_dir,
        enabled=True,
    )
    store.upsert(source)


def _seed_docs_database(workspace: Path) -> Path:
    """Ensure the docs database exists with a baseline batch/model."""

    paths = resolve_workspace(workspace_override=workspace)
    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    config = store.load()
    payload = config.model_dump(mode="python")

    manifest_service = ManifestService(
        workspace=paths,
        settings=manifest_settings_from_config(payload),
    )
    db_service = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest_service,
        db_settings=db_settings_from_mapping(payload),
    )

    db_path = db_service.ensure("docs")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            (
                "batch-001",
                None,
                datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
                None,
            ),
        )
        connection.execute(
            (
                "INSERT INTO embedding_models (provider, name, dim) "
                "VALUES (?, ?, ?)"
            ),
            ("openai", "test", 1536),
        )
    return db_path


def _seed_docs_vdb_artifacts(
    db_path: Path,
    *,
    vdb_name: str,
    batch_id: str,
    timestamp: datetime,
) -> tuple[Path, Path, Path]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        vdb_row = connection.execute(
            (
                "SELECT id, faiss_path, embedding_model_id "
                "FROM vdbs WHERE name = ?"
            ),
            (vdb_name,),
        ).fetchone()
        assert vdb_row is not None
        vdb_id = int(vdb_row["id"])
        faiss_path = Path(vdb_row["faiss_path"])
        embedding_model_id = int(vdb_row["embedding_model_id"])

        file_id = connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (batch_id, "src/example.py", "python", "sha", 0, 16),
        ).lastrowid

        symbol_id = connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, "
                "returns_json, imports_json, deps_out_json, docstring, "
                "summary, tokens, first_seen_batch, last_seen_batch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "function",
                "example:example",
                1,
                2,
                "sym",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                16,
                batch_id,
                batch_id,
            ),
        ).lastrowid

        chunk_id = connection.execute(
            (
                "INSERT INTO chunks (symbol_id, vdb_id, header_md, body_text, "
                "token_count) VALUES (?, ?, ?, ?, ?)"
            ),
            (
                symbol_id,
                vdb_id,
                "# example\n- File: `src/example.py`",
                "def example():\n    return 42\n",
                16,
            ),
        ).lastrowid

        connection.execute(
            "INSERT INTO vectors (chunk_id, vdb_id, dim) VALUES (?, ?, ?)",
            (chunk_id, vdb_id, 1536),
        )
        connection.commit()

    faiss_path.parent.mkdir(parents=True, exist_ok=True)
    faiss_path.write_bytes(b"FAKE")

    sidecar_path = Path(f"{faiss_path}.meta.json")
    sidecar_payload = {
        "version": 1,
        "provider": "openai",
        "model_id": embedding_model_id,
        "model_name": "test",
        "dim": 1536,
        "metric": "cosine",
        "index_type": "IDMap,Flat",
        "vector_count": 1,
        "built_at": timestamp.isoformat(),
        "checksum": "0" * 64,
        "vdb_id": vdb_id,
    }
    sidecar_path.write_text(
        json.dumps(sidecar_payload, indent=2),
        encoding="utf-8",
    )

    lock_path = Path(f"{faiss_path}.lock")
    lock_path.write_text("locked", encoding="utf-8")

    return faiss_path, sidecar_path, lock_path


def test_vdb_cli_create_success(workspace: Path, runner: CliRunner) -> None:
    """`raggd vdb create` succeeds when the source and batch exist."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@latest",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert (
        "Created VDB base for docs@latest using model openai:test"
        in result.stdout
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM vdbs WHERE name = ?",
            ("base",),
        ).fetchone()
    assert row is not None and row[0] == 1


def test_vdb_cli_info_reports_records(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """`raggd vdb info --json` emits summaries for existing VDBs."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@batch-001",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        vdb_query = (
            "SELECT id, faiss_path, embedding_model_id FROM vdbs WHERE name = ?"
        )
        vdb_row = connection.execute(vdb_query, ("base",)).fetchone()
        assert vdb_row is not None
        vdb_id = int(vdb_row["id"])
        embedding_model_id = int(vdb_row["embedding_model_id"])
        faiss_path = Path(vdb_row["faiss_path"])

        file_insert = (
            "INSERT INTO files (batch_id, repo_path, lang, file_sha, mtime_ns, "
            "size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
        )
        file_id = connection.execute(
            file_insert,
            ("batch-001", "src/example.py", "python", "sha", 0, 4),
        ).lastrowid

        symbol_insert = (
            "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
            "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
            "imports_json, deps_out_json, docstring, summary, tokens, "
            "first_seen_batch, last_seen_batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        symbol_id = connection.execute(
            symbol_insert,
            (
                file_id,
                "function",
                "example:example",
                1,
                2,
                "sym",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                4,
                "batch-001",
                "batch-001",
            ),
        ).lastrowid

        chunk_insert = (
            "INSERT INTO chunks (symbol_id, vdb_id, header_md, body_text, "
            "token_count) VALUES (?, ?, ?, ?, ?)"
        )
        chunk_id = connection.execute(
            chunk_insert,
            (
                symbol_id,
                vdb_id,
                "# example",
                "def example():\n    return 42\n",
                4,
            ),
        ).lastrowid

        connection.execute(
            "INSERT INTO vectors (chunk_id, vdb_id, dim) VALUES (?, ?, ?)",
            (chunk_id, vdb_id, 1536),
        )
        connection.commit()

    faiss_path.parent.mkdir(parents=True, exist_ok=True)
    faiss_path.write_bytes(b"FAKE")
    sidecar_payload = {
        "version": 1,
        "provider": "openai",
        "model_id": embedding_model_id,
        "model_name": "test",
        "dim": 1536,
        "metric": "cosine",
        "index_type": "IDMap,Flat",
        "vector_count": 1,
        "built_at": datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
        "checksum": "0" * 64,
        "vdb_id": vdb_id,
    }
    sidecar_path = Path(f"{faiss_path}.meta.json")
    sidecar_path.write_text(
        json.dumps(sidecar_payload, indent=2),
        encoding="utf-8",
    )

    info_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "info",
            "docs",
            "--json",
        ],
        catch_exceptions=False,
    )

    assert info_result.exit_code == 0, info_result.stdout
    stdout = info_result.stdout
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    record = payload[0]
    assert record["selector"] == "docs:base"
    assert record["counts"]["vectors"] == 1
    assert record["faiss_path"] == str(faiss_path)


def test_vdb_cli_reset_requires_force(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """Reset should instruct users to rerun with --force when artifacts
    exist."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    create_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@batch-001",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )
    assert create_result.exit_code == 0, create_result.stdout

    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    faiss_path, sidecar_path, lock_path = _seed_docs_vdb_artifacts(
        db_path,
        vdb_name="base",
        batch_id="batch-001",
        timestamp=timestamp,
    )

    reset_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "reset",
            "docs",
            "--vdb",
            "base",
        ],
        catch_exceptions=False,
    )

    assert reset_result.exit_code == 1
    assert "VDB reset failed" in reset_result.stdout
    assert "--force" in reset_result.stdout
    assert faiss_path.exists()
    assert sidecar_path.exists()
    assert lock_path.exists()


def test_vdb_cli_reset_force_success(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """Reset should purge artifacts when --force is supplied."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    create_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@batch-001",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )
    assert create_result.exit_code == 0, create_result.stdout

    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    faiss_path, sidecar_path, lock_path = _seed_docs_vdb_artifacts(
        db_path,
        vdb_name="base",
        batch_id="batch-001",
        timestamp=timestamp,
    )

    reset_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "reset",
            "docs",
            "--vdb",
            "base",
            "--force",
        ],
        catch_exceptions=False,
    )
    assert reset_result.exit_code == 0, reset_result.stdout
    assert "VDB reset complete" in reset_result.stdout
    assert "drop: False" in reset_result.stdout
    assert "dropped_vdbs" not in reset_result.stdout
    assert "vectors_deleted: 1" in reset_result.stdout
    assert "chunks_deleted: 1" in reset_result.stdout

    assert not faiss_path.exists()
    assert not sidecar_path.exists()
    assert not lock_path.exists()
    assert not faiss_path.parent.exists()

    with sqlite3.connect(db_path) as connection:
        vectors = connection.execute(
            (
                "SELECT COUNT(*) FROM vectors "
                "WHERE vdb_id = (SELECT id FROM vdbs WHERE name = ?)"
            ),
            ("base",),
        ).fetchone()[0]
        chunks = connection.execute(
            (
                "SELECT COUNT(*) FROM chunks "
                "WHERE vdb_id = (SELECT id FROM vdbs WHERE name = ?)"
            ),
            ("base",),
        ).fetchone()[0]
    assert vectors == 0
    assert chunks == 0


def test_vdb_cli_reset_drop_removes_vdb(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """Reset with --drop should remove the VDB row."""

    _configure_docs_source(workspace)
    db_path = _seed_docs_database(workspace)

    app = create_vdb_app()
    create_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "create",
            "docs@batch-001",
            "base",
            "--model",
            "openai:test",
        ],
        catch_exceptions=False,
    )
    assert create_result.exit_code == 0, create_result.stdout

    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    faiss_path, sidecar_path, lock_path = _seed_docs_vdb_artifacts(
        db_path,
        vdb_name="base",
        batch_id="batch-001",
        timestamp=timestamp,
    )

    reset_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "reset",
            "docs",
            "--vdb",
            "base",
            "--drop",
        ],
        catch_exceptions=False,
    )
    assert reset_result.exit_code == 1
    assert "--force" in reset_result.stdout

    drop_result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "reset",
            "docs",
            "--vdb",
            "base",
            "--drop",
            "--force",
        ],
        catch_exceptions=False,
    )
    assert drop_result.exit_code == 0, drop_result.stdout
    assert "drop: True" in drop_result.stdout
    assert "dropped_vdbs: ('base',)" in drop_result.stdout

    assert not faiss_path.exists()
    assert not sidecar_path.exists()
    assert not lock_path.exists()
    assert not faiss_path.parent.exists()

    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vdbs WHERE name = ?",
            ("base",),
        ).fetchone()[0]
    assert count == 0


def test_vdb_cli_sync_conflicting_flags(
    workspace: Path,
    runner: CliRunner,
) -> None:
    """Mutually exclusive sync flags should trigger a CLI error."""

    app = create_vdb_app()
    result = runner.invoke(
        app,
        [
            "--workspace",
            workspace.as_posix(),
            "sync",
            "docs",
            "--missing-only",
            "--recompute",
        ],
    )

    assert result.exit_code == 2
    assert "Invalid value for --missing-only/--recompute" in result.output
