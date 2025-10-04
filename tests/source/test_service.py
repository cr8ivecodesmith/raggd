from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from structlog import get_logger
from structlog.testing import capture_logs

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.source import (
    SourceConfigStore,
    SourceDisabledError,
    SourceDirectoryConflictError,
    SourceExistsError,
    SourceHealthCheckError,
    SourceHealthStatus,
    SourceNotFoundError,
    SourceService,
)
from raggd.source.models import SourceHealthSnapshot


class StubHealthEvaluator:
    """Test double for deterministic health evaluations."""

    def __init__(self) -> None:
        self.status = SourceHealthStatus.OK

    def __call__(
        self,
        *,
        config,
        manifest,
    ) -> SourceHealthSnapshot:
        return SourceHealthSnapshot(status=self.status)


def _make_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def _make_service(
    tmp_path: Path,
    health: StubHealthEvaluator,
    *,
    logger=None,
) -> tuple[SourceService, WorkspacePaths]:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    store = SourceConfigStore(config_path=paths.config_file)
    fixed_now = datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)
    service = SourceService(
        workspace=paths,
        config_store=store,
        health_evaluator=health,
        now=lambda: fixed_now,
        logger=logger,
    )
    return service, paths


def test_init_creates_source_without_target(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    state = service.init("Demo")

    assert state.config.name == "demo"
    assert state.config.enabled is False
    manifest_path = paths.source_manifest_path("demo")
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["enabled"] is False
    assert manifest["target"] is None
    assert manifest["last_refresh_at"] is None


def test_init_with_target_enables_and_refreshes(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)

    state = service.init("demo", target=target_dir)

    assert state.config.enabled is True
    assert state.config.target == target_dir
    assert state.manifest.last_refresh_at == datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)
    manifest_path = paths.source_manifest_path("demo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["enabled"] is True
    assert manifest["target"] == str(target_dir)


def test_init_rejects_duplicate_name(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    service.init("demo")

    with pytest.raises(SourceExistsError):
        service.init("demo")


def test_init_detects_directory_conflict(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    orphan_dir = paths.source_dir("demo")
    orphan_dir.mkdir()

    with pytest.raises(SourceDirectoryConflictError):
        service.init("demo")


def test_set_target_requires_enabled_or_force(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("demo")

    target_dir = paths.workspace / "sources-data"
    target_dir.mkdir(parents=True)

    with pytest.raises(SourceDisabledError):
        service.set_target("demo", target_dir)

    service.enable("demo")
    result = service.set_target("demo", target_dir)

    assert result.config.target == target_dir
    assert result.manifest.target == target_dir
    assert result.manifest.last_refresh_at == datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)


def test_set_target_can_clear_target(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    cleared = service.set_target("demo", None)

    assert cleared.config.target is None
    assert cleared.manifest.target is None


def test_refresh_disables_source_on_failed_health(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.DEGRADED
    with pytest.raises(SourceHealthCheckError):
        service.refresh("demo")

    config = service.list()[0].config
    assert config.enabled is False
    manifest = json.loads(paths.source_manifest_path("demo").read_text(encoding="utf-8"))
    assert manifest["enabled"] is False
    assert manifest["last_health"]["status"] == "degraded"

    # Forced refresh proceeds even when disabled/unhealthy.
    state = service.refresh("demo", force=True)
    assert state.manifest.last_refresh_at == datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)


def test_set_target_blocks_when_health_fails_without_force(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    replacement = paths.workspace / "data" / "replacement"
    replacement.mkdir(parents=True)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.set_target("demo", replacement)

    [state] = service.list()
    assert state.config.enabled is False

    manifest_data = json.loads(paths.source_manifest_path("demo").read_text(encoding="utf-8"))
    assert manifest_data["enabled"] is False
    assert manifest_data["last_health"]["status"] == "error"


def test_refresh_logs_auto_disable_event(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    with capture_logs() as logs:
        service, paths = _make_service(
            tmp_path,
            health,
            logger=get_logger(__name__),
        )

        target_dir = paths.workspace / "data" / "demo"
        target_dir.mkdir(parents=True)
        service.init("demo", target=target_dir)

        health.status = SourceHealthStatus.ERROR

        with pytest.raises(SourceHealthCheckError):
            service.refresh("demo")

    events = [entry for entry in logs if entry.get("event") == "source-auto-disabled"]
    assert len(events) == 1
    payload = events[0]
    assert payload["source"] == "demo"
    assert payload["status"] == "error"


def test_set_target_force_allows_remediation_after_health_failure(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    replacement = paths.workspace / "data" / "replacement"
    replacement.mkdir(parents=True)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.refresh("demo")

    health.status = SourceHealthStatus.OK

    state = service.set_target("demo", replacement, force=True)

    assert state.config.target == replacement
    assert state.manifest.target == replacement
    assert state.manifest.last_refresh_at == datetime(2025, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert state.manifest.last_health.status == SourceHealthStatus.OK
    assert state.config.enabled is False


def test_rename_updates_configuration_and_filesystem(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    service.rename("demo", "renamed", force=True)

    names = [state.config.name for state in service.list()]
    assert names == ["renamed"]
    assert (paths.sources_dir / "renamed").exists()
    assert not (paths.sources_dir / "demo").exists()


def test_rename_same_name_is_noop(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)
    service.init("demo")
    service.enable("demo")

    state = service.rename("demo", "demo")

    assert state.config.name == "demo"


def test_rename_missing_directory_raises(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    shutil.rmtree(paths.source_dir("demo"))

    with pytest.raises(SourceDirectoryConflictError):
        service.rename("demo", "renamed", force=True)


def test_rename_target_directory_conflict(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    orphan_dir = paths.source_dir("renamed")
    orphan_dir.mkdir()

    with pytest.raises(SourceDirectoryConflictError):
        service.rename("demo", "renamed", force=True)


def test_rename_rejects_existing_name(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)
    service.init("demo")
    service.init("second")
    service.enable("demo")

    with pytest.raises(SourceExistsError):
        service.rename("demo", "second")


def test_remove_prunes_config_and_directory(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("demo")
    service.enable("demo")

    directory = paths.source_dir("demo")
    assert directory.exists()

    service.remove("demo", force=True)

    assert directory.exists() is False
    assert service.list() == []


def test_rename_blocks_when_health_fails_without_force(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.rename("demo", "renamed")

    [state] = service.list()
    assert state.config.enabled is False
    manifest_data = json.loads(paths.source_manifest_path("demo").read_text(encoding="utf-8"))
    assert manifest_data["enabled"] is False
    assert manifest_data["last_health"]["status"] == "error"


def test_remove_requires_force_when_health_fails(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)

    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    health.status = SourceHealthStatus.ERROR
    with pytest.raises(SourceHealthCheckError):
        service.remove("demo")

    manifest_data = json.loads(paths.source_manifest_path("demo").read_text(encoding="utf-8"))
    assert manifest_data["enabled"] is False
    assert manifest_data["last_health"]["status"] == "error"
    assert (paths.sources_dir / "demo").exists()


def test_remove_blocks_when_disabled(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    service.init("demo")

    with pytest.raises(SourceDisabledError):
        service.remove("demo")


def test_enable_and_disable_update_state(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    service.init("alpha")
    service.init("bravo")

    enabled_states = service.enable("alpha", "bravo")
    assert [state.config.enabled for state in enabled_states] == [True, True]
    manifest_path = paths.source_manifest_path("alpha")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["last_health"]["status"] == "ok"

    disabled_states = service.disable("alpha")
    assert disabled_states[0].config.enabled is False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["enabled"] is False


def test_refresh_requires_existing_directory(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, paths = _make_service(tmp_path, health)
    target_dir = paths.workspace / "data" / "demo"
    target_dir.mkdir(parents=True)
    service.init("demo", target=target_dir)

    shutil.rmtree(paths.source_dir("demo"))

    with pytest.raises(SourceDirectoryConflictError):
        service.refresh("demo", force=True)


def test_enable_requires_existing_source(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.enable("missing")


def test_enable_requires_names(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.enable()


def test_default_health_evaluator_reports_degraded_when_target_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)

    service.init("demo")
    [state] = service.enable("demo")

    assert state.manifest.last_health.status == SourceHealthStatus.DEGRADED

    refreshed = service.refresh("demo", force=True)
    assert refreshed.manifest.last_refresh_at is not None


def test_refresh_missing_source_raises(tmp_path: Path) -> None:
    health = StubHealthEvaluator()
    service, _ = _make_service(tmp_path, health)

    with pytest.raises(SourceNotFoundError):
        service.refresh("missing", force=True)
