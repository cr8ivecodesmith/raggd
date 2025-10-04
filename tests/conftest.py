"""Shared pytest fixtures for db-module work."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Mapping

import pytest

from raggd.core.paths import WorkspacePaths
from raggd.modules.manifest import ManifestService, ManifestSettings, SourceRef


def _sanitize_node_id(node_id: str) -> str:
    """Return a filesystem-friendly slug for ``node_id``."""

    slug = node_id.replace("::", "-")
    slug = slug.replace(os.sep, "-")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", slug)
    slug = slug.strip("-._")
    return slug or "test"


@pytest.fixture
def manifest_workspace(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[WorkspacePaths]:
    """Provide a temporary workspace rooted under ``.tmp/db-module-tests``.

    The fixture creates deterministic per-test directories so golden files and
    on-disk manifests can be inspected after failures without colliding across
    parametrized tests.
    """

    base = Path(".tmp") / "db-module-tests"
    base.mkdir(parents=True, exist_ok=True)

    slug = _sanitize_node_id(request.node.nodeid)
    root = base / slug
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=False)

    paths = WorkspacePaths(
        workspace=root,
        config_file=root / "raggd.toml",
        logs_dir=root / "logs",
        archives_dir=root / "archives",
        sources_dir=root / "sources",
    )

    for path in paths.iter_all():
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)

    try:
        yield paths
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def manifest_service(manifest_workspace: WorkspacePaths) -> ManifestService:
    """Instantiate :class:`ManifestService` bound to ``manifest_workspace``."""

    return ManifestService(
        workspace=manifest_workspace,
        settings=ManifestSettings(),
    )


@pytest.fixture
def seed_manifest(
    manifest_service: ManifestService,
) -> Iterator[Callable[[str, Mapping[str, Any]], SourceRef]]:
    """Write an arbitrary manifest payload for ``name`` using raw JSON."""

    def _seed(name: str, payload: Mapping[str, Any]) -> SourceRef:
        ref = manifest_service.resolve(name)
        ref.ensure_directories()
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        ref.manifest_path.write_text(serialized, encoding="utf-8")
        return ref

    yield _seed


@pytest.fixture
def legacy_manifest_payload() -> Mapping[str, Any]:
    """Return a representative legacy manifest payload."""

    return {
        "name": "legacy",
        "enabled": False,
        "target": None,
        "path": "./sources/legacy",
        "last_refresh_at": None,
        "last_health": {
            "status": "ok",
            "details": {},
            "checked_at": None,
        },
    }
