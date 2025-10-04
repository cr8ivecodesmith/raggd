"""Data models for source management features."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SourceHealthStatus(StrEnum):
    """Enumerate known health states for managed sources."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


class SourceHealthSnapshot(BaseModel):
    """Capture the most recent health evaluation for a source."""

    status: SourceHealthStatus = Field(
        default=SourceHealthStatus.UNKNOWN,
        description="Overall health classification for the source.",
    )
    checked_at: datetime | None = Field(
        default=None,
        description="Timestamp of the most recent health evaluation.",
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Short explanation of the health status or issues detected."
        ),
    )
    actions: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Remediation or follow-up actions recommended for operators."
        ),
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "use_enum_values": True,
    }

    @model_validator(mode="after")
    def _normalize(self) -> "SourceHealthSnapshot":
        """Normalize tuple fields after validation for deterministic output."""

        if self.actions:
            normalized = tuple(str(action).strip() for action in self.actions)
            object.__setattr__(self, "actions", normalized)
        return self


class WorkspaceSourceConfig(BaseModel):
    """Workspace configuration entry describing a managed source."""

    name: str = Field(
        description="Normalized name (slug) for the source entry.",
    )
    path: Path = Field(
        description=(
            "Absolute path to the source directory inside the workspace."
        ),
    )
    enabled: bool = Field(
        default=False,
        description="Whether the source is currently enabled for operations.",
    )
    target: Path | None = Field(
        default=None,
        description=(
            "Optional absolute path representing the upstream target for "
            "refreshes."
        ),
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }

    @model_validator(mode="after")
    def _normalize(self) -> "WorkspaceSourceConfig":
        """Normalize path fields after validation."""

        object.__setattr__(self, "path", self.path.expanduser())
        if self.target is not None:
            object.__setattr__(self, "target", self.target.expanduser())
        object.__setattr__(self, "name", self.name.strip())
        return self


class SourceManifest(BaseModel):
    """Operational manifest persisted alongside the source directory."""

    name: str = Field(
        description="Normalized name (slug) for the source entry.",
    )
    path: Path = Field(
        description=(
            "Absolute path to the source directory inside the workspace."
        ),
    )
    enabled: bool = Field(
        description="Whether the source is currently enabled for operations.",
    )
    target: Path | None = Field(
        default=None,
        description=(
            "Optional absolute path representing the upstream target for "
            "refreshes."
        ),
    )
    last_refresh_at: datetime | None = Field(
        default=None,
        description="Timestamp of the most recent successful refresh.",
    )
    last_health: SourceHealthSnapshot = Field(
        default_factory=SourceHealthSnapshot,
        description="Latest health snapshot recorded for the source.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "use_enum_values": True,
    }

    @model_validator(mode="after")
    def _normalize(self) -> "SourceManifest":
        """Normalize string/path fields after validation."""

        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "path", self.path.expanduser())
        if self.target is not None:
            object.__setattr__(self, "target", self.target.expanduser())
        return self


def workspace_source_config_schema() -> dict[str, Any]:
    """Return the JSON schema for :class:`WorkspaceSourceConfig`."""

    return WorkspaceSourceConfig.model_json_schema()


def source_manifest_schema() -> dict[str, Any]:
    """Return the JSON schema for :class:`SourceManifest`."""

    return SourceManifest.model_json_schema()


__all__ = [
    "SourceHealthSnapshot",
    "SourceHealthStatus",
    "SourceManifest",
    "workspace_source_config_schema",
    "source_manifest_schema",
    "WorkspaceSourceConfig",
]
