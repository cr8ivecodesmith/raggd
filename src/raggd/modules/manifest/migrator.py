"""Legacy manifest migration utilities."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from raggd.core.logging import Logger, get_logger

from .config import ManifestSettings
from .types import SourceRef

__all__ = [
    "ManifestMigrationResult",
    "ManifestMigrator",
]


MODULES_VERSION = 1
"""Current manifest modules layout version."""

SOURCE_MODULE_KEY = "source"
"""Module key used for source-specific manifest state."""

_LEGACY_SOURCE_FIELDS = frozenset(
    {
        "name",
        "path",
        "enabled",
        "target",
        "last_refresh_at",
        "last_health",
    }
)

_DEFAULT_DB_MODULE_PAYLOAD = {
    "bootstrap_shortuuid7": None,
    "head_migration_uuid7": None,
    "head_migration_shortuuid7": None,
    "ledger_checksum": None,
    "last_vacuum_at": None,
    "last_ensure_at": None,
    "pending_migrations": [],
}


@dataclass(frozen=True, slots=True)
class ManifestMigrationResult:
    """Outcome of a manifest migration attempt."""

    applied: bool
    data: Mapping[str, Any]
    reason: str | None = None


class ManifestMigrator:
    """Apply structural migrations to source manifests."""

    def __init__(
        self,
        *,
        settings: ManifestSettings,
        logger: Logger | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger or get_logger(
            __name__,
            component="manifest-migrator",
        )

    def migrate(
        self,
        *,
        source: SourceRef,
        data: Mapping[str, Any],
        dry_run: bool = False,
    ) -> ManifestMigrationResult:
        """Return a migrated manifest mapping if changes are required."""

        modules_key = self._settings.modules_key
        db_module_key = self._settings.db_module_key

        updated = copy.deepcopy(dict(data))
        modules_value = updated.get(modules_key)

        changes: list[str] = []

        if not isinstance(modules_value, dict):
            modules_value = {}
            updated[modules_key] = modules_value
            changes.append("initialized modules namespace")

        source_module = modules_value.get(SOURCE_MODULE_KEY)
        if not isinstance(source_module, dict):
            source_module = {}
            modules_value[SOURCE_MODULE_KEY] = source_module
            changes.append("created modules.source payload")

        moved_fields = False
        for field in _LEGACY_SOURCE_FIELDS:
            if field in updated:
                source_module[field] = copy.deepcopy(updated.pop(field))
                moved_fields = True
        if moved_fields:
            changes.append("relocated legacy source fields")

        db_module = modules_value.get(db_module_key)
        if not isinstance(db_module, dict):
            modules_value[db_module_key] = copy.deepcopy(
                _DEFAULT_DB_MODULE_PAYLOAD
            )
            changes.append("seeded modules.db defaults")
        else:
            seeded = False
            for key, default_value in _DEFAULT_DB_MODULE_PAYLOAD.items():
                if key not in db_module:
                    db_module[key] = copy.deepcopy(default_value)
                    seeded = True
            if seeded:
                changes.append("completed modules.db defaults")

        current_version = updated.get("modules_version")
        if current_version != MODULES_VERSION:
            updated["modules_version"] = MODULES_VERSION
            changes.append("stamped modules_version")

        if not changes:
            return ManifestMigrationResult(applied=False, data=data)

        reason = "; ".join(changes)

        if not dry_run:
            self._logger.info(
                "manifest-migration",
                source=source.name,
                message=reason,
            )

        return ManifestMigrationResult(
            applied=True,
            data=updated,
            reason=reason,
        )
