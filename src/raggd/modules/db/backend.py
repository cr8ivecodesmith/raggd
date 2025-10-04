"""Lifecycle backend interfaces and default stubs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .models import DbManifestState

__all__ = [
    "DbEnsureOutcome",
    "DbUpgradeOutcome",
    "DbDowngradeOutcome",
    "DbInfoOutcome",
    "DbVacuumOutcome",
    "DbRunOutcome",
    "DbResetOutcome",
    "DbLifecycleBackend",
    "build_default_backend",
]


@dataclass(slots=True)
class DbEnsureOutcome:
    """Result payload returned from ``ensure`` operations."""

    status: DbManifestState
    applied_migrations: Sequence[str] = ()


@dataclass(slots=True)
class DbUpgradeOutcome:
    """Result payload returned from ``upgrade`` operations."""

    status: DbManifestState
    applied_migrations: Sequence[str]


@dataclass(slots=True)
class DbDowngradeOutcome:
    """Result payload returned from ``downgrade`` operations."""

    status: DbManifestState
    rolled_back_migrations: Sequence[str]


@dataclass(slots=True)
class DbInfoOutcome:
    """Information returned from ``info`` operations."""

    status: DbManifestState
    schema: str | None = None
    metadata: Mapping[str, object] | None = None


@dataclass(slots=True)
class DbVacuumOutcome:
    """Result payload returned from ``vacuum`` operations."""

    status: DbManifestState


@dataclass(slots=True)
class DbRunOutcome:
    """Result payload returned from ``run`` operations."""

    status: DbManifestState


@dataclass(slots=True)
class DbResetOutcome:
    """Result payload returned from ``reset`` operations."""

    status: DbManifestState


class DbLifecycleBackend(Protocol):
    """Backend interface coordinating concrete SQLite operations."""

    def ensure(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
    ) -> DbEnsureOutcome: ...

    def upgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int | None,
    ) -> DbUpgradeOutcome: ...

    def downgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int,
    ) -> DbDowngradeOutcome: ...

    def info(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        include_schema: bool,
    ) -> DbInfoOutcome: ...

    def vacuum(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        concurrency: int | str | None,
    ) -> DbVacuumOutcome: ...

    def run(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        sql_path: Path,
        autocommit: bool,
    ) -> DbRunOutcome: ...

    def reset(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        force: bool,
    ) -> DbResetOutcome: ...


class _NullLifecycleBackend:
    """Do-nothing backend used until the SQLite runner is implemented."""

    def ensure(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
    ) -> DbEnsureOutcome:
        return DbEnsureOutcome(status=manifest)

    def upgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int | None,
    ) -> DbUpgradeOutcome:
        return DbUpgradeOutcome(status=manifest, applied_migrations=())

    def downgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int,
    ) -> DbDowngradeOutcome:
        return DbDowngradeOutcome(status=manifest, rolled_back_migrations=())

    def info(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        include_schema: bool,
    ) -> DbInfoOutcome:
        return DbInfoOutcome(status=manifest, schema=None, metadata={})

    def vacuum(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        concurrency: int | str | None,
    ) -> DbVacuumOutcome:
        return DbVacuumOutcome(status=manifest)

    def run(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        sql_path: Path,
        autocommit: bool,
    ) -> DbRunOutcome:
        return DbRunOutcome(status=manifest)

    def reset(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        force: bool,
    ) -> DbResetOutcome:
        return DbResetOutcome(status=manifest)


def build_default_backend() -> DbLifecycleBackend:
    """Return the default backend implementation."""

    return _NullLifecycleBackend()

