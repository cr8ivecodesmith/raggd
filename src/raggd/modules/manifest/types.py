"""Shared types for the manifest module."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from raggd.core.paths import WorkspacePaths

__all__ = ["SourceRef"]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Reference to a workspace source and its manifest."""

    name: str
    root: Path
    manifest_path: Path

    @classmethod
    def from_workspace(
        cls,
        *,
        workspace: WorkspacePaths,
        name: str,
    ) -> "SourceRef":
        """Build a :class:`SourceRef` from workspace paths."""

        source_dir = workspace.source_dir(name)
        return cls(
            name=name,
            root=source_dir,
            manifest_path=workspace.source_manifest_path(name),
        )

    def ensure_directories(self) -> None:
        """Ensure the source directory exists on disk."""

        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
