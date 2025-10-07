from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.parser.sql import load_sql, sql_path


def _make_paths(root: Path) -> WorkspacePaths:
    workspace = root / "workspace"
    init_workspace(workspace=workspace)
    return WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )


def test_sql_path_requires_name() -> None:
    with pytest.raises(ValueError):
        sql_path("")


def test_chunk_slice_statements_roundtrip(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    service = DbLifecycleService(workspace=paths)
    db_path = service.ensure("alpha")

    insert_sql = load_sql("chunk_slices_upsert.sql")
    select_sql = load_sql("chunk_slices_select_by_chunk.sql")
    delete_sql = load_sql("chunk_slices_delete_by_batch.sql")

    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Seed minimal foreign key rows so the insert succeeds.
        conn.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            ("batch-1", None, now, None),
        )
        conn.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            ("batch-1", "src/example.py", "python", "sha:file", 0, 123),
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE batch_id = ?",
            ("batch-1",),
        ).fetchone()[0]
        conn.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, "
                "returns_json, imports_json, deps_out_json, docstring, "
                "summary, tokens, first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "module",
                "example",
                1,
                10,
                "sha:symbol",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                42,
                "batch-1",
                "batch-1",
            ),
        )
        symbol_id = conn.execute(
            "SELECT id FROM symbols WHERE file_id = ?",
            (file_id,),
        ).fetchone()[0]

        params = {
            "batch_id": "batch-1",
            "file_id": file_id,
            "symbol_id": symbol_id,
            "parent_symbol_id": symbol_id,
            "chunk_id": "chunk://example#0",
            "handler_name": "text",
            "handler_version": "v1",
            "part_index": 0,
            "part_total": 1,
            "start_line": 1,
            "end_line": 10,
            "start_byte": 0,
            "end_byte": 100,
            "token_count": 50,
            "content_hash": "sha:chunk",
            "content_norm_hash": None,
            "content_text": "initial",
            "overflow_is_truncated": 0,
            "overflow_reason": None,
            "metadata_json": "{}",
            "created_at": now,
            "updated_at": now,
            "first_seen_batch": "batch-1",
            "last_seen_batch": "batch-1",
        }

        conn.execute(insert_sql, params)

        params.update(
            {
                "content_text": "updated",
                "content_hash": "sha:chunk2",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        conn.execute(insert_sql, params)

        rows = conn.execute(
            select_sql,
            {"batch_id": "batch-1", "chunk_id": "chunk://example#0"},
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["content_text"] == "updated"
        assert row["content_hash"] == "sha:chunk2"

        conn.execute(delete_sql, {"batch_id": "batch-1"})
        remaining = conn.execute(
            "SELECT COUNT(*) AS total FROM chunk_slices",
        ).fetchone()[0]
        assert remaining == 0
