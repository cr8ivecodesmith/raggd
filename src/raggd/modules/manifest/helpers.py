"""Helper utilities for modules consuming the manifest service."""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from .config import ManifestSettings, manifest_settings_from_mapping

__all__ = [
    "manifest_db_namespace",
    "manifest_settings_from_config",
]


def manifest_settings_from_config(
    config: Mapping[str, Any] | None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> ManifestSettings:
    """Return :class:`ManifestSettings` derived from ``config``.

    The helper mirrors :func:`manifest_settings_from_mapping` while clarifying
    its intent for feature modules that operate on the loaded application
    configuration payload (typically produced by ``raggd.config.load()``).
    ``overrides`` allow tests to force specific values without mutating the
    source mapping.
    """

    return manifest_settings_from_mapping(config, overrides=overrides)


def manifest_db_namespace(
    settings: ManifestSettings | None = None,
) -> Tuple[str, str]:
    """Return the ``(modules_key, db_module_key)`` tuple for manifests.

    Consumers can call this helper to locate the database module payload within
    a manifest document without hard-coding the default ``("modules", "db")``
    pair. Passing ``None`` falls back to :class:`ManifestSettings` defaults.
    """

    effective = settings or ManifestSettings()
    return effective.modules_key, effective.db_module_key

