"""Lifecycle backend interfaces and SQLite implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.resources
import sqlite3
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
import uuid

from raggd.core.logging import Logger, get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.manifest import ManifestSettings

from .migrations import (
    Migration,
    MigrationLoadError,
    MigrationPlan,
    MigrationRunner,
)
from .models import DbManifestState
from .settings import DbModuleSettings

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
        now: datetime,
    ) -> DbEnsureOutcome: ...

    def upgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int | None,
        now: datetime,
    ) -> DbUpgradeOutcome: ...

    def downgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int,
        now: datetime,
    ) -> DbDowngradeOutcome: ...

    def info(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        include_schema: bool,
        now: datetime,
    ) -> DbInfoOutcome: ...

    def vacuum(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        concurrency: int | str | None,
        now: datetime,
    ) -> DbVacuumOutcome: ...

    def run(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        sql_path: Path,
        autocommit: bool,
        now: datetime,
    ) -> DbRunOutcome: ...

    def reset(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        force: bool,
        now: datetime,
    ) -> DbResetOutcome: ...


@dataclass(slots=True)
class _SchemaMeta:
    bootstrap_shortuuid7: str
    head_uuid: uuid.UUID
    head_short: str
    ledger_checksum: str
    created_at: datetime
    updated_at: datetime
    last_vacuum_at: datetime | None
    last_sql_run_at: datetime | None


@dataclass(slots=True)
class _DbState:
    applied: tuple[Migration, ...]
    pending: tuple[str, ...]
    meta: _SchemaMeta


class SQLiteLifecycleBackend(DbLifecycleBackend):
    """Concrete lifecycle backend backed by SQLite migrations."""

    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        settings: DbModuleSettings,
        manifest_settings: ManifestSettings,
        logger: Logger | None,
        now: Callable[[], datetime],
    ) -> None:
        self._workspace = workspace
        self._settings = settings
        self._manifest_settings = manifest_settings
        self._logger = logger or get_logger(__name__, component="db-backend")
        self._now = now
        self._runner = self._load_runner(settings.migrations_path)

    # ------------------------------------------------------------------
    # Lifecycle operations

    def ensure(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        now: datetime,
    ) -> DbEnsureOutcome:
        db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            state = self._load_state(conn, timestamp=now)
            plan = self._runner.pending(m.short_value for m in state.applied)

            applied: list[str] = []
            if self._settings.ensure_auto_upgrade and plan.migrations:
                applied = list(self._apply_upgrades(conn, plan, now))

            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbEnsureOutcome(
                status=manifest_state,
                applied_migrations=tuple(applied),
            )

    def upgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int | None,
        now: datetime,
    ) -> DbUpgradeOutcome:
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            state = self._load_state(conn, timestamp=now)

            plan = self._runner.pending(m.short_value for m in state.applied)
            if steps is not None:
                plan = MigrationPlan(plan.migrations[:steps])

            applied = list(self._apply_upgrades(conn, plan, now))
            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbUpgradeOutcome(
                status=manifest_state,
                applied_migrations=tuple(applied),
            )

    def downgrade(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        steps: int,
        now: datetime,
    ) -> DbDowngradeOutcome:
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            state = self._load_state(conn, timestamp=now)

            applied_shorts = tuple(m.short_value for m in state.applied)
            plan = self._runner.downgrade_plan(applied_shorts, steps)
            rolled_back = list(self._apply_downgrades(conn, plan, now))

            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbDowngradeOutcome(
                status=manifest_state,
                rolled_back_migrations=tuple(rolled_back),
            )

    def info(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        include_schema: bool,
        now: datetime,
    ) -> DbInfoOutcome:
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)

            schema_dump: str | None = None
            if include_schema:
                schema_dump = "\n".join(conn.iterdump())

            metadata = {
                "bootstrap_shortuuid7": state.meta.bootstrap_shortuuid7,
                "head_migration_uuid7": str(state.meta.head_uuid),
                "head_migration_shortuuid7": state.meta.head_short,
                "ledger_checksum": state.meta.ledger_checksum,
                "applied_migrations": [m.short_value for m in state.applied],
                "pending_migrations": list(state.pending),
            }

            return DbInfoOutcome(
                status=manifest_state,
                schema=schema_dump,
                metadata=metadata,
            )

    def vacuum(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        concurrency: int | str | None,
        now: datetime,
    ) -> DbVacuumOutcome:
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            conn.execute("VACUUM")
            self._update_meta(conn, now=now, last_vacuum_at=now)
            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbVacuumOutcome(status=manifest_state)

    def run(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        sql_path: Path,
        autocommit: bool,
        now: datetime,
    ) -> DbRunOutcome:
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            sql_text = sql_path.read_text(encoding="utf-8")
            if autocommit:
                conn.executescript(sql_text)
            else:
                with conn:
                    conn.executescript(sql_text)
            self._update_meta(conn, now=now, last_sql_run_at=now)
            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbRunOutcome(status=manifest_state)

    def reset(
        self,
        *,
        source: str,
        db_path: Path,
        manifest: DbManifestState,
        force: bool,
        now: datetime,
    ) -> DbResetOutcome:
        if db_path.exists():
            db_path.unlink()
        with self._connect(db_path) as conn:
            self._initialize_schema(conn)
            plan = self._runner.pending(())
            list(self._apply_upgrades(conn, plan, now))
            state = self._load_state(conn, timestamp=now)
            manifest_state = self._manifest_state(state)
            return DbResetOutcome(status=manifest_state)

    # ------------------------------------------------------------------
    # Internal helpers

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                bootstrap_shortuuid7 TEXT NOT NULL,
                head_migration_uuid7 TEXT NOT NULL,
                head_migration_shortuuid7 TEXT NOT NULL,
                ledger_checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_vacuum_at TEXT,
                last_sql_run_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid7 TEXT UNIQUE NOT NULL,
                shortuuid7 TEXT UNIQUE NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('up','down')),
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_schema_migrations_short
            ON schema_migrations(shortuuid7)
            """
        )

    def _load_state(
        self,
        conn: sqlite3.Connection,
        *,
        timestamp: datetime | None = None,
    ) -> _DbState:
        stamp = timestamp or self._now()
        rows = conn.execute(
            "SELECT shortuuid7, uuid7, direction, checksum, applied_at "
            "FROM schema_migrations"
        ).fetchall()
        direction_map = {row["shortuuid7"]: row for row in rows}

        applied: list[Migration] = []
        for migration in self._runner.list_all():
            record = direction_map.get(migration.short_value)
            if record and record["direction"] == "up":
                applied.append(migration)

        pending_plan = self._runner.pending(m.short_value for m in applied)
        meta = self._load_meta(conn, applied, timestamp=stamp)
        return _DbState(
            applied=tuple(applied),
            pending=pending_plan.short_values(),
            meta=meta,
        )

    def _apply_upgrades(
        self,
        conn: sqlite3.Connection,
        plan: MigrationPlan,
        now: datetime,
    ) -> list[str]:
        applied: list[str] = []
        if not plan.migrations:
            return applied
        with conn:
            for migration in plan.migrations:
                conn.executescript(migration.up_sql)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (
                        uuid7,
                        shortuuid7,
                        direction,
                        checksum,
                        applied_at
                    )
                    VALUES (?, ?, 'up', ?, ?)
                    ON CONFLICT(uuid7) DO UPDATE SET
                        direction='up',
                        checksum=excluded.checksum,
                        applied_at=excluded.applied_at
                    """,
                    (
                        str(migration.uuid),
                        migration.short_value,
                        migration.checksum_up,
                        _to_iso(now),
                    ),
                )
                applied.append(migration.short_value)
            self._update_meta(conn, now=now)
        return applied

    def _apply_downgrades(
        self,
        conn: sqlite3.Connection,
        plan: MigrationPlan,
        now: datetime,
    ) -> list[str]:
        rolled_back: list[str] = []
        if not plan.migrations:
            return rolled_back
        with conn:
            for migration in plan.migrations:
                if not migration.down_sql:
                    raise MigrationLoadError(
                        "Missing .down script for migration "
                        f"{migration.short_value}"
                    )
                conn.executescript(migration.down_sql)
                checksum = migration.checksum_down or migration.checksum_up
                conn.execute(
                    """
                    INSERT INTO schema_migrations (
                        uuid7,
                        shortuuid7,
                        direction,
                        checksum,
                        applied_at
                    )
                    VALUES (?, ?, 'down', ?, ?)
                    ON CONFLICT(uuid7) DO UPDATE SET
                        direction='down',
                        checksum=excluded.checksum,
                        applied_at=excluded.applied_at
                    """,
                    (
                        str(migration.uuid),
                        migration.short_value,
                        checksum,
                        _to_iso(now),
                    ),
                )
                rolled_back.append(migration.short_value)
            self._update_meta(conn, now=now)
        return rolled_back

    def _load_meta(
        self,
        conn: sqlite3.Connection,
        applied: Sequence[Migration],
        *,
        timestamp: datetime,
    ) -> _SchemaMeta:
        row = conn.execute("SELECT * FROM schema_meta WHERE id = 1").fetchone()
        bootstrap = self._runner.bootstrap()
        head = applied[-1] if applied else bootstrap
        checksum = _ledger_checksum(applied)
        now = timestamp

        if row is None:
            conn.execute(
                """
                INSERT INTO schema_meta (
                    id,
                    bootstrap_shortuuid7,
                    head_migration_uuid7,
                    head_migration_shortuuid7,
                    ledger_checksum,
                    created_at,
                    updated_at,
                    last_vacuum_at,
                    last_sql_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    1,
                    bootstrap.short_value,
                    str(head.uuid),
                    head.short_value,
                    checksum,
                    _to_iso(now),
                    _to_iso(now),
                ),
            )
            last_vacuum_at = None
            last_sql_run_at = None
            created_at = now
        else:
            created_at = _from_iso(row["created_at"]) or now
            last_vacuum_at = _from_iso(row["last_vacuum_at"])
            last_sql_run_at = _from_iso(row["last_sql_run_at"])

            conn.execute(
                """
                UPDATE schema_meta
                SET
                    head_migration_uuid7 = ?,
                    head_migration_shortuuid7 = ?,
                    ledger_checksum = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    str(head.uuid),
                    head.short_value,
                    checksum,
                    _to_iso(now),
                ),
            )
        return _SchemaMeta(
            bootstrap_shortuuid7=bootstrap.short_value,
            head_uuid=head.uuid,
            head_short=head.short_value,
            ledger_checksum=checksum,
            created_at=created_at,
            updated_at=now,
            last_vacuum_at=last_vacuum_at,
            last_sql_run_at=last_sql_run_at,
        )

    def _update_meta(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime,
        last_vacuum_at: datetime | None = None,
        last_sql_run_at: datetime | None = None,
    ) -> None:
        state = self._load_state(conn, timestamp=now)
        updates: dict[str, str | None] = {
            "head_migration_uuid7": str(state.meta.head_uuid),
            "head_migration_shortuuid7": state.meta.head_short,
            "ledger_checksum": state.meta.ledger_checksum,
            "updated_at": _to_iso(now),
            "last_vacuum_at": _to_iso(last_vacuum_at)
            if last_vacuum_at
            else _to_iso(state.meta.last_vacuum_at),
            "last_sql_run_at": _to_iso(last_sql_run_at)
            if last_sql_run_at
            else _to_iso(state.meta.last_sql_run_at),
        }
        conn.execute(
            """
            UPDATE schema_meta
            SET
                head_migration_uuid7 = :head_migration_uuid7,
                head_migration_shortuuid7 = :head_migration_shortuuid7,
                ledger_checksum = :ledger_checksum,
                updated_at = :updated_at,
                last_vacuum_at = :last_vacuum_at,
                last_sql_run_at = :last_sql_run_at
            WHERE id = 1
            """,
            updates,
        )

    def _manifest_state(self, state: _DbState) -> DbManifestState:
        return DbManifestState(
            bootstrap_shortuuid7=state.meta.bootstrap_shortuuid7,
            head_migration_uuid7=str(state.meta.head_uuid),
            head_migration_shortuuid7=state.meta.head_short,
            ledger_checksum=state.meta.ledger_checksum,
            last_vacuum_at=state.meta.last_vacuum_at,
            last_sql_run_at=state.meta.last_sql_run_at,
            pending_migrations=state.pending,
        )

    def _load_runner(self, path_value: str) -> MigrationRunner:
        path = Path(path_value)
        if not path.is_absolute():
            base = importlib.resources.files("raggd.modules.db")
            path = Path(base.joinpath(path_value))
        return MigrationRunner.from_path(path)


def _ledger_checksum(applied: Sequence[Migration]) -> str:
    parts = [
        f"{migration.short_value}:{migration.checksum_up or ''}"
        for migration in applied
    ]
    payload = "|".join(parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_default_backend(
    *,
    workspace: WorkspacePaths,
    settings: DbModuleSettings,
    manifest_settings: ManifestSettings,
    logger: Logger | None = None,
    now: Callable[[], datetime] | None = None,
) -> DbLifecycleBackend:
    """Return the default backend implementation."""

    return SQLiteLifecycleBackend(
        workspace=workspace,
        settings=settings,
        manifest_settings=manifest_settings,
        logger=logger,
        now=now or (lambda: datetime.now(timezone.utc)),
    )
