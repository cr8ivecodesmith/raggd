from __future__ import annotations

import os
from pathlib import Path

import pytest

from raggd.core.paths import WorkspacePaths
from raggd.source import (
    SourcePathError,
    SourceSlugError,
    ensure_workspace_path,
    normalize_source_slug,
    resolve_target_path,
)


def _build_workspace(tmp_path: Path) -> WorkspacePaths:
    workspace = tmp_path / "workspace"
    return WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )


def test_normalize_source_slug_collapses_and_transliterates() -> None:
    assert normalize_source_slug(" Résumé   Docs! 2025 ") == "resume-docs-2025"
    assert normalize_source_slug("Data__Pipelines") == "data-pipelines"
    assert normalize_source_slug("AlreadySlug") == "alreadyslug"


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "--__--"])
def test_normalize_source_slug_rejects_invalid(raw: str) -> None:
    with pytest.raises(SourceSlugError):
        normalize_source_slug(raw)


def test_normalize_source_slug_requires_string() -> None:
    with pytest.raises(SourceSlugError):
        normalize_source_slug(123)  # type: ignore[arg-type]


def test_ensure_workspace_path_allows_child(tmp_path: Path) -> None:
    base = (tmp_path / "workspace" / "sources").resolve()
    candidate = base / "alpha"

    resolved = ensure_workspace_path(base, candidate)

    assert resolved == candidate.resolve()


def test_ensure_workspace_path_blocks_escape(tmp_path: Path) -> None:
    base = (tmp_path / "workspace" / "sources").resolve()
    escape_candidate = base.parent / "secrets"

    with pytest.raises(SourcePathError):
        ensure_workspace_path(base, escape_candidate)


def test_resolve_target_path_handles_relative_and_absolute(tmp_path: Path) -> None:
    paths = _build_workspace(tmp_path)
    target = paths.workspace / "data" / "docs"
    target.mkdir(parents=True)

    resolved_relative = resolve_target_path(
        "data/docs",
        workspace=paths,
    )
    assert resolved_relative == target.resolve()

    resolved_absolute = resolve_target_path(
        target,
        workspace=paths,
    )
    assert resolved_absolute == target.resolve()


def test_resolve_target_path_rejects_missing_or_nondirectory(tmp_path: Path) -> None:
    paths = _build_workspace(tmp_path)
    missing = paths.workspace / "missing"

    with pytest.raises(SourcePathError):
        resolve_target_path(missing, workspace=paths)

    target = paths.workspace / "data"
    target.mkdir(parents=True)
    file_path = target / "file.txt"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(SourcePathError):
        resolve_target_path(file_path, workspace=paths)


def test_resolve_target_path_enforces_allowed_parents(tmp_path: Path) -> None:
    paths = _build_workspace(tmp_path)
    allowed = paths.workspace / "data"
    allowed.mkdir(parents=True)
    ok_target = allowed / "docs"
    ok_target.mkdir()

    result = resolve_target_path(
        ok_target,
        workspace=paths,
        allowed_parents=[allowed],
    )
    assert result == ok_target.resolve()

    disallowed = tmp_path / "external"
    disallowed.mkdir()

    with pytest.raises(SourcePathError):
        resolve_target_path(
            disallowed,
            workspace=paths,
            allowed_parents=[allowed],
        )


def test_resolve_target_path_detects_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _build_workspace(tmp_path)
    target = paths.workspace / "data"
    target.mkdir(parents=True)
    target_resolved = target.resolve()

    original_access = os.access

    def fake_access(path: os.PathLike[str] | str, mode: int) -> bool:
        if Path(path).resolve(strict=False) == target_resolved:
            return False
        return original_access(path, mode)

    monkeypatch.setattr("raggd.source.utils.os.access", fake_access)

    with pytest.raises(SourcePathError):
        resolve_target_path(target, workspace=paths)
