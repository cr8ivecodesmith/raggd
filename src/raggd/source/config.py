"""Workspace source configuration store utilities."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import tomllib
import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import String, Table

from raggd.core.config import AppConfig, load_config, load_packaged_defaults
from raggd.modules.manifest import (
    ManifestSettings,
    manifest_settings_from_config,
)
from raggd.source.models import WorkspaceSourceConfig


class SourceConfigError(RuntimeError):
    """Base error for workspace source configuration failures."""


class SourceConfigWriteError(SourceConfigError):
    """Raised when persisting configuration updates fails."""

    def __init__(self, path: Path, stage: str, cause: OSError) -> None:
        message = (
            f"Failed to {stage} workspace config at {path}: {cause}"
        ).strip()
        super().__init__(message)
        self.path = path
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True, slots=True)
class SourceConfigSnapshot:
    """Pair an :class:`AppConfig` with its TOML document representation."""

    config: AppConfig
    document: TOMLDocument
    manifest_settings: ManifestSettings


class SourceConfigStore:
    """Manage workspace source entries within ``raggd.toml`` safely."""

    def __init__(self, *, config_path: Path) -> None:
        self._config_path = config_path

    def load(self) -> AppConfig:
        """Return the current application configuration."""

        snapshot = self._load_snapshot()
        return snapshot.config

    def manifest_settings(self) -> ManifestSettings:
        """Return manifest settings derived from the configuration stack."""

        snapshot = self._load_snapshot()
        return snapshot.manifest_settings

    def get(self, name: str) -> WorkspaceSourceConfig | None:
        """Return the configuration for a named source if it exists."""

        config = self.load()
        return config.workspace_sources.get(name)

    def upsert(self, source: WorkspaceSourceConfig) -> AppConfig:
        """Create or update a source entry and persist the configuration."""

        snapshot = self._load_snapshot()
        workspace_table = self._ensure_workspace_table(
            snapshot.document,
            snapshot.config,
        )

        sources = dict(snapshot.config.workspace_sources)
        sources[source.name] = source
        self._write_sources_table(workspace_table, sources)
        self._write_document(snapshot.document)
        return self.load()

    def remove(self, name: str) -> AppConfig:
        """Remove a source entry if present and persist the configuration."""

        snapshot = self._load_snapshot()
        workspace_table = self._ensure_workspace_table(
            snapshot.document,
            snapshot.config,
        )

        sources = dict(snapshot.config.workspace_sources)
        sources.pop(name, None)
        self._write_sources_table(workspace_table, sources)
        self._write_document(snapshot.document)
        return self.load()

    def replace_all(
        self,
        sources: Mapping[str, WorkspaceSourceConfig],
    ) -> AppConfig:
        """Persist the provided mapping as the authoritative source set."""

        snapshot = self._load_snapshot()
        workspace_table = self._ensure_workspace_table(
            snapshot.document,
            snapshot.config,
        )

        normalized: dict[str, WorkspaceSourceConfig] = {}
        for key, value in sources.items():
            normalized[key] = (
                value
                if value.name == key
                else value.model_copy(update={"name": key})
            )
        self._write_sources_table(workspace_table, normalized)
        self._write_document(snapshot.document)
        return self.load()

    def _load_snapshot(self) -> SourceConfigSnapshot:
        defaults = load_packaged_defaults()
        user_text = self._read_config_text()
        user_data = tomllib.loads(user_text) if user_text else None
        document = tomlkit.loads(user_text) if user_text else tomlkit.document()
        config = load_config(defaults=defaults, user_config=user_data)
        config_payload = config.model_dump(mode="python")
        manifest_settings = manifest_settings_from_config(config_payload)
        return SourceConfigSnapshot(
            config=config,
            document=document,
            manifest_settings=manifest_settings,
        )

    def _read_config_text(self) -> str | None:
        path = self._config_path
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - surfaced via runtime errors
            raise SourceConfigError(
                f"Failed to read workspace config at {path}: {exc}"
            ) from exc  # pragma: no cover - surfaced via runtime errors

    def _ensure_workspace_table(
        self,
        document: tomlkit.TOMLDocument,
        config: AppConfig,
    ) -> Table:
        workspace_value = document.get("workspace")

        if isinstance(workspace_value, Table):
            table = workspace_value
        else:
            table = tomlkit.table()
            root_value = self._extract_legacy_root(workspace_value, config)
            table["root"] = root_value
            if "workspace" in document:
                document["workspace"] = table
            else:
                document.add("workspace", table)

        root_item = table.get("root")
        table["root"] = (
            str(root_item) if root_item is not None else str(config.workspace)
        )
        return table

    def _extract_legacy_root(
        self,
        workspace_value: object,
        config: AppConfig,
    ) -> str:
        if isinstance(workspace_value, String):
            return str(workspace_value)
        if workspace_value is None:
            return str(config.workspace)
        return str(workspace_value)

    def _write_sources_table(
        self,
        workspace_table: Table,
        sources: Mapping[str, WorkspaceSourceConfig],
    ) -> None:
        if "sources" in workspace_table:
            del workspace_table["sources"]

        if not sources:
            return

        sources_table = tomlkit.table(is_super_table=True)
        for name in sorted(sources):
            cfg = sources[name]
            entry = tomlkit.table()
            entry["enabled"] = cfg.enabled
            entry["path"] = str(cfg.path)
            if cfg.target is not None:
                entry["target"] = str(cfg.target)
            sources_table.add(name, entry)

        workspace_table.add("sources", sources_table)

    def _write_document(self, document: tomlkit.TOMLDocument) -> None:
        text = tomlkit.dumps(document)
        path = self._config_path
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
        except OSError as exc:  # pragma: no cover
            raise SourceConfigWriteError(
                path,
                "write temporary file",
                exc,
            ) from exc  # pragma: no cover - I/O failure simulated via tests

        try:
            os.replace(temp_path, path)
        except OSError as exc:  # pragma: no cover
            try:
                temp_path.unlink()
            except OSError:  # pragma: no cover - cleanup best effort
                pass
            raise SourceConfigWriteError(
                path,
                "replace target file",
                exc,
            ) from exc  # pragma: no cover - I/O failure simulated via tests


__all__ = [
    "SourceConfigError",
    "SourceConfigSnapshot",
    "SourceConfigStore",
    "SourceConfigWriteError",
]
