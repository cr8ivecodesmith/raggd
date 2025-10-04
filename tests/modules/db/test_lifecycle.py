from __future__ import annotations

from pathlib import Path

import pytest

from raggd.cli.init import init_workspace
from raggd.core.paths import WorkspacePaths
from raggd.modules.db import (
    DbLifecycleNotImplementedError,
    DbLifecycleService,
)
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


@pytest.mark.parametrize(
    "method",
    [
        "upgrade",
        "downgrade",
        "info",
        "vacuum",
        "run",
        "reset",
    ],
)
def test_db_lifecycle_placeholder_methods_raise(
    tmp_path: Path,
    method: str,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace=workspace)
    paths = _make_paths(workspace)
    service = DbLifecycleService(workspace=paths)

    args: tuple[object, ...]
    kwargs: dict[str, object]

    if method == "info":
        args = ("demo",)
        kwargs = {"include_schema": False}
    elif method == "vacuum":
        args = ("demo",)
        kwargs = {"concurrency": None}
    elif method == "run":
        args = ("demo",)
        kwargs = {"sql_path": paths.workspace / "script.sql"}
    elif method == "reset":
        args = ("demo",)
        kwargs = {"force": True}
    elif method == "downgrade":
        args = ("demo",)
        kwargs = {"steps": 1}
    elif method == "upgrade":
        args = ("demo",)
        kwargs = {"steps": None}
    else:  # pragma: no cover - defensive fallback
        raise AssertionError(f"Unknown method {method}")

    with pytest.raises(DbLifecycleNotImplementedError):
        getattr(service, method)(*args, **kwargs)
