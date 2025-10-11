from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest

from raggd.core.config import load_config, load_packaged_defaults
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import (
    ManifestService,
    manifest_settings_from_config,
)
from raggd.modules.vdb.providers import (
    EmbedRequestOptions,
    EmbeddingMatrix,
    EmbeddingProviderCaps,
    EmbeddingProviderModel,
    ProviderInitContext,
    ProviderRegistry,
)
from raggd.modules.vdb.service import VdbCreateError, VdbService


class _StubProvider:
    """Minimal embedding provider returning static metadata for tests."""

    def __init__(self, context: ProviderInitContext) -> None:
        self.logger = context.logger

    def describe_model(self, model: str) -> EmbeddingProviderModel:
        return EmbeddingProviderModel(provider="stub", name=model, dim=1536)

    def capabilities(
        self,
        *,
        model: str | None = None,
    ) -> EmbeddingProviderCaps:
        return EmbeddingProviderCaps(max_batch_size=16, max_parallel_requests=2)

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:
        return tuple((0.0,) * 1536 for _ in texts)


def _stub_factory(context: ProviderInitContext) -> _StubProvider:
    return _StubProvider(context)


def _build_service(
    tmp_path: Path,
) -> tuple[VdbService, DbLifecycleService, WorkspacePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    paths = WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )

    defaults = load_packaged_defaults()
    user_config = {
        "workspace": {
            "root": str(workspace),
            "sources": {
                "demo": {
                    "enabled": True,
                    "path": str(paths.source_dir("demo")),
                }
            },
        },
    }
    config = load_config(defaults=defaults, user_config=user_config)

    config_payload = config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(config_payload)
    db_settings = db_settings_from_mapping(config_payload)

    manifest_service = ManifestService(
        workspace=paths,
        settings=manifest_settings,
        logger=get_logger("test.vdb.manifest"),
    )
    db_service = DbLifecycleService(
        workspace=paths,
        manifest_service=manifest_service,
        db_settings=db_settings,
        logger=get_logger("test.vdb.db-service"),
    )

    registry = ProviderRegistry({"stub": _stub_factory})

    service = VdbService(
        workspace=paths,
        config=config,
        db_service=db_service,
        providers=registry,
        logger=get_logger("test.vdb.service"),
        now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    paths.sources_dir.mkdir(parents=True, exist_ok=True)
    paths.source_dir("demo").mkdir(parents=True, exist_ok=True)

    return service, db_service, paths


def _seed_batch(db_path: Path, batch_id: str, generated_at: datetime) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            (batch_id, None, generated_at.isoformat(), None),
        )


def test_create_inserts_vdb_and_embedding_model(tmp_path: Path) -> None:
    service, db_service, paths = _build_service(tmp_path)
    db_path = db_service.ensure("demo")
    _seed_batch(
        db_path,
        "batch-001",
        datetime(2023, 12, 31, tzinfo=timezone.utc),
    )

    service.create(
        selector="demo@batch-001",
        name="primary",
        model="stub:model-a",
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        vdb_row = connection.execute(
            (
                "SELECT batch_id, embedding_model_id, faiss_path "
                "FROM vdbs WHERE name = ?"
            ),
            ("primary",),
        ).fetchone()
        assert vdb_row is not None
        assert vdb_row["batch_id"] == "batch-001"

        model_row = connection.execute(
            ("SELECT provider, name, dim FROM embedding_models WHERE id = ?"),
            (vdb_row["embedding_model_id"],),
        ).fetchone()

    assert model_row is not None
    assert model_row["provider"] == "stub"
    assert model_row["name"] == "model-a"
    assert model_row["dim"] == 1536

    expected_path = (
        paths.source_dir("demo") / "vectors" / "primary" / "index.faiss"
    )
    assert Path(vdb_row["faiss_path"]) == expected_path
    assert expected_path.parent.is_dir()

    # Idempotent second invocation
    service.create(
        selector="demo@batch-001",
        name="primary",
        model="stub:model-a",
    )

    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vdbs WHERE name = ?",
            ("primary",),
        ).fetchone()[0]
    assert count == 1


