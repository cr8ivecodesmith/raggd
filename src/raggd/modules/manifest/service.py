"""Manifest service entry points."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from raggd.core.logging import Logger, get_logger

from .backups import ManifestBackupError, create_backup
from .config import ManifestSettings
from .locks import FileLock, ManifestLockError, build_lock_path
from .migrator import ManifestMigrator
from .types import SourceRef

if TYPE_CHECKING:
    from raggd.core.paths import WorkspacePaths

__all__ = [
    "ManifestError",
    "ManifestReadError",
    "ManifestWriteError",
    "ManifestTransactionError",
    "ManifestSnapshot",
    "ManifestTransaction",
    "ManifestService",
]


class ManifestError(RuntimeError):
    """Base error for manifest operations."""


class ManifestReadError(ManifestError):
    """Raised when a manifest cannot be read from disk."""


class ManifestWriteError(ManifestError):
    """Raised when persisting a manifest to disk fails."""


class ManifestTransactionError(ManifestError):
    """Raised when manifest transaction processing fails."""


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> Any:
    from pathlib import Path as _Path
    from datetime import date, datetime as _datetime
    from enum import Enum

    if isinstance(value, _Path):
        return str(value)
    if isinstance(value, _datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Enum):
        return getattr(value, "value", value.name)
    if hasattr(value, "model_dump"):  # pydantic models
        try:
            return value.model_dump(mode="json")
        except Exception:  # pragma: no cover - defensive fallback
            pass
    return str(value)


def _compute_checksum(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return __import__("hashlib").sha256(serialized.encode("utf-8")).hexdigest()


def _serialize(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        default=_json_default,
    )


@dataclass(slots=True)
class ManifestSnapshot:
    """In-memory representation of a manifest document."""

    source: SourceRef
    data: dict[str, Any]
    modules_key: str
    db_module_key: str

    @property
    def checksum(self) -> str:
        return _compute_checksum(self.data)

    def copy(self) -> "ManifestSnapshot":
        """Return a deep copy of this snapshot."""

        return ManifestSnapshot(
            source=self.source,
            data=copy.deepcopy(self.data),
            modules_key=self.modules_key,
            db_module_key=self.db_module_key,
        )

    def ensure_modules(self) -> dict[str, Any]:
        """Ensure the modules namespace exists and return it."""

        modules = self.data.get(self.modules_key)
        if not isinstance(modules, dict):
            modules = {}
            self.data[self.modules_key] = modules
        return modules

    def ensure_module(self, module: str) -> dict[str, Any]:
        """Ensure ``modules[module]`` exists and return it."""

        modules = self.ensure_modules()
        entry = modules.get(module)
        if not isinstance(entry, dict):
            entry = {}
            modules[module] = entry
        return entry

    def module(self, module: str) -> dict[str, Any] | None:
        modules = self.data.get(self.modules_key)
        if isinstance(modules, dict):
            entry = modules.get(module)
            if isinstance(entry, dict):
                return entry
        return None


@dataclass(slots=True)
class ManifestTransaction:
    """Transactional manifest write context."""

    snapshot: ManifestSnapshot
    baseline_checksum: str
    backup_enabled: bool
    _on_commit: list[Callable[[ManifestSnapshot], None]] = field(
        default_factory=list,
        repr=False,
    )
    _on_rollback: list[Callable[[ManifestSnapshot], None]] = field(
        default_factory=list,
        repr=False,
    )
    result: ManifestSnapshot | None = None

    def on_commit(self, callback: Callable[[ManifestSnapshot], None]) -> None:
        self._on_commit.append(callback)

    def on_rollback(self, callback: Callable[[ManifestSnapshot], None]) -> None:
        self._on_rollback.append(callback)

    def _run_commit(self) -> None:
        for callback in self._on_commit:
            callback(self.snapshot)

    def _run_rollback(self) -> None:
        for callback in reversed(self._on_rollback):
            callback(self.snapshot)


class ManifestService:
    """High-level API for manifest reads, writes, and migrations."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        settings: ManifestSettings | None = None,
        logger: Logger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        # Lazy import avoids circular dependency during module import.
        from raggd.core.paths import WorkspacePaths

        if not isinstance(workspace, WorkspacePaths):  # pragma: no cover
            raise TypeError("workspace must be a WorkspacePaths instance")

        self._paths = workspace
        self._settings = settings or ManifestSettings()
        self._logger = logger or get_logger(
            __name__,
            component="manifest-service",
        )
        self._now = now or _default_now
        self._migrator = ManifestMigrator(
            settings=self._settings,
            logger=self._logger,
        )

    def resolve(self, name: str) -> SourceRef:
        """Return a :class:`SourceRef` for ``name``."""

        return SourceRef.from_workspace(workspace=self._paths, name=name)

    def load(
        self,
        source: SourceRef | str,
        *,
        apply_migrations: bool = False,
        dry_run: bool = False,
    ) -> ManifestSnapshot:
        """Load the manifest for ``source`` into memory."""

        ref = self._coerce_source(source)

        if apply_migrations:
            with self._acquire_lock(ref):
                data = self._read_manifest(ref)
                result = self._migrator.migrate(
                    source=ref,
                    data=data,
                    dry_run=dry_run,
                )
                if result.applied and not dry_run:
                    data = dict(result.data)
                    self._persist(ref, data, backup=True)
        else:
            data = self._read_manifest(ref)

        snapshot = ManifestSnapshot(
            source=ref,
            data=copy.deepcopy(data),
            modules_key=self._settings.modules_key,
            db_module_key=self._settings.db_module_key,
        )
        return snapshot

    def write(
        self,
        source: SourceRef | str,
        mutate: Callable[[ManifestSnapshot], None] | None = None,
        *,
        backup: bool = True,
    ) -> ManifestSnapshot:
        """Write manifest changes for ``source`` using ``mutate`` callback."""

        ref = self._coerce_source(source)
        with self._acquire_lock(ref):
            baseline = self._read_manifest(ref)
            baseline_checksum = _compute_checksum(baseline)
            snapshot = ManifestSnapshot(
                source=ref,
                data=copy.deepcopy(baseline),
                modules_key=self._settings.modules_key,
                db_module_key=self._settings.db_module_key,
            )

            if mutate is not None:
                mutate(snapshot)

            updated_checksum = snapshot.checksum
            if updated_checksum != baseline_checksum:
                self._persist(
                    ref,
                    snapshot.data,
                    backup=backup,
                )
            return snapshot.copy()

    @contextmanager
    def with_transaction(
        self,
        source: SourceRef | str,
        *,
        backup: bool = True,
    ) -> Iterator[ManifestTransaction]:
        """Context manager pairing manifest writes with rollback hooks."""

        ref = self._coerce_source(source)
        with self._acquire_lock(ref):
            baseline = self._read_manifest(ref)
            baseline_snapshot = ManifestSnapshot(
                source=ref,
                data=copy.deepcopy(baseline),
                modules_key=self._settings.modules_key,
                db_module_key=self._settings.db_module_key,
            )
            txn = ManifestTransaction(
                snapshot=baseline_snapshot,
                baseline_checksum=_compute_checksum(baseline),
                backup_enabled=backup,
            )
            try:
                yield txn
            except Exception:
                txn._run_rollback()
                raise
            else:
                try:
                    updated_checksum = txn.snapshot.checksum
                    if updated_checksum != txn.baseline_checksum:
                        self._persist(
                            ref,
                            txn.snapshot.data,
                            backup=txn.backup_enabled,
                        )
                    txn.result = txn.snapshot.copy()
                except Exception as exc:  # pragma: no cover
                    txn._run_rollback()
                    raise ManifestTransactionError(
                        "Manifest transaction failed"
                    ) from exc
                else:
                    txn._run_commit()

    def migrate(
        self,
        source: SourceRef | str,
        *,
        dry_run: bool = False,
    ) -> bool:
        """Trigger a manifest migration, returning ``True`` if applied."""

        ref = self._coerce_source(source)
        with self._acquire_lock(ref):
            data = self._read_manifest(ref)
            result = self._migrator.migrate(
                source=ref,
                data=data,
                dry_run=dry_run,
            )
            if result.applied and not dry_run:
                self._persist(ref, dict(result.data), backup=True)
            return result.applied

    def _coerce_source(self, source: SourceRef | str) -> SourceRef:
        if isinstance(source, SourceRef):
            return source
        return self.resolve(source)

    @contextmanager
    def _acquire_lock(self, source: SourceRef) -> Iterator[FileLock]:
        lock_path = build_lock_path(
            source.manifest_path,
            suffix=self._settings.lock_suffix,
        )
        lock = FileLock(
            path=lock_path,
            timeout=self._settings.lock_timeout,
            poll_interval=self._settings.lock_poll_interval,
        )
        try:
            lock.acquire()
        except ManifestLockError as exc:  # pragma: no cover - runtime failure
            raise ManifestError(str(exc)) from exc
        try:
            yield lock
        finally:
            lock.release()

    def _read_manifest(self, source: SourceRef) -> dict[str, Any]:
        path = source.manifest_path
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestReadError(
                f"Failed to read manifest at {path}: {exc}"
            ) from exc
        if not text.strip():
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestReadError(
                f"Malformed manifest at {path}: {exc}"
            ) from exc
        if isinstance(payload, dict):
            return payload
        raise ManifestReadError(f"Manifest at {path} is not a JSON object")

    def _persist(
        self,
        source: SourceRef,
        data: Mapping[str, Any],
        *,
        backup: bool,
    ) -> None:
        source.ensure_directories()
        path = source.manifest_path
        if backup and self._settings.backups_enabled:
            try:
                create_backup(
                    path,
                    suffix=self._settings.backup_suffix,
                    retention=self._settings.backup_retention,
                    timestamp=self._now(),
                )
            except ManifestBackupError as exc:
                raise ManifestWriteError(
                    f"Failed backing up manifest for {source.name}: {exc}"
                ) from exc

        payload = _serialize(data)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
        except OSError as exc:
            raise ManifestWriteError(
                f"Failed staging manifest for {source.name}: {exc}"
            ) from exc

        try:
            os.replace(temp_path, path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise ManifestWriteError(
                f"Failed writing manifest for {source.name}: {exc}"
            ) from exc

        self._logger.info(
            "manifest-write",
            source=source.name,
            path=str(path),
        )
