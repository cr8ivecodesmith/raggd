"""Transactional staging helpers for parser persistence."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Callable, Iterator, Mapping, MutableMapping, Sequence

from raggd.modules.db import DbLifecycleService

from .handlers.base import HandlerChunk, HandlerResult, HandlerSymbol
from .hashing import DEFAULT_HASH_ALGORITHM, hash_text
from .persistence import ChunkSliceRepository, ChunkWritePipeline

__all__ = [
    "FileStageOutcome",
    "ParserPersistenceTransaction",
    "parser_transaction",
]


@dataclass(slots=True)
class FileStageOutcome:
    """Aggregate of persistence counts for a staged file."""

    file_id: int
    symbols_written: int
    symbols_reused: int
    chunks_inserted: int
    chunks_reused: int


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_path(path: Path | str) -> str:
    if isinstance(path, Path):
        return path.as_posix()
    return str(path).replace("\\", "/")


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return int(candidate)
        except ValueError:
            return None
    return None


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _chunk_groups(
    chunks: Sequence[HandlerChunk],
) -> Mapping[str, list[HandlerChunk]]:
    grouped: dict[str, list[HandlerChunk]] = {}
    for chunk in chunks:
        symbol_key = chunk.parent_symbol_id
        if not symbol_key:
            continue
        grouped.setdefault(symbol_key, []).append(chunk)
    for parts in grouped.values():
        parts.sort(key=lambda item: item.part_index)
    return grouped


@dataclass(slots=True)
class _SymbolCandidate:
    symbol: HandlerSymbol
    symbol_path: str
    kind: str
    start_line: int
    end_line: int
    text: str
    normalized_text: str
    symbol_sha: str
    symbol_norm_sha: str | None
    tokens: int
    docstring: str | None


class _FileRepository:
    """Lightweight repository for ``files`` table operations."""

    def __init__(self) -> None:
        self._select_sql = (
            "SELECT id, batch_id, repo_path, lang, file_sha,\n"
            "       mtime_ns, size_bytes\n"
            "FROM files\n"
            "WHERE repo_path = :repo_path"
        )
        self._insert_sql = (
            "INSERT INTO files (\n"
            "    batch_id, repo_path, lang, file_sha, mtime_ns, size_bytes\n"
            ") VALUES (\n"
            "    :batch_id, :repo_path, :lang, :file_sha,\n"
            "    :mtime_ns, :size_bytes\n"
            ")"
        )
        self._update_sql = (
            "UPDATE files\n"
            "SET batch_id = :batch_id,\n"
            "    lang = :lang,\n"
            "    file_sha = :file_sha,\n"
            "    mtime_ns = :mtime_ns,\n"
            "    size_bytes = :size_bytes\n"
            "WHERE id = :id"
        )

    def upsert(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        repo_path: str,
        lang: str,
        file_sha: str,
        mtime_ns: int | None,
        size_bytes: int | None,
    ) -> int:
        row = connection.execute(
            self._select_sql, {"repo_path": repo_path}
        ).fetchone()
        params = {
            "batch_id": batch_id,
            "repo_path": repo_path,
            "lang": lang,
            "file_sha": file_sha,
            "mtime_ns": mtime_ns,
            "size_bytes": size_bytes,
        }
        if row is None:
            cursor = connection.execute(self._insert_sql, params)
            return int(cursor.lastrowid)
        params["id"] = row["id"]
        connection.execute(self._update_sql, params)
        return int(row["id"])


class _SymbolRepository:
    """Manage CRUD for ``symbols`` with reuse tracking."""

    def __init__(self, *, hash_algorithm: str = DEFAULT_HASH_ALGORITHM) -> None:
        self._hash_algorithm = hash_algorithm
        self._select_sql = (
            "SELECT id, symbol_path, kind, start_line, end_line, symbol_sha,\n"
            "       symbol_norm_sha, docstring, tokens, first_seen_batch,\n"
            "       last_seen_batch\n"
            "FROM symbols\n"
            "WHERE file_id = :file_id"
        )
        self._insert_sql = (
            "INSERT INTO symbols (\n"
            "    file_id, kind, symbol_path, start_line, end_line,\n"
            "    symbol_sha, symbol_norm_sha, args_json, returns_json,\n"
            "    imports_json, deps_out_json, docstring, summary, tokens,\n"
            "    first_seen_batch, last_seen_batch\n"
            ") VALUES (\n"
            "    :file_id, :kind, :symbol_path, :start_line, :end_line,\n"
            "    :symbol_sha, :symbol_norm_sha, :args_json,\n"
            "    :returns_json, :imports_json, :deps_out_json,\n"
            "    :docstring, :summary, :tokens,\n"
            "    :first_seen_batch, :last_seen_batch\n"
            ")"
        )
        self._update_sql = (
            "UPDATE symbols\n"
            "SET kind = :kind,\n"
            "    start_line = :start_line,\n"
            "    end_line = :end_line,\n"
            "    symbol_sha = :symbol_sha,\n"
            "    symbol_norm_sha = :symbol_norm_sha,\n"
            "    docstring = :docstring,\n"
            "    tokens = :tokens,\n"
            "    last_seen_batch = :last_seen_batch\n"
            "WHERE id = :id"
        )
        self._touch_sql = (
            "UPDATE symbols\n"
            "SET last_seen_batch = :last_seen_batch\n"
            "WHERE id = :id"
        )

    def persist(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        file_id: int,
        handler_name: str,
        handler_version: str,
        result: HandlerResult,
    ) -> tuple[dict[str, int], int, int]:
        existing = self._load_existing(connection, file_id=file_id)
        grouped_chunks = _chunk_groups(result.chunks)
        candidates = list(
            self._build_candidates(
                result.symbols,
                grouped_chunks,
                handler_name=handler_name,
                handler_version=handler_version,
            )
        )

        symbol_ids: dict[str, int] = {}
        written = 0
        reused = 0

        for candidate in candidates:
            current = existing.get(candidate.symbol_path)
            if current is None:
                symbol_id = self._insert(
                    connection,
                    file_id=file_id,
                    batch_id=batch_id,
                    candidate=candidate,
                )
                written += 1
                symbol_ids[candidate.symbol.symbol_id] = symbol_id
                continue

            if self._is_equivalent(current, candidate):
                if current["last_seen_batch"] != batch_id:
                    connection.execute(
                        self._touch_sql,
                        {"id": current["id"], "last_seen_batch": batch_id},
                    )
                reused += 1
                symbol_ids[candidate.symbol.symbol_id] = int(current["id"])
                continue

            self._update(
                connection,
                batch_id=batch_id,
                existing=current,
                candidate=candidate,
            )
            symbol_ids[candidate.symbol.symbol_id] = int(current["id"])
            written += 1

        return symbol_ids, written, reused

    def _load_existing(
        self,
        connection: sqlite3.Connection,
        *,
        file_id: int,
    ) -> MutableMapping[str, sqlite3.Row]:
        rows = connection.execute(
            self._select_sql,
            {"file_id": file_id},
        ).fetchall()
        return {str(row["symbol_path"]): row for row in rows}

    def _build_candidates(
        self,
        symbols: Sequence[HandlerSymbol],
        grouped_chunks: Mapping[str, Sequence[HandlerChunk]],
        *,
        handler_name: str,
        handler_version: str,
    ) -> Iterator[_SymbolCandidate]:
        for symbol in symbols:
            symbol_path = symbol.symbol_id or f"{handler_name}:{symbol.name}"
            chunks = grouped_chunks.get(symbol.symbol_id, ())
            start_line, end_line = self._line_bounds(symbol, chunks)
            text = "".join(chunk.text for chunk in chunks)
            normalized = _normalize_text(text) if text else ""
            token_count = sum(chunk.token_count or 0 for chunk in chunks)
            symbol_sha = hash_text(
                text,
                handler_version=handler_version,
                algorithm=self._hash_algorithm,
                extra=(symbol_path,),
            )
            symbol_norm_sha = None
            if normalized:
                symbol_norm_sha = hash_text(
                    normalized,
                    handler_version=handler_version,
                    algorithm=self._hash_algorithm,
                    extra=(symbol_path,),
                )
            yield _SymbolCandidate(
                symbol=symbol,
                symbol_path=symbol_path,
                kind=symbol.kind,
                start_line=start_line,
                end_line=end_line,
                text=text,
                normalized_text=normalized,
                symbol_sha=symbol_sha,
                symbol_norm_sha=symbol_norm_sha,
                tokens=token_count,
                docstring=symbol.docstring,
            )

    def _line_bounds(
        self,
        symbol: HandlerSymbol,
        chunks: Sequence[HandlerChunk],
    ) -> tuple[int, int]:
        start_line: int | None = None
        end_line: int | None = None
        for chunk in chunks:
            metadata = chunk.metadata or {}
            start = _coerce_int(metadata.get("start_line"))
            end = _coerce_int(metadata.get("end_line"))
            if start is not None:
                start_line = (
                    start if start_line is None else min(start_line, start)
                )
            if end is not None:
                end_line = end if end_line is None else max(end_line, end)
        meta = symbol.metadata or {}
        if start_line is None:
            start_line = _coerce_int(meta.get("start_line"))
        if start_line is None:
            start_line = _coerce_int(meta.get("line"))
        if start_line is None:
            start_line = 0
        if end_line is None:
            end_line = _coerce_int(meta.get("end_line")) or start_line
        return start_line, end_line

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        file_id: int,
        batch_id: str,
        candidate: _SymbolCandidate,
    ) -> int:
        params = {
            "file_id": file_id,
            "kind": candidate.kind,
            "symbol_path": candidate.symbol_path,
            "start_line": candidate.start_line,
            "end_line": candidate.end_line,
            "symbol_sha": candidate.symbol_sha,
            "symbol_norm_sha": candidate.symbol_norm_sha,
            "args_json": None,
            "returns_json": None,
            "imports_json": None,
            "deps_out_json": None,
            "docstring": candidate.docstring,
            "summary": None,
            "tokens": candidate.tokens,
            "first_seen_batch": batch_id,
            "last_seen_batch": batch_id,
        }
        cursor = connection.execute(self._insert_sql, params)
        return int(cursor.lastrowid)

    def _update(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        existing: sqlite3.Row,
        candidate: _SymbolCandidate,
    ) -> None:
        params = {
            "id": existing["id"],
            "kind": candidate.kind,
            "start_line": candidate.start_line,
            "end_line": candidate.end_line,
            "symbol_sha": candidate.symbol_sha,
            "symbol_norm_sha": candidate.symbol_norm_sha,
            "docstring": candidate.docstring,
            "tokens": candidate.tokens,
            "last_seen_batch": batch_id,
        }
        connection.execute(self._update_sql, params)

    def _is_equivalent(
        self,
        row: sqlite3.Row,
        candidate: _SymbolCandidate,
    ) -> bool:
        return (
            row["kind"] == candidate.kind
            and row["start_line"] == candidate.start_line
            and row["end_line"] == candidate.end_line
            and row["symbol_sha"] == candidate.symbol_sha
            and row["symbol_norm_sha"] == candidate.symbol_norm_sha
            and row["docstring"] == candidate.docstring
            and int(row["tokens"]) == candidate.tokens
        )


class ParserPersistenceTransaction:
    """Orchestrate parser persistence within an active transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
        now: Callable[[], datetime] | None = None,
        chunk_repository: ChunkSliceRepository | None = None,
        chunk_pipeline: ChunkWritePipeline | None = None,
    ) -> None:
        connection.row_factory = sqlite3.Row
        self._connection = connection
        self._now = now or _default_now
        self._file_repo = _FileRepository()
        self._symbol_repo = _SymbolRepository(hash_algorithm=hash_algorithm)
        repository = chunk_repository or ChunkSliceRepository()
        self._chunk_pipeline = chunk_pipeline or ChunkWritePipeline(
            repository=repository,
            hash_algorithm=hash_algorithm,
            now=self._now,
        )
        self.lock_wait_seconds: float = 0.0

    def ensure_batch(
        self,
        *,
        batch_id: str,
        ref: str | None = None,
        generated_at: datetime | None = None,
        notes: str | None = None,
    ) -> None:
        timestamp = (generated_at or self._now()).isoformat()
        self._connection.execute(
            (
                "INSERT INTO batches (id, ref, generated_at, notes) "
                "VALUES (:id, :ref, :generated_at, :notes) "
                "ON CONFLICT(id) DO UPDATE SET ref = excluded.ref, "
                "generated_at = excluded.generated_at, notes = excluded.notes"
            ),
            {
                "id": batch_id,
                "ref": ref,
                "generated_at": timestamp,
                "notes": notes,
            },
        )

    def stage_file(
        self,
        *,
        batch_id: str,
        repo_path: Path | str,
        language: str,
        file_sha: str,
        handler_name: str,
        handler_version: str,
        handler_versions: Mapping[str, str],
        result: HandlerResult,
        absolute_path: Path | None = None,
        mtime_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> FileStageOutcome:
        normalized_path = _normalize_path(repo_path)
        resolved_size = size_bytes
        stat_result = None
        if resolved_size is None:
            resolved_size = _coerce_int(
                (result.file.metadata or {}).get("size_bytes")
            )
        if absolute_path is not None and absolute_path.exists():
            stat_result = absolute_path.stat()
            if resolved_size is None:
                resolved_size = stat_result.st_size

        resolved_mtime = mtime_ns
        if resolved_mtime is None:
            resolved_mtime = _coerce_int(
                (result.file.metadata or {}).get("mtime_ns")
            )
        if stat_result is not None and resolved_mtime is None:
            resolved_mtime = stat_result.st_mtime_ns

        file_id = self._file_repo.upsert(
            self._connection,
            batch_id=batch_id,
            repo_path=normalized_path,
            lang=language,
            file_sha=file_sha,
            mtime_ns=resolved_mtime,
            size_bytes=resolved_size,
        )

        symbol_ids, symbols_written, symbols_reused = self._symbol_repo.persist(
            self._connection,
            batch_id=batch_id,
            file_id=file_id,
            handler_name=handler_name,
            handler_version=handler_version,
            result=result,
        )

        effective_versions = dict(handler_versions)
        effective_versions.setdefault(handler_name, handler_version)

        inserted_rows = self._chunk_pipeline.persist_chunks(
            connection=self._connection,
            batch_id=batch_id,
            file_id=file_id,
            handler_name=handler_name,
            handler_version=handler_version,
            result=result,
            handler_versions=effective_versions,
            symbol_ids=symbol_ids,
        )

        chunks_inserted = len(inserted_rows)
        chunks_reused = len(result.chunks) - chunks_inserted

        return FileStageOutcome(
            file_id=file_id,
            symbols_written=symbols_written,
            symbols_reused=symbols_reused,
            chunks_inserted=chunks_inserted,
            chunks_reused=max(chunks_reused, 0),
        )


@contextmanager
def parser_transaction(
    db_service: DbLifecycleService,
    source: str,
    *,
    hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
    now: Callable[[], datetime] | None = None,
) -> Iterator[ParserPersistenceTransaction]:
    """Yield a parser persistence transaction for ``source``."""

    start = time.perf_counter()
    with db_service.lock(source, action="parser-stage"):
        wait_seconds = max(time.perf_counter() - start, 0.0)
        db_path = db_service.ensure(source)
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                transaction = ParserPersistenceTransaction(
                    connection,
                    hash_algorithm=hash_algorithm,
                    now=now,
                )
                transaction.lock_wait_seconds = wait_seconds
                yield transaction
        finally:
            connection.close()
