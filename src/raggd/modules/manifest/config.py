"""Settings adapters for the manifest subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_DEFAULT_MODULES_KEY = "modules"
_DEFAULT_DB_MODULE_KEY = "db"
_DEFAULT_BACKUP_RETENTION = 5
_DEFAULT_LOCK_TIMEOUT = 5.0
_DEFAULT_LOCK_POLL_INTERVAL = 0.1
_DEFAULT_LOCK_SUFFIX = ".lock"
_DEFAULT_BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True, slots=True)
class ManifestSettings:
    """Configuration bundle controlling manifest IO behavior."""

    modules_key: str = _DEFAULT_MODULES_KEY
    db_module_key: str = _DEFAULT_DB_MODULE_KEY
    backup_retention: int = _DEFAULT_BACKUP_RETENTION
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT
    lock_poll_interval: float = _DEFAULT_LOCK_POLL_INTERVAL
    lock_suffix: str = _DEFAULT_LOCK_SUFFIX
    backup_suffix: str = _DEFAULT_BACKUP_SUFFIX
    strict_writes: bool = True
    backups_enabled: bool = True

    def module_key(self, module: str) -> tuple[str, str]:
        """Return the fully qualified modules key for ``module``."""

        return (self.modules_key, module)


def manifest_settings_from_mapping(
    payload: Mapping[str, Any] | None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> ManifestSettings:
    """Build :class:`ManifestSettings` from an app configuration mapping.

    ``payload`` is expected to contain a ``db`` entry following the defaults
    defined in ``raggd.defaults.toml``. Missing keys fall back to sensible
    defaults so consumers can progressively adopt manifest settings without
    breaking existing workspaces.
    """

    db_settings: Mapping[str, Any] | None = None
    if payload is not None:
        db_candidate = payload.get("db")
        if isinstance(db_candidate, Mapping):
            db_settings = db_candidate

    def _read(key: str, default: Any) -> Any:
        if overrides and key in overrides:
            return overrides[key]
        if db_settings and key in db_settings:
            return db_settings[key]
        return default

    modules_key = str(_read("manifest_modules_key", _DEFAULT_MODULES_KEY))
    db_module_key = str(_read("manifest_db_module_key", _DEFAULT_DB_MODULE_KEY))

    backup_retention = int(
        _read("manifest_backup_retention", _DEFAULT_BACKUP_RETENTION)
    )
    lock_timeout = float(_read("manifest_lock_timeout", _DEFAULT_LOCK_TIMEOUT))
    lock_poll_interval = float(
        _read("manifest_lock_poll_interval", _DEFAULT_LOCK_POLL_INTERVAL)
    )
    lock_suffix = str(_read("manifest_lock_suffix", _DEFAULT_LOCK_SUFFIX))
    backup_suffix = str(_read("manifest_backup_suffix", _DEFAULT_BACKUP_SUFFIX))
    strict_writes = bool(_read("manifest_strict", True))
    backups_enabled = bool(_read("manifest_backups_enabled", True))

    return ManifestSettings(
        modules_key=modules_key,
        db_module_key=db_module_key,
        backup_retention=backup_retention,
        lock_timeout=lock_timeout,
        lock_poll_interval=lock_poll_interval,
        lock_suffix=lock_suffix,
        backup_suffix=backup_suffix,
        strict_writes=strict_writes,
        backups_enabled=backups_enabled,
    )


__all__ = [
    "ManifestSettings",
    "manifest_settings_from_mapping",
]
