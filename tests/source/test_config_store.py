from __future__ import annotations

import os
from pathlib import Path

import tomllib
import pytest

from raggd.cli.init import init_workspace
from raggd.source import (
    SourceConfigStore,
    SourceConfigWriteError,
    WorkspaceSourceConfig,
)


def test_upsert_creates_source_entry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)

    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    config = store.upsert(
        WorkspaceSourceConfig(
            name="demo",
            path=workspace / "sources" / "demo",
            enabled=True,
            target=workspace / "data" / "demo",
        ),
    )

    assert "demo" in config.workspace_sources
    saved = config.workspace_sources["demo"]
    assert saved.enabled is True
    assert saved.target == workspace / "data" / "demo"

    rendered_text = (workspace / "raggd.toml").read_text(encoding="utf-8")
    rendered = tomllib.loads(rendered_text)
    assert rendered["workspace"]["root"].endswith("workspace")
    entry = rendered["workspace"]["sources"]["demo"]
    assert entry["enabled"] is True
    assert entry["path"].endswith("sources/demo")
    assert entry["target"].endswith("data/demo")

    retrieved = store.get("demo")
    assert retrieved is not None and retrieved.enabled is True
    assert store.get("missing") is None


def test_remove_prunes_sources_table(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    store = SourceConfigStore(config_path=workspace / "raggd.toml")
    store.upsert(
        WorkspaceSourceConfig(
            name="demo",
            path=workspace / "sources" / "demo",
            enabled=True,
        )
    )

    config = store.remove("demo")
    assert "demo" not in config.workspace_sources

    rendered_text = (workspace / "raggd.toml").read_text(encoding="utf-8")
    rendered = tomllib.loads(rendered_text)
    assert "sources" not in rendered["workspace"]


def test_replace_all_overwrites_entries_sorted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    store = SourceConfigStore(config_path=workspace / "raggd.toml")

    sources = {
        "bravo": WorkspaceSourceConfig(
            name="bravo",
            path=workspace / "sources" / "bravo",
        ),
        "alpha": WorkspaceSourceConfig(
            name="alpha",
            path=workspace / "sources" / "alpha",
            enabled=True,
        ),
    }

    config = store.replace_all(sources)
    assert list(config.workspace_sources) == ["alpha", "bravo"]

    rendered_text = (workspace / "raggd.toml").read_text(encoding="utf-8")
    alpha_idx = rendered_text.index("[workspace.sources.alpha]")
    bravo_idx = rendered_text.index("[workspace.sources.bravo]")
    assert alpha_idx < bravo_idx


def test_write_failure_raises_and_preserves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    store = SourceConfigStore(config_path=workspace / "raggd.toml")

    config_path = workspace / "raggd.toml"
    original = config_path.read_text(encoding="utf-8")

    def fake_replace(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
    ) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(SourceConfigWriteError) as exc:
        store.upsert(
            WorkspaceSourceConfig(
                name="demo",
                path=workspace / "sources" / "demo",
            )
        )

    assert "replace target file" in str(exc.value)
    assert config_path.read_text(encoding="utf-8") == original


def test_upsert_converts_legacy_scalar_workspace(tmp_path: Path) -> None:
    config_path = tmp_path / "raggd.toml"
    config_path.write_text(
        'workspace = "/tmp/legacy"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )

    store = SourceConfigStore(config_path=config_path)
    store.upsert(
        WorkspaceSourceConfig(
            name="demo",
            path=Path("/tmp/legacy/sources/demo"),
        )
    )

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["workspace"]["root"] == "/tmp/legacy"
    assert "demo" in parsed["workspace"]["sources"]


def test_upsert_handles_missing_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace" / "raggd.toml"
    config_path.parent.mkdir()

    store = SourceConfigStore(config_path=config_path)
    result = store.upsert(
        WorkspaceSourceConfig(
            name="alpha",
            path=Path("/tmp/workspace/sources/alpha"),
        )
    )

    assert "alpha" in result.workspace_sources
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["workspace"]["root"] == str(result.workspace)


def test_extract_legacy_root_handles_non_string(tmp_path: Path) -> None:
    store = SourceConfigStore(config_path=tmp_path / "missing.toml")
    config = store.load()

    assert store._extract_legacy_root(None, config) == str(config.workspace)
    assert store._extract_legacy_root(123, config) == "123"
