from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.parser.handlers.base import (
    HandlerChunk,
    HandlerFile,
    HandlerResult,
)
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


def test_recompose_attaches_delegated_children(tmp_path: Path) -> None:
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
                256,
            ),
        )
        file_id = connection.execute(
            "SELECT id FROM files WHERE batch_id = ?",
            ("batch-1",),
        ).fetchone()[0]

        connection.execute(
            (
                "INSERT INTO symbols (\n"
                "    file_id, kind, symbol_path, start_line, end_line,\n"
                "    symbol_sha, symbol_norm_sha, args_json, returns_json,\n"
                "    imports_json, deps_out_json, docstring, summary, tokens,\n"
                "    first_seen_batch, last_seen_batch\n"
                ") VALUES (\n"
                "    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?\n"
                ")"
            ),
            (
                file_id,
                "section",
                "heading",
                1,
                8,
                "sha:heading",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                16,
                "batch-1",
                "batch-1",
            ),
        )
        section_symbol_id = connection.execute(
            "SELECT id FROM symbols WHERE symbol_path = ?",
            ("heading",),
        ).fetchone()[0]

        connection.execute(
            (
                "INSERT INTO symbols (\n"
                "    file_id, kind, symbol_path, start_line, end_line,\n"
                "    symbol_sha, symbol_norm_sha, args_json, returns_json,\n"
                "    imports_json, deps_out_json, docstring, summary, tokens,\n"
                "    first_seen_batch, last_seen_batch\n"
                ") VALUES (\n"
                "    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?\n"
                ")"
            ),
            (
                file_id,
                "code",
                "code-inline",
                9,
                12,
                "sha:inline",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                6,
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

        section_chunk = HandlerChunk(
            chunk_id="markdown:heading:0:180",
            text="Section body\n",
            token_count=8,
            start_offset=0,
            end_offset=180,
            part_index=0,
            parent_symbol_id="heading-symbol",
            metadata={
                "kind": "section",
                "start_line": 1,
                "end_line": 8,
                "part_total": 1,
            },
        )

        delegate_metadata = delegated_metadata(
            delegate="python",
            parent_handler="markdown",
            parent_symbol_id="heading-symbol",
            parent_chunk_id=section_chunk.chunk_id,
            extra={
                "kind": "fenced_code",
                "start_line": 9,
                "end_line": 12,
                "char_start": 180,
                "char_end": 240,
            },
        )

        delegated_chunk = HandlerChunk(
            chunk_id="python:delegate:markdown:fenced_code:180:240",
            text="print('hello')\n",
            token_count=5,
            start_offset=180,
            end_offset=240,
            part_index=0,
            parent_symbol_id="inline-code-symbol",
            delegate="python",
            metadata=delegate_metadata,
        )

        result = HandlerResult(
            file=handler_file,
            chunks=(section_chunk, delegated_chunk),
        )

        handler_versions = {"markdown": "1.0.0", "python": "2.0.0"}
        symbol_lookup = {
            "heading-symbol": section_symbol_id,
            "inline-code-symbol": inline_symbol_id,
        }

        pipeline.persist_chunks(
            connection=connection,
            batch_id="batch-1",
            file_id=file_id,
            handler_name="markdown",
            handler_version="1.0.0",
            result=result,
            handler_versions=handler_versions,
            symbol_ids=symbol_lookup,
        )

        chunks = recomposer.for_file(
            connection,
            batch_id="batch-1",
            file_id=file_id,
        )

        assert len(chunks) == 1
        parent = chunks[0]
        assert parent.chunk_id == section_chunk.chunk_id
        assert parent.token_count == section_chunk.token_count
        assert parent.symbol_id == section_symbol_id
        assert parent.delegate_children
        child = parent.delegate_children[0]
        assert child.chunk_id == delegated_chunk.chunk_id
        assert child.delegate_parent_chunk_id == parent.chunk_id
        assert child.symbol_id == inline_symbol_id
        assert child.metadata["delegate_parent_handler"] == "markdown"
        assert child.metadata["delegate_parent_symbol"] == "heading-symbol"
        assert parent.metadata["kind"] == "section"
        assert child.metadata["kind"] == "fenced_code"
