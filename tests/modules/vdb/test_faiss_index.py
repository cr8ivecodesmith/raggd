"""Unit tests for the FAISS index adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

try:  # pragma: no cover - exercised via pytest skip path
    import faiss
except ImportError:  # pragma: no cover - skip when extra missing
    faiss = None  # type: ignore[assignment]

try:  # pragma: no cover - numpy is required only when exercising adapter
    import numpy as np
except ImportError:  # pragma: no cover - skip when numpy missing
    np = None  # type: ignore[assignment]

EXTRA_MISSING = faiss is None or np is None
SKIP_REASON = "faiss+numpy extras missing"

pytestmark = pytest.mark.skipif(EXTRA_MISSING, reason=SKIP_REASON)
skip_if_missing = pytest.mark.skipif(EXTRA_MISSING, reason=SKIP_REASON)

if faiss is not None and np is not None:
    from raggd.modules.vdb import (
        FaissIndex,
        FaissIndexError,
        FaissIndexLockTimeoutError,
        FaissIndexMetric,
        FaissIndexRemoveError,
        index_lock_path,
        index_writer_lock,
        persist_index_artifacts,
        sidecar_path_for_index,
    )
else:  # pragma: no cover - tests skipped when dependencies missing
    FaissIndex = None  # type: ignore[assignment]
    FaissIndexError = None  # type: ignore[assignment]
    FaissIndexLockTimeoutError = None  # type: ignore[assignment]
    FaissIndexMetric = None  # type: ignore[assignment]
    FaissIndexRemoveError = None  # type: ignore[assignment]
    index_lock_path = None  # type: ignore[assignment]
    index_writer_lock = None  # type: ignore[assignment]
    persist_index_artifacts = None  # type: ignore[assignment]
    sidecar_path_for_index = None  # type: ignore[assignment]


@skip_if_missing
def test_create_add_search_roundtrip() -> None:
    index = FaissIndex.create(
        dim=3,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    assert isinstance(index.metric, FaissIndexMetric)
    assert index.metric.name == "cosine"
    index.add(
        ids=[10, 20, 30],
        vectors=[
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
    )
    assert index.size == 3
    distances, ids = index.search([[1.0, 0.0, 0.0]], k=2)
    np.testing.assert_allclose(distances[0, 0], 1.0, rtol=1e-6)
    assert ids.tolist() == [[10, 20]]


@skip_if_missing
def test_add_with_mismatched_dimensions_raises() -> None:
    index = FaissIndex.create(
        dim=4,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        index.add(ids=[1], vectors=[[1.0, 2.0, 3.0]])


@skip_if_missing
def test_remove_ids_and_size() -> None:
    index = FaissIndex.create(
        dim=2,
        metric="ip",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    vectors = [[0.1, 0.2], [0.2, 0.3]]
    index.add(ids=[1, 2], vectors=vectors)
    assert index.size == 2
    index.remove([1])
    assert index.size == 1
    with pytest.raises(FaissIndexRemoveError):
        index.remove([99])


@skip_if_missing
def test_serialization_roundtrip_preserves_index() -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    index.add(ids=[7], vectors=[[1.0, 0.0]])
    payload = index.to_bytes()
    loaded = FaissIndex.from_bytes(payload, metric="cosine")
    assert loaded.size == 1
    distances, ids = loaded.search([[1.0, 0.0]], k=1)
    np.testing.assert_allclose(distances[0, 0], 1.0, rtol=1e-6)
    assert ids[0, 0] == 7


@skip_if_missing
def test_search_with_empty_queries_returns_empty_arrays() -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    distances, ids = index.search([], k=3)
    assert distances.shape == (0, 3)
    assert ids.shape == (0, 3)


@skip_if_missing
def test_from_bytes_without_idmap_raises_error() -> None:
    # Build a vanilla index without IDMap to ensure the adapter rejects it.
    plain = faiss.index_factory(2, "Flat", faiss.METRIC_INNER_PRODUCT)  # type: ignore[union-attr]
    serialized = faiss.serialize_index(plain)
    with pytest.raises(FaissIndexError):
        FaissIndex.from_bytes(serialized, metric="cosine")


@skip_if_missing
def test_add_and_search_accept_numpy_inputs() -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="Flat",
    )  # type: ignore[union-attr]
    ids = np.array([11, 12], dtype="int64")
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
    index.add(ids=ids, vectors=vectors)
    distances, ids_out = index.search(
        np.array([[0.0, 1.0]], dtype="float32"),
        k=1,
    )
    np.testing.assert_allclose(distances[0, 0], 1.0, rtol=1e-6)
    assert ids_out[0, 0] == 12


@skip_if_missing
def test_remove_ignores_empty_iterables() -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="Flat",
    )  # type: ignore[union-attr]
    index.add(ids=[1], vectors=[[1.0, 0.0]])
    index.remove([])
    assert index.size == 1


@skip_if_missing
def test_persist_index_artifacts_writes_index_and_sidecar(tmp_path) -> None:
    index = FaissIndex.create(
        dim=3,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    vectors = np.eye(3, dtype="float32")
    index.add(ids=[101, 102, 103], vectors=vectors)

    built_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    index_path = tmp_path / "index.faiss"
    metadata = persist_index_artifacts(  # type: ignore[union-attr]
        index,
        index_path=index_path,
        provider="openai",
        model_id=7,
        model_name="text-embedding-3-small",
        index_type="IDMap,Flat",
        built_at=built_at,
        vdb_id=42,
    )

    assert index_path.exists()
    payload = index_path.read_bytes()
    assert payload  # ensure non-empty write

    expected_checksum = hashlib.sha256(payload).hexdigest()
    assert metadata.checksum == expected_checksum
    assert metadata.vector_count == index.size
    assert metadata.metric == "cosine"

    sidecar_path = sidecar_path_for_index(index_path)  # type: ignore[union-attr]
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["dim"] == 3
    assert sidecar["metric"] == "cosine"
    assert sidecar["vector_count"] == 3
    assert sidecar["vdb_id"] == 42
    assert sidecar["checksum"] == expected_checksum
    assert sidecar["built_at"] == "2025-01-02T03:04:05Z"

    temp_artifacts = [
        candidate
        for candidate in tmp_path.iterdir()
        if candidate.name.startswith(".faiss-")
    ]
    assert temp_artifacts == []


@skip_if_missing
def test_persist_index_artifacts_releases_lock(tmp_path) -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    index.add(ids=[1], vectors=[[1.0, 0.0]])

    built_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    index_path = tmp_path / "index.faiss"

    persist_index_artifacts(  # type: ignore[union-attr]
        index,
        index_path=index_path,
        provider="openai",
        model_id=1,
        model_name="text-embedding-3-small",
        index_type="IDMap,Flat",
        built_at=built_at,
        vdb_id=7,
        lock_timeout=0.1,
        lock_poll_interval=0.01,
    )

    lock_path = index_lock_path(index_path)  # type: ignore[union-attr]
    assert not lock_path.exists()


@skip_if_missing
def test_persist_index_artifacts_times_out_when_lock_held(tmp_path) -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    index.add(ids=[1], vectors=[[1.0, 0.0]])

    built_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    index_path = tmp_path / "index.faiss"

    with index_writer_lock(  # type: ignore[union-attr]
        index_path,
        timeout=0.1,
        poll_interval=0.01,
    ):
        with pytest.raises(FaissIndexLockTimeoutError):  # type: ignore[union-attr]
            persist_index_artifacts(
                index,
                index_path=index_path,
                provider="openai",
                model_id=1,
                model_name="text-embedding-3-small",
                index_type="IDMap,Flat",
                built_at=built_at,
                vdb_id=7,
                lock_timeout=0.05,
                lock_poll_interval=0.01,
            )


@skip_if_missing
def test_persist_index_artifacts_accepts_preacquired_lock(tmp_path) -> None:
    index = FaissIndex.create(
        dim=2,
        metric="cosine",
        index_type="IDMap,Flat",
    )  # type: ignore[union-attr]
    index.add(ids=[1], vectors=[[1.0, 0.0]])

    built_at = datetime(2025, 1, 3, tzinfo=timezone.utc)
    index_path = tmp_path / "index.faiss"

    with index_writer_lock(  # type: ignore[union-attr]
        index_path,
        timeout=0.1,
        poll_interval=0.01,
    ) as lock:
        lock_path = index_lock_path(index_path)  # type: ignore[union-attr]
        assert lock_path.exists()
        persist_index_artifacts(
            index,
            index_path=index_path,
            provider="openai",
            model_id=1,
            model_name="text-embedding-3-small",
            index_type="IDMap,Flat",
            built_at=built_at,
            vdb_id=7,
            lock=lock,
        )
        assert lock_path.exists()

    assert not index_lock_path(index_path).exists()  # type: ignore[union-attr]


@skip_if_missing
def test_sidecar_path_for_index_appends_suffix(tmp_path) -> None:
    index_path = tmp_path / "foo" / "index.faiss"
    expected = tmp_path / "foo" / "index.faiss.meta.json"
    assert sidecar_path_for_index(index_path) == expected  # type: ignore[union-attr]


@skip_if_missing
def test_index_lock_path_appends_suffix(tmp_path) -> None:
    index_path = tmp_path / "foo" / "index.faiss"
    expected = tmp_path / "foo" / "index.faiss.lock"
    assert index_lock_path(index_path) == expected  # type: ignore[union-attr]
