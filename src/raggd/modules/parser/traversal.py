"""Filesystem traversal helpers for the parser module."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from raggd.core.config import ParserGitignoreBehavior

try:  # pragma: no cover - exercised via traversal tests
    from pathspec import PathSpec
except ImportError as exc:  # pragma: no cover - dependency guard
    raise RuntimeError(
        "The 'pathspec' package is required for parser traversal."
    ) from exc

__all__ = [
    "TraversalScope",
    "TraversalResult",
    "TraversalService",
]


@dataclass(frozen=True, slots=True)
class TraversalScope:
    """Describe traversal constraints for a source target."""

    include: tuple[Path, ...] = ()

    @classmethod
    def from_iterable(cls, paths: Iterable[Path]) -> "TraversalScope":
        normalized: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            absolute = path.resolve()
            key = absolute.as_posix()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(absolute)
        return cls(include=tuple(normalized))


@dataclass(frozen=True, slots=True)
class TraversalResult:
    """Container describing a file discovered during traversal."""

    absolute_path: Path
    relative_path: Path


class TraversalService:
    """Enumerate files under a source while respecting ignore rules."""

    def __init__(
        self,
        *,
        root: Path,
        gitignore_behavior: ParserGitignoreBehavior,
        workspace_patterns: Sequence[str] = (),
        follow_symlinks: bool = False,
    ) -> None:
        if not root.exists():
            raise FileNotFoundError(f"Traversal root not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(
                f"Traversal root must be a directory: {root}"
            )
        self._root = root.resolve()
        self._behavior = gitignore_behavior
        self._workspace_spec = (
            PathSpec.from_lines("gitwildmatch", workspace_patterns)
            if workspace_patterns
            and gitignore_behavior
            in (
                ParserGitignoreBehavior.WORKSPACE,
                ParserGitignoreBehavior.COMBINED,
            )
            else None
        )
        self._follow_symlinks = follow_symlinks
        self._repo_enabled = gitignore_behavior in (
            ParserGitignoreBehavior.REPO,
            ParserGitignoreBehavior.COMBINED,
        )
        self._gitignore_cache: dict[Path, PathSpec | None] = {}

    def iter_files(
        self,
        scope: TraversalScope | None = None,
    ) -> Iterator[TraversalResult]:
        """Yield files within the traversal scope honoring ignore rules."""

        if scope and scope.include:
            for path in scope.include:
                if not path.exists():
                    continue
                try:
                    path.relative_to(self._root)
                except ValueError:
                    continue
                if path.is_dir():
                    yield from self._walk_directory(path)
                elif path.is_file():
                    if not self._is_ignored(path):
                        yield TraversalResult(
                            absolute_path=path,
                            relative_path=path.relative_to(self._root),
                        )
        else:
            yield from self._walk_directory(self._root)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _walk_directory(self, directory: Path) -> Iterator[TraversalResult]:
        if not directory.exists():
            return
        directory = directory.resolve()
        stack: list[PathSpec] = []
        if directory != self._root:
            for spec in self._ancestors_specs(directory, include_self=False):
                stack.append(spec)
        for result in self._walk_inner(directory, stack):
            yield result

    def _walk_inner(
        self,
        directory: Path,
        stack: list[PathSpec],
    ) -> Iterator[TraversalResult]:
        local_spec = self._load_gitignore(directory)
        if local_spec is not None:
            stack.append(local_spec)

        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except PermissionError:
            return

        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue

            if entry.is_symlink() and not self._follow_symlinks:
                continue

            if self._is_ignored(entry, stack=stack, is_dir=is_dir):
                continue

            if is_dir:
                yield from self._walk_inner(entry, stack.copy())
                continue

            if not entry.is_file():
                continue

            yield TraversalResult(
                absolute_path=entry,
                relative_path=entry.relative_to(self._root),
            )

    def _ancestors_specs(
        self,
        directory: Path,
        *,
        include_self: bool,
    ) -> Iterator[PathSpec]:
        directory = directory.resolve()
        current = directory
        lineage: list[Path] = []
        while True:
            lineage.append(current)
            if current == self._root:
                break
            current = current.parent
        lineage.reverse()
        if not include_self and lineage and lineage[-1] == directory:
            lineage = lineage[:-1]

        for ancestor in lineage:
            spec = self._load_gitignore(ancestor)
            if spec is not None:
                yield spec

    def _is_ignored(
        self,
        path: Path,
        *,
        stack: Sequence[PathSpec] | None = None,
        is_dir: bool | None = None,
    ) -> bool:
        absolute = self._absolute_path(path)
        if absolute is None:
            return False

        relative = self._relative_to_root(absolute)
        if relative is None:
            return True

        resolved_is_dir = self._resolve_is_dir_flag(absolute, is_dir)
        candidate = self._candidate_for_ignore(relative, resolved_is_dir)

        if self._matches_workspace(candidate):
            return True

        specs = self._resolve_gitignore_specs(absolute.parent, stack=stack)
        return self._matches_gitignore(candidate, specs)

    def _absolute_path(self, path: Path) -> Path | None:
        if not path.is_absolute():
            path = path.resolve()
        if path.exists() or path.is_symlink():
            return path
        return None

    def _relative_to_root(self, path: Path) -> Path | None:
        try:
            return path.relative_to(self._root)
        except ValueError:
            return None

    def _resolve_is_dir_flag(
        self,
        path: Path,
        provided: bool | None,
    ) -> bool:
        if provided is not None:
            return provided
        try:
            return path.is_dir()
        except OSError:
            return False

    @staticmethod
    def _candidate_for_ignore(relative: Path, is_dir: bool) -> str:
        candidate = relative.as_posix()
        return f"{candidate}/" if is_dir else candidate

    def _matches_workspace(self, candidate: str) -> bool:
        if self._workspace_spec is None:
            return False
        return self._workspace_spec.match_file(candidate)

    def _resolve_gitignore_specs(
        self,
        parent: Path,
        *,
        stack: Sequence[PathSpec] | None,
    ) -> Sequence[PathSpec]:
        if stack is not None:
            return stack
        return tuple(self._ancestors_specs(parent, include_self=True))

    @staticmethod
    def _matches_gitignore(candidate: str, specs: Sequence[PathSpec]) -> bool:
        for spec in specs:
            if spec.match_file(candidate):
                return True
        return False

    def _load_gitignore(self, directory: Path) -> PathSpec | None:
        if not self._repo_enabled:
            return None
        directory = directory.resolve()
        if directory in self._gitignore_cache:
            return self._gitignore_cache[directory]
        gitignore = directory / ".gitignore"
        if not gitignore.exists() or not gitignore.is_file():
            self._gitignore_cache[directory] = None
            return None
        try:
            lines = gitignore.read_text(encoding="utf-8").splitlines()
        except OSError:
            self._gitignore_cache[directory] = None
            return None
        spec = PathSpec.from_lines("gitwildmatch", lines)
        self._gitignore_cache[directory] = spec
        return spec
