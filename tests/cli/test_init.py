"""Tests for :mod:`raggd.cli.init`."""

from __future__ import annotations

import tomllib
from pathlib import Path

from raggd.cli.init import init_workspace
from raggd.core.config import DEFAULTS_RESOURCE_NAME


def test_init_workspace_seeds_defaults_and_config(tmp_path) -> None:
    workspace = tmp_path / "workspace"

    config = init_workspace(workspace=workspace)

    defaults_path = workspace / DEFAULTS_RESOURCE_NAME
    config_path = workspace / "raggd.toml"

    assert defaults_path.exists()
    assert config_path.exists()
    assert (workspace / "logs").is_dir()
    assert (workspace / "archives").is_dir()

    defaults = tomllib.loads(defaults_path.read_text(encoding="utf-8"))
    assert defaults["log_level"] == "INFO"

    rendered = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert rendered["workspace"].endswith("workspace")
    assert rendered["log_level"] == "INFO"

    assert config.workspace == Path(rendered["workspace"]).expanduser()
    assert config.log_level == "INFO"


def test_init_workspace_respects_overrides_and_refresh(tmp_path) -> None:
    workspace = tmp_path / "custom"
    init_workspace(workspace=workspace)

    defaults_path = workspace / DEFAULTS_RESOURCE_NAME
    config_path = workspace / "raggd.toml"

    defaults_path.write_text("# mutated\n", encoding="utf-8")
    config_path.write_text("workspace = \"/tmp/elsewhere\"\n", encoding="utf-8")

    config = init_workspace(
        workspace=workspace,
        refresh=True,
        log_level="debug",
        module_overrides={"file-monitoring": True},
    )

    defaults = defaults_path.read_text(encoding="utf-8")
    assert defaults.startswith("# Default configuration bundled with raggd")

    rendered = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert rendered["log_level"] == "DEBUG"
    assert rendered["modules"]["file-monitoring"]["enabled"] is True

    assert config.log_level == "DEBUG"
    assert config.modules["file-monitoring"].enabled is True

    archive_entries = list((workspace / "archives").iterdir())
    assert archive_entries, "refresh should archive previous workspace contents"

    archived_names = {child.name for child in archive_entries[0].iterdir()}
    assert "raggd.toml" in archived_names
    assert DEFAULTS_RESOURCE_NAME in archived_names

