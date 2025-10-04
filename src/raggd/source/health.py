"""Health evaluation helpers for managed sources."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from raggd.source.models import (
    SourceHealthSnapshot,
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)

# Severity ordering used when consolidating issues.
_SEVERITY_ORDER: dict[SourceHealthStatus, int] = {
    SourceHealthStatus.OK: 0,
    SourceHealthStatus.UNKNOWN: 1,
    SourceHealthStatus.DEGRADED: 2,
    SourceHealthStatus.ERROR: 3,
}


class SourceHealthIssue:
    """Intermediate representation of a detected health problem."""

    __slots__ = ("status", "summary", "actions")

    def __init__(
        self,
        status: SourceHealthStatus,
        summary: str,
        actions: Sequence[str] | None = None,
    ) -> None:
        self.status = status
        self.summary = summary
        self.actions = tuple(actions or ())


def evaluate_source_health(
    *,
    config: WorkspaceSourceConfig,
    manifest: SourceManifest,
    now: Callable[[], datetime] | None = None,
) -> SourceHealthSnapshot:
    """Evaluate the health status for a single source."""

    timestamp = (now or _default_now)()
    issues: list[SourceHealthIssue] = []

    issues.extend(_detect_disabled(config))
    issues.extend(_detect_directory_issues(config.path))

    if config.target is None:
        issues.append(
            SourceHealthIssue(
                SourceHealthStatus.DEGRADED,
                "No target is configured for this source.",
                (
                    "Set a target with `raggd source target "
                    f"{config.name} <path>`.",
                ),
            )
        )
    else:
        issues.extend(_detect_target_issues(config))
        issues.extend(_detect_refresh_staleness(config, manifest))

    if not issues:
        return SourceHealthSnapshot(
            status=SourceHealthStatus.OK,
            checked_at=timestamp,
            summary=None,
            actions=(),
        )

    status = SourceHealthStatus.OK
    summary: str | None = None
    actions: list[str] = []

    for issue in sorted(
        issues,
        key=lambda item: _SEVERITY_ORDER[item.status],
        reverse=True,
    ):
        if _SEVERITY_ORDER[issue.status] > _SEVERITY_ORDER[status]:
            status = issue.status
            summary = issue.summary

        for action in issue.actions:
            if action not in actions:
                actions.append(action)

    return SourceHealthSnapshot(
        status=status,
        checked_at=timestamp,
        summary=summary,
        actions=tuple(actions),
    )


def _detect_disabled(
    config: WorkspaceSourceConfig,
) -> Iterable[SourceHealthIssue]:
    if config.enabled:
        return ()

    return (
        SourceHealthIssue(
            SourceHealthStatus.UNKNOWN,
            "Source is disabled.",
            (
                "Enable the source with `raggd source enable "
                f"{config.name}` when it is ready.",
            ),
        ),
    )


def _detect_directory_issues(path: Path) -> Iterable[SourceHealthIssue]:
    try:
        exists = path.exists()
    except OSError:  # pragma: no cover - filesystem edge case
        exists = False

    if not exists:
        return (
            SourceHealthIssue(
                SourceHealthStatus.ERROR,
                f"Source directory is missing: {path}",
                (
                    "Recreate the source directory or re-run "
                    "`raggd source init`.",
                ),
            ),
        )

    if not path.is_dir():
        return (
            SourceHealthIssue(
                SourceHealthStatus.ERROR,
                f"Source path is not a directory: {path}",
                ("Update the workspace config to point at a directory path.",),
            ),
        )

    return ()


def _detect_target_issues(
    config: WorkspaceSourceConfig,
) -> Iterable[SourceHealthIssue]:
    target = config.target
    if target is None:  # pragma: no cover - guarded by evaluate_source_health
        return ()
    issues: list[SourceHealthIssue] = []

    try:
        exists = target.exists()
    except OSError:  # pragma: no cover - filesystem edge case
        exists = False

    if not exists:
        issues.append(
            SourceHealthIssue(
                SourceHealthStatus.ERROR,
                f"Target path does not exist: {target}",
                (
                    "Create the target directory or update the source target "
                    "path.",
                ),
            )
        )
        return issues

    if not target.is_dir():
        issues.append(
            SourceHealthIssue(
                SourceHealthStatus.DEGRADED,
                f"Target path is not a directory: {target}",
                (
                    "Point the source at a directory with `raggd source target "
                    f"{config.name} <dir>`.",
                ),
            )
        )

    if not os.access(target, os.R_OK):
        issues.append(
            SourceHealthIssue(
                SourceHealthStatus.ERROR,
                f"Target path is not readable: {target}",
                ("Adjust permissions or choose a readable directory.",),
            )
        )

    return issues


def _detect_refresh_staleness(
    config: WorkspaceSourceConfig,
    manifest: SourceManifest,
) -> Iterable[SourceHealthIssue]:
    if manifest.last_refresh_at is None:
        return (
            SourceHealthIssue(
                SourceHealthStatus.UNKNOWN,
                "Source has not been refreshed yet.",
                (
                    "Run `raggd source refresh "
                    f"{config.name}` to populate managed artifacts.",
                ),
            ),
        )

    return ()


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["evaluate_source_health", "SourceHealthIssue"]
