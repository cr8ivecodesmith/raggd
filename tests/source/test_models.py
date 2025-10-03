from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from raggd.source import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
    source_manifest_schema,
    workspace_source_config_schema,
)


def test_workspace_source_config_defaults_and_normalization(tmp_path: Path) -> None:
    cfg = WorkspaceSourceConfig(
        name=" demo ",
        path=tmp_path / "sources" / "demo",
    )

    assert cfg.name == "demo"
    assert cfg.path == tmp_path / "sources" / "demo"
    assert cfg.enabled is False
    assert cfg.target is None

    cfg_with_target = WorkspaceSourceConfig(
        name="example",
        path=tmp_path / "sources" / "example",
        target="~/project ",
    )

    assert cfg_with_target.target == Path("~/project").expanduser()


def test_source_manifest_defaults_and_dump(tmp_path: Path) -> None:
    manifest = SourceManifest(
        name="alpha",
        path=tmp_path / "sources" / "alpha",
        enabled=True,
        target=tmp_path / "projects" / "alpha",
    )

    assert manifest.last_refresh_at is None
    assert manifest.last_health.status == SourceHealthStatus.UNKNOWN
    assert manifest.last_health.actions == ()

    dumped = manifest.model_dump(mode="json")
    assert dumped["last_health"]["status"] == "unknown"
    assert Path(dumped["target"]) == tmp_path / "projects" / "alpha"

    refreshed_at = datetime(2025, 10, 4, 12, 30, tzinfo=timezone.utc)
    manifest.last_refresh_at = refreshed_at
    assert manifest.last_refresh_at == refreshed_at


def test_source_health_snapshot_normalizes_actions() -> None:
    snapshot = SourceHealthSnapshot(actions=["  restart service  ", "check logs"])

    assert snapshot.actions == ("restart service", "check logs")


def test_workspace_source_config_schema_contains_expected_properties() -> None:
    schema = workspace_source_config_schema()

    assert schema["type"] == "object"
    for key in ("name", "path", "enabled", "target"):
        assert key in schema["properties"]


def test_source_manifest_schema_references_nested_health_snapshot() -> None:
    schema = source_manifest_schema()

    assert schema["type"] == "object"
    assert "last_health" in schema["properties"]
    health_schema = schema["properties"]["last_health"]
    assert "$ref" in health_schema
    ref = health_schema["$ref"]
    assert isinstance(ref, str) and ref.startswith("#/$defs")
