"""Health integration for the database module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib.resources
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence
import hashlib

from raggd.modules import HealthReport, HealthStatus, WorkspaceHandle
from raggd.modules.manifest import (
    ManifestError,
    ManifestService,
    manifest_settings_from_mapping,
)

from .migrations import MigrationRunner, MigrationLoadError
from .models import DbManifestState
from .settings import DbModuleSettings, db_settings_from_mapping

__all__ = ["db_health_hook"]


@dataclass(slots=True)
class _ObservedState:
    """Snapshot of on-disk database state for health comparisons."""

    bootstrap_shortuuid7: str
    head_migration_uuid7: str
    head_migration_shortuuid7: str
    ledger_checksum: str
    pending_migrations: tuple[str, ...]
    applied_migrations: tuple[str, ...]
    last_vacuum_at: datetime | None


class _DbInspectionError(RuntimeError):
    """Raised when the database cannot be inspected safely."""

    def __init__(self, message: str, *, actions: Sequence[str] | None = None):
        super().__init__(message)
        self.actions: tuple[str, ...] = tuple(actions or ())


_SEVERITY_ORDER: dict[HealthStatus, int] = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.DEGRADED: 2,
    HealthStatus.ERROR: 3,
}


def _resolve_migrations_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    base = importlib.resources.files("raggd.modules.db")
    return Path(base.joinpath(path_value))


def _load_runner(settings: DbModuleSettings) -> MigrationRunner:
    path = _resolve_migrations_path(settings.migrations_path)
    return MigrationRunner.from_path(path)


def _compute_ledger_checksum(
    migrations: Iterable[str],
    *,
    runner: MigrationRunner,
) -> str:
    lookup = {m.short_value: m for m in runner.list_all()}
    parts = []
    for short in migrations:
        migration = lookup.get(short)
        if migration is None:
            raise _DbInspectionError(
                f"Unknown migration recorded in ledger: {short}",
                actions=("Verify packaged migrations match workspace database.",),
            )
        parts.append(f"{migration.short_value}:{migration.checksum_up or ''}")
    payload = "|".join(parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _parse_iso(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _inspect_database(
    db_path: Path,
    *,
    runner: MigrationRunner,
) -> _ObservedState:
    if not db_path.exists():
        raise _DbInspectionError(
            f"Database missing at {db_path}",
            actions=("Run `raggd db ensure <source>` to bootstrap the database.",),
        )

    uri = f"file:{db_path}?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
    except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
        raise _DbInspectionError(
            f"Failed to open database {db_path}: {exc}",
            actions=("Inspect permissions or recreate the database via ensure.",),
        ) from exc

    connection.row_factory = sqlite3.Row
    try:
        try:
            meta = connection.execute(
                """
                SELECT
                    bootstrap_shortuuid7,
                    head_migration_uuid7,
                    head_migration_shortuuid7,
                    ledger_checksum,
                    last_vacuum_at
                FROM schema_meta
                WHERE id = 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise _DbInspectionError(
                "Database schema metadata missing",
                actions=("Run `raggd db ensure <source>` to initialize schema.",),
            ) from exc

        if meta is None:
            raise _DbInspectionError(
                "Database schema metadata not initialized",
                actions=("Run `raggd db ensure <source>` to initialize schema.",),
            )

        rows = connection.execute(
            """
            SELECT shortuuid7, direction, checksum
            FROM schema_migrations
            ORDER BY applied_at
            """
        ).fetchall()
    finally:
        connection.close()

    recorded: dict[str, sqlite3.Row] = {}
    for row in rows:
        short = row["shortuuid7"]
        recorded[short] = row

    ordered_migrations = runner.list_all()
    applied: list[str] = []
    pending: list[str] = []

    for migration in ordered_migrations:
        entry = recorded.get(migration.short_value)
        if entry and entry["direction"] == "up":
            applied.append(migration.short_value)
        else:
            pending.append(migration.short_value)

    expected_checksum = _compute_ledger_checksum(applied, runner=runner)
    ledger_checksum = meta["ledger_checksum"]
    if ledger_checksum != expected_checksum:
        raise _DbInspectionError(
            "Ledger checksum mismatch detected",
            actions=(
                "Verify migration files were not modified and rerun "
                "`raggd db ensure <source>`.",
            ),
        )

    head_short = applied[-1] if applied else ordered_migrations[0].short_value
    lookup = {m.short_value: m for m in ordered_migrations}
    head_uuid = str(lookup[head_short].uuid)

    return _ObservedState(
        bootstrap_shortuuid7=str(meta["bootstrap_shortuuid7"]),
        head_migration_uuid7=head_uuid,
        head_migration_shortuuid7=head_short,
        ledger_checksum=ledger_checksum,
        pending_migrations=tuple(pending),
        applied_migrations=tuple(applied),
        last_vacuum_at=_parse_iso(meta["last_vacuum_at"]),
    )


def _within_drift_window(
    manifest_state: DbManifestState,
    *,
    now: datetime,
    threshold_seconds: int,
) -> bool:
    if threshold_seconds <= 0:
        return False
    if manifest_state.last_ensure_at is None:
        return False
    delta = now - manifest_state.last_ensure_at
    return delta.total_seconds() <= threshold_seconds


