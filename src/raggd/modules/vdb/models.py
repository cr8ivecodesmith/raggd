"""Typed representations supporting the VDB service layer."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from raggd.modules import HealthReport, HealthStatus

__all__ = [
    "EmbeddingModel",
    "Vdb",
    "VdbHealthEntry",
    "VdbInfoCounts",
    "VdbInfoSummary",
]


def _parse_int(value: Any, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        result = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field} cannot be empty")
        try:
            result = int(stripped)
        except ValueError as exc:
            message = f"{field} must be an integer (got {value!r})"
            raise ValueError(message) from exc
    else:
        type_message = (
            f"{field} must be an integer-compatible value "
            f"(got {type(value)!r})"
        )
        raise TypeError(type_message)
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be >= {minimum} (got {result})")
    return result


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field} cannot be empty")
        if stripped.endswith("Z"):
            stripped = f"{stripped[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError as exc:
            message = f"{field} must be ISO-8601 (got {value!r})"
            raise ValueError(message) from exc
    else:
        type_message = (
            f"{field} must be ISO-8601 string or datetime; got "
            f"{type(value)!r}"
        )
        raise TypeError(type_message)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_optional_datetime(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field=field)


def _parse_path(value: Any, *, field: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field} cannot be empty")
        path = Path(stripped)
    else:
        message = f"{field} must be a path-like string; got {type(value)!r}"
        raise TypeError(message)
    return path


def _normalize_string(value: Any, *, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} cannot be empty")
    return result


def _normalize_counts_value(value: Any) -> "VdbInfoCounts":
    if value is None:
        return VdbInfoCounts()
    if isinstance(value, VdbInfoCounts):
        return value
    if isinstance(value, Mapping):
        return VdbInfoCounts.from_mapping(value)
    raise TypeError("counts must be mapping-like or VdbInfoCounts instance")


def _normalize_health_entries(
    entries: Sequence["VdbHealthEntry" | Mapping[str, Any]] | None,
) -> tuple["VdbHealthEntry", ...]:
    if not entries:
        return ()
    normalized: list[VdbHealthEntry] = []
    for entry in entries:
        if isinstance(entry, VdbHealthEntry):
            normalized.append(entry)
            continue
        if isinstance(entry, Mapping):
            normalized.append(VdbHealthEntry(**entry))
            continue
        raise TypeError(
            "info.health entries must be VdbHealthEntry or mapping",
        )
    return tuple(normalized)


def _resolve_sidecar_path(
    sidecar: Path | str | None,
    *,
    faiss_path: Path,
) -> Path | None:
    if sidecar is None:
        text = str(faiss_path).strip()
        if text in {"", ".", "./"}:
            return None
        return Path(f"{faiss_path}.meta.json")
    if isinstance(sidecar, Path):
        return Path(str(sidecar))
    return _parse_path(sidecar, field="info.sidecar_path")


def _normalize_pathlike(value: Path | str, *, field: str) -> Path:
    if isinstance(value, Path):
        return Path(str(value))
    return _parse_path(value, field=field)


@dataclass(frozen=True, slots=True)
class EmbeddingModel:
    """Embedding model metadata recorded in the ledger."""

    id: int
    provider: str
    name: str
    dim: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EmbeddingModel":
        return cls(
            id=_parse_int(row.get("id"), field="embedding_model.id", minimum=1),
            provider=_normalize_string(
                row.get("provider"),
                field="embedding_model.provider",
            ),
            name=_normalize_string(
                row.get("name"),
                field="embedding_model.name",
            ),
            dim=_parse_int(
                row.get("dim"),
                field="embedding_model.dim",
                minimum=1,
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _normalize_string(
                self.provider,
                field="embedding_model.provider",
            ),
        )
        object.__setattr__(
            self,
            "name",
            _normalize_string(
                self.name,
                field="embedding_model.name",
            ),
        )

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.name}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "dim": self.dim,
        }


@dataclass(frozen=True, slots=True)
class Vdb:
    """Canonical view over a `vdbs` table row."""

    id: int
    name: str
    batch_id: str
    embedding_model_id: int
    faiss_path: Path
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Vdb":
        return cls(
            id=_parse_int(row.get("id"), field="vdb.id", minimum=1),
            name=_normalize_string(row.get("name"), field="vdb.name"),
            batch_id=_normalize_string(
                row.get("batch_id"),
                field="vdb.batch_id",
            ),
            embedding_model_id=_parse_int(
                row.get("embedding_model_id"),
                field="vdb.embedding_model_id",
                minimum=1,
            ),
            faiss_path=_parse_path(
                row.get("faiss_path"),
                field="vdb.faiss_path",
            ),
            created_at=_parse_datetime(
                row.get("created_at"),
                field="vdb.created_at",
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _normalize_string(self.name, field="vdb.name"),
        )
        object.__setattr__(
            self,
            "batch_id",
            _normalize_string(self.batch_id, field="vdb.batch_id"),
        )

    @property
    def sidecar_path(self) -> Path:
        return Path(f"{self.faiss_path}.meta.json")

    def selector(self, source: str) -> str:
        source_id = _normalize_string(source, field="vdb.source")
        return f"{source_id}:{self.name}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "batch_id": self.batch_id,
            "embedding_model_id": self.embedding_model_id,
            "faiss_path": str(self.faiss_path),
            "created_at": self.created_at.isoformat(),
        }


_STATUS_TO_LEVEL = {
    HealthStatus.OK: "info",
    HealthStatus.DEGRADED: "warning",
    HealthStatus.ERROR: "error",
    HealthStatus.UNKNOWN: "unknown",
}


@dataclass(frozen=True, slots=True)
class VdbHealthEntry:
    """Health finding surfaced alongside VDB info output."""

    code: str
    level: str
    message: str
    actions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _normalize_string(self.code, field="health.code"),
        )
        object.__setattr__(
            self,
            "level",
            _normalize_string(self.level, field="health.level"),
        )
        object.__setattr__(
            self,
            "message",
            _normalize_string(self.message, field="health.message"),
        )
        object.__setattr__(
            self,
            "actions",
            tuple(
                action.strip()
                for action in self.actions
                if action.strip()
            ),
        )

    @classmethod
    def from_report(cls, report: HealthReport) -> "VdbHealthEntry":
        level = _STATUS_TO_LEVEL.get(report.status, "unknown")
        message = report.summary or report.status.value
        return cls(
            code=report.name,
            level=level,
            message=message,
            actions=report.actions,
        )

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "level": self.level,
            "message": self.message,
        }
        if self.actions:
            payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True, slots=True)
class VdbInfoCounts:
    """Counter snapshot returned with VDB info output."""

    chunks: int = 0
    vectors: int = 0
    index: int = 0

    def __post_init__(self) -> None:
        chunks = _parse_int(self.chunks, field="counts.chunks", minimum=0)
        vectors = _parse_int(self.vectors, field="counts.vectors", minimum=0)
        index = _parse_int(self.index, field="counts.index", minimum=0)
        object.__setattr__(self, "chunks", chunks)
        object.__setattr__(self, "vectors", vectors)
        object.__setattr__(self, "index", index)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "VdbInfoCounts":
        if not payload:
            return cls()
        return cls(
            chunks=payload.get("chunks", 0),
            vectors=payload.get("vectors", 0),
            index=payload.get("index", 0),
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "chunks": self.chunks,
            "vectors": self.vectors,
            "index": self.index,
        }


@dataclass(frozen=True, slots=True)
class VdbInfoSummary:
    """Aggregated info payload rendered by `raggd vdb info`."""

    id: int
    source_id: str
    selector: str
    name: str
    batch_id: str
    embedding_model: EmbeddingModel
    metric: str
    index_type: str
    faiss_path: Path
    counts: VdbInfoCounts = field(default_factory=VdbInfoCounts)
    sidecar_path: Path | None = None
    built_at: datetime | None = None
    last_sync_at: datetime | None = None
    stale_relative_to_latest: bool = False
    health: tuple[VdbHealthEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for attr, field_name in (
            ("name", "info.name"),
            ("batch_id", "info.batch_id"),
            ("source_id", "info.source_id"),
            ("selector", "info.selector"),
        ):
            value = getattr(self, attr)
            normalized_value = _normalize_string(value, field=field_name)
            object.__setattr__(self, attr, normalized_value)

        object.__setattr__(
            self,
            "metric",
            _normalize_string(self.metric, field="info.metric"),
        )
        object.__setattr__(
            self,
            "index_type",
            _normalize_string(self.index_type, field="info.index_type"),
        )

        object.__setattr__(
            self,
            "counts",
            _normalize_counts_value(self.counts),
        )

        normalized_faiss = _normalize_pathlike(
            self.faiss_path,
            field="info.faiss_path",
        )
        object.__setattr__(self, "faiss_path", normalized_faiss)

        object.__setattr__(
            self,
            "sidecar_path",
            _resolve_sidecar_path(
                self.sidecar_path,
                faiss_path=normalized_faiss,
            ),
        )

        object.__setattr__(
            self,
            "health",
            _normalize_health_entries(self.health),
        )

        object.__setattr__(
            self,
            "built_at",
            _parse_optional_datetime(self.built_at, field="info.built_at"),
        )
        object.__setattr__(
            self,
            "last_sync_at",
            _parse_optional_datetime(
                self.last_sync_at,
                field="info.last_sync_at",
            ),
        )

    @classmethod
    def from_sources(
        cls,
        *,
        vdb: Vdb,
        source_id: str,
        embedding_model: EmbeddingModel,
        metric: str,
        index_type: str,
        counts: VdbInfoCounts | Mapping[str, Any] | None = None,
        built_at: datetime | str | None = None,
        last_sync_at: datetime | str | None = None,
        stale_relative_to_latest: bool = False,
        health: Sequence[VdbHealthEntry | Mapping[str, Any]] | None = None,
        faiss_path: Path | str | None = None,
        sidecar_path: Path | str | None = None,
    ) -> "VdbInfoSummary":
        selector = vdb.selector(source_id)
        resolved_counts: VdbInfoCounts
        if counts is None:
            resolved_counts = VdbInfoCounts()
        elif isinstance(counts, VdbInfoCounts):
            resolved_counts = counts
        else:
            resolved_counts = VdbInfoCounts.from_mapping(dict(counts))
        payload_health = tuple(health or ())
        resolved_faiss_path = faiss_path or vdb.faiss_path
        return cls(
            id=vdb.id,
            source_id=source_id,
            selector=selector,
            name=vdb.name,
            batch_id=vdb.batch_id,
            embedding_model=embedding_model,
            metric=metric,
            index_type=index_type,
            counts=resolved_counts,
            faiss_path=resolved_faiss_path,
            sidecar_path=sidecar_path,
            built_at=built_at,
            last_sync_at=last_sync_at,
            stale_relative_to_latest=bool(stale_relative_to_latest),
            health=payload_health,
        )

    def with_health_reports(
        self,
        reports: Sequence[HealthReport],
    ) -> "VdbInfoSummary":
        entries = tuple(
            VdbHealthEntry.from_report(report)
            for report in reports
        )
        return replace(self, health=entries)

    def replace(self, **updates: Any) -> "VdbInfoSummary":
        return replace(self, **updates)

    def to_mapping(self) -> dict[str, Any]:
        sidecar = (
            str(self.sidecar_path)
            if self.sidecar_path is not None
            else None
        )

        return {
            "id": self.id,
            "source_id": self.source_id,
            "selector": self.selector,
            "name": self.name,
            "batch_id": self.batch_id,
            "embedding_model": self.embedding_model.to_mapping(),
            "metric": self.metric,
            "index_type": self.index_type,
            "counts": self.counts.to_mapping(),
            "faiss_path": str(self.faiss_path),
            "sidecar_path": sidecar,
            "built_at": self._serialize_datetime(self.built_at),
            "last_sync_at": self._serialize_datetime(self.last_sync_at),
            "stale_relative_to_latest": bool(self.stale_relative_to_latest),
            "health": [entry.to_mapping() for entry in self.health],
        }

    @staticmethod
    def _serialize_datetime(value: datetime | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            parsed = _parse_datetime(value, field="info.timestamp")
        else:
            parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
