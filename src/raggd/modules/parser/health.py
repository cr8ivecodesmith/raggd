"""Health integration for the parser module."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from pydantic import ValidationError

from raggd.core.config import PARSER_MODULE_KEY, ParserModuleSettings
from raggd.modules import HealthReport, HealthStatus, WorkspaceHandle
from raggd.modules.manifest import (
    ManifestError,
    ManifestService,
    manifest_settings_from_config,
)

from .models import ParserManifestState, ParserRunMetrics

__all__ = ["parser_health_hook"]


_SEVERITY_ORDER: Mapping[HealthStatus, int] = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.DEGRADED: 2,
    HealthStatus.ERROR: 3,
}


@dataclass(frozen=True, slots=True)
class _ChunkSlices:
    """Subset of chunk slice information used for integrity checks."""

    chunk_id: str
    part_index: int
    part_total: int
    metadata_json: str | None


def parser_health_hook(handle: WorkspaceHandle) -> Sequence[HealthReport]:
    """Return health reports for each configured parser source."""

    settings = handle.config.parser
    if isinstance(settings, ParserModuleSettings) and not settings.enabled:
        return (
            HealthReport(
                name="parser-module",
                status=HealthStatus.UNKNOWN,
                summary="Parser module disabled via configuration.",
                actions=(
                    "Set `modules.parser.enabled = true` in raggd.toml "
                    "to enable checks.",
                ),
                last_refresh_at=None,
            ),
        )

    config_payload = handle.config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(config_payload)
    manifest_service = ManifestService(
        workspace=handle.paths,
        settings=manifest_settings,
    )
    _, parser_module_key = manifest_settings.module_key(PARSER_MODULE_KEY)

    reports: list[HealthReport] = []
    for name, _ in sorted(handle.config.iter_workspace_sources()):
        report = _evaluate_source(
            name=name,
            handle=handle,
            manifest_service=manifest_service,
            parser_module_key=parser_module_key,
            settings=settings,
        )
        reports.append(report)

    return tuple(reports)


def _evaluate_source(
    *,
    name: str,
    handle: WorkspaceHandle,
    manifest_service: ManifestService,
    parser_module_key: str,
    settings: ParserModuleSettings,
) -> HealthReport:
    parser_actions = (
        f"Run `raggd parser parse {name}` to rebuild parser data.",
    )

    state, entry_missing, manifest_error = _load_manifest_state(
        name=name,
        manifest_service=manifest_service,
        parser_module_key=parser_module_key,
        parser_actions=parser_actions,
    )
    if manifest_error is not None:
        return manifest_error

    issues: list[str] = []
    actions: set[str] = set(state.last_run_notes or ())
    status = state.last_run_status
    summary = state.last_run_summary

    if entry_missing:
        issues.append("parser manifest entry missing")
        actions.add(parser_actions[0])
        status = _elevate_status(status, HealthStatus.UNKNOWN)

    batch_id = state.last_batch_id
    if not batch_id:
        issues.append("parser has not completed a batch yet")
        actions.add(parser_actions[0])
        summary = issues[-1]
        status = _elevate_status(status, HealthStatus.UNKNOWN)
        return HealthReport(
            name=name,
            status=status,
            summary=_summarize(issues, summary),
            actions=tuple(sorted(actions)),
            last_refresh_at=state.last_run_completed_at,
        )

    observed_rows, observed_error = _observe_batch(
        name=name,
        batch_id=batch_id,
        handle=handle,
        parser_actions=parser_actions,
        last_refresh_at=state.last_run_completed_at,
    )
    if observed_error is not None:
        return observed_error

    severity, chunk_issues, chunk_actions = _assess_chunk_integrity(
        observed_rows,
    )
    if severity is not None:
        status = _elevate_status(status, severity)
    issues.extend(chunk_issues)
    actions.update(chunk_actions)
    if chunk_issues:
        actions.add(parser_actions[0])

    metrics_result = _assess_concurrency_metrics(
        state.metrics,
        settings=settings,
    )
    metrics_severity, metrics_issues, metrics_actions = metrics_result
    if metrics_severity is not None:
        status = _elevate_status(status, metrics_severity)
    issues.extend(metrics_issues)
    actions.update(metrics_actions)

    summary = _summarize(issues, summary)
    return HealthReport(
        name=name,
        status=status,
        summary=summary,
        actions=tuple(sorted(actions)) if actions else (),
        last_refresh_at=state.last_run_completed_at,
    )


def _load_manifest_state(
    *,
    name: str,
    manifest_service: ManifestService,
    parser_module_key: str,
    parser_actions: tuple[str, ...],
) -> tuple[ParserManifestState, bool, HealthReport | None]:
    try:
        snapshot = manifest_service.load(name, apply_migrations=True)
    except ManifestError as exc:
        return (
            ParserManifestState(),
            False,
            HealthReport(
                name=name,
                status=HealthStatus.ERROR,
                summary=f"Failed to read manifest: {exc}",
                actions=parser_actions,
                last_refresh_at=None,
            ),
        )

    payload = snapshot.module(parser_module_key)
    entry_missing = payload is None
    try:
        state = ParserManifestState.from_mapping(payload)
    except ValidationError as exc:
        return (
            ParserManifestState(),
            entry_missing,
            HealthReport(
                name=name,
                status=HealthStatus.ERROR,
                summary=f"Parser manifest invalid: {exc}",
                actions=parser_actions,
                last_refresh_at=None,
            ),
        )

    return state, entry_missing, None


def _observe_batch(
    *,
    name: str,
    batch_id: str,
    handle: WorkspaceHandle,
    parser_actions: tuple[str, ...],
    last_refresh_at: datetime | None,
) -> tuple[tuple[_ChunkSlices, ...], HealthReport | None]:
    db_path = handle.paths.source_database_path(name)
    if not db_path.exists():
        return (
            (),
            HealthReport(
                name=name,
                status=HealthStatus.ERROR,
                summary=(
                    "Parser database missing while manifest references a batch."
                ),
                actions=parser_actions,
                last_refresh_at=last_refresh_at,
            ),
        )

    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        return (
            (),
            HealthReport(
                name=name,
                status=HealthStatus.ERROR,
                summary=(f"Failed to open parser database {db_path}: {exc}"),
                actions=parser_actions,
                last_refresh_at=last_refresh_at,
            ),
        )

    try:
        connection.row_factory = sqlite3.Row
        with connection:
            has_batch = connection.execute(
                "SELECT 1 FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if has_batch is None:
                return (
                    (),
                    HealthReport(
                        name=name,
                        status=HealthStatus.ERROR,
                        summary=(
                            "Manifest last_batch_id missing from batches table."
                        ),
                        actions=parser_actions,
                        last_refresh_at=last_refresh_at,
                    ),
                )

            rows = connection.execute(
                (
                    "SELECT chunk_id, part_index, part_total, metadata_json "
                    "FROM chunk_slices WHERE batch_id = ?"
                ),
                (batch_id,),
            ).fetchall()
    finally:
        connection.close()

    if not rows:
        return (
            (),
            HealthReport(
                name=name,
                status=HealthStatus.ERROR,
                summary=(
                    "Chunk slices missing for manifest last_batch_id; rerun "
                    "the parser to repopulate data."
                ),
                actions=parser_actions,
                last_refresh_at=last_refresh_at,
            ),
        )

    slices = tuple(
        _ChunkSlices(
            chunk_id=str(row["chunk_id"]),
            part_index=int(row["part_index"]),
            part_total=int(row["part_total"]),
            metadata_json=row["metadata_json"],
        )
        for row in rows
    )
    return slices, None


def _assess_chunk_integrity(
    rows: tuple[_ChunkSlices, ...],
) -> tuple[HealthStatus | None, list[str], set[str]]:
    severity: HealthStatus | None = None
    issues: list[str] = []
    actions: set[str] = set()

    chunk_indices: dict[str, list[int]] = {}
    chunk_totals: dict[str, int] = {}
    parent_links: dict[str, str] = {}

    for row in rows:
        chunk_indices.setdefault(row.chunk_id, []).append(row.part_index)
        recorded_total = chunk_totals.setdefault(row.chunk_id, row.part_total)
        if recorded_total != row.part_total:
            severity = _max_severity(severity, HealthStatus.ERROR)
            issues.append(
                f"chunk {row.chunk_id!r} has inconsistent part totals"
            )

        if row.metadata_json:
            try:
                metadata = json.loads(row.metadata_json)
            except json.JSONDecodeError:
                severity = _max_severity(severity, HealthStatus.ERROR)
                issues.append(f"chunk {row.chunk_id!r} metadata not valid JSON")
                continue

            parent = metadata.get("delegate_parent_chunk")
            if isinstance(parent, str) and parent:
                parent_links[row.chunk_id] = parent

    for chunk_id, indices in chunk_indices.items():
        part_total = chunk_totals.get(chunk_id, 0)
        sorted_indices = sorted(indices)
        expected = list(range(part_total))
        if sorted_indices != expected:
            severity = _max_severity(severity, HealthStatus.ERROR)
            issues.append(f"chunk {chunk_id!r} part indices not contiguous")

    chunk_ids = set(chunk_indices)
    for chunk_id, parent in parent_links.items():
        if parent not in chunk_ids:
            severity = _max_severity(severity, HealthStatus.ERROR)
            issues.append(
                f"chunk {chunk_id!r} references missing parent {parent!r}"
            )

    return severity, issues, actions


def _assess_concurrency_metrics(
    metrics: ParserRunMetrics,
    *,
    settings: ParserModuleSettings,
) -> tuple[HealthStatus | None, list[str], set[str]]:
    severity: HealthStatus | None = None
    issues: list[str] = []
    actions: set[str] = set()

    wait_seconds = float(metrics.lock_wait_seconds or 0.0)
    error_wait = settings.lock_wait_error_seconds
    warn_wait = settings.lock_wait_warning_seconds
    error_contention = settings.lock_contention_error
    warn_contention = settings.lock_contention_warning

    if wait_seconds >= error_wait:
        severity = _max_severity(severity, HealthStatus.ERROR)
        issues.append(
            (
                "parser lock waits accumulated "
                f"{wait_seconds:.2f}s (error threshold {error_wait:.2f}s)"
            )
        )
    elif wait_seconds >= warn_wait:
        severity = _max_severity(severity, HealthStatus.DEGRADED)
        issues.append(
            (
                "parser lock waits accumulated "
                f"{wait_seconds:.2f}s (warning threshold {warn_wait:.2f}s)"
            )
        )

    contention_events = int(metrics.lock_contention_events or 0)
    if contention_events >= error_contention:
        severity = _max_severity(severity, HealthStatus.ERROR)
        issues.append(
            "parser recorded "
            f"{contention_events} lock contention events "
            f"(error threshold {error_contention})"
        )
    elif contention_events >= warn_contention:
        severity = _max_severity(severity, HealthStatus.DEGRADED)
        issues.append(
            "parser recorded "
            f"{contention_events} lock contention events "
            f"(warning threshold {warn_contention})"
        )

    if severity is not None:
        actions.add(
            (
                "Inspect parser concurrency telemetry (see "
                "docs/contribute/parser-runbook.md#alerts) and adjust "
                "modules.parser.max_concurrency or stagger runs."
            )
        )

    return severity, issues, actions


def _summarize(issues: list[str], manifest_summary: str | None) -> str | None:
    if issues:
        return ", ".join(dict.fromkeys(issues))
    return manifest_summary or "parser healthy"


def _elevate_status(
    current: HealthStatus,
    candidate: HealthStatus,
) -> HealthStatus:
    if _SEVERITY_ORDER[candidate] > _SEVERITY_ORDER[current]:
        return candidate
    return current


def _max_severity(
    current: HealthStatus | None,
    candidate: HealthStatus,
) -> HealthStatus:
    if current is None:
        return candidate
    if _SEVERITY_ORDER[candidate] > _SEVERITY_ORDER[current]:
        return candidate
    return current