def _evaluate_source(
    *,
    name: str,
    handle: WorkspaceHandle,
    manifest_service: ManifestService,
    runner: MigrationRunner,
    db_settings: DbModuleSettings,
    now: datetime,
) -> HealthReport:
    manifest_actions = (
        f"Run `raggd db ensure {name}` to regenerate the manifest entry.",
    )

    try:
        snapshot = manifest_service.load(name)
    except ManifestError as exc:
        return HealthReport(
            name=name,
            status=HealthStatus.ERROR,
            summary=f"Failed to read manifest: {exc}",
            actions=manifest_actions,
            last_refresh_at=None,
        )

    module_payload = snapshot.module(manifest_service.settings.db_module_key)
    if not isinstance(module_payload, dict):
        return HealthReport(
            name=name,
            status=HealthStatus.ERROR,
            summary="Database manifest entry missing.",
            actions=manifest_actions,
            last_refresh_at=None,
        )

    manifest_state = DbManifestState.from_mapping(module_payload)

    try:
        observed = _inspect_database(
            handle.paths.source_database_path(name),
            runner=runner,
        )
    except _DbInspectionError as exc:
        # Inject the concrete source into action hints when placeholders exist.
        actions = tuple(
            action.replace("<source>", name) for action in exc.actions
        ) or manifest_actions
        return HealthReport(
            name=name,
            status=HealthStatus.ERROR,
            summary=str(exc),
            actions=actions,
            last_refresh_at=manifest_state.last_ensure_at,
        )

    status = HealthStatus.OK
    issues: list[str] = []
    actions: set[str] = set()

    def elevate(candidate: HealthStatus) -> None:
        nonlocal status
        if _SEVERITY_ORDER[candidate] > _SEVERITY_ORDER[status]:
            status = candidate

    if observed.pending_migrations:
        elevate(HealthStatus.DEGRADED)
        issues.append(
            "pending migrations: " + ", ".join(observed.pending_migrations)
        )
        actions.add(f"Run `raggd db upgrade {name}` to apply migrations.")

    manifest_pending = tuple(manifest_state.pending_migrations)
    if manifest_pending != observed.pending_migrations:
        if not _within_drift_window(
            manifest_state,
            now=now,
            threshold_seconds=int(db_settings.drift_warning_seconds),
        ):
            elevate(HealthStatus.DEGRADED)
            issues.append("manifest pending migrations out of sync")
            actions.add(
                f"Run `raggd db ensure {name}` to resync manifest metadata."
            )

    drift_components: list[str] = []
    if manifest_state.head_migration_shortuuid7 != (
        observed.head_migration_shortuuid7
    ):
        drift_components.append("head migration")
    if manifest_state.head_migration_uuid7 != observed.head_migration_uuid7:
        drift_components.append("head migration UUID")
    if manifest_state.bootstrap_shortuuid7 and (
        manifest_state.bootstrap_shortuuid7 != observed.bootstrap_shortuuid7
    ):
        drift_components.append("bootstrap identifier")
    if manifest_state.ledger_checksum and (
        manifest_state.ledger_checksum != observed.ledger_checksum
    ):
        drift_components.append("ledger checksum")

    if drift_components:
        if not _within_drift_window(
            manifest_state,
            now=now,
            threshold_seconds=int(db_settings.drift_warning_seconds),
        ):
            elevate(HealthStatus.DEGRADED)
            joined = ", ".join(drift_components)
            issues.append(f"manifest drift detected ({joined})")
            actions.add(
                f"Run `raggd db ensure {name}` to refresh manifest metadata."
            )

    if db_settings.vacuum_max_stale_days >= 0:
        stale_limit = timedelta(days=db_settings.vacuum_max_stale_days)
        if observed.last_vacuum_at is None:
            elevate(HealthStatus.DEGRADED)
            issues.append("vacuum has never been executed")
            actions.add(f"Run `raggd db vacuum {name}` to perform maintenance.")
        elif now - observed.last_vacuum_at > stale_limit:
            elevate(HealthStatus.DEGRADED)
            stale_days = (now - observed.last_vacuum_at).days
            issues.append(
                f"vacuum stale ({stale_days} days since last maintenance)"
            )
            actions.add(f"Run `raggd db vacuum {name}` to perform maintenance.")

    summary = ", ".join(issues) if issues else "database healthy"

    return HealthReport(
        name=name,
        status=status,
        summary=summary,
        actions=tuple(sorted(actions)),
        last_refresh_at=manifest_state.last_ensure_at,
    )


def db_health_hook(handle: WorkspaceHandle) -> Sequence[HealthReport]:
    """Evaluate health for each configured source database."""

    toggle = handle.config.modules.get("db")
    if toggle is not None and not toggle.enabled:
        return (
            HealthReport(
                name="db-module",
                status=HealthStatus.UNKNOWN,
                summary="Database module disabled via configuration.",
                actions=(
                    "Set `modules.db.enabled = true` in raggd.toml to enable checks.",
                ),
                last_refresh_at=None,
            ),
        )

    payload = handle.config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_mapping(payload)
    db_settings = db_settings_from_mapping(payload)

    try:
        runner = _load_runner(db_settings)
    except MigrationLoadError as exc:
        return (
            HealthReport(
                name="migrations",
                status=HealthStatus.ERROR,
                summary=f"Failed to load migrations: {exc}",
                actions=(
                    "Verify packaged SQL migrations are present and reinstall raggd.",
                ),
                last_refresh_at=None,
            ),
        )

    manifest_service = ManifestService(
        workspace=handle.paths,
        settings=manifest_settings,
    )

    now = datetime.now(timezone.utc)
    reports: list[HealthReport] = []

    for name, _ in sorted(handle.config.iter_workspace_sources()):
        report = _evaluate_source(
            name=name,
            handle=handle,
            manifest_service=manifest_service,
            runner=runner,
            db_settings=db_settings,
            now=now,
        )
        reports.append(report)

    return tuple(reports)

