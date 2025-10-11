"""Service layer implementing VDB lifecycle operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from raggd.core.config import AppConfig
from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import (
    DbLifecycleService,
    DbLockError,
    DbLockTimeoutError,
)
from raggd.modules.vdb.providers import (
    EmbeddingProviderModel,
    ProviderNotRegisteredError,
    ProviderRegistry,
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
        raise NotImplementedError  # pragma: no cover - not implemented yet

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
    def _normalize_identifier(value: str, *, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise VdbCreateError(f"{field} cannot be empty")
        return normalized
