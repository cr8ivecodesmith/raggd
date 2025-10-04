"""Backup rotation helpers for manifests."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__all__ = [
    "ManifestBackupError",
    "create_backup",
    "prune_backups",
]


class ManifestBackupError(RuntimeError):
    """Raised when manifest backup rotation fails."""


@dataclass(frozen=True, slots=True)
class BackupRequest:
    """Parameters describing a backup operation."""

    source: Path
    suffix: str
    retention: int
    timestamp: datetime


def create_backup(
    manifest_path: Path,
    *,
    suffix: str = ".bak",
    retention: int = 5,
    timestamp: datetime | None = None,
) -> Path | None:
    """Create a timestamped backup for ``manifest_path`` if it exists."""

    if retention <= 0 or not manifest_path.exists():
        return None

    request = BackupRequest(
        source=manifest_path,
        suffix=suffix,
        retention=retention,
        timestamp=timestamp or datetime.now(timezone.utc),
    )
    backup_path = _write_backup(request)
    prune_backups(manifest_path, suffix=suffix, retention=retention)
    return backup_path


def prune_backups(
    manifest_path: Path,
    *,
    suffix: str = ".bak",
    retention: int = 5,
) -> None:
    """Remove old backups keeping the newest ``retention`` entries."""

    if retention <= 0:
        return

    backups = sorted(
        _iter_backups(manifest_path, suffix=suffix),
        key=lambda candidate: (
            candidate.stat().st_mtime_ns,
            candidate.name,
        ),
    )
    excess = len(backups) - retention
    if excess <= 0:
        return

    for path in backups[:excess]:
        try:
            path.unlink()
        except FileNotFoundError:  # pragma: no cover - best effort cleanup
            continue
        except OSError as exc:  # pragma: no cover - surfaced at runtime
            raise ManifestBackupError(
                f"Failed pruning manifest backup {path}: {exc}"
            ) from exc


def _iter_backups(manifest_path: Path, *, suffix: str) -> Iterable[Path]:
    prefix = f"{manifest_path.name}."
    pattern = f"{manifest_path.name}.*{suffix}"
    for candidate in manifest_path.parent.glob(pattern):
        starts_with_prefix = candidate.name.startswith(prefix)
        ends_with_suffix = candidate.name.endswith(suffix)
        if starts_with_prefix and ends_with_suffix:
            yield candidate


def _write_backup(request: BackupRequest) -> Path:
    timestamp_label = request.timestamp.strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{request.source.name}.{timestamp_label}{request.suffix}"
    destination = request.source.with_name(backup_name)
    try:
        shutil.copy2(request.source, destination)
    except OSError as exc:  # pragma: no cover - surfaced at runtime
        raise ManifestBackupError(
            f"Failed creating manifest backup at {destination}: {exc}"
        ) from exc
    return destination
