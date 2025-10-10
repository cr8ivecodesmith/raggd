"""Module health hooks for source management."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from raggd.modules import HealthReport, HealthStatus, WorkspaceHandle
from raggd.modules.manifest.helpers import manifest_settings_from_config
from raggd.modules.manifest.migrator import SOURCE_MODULE_KEY
from raggd.modules.manifest.service import (
    ManifestError,
    ManifestReadError,
    ManifestService,
)
from raggd.source.models import (
    SourceHealthStatus,
    SourceManifest,
    WorkspaceSourceConfig,
)


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

    payload = handle.config.model_dump(mode="python")
    manifest_settings = manifest_settings_from_config(payload)
    source_module_key = _read_source_module_key(payload)
    manifest_service = ManifestService(
        workspace=handle.paths,
        settings=manifest_settings,
    )

    reports: list[HealthReport] = []

    for name, source_config in sorted(handle.config.iter_workspace_sources()):
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
            manifest = _load_manifest(
                service=manifest_service,
                source=name,
                module_key=source_module_key,
                manifest_path=manifest_path,
                config=source_config,
            )
        except ManifestReadError as exc:
            reports.append(
                HealthReport(
                    name=name,
                    status=HealthStatus.ERROR,
                    summary=str(exc),
                    actions=(
                        "Verify permissions and rerun "
                        f"`raggd source refresh {name}`.",
                    ),
                    last_refresh_at=None,
                )
            )
            continue
        except (ManifestError, ValidationError) as exc:
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


def _load_manifest(
    *,
    service: ManifestService,
    source: str,
    module_key: str,
    manifest_path: Path,
    config: WorkspaceSourceConfig,
) -> SourceManifest:
    snapshot = service.load(source)
    module_payload = snapshot.module(module_key)

    if isinstance(module_payload, Mapping):
        payload = dict(module_payload)
    else:
        payload = _extract_legacy_fields(snapshot.data)
        if not payload:
            raise ManifestError(
                f"Manifest {manifest_path} is missing source payload "
                "under modules namespace."
            )

    defaults = {
        "name": config.name,
        "path": config.path,
        "enabled": config.enabled,
        "target": config.target,
        "last_refresh_at": None,
    }

    if "last_refresh_at" not in payload and "last_refresh_at" in snapshot.data:
        defaults["last_refresh_at"] = snapshot.data.get("last_refresh_at")

    merged = {**defaults, **payload}
    return SourceManifest.model_validate(merged)


def _read_source_module_key(payload: Mapping[str, object] | None) -> str:
    db_settings: Mapping[str, object] | None = None
    if isinstance(payload, Mapping):
        candidate = payload.get("db")
        if isinstance(candidate, Mapping):
            db_settings = candidate

    if db_settings is not None:
        key = db_settings.get("manifest_source_module_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return SOURCE_MODULE_KEY


def _extract_legacy_fields(data: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "name",
        "path",
        "enabled",
        "target",
        "last_refresh_at",
        "last_health",
    )
    extracted = {key: data[key] for key in keys if key in data}
    return extracted
