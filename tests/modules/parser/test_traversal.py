"""Tests for parser traversal utilities."""

from __future__ import annotations

from pathlib import Path

from raggd.core.config import ParserGitignoreBehavior
from raggd.modules.parser.traversal import (
    TraversalScope,
    TraversalService,
)


def _write(path: Path, contents: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _collect(
    service: TraversalService, scope: TraversalScope | None = None
) -> set[str]:
    return {
        result.relative_path.as_posix()
        for result in service.iter_files(scope=scope)
    }


def test_traversal_respects_repo_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    _write(root / ".gitignore", "build/\n*.log\n")

    _write(root / "build" / "generated.txt")
    _write(root / "logs" / "app.log")
    _write(root / "src" / "main.py")
    _write(root / "src" / "keep.py")
    _write(root / "src" / ".gitignore", "secret.py\n")
    _write(root / "src" / "secret.py")
    _write(root / "src" / "sub" / "nested.py")

    service = TraversalService(
        root=root,
        gitignore_behavior=ParserGitignoreBehavior.REPO,
    )

    files = _collect(service)

    assert "src/main.py" in files
    assert "src/keep.py" in files
    assert "src/sub/nested.py" in files
    assert "build/generated.txt" not in files
    assert "logs/app.log" not in files
    assert "src/secret.py" not in files


def test_traversal_workspace_patterns(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    _write(root / ".gitignore", "*.log\n")
    _write(root / "notes.workspace")
    _write(root / "logs" / "ignored.log")
    _write(root / "docs" / "manual.md")

    service = TraversalService(
        root=root,
        gitignore_behavior=ParserGitignoreBehavior.WORKSPACE,
        workspace_patterns=("**/*.workspace",),
    )

    files = _collect(service)

    assert "docs/manual.md" in files
    assert "logs/ignored.log" in files  # repo gitignore disabled
    assert "notes.workspace" not in files


def test_traversal_combined_behavior(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    _write(root / ".gitignore", "build/\n")
    _write(root / "build" / "skip.txt")
    _write(root / "include" / "keep.txt")
    _write(root / "include" / "notes.workspace")

    service = TraversalService(
        root=root,
        gitignore_behavior=ParserGitignoreBehavior.COMBINED,
        workspace_patterns=("**/*.workspace",),
    )

    files = _collect(service)

    assert "include/keep.txt" in files
    assert "build/skip.txt" not in files
    assert "include/notes.workspace" not in files


def test_traversal_scope_limits_results(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    _write(root / "src" / "main.py")
    _write(root / "docs" / "guide.md")

    service = TraversalService(
        root=root,
        gitignore_behavior=ParserGitignoreBehavior.REPO,
    )

    scope = TraversalScope.from_iterable([root / "docs"])
    files = _collect(service, scope=scope)

    assert files == {"docs/guide.md"}

    file_scope = TraversalScope.from_iterable([root / "src" / "main.py"])
    files = _collect(service, scope=file_scope)
    assert files == {"src/main.py"}
