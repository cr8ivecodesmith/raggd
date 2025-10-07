from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.parser.handlers.base import HandlerChunk, HandlerFile, HandlerResult
from raggd.modules.parser.handlers.delegation import delegated_metadata
from raggd.modules.parser.persistence import (
    ChunkSliceRepository,
    ChunkWritePipeline,
)
from raggd.modules.parser.recomposition import ChunkRecomposer


def _make_workspace(tmp_path: Path) -> WorkspacePaths:
    workspace = tmp_path / "workspace"
    config_file = workspace / "raggd.toml"
    logs_dir = workspace / "logs"
    archives_dir = workspace / "archives"
    sources_dir = workspace / "sources"

    logs_dir.mkdir(parents=True, exist_ok=True)
    archives_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.touch()

    return WorkspacePaths(
        workspace=workspace,
        config_file=config_file,
        logs_dir=logs_dir,
        archives_dir=archives_dir,
        sources_dir=sources_dir,
    )


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kw: object) -> None:
        self.records.append((event, kw))

    def bind(self, **kw: object) -> "RecordingLogger":  # pragma: no cover - parity with structlog
        return self


def test_chunk_write_pipeline_persists_delegate_slices(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    db_service = DbLifecycleService(workspace=paths)
    db_path = db_service.ensure("alpha")

    repository = ChunkSliceRepository()
    pipeline = ChunkWritePipeline(repository=repository)

    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            ("batch-1", None, now, None),
        )
        connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                "batch-1",
                "docs/readme.md",
                "markdown",
                "sha:file",
                0,
                123,
            ),
        )
        file_id = connection.execute(
            "SELECT id FROM files WHERE batch_id = ?",
            ("batch-1",),
        ).fetchone()[0]

        connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "section",
                "heading",
                1,
                5,
                "sha:heading",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                10,
                "batch-1",
                "batch-1",
            ),
        )
        heading_symbol_id = connection.execute(
            "SELECT id FROM symbols WHERE symbol_path = ?",
            ("heading",),
        ).fetchone()[0]

        connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "code",
                "code-inline",
                6,
                8,
                "sha:inline",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                5,
                "batch-1",
                "batch-1",
            ),
        )
        inline_symbol_id = connection.execute(
            "SELECT id FROM symbols WHERE symbol_path = ?",
            ("code-inline",),
        ).fetchone()[0]

        handler_file = HandlerFile(
            path=Path("docs/readme.md"),
            language="markdown",
        )

        primary_chunk = HandlerChunk(
            chunk_id="markdown:heading:0:120",
            text="Section body",
            token_count=3,
            start_offset=0,
            end_offset=120,
            part_index=0,
            parent_symbol_id="heading-symbol",
            metadata={
                "kind": "section",
                "start_line": 1,
                "end_line": 5,
            },
        )

        delegate_metadata = delegated_metadata(
            delegate="python",
            parent_handler="markdown",
            parent_symbol_id="heading-symbol",
            parent_chunk_id=primary_chunk.chunk_id,
            extra={
                "kind": "fenced_code",
                "start_line": 6,
                "end_line": 8,
                "char_start": 120,
                "char_end": 180,
            },
        )

        delegated_chunk = HandlerChunk(
            chunk_id="python:delegate:markdown:fenced_code:120:180",
            text="print('hi')\n",
            token_count=4,
            start_offset=120,
            end_offset=180,
            part_index=1,
            parent_symbol_id="inline-code-symbol",
            delegate="python",
            metadata=delegate_metadata,
        )

        result = HandlerResult(
            file=handler_file,
            chunks=(primary_chunk, delegated_chunk),
        )

        handler_versions = {"markdown": "1.0.0", "python": "2.0.0"}
        symbol_lookup = {
            "heading-symbol": heading_symbol_id,
            "inline-code-symbol": inline_symbol_id,
        }

        rows = pipeline.persist_chunks(
            connection=connection,
            batch_id="batch-1",
            file_id=file_id,
            handler_name="markdown",
            handler_version="1.0.0",
            result=result,
            handler_versions=handler_versions,
            symbol_ids=symbol_lookup,
        )

        assert len(rows) == 2

        stored = connection.execute(
            "SELECT handler_name, handler_version, chunk_id, symbol_id, "
            "parent_symbol_id, metadata_json, content_hash, content_norm_hash "
            "FROM chunk_slices ORDER BY handler_name"
        ).fetchall()

        assert [row["handler_name"] for row in stored] == [
            "markdown",
            "python",
        ]
        markdown_row = stored[0]
        assert markdown_row["symbol_id"] == heading_symbol_id
        assert markdown_row["parent_symbol_id"] is None
        assert len(markdown_row["content_hash"]) == 64
        assert len(markdown_row["content_norm_hash"]) == 64

        python_row = stored[1]
        assert python_row["symbol_id"] == inline_symbol_id
        assert python_row["parent_symbol_id"] == heading_symbol_id
        metadata_json = python_row["metadata_json"]
        assert "delegate_parent_symbol" in metadata_json
        assert len(python_row["content_hash"]) == 64


