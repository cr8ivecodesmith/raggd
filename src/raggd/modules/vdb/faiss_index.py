"""FAISS index adapter hiding IDMap setup and common operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:  # pragma: no cover - import guard exercised in tests via functionality
    import faiss
except ImportError as exc:  # pragma: no cover - bubble missing optional extra
    message = (
        "faiss is required for vector index operations; install the 'vdb' extra"
    )
    raise ImportError(message) from exc


__all__ = [
    "FaissIndex",
    "FaissIndexError",
    "FaissIndexMetric",
    "FaissIndexRemoveError",
    "FaissIndexPersistenceError",
    "FaissIndexSidecar",
    "persist_index_artifacts",
    "sidecar_path_for_index",
]


class FaissIndexError(RuntimeError):
    """Base error raised for FAISS adapter failures."""


class FaissIndexRemoveError(FaissIndexError):
    """Raised when removal fails (e.g., ids not present)."""


class FaissIndexPersistenceError(FaissIndexError):
    """Raised when index artifacts cannot be persisted."""


@dataclass(frozen=True)
class FaissIndexMetric:
    """Metric descriptor bridging human-readable names to FAISS IDs."""

    name: str
    faiss_metric: int

    @classmethod
    def from_name(cls, name: str) -> "FaissIndexMetric":
        normalized = name.strip().lower()
        if normalized in {"l2", "euclidean"}:
            return cls(name="l2", faiss_metric=faiss.METRIC_L2)
        if normalized in {"ip", "inner_product"}:
            return cls(name="ip", faiss_metric=faiss.METRIC_INNER_PRODUCT)
        if normalized == "cosine":
            return cls(name="cosine", faiss_metric=faiss.METRIC_INNER_PRODUCT)
        raise ValueError(f"Unsupported FAISS metric: {name!r}")


class FaissIndex:
    """Thin wrapper around ``faiss.IndexIDMap`` with typed helpers."""

    def __init__(
        self,
        *,
        index: faiss.Index,
        metric: FaissIndexMetric,
    ) -> None:
        if not isinstance(index, faiss.IndexIDMap):
            raise TypeError("index must be an instance of faiss.IndexIDMap")
        self._index = index
        self._metric = metric

    @property
    def dim(self) -> int:
        return self._index.d

    @property
    def metric(self) -> FaissIndexMetric:
        return self._metric

    @property
    def size(self) -> int:
        """Number of vectors stored in the index."""

        return self._index.ntotal

    @classmethod
    def create(
        cls,
        *,
        dim: int,
        metric: str,
        index_type: str,
    ) -> "FaissIndex":
        metric_descriptor = FaissIndexMetric.from_name(metric)
        inner_index = _build_index(
            dim=dim,
            index_type=index_type,
            metric=metric_descriptor,
        )
        wrapped = faiss.IndexIDMap(inner_index)
        return cls(index=wrapped, metric=metric_descriptor)

    @classmethod
    def from_bytes(cls, data: bytes, *, metric: str) -> "FaissIndex":
        metric_descriptor = FaissIndexMetric.from_name(metric)
        raw_index = faiss.deserialize_index(data)
        if not isinstance(raw_index, faiss.IndexIDMap):
            raise FaissIndexError(
                "Serialized index must wrap an IDMap; rebuild with IDMap,Flat",
            )
        return cls(index=raw_index, metric=metric_descriptor)

    def to_bytes(self) -> bytes:
        return faiss.serialize_index(self._index)

    def add(
        self,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        id_array = _ids_to_array(ids)
        if id_array.size == 0:
            return
        vector_array = _vectors_to_array(vectors, dim=self.dim)
        if len(vector_array) == 0:
            return
        if len(id_array) != len(vector_array):
            raise ValueError("ids and vectors must have matching lengths")
        self._index.add_with_ids(vector_array, id_array)

    def remove(self, ids: Iterable[int]) -> None:
        id_array = _ids_to_array(ids)
        if id_array.size == 0:
            return
        id_selector = faiss.IDSelectorBatch(id_array)
        removed_count = self._index.remove_ids(id_selector)
        if removed_count < id_array.size:
            missing = id_array.size - removed_count
            raise FaissIndexRemoveError(
                f"Failed to remove {missing} ids from index",
            )

    def search(
        self,
        query_vectors: Sequence[Sequence[float]],
        *,
        k: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        if k <= 0:
            raise ValueError("k must be positive")
        queries = _vectors_to_array(query_vectors, dim=self.dim)
        if queries.shape[0] == 0:
            return (
                np.empty((0, k), dtype="float32"),
                np.empty((0, k), dtype="int64"),
            )
        distances, ids = self._index.search(queries, k)
        return distances, ids

    def reconstruct(self, ids: Iterable[int]) -> np.ndarray:
        id_array = _ids_to_array(ids)
        if id_array.size == 0:
            return np.zeros((0, self.dim), dtype="float32")
        vectors = np.zeros((id_array.size, self.dim), dtype="float32")
        for idx, identifier in enumerate(id_array):
            try:
                vectors[idx] = self._index.reconstruct(int(identifier))
            except RuntimeError as exc:  # pragma: no cover - missing identifier
                message = f"Failed to reconstruct vector for id {identifier}"
                raise FaissIndexError(message) from exc
        return vectors


@dataclass(frozen=True, slots=True)
class FaissIndexSidecar:
    """Structured payload persisted alongside the FAISS index file."""

    version: int
    provider: str
    model_id: int
    model_name: str
    dim: int
    metric: str
    index_type: str
    vector_count: int
    built_at: datetime
    checksum: str
    vdb_id: int

    def to_json(self) -> str:
        """Serialize metadata to a formatted JSON string."""

        payload = {
            "version": self.version,
            "provider": self.provider,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "dim": self.dim,
            "metric": self.metric,
            "index_type": self.index_type,
            "vector_count": self.vector_count,
            "built_at": _format_timestamp(self.built_at),
            "checksum": self.checksum,
            "vdb_id": self.vdb_id,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def persist_index_artifacts(
    index: FaissIndex,
    *,
    index_path: Path,
    provider: str,
    model_id: int,
    model_name: str,
    index_type: str,
    built_at: datetime,
    vdb_id: int,
    version: int = 1,
) -> FaissIndexSidecar:
    """Persist the FAISS index and sidecar metadata atomically."""

    index_bytes = index.to_bytes()
    checksum = hashlib.sha256(index_bytes).hexdigest()
    sidecar = FaissIndexSidecar(
        version=version,
        provider=provider,
        model_id=model_id,
        model_name=model_name,
        dim=index.dim,
        metric=index.metric.name,
        index_type=index_type,
        vector_count=index.size,
        built_at=built_at,
        checksum=checksum,
        vdb_id=vdb_id,
    )

    sidecar_path = sidecar_path_for_index(index_path)
    directory = index_path.parent
    directory.mkdir(parents=True, exist_ok=True)

    try:
        _atomic_write_bytes(index_path, index_bytes)
        _atomic_write_text(sidecar_path, sidecar.to_json())
    except OSError as exc:
        # Avoid mismatched artifacts when sidecar writing fails after the index
        # is persisted.
        if index_path.exists():
            try:
                index_path.unlink()
            except OSError:
                pass
        raise FaissIndexPersistenceError(
            f"Failed to persist FAISS index artifacts under {directory}: {exc}"
        ) from exc

    return sidecar


def sidecar_path_for_index(index_path: Path) -> Path:
    """Derive the sidecar metadata path from the FAISS index path."""

    return index_path.with_name(f"{index_path.name}.meta.json")


def _build_index(
    *,
    dim: int,
    index_type: str,
    metric: FaissIndexMetric,
) -> faiss.Index:
    parsed = index_type.strip()
    if not parsed:
        raise ValueError("index_type cannot be empty")
    normalized = parsed.replace(" ", "")
    prefix = "IDMap,"
    if normalized.lower().startswith(prefix.lower()):
        normalized = normalized[len(prefix) :]
    if not normalized:
        raise ValueError("index_type must specify base index after IDMap,")
    base_index = faiss.index_factory(dim, normalized, metric.faiss_metric)
    return base_index


def _ids_to_array(ids: Iterable[int]) -> np.ndarray:
    if isinstance(ids, np.ndarray):
        return ids.astype("int64", copy=False).reshape(-1)
    if isinstance(ids, Sequence):
        return np.asarray(ids, dtype="int64").reshape(-1)
    return np.fromiter(
        (int(identifier) for identifier in ids),
        dtype="int64",
        count=-1,
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".faiss-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _atomic_write_text(path: Path, data: str) -> None:
    temp_path: Path | None = None
    encoded = data.encode("utf-8")
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".faiss-",
            suffix=".json.tmp",
            delete=False,
        ) as handle:
            handle.write(encoded)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _format_timestamp(value: datetime) -> str:
    timestamp = value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _vectors_to_array(
    vectors: Sequence[Sequence[float]],
    *,
    dim: int,
) -> np.ndarray:
    if isinstance(vectors, np.ndarray):
        array = np.asarray(vectors, dtype="float32")
    else:
        if len(vectors) == 0:
            return np.empty((0, dim), dtype="float32")
        array = np.asarray(vectors, dtype="float32")
    if array.size == 0:
        return np.empty((0, dim), dtype="float32")
    if array.ndim != 2:
        raise ValueError("vectors must be a 2-D array of shape (n, dim)")
    if array.shape[1] != dim:
        message = (
            "Vector dimensionality mismatch: expected "
            f"{dim}, got {array.shape[1]}"
        )
        raise ValueError(message)
    return array
