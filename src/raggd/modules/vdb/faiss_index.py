"""FAISS index adapter hiding IDMap setup and common operations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

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
]


class FaissIndexError(RuntimeError):
    """Base error raised for FAISS adapter failures."""


class FaissIndexRemoveError(FaissIndexError):
    """Raised when removal fails (e.g., ids not present)."""


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
