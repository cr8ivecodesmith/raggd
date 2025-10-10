"""FAISS index adapter hiding IDMap setup and common operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from contextlib import contextmanager

try:  # pragma: no cover - import guard exercised in tests via functionality
    import faiss
except ImportError as exc:  # pragma: no cover - bubble missing optional extra
    message = (
        "faiss is required for vector index operations; install the 'vdb' extra"
    )
    raise ImportError(message) from exc

from raggd.modules.manifest.locks import (
    FileLock,
    ManifestLockError,
    ManifestLockTimeoutError,
)


__all__ = [
    "FaissIndex",
    "FaissIndexError",
    "FaissIndexLoadError",
    "FaissIndexLockError",
    "FaissIndexLockTimeoutError",
    "FaissIndexMetric",
    "FaissIndexPersistenceError",
    "FaissIndexRemoveError",
    "FaissIndexSidecar",
    "FaissIndexValidationError",
    "index_lock_path",
    "index_writer_lock",
    "load_index_artifacts",
    "persist_index_artifacts",
    "sidecar_path_for_index",
]


class FaissIndexError(RuntimeError):
    """Base error raised for FAISS adapter failures."""


class FaissIndexRemoveError(FaissIndexError):
    """Raised when removal fails (e.g., ids not present)."""


class FaissIndexPersistenceError(FaissIndexError):
    """Raised when index artifacts cannot be persisted."""


class FaissIndexLockError(FaissIndexError):
    """Raised when acquiring or releasing an index lock fails."""

    def __init__(
        self,
        *,
        index_path: Path,
        lock_path: Path,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.index_path = index_path
        self.lock_path = lock_path
        self.__cause__ = cause


class FaissIndexLockTimeoutError(FaissIndexLockError):
    """Raised when acquiring an index lock times out."""


class FaissIndexLoadError(FaissIndexError):
    """Raised when index artifacts cannot be loaded from disk."""

    def __init__(
        self,
        *,
        index_path: Path,
        message: str,
        sidecar_path: Path | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.index_path = index_path
        self.sidecar_path = sidecar_path
        self.__cause__ = cause


class FaissIndexValidationError(FaissIndexError):
    """Raised when persisted artifacts fail validation checks."""

    def __init__(
        self,
        *,
        index_path: Path,
        sidecar_path: Path,
        field: str,
        expected: Any,
        actual: Any,
        message: str | None = None,
    ) -> None:
        default = (
            "Validation failed for "
            f"{field}: expected {expected!r}, got {actual!r}"
        )
        detail = message or default
        super().__init__(detail)
        self.index_path = index_path
        self.sidecar_path = sidecar_path
        self.field = field
        self.expected = expected
        self.actual = actual


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
    def from_bytes(
        cls,
        data: bytes | np.ndarray,
        *,
        metric: str,
    ) -> "FaissIndex":
        metric_descriptor = FaissIndexMetric.from_name(metric)
        if isinstance(data, (bytes, bytearray, memoryview)):
            buffer = np.frombuffer(data, dtype="uint8")
            raw_index = faiss.deserialize_index(buffer)
        else:
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

    @classmethod
    def from_json(cls, payload: str) -> "FaissIndexSidecar":
        """Parse metadata from a JSON payload."""

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid sidecar JSON payload") from exc
        if not isinstance(data, Mapping):  # pragma: no cover - defensive guard
            raise ValueError("Sidecar JSON must decode to an object")
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FaissIndexSidecar":
        """Build sidecar metadata from a mapping."""

        version = _coerce_int(
            payload.get("version"),
            field="sidecar.version",
            minimum=1,
        )
        provider = _coerce_string(
            payload.get("provider"),
            field="sidecar.provider",
        )
        model_id = _coerce_int(
            payload.get("model_id"),
            field="sidecar.model_id",
            minimum=1,
        )
        model_name = _coerce_string(
            payload.get("model_name"),
            field="sidecar.model_name",
        )
        dim = _coerce_int(
            payload.get("dim"),
            field="sidecar.dim",
            minimum=1,
        )
        metric_value = _coerce_string(
            payload.get("metric"),
            field="sidecar.metric",
        )
        metric_descriptor = FaissIndexMetric.from_name(metric_value)
        index_type = _coerce_string(
            payload.get("index_type"),
            field="sidecar.index_type",
        )
        vector_count = _coerce_int(
            payload.get("vector_count"),
            field="sidecar.vector_count",
            minimum=0,
        )
        built_at_raw = payload.get("built_at")
        if built_at_raw is None:
            raise ValueError("sidecar.built_at is required")
        built_at = _parse_timestamp(built_at_raw)
        checksum = _coerce_string(
            payload.get("checksum"),
            field="sidecar.checksum",
        )
        if len(checksum) != 64:
            raise ValueError(
                "sidecar.checksum must be a 64-character hex digest",
            )
        vdb_id = _coerce_int(
            payload.get("vdb_id"),
            field="sidecar.vdb_id",
            minimum=1,
        )

        return cls(
            version=version,
            provider=provider,
            model_id=model_id,
            model_name=model_name,
            dim=dim,
            metric=metric_descriptor.name,
            index_type=index_type,
            vector_count=vector_count,
            built_at=built_at,
            checksum=checksum,
            vdb_id=vdb_id,
        )


_DEFAULT_LOCK_TIMEOUT = 30.0
_DEFAULT_LOCK_POLL_INTERVAL = 0.1


def index_lock_path(index_path: Path) -> Path:
    """Return the filesystem lock path for ``index_path``."""

    return index_path.with_name(f"{index_path.name}.lock")


@contextmanager
def index_writer_lock(
    index_path: Path,
    *,
    timeout: float = _DEFAULT_LOCK_TIMEOUT,
    poll_interval: float = _DEFAULT_LOCK_POLL_INTERVAL,
) -> Iterator[FileLock]:
    """Serialize writes to the FAISS index file for ``index_path``."""

    lock = _acquire_index_lock(
        index_path=index_path,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    try:
        yield lock
    finally:
        _release_index_lock(lock=lock, index_path=index_path)


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
    lock: FileLock | None = None,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    lock_poll_interval: float = _DEFAULT_LOCK_POLL_INTERVAL,
) -> FaissIndexSidecar:
    """Persist the FAISS index and sidecar metadata atomically."""

    active_lock = lock
    release_lock = False
    if active_lock is None:
        active_lock = _acquire_index_lock(
            index_path=index_path,
            timeout=lock_timeout,
            poll_interval=lock_poll_interval,
        )
        release_lock = True

    try:
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
            # Avoid mismatched artifacts when sidecar writing fails after the
            # index is persisted.
            if index_path.exists():
                try:
                    index_path.unlink()
                except OSError:
                    pass
            message = (
                "Failed to persist FAISS index artifacts under "
                f"{directory}: {exc}"
            )
            raise FaissIndexPersistenceError(message) from exc
    finally:
        if release_lock and active_lock is not None:
            _release_index_lock(lock=active_lock, index_path=index_path)

    return sidecar


def sidecar_path_for_index(index_path: Path) -> Path:
    """Derive the sidecar metadata path from the FAISS index path."""

    return index_path.with_name(f"{index_path.name}.meta.json")


def load_index_artifacts(
    *,
    index_path: Path,
    sidecar_path: Path | None = None,
    expected_dim: int | None = None,
    expected_metric: str | None = None,
    validate_checksum: bool = True,
) -> tuple[FaissIndex, FaissIndexSidecar]:
    """Load FAISS index artifacts and validate against metadata."""

    resolved_sidecar = sidecar_path or sidecar_path_for_index(index_path)
    _ensure_index_exists(index_path=index_path, sidecar_path=resolved_sidecar)
    _ensure_sidecar_exists(index_path=index_path, sidecar_path=resolved_sidecar)

    sidecar = _load_sidecar(
        sidecar_path=resolved_sidecar,
        index_path=index_path,
    )
    index_bytes = _load_index_bytes(
        index_path=index_path,
        sidecar_path=resolved_sidecar,
    )

    if validate_checksum:
        _assert_checksum_matches(
            index_bytes=index_bytes,
            sidecar=sidecar,
            index_path=index_path,
            sidecar_path=resolved_sidecar,
        )

    index = _deserialize_index(
        index_bytes=index_bytes,
        metric=sidecar.metric,
        index_path=index_path,
        sidecar_path=resolved_sidecar,
    )

    _assert_index_consistency(
        index=index,
        sidecar=sidecar,
        index_path=index_path,
        sidecar_path=resolved_sidecar,
    )
    _assert_expected_values(
        index=index,
        sidecar=sidecar,
        expected_dim=expected_dim,
        expected_metric=expected_metric,
        index_path=index_path,
        sidecar_path=resolved_sidecar,
    )

    return index, sidecar


def _ensure_index_exists(*, index_path: Path, sidecar_path: Path) -> None:
    if index_path.exists():
        return
    raise FaissIndexLoadError(
        index_path=index_path,
        sidecar_path=sidecar_path,
        message=f"FAISS index not found at {index_path}",
    )


def _ensure_sidecar_exists(*, index_path: Path, sidecar_path: Path) -> None:
    if sidecar_path.exists():
        return
    raise FaissIndexLoadError(
        index_path=index_path,
        sidecar_path=sidecar_path,
        message=f"Sidecar metadata not found at {sidecar_path}",
    )


def _load_sidecar(*, sidecar_path: Path, index_path: Path) -> FaissIndexSidecar:
    try:
        payload = sidecar_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FaissIndexLoadError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            message=f"Failed reading sidecar metadata: {exc}",
            cause=exc,
        ) from exc

    try:
        return FaissIndexSidecar.from_json(payload)
    except ValueError as exc:
        raise FaissIndexLoadError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            message=f"Invalid sidecar metadata at {sidecar_path}: {exc}",
            cause=exc,
        ) from exc


def _load_index_bytes(*, index_path: Path, sidecar_path: Path) -> bytes:
    try:
        payload = index_path.read_bytes()
    except OSError as exc:
        raise FaissIndexLoadError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            message=f"Failed reading FAISS index: {exc}",
            cause=exc,
        ) from exc

    if not payload:
        raise FaissIndexLoadError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            message=f"FAISS index at {index_path} is empty",
        )
    return payload


def _assert_checksum_matches(
    *,
    index_bytes: bytes,
    sidecar: FaissIndexSidecar,
    index_path: Path,
    sidecar_path: Path,
) -> None:
    digest = hashlib.sha256(index_bytes).hexdigest()
    if digest == sidecar.checksum:
        return
    raise FaissIndexValidationError(
        index_path=index_path,
        sidecar_path=sidecar_path,
        field="checksum",
        expected=sidecar.checksum,
        actual=digest,
        message=(
            "Checksum mismatch between FAISS index and sidecar metadata "
            f"at {index_path}"
        ),
    )


def _deserialize_index(
    *,
    index_bytes: bytes,
    metric: str,
    index_path: Path,
    sidecar_path: Path,
) -> FaissIndex:
    try:
        return FaissIndex.from_bytes(index_bytes, metric=metric)
    except (FaissIndexError, ValueError) as exc:
        raise FaissIndexLoadError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            message=f"Failed deserializing FAISS index: {exc}",
            cause=exc,
        ) from exc


def _assert_index_consistency(
    *,
    index: FaissIndex,
    sidecar: FaissIndexSidecar,
    index_path: Path,
    sidecar_path: Path,
) -> None:
    if index.dim != sidecar.dim:
        raise FaissIndexValidationError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            field="dim",
            expected=sidecar.dim,
            actual=index.dim,
            message=(
                "Dimension mismatch between FAISS index and sidecar metadata "
                f"at {index_path}"
            ),
        )
    if index.size != sidecar.vector_count:
        raise FaissIndexValidationError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            field="vector_count",
            expected=sidecar.vector_count,
            actual=index.size,
            message=(
                "Vector count mismatch between FAISS index and "
                "sidecar metadata at "
                f"{index_path}"
            ),
        )


def _assert_expected_values(
    *,
    index: FaissIndex,
    sidecar: FaissIndexSidecar,
    expected_dim: int | None,
    expected_metric: str | None,
    index_path: Path,
    sidecar_path: Path,
) -> None:
    if expected_dim is not None and expected_dim != sidecar.dim:
        raise FaissIndexValidationError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            field="dim",
            expected=expected_dim,
            actual=sidecar.dim,
            message=(
                f"Expected dimension {expected_dim} did not match sidecar dim "
                f"{sidecar.dim} at {index_path}"
            ),
        )

    if expected_metric is None:
        return

    try:
        metric_name = FaissIndexMetric.from_name(expected_metric).name
    except ValueError as exc:
        raise FaissIndexValidationError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            field="metric",
            expected=expected_metric,
            actual=sidecar.metric,
            message=f"Unsupported expected metric {expected_metric!r}: {exc}",
        ) from exc

    if metric_name != index.metric.name:
        raise FaissIndexValidationError(
            index_path=index_path,
            sidecar_path=sidecar_path,
            field="metric",
            expected=metric_name,
            actual=index.metric.name,
            message=(
                f"Expected metric {metric_name!r} did not match index metric "
                f"{index.metric.name!r} at {index_path}"
            ),
        )


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


def _acquire_index_lock(
    *,
    index_path: Path,
    timeout: float,
    poll_interval: float,
) -> FileLock:
    lock_path = index_lock_path(index_path)
    lock = FileLock(
        path=lock_path,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    try:
        lock.acquire()
    except ManifestLockTimeoutError as exc:
        raise FaissIndexLockTimeoutError(
            index_path=index_path,
            lock_path=lock_path,
            message=f"Timed out acquiring index lock at {lock_path}",
            cause=exc,
        ) from exc
    except ManifestLockError as exc:
        raise FaissIndexLockError(
            index_path=index_path,
            lock_path=lock_path,
            message=f"Failed acquiring index lock at {lock_path}: {exc}",
            cause=exc,
        ) from exc
    return lock


def _release_index_lock(*, lock: FileLock, index_path: Path) -> None:
    try:
        lock.release()
    except ManifestLockError as exc:
        lock_path = lock.path
        raise FaissIndexLockError(
            index_path=index_path,
            lock_path=lock_path,
            message=f"Failed releasing index lock at {lock_path}: {exc}",
            cause=exc,
        ) from exc


def _coerce_string(value: Any, *, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    return text


def _coerce_int(
    value: Any,
    *,
    field: str,
    minimum: int | None = None,
) -> int:
    if value is None:
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):  # Avoid treating booleans as integers
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return parsed


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("sidecar.built_at cannot be empty")
        normalized = text
        if text.endswith("Z"):
            normalized = text[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(
                f"sidecar.built_at must be an ISO-8601 timestamp: {text}",
            ) from exc
    else:
        raise ValueError("sidecar.built_at must be a string or datetime")

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp


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
