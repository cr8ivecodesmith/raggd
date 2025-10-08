"""Data models supporting parser service orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

from raggd.modules import HealthReport, HealthStatus

__all__ = [
    "ParserRunMetrics",
    "ParserRunRecord",
    "ParserManifestState",
]


def _normalize_tuple(values: Any) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, tuple):
        return tuple(str(item).strip() for item in values if item is not None)
    if isinstance(values, list):
        return tuple(str(item).strip() for item in values if item is not None)
    return (str(values).strip(),)


class ParserRunMetrics(BaseModel):
    """Aggregated counters describing the outcome of a parser run."""

    files_discovered: int = Field(default=0, ge=0)
    files_parsed: int = Field(default=0, ge=0)
    files_reused: int = Field(default=0, ge=0)
    files_failed: int = Field(default=0, ge=0)
    chunks_emitted: int = Field(default=0, ge=0)
    chunks_reused: int = Field(default=0, ge=0)
    fallbacks: int = Field(default=0, ge=0)
    queue_depth: int = Field(default=0, ge=0)
    handlers_invoked: dict[str, int] = Field(default_factory=dict)
    handler_runtime_seconds: dict[str, float] = Field(default_factory=dict)
    lock_wait_seconds: float = Field(default=0.0, ge=0.0)
    lock_contention_events: int = Field(default=0, ge=0)

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "use_enum_values": True,
    }

    @field_validator("handlers_invoked")
    @classmethod
    def _validate_handlers(
        cls,
        value: Mapping[str, int],
    ) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for name, count in (value or {}).items():
            if count < 0:
                raise ValueError(
                    "Handler invocation counts must be >= 0 (got "
                    f"{count} for {name!r})."
                )
            normalized[str(name).strip()] = int(count)
        return normalized

    @field_validator("handler_runtime_seconds")
    @classmethod
    def _validate_handler_runtime(
        cls,
        value: Mapping[str, float],
    ) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for name, seconds in (value or {}).items():
            if seconds < 0:
                raise ValueError(
                    "Handler runtime seconds must be >= 0 (got "
                    f"{seconds} for {name!r})."
                )
            normalized[str(name).strip()] = float(seconds)
        return normalized

    def increment_handler(self, name: str, *, count: int = 1) -> None:
        """Increment the invocation counter for ``name`` by ``count``."""

        if count < 0:
            raise ValueError("count must be >= 0")
        key = name.strip()
        current = self.handlers_invoked.get(key, 0)
        self.handlers_invoked[key] = current + count

    def record_handler_runtime(
        self,
        name: str,
        seconds: float,
    ) -> None:
        """Accumulate runtime seconds for ``name``."""

        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        key = name.strip()
        current = self.handler_runtime_seconds.get(key, 0.0)
        self.handler_runtime_seconds[key] = current + float(seconds)

    def record_fallback(self) -> None:
        """Increment the fallback counter."""

        self.fallbacks += 1

    def record_lock_wait(
        self,
        seconds: float,
        *,
        threshold: float = 1e-6,
    ) -> None:
        """Accumulate database lock wait metrics."""

        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self.lock_wait_seconds += seconds
        if seconds > threshold:
            self.lock_contention_events += 1

    def copy(self) -> "ParserRunMetrics":
        """Return a shallow copy of the metrics snapshot."""

        data = self.model_dump()
        return ParserRunMetrics(**data)


class ParserRunRecord(BaseModel):
    """Structured summary persisted after a parser run completes."""

    batch_id: str | None = Field(default=None)
    started_at: datetime
    completed_at: datetime | None = Field(default=None)
    status: HealthStatus = Field(default=HealthStatus.UNKNOWN)
    summary: str | None = Field(default=None)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    errors: tuple[str, ...] = Field(default_factory=tuple)
    notes: tuple[str, ...] = Field(default_factory=tuple)
    handler_versions: dict[str, str] = Field(default_factory=dict)
    metrics: ParserRunMetrics = Field(default_factory=ParserRunMetrics)

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "use_enum_values": True,
    }

    @model_validator(mode="after")
    def _normalize(self) -> "ParserRunRecord":
        object.__setattr__(self, "warnings", _normalize_tuple(self.warnings))
        object.__setattr__(self, "errors", _normalize_tuple(self.errors))
        object.__setattr__(self, "notes", _normalize_tuple(self.notes))
        normalized_versions: dict[str, str] = {}
        for name, version in self.handler_versions.items():
            normalized_versions[str(name).strip()] = str(version).strip()
        object.__setattr__(self, "handler_versions", normalized_versions)
        return self

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def copy(self) -> "ParserRunRecord":
        data = self.model_dump()
        return ParserRunRecord(**data)


class ParserManifestState(BaseModel):
    """Manifest payload persisted under ``modules.parser`` for a source."""

    enabled: bool = Field(default=True)
    last_batch_id: str | None = Field(default=None)
    last_run_started_at: datetime | None = Field(default=None)
    last_run_completed_at: datetime | None = Field(default=None)
    last_run_status: HealthStatus = Field(default=HealthStatus.UNKNOWN)
    last_run_summary: str | None = Field(default=None)
    last_run_warnings: tuple[str, ...] = Field(default_factory=tuple)
    last_run_errors: tuple[str, ...] = Field(default_factory=tuple)
    last_run_notes: tuple[str, ...] = Field(default_factory=tuple)
    handler_versions: dict[str, str] = Field(default_factory=dict)
    metrics: ParserRunMetrics = Field(default_factory=ParserRunMetrics)

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "use_enum_values": True,
        "extra": "allow",
    }

    @model_validator(mode="after")
    def _normalize(self) -> "ParserManifestState":
        object.__setattr__(
            self,
            "last_run_warnings",
            _normalize_tuple(self.last_run_warnings),
        )
        object.__setattr__(
            self,
            "last_run_errors",
            _normalize_tuple(self.last_run_errors),
        )
        object.__setattr__(
            self,
            "last_run_notes",
            _normalize_tuple(self.last_run_notes),
        )
        normalized_versions: dict[str, str] = {}
        for name, version in self.handler_versions.items():
            normalized_versions[str(name).strip()] = str(version).strip()
        object.__setattr__(self, "handler_versions", normalized_versions)
        return self

    @property
    def warning_count(self) -> int:
        return len(self.last_run_warnings)

    @property
    def error_count(self) -> int:
        return len(self.last_run_errors)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any] | None,
    ) -> "ParserManifestState":
        """Instantiate state from a manifest payload mapping."""

        if payload is None:
            return cls()
        return cls.model_validate(payload)

    def apply_run(
        self,
        run: ParserRunRecord,
        *,
        enabled: bool | None = None,
    ) -> "ParserManifestState":
        """Return a copy updated with the latest parser run."""

        update = {
            "last_batch_id": run.batch_id,
            "last_run_started_at": run.started_at,
            "last_run_completed_at": run.completed_at,
            "last_run_status": run.status,
            "last_run_summary": run.summary,
            "last_run_warnings": run.warnings,
            "last_run_errors": run.errors,
            "last_run_notes": run.notes,
            "handler_versions": dict(run.handler_versions),
            "metrics": run.metrics.copy(),
        }
        if enabled is not None:
            update["enabled"] = enabled
        data = self.model_copy(update=update)
        return data

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping representation."""

        return self.model_dump(mode="json")

    def to_health_report(
        self,
        *,
        module: str,
    ) -> HealthReport:
        """Translate manifest state into a :class:`HealthReport`."""

        summary = self.last_run_summary
        actions = self.last_run_notes or ()
        return HealthReport(
            name=module,
            status=self.last_run_status,
            summary=summary,
            actions=actions,
            last_refresh_at=self.last_run_completed_at,
        )
