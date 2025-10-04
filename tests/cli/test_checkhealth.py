from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from raggd.cli import create_app
from raggd.cli.checkhealth import (
    _load_app_config,
    _render_timestamp,
    _select_modules,
    register_checkhealth_command,
)
from raggd.cli.init import init_workspace
from raggd.core.config import ModuleToggle
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.health import HealthDocumentError
from raggd.modules import ModuleDescriptor, ModuleRegistry
from raggd.source import SourceConfigStore
from raggd.source.models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)


_runner = CliRunner()


def _write_manifest(
    paths: WorkspacePaths,
    *,
    status: SourceHealthStatus,
) -> None:
    manifest = SourceManifest(
        name="demo",
        path=paths.sources_dir / "demo",
        enabled=True,
        target=paths.workspace / "data",
        last_refresh_at=datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc),
        last_health=SourceHealthSnapshot(
            status=status,
            checked_at=datetime(2025, 10, 5, 12, 15, tzinfo=timezone.utc),
            summary=(
                "All systems nominal"
                if status is SourceHealthStatus.OK
                else "Needs attention"
            ),
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


def test_render_timestamp_handles_non_datetime() -> None:
    assert _render_timestamp("value") == "never"
    sentinel = type("T", (), {"isoformat": lambda self: "stamp"})()
    assert _render_timestamp(sentinel) == "stamp"


def test_select_modules_ignores_duplicates() -> None:
    registry = ModuleRegistry(
        (
            ModuleDescriptor(
                name="alpha",
                description="Alpha",
                default_toggle=ModuleToggle(enabled=True),
                health_hook=lambda _: (),
            ),
            ModuleDescriptor(
                name="beta",
                description="Beta",
                default_toggle=ModuleToggle(enabled=True),
                health_hook=lambda _: (),
            ),
        )
    )

    selected = _select_modules(registry, ["alpha", "Alpha", "beta", "alpha"])
    assert selected == ["alpha", "beta"]


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

    manifest_text = (workspace / ".health.json").read_text(encoding="utf-8")
    payload = json.loads(manifest_text)
    assert payload["source"]["status"] == "unknown"


def test_load_app_config_handles_empty_file(tmp_path: Path) -> None:
    workspace, paths = _prepare_workspace(tmp_path)
    paths.config_file.write_text("", encoding="utf-8")

    config = _load_app_config(paths)
    assert config.workspace == paths.workspace
    assert config.workspace_settings.root == paths.workspace


def test_checkhealth_requires_workspace_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Run `raggd init` first" in result.stdout


def test_checkhealth_workspace_option_rejects_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    config_file = workspace / "raggd.toml"
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth", "--workspace", str(config_file)],
        env={"HOME": str(tmp_path)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace error" in result.stdout


def test_checkhealth_handles_config_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, paths = _prepare_workspace(tmp_path)
    original = Path.read_text

    def _raising(self: Path, *args: object, **kwargs: object) -> str:
        if self == paths.config_file:
            raise OSError("boom")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raising)

    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to read config file" in result.stdout


def test_checkhealth_handles_invalid_toml(tmp_path: Path) -> None:
    workspace, paths = _prepare_workspace(tmp_path)
    paths.config_file.write_text("[workspace\n", encoding="utf-8")
    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "TOML" in result.stdout


def test_checkhealth_handles_no_registered_hooks(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = typer.Typer()
    registry = ModuleRegistry(())
    register_checkhealth_command(app, registry=registry)

    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No modules with health hooks" in result.stdout


def test_checkhealth_handles_store_load_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = _prepare_workspace(tmp_path)

    monkeypatch.setattr(
        "raggd.cli.checkhealth.HealthDocumentStore.load",
        lambda self: (_ for _ in ()).throw(HealthDocumentError("boom")),
    )

    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to load health document" in result.stdout


def test_checkhealth_handles_store_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = _prepare_workspace(tmp_path)

    def _raise_write_error(self: object, document: object) -> None:
        raise HealthDocumentError("boom")

    monkeypatch.setattr(
        "raggd.cli.checkhealth.HealthDocumentStore.write",
        _raise_write_error,
    )

    app = create_app()
    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to write health document" in result.stdout


def test_checkhealth_emits_no_details_block(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = typer.Typer()

    def _hook(_: object) -> tuple[()]:
        return ()

    registry = ModuleRegistry(
        (
            ModuleDescriptor(
                name="stub",
                description="Stub module",
                default_toggle=ModuleToggle(enabled=True),
                health_hook=lambda handle: _hook(handle),
            ),
        )
    )
    register_checkhealth_command(app, registry=registry)

    result = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "stub: ok" in result.stdout
    assert "no health entries reported" in result.stdout


def test_checkhealth_logs_carried_forward_modules(tmp_path: Path) -> None:
    workspace, _ = _prepare_workspace(tmp_path)
    app = create_app()

    first = _runner.invoke(
        app,
        ["checkhealth"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )
    assert first.exit_code == 0

    document_path = workspace / ".health.json"
    payload = json.loads(document_path.read_text(encoding="utf-8"))
    payload["legacy"] = {
        "status": "ok",
        "checked_at": datetime(2025, 10, 5, tzinfo=timezone.utc).isoformat(),
        "details": [],
    }
    document_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = _runner.invoke(
        app,
        ["checkhealth", "source"],
        env={"RAGGD_WORKSPACE": str(workspace)},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "source:" in result.stdout
