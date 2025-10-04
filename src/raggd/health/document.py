"""Health document models and persistence helpers."""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping

from pydantic import BaseModel, RootModel, Field

from raggd.modules.registry import HealthReport, HealthStatus

from .errors import HealthDocumentReadError, HealthDocumentWriteError


_SEVERITY_ORDER: dict[HealthStatus, int] = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.DEGRADED: 2,
    HealthStatus.ERROR: 3,
}


class HealthDetail(BaseModel):
    """Detailed record contributed by a module health hook."""

    name: str = Field(description="Identifier for the entity being checked.")
    status: HealthStatus = Field(
        description="Severity classification for the entry.",
    )
    summary: str | None = Field(
        default=None,
        description="Optional short explanation describing the status.",
    )
    actions: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Optional remediation steps suggested to the operator.",
    )
    last_refresh_at: datetime | None = Field(
        default=None,
        description=(
            "Timestamp representing the most recent refresh for the entity."
        ),
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }


class HealthModuleSnapshot(BaseModel):
    """Snapshot of aggregated health information for a module."""

    checked_at: datetime = Field(
        description="Timestamp when the health hook was evaluated.",
    )
    status: HealthStatus = Field(
        description="Highest-severity status reported by the module hook.",
    )
    details: tuple[HealthDetail, ...] = Field(
        default_factory=tuple,
        description="Ordered collection of individual health entries.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }


class HealthDocument(RootModel[dict[str, HealthModuleSnapshot]]):
    """Top-level representation of the `.health.json` file."""

    model_config = {
        "validate_assignment": True,
    }

    def modules(self) -> dict[str, HealthModuleSnapshot]:
        """Return document content as a mutable mapping copy."""

        return dict(self.root)

    def merge(
        self,
        updates: Mapping[str, HealthModuleSnapshot],
    ) -> "HealthDocument":
        """Return a new document with module entries replaced by updates."""

        merged = dict(self.root)
        merged.update(updates)
        ordered = OrderedDict(sorted(merged.items()))
        return HealthDocument.model_validate(ordered)


def build_module_snapshot(
    reports: Iterable[HealthReport],
    *,
    checked_at: datetime | None = None,
) -> HealthModuleSnapshot:
    """Create a module snapshot from a sequence of health reports."""

    timestamp = checked_at or datetime.now(timezone.utc)
    details: list[HealthDetail] = []
    status = HealthStatus.OK

    for report in reports:
        detail = HealthDetail(
            name=report.name,
            status=report.status,
            summary=report.summary,
            actions=tuple(report.actions),
            last_refresh_at=report.last_refresh_at,
        )
        details.append(detail)

        if _SEVERITY_ORDER[report.status] > _SEVERITY_ORDER[status]:
            status = report.status

    return HealthModuleSnapshot(
        checked_at=timestamp,
        status=status,
        details=tuple(details),
    )


def _deserialize_document(data: Mapping[str, object]) -> HealthDocument:
    return HealthDocument.model_validate(dict(data))


def load_health_document(path: Path) -> HealthDocument:
    """Load persisted health results, returning empty data when missing."""

    if not path.exists():
        return HealthDocument.model_validate({})

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:  # pragma: no cover - defensive
        raise HealthDocumentReadError(
            f"Failed to load health document from {path}: {exc}"
        ) from exc  # pragma: no cover - defensive

    if not isinstance(raw, MutableMapping):
        raise HealthDocumentReadError(
            f"Health document at {path} has unexpected structure."
        )

    return _deserialize_document(raw)


def dump_health_document(document: HealthDocument) -> str:
    """Serialize the health document to a formatted JSON string."""

    payload = document.model_dump(mode="json")
    ordered = OrderedDict(sorted(payload.items()))
    return (
        json.dumps(
            ordered,
            indent=2,
            sort_keys=False,
            ensure_ascii=False,
        )
        + "\n"
    )


@dataclass(slots=True)
class HealthDocumentStore:
    """Manage persisted health documents with atomic writes."""

    path: Path

    def load(self) -> HealthDocument:
        return load_health_document(self.path)

    def write(self, document: HealthDocument) -> None:
        serialized = dump_health_document(document)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".health-",
                suffix=".json.tmp",
                delete=False,
            ) as handle:
                handle.write(serialized)
                temp_path = Path(handle.name)

            os.replace(temp_path, self.path)
        except OSError as exc:  # pragma: no cover - error handling branch
            if "temp_path" in locals():
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise HealthDocumentWriteError(
                f"Failed to write health document to {self.path}: {exc}"
            ) from exc

    def update(
        self,
        updates: Mapping[str, HealthModuleSnapshot],
    ) -> HealthDocument:
        document = self.load()
        merged = document.merge(updates)
        self.write(merged)
        return merged


__all__ = [
    "HealthDetail",
    "HealthDocument",
    "HealthDocumentStore",
    "HealthModuleSnapshot",
    "build_module_snapshot",
    "dump_health_document",
    "load_health_document",
]
