from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from raggd.modules import HealthReport, HealthStatus
from raggd.modules.vdb import (
    EmbeddingModel,
    Vdb,
    VdbHealthEntry,
    VdbInfoCounts,
    VdbInfoSummary,
)
from raggd.modules.vdb import models as vdb_models


def test_embedding_model_from_row_round_trips() -> None:
    row = {
        "id": "7",
        "provider": " openai ",
        "name": " text-embedding-3-small ",
        "dim": " 1536 ",
    }

    model = EmbeddingModel.from_row(row)

    assert model.id == 7
    assert model.provider == "openai"
    assert model.name == "text-embedding-3-small"
    assert model.dim == 1536
    assert model.key == "openai:text-embedding-3-small"
    assert model.to_mapping() == {
        "id": 7,
        "provider": "openai",
        "name": "text-embedding-3-small",
        "dim": 1536,
    }


def test_vdb_from_row_and_helpers() -> None:
    created_at = datetime(2025, 10, 10, 17, 20, 35, tzinfo=timezone.utc)
    row = {
        "id": 42,
        "name": " base ",
        "batch_id": " batch-123 ",
        "embedding_model_id": 7,
        "faiss_path": " /workspace/sources/docs/vectors/base/index.faiss ",
        "created_at": created_at.isoformat(),
    }

    vdb = Vdb.from_row(row)

    assert vdb.id == 42
    assert vdb.name == "base"
    assert vdb.batch_id == "batch-123"
    assert vdb.embedding_model_id == 7
    expected_index = Path("/workspace/sources/docs/vectors/base/index.faiss")
    assert vdb.faiss_path == expected_index
    expected_sidecar = Path(
        "/workspace/sources/docs/vectors/base/index.faiss.meta.json"
    )
    assert vdb.sidecar_path == expected_sidecar
    assert vdb.selector("docs") == "docs:base"

    mapping = vdb.to_mapping()
    assert (
        mapping["faiss_path"]
        == "/workspace/sources/docs/vectors/base/index.faiss"
    )
    assert mapping["created_at"] == created_at.isoformat()


def test_vdb_info_summary_from_sources_and_health() -> None:
    model = EmbeddingModel(
        id=3,
        provider="openai",
        name="text-embedding-3-small",
        dim=1536,
    )
    created_at = datetime(2025, 10, 10, 17, 20, 35, tzinfo=timezone.utc)
    vdb = Vdb(
        id=1,
        name="base",
        batch_id="batch-aaa",
        embedding_model_id=3,
        faiss_path=Path("/tmp/index.faiss"),
        created_at=created_at,
    )
    counts_payload = {"chunks": 1200, "vectors": 1198, "index": 1198}

    summary = VdbInfoSummary.from_sources(
        vdb=vdb,
        source_id="docs",
        embedding_model=model,
        metric="cosine",
        index_type="IDMap,Flat",
        counts=counts_payload,
        built_at="2025-10-10T17:25:00Z",
        last_sync_at=datetime(2025, 10, 10, 17, 25, tzinfo=timezone.utc),
        stale_relative_to_latest=True,
        health=(
            VdbHealthEntry(code="stale", level="warning", message="needs sync"),
        ),
    )

    assert summary.selector == "docs:base"
    assert summary.counts.chunks == 1200
    assert summary.health[0].code == "stale"
    assert summary.sidecar_path == Path("/tmp/index.faiss.meta.json")

    mapped = summary.to_mapping()
    assert mapped["embedding_model"]["name"] == "text-embedding-3-small"
    assert mapped["counts"]["vectors"] == 1198
    assert mapped["sidecar_path"] == "/tmp/index.faiss.meta.json"
    assert mapped["stale_relative_to_latest"] is True

    report = HealthReport(
        name="vdb-index",
        status=HealthStatus.OK,
        summary="healthy",
        actions=("raggd vdb sync docs --recompute",),
    )
    with_reports = summary.with_health_reports([report])

    assert with_reports.health[0].code == "vdb-index"
    assert with_reports.health[0].level == "info"
    assert with_reports.health[0].message == "healthy"
    assert with_reports.health[0].actions == (
        "raggd vdb sync docs --recompute",
    )
    with_reports_mapping = with_reports.to_mapping()
    assert with_reports_mapping["health"][0]["actions"] == [
        "raggd vdb sync docs --recompute"
    ]


