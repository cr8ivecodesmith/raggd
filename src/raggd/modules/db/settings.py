"""Configuration helpers for the database module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "DbModuleSettings",
    "db_settings_from_mapping",
]


@dataclass(frozen=True, slots=True)
class DbModuleSettings:
    """Normalized configuration values for the database module."""

    migrations_path: str = "resources/db/migrations"
    ensure_auto_upgrade: bool = True
    vacuum_max_stale_days: int = 7
    vacuum_concurrency: str | int = "auto"
    run_allow_outside: bool = True
    run_autocommit_default: bool = False
    drift_warning_seconds: int = 0
    lock_timeout: float = 10.0
    lock_poll_interval: float = 0.1
    lock_suffix: str = ".lock"
    lock_namespace: str = "db"
    info_count_timeout_ms: int = 1000
    info_count_row_limit: int = 500_000


def db_settings_from_mapping(
    payload: Mapping[str, Any] | None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> DbModuleSettings:
    """Build :class:`DbModuleSettings` from a configuration mapping."""

    db_settings: Mapping[str, Any] | None = None
    if payload is not None:
        candidate = payload.get("db") if isinstance(payload, Mapping) else None
        if isinstance(candidate, Mapping):
            db_settings = candidate

    def _read(key: str, default: Any) -> Any:
        if overrides and key in overrides:
            return overrides[key]
        if db_settings and key in db_settings:
            return db_settings[key]
        return default

    migrations_path = str(_read("migrations_path", "resources/db/migrations"))
    ensure_auto_upgrade = bool(_read("ensure_auto_upgrade", True))
    vacuum_max_stale_days = int(_read("vacuum_max_stale_days", 7))
    vacuum_concurrency = _read("vacuum_concurrency", "auto")
    run_allow_outside = bool(_read("run_allow_outside", True))
    run_autocommit_default = bool(_read("run_autocommit_default", False))
    drift_warning_seconds = int(_read("drift_warning_seconds", 0))
    lock_timeout = float(_read("lock_timeout", 10.0))
    lock_poll_interval = float(_read("lock_poll_interval", 0.1))
    lock_suffix = str(_read("lock_suffix", ".lock"))
    lock_namespace = str(_read("lock_namespace", "db"))
    info_count_timeout_ms = int(_read("info_count_timeout_ms", 1000))
    info_count_row_limit = int(_read("info_count_row_limit", 500_000))

    return DbModuleSettings(
        migrations_path=migrations_path,
        ensure_auto_upgrade=ensure_auto_upgrade,
        vacuum_max_stale_days=vacuum_max_stale_days,
        vacuum_concurrency=vacuum_concurrency,
        run_allow_outside=run_allow_outside,
        run_autocommit_default=run_autocommit_default,
        drift_warning_seconds=drift_warning_seconds,
        lock_timeout=lock_timeout,
        lock_poll_interval=lock_poll_interval,
        lock_suffix=lock_suffix,
        lock_namespace=lock_namespace,
        info_count_timeout_ms=info_count_timeout_ms,
        info_count_row_limit=info_count_row_limit,
    )
