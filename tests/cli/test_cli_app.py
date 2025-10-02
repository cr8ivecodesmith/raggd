"""Integration tests for the Typer application exposed by :mod:`raggd.cli`."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raggd.cli import (
    _EXTRA_SENTINELS,
    _build_module_overrides,
    _detect_available_extras,
    _render_module_line,
    create_app,
)
from raggd.core.config import DEFAULTS_RESOURCE_NAME


def _workspace_env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    return {
        "HOME": str(tmp_path),
        "RAGGD_WORKSPACE": str(workspace),
    }


@pytest.fixture()
def runner() -> CliRunner:
    """Return a Typer CLI runner for invoking the application."""

    return CliRunner()


def test_cli_init_respects_env_and_outputs_status(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _workspace_env(tmp_path)
    env["RAGGD_LOG_LEVEL"] = "warning"

    monkeypatch.setattr(
        "raggd.cli._detect_available_extras",
        lambda: {"file-monitoring"},
    )

    app = create_app()
    result = runner.invoke(app, ["init"], env=env, catch_exceptions=False)

    assert result.exit_code == 0, result.stdout
    assert "Workspace initialized" in result.stdout
    assert "log level: WARNING" in result.stdout
    assert "file-monitoring" in result.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    config_path = workspace / "raggd.toml"
    defaults_path = workspace / DEFAULTS_RESOURCE_NAME

    assert config_path.exists()
    assert defaults_path.exists()
    assert (workspace / "logs").is_dir()

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["log_level"] == "WARNING"


def test_cli_init_module_overrides_and_missing_extras(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _workspace_env(tmp_path)

    monkeypatch.setattr(
        "raggd.cli._detect_available_extras",
        lambda: set(),
    )

    app = create_app()
    result = runner.invoke(
        app,
        ["init", "--enable-module", "rag", "--disable-module", "file-monitoring"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.stdout
    assert "rag: disabled - missing extras: rag" in result.stdout
    assert "file-monitoring: disabled" in result.stdout

    workspace = Path(env["RAGGD_WORKSPACE"])  # type: ignore[arg-type]
    config = tomllib.loads((workspace / "raggd.toml").read_text(encoding="utf-8"))
    assert config["modules"]["rag"]["enabled"] is True
    assert config["modules"]["file-monitoring"]["enabled"] is False


def test_cli_init_rejects_conflicting_module_overrides(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    env = _workspace_env(tmp_path)

    app = create_app()
    result = runner.invoke(
        app,
        ["init", "--enable-module", "rag", "--disable-module", "rag"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert "Invalid value for --enable-module/--disable-module" in result.output


def test_cli_init_existing_workspace_note(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _workspace_env(tmp_path)

    monkeypatch.setattr("raggd.cli._detect_available_extras", lambda: set())

    app = create_app()
    first = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert first.exit_code == 0

    second = runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    assert second.exit_code == 0
    assert "existing workspace detected" in second.output


def test_cli_init_refresh_note(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _workspace_env(tmp_path)

    monkeypatch.setattr("raggd.cli._detect_available_extras", lambda: set())

    app = create_app()
    runner.invoke(app, ["init"], env=env, catch_exceptions=False)
    result = runner.invoke(
        app,
        ["init", "--refresh"],
        env=env,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "archived previous workspace before refresh" in result.output


def test_build_module_overrides_sanitizes_names() -> None:
    result = _build_module_overrides(["rag", " "], ["file_monitoring"])
    assert result == {"rag": True, "file-monitoring": False}


def test_render_module_line_states() -> None:
    enabled_line = _render_module_line("alpha", {"alpha": True}, {"alpha": "enabled"})
    assert enabled_line.endswith("enabled")

    unknown_line = _render_module_line(
        "ghost",
        {},
        {"ghost": "unknown module"},
    )
    assert "unknown" in unknown_line


def test_detect_available_extras_handles_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_find_spec(name: str):
        return object() if name == "marker" else None

    monkeypatch.setattr("raggd.cli.importlib_util.find_spec", fake_find_spec)
    monkeypatch.setitem(_EXTRA_SENTINELS, "noop", ())
    monkeypatch.setitem(_EXTRA_SENTINELS, "custom", ("marker",))

    available = _detect_available_extras()

    assert "noop" in available
    assert "custom" in available