def test_vdb_info_summary_sidecar_override_and_replace() -> None:
    model = EmbeddingModel(id=11, provider="openai", name="ada", dim=1024)
    now = datetime.now(timezone.utc)
    vdb = Vdb(
        id=5,
        name="custom",
        batch_id="batch-zzz",
        embedding_model_id=11,
        faiss_path=Path("/tmp/alt.faiss"),
        created_at=now,
    )

    summary = VdbInfoSummary.from_sources(
        vdb=vdb,
        source_id="api",
        embedding_model=model,
        metric="cosine",
        index_type="IDMap,Flat",
        counts=VdbInfoCounts(chunks=1, vectors=1, index=1),
        sidecar_path="/tmp/sidecar.json",
    )

    assert summary.sidecar_path == Path("/tmp/sidecar.json")

    updated = summary.replace(metric="dot", index_type="IDMap,Flat")
    assert updated.metric == "dot"
    assert updated.selector == "api:custom"

    naive = datetime(2025, 1, 1, 12, 30, 15)
    assert summary._serialize_datetime(naive) == "2025-01-01T12:30:15+00:00"
    assert (
        VdbInfoSummary._serialize_datetime(naive)
        == "2025-01-01T12:30:15+00:00"
    )
    aware = datetime(2025, 1, 1, 12, 30, 15, tzinfo=timezone.utc)
    assert (
        VdbInfoSummary._serialize_datetime(aware)
        == "2025-01-01T12:30:15+00:00"
    )
    assert (
        VdbInfoSummary._serialize_datetime("2025-01-01T12:30:15Z")
        == "2025-01-01T12:30:15+00:00"
    )
    assert VdbInfoSummary._serialize_datetime(None) is None


def test_vdb_info_summary_health_validation() -> None:
    model = EmbeddingModel(id=9, provider="openai", name="align", dim=512)
    vdb = Vdb(
        id=2,
        name="alpha",
        batch_id="batch-alpha",
        embedding_model_id=9,
        faiss_path=Path("/tmp/alpha.faiss"),
        created_at=datetime.now(timezone.utc),
    )

    summary = VdbInfoSummary.from_sources(
        vdb=vdb,
        source_id="docs",
        embedding_model=model,
        metric="cosine",
        index_type="IDMap,Flat",
        health=[{"code": "ok", "level": "info", "message": "all good"}],
    )

    assert summary.health[0].code == "ok"

    with pytest.raises(TypeError):
        VdbInfoSummary.from_sources(
            vdb=vdb,
            source_id="docs",
            embedding_model=model,
            metric="cosine",
            index_type="IDMap,Flat",
            health=[123],
        )


def test_vdb_info_summary_direct_instantiation_normalizes_inputs() -> None:
    model = EmbeddingModel(id=21, provider="openai", name="beta", dim=2048)

    summary = VdbInfoSummary(
        id=8,
        source_id="docs",
        selector="docs:beta",
        name="beta",
        batch_id="batch-beta",
        embedding_model=model,
        metric="cosine",
        index_type="IDMap,Flat",
        faiss_path=" /tmp/beta.faiss ",
        counts={"chunks": 2, "vectors": 2, "index": 2},
        health=[{"code": "warn", "level": "warning", "message": "check"}],
        last_sync_at="2025-10-10T17:25:00Z",
        sidecar_path=Path("/tmp/custom.meta.json"),
    )

    assert summary.faiss_path == Path("/tmp/beta.faiss")
    assert summary.counts.vectors == 2
    assert summary.health[0].code == "warn"
    assert summary.last_sync_at.tzinfo == timezone.utc

    naive_summary = summary.replace(
        built_at=datetime(2025, 5, 5, 8, 9, 10),
        last_sync_at=datetime(2025, 5, 5, 9, 10, 11),
    )
    mapped = naive_summary.to_mapping()
    assert mapped["built_at"].endswith("+00:00")
    assert mapped["last_sync_at"].endswith("+00:00")



