from __future__ import annotations

from pathlib import Path

import pytest

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import DbLifecycleService
from raggd.modules.manifest import ManifestService, ManifestSettings


def _make_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )


def test_db_lifecycle_rejects_conflicting_manifest_args(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    manifest = ManifestService(workspace=paths)

    with pytest.raises(ValueError):
        DbLifecycleService(
            workspace=paths,
            manifest_service=manifest,
            manifest_settings=ManifestSettings(),
        )
