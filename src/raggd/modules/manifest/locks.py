"""Filesystem locking helpers for manifest operations."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ManifestLockError",
    "ManifestLockTimeoutError",
    "FileLock",
    "build_lock_path",
]


class ManifestLockError(RuntimeError):
    """Base error type for manifest locking failures."""


class ManifestLockTimeoutError(ManifestLockError):
    """Raised when acquiring a manifest lock times out."""


@dataclass(slots=True)
class FileLock:
    """Simple lock file implementation with timeout semantics."""

    path: Path
    timeout: float = 5.0
    poll_interval: float = 0.1
    _handle: int | None = field(init=False, default=None, repr=False)

    def acquire(self) -> None:
        """Acquire the lock, waiting up to ``timeout`` seconds."""

        if self._handle is not None:
            return

        deadline = time.monotonic() + max(self.timeout, 0.0)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                handle = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                self._handle = handle
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ManifestLockTimeoutError(
                        f"Timed out acquiring manifest lock at {self.path}"
                    ) from None
                time.sleep(self.poll_interval)
            except OSError as exc:  # pragma: no cover - surfaced at runtime
                raise ManifestLockError(
                    f"Failed acquiring manifest lock at {self.path}: {exc}"
                ) from exc

    def release(self) -> None:
        """Release the lock if held."""

        handle = self._handle
        if handle is None:
            return

        try:
            os.close(handle)
        finally:
            self._handle = None
            try:
                self.path.unlink()
            except FileNotFoundError:  # pragma: no cover - best effort cleanup
                pass
            except OSError as exc:  # pragma: no cover - surfaced at runtime
                raise ManifestLockError(
                    f"Failed removing manifest lock at {self.path}: {exc}"
                ) from exc

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def build_lock_path(manifest_path: Path, *, suffix: str = ".lock") -> Path:
    """Return the lock file path for ``manifest_path``."""

    return manifest_path.with_name(f"{manifest_path.name}{suffix}")