@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"chunks": 5, "vectors": 4, "index": 3}, (5, 4, 3)),
        (None, (0, 0, 0)),
    ],
)
def test_vdb_info_counts_from_mapping(payload, expected) -> None:
    counts = VdbInfoCounts.from_mapping(payload)

    assert (counts.chunks, counts.vectors, counts.index) == expected
    assert counts.to_mapping() == {
        "chunks": expected[0],
        "vectors": expected[1],
        "index": expected[2],
    }


def test_vdb_health_entry_from_report() -> None:
    report = HealthReport(
        name="vdb-dim",
        status=HealthStatus.DEGRADED,
        summary="Dimension mismatch",
        actions=("raggd vdb reset docs --recompute",),
    )

    entry = VdbHealthEntry.from_report(report)

    assert entry.code == "vdb-dim"
    assert entry.level == "warning"
    assert entry.message == "Dimension mismatch"
    assert entry.actions == ("raggd vdb reset docs --recompute",)


def test_validation_helpers_cover_branches() -> None:
    assert vdb_models._parse_int(True, field="flag") == 1
    assert vdb_models._parse_int("7", field="value", minimum=0) == 7

    with pytest.raises(ValueError):
        vdb_models._parse_int("  ", field="value")
    with pytest.raises(ValueError):
        vdb_models._parse_int("abc", field="value")
    with pytest.raises(ValueError):
        vdb_models._parse_int(0, field="value", minimum=1)
    with pytest.raises(TypeError):
        vdb_models._parse_int(object(), field="value")

    dt = datetime(2025, 2, 2, 3, 4, 5)
    parsed = vdb_models._parse_datetime(dt, field="ts")
    assert parsed.tzinfo == timezone.utc
    assert (
        vdb_models._parse_datetime("2025-02-02T03:04:05Z", field="ts").tzinfo
        == timezone.utc
    )

    with pytest.raises(ValueError):
        vdb_models._parse_datetime(" ", field="ts")
    with pytest.raises(ValueError):
        vdb_models._parse_datetime("invalid", field="ts")
    with pytest.raises(TypeError):
        vdb_models._parse_datetime(123, field="ts")

    assert vdb_models._parse_optional_datetime(None, field="ts") is None

    assert vdb_models._parse_path(Path("/tmp"), field="path") == Path("/tmp")
    assert vdb_models._parse_path(" /tmp/dir ", field="path") == Path(
        "/tmp/dir"
    )
    with pytest.raises(ValueError):
        vdb_models._parse_path(" ", field="path")
    with pytest.raises(TypeError):
        vdb_models._parse_path(123, field="path")

    assert vdb_models._normalize_string(" value ", field="field") == "value"
    with pytest.raises(ValueError):
        vdb_models._normalize_string(None, field="field")
    with pytest.raises(ValueError):
        vdb_models._normalize_string(" ", field="field")

    counts_default = vdb_models._normalize_counts_value(None)
    assert counts_default.to_mapping() == {
        "chunks": 0,
        "vectors": 0,
        "index": 0,
    }

    counts_instance = VdbInfoCounts(chunks=1, vectors=2, index=3)
    assert (
        vdb_models._normalize_counts_value(counts_instance)
        is counts_instance
    )

    counts_from_mapping = vdb_models._normalize_counts_value(
        {"chunks": 4, "vectors": 5, "index": 6}
    )
    assert counts_from_mapping.to_mapping() == {
        "chunks": 4,
        "vectors": 5,
        "index": 6,
    }

    with pytest.raises(TypeError):
        vdb_models._normalize_counts_value(["bad"])

    base_index_path = Path("/tmp/example.faiss")
    resolved_sidecar = vdb_models._resolve_sidecar_path(
        None,
        faiss_path=base_index_path,
    )
    assert resolved_sidecar == Path("/tmp/example.faiss.meta.json")

    explicit_sidecar = Path("/tmp/custom.json")
    assert (
        vdb_models._resolve_sidecar_path(
            explicit_sidecar,
            faiss_path=base_index_path,
        )
        == explicit_sidecar
    )

    resolved_from_str = vdb_models._resolve_sidecar_path(
        " /tmp/sidecar.json ",
        faiss_path=base_index_path,
    )
    assert resolved_from_str == Path("/tmp/sidecar.json")

    empty_sidecar = vdb_models._resolve_sidecar_path(
        None,
        faiss_path=Path("."),
    )
    assert empty_sidecar is None
