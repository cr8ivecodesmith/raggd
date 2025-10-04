"""Integration tests for the `raggd source` command group."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from raggd.cli import create_app
from raggd.cli.init import init_workspace
from raggd.cli.source import (
    _emit_health_guidance,
    _emit_state_summary,
    _format_timestamp,
    _normalize_status,
    _require_context,
    _status_color,
)
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.source import (
    SourceConfigError,
    SourceConfigStore,
    SourceError,
    SourceService,
)
from raggd.source.models import SourceHealthStatus


@pytest.fixture()
def runner() -> CliRunner:
    """Return a Typer CLI runner."""

    return CliRunner()


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, dict[str, str], WorkspacePaths]:
    """Initialize a workspace directory and return environment + paths."""

    base = tmp_path
    root = base / "workspace"
    init_workspace(workspace=root)
    env = {"HOME": str(base), "RAGGD_WORKSPACE": str(root)}
    paths = resolve_workspace(workspace_override=root)
    return root, env, paths


def test_require_context_reports_missing_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Dummy:
        pass

    ctx = _Dummy()

    with pytest.raises(typer.Exit) as excinfo:
        _require_context(ctx)  # type: ignore[arg-type]
    assert excinfo.value.exit_code == 1

    output = capsys.readouterr().out
    assert "source context not initialized" in output


def test_status_color_covers_known_health_states() -> None:
    assert _status_color(SourceHealthStatus.OK) == typer.colors.GREEN
    assert _status_color("ok") == typer.colors.GREEN
    assert _status_color(SourceHealthStatus.UNKNOWN) == typer.colors.YELLOW
    assert _status_color("unknown") == typer.colors.YELLOW
    assert _status_color(SourceHealthStatus.DEGRADED) == (
        typer.colors.BRIGHT_YELLOW
    )
    assert _status_color(SourceHealthStatus.ERROR) == typer.colors.RED
    assert _status_color("error") == typer.colors.RED
    assert _status_color("bogus") is None


def test_helpers_handle_none_and_unrecognized_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _format_timestamp(None) == "never"
    assert _normalize_status("bogus") is SourceHealthStatus.UNKNOWN

    fake_state = SimpleNamespace(
        config=SimpleNamespace(
            name="demo",
            enabled=True,
            path=Path("/tmp/demo"),
            target=None,
        ),
        manifest=SimpleNamespace(
            last_health=SimpleNamespace(
                status="bogus",
                summary=None,
                actions=(),
            ),
            last_refresh_at=None,
        ),
    )

    _emit_state_summary(fake_state)  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert "health: unknown" in output
    assert "target: <unset>" in output


def test_normalize_status_supports_enum_and_none() -> None:
    assert _normalize_status(SourceHealthStatus.OK) is SourceHealthStatus.OK
    assert _normalize_status(None) is SourceHealthStatus.UNKNOWN


def test_status_color_handles_non_string_inputs() -> None:
    assert _status_color(None) is None
    assert _status_color(object()) is None  # type: ignore[arg-type]


def test_emit_health_guidance_handles_service_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Service:
        def list(self) -> list[object]:
            raise SourceError("boom")

    context = SimpleNamespace(service=_Service())

    _emit_health_guidance(context, "demo")  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert output == ""


def test_source_init_creates_source_and_prevents_duplicates(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)

    app = create_app()
    result = runner.invoke(
        app,
        ["source", "init", "Alpha Source", "--target", str(target)],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "source: alpha-source" in result.stdout
    assert "target: " in result.stdout
    assert "health: unknown" in result.stdout

    store = SourceConfigStore(config_path=paths.config_file)
    config = store.load()
    assert "alpha-source" in config.workspace_sources
    source_config = config.workspace_sources["alpha-source"]
    assert source_config.target == target
    assert source_config.enabled is True

    duplicate = runner.invoke(
        app,
        ["source", "init", "Alpha Source", "--target", str(target)],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )

    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.stdout


def test_source_target_update_and_clear(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    primary = root / "data"
    primary.mkdir(parents=True, exist_ok=True)
    service = SourceService(
        workspace=paths,
        config_store=SourceConfigStore(config_path=paths.config_file),
    )
    service.init("alpha", target=primary, force_refresh=True)

    app = create_app()

    clear_result = runner.invoke(
        app,
        ["source", "target", "alpha", "--clear"],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )
    assert clear_result.exit_code == 0, clear_result.stdout
    assert "target: <unset>" in clear_result.stdout

    missing_result = runner.invoke(
        app,
        ["source", "target", "alpha"],
        env=env,
        catch_exceptions=False,
    )
    assert missing_result.exit_code == 1
    assert "A target directory is required" in missing_result.stdout

    conflict_result = runner.invoke(
        app,
        ["source", "target", "alpha", str(primary), "--clear"],
        env=env,
        catch_exceptions=False,
    )
    assert conflict_result.exit_code == 1
    assert "cannot be combined" in conflict_result.stdout

    secondary = root / "data-secondary"
    secondary.mkdir(parents=True, exist_ok=True)
    update_failed = runner.invoke(
        app,
        ["source", "target", "alpha", str(secondary)],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )
    assert update_failed.exit_code == 1
    assert "Health check for source" in update_failed.stdout

    forced_update = runner.invoke(
        app,
        ["source", "target", "alpha", str(secondary), "--force"],
        env=env,
        catch_exceptions=False,
    )
    assert forced_update.exit_code == 0, forced_update.stdout
    assert "target: " + str(secondary) in forced_update.stdout

    store = SourceConfigStore(config_path=paths.config_file)
    config = store.load()
    source = config.workspace_sources["alpha"]
    assert source.target == secondary


def test_source_refresh_handles_health_failures(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)
    service.init("alpha", target=target, force_refresh=True)

    target.rmdir()

    app = create_app()
    failed = runner.invoke(
        app,
        ["source", "refresh", "alpha"],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )
    assert failed.exit_code == 1
    assert "Health check for source" in failed.stdout
    assert "Latest health snapshot for alpha" in failed.stdout

    target.mkdir(parents=True, exist_ok=True)

    forced = runner.invoke(
        app,
        ["source", "refresh", "alpha", "--force"],
        env=env,
        catch_exceptions=False,
    )
    assert forced.exit_code == 0
    assert "enabled: no" in forced.stdout

    enable_result = runner.invoke(
        app,
        ["source", "enable", "alpha"],
        env=env,
        catch_exceptions=False,
    )
    assert enable_result.exit_code == 0
    assert "enabled: yes" in enable_result.stdout
    assert "health: ok" in enable_result.stdout

    disable_result = runner.invoke(
        app,
        ["source", "disable", "alpha"],
        env=env,
        catch_exceptions=False,
    )
    assert disable_result.exit_code == 0
    assert "enabled: no" in disable_result.stdout


def test_source_list_reports_health_status(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    target_alpha = root / "data-alpha"
    target_beta = root / "data-beta"
    target_alpha.mkdir(parents=True, exist_ok=True)
    target_beta.mkdir(parents=True, exist_ok=True)

    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)
    service.init("alpha", target=target_alpha, force_refresh=True)
    service.init("beta", target=target_beta, force_refresh=True)

    # Remove beta's target and force a refresh to record the failure snapshot.
    target_beta.rmdir()

    app = create_app()
    refresh_beta = runner.invoke(
        app,
        ["source", "refresh", "beta", "--force"],
        env=env,
        catch_exceptions=False,
    )
    assert refresh_beta.exit_code == 0
    assert "health: error" in refresh_beta.stdout

    result = runner.invoke(
        app,
        ["source", "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Configured sources:" in result.stdout
    assert "beta" in result.stdout


def test_source_rename_and_remove(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)
    service.init("alpha", target=target, force_refresh=True)

    app = create_app()
    rename_result = runner.invoke(
        app,
        ["source", "rename", "alpha", "beta"],
        env=env,
        catch_exceptions=False,
    )
    assert rename_result.exit_code == 0
    assert "source: beta" in rename_result.stdout

    remove_cancel = runner.invoke(
        app,
        ["source", "remove", "beta"],
        env=env,
        input="n\n",
        catch_exceptions=False,
    )
    assert remove_cancel.exit_code == 1
    assert "Operation cancelled" in remove_cancel.stdout

    remove_result = runner.invoke(
        app,
        ["source", "remove", "beta"],
        env=env,
        input="y\n",
        catch_exceptions=False,
    )
    assert remove_result.exit_code == 0
    assert "Removed source beta" in remove_result.stdout

    config = store.load()
    assert "beta" not in config.workspace_sources


def test_source_enable_handles_missing_sources(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, _ = workspace
    app = create_app()
    result = runner.invoke(
        app,
        ["source", "enable", "ghost"],
        env=env,
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_source_list_requires_workspace_init(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    env = {"HOME": str(tmp_path), "RAGGD_WORKSPACE": str(workspace_dir)}
    app = create_app()

    result = runner.invoke(
        app,
        ["source", "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Run `raggd init` first" in result.stdout


def test_source_callback_invalid_workspace_override(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    workspace_file = tmp_path / "workspace-file"
    workspace_file.write_text("stub", encoding="utf-8")
    env = {"HOME": str(tmp_path)}
    app = create_app()

    result = runner.invoke(
        app,
        ["source", "--workspace", str(workspace_file), "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace error" in result.stdout


def test_source_callback_handles_config_error(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, env, paths = workspace

    store = SourceConfigStore(config_path=paths.config_file)
    monkeypatch.setattr(
        "raggd.cli.source.SourceConfigStore",
        lambda config_path: store,
    )

    monkeypatch.setattr(
        store,
        "load",
        lambda: (_ for _ in ()).throw(SourceConfigError("boom")),
    )

    app = create_app()
    result = runner.invoke(
        app,
        ["source", "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed to load workspace config" in result.stdout


def test_source_list_reports_empty_workspace(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, _ = workspace
    app = create_app()
    result = runner.invoke(
        app,
        ["source", "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No sources are configured" in result.stdout


def test_source_list_handles_service_error(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, env, _ = workspace

    def _boom(self: SourceService) -> list[object]:  # pragma: no cover - helper
        raise SourceError("boom")

    monkeypatch.setattr(SourceService, "list", _boom)

    app = create_app()
    result = runner.invoke(
        app,
        ["source", "list"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "boom" in result.stdout


def test_source_rename_handles_missing_source(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, _ = workspace
    app = create_app()
    result = runner.invoke(
        app,
        ["source", "rename", "ghost", "other"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_source_remove_handles_missing_source(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, _ = workspace
    app = create_app()
    result = runner.invoke(
        app,
        ["source", "remove", "ghost", "--force"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_source_disable_handles_missing_sources(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, _ = workspace
    app = create_app()
    result = runner.invoke(
        app,
        ["source", "disable", "ghost"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_source_enable_multiple_sources(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    _, env, paths = workspace
    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)
    service.init("alpha")
    service.init("beta")

    app = create_app()
    result = runner.invoke(
        app,
        ["source", "enable", "alpha", "beta"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.stdout.count("source:") == 2


def test_source_disable_multiple_sources(
    runner: CliRunner,
    workspace: tuple[Path, dict[str, str], WorkspacePaths],
) -> None:
    root, env, paths = workspace
    store = SourceConfigStore(config_path=paths.config_file)
    service = SourceService(workspace=paths, config_store=store)
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    service.init("alpha", target=target, force_refresh=True)
    service.init("beta", target=target, force_refresh=True)

    app = create_app()
    result = runner.invoke(
        app,
        ["source", "disable", "alpha", "beta"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.stdout.count("enabled: no") >= 2
