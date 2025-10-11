"""Health integration for the VDB module."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from raggd.core.logging import get_logger
from raggd.modules import HealthReport, HealthStatus, WorkspaceHandle
from raggd.modules.db import DbLifecycleService, db_settings_from_mapping
from raggd.modules.manifest import (
    ManifestService,
    manifest_settings_from_config,
)
from raggd.modules.vdb.providers import ProviderRegistry
from raggd.modules.vdb.service import VdbInfoError, VdbService

__all__ = ["vdb_health_hook"]

_SEVERITY_ORDER: dict[HealthStatus, int] = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.DEGRADED: 2,
    HealthStatus.ERROR: 3,
}

_LEVEL_TO_STATUS: dict[str, HealthStatus] = {
    "info": HealthStatus.OK,
    "ok": HealthStatus.OK,
    "warning": HealthStatus.DEGRADED,
    "warn": HealthStatus.DEGRADED,
    "error": HealthStatus.ERROR,
    "critical": HealthStatus.ERROR,
    "unknown": HealthStatus.UNKNOWN,
}


def _parse_timestamp(value: object) -> datetime | None:
    """Parse ISO-8601 timestamps while tolerating missing/blank values."""

    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _counts_summary(payload: Mapping[str, Any] | None) -> str | None:
    """Render a concise counts summary from the VDB info payload."""

    if not isinstance(payload, Mapping):
        return None

    def _coerce_int(key: str) -> int:
        try:
            return int(payload.get(key, 0))
        except (TypeError, ValueError):
            return 0

    chunks = _coerce_int("chunks")
    vectors = _coerce_int("vectors")
    index = _coerce_int("index")
    return f"chunks={chunks}, vectors={vectors}, index={index}"


def _entry_status(level: str) -> HealthStatus:
    return _LEVEL_TO_STATUS.get(level.strip().lower(), HealthStatus.UNKNOWN)


def _summaries_from_entries(
    entries: Sequence[Mapping[str, Any]],
) -> tuple[HealthStatus, str | None, tuple[str, ...]]:
    """Aggregate status, summary text, and actions from health entries."""

    status = HealthStatus.OK
    messages: list[str] = []
    actions: list[str] = []

    for entry in entries:
        level = str(entry.get("level", "unknown"))
        entry_status = _entry_status(level)
        if _SEVERITY_ORDER[entry_status] > _SEVERITY_ORDER[status]:
            status = entry_status

        message = str(entry.get("message", "")).strip()
        if message:
            messages.append(message)

        raw_actions = entry.get("actions", ())
        if isinstance(raw_actions, Sequence) and not isinstance(
            raw_actions,
            (str, bytes),
        ):
            for action in raw_actions:
                action_text = str(action).strip()
                if action_text:
                    actions.append(action_text)

    summary = "; ".join(dict.fromkeys(messages)) if messages else None
    unique_actions = tuple(dict.fromkeys(actions))
    return status, summary, unique_actions


def _build_report(payload: Mapping[str, Any]) -> HealthReport:
    """Translate a VDB info mapping into a :class:`HealthReport`."""

    name = str(
        payload.get("selector")
        or payload.get("name")
        or payload.get("id")
        or "vdb"
    )

    raw_entries = payload.get("health", ())
    entries: list[Mapping[str, Any]] = []
    if isinstance(raw_entries, Sequence):
        for entry in raw_entries:
            if isinstance(entry, Mapping):
                entries.append(entry)

    status, entry_summary, actions = _summaries_from_entries(entries)
    counts_summary = _counts_summary(
        payload.get("counts") if isinstance(payload, Mapping) else None
    )

    summary_parts: list[str] = []
    if counts_summary:
        summary_parts.append(counts_summary)
    if entry_summary:
        summary_parts.append(entry_summary)
    elif counts_summary:
        summary_parts.append("healthy")

    summary_text = " — ".join(summary_parts) if summary_parts else None
    last_refresh = (
        _parse_timestamp(payload.get("last_sync_at"))
        or _parse_timestamp(payload.get("built_at"))
    )

    return HealthReport(
        name=name,
        status=status,
        summary=summary_text,
        actions=actions,
        last_refresh_at=last_refresh,
    )


def _build_service(handle: WorkspaceHandle) -> VdbService:
    """Construct a ``VdbService`` instance for health evaluations."""

    logger = get_logger(__name__, component="vdb-health")
    payload = handle.config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(payload)
    db_settings = db_settings_from_mapping(payload)

    manifest_service = ManifestService(
        workspace=handle.paths,
        settings=manifest_settings,
        logger=logger.bind(component="manifest"),
    )
    db_service = DbLifecycleService(
        workspace=handle.paths,
        manifest_service=manifest_service,
        db_settings=db_settings,
        logger=logger.bind(component="db-service"),
    )

    return VdbService(
        workspace=handle.paths,
        config=handle.config,
        db_service=db_service,
        providers=ProviderRegistry(),
        logger=logger.bind(component="service"),
    )


def vdb_health_hook(handle: WorkspaceHandle) -> Sequence[HealthReport]:
    """Return health reports for each VDB present in the workspace."""

    try:
        service = _build_service(handle)
    except Exception as exc:  # pragma: no cover - defensive
        return (
            HealthReport(
                name="vdb",
                status=HealthStatus.ERROR,
                summary=f"Failed to initialize VDB health inspection: {exc}",
                actions=("Inspect workspace configuration and retry.",),
                last_refresh_at=None,
            ),
        )

    try:
        summaries = service.info(source=None, vdb=None)
    except VdbInfoError as exc:
        summary = f"Failed to collect VDB health: {exc}"
        actions = (
            "Verify workspace databases and rerun `raggd vdb info --json`.",
        )
        return (
            HealthReport(
                name="vdb",
                status=HealthStatus.ERROR,
                summary=summary,
                actions=actions,
                last_refresh_at=None,
            ),
        )

    reports: list[HealthReport] = []
    for payload in summaries:
        if isinstance(payload, Mapping):
            reports.append(_build_report(payload))

    return tuple(reports)
