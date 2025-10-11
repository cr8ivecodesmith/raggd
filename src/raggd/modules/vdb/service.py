"""Service layer implementing VDB lifecycle operations."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, Type

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import (
    DbLifecycleService,
    DbLockError,
    DbLockTimeoutError,
)
from raggd.modules.vdb.models import EmbeddingModel
from raggd.modules.vdb.providers import (
    EmbedRequestOptions,
    EmbeddingProviderModel,
    ProviderNotRegisteredError,
    ProviderRegistry,
    resolve_sync_concurrency,
)

__all__ = [
    "VdbService",
    "VdbServiceError",
    "VdbCreateError",
    "VdbSyncError",
    "VdbInfoError",
    "VdbResetError",
]


class VdbServiceError(RuntimeError):
    """Base error raised by :class:`VdbService`."""


class VdbCreateError(VdbServiceError):
    """Raised when VDB creation fails."""


class VdbSyncError(VdbServiceError):
    """Raised when vector synchronization fails."""


class VdbInfoError(VdbServiceError):
    """Raised when info aggregation fails."""


class VdbResetError(VdbServiceError):
    """Raised when reset operations fail."""


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _ChunkPayload:
    """Intermediate payload prepared for chunk materialization."""

    chunk_key: str
    symbol_id: int
    file_path: str
    header_md: str
    body_text: str
    token_count: int
    start_line: int | None
    end_line: int | None


@dataclass(slots=True)
class _ChunkRecord:
    """Persisted (or planned) chunk record for embedding operations."""

    id: int | None
    chunk_key: str
    symbol_id: int
    body_text: str
    token_count: int


@dataclass(slots=True)
class _FaissSupport:
    """Container for FAISS integration hooks."""

    index_cls: type
    error: type[Exception]
    load: Callable[..., Any]
    persist: Callable[..., Any]


@dataclass(slots=True)
class _SyncStats:
    """Aggregate counters gathered during a sync run."""

    vdb_names: list[str] = field(default_factory=list)
    total_chunks: int = 0
    inserted_chunks: int = 0
    updated_chunks: int = 0
    embedded_vectors: int = 0
    planned_vectors: int = 0
    skipped_vectors: int = 0


@dataclass(slots=True)
class VdbService:
    """Orchestrate VDB lifecycle operations for CLI consumers."""

    workspace: WorkspacePaths
    config: AppConfig
    db_service: DbLifecycleService
    providers: ProviderRegistry
    logger: Logger | None = None
    now: Callable[[], datetime] = _default_now

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = get_logger(__name__, component="vdb-service")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def create(self, *, selector: str, name: str, model: str) -> None:
        """Create a VDB bound to ``selector`` and ``model``."""

        source, batch_selector = self._parse_selector(selector)
        vdb_name = self._normalize_identifier(name, field="VDB name")
        provider_key, model_name = self._parse_model(model)

        source_config = self.config.workspace_sources.get(source)
        if source_config is None:
            raise VdbCreateError(
                f"Source {source!r} is not configured in this workspace."
            )

        db_path = self.db_service.ensure(source)
        vectors_root = self.workspace.source_dir(source) / "vectors"
        vdb_dir = vectors_root / vdb_name
        faiss_path = vdb_dir / "index.faiss"
        created_at = self._resolve_timestamp()

        try:
            with self.db_service.lock(source, action="vdb-create"):
                with sqlite3.connect(db_path) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys = ON")
                    with connection:
                        batch_id = self._resolve_batch_id(
                            connection,
                            selector=batch_selector,
                            source=source,
                        )
                        model_id, model_dim = self._ensure_embedding_model(
                            connection,
                            provider_key=provider_key,
                            model_name=model_name,
                        )
                        existing = connection.execute(
                            (
                                "SELECT id, batch_id, embedding_model_id, "
                                "faiss_path FROM vdbs WHERE name = ?"
                            ),
                            (vdb_name,),
                        ).fetchone()
                        if existing is not None:
                            self._verify_idempotent(
                                existing,
                                batch_id=batch_id,
                                model_id=model_id,
                                faiss_path=faiss_path,
                            )
                            self._log_create(
                                source=source,
                                vdb_name=vdb_name,
                                batch_id=batch_id,
                                model_key=f"{provider_key}:{model_name}",
                                model_dim=model_dim,
                                idempotent=True,
                            )
                            return

                        connection.execute(
                            (
                                "INSERT INTO vdbs ("  # noqa: S608 - parameterized
                                "name, batch_id, embedding_model_id, "
                                "faiss_path, created_at"
                                ") VALUES (?, ?, ?, ?, ?)"
                            ),
                            (
                                vdb_name,
                                batch_id,
                                model_id,
                                str(faiss_path),
                                created_at,
                            ),
                        )
        except (DbLockError, DbLockTimeoutError) as exc:
            raise VdbCreateError(
                f"Failed to acquire database lock for source {source!r}: {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise VdbCreateError(
                f"SQLite error while creating VDB {vdb_name!r}: {exc}"
            ) from exc

        vdb_dir.mkdir(parents=True, exist_ok=True)

        self._log_create(
            source=source,
            vdb_name=vdb_name,
            batch_id=batch_id,
            model_key=f"{provider_key}:{model_name}",
            model_dim=model_dim,
            idempotent=False,
        )

    def info(
        self,
        *,
        source: str | None,
        vdb: str | None,
    ) -> tuple[dict[str, object], ...]:
        raise NotImplementedError  # pragma: no cover - not implemented yet

    def sync(
        self,
        *,
        source: str,
        vdb: str | None,
        missing_only: bool,
        recompute: bool,
        limit: int | None,
        concurrency: int | str | None,
        dry_run: bool,
    ) -> dict[str, object]:
        self._validate_sync_options(
            missing_only=missing_only,
            recompute=recompute,
        )
        normalized_source, normalized_vdb = self._normalize_sync_identifiers(
            source=source,
            vdb=vdb,
        )
        self._ensure_source_configured(normalized_source)

        faiss_support = self._import_faiss_support()
        processed_at = self._resolve_timestamp()

        stats = self._perform_sync(
            source=normalized_source,
            normalized_vdb=normalized_vdb,
            missing_only=missing_only,
            recompute=recompute,
            limit=limit,
            concurrency=concurrency,
            dry_run=dry_run,
            faiss_support=faiss_support,
        )

        return self._build_sync_summary(
            stats=stats,
            processed_at=processed_at,
            source=normalized_source,
            normalized_vdb=normalized_vdb,
            limit=limit,
            missing_only=missing_only,
            recompute=recompute,
            dry_run=dry_run,
        )

    def reset(
        self,
        *,
        source: str,
        vdb: str | None,
        drop: bool,
        force: bool,
    ) -> dict[str, object]:
        raise NotImplementedError  # pragma: no cover - not implemented yet

    # ------------------------------------------------------------------
    # Internal helpers - sync orchestration
    # ------------------------------------------------------------------
    def _validate_sync_options(
        self,
        *,
        missing_only: bool,
        recompute: bool,
    ) -> None:
        if missing_only and recompute:
            raise VdbSyncError(
                "--missing-only and --recompute cannot be combined."
            )

    def _normalize_sync_identifiers(
        self,
        *,
        source: str,
        vdb: str | None,
    ) -> tuple[str, str | None]:
        normalized_source = self._normalize_identifier(
            source,
            field="Source",
            error=VdbSyncError,
        )
        normalized_vdb: str | None = None
        if vdb is not None:
            normalized_vdb = self._normalize_identifier(
                vdb,
                field="VDB name",
                error=VdbSyncError,
            )
        return normalized_source, normalized_vdb

    def _ensure_source_configured(self, source: str) -> None:
        if source not in self.config.workspace_sources:
            raise VdbSyncError(
                f"Source {source!r} is not configured in this workspace."
            )

    def _import_faiss_support(self) -> _FaissSupport:
        try:
            from raggd.modules.vdb.faiss_index import (
                FaissIndex,
                FaissIndexError,
                load_index_artifacts,
                persist_index_artifacts,
            )
        except ImportError as exc:  # pragma: no cover
            # Exercised in error tests.
            raise VdbSyncError(
                "FAISS support is required for VDB sync; "
                "install the 'vdb' extra."
            ) from exc

        return _FaissSupport(
            index_cls=FaissIndex,
            error=FaissIndexError,
            load=load_index_artifacts,
            persist=persist_index_artifacts,
        )

    def _perform_sync(
        self,
        *,
        source: str,
        normalized_vdb: str | None,
        missing_only: bool,
        recompute: bool,
        limit: int | None,
        concurrency: int | str | None,
        dry_run: bool,
        faiss_support: _FaissSupport,
    ) -> _SyncStats:
        db_path = self.db_service.ensure(source)
        vdb_config = self.config.vdb
        stats = _SyncStats()

        try:
            with self.db_service.lock(source, action="vdb-sync"):
                with sqlite3.connect(db_path) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys = ON")

                    vdb_rows = self._select_target_vdbs(
                        connection,
                        source=source,
                        vdb_name=normalized_vdb,
                    )

                    for vdb_row in vdb_rows:
                        per_vdb = self._sync_single_vdb(
                            connection=connection,
                            source=source,
                            vdb_row=vdb_row,
                            metric=vdb_config.metric,
                            index_type=vdb_config.index_type,
                            missing_only=missing_only,
                            recompute=recompute,
                            limit=limit,
                            requested_concurrency=concurrency,
                            dry_run=dry_run,
                            config_batch_size=vdb_config.batch_size,
                            faiss_index_cls=faiss_support.index_cls,
                            load_index_artifacts=faiss_support.load,
                            persist_index_artifacts=faiss_support.persist,
                        )

                        stats.vdb_names.append(str(per_vdb["vdb"]))
                        stats.total_chunks += int(per_vdb["chunks_total"])
                        stats.inserted_chunks += int(per_vdb["chunks_inserted"])
                        stats.updated_chunks += int(per_vdb["chunks_updated"])
                        stats.embedded_vectors += int(
                            per_vdb["vectors_embedded"]
                        )
                        stats.skipped_vectors += int(
                            per_vdb["vectors_skipped"]
                        )
                        stats.planned_vectors += int(
                            per_vdb.get("vectors_planned", 0)
                        )
        except (DbLockError, DbLockTimeoutError) as exc:
            raise VdbSyncError(
                f"Failed to acquire database lock for source {source!r}: {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise VdbSyncError(
                f"SQLite error while syncing source {source!r}: {exc}"
            ) from exc
        except faiss_support.error as exc:
            raise VdbSyncError(f"FAISS index error: {exc}") from exc

        return stats

    def _build_sync_summary(
        self,
        *,
        stats: _SyncStats,
        processed_at: str,
        source: str,
        normalized_vdb: str | None,
        limit: int | None,
        missing_only: bool,
        recompute: bool,
        dry_run: bool,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "source": source,
            "vdbs": tuple(stats.vdb_names),
            "chunks_total": stats.total_chunks,
            "chunks_inserted": stats.inserted_chunks,
            "chunks_updated": stats.updated_chunks,
            "vectors_embedded": stats.embedded_vectors,
            "vectors_skipped": stats.skipped_vectors,
            "missing_only": missing_only,
            "recompute": recompute,
            "dry_run": dry_run,
            "processed_at": processed_at,
        }
        if dry_run:
            summary["vectors_planned"] = stats.planned_vectors
        if limit is not None:
            summary["limit"] = limit
        if normalized_vdb is not None:
            summary["target_vdb"] = normalized_vdb
        return summary

    def _select_target_vdbs(
        self,
        connection: sqlite3.Connection,
        *,
        source: str,
        vdb_name: str | None,
    ) -> tuple[sqlite3.Row, ...]:
        """Return VDB rows filtered by ``vdb_name`` when provided."""

        if vdb_name is not None:
            row = connection.execute(
                (
                    "SELECT id, name, batch_id, embedding_model_id, faiss_path "
                    "FROM vdbs WHERE name = ?"
                ),
                (vdb_name,),
            ).fetchone()
            if row is None:
                raise VdbSyncError(
                    f"VDB {vdb_name!r} was not found for source {source!r}."
                )
            return (row,)

        rows = connection.execute(
            (
                "SELECT id, name, batch_id, embedding_model_id, faiss_path "
                "FROM vdbs ORDER BY name"
            ),
        ).fetchall()
        if not rows:
            raise VdbSyncError(
                f"Source {source!r} has no VDBs; run `raggd vdb create` first."
            )
        return tuple(rows)

    def _resolve_faiss_path(self, stored: object) -> Path:
        """Normalize the FAISS index path stored in the database."""

        if stored is None:
            raise VdbSyncError("VDB record is missing a faiss_path value.")
        path = Path(str(stored)).expanduser()
        if not path.is_absolute():
            path = (self.workspace.workspace / path).resolve()
        return path

    def _load_embedding_model(
        self,
        connection: sqlite3.Connection,
        *,
        model_id: int,
        vdb_name: str,
    ) -> EmbeddingModel:
        """Load embedding model metadata for the given identifier."""

        row = connection.execute(
            (
                "SELECT id, provider, name, dim FROM embedding_models "
                "WHERE id = ?"
            ),
            (model_id,),
        ).fetchone()
        if row is None:
            raise VdbSyncError(
                f"Embedding model {model_id} referenced by VDB {vdb_name!r} "
                "is missing."
            )
        return EmbeddingModel.from_row(dict(row))

    def _create_provider(
        self,
        *,
        provider_key: str,
        model_name: str,
        source: str,
        vdb_name: str,
    ):
        """Instantiate provider and translate failures to sync errors."""

        try:
            provider = self.providers.create(
                provider_key,
                logger=self.logger.bind(
                    component="vdb-provider",
                    provider=provider_key,
                    source=source,
                    vdb=vdb_name,
                ),
                config=None,
            )
        except ProviderNotRegisteredError as exc:
            raise VdbSyncError(
                f"Embedding provider {provider_key!r} is not registered."
            ) from exc

        return provider

    def _resolve_batch_size(
        self,
        *,
        config_value: int | str,
        capabilities,
    ) -> int:
        """Resolve embedding batch size honoring config and provider caps."""

        provider_limit = max(1, int(capabilities.max_batch_size))
        if isinstance(config_value, str):
            resolved = provider_limit
            mode = "auto"
        else:
            resolved = max(1, min(int(config_value), provider_limit))
            mode = "fixed"

        self.logger.debug(
            "vdb-batch-size",
            mode=mode,
            resolved=resolved,
            provider_limit=provider_limit,
        )
        return resolved

    def _sync_single_vdb(
        self,
        *,
        connection: sqlite3.Connection,
        source: str,
        vdb_row: sqlite3.Row,
        metric: str,
        index_type: str,
        missing_only: bool,
        recompute: bool,
        limit: int | None,
        requested_concurrency: int | str | None,
        dry_run: bool,
        config_batch_size: int | str,
        faiss_index_cls,
        load_index_artifacts,
        persist_index_artifacts,
    ) -> dict[str, int | str]:
        """Execute sync for a single VDB row returning per-VDB statistics."""

        vdb_id = int(vdb_row["id"])
        vdb_name = str(vdb_row["name"])
        batch_id = str(vdb_row["batch_id"])
        embedding_model_id = int(vdb_row["embedding_model_id"])
        faiss_path = self._resolve_faiss_path(vdb_row["faiss_path"])

        model = self._load_embedding_model(
            connection,
            model_id=embedding_model_id,
            vdb_name=vdb_name,
        )

        provider = self._create_provider(
            provider_key=model.provider,
            model_name=model.name,
            source=source,
            vdb_name=vdb_name,
        )
        capabilities = provider.capabilities(model=model.name)

        resolved_concurrency = resolve_sync_concurrency(
            requested=requested_concurrency,
            provider_caps=capabilities,
            config_value=self.config.vdb.max_concurrency,
            logger=self.logger.bind(
                component="vdb-sync",
                source=source,
                vdb=vdb_name,
                stage="concurrency",
            ),
        )

        batch_size = self._resolve_batch_size(
            config_value=config_batch_size,
            capabilities=capabilities,
        )
        embed_options = EmbedRequestOptions(max_batch_size=batch_size)

        payloads = self._collect_chunk_payloads(
            connection,
            source=source,
            vdb_name=vdb_name,
            batch_id=batch_id,
        )

        records, inserted, updated = self._persist_chunks(
            connection,
            vdb_id=vdb_id,
            payloads=payloads,
            dry_run=dry_run,
        )

        vectors_embedded = 0
        vectors_planned = 0
        vectors_skipped = 0

        if dry_run:
            vectors_planned = self._count_planned_vectors(
                connection,
                vdb_id=vdb_id,
                records=records,
                recompute=recompute,
                missing_only=missing_only,
                limit=limit,
            )
        else:
            vectors_embedded, vectors_skipped = self._embed_and_persist_vectors(
                connection=connection,
                provider=provider,
                embed_options=embed_options,
                metric=metric,
                index_type=index_type,
                model=model,
                records=records,
                vdb_id=vdb_id,
                faiss_path=faiss_path,
                missing_only=missing_only,
                recompute=recompute,
                limit=limit,
                faiss_index_cls=faiss_index_cls,
                load_index_artifacts=load_index_artifacts,
                persist_index_artifacts=persist_index_artifacts,
                concurrency=resolved_concurrency,
                source=source,
                vdb_name=vdb_name,
            )

        self.logger.info(
            "vdb-sync-summary",
            source=source,
            vdb=vdb_name,
            batch=batch_id,
            dry_run=dry_run,
            chunks_total=len(payloads),
            chunks_inserted=inserted,
            chunks_updated=updated,
            vectors_embedded=vectors_embedded,
            vectors_planned=vectors_planned,
            vectors_skipped=vectors_skipped,
            concurrency=resolved_concurrency,
            batch_size=batch_size,
        )

        result: dict[str, int | str] = {
            "vdb": vdb_name,
            "chunks_total": len(payloads),
            "chunks_inserted": inserted,
            "chunks_updated": updated,
            "vectors_embedded": vectors_embedded,
            "vectors_skipped": vectors_skipped,
        }
        if dry_run:
            result["vectors_planned"] = vectors_planned
        return result

    def _collect_chunk_payloads(
        self,
        connection: sqlite3.Connection,
        *,
        source: str,
        vdb_name: str,
        batch_id: str,
    ) -> list[_ChunkPayload]:
        """Recompose chunk slices for a batch into payloads."""

        rows = connection.execute(
            (
                "SELECT chunk_id, symbol_id, file_id, handler_name, "
                "handler_version, part_index, part_total, start_line, "
                "end_line, token_count, content_text "
                "FROM chunk_slices WHERE batch_id = ? "
                "ORDER BY chunk_id, part_index"
            ),
            (batch_id,),
        ).fetchall()

        if not rows:
            self.logger.info(
                "vdb-sync-no-chunks",
                source=source,
                vdb=vdb_name,
                batch=batch_id,
            )
            return []

        grouped: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
        symbol_ids: set[int] = set()
        file_ids: set[int] = set()

        for row in rows:
            symbol_id = row["symbol_id"]
            if symbol_id is None:
                self.logger.warning(
                    "vdb-chunk-missing-symbol",
                    source=source,
                    vdb=vdb_name,
                    batch=batch_id,
                    chunk=row["chunk_id"],
                )
                continue
            grouped[str(row["chunk_id"])].append(row)
            symbol_ids.add(int(symbol_id))
            file_ids.add(int(row["file_id"]))

        symbol_map = self._fetch_symbol_map(connection, symbol_ids)
        file_map = self._fetch_file_map(connection, file_ids)

        payloads: list[_ChunkPayload] = []
        for chunk_key in sorted(grouped):
            parts = grouped[chunk_key]
            first = parts[0]
            symbol_id = int(first["symbol_id"])
            file_id = int(first["file_id"])

            symbol_row = symbol_map.get(symbol_id)
            if symbol_row is None:
                raise VdbSyncError(
                    (
                        "Symbol {symbol} referenced by chunk {chunk} "
                        "is missing; rerun the parser before syncing."
                    ).format(symbol=symbol_id, chunk=chunk_key)
                )

            file_row = file_map.get(file_id)
            if file_row is None:
                raise VdbSyncError(
                    (
                        "File {file} referenced by chunk {chunk} is missing; "
                        "rerun the parser before syncing."
                    ).format(file=file_id, chunk=chunk_key)
                )

            ordered = sorted(parts, key=lambda item: int(item["part_index"]))
            body_text = "".join(part["content_text"] for part in ordered)
            token_count = sum(int(part["token_count"]) for part in ordered)
            start_line = self._min_optional(
                part["start_line"] for part in ordered
            )
            end_line = self._max_optional(
                part["end_line"] for part in ordered
            )
            header_md = self._build_chunk_header(
                symbol_row=symbol_row,
                file_row=file_row,
                chunk_key=chunk_key,
                handler_name=first["handler_name"],
                handler_version=first["handler_version"],
                start_line=start_line,
                end_line=end_line,
            )

            payloads.append(
                _ChunkPayload(
                    chunk_key=chunk_key,
                    symbol_id=symbol_id,
                    file_path=str(file_row["repo_path"]),
                    header_md=header_md,
                    body_text=body_text,
                    token_count=token_count,
                    start_line=start_line,
                    end_line=end_line,
                )
            )

        return payloads

    def _fetch_symbol_map(
        self,
        connection: sqlite3.Connection,
        ids: Iterable[int],
    ) -> dict[int, sqlite3.Row]:
        """Return mapping of symbol id to row for the provided identifiers."""

        id_tuple = tuple(sorted(set(ids)))
        if not id_tuple:
            return {}
        placeholders = ",".join("?" for _ in id_tuple)
        query = (
            "SELECT id, symbol_path, kind FROM symbols WHERE id IN ("
            f"{placeholders})"
        )
        rows = connection.execute(query, id_tuple).fetchall()
        return {int(row["id"]): row for row in rows}

    def _fetch_file_map(
        self,
        connection: sqlite3.Connection,
        ids: Iterable[int],
    ) -> dict[int, sqlite3.Row]:
        """Return mapping of file id to row for the provided identifiers."""

        id_tuple = tuple(sorted(set(ids)))
        if not id_tuple:
            return {}
        placeholders = ",".join("?" for _ in id_tuple)
        query = (
            "SELECT id, repo_path FROM files WHERE id IN ("
            f"{placeholders})"
        )
        rows = connection.execute(query, id_tuple).fetchall()
        return {int(row["id"]): row for row in rows}

    def _build_chunk_header(
        self,
        *,
        symbol_row: sqlite3.Row,
        file_row: sqlite3.Row,
        chunk_key: str,
        handler_name: str,
        handler_version: str,
        start_line: int | None,
        end_line: int | None,
    ) -> str:
        """Compose a Markdown header describing a recomposed chunk."""

        lines = [
            f"# {symbol_row['symbol_path']}",
            "",
            f"- File: `{file_row['repo_path']}`",
            f"- Kind: {symbol_row['kind']}",
            f"- Chunk: `{chunk_key}`",
            f"- Handler: {handler_name} v{handler_version}",
        ]
        if start_line is not None and end_line is not None:
            lines.append(f"- Lines: {start_line}-{end_line}")
        return "\n".join(lines)

    def _persist_chunks(
        self,
        connection: sqlite3.Connection,
        *,
        vdb_id: int,
        payloads: Sequence[_ChunkPayload],
        dry_run: bool,
    ) -> tuple[list[_ChunkRecord], int, int]:
        """Insert or update ``chunks`` rows returning embedding records."""

        existing_rows = connection.execute(
            "SELECT id, symbol_id FROM chunks WHERE vdb_id = ?",
            (vdb_id,),
        ).fetchall()
        existing_by_symbol = {
            int(row["symbol_id"]): int(row["id"])
            for row in existing_rows
        }

        records: list[_ChunkRecord] = []
        inserted = 0
        updated = 0

        for payload in payloads:
            existing_id = existing_by_symbol.get(payload.symbol_id)

            if existing_id is None:
                inserted += 1
                chunk_id: int | None
                if dry_run:
                    chunk_id = None
                else:
                    cursor = connection.execute(
                        (
                            "INSERT INTO chunks (symbol_id, vdb_id, header_md, "
                            "body_text, token_count) VALUES (?, ?, ?, ?, ?)"
                        ),
                        (
                            payload.symbol_id,
                            vdb_id,
                            payload.header_md,
                            payload.body_text,
                            payload.token_count,
                        ),
                    )
                    chunk_id = int(cursor.lastrowid)
                    existing_by_symbol[payload.symbol_id] = chunk_id

                records.append(
                    _ChunkRecord(
                        id=chunk_id,
                        chunk_key=payload.chunk_key,
                        symbol_id=payload.symbol_id,
                        body_text=payload.body_text,
                        token_count=payload.token_count,
                    )
                )
                continue

            updated += 1
            if not dry_run:
                connection.execute(
                    (
                        "UPDATE chunks SET header_md = ?, body_text = ?, "
                        "token_count = ? WHERE id = ?"
                    ),
                    (
                        payload.header_md,
                        payload.body_text,
                        payload.token_count,
                        existing_id,
                    ),
                )

            records.append(
                _ChunkRecord(
                    id=existing_id,
                    chunk_key=payload.chunk_key,
                    symbol_id=payload.symbol_id,
                    body_text=payload.body_text,
                    token_count=payload.token_count,
                )
            )

        return records, inserted, updated

    def _count_planned_vectors(
        self,
        connection: sqlite3.Connection,
        *,
        vdb_id: int,
        records: Sequence[_ChunkRecord],
        recompute: bool,
        missing_only: bool,
        limit: int | None,
    ) -> int:
        """Return how many vectors would be embedded for a dry-run."""

        existing_ids = self._fetch_existing_vector_ids(
            connection,
            vdb_id=vdb_id,
        )
        if recompute:
            candidates = list(records)
        elif missing_only:
            candidates = [
                record
                for record in records
                if record.id is None or record.id not in existing_ids
            ]
        else:
            candidates = [
                record
                for record in records
                if record.id is None or record.id not in existing_ids
            ]
        count = len(candidates)
        if limit is not None:
            count = min(count, limit)
        # ``missing_only`` currently mirrors the default behavior of skipping
        # existing vectors; included for future branching conditions.
        return count

    def _fetch_existing_vector_ids(
        self,
        connection: sqlite3.Connection,
        *,
        vdb_id: int,
    ) -> set[int]:
        rows = connection.execute(
            "SELECT chunk_id FROM vectors WHERE vdb_id = ?",
            (vdb_id,),
        ).fetchall()
        return {int(row["chunk_id"]) for row in rows}

    def _embed_and_persist_vectors(
        self,
        *,
        connection: sqlite3.Connection,
        provider,
        embed_options: EmbedRequestOptions,
        metric: str,
        index_type: str,
        model: EmbeddingModel,
        records: Sequence[_ChunkRecord],
        vdb_id: int,
        faiss_path: Path,
        missing_only: bool,
        recompute: bool,
        limit: int | None,
        faiss_index_cls,
        load_index_artifacts,
        persist_index_artifacts,
        concurrency: int,
        source: str,
        vdb_name: str,
    ) -> tuple[int, int]:
        """Embed chunk records and persist FAISS artifacts."""

        existing_ids = self._fetch_existing_vector_ids(
            connection,
            vdb_id=vdb_id,
        )
        if recompute:
            connection.execute(
                "DELETE FROM vectors WHERE vdb_id = ?",
                (vdb_id,),
            )
            existing_ids.clear()

        targets, skipped = self._filter_embedding_targets(
            records=records,
            existing_ids=existing_ids,
            recompute=recompute,
            limit=limit,
        )

        index = self._prepare_faiss_index(
            faiss_path=faiss_path,
            faiss_index_cls=faiss_index_cls,
            load_index_artifacts=load_index_artifacts,
            model=model,
            metric=metric,
            index_type=index_type,
            recompute=recompute,
        )

        if not targets:
            if recompute:
                persist_index_artifacts(
                    index,
                    index_path=faiss_path,
                    provider=model.provider,
                    model_id=model.id,
                    model_name=model.name,
                    index_type=index_type,
                    built_at=self.now(),
                    vdb_id=vdb_id,
                )
            return 0, skipped

        vector_rows = self._embed_batches(
            provider=provider,
            embed_options=embed_options,
            metric=metric,
            model=model,
            targets=targets,
            index=index,
            vdb_id=vdb_id,
        )

        connection.executemany(
            (
                "INSERT INTO vectors (chunk_id, vdb_id, dim) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET dim = excluded.dim"
            ),
            vector_rows,
        )

        persist_index_artifacts(
            index,
            index_path=faiss_path,
            provider=model.provider,
            model_id=model.id,
            model_name=model.name,
            index_type=index_type,
            built_at=self.now(),
            vdb_id=vdb_id,
        )

        self.logger.debug(
            "vdb-sync-embedded",
            source=source,
            vdb=vdb_name,
            chunks=len(targets),
            concurrency=concurrency,
            missing_only=missing_only,
            recompute=recompute,
        )

        return len(targets), skipped

    def _filter_embedding_targets(
        self,
        *,
        records: Sequence[_ChunkRecord],
        existing_ids: set[int],
        recompute: bool,
        limit: int | None,
    ) -> tuple[list[_ChunkRecord], int]:
        """Filter chunk records into embedding targets and count skips."""

        targets: list[_ChunkRecord] = []
        skipped = 0
        for record in records:
            chunk_id = record.id
            if chunk_id is None:
                raise VdbSyncError(
                    "Chunk identifiers must be materialized before embedding."
                )
            if not recompute and chunk_id in existing_ids:
                skipped += 1
                continue
            targets.append(record)

        if limit is not None and len(targets) > limit:
            skipped += len(targets) - limit
            targets = list(targets[:limit])
        return targets, skipped

    def _prepare_faiss_index(
        self,
        *,
        faiss_path: Path,
        faiss_index_cls,
        load_index_artifacts,
        model: EmbeddingModel,
        metric: str,
        index_type: str,
        recompute: bool,
    ):
        """Create or load the FAISS index required for embedding."""

        faiss_path.parent.mkdir(parents=True, exist_ok=True)
        if recompute or not faiss_path.exists():
            return faiss_index_cls.create(
                dim=model.dim,
                metric=metric,
                index_type=index_type,
            )

        index, _ = load_index_artifacts(
            index_path=faiss_path,
            expected_dim=model.dim,
            expected_metric=metric,
        )
        return index

    def _embed_batches(
        self,
        *,
        provider,
        embed_options: EmbedRequestOptions,
        metric: str,
        model: EmbeddingModel,
        targets: Sequence[_ChunkRecord],
        index,
        vdb_id: int,
    ) -> list[tuple[int, int, int]]:
        """Embed chunk batches and update the FAISS index."""

        vector_rows: list[tuple[int, int, int]] = []
        for start in range(0, len(targets), embed_options.max_batch_size):
            batch = targets[start : start + embed_options.max_batch_size]
            texts = [record.body_text for record in batch]
            embeddings = provider.embed_texts(
                texts,
                model=model.name,
                options=embed_options,
            )
            if len(embeddings) != len(batch):
                raise VdbSyncError(
                    (
                        "Provider returned {actual} vectors for {expected} "
                        "inputs."
                    ).format(actual=len(embeddings), expected=len(batch))
                )

            ids = [int(record.id) for record in batch]
            normalized_vectors = [
                self._normalize_embedding(vector, metric=metric)
                for vector in embeddings
            ]

            for norm in normalized_vectors:
                if len(norm) != model.dim:
                    raise VdbSyncError(
                        (
                            "Embedding dimension mismatch for model {model}: "
                            "expected {expected}, got {actual}."
                        ).format(
                            model=model.key,
                            expected=model.dim,
                            actual=len(norm),
                        )
                    )

            index.add(ids, normalized_vectors)
            vector_rows.extend(
                (chunk_id, vdb_id, model.dim) for chunk_id in ids
            )
        return vector_rows

    @staticmethod
    def _normalize_embedding(
        vector: Sequence[float],
        *,
        metric: str,
    ) -> tuple[float, ...]:
        """Normalize embedding vectors when cosine similarity is requested."""

        floats = tuple(float(value) for value in vector)
        if metric.strip().lower() != "cosine":
            return floats
        norm = math.sqrt(sum(value * value for value in floats))
        if norm == 0:
            return floats
        return tuple(value / norm for value in floats)

    @staticmethod
    def _min_optional(values: Iterable[int | None]) -> int | None:
        filtered = [value for value in values if value is not None]
        return min(filtered) if filtered else None

    @staticmethod
    def _max_optional(values: Iterable[int | None]) -> int | None:
        filtered = [value for value in values if value is not None]
        return max(filtered) if filtered else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _parse_selector(self, selector: str) -> tuple[str, str]:
        parts = selector.split("@", 1)
        if len(parts) != 2:
            raise VdbCreateError(
                "Selector must be formatted as <source>@<batch|latest>."
            )
        source = self._normalize_identifier(parts[0], field="Source")
        batch_selector = self._normalize_identifier(
            parts[1],
            field="Batch selector",
        )
        return source, batch_selector

    def _parse_model(self, model: str) -> tuple[str, str]:
        parts = model.split(":", 1)
        if len(parts) != 2:
            raise VdbCreateError(
                "Model must be formatted as <provider>:<model>."
            )
        provider = self._normalize_identifier(
            parts[0],
            field="Provider",
        ).lower()
        model_name = self._normalize_identifier(
            parts[1],
            field="Model name",
        )
        return provider, model_name

    def _resolve_batch_id(
        self,
        connection: sqlite3.Connection,
        *,
        selector: str,
        source: str,
    ) -> str:
        if selector.lower() == "latest":
            row = connection.execute(
                (
                    "SELECT id "
                    "FROM batches "
                    "ORDER BY generated_at DESC, id DESC "
                    "LIMIT 1"
                )
            ).fetchone()
            if row is None:
                raise VdbCreateError(
                    (
                        f"Source {source!r} has no parser batches; run "
                        "`raggd parser parse` first."
                    )
                )
            return str(row["id"])

        row = connection.execute(
            "SELECT id FROM batches WHERE id = ?",
            (selector,),
        ).fetchone()
        if row is None:
            raise VdbCreateError(
                f"Batch {selector!r} was not found for source {source!r}."
            )
        return str(row["id"])

    def _ensure_embedding_model(
        self,
        connection: sqlite3.Connection,
        *,
        provider_key: str,
        model_name: str,
    ) -> tuple[int, int]:
        row = connection.execute(
            (
                "SELECT id, dim FROM embedding_models "
                "WHERE provider = ? AND name = ?"
            ),
            (provider_key, model_name),
        ).fetchone()

        if row is not None:
            existing_dim = int(row["dim"])
            try:
                model_info = self._describe_model(provider_key, model_name)
            except VdbCreateError:
                # Provider registration missing; trust the recorded dimension.
                return int(row["id"]), existing_dim

            provider_dim = model_info.dim
            if provider_dim is not None and provider_dim != existing_dim:
                raise VdbCreateError(
                    (
                        "Embedding model dimension mismatch for "
                        f"{provider_key}:{model_name}. Existing dim is "
                        f"{existing_dim}, provider reports {provider_dim}. "
                        "Reset VDBs referencing this model or drop the "
                        "model entry before retrying."
                    )
                )
            return int(row["id"]), existing_dim

        model_info = self._describe_model(provider_key, model_name)
        provider_dim = model_info.dim

        if provider_dim is None:
            raise VdbCreateError(
                (
                    f"Provider {provider_key!r} did not report a dimension "
                    f"for {model_name!r}. Run `raggd vdb sync` once to "
                    "establish the dimension, then retry."
                )
            )

        cursor = connection.execute(
            (
                "INSERT INTO embedding_models (provider, name, dim) "
                "VALUES (?, ?, ?)"
            ),
            (provider_key, model_name, provider_dim),
        )
        return int(cursor.lastrowid), provider_dim

    def _describe_model(
        self,
        provider_key: str,
        model_name: str,
    ) -> EmbeddingProviderModel:
        try:
            provider = self.providers.create(
                provider_key,
                logger=self.logger.bind(
                    component="provider",
                    provider=provider_key,
                ),
                config=None,
            )
        except ProviderNotRegisteredError as exc:
            raise VdbCreateError(
                f"Embedding provider {provider_key!r} is not registered."
            ) from exc
        return provider.describe_model(model_name)

    @staticmethod
    def _verify_idempotent(
        existing: sqlite3.Row,
        *,
        batch_id: str,
        model_id: int,
        faiss_path: Path,
    ) -> None:
        if (
            existing["batch_id"] == batch_id
            and int(existing["embedding_model_id"]) == model_id
            and Path(str(existing["faiss_path"])) == faiss_path
        ):
            return
        raise VdbCreateError(
            (
                "A VDB with this name already exists but targets a different "
                "batch or embedding model. Use `raggd vdb reset --drop` "
                "before recreating it, or choose a new VDB name."
            )
        )

    def _log_create(
        self,
        *,
        source: str,
        vdb_name: str,
        batch_id: str,
        model_key: str,
        model_dim: int,
        idempotent: bool,
    ) -> None:
        self.logger.info(
            "vdb-create",
            source=source,
            name=vdb_name,
            batch=batch_id,
            model=model_key,
            dim=model_dim,
            idempotent=idempotent,
        )

    def _resolve_timestamp(self) -> str:
        timestamp = self.now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        return timestamp.isoformat()

    @staticmethod
    def _normalize_identifier(
        value: str,
        *,
        field: str,
        error: Type[VdbServiceError] = VdbCreateError,
    ) -> str:
        normalized = value.strip()
        if not normalized:
            raise error(f"{field} cannot be empty")
        return normalized
