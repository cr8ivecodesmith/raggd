"""Module health hooks for source management."""

from __future__ import annotations

import json
from typing import Sequence

from pydantic import ValidationError

from raggd.modules import HealthReport, HealthStatus, WorkspaceHandle
from raggd.source.models import SourceHealthStatus, SourceManifest


def _convert_status(status: SourceHealthStatus | str | None) -> HealthStatus:
    """Normalize source health statuses to module-level health statuses."""

    if status is None:
        return HealthStatus.UNKNOWN
    if isinstance(status, SourceHealthStatus):
        return HealthStatus(status.value)
    try:
        return HealthStatus(str(status))
    except ValueError:
        return HealthStatus.UNKNOWN


def source_health_hook(handle: WorkspaceHandle) -> Sequence[HealthReport]:
    """Return health reports for all configured sources in the workspace.

    The hook is read-only: it inspects manifests, mirrors their recorded
    ``last_health`` payload, and does not modify configuration or manifests.
    Missing or malformed manifests emit ``error``/``unknown`` reports with
    guidance on remediation so operators can recover via the CLI flows.
    """

    reports: list[HealthReport] = []

    for name, _ in sorted(handle.config.iter_workspace_sources()):
        manifest_path = handle.paths.source_manifest_path(name)

        if not manifest_path.exists():
            reports.append(
                HealthReport(
                    name=name,
                    status=HealthStatus.UNKNOWN,
                    summary=f"Manifest missing for source: {manifest_path}",
                    actions=(
                        "Run `raggd source refresh "
                        f"{name}` to recreate the manifest.",
                    ),
                    last_refresh_at=None,
                )
            )
            continue

        try:
            raw = manifest_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            manifest = SourceManifest.model_validate(payload)
        except OSError as exc:
            reports.append(
                HealthReport(
                    name=name,
                    status=HealthStatus.ERROR,
                    summary=f"Failed to read manifest {manifest_path}: {exc}",
                    actions=(
                        "Verify permissions and rerun "
                        f"`raggd source refresh {name}`.",
                    ),
                    last_refresh_at=None,
                )
            )
            continue
        except (json.JSONDecodeError, ValidationError) as exc:
            reports.append(
                HealthReport(
                    name=name,
                    status=HealthStatus.ERROR,
                    summary=f"Manifest at {manifest_path} is invalid: {exc}",
                    actions=(
                        "Inspect the manifest file or recreate it via "
                        f"`raggd source refresh {name}`.",
                    ),
                    last_refresh_at=None,
                )
            )
            continue

        snapshot = manifest.last_health
        reports.append(
            HealthReport(
                name=manifest.name,
                status=_convert_status(snapshot.status),
                summary=snapshot.summary,
                actions=tuple(snapshot.actions),
                last_refresh_at=manifest.last_refresh_at,
            )
        )

    return tuple(reports)


__all__ = ["source_health_hook"]
