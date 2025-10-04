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

        updated: dict[str, Any] = copy.deepcopy(dict(data))

        changes: list[str] = []

        modules_value, namespace_changes = self._ensure_modules_namespace(
            updated,
            modules_key,
        )
        changes.extend(namespace_changes)

        source_module, source_changes = self._ensure_source_module(
            modules_value
        )
        changes.extend(source_changes)

        relocated = self._relocate_legacy_fields(updated, source_module)
        if relocated is not None:
            changes.append(relocated)

        db_change = self._ensure_db_module_defaults(
            modules_value,
            db_module_key,
        )
        if db_change is not None:
            changes.append(db_change)

        version_change = self._ensure_modules_version(updated)
        if version_change is not None:
            changes.append(version_change)

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

    def _ensure_modules_namespace(
        self,
        updated: dict[str, Any],
        modules_key: str,
    ) -> tuple[dict[str, Any], list[str]]:
        modules_value = updated.get(modules_key)
        if isinstance(modules_value, dict):
            return modules_value, []

        modules_dict: dict[str, Any] = {}
        updated[modules_key] = modules_dict
        return modules_dict, ["initialized modules namespace"]

    def _ensure_source_module(
        self,
        modules_value: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        source_module = modules_value.get(SOURCE_MODULE_KEY)
        if isinstance(source_module, dict):
            return source_module, []

        source_payload: dict[str, Any] = {}
        modules_value[SOURCE_MODULE_KEY] = source_payload
        return source_payload, ["created modules.source payload"]

    def _relocate_legacy_fields(
        self,
        updated: dict[str, Any],
        source_module: dict[str, Any],
    ) -> str | None:
        moved_fields = False
        for field in _LEGACY_SOURCE_FIELDS:
            if field in updated:
                source_module[field] = copy.deepcopy(updated.pop(field))
                moved_fields = True

        if moved_fields:
            return "relocated legacy source fields"
        return None

    def _ensure_db_module_defaults(
        self,
        modules_value: dict[str, Any],
        db_module_key: str,
    ) -> str | None:
        db_module = modules_value.get(db_module_key)
        if not isinstance(db_module, dict):
            modules_value[db_module_key] = copy.deepcopy(
                _DEFAULT_DB_MODULE_PAYLOAD
            )
            return "seeded modules.db defaults"

        seeded = False
        for key, default_value in _DEFAULT_DB_MODULE_PAYLOAD.items():
            if key not in db_module:
                db_module[key] = copy.deepcopy(default_value)
                seeded = True

        if seeded:
            return "completed modules.db defaults"
        return None

    def _ensure_modules_version(self, updated: dict[str, Any]) -> str | None:
        if updated.get("modules_version") == MODULES_VERSION:
            return None

        updated["modules_version"] = MODULES_VERSION
        return "stamped modules_version"