def test_create_rejects_conflicting_vdb(tmp_path: Path) -> None:
    service, db_service, _paths = _build_service(tmp_path)
    db_path = db_service.ensure("demo")
    _seed_batch(
        db_path,
        "batch-001",
        datetime(2023, 12, 31, tzinfo=timezone.utc),
    )
    _seed_batch(
        db_path,
        "batch-002",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    service.create(
        selector="demo@batch-001",
        name="primary",
        model="stub:model-a",
    )

    with pytest.raises(VdbCreateError) as exc:
        service.create(
            selector="demo@batch-002",
            name="primary",
            model="stub:model-a",
        )

    assert "reset --drop" in str(exc.value)


def test_sync_materializes_chunks_and_vectors(tmp_path: Path) -> None:
    pytest.importorskip("faiss")
    pytest.importorskip("numpy")

    service, db_service, paths = _build_service(tmp_path)
    db_path = db_service.ensure("demo")
    batch_id = "batch-001"
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)

    _seed_batch(db_path, batch_id, timestamp)

    service.create(
        selector=f"demo@{batch_id}",
        name="primary",
        model="stub:model-a",
    )

    chunk_text = "def example():\n    return 42\n"

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        file_id = connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                batch_id,
                "src/example.py",
                "python",
                "sha-example",
                0,
                len(chunk_text),
            ),
        ).lastrowid

        symbol_id = connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, "
                "start_line, end_line, symbol_sha, symbol_norm_sha, "
                "args_json, returns_json, imports_json, deps_out_json, "
                "docstring, summary, tokens, first_seen_batch, "
                "last_seen_batch) VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "function",
                "example:example",
                1,
                2,
                "sym-example",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                8,
                batch_id,
                batch_id,
            ),
        ).lastrowid

        connection.execute(
            (
                "INSERT INTO chunk_slices (batch_id, file_id, symbol_id, "
                "parent_symbol_id, chunk_id, handler_name, handler_version, "
                "part_index, part_total, start_line, end_line, start_byte, "
                "end_byte, token_count, content_hash, content_norm_hash, "
                "content_text, overflow_is_truncated, overflow_reason, "
                "metadata_json, created_at, updated_at, first_seen_batch, "
                "last_seen_batch) VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)"
            ),
            (
                batch_id,
                file_id,
                symbol_id,
                None,
                "chunk-example",
                "python",
                "1.0.0",
                0,
                1,
                1,
                2,
                0,
                len(chunk_text),
                12,
                "hash-example",
                None,
                chunk_text,
                0,
                None,
                "{}",
                timestamp.isoformat(),
                timestamp.isoformat(),
                batch_id,
                batch_id,
            ),
        )

    summary = service.sync(
        source="demo",
        vdb="primary",
        missing_only=False,
        recompute=False,
        limit=None,
        concurrency=None,
        dry_run=False,
    )

    assert summary["chunks_total"] == 1
    assert summary["vectors_embedded"] == 1
    assert summary["dry_run"] is False

    index_path = (
        paths.source_dir("demo") / "vectors" / "primary" / "index.faiss"
    )
    assert index_path.exists()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        chunk_row = connection.execute(
            (
                "SELECT c.header_md, c.body_text FROM chunks AS c "
                "JOIN vdbs AS v ON v.id = c.vdb_id WHERE v.name = ?"
            ),
            ("primary",),
        ).fetchone()
        assert chunk_row is not None
        assert chunk_row["body_text"] == chunk_text
        assert "Chunk: `chunk-example`" in chunk_row["header_md"]

        vector_row = connection.execute(
            (
                "SELECT dim FROM vectors AS vect "
                "JOIN vdbs AS v ON v.id = vect.vdb_id WHERE v.name = ?"
            ),
            ("primary",),
        ).fetchone()
        assert vector_row is not None
        assert vector_row["dim"] == 1536

    dry_run_summary = service.sync(
        source="demo",
        vdb="primary",
        missing_only=False,
        recompute=False,
        limit=None,
        concurrency=None,
        dry_run=True,
    )

    assert dry_run_summary["vectors_planned"] == 0
    assert dry_run_summary["chunks_total"] == 1


def test_create_supports_latest_alias(tmp_path: Path) -> None:
    service, db_service, paths = _build_service(tmp_path)
    db_path = db_service.ensure("demo")
    _seed_batch(
        db_path,
        "batch-old",
        datetime(2023, 12, 30, tzinfo=timezone.utc),
    )
    _seed_batch(
        db_path,
        "batch-new",
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )

    service.create(
        selector="demo@latest",
        name="latest-index",
        model="stub:model-a",
    )

    with sqlite3.connect(db_path) as connection:
        batch_id = connection.execute(
            "SELECT batch_id FROM vdbs WHERE name = ?",
            ("latest-index",),
        ).fetchone()[0]

    assert batch_id == "batch-new"
    expected_path = paths.source_dir("demo") / "vectors" / "latest-index"
    assert expected_path.is_dir()
