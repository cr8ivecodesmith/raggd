"""Utility helpers for source management validation."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from raggd.core.paths import WorkspacePaths

__all__ = [
    "SourcePathError",
    "SourceSlugError",
    "ensure_workspace_path",
    "normalize_source_slug",
    "resolve_target_path",
]


class SourceSlugError(ValueError):
    """Raised when a source name cannot be normalized."""


class SourcePathError(ValueError):
    """Raised when a source path fails validation."""


_SLUG_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def normalize_source_slug(raw: str) -> str:
    """Normalize arbitrary input into a lowercase kebab-case slug.

    The helper strips surrounding whitespace, transliterates unicode characters
    to ASCII, collapses separator runs, and ensures the slug only includes the
    ``[a-z0-9-]`` character set.

    Args:
        raw: Arbitrary user-provided source identifier.

    Returns:
        Normalized slug suitable for workspace directory names and configuration
        keys.

    Raises:
        SourceSlugError: If the input does not contain any alphanumeric content.
    """

    if not isinstance(raw, str):
        raise SourceSlugError("Source name must be a string.")

    trimmed = raw.strip()
    if not trimmed:
        raise SourceSlugError("Source name cannot be empty.")

    normalized = unicodedata.normalize("NFKD", trimmed)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowercase = ascii_only.lower()
    tokens = _SLUG_TOKEN_PATTERN.findall(lowercase)
    if not tokens:
        raise SourceSlugError(
            "Source name must include alphanumeric characters."
        )
    return "-".join(tokens)


def ensure_workspace_path(base: Path, candidate: Path) -> Path:
    """Ensure ``candidate`` stays within the ``base`` workspace subtree.

    Args:
        base: Workspace directory that bounds allowed paths.
        candidate: Path derived from user input to validate.

    Returns:
        The resolved absolute candidate path when validation succeeds.

    Raises:
        SourcePathError: If the candidate escapes the workspace subtree.
    """

    base_path = Path(base).expanduser().resolve(strict=False)
    candidate_path = Path(candidate).expanduser().resolve(strict=False)

    if candidate_path.is_relative_to(base_path):
        return candidate_path

    message = (
        f"Path {candidate_path} is outside of workspace subtree {base_path}."
    )
    raise SourcePathError(message)


def resolve_target_path(
    candidate: os.PathLike[str] | str,
    *,
    workspace: WorkspacePaths,
    must_exist: bool = True,
    require_directory: bool = True,
    allowed_parents: Iterable[Path] | None = None,
) -> Path:
    """Resolve a user-provided target path to an absolute location.

    Args:
        candidate: Raw target path supplied by the user.
        workspace: Workspace paths used as the anchor for relative targets.
        must_exist: When ``True`` (default), require the target to exist.
        require_directory: When ``True`` (default), require the resolved path to
            be a directory.
        allowed_parents: Optional iterable of base directories that the target
            must reside within. When omitted, any absolute path is allowed.

    Returns:
        Absolute, normalized path to the target.

    Raises:
        SourcePathError: If validation fails according to the provided rules.
    """

    raw_path = Path(candidate).expanduser()
    if not raw_path.is_absolute():
        raw_path = (workspace.workspace / raw_path).resolve(strict=False)
    else:
        raw_path = raw_path.resolve(strict=False)

    if must_exist and not raw_path.exists():
        raise SourcePathError(f"Target path does not exist: {raw_path}")
    if require_directory and raw_path.exists() and not raw_path.is_dir():
        raise SourcePathError(f"Target path must be a directory: {raw_path}")
    if must_exist and raw_path.exists() and not os.access(raw_path, os.R_OK):
        raise SourcePathError(f"Target path is not readable: {raw_path}")

    if allowed_parents is not None:
        raw_resolved = raw_path
        for parent in allowed_parents:
            parent_path = Path(parent).expanduser().resolve(strict=False)
            if raw_resolved.is_relative_to(parent_path):
                break
        else:
            raise SourcePathError(
                f"Target path {raw_resolved} is outside allowed directories."
            )

    return raw_path