def test_chunk_write_pipeline_reuses_rows_when_unchanged(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    db_service = DbLifecycleService(workspace=paths)
    db_path = db_service.ensure("alpha")

    repository = ChunkSliceRepository()
    pipeline = ChunkWritePipeline(repository=repository)
    recomposer = ChunkRecomposer(repository)

    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            ("batch-1", None, now, None),
        )
        connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                "batch-1",
                "docs/readme.md",
                "markdown",
                "sha:file",
                0,
                123,
            ),
        )
        file_id = connection.execute(
            "SELECT id FROM files WHERE batch_id = ?",
            ("batch-1",),
        ).fetchone()[0]

        connection.execute(
            (
                "INSERT INTO symbols (file_id, kind, symbol_path, start_line, "
                "end_line, symbol_sha, symbol_norm_sha, args_json, returns_json, "
                "imports_json, deps_out_json, docstring, summary, tokens, "
                "first_seen_batch, last_seen_batch) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                file_id,
                "section",
                "heading",
                1,
                5,
                "sha:heading",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                10,
                "batch-1",
                "batch-1",
            ),
        )
        heading_symbol_id = connection.execute(
            "SELECT id FROM symbols WHERE symbol_path = ?",
            ("heading",),
        ).fetchone()[0]

        handler_file = HandlerFile(
            path=Path("docs/readme.md"),
            language="markdown",
        )

        chunk = HandlerChunk(
            chunk_id="markdown:heading:0:120",
            text="Section body",
            token_count=3,
            start_offset=0,
            end_offset=120,
            part_index=0,
            parent_symbol_id="heading-symbol",
            metadata={
                "kind": "section",
                "start_line": 1,
                "end_line": 5,
            },
        )

        result = HandlerResult(
            file=handler_file,
            chunks=(chunk,),
        )

        handler_versions = {"markdown": "1.0.0"}
        symbol_lookup = {"heading-symbol": heading_symbol_id}

        inserted = pipeline.persist_chunks(
            connection=connection,
            batch_id="batch-1",
            file_id=file_id,
            handler_name="markdown",
            handler_version="1.0.0",
            result=result,
            handler_versions=handler_versions,
            symbol_ids=symbol_lookup,
        )
        assert len(inserted) == 1

        stored = connection.execute(
            "SELECT batch_id, first_seen_batch, last_seen_batch FROM chunk_slices"
        ).fetchone()
        assert stored["batch_id"] == "batch-1"
        assert stored["first_seen_batch"] == "batch-1"
        assert stored["last_seen_batch"] == "batch-1"

        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            ("batch-2", None, now, None),
        )

        reused = pipeline.persist_chunks(
            connection=connection,
            batch_id="batch-2",
            file_id=file_id,
            handler_name="markdown",
            handler_version="1.0.0",
            result=result,
            handler_versions=handler_versions,
            symbol_ids=symbol_lookup,
        )
        assert reused == ()

        stored_after = connection.execute(
            "SELECT batch_id, first_seen_batch, last_seen_batch FROM chunk_slices"
        ).fetchone()
        assert stored_after["batch_id"] == "batch-1"
        assert stored_after["first_seen_batch"] == "batch-1"
        assert stored_after["last_seen_batch"] == "batch-2"

        active_chunks = recomposer.for_file(
            connection,
            batch_id="batch-2",
            file_id=file_id,
        )
        assert len(active_chunks) == 1
        assert active_chunks[0].chunk_id == chunk.chunk_id

        tombstoned = recomposer.for_file(
            connection,
            batch_id="batch-3",
            file_id=file_id,
        )
        assert tombstoned == ()


def test_chunk_write_pipeline_logs_overflow_metadata(tmp_path: Path) -> None:
    paths = _make_workspace(tmp_path)
    db_service = DbLifecycleService(workspace=paths)
    db_path = db_service.ensure("alpha")

    repository = ChunkSliceRepository()
    logger = RecordingLogger()
    pipeline = ChunkWritePipeline(repository=repository, logger=logger)

    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (?, ?, ?, ?)"
            ),
            ("batch-1", None, now, None),
        )
        connection.execute(
            (
                "INSERT INTO files (batch_id, repo_path, lang, file_sha, "
                "mtime_ns, size_bytes) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                "batch-1",
                "docs/log.txt",
                "text",
                "sha:file",
                0,
                90,
            ),
        )
        file_id = connection.execute(
            "SELECT id FROM files WHERE repo_path = ?",
            ("docs/log.txt",),
        ).fetchone()[0]

        handler_file = HandlerFile(
            path=Path("docs/log.txt"),
            language="text",
        )

        overflow_chunk = HandlerChunk(
            chunk_id="text:chunk:0:90:0",
            text="a" * 90,
            token_count=30,
            start_offset=0,
            end_offset=90,
            part_index=0,
            metadata={
                "overflow": True,
                "overflow_reason": "max_tokens",
                "part_total": 2,
                "start_line": 1,
                "end_line": 10,
            },
        )

        result = HandlerResult(
            file=handler_file,
            chunks=(overflow_chunk,),
        )

        pipeline.persist_chunks(
            connection=connection,
            batch_id="batch-1",
            file_id=file_id,
            handler_name="text",
            handler_version="1.0.0",
            result=result,
            handler_versions={"text": "1.0.0"},
            symbol_ids={},
        )

    assert logger.records
    event, payload = logger.records[0]
    assert event == "parser-chunk-overflow"
    assert (
        payload["chunk_key"] == "batch-1:text:docs/log.txt:0:90:0"
    )
    assert payload["overflow_reason"] == "max_tokens"
    assert payload["overflow_is_truncated"] is True
    assert payload["handler"] == "text"
