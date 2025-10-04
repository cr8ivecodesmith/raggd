from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from raggd.cli import create_app
from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.source import SourceConfigStore
from raggd.source.models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)


_runner = CliRunner()


def _write_manifest(paths: WorkspacePaths, *, status: SourceHealthStatus) -> None:
    manifest = SourceManifest(
        name="demo",
        path=paths.sources_dir / "demo",
        enabled=True,
        target=paths.workspace / "data",
        last_refresh_at=datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc),
        last_health=SourceHealthSnapshot(
            status=status,
            checked_at=datetime(2025, 10, 5, 12, 15, tzinfo=timezone.utc),
            summary="All systems nominal" if status is SourceHealthStatus.OK else "Needs attention",
            actions=("Review workspace setup",),
        ),
    )
    manifest_path = paths.source_manifest_path("demo")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )


def _prepare_workspace(
    tmp_path: Path,
    *,
    include_manifest: bool = True,
    status: SourceHealthStatus = SourceHealthStatus.OK,
) -> tuple[Path, WorkspacePaths]:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = resolve_workspace(workspace_override=workspace)

    source_dir = paths.sources_dir / "demo"
    source_dir.mkdir(parents=True, exist_ok=True)
    data_dir = paths.workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    store = SourceConfigStore(config_path=paths.config_file)
    store.upsert(
        WorkspaceSourceConfig(
            name="demo",
            path=source_dir,
            enabled=True,
            target=data_dir,
        )
    )

    if include_manifest:
        _write_manifest(paths, status=status)
    else:
        manifest_path = paths.source_manifest_path("demo")
        manifest_path.unlink(missing_ok=True)

    return workspace, paths


def test_checkhealth_generates_health_document(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "source: ok" in result.stdout
    assert "  - demo: ok" in result.stdout

    document_path = workspace / ".health.json"
    assert document_path.exists()
    payload = json.loads(document_path.read_text(encoding="utf-8"))
    assert payload["source"]["status"] == "ok"
    [detail] = payload["source"]["details"]
    assert detail["name"] == "demo"
    assert detail["status"] == "ok"


def test_checkhealth_accepts_module_filter(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth", "source"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "source: ok" in result.stdout


def test_checkhealth_reports_unknown_module(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth", "unknown"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Unknown module(s): unknown" in result.stdout


def test_checkhealth_handles_missing_manifest(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path, include_manifest=False)
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "source: unknown" in result.stdout
    assert "Manifest missing" in result.stdout

    payload = json.loads((workspace / ".health.json").read_text(encoding="utf-8"))
    assert payload["source"]["status"] == "unknown"
