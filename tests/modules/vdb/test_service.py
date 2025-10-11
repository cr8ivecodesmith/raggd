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
    ) -> EmbeddingProviderCaps:  # pragma: no cover - helper for future steps
        return EmbeddingProviderCaps(max_batch_size=16, max_parallel_requests=2)

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:  # pragma: no cover - not exercised yet
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
