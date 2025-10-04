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
        modules_value = data.get(modules_key)
        if isinstance(modules_value, Mapping):
            return ManifestMigrationResult(applied=False, data=data)

        updated = copy.deepcopy(dict(data))
        updated[modules_key] = {}

        message = (
            "initialized modules namespace"
            if not isinstance(modules_value, Mapping)
            else "no-op"
        )

        if not dry_run:
            self._logger.info(
                "manifest-migration",
                source=source.name,
                message=message,
            )

        return ManifestMigrationResult(
            applied=True,
            data=updated,
            reason=message,
        )
