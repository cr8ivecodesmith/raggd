"""Configuration models and loaders for :mod:`raggd`."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from pathlib import Path
from enum import StrEnum
from typing import Any, Iterable, Mapping, Literal

import tomllib
import tomlkit
from pydantic import BaseModel, Field, field_validator, model_validator

from raggd.resources import get_resource
from raggd.source.models import WorkspaceSourceConfig


class DbSettings(BaseModel):
    """Database module configuration values."""

    manifest_modules_key: str = Field(
        default="modules",
        description="Key that stores module payloads within manifests.",
    )
    manifest_db_module_key: str = Field(
        default="db",
        description=(
            "Key used for the database module payload within manifests."
        ),
    )
    manifest_backup_retention: int = Field(
        default=5,
        ge=0,
        description="Number of manifest backups retained during rotations.",
    )
    manifest_lock_timeout: float = Field(
        default=5.0,
        ge=0.0,
        description="Seconds to wait when acquiring the manifest lock.",
    )
    manifest_lock_poll_interval: float = Field(
        default=0.1,
        gt=0.0,
        description="Polling interval in seconds while waiting on the lock.",
    )
    manifest_lock_suffix: str = Field(
        default=".lock",
        description="Suffix appended to manifest lock files.",
    )
    manifest_backup_suffix: str = Field(
        default=".bak",
        description="Suffix appended to manifest backup files.",
    )
    manifest_strict: bool = Field(
        default=True,
        description="Whether manifest write failures abort the operation.",
    )
    manifest_backups_enabled: bool = Field(
        default=True,
        description="Whether manifest backups are created during writes.",
    )
    migrations_path: str = Field(
        default="resources/db/migrations",
        description="Path containing packaged SQL migration files.",
    )
    ensure_auto_upgrade: bool = Field(
        default=True,
        description="Whether ensure applies pending migrations automatically.",
    )
    vacuum_max_stale_days: int = Field(
        default=7,
        ge=0,
        description="Maximum days since last vacuum before health warns.",
    )
    vacuum_concurrency: int | str = Field(
        default="auto",
        description="Worker count for vacuum operations (integer or 'auto').",
    )
    run_allow_outside: bool = Field(
        default=True,
        description=(
            "Whether db run allows executing scripts outside the workspace."
        ),
    )
    run_autocommit_default: bool = Field(
        default=False,
        description="Default autocommit behavior for db run.",
    )
    drift_warning_seconds: int = Field(
        default=0,
        ge=0,
        description="Threshold before reporting manifest/database drift.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }


class WorkspaceSettings(BaseModel):
    """Workspace-level configuration values and managed sources."""

    root: Path = Field(
        default_factory=lambda: Path("~/.raggd").expanduser(),
        description="Absolute path to the workspace root.",
    )
    sources: dict[str, WorkspaceSourceConfig] = Field(
        default_factory=dict,
        description="Registered workspace sources keyed by normalized name.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "populate_by_name": True,
    }

    @model_validator(mode="before")
    @classmethod
    def _coerce_raw(
        cls,
        value: Any,
    ) -> "WorkspaceSettings" | Mapping[str, Any] | Any:
        """Support legacy scalar workspace values when loading configs."""

        if value is None or isinstance(value, (WorkspaceSettings, MappingABC)):
            return value
        if isinstance(value, (str, Path)):
            return {"root": value}
        msg = f"Unsupported workspace configuration payload: {value!r}"
        raise TypeError(msg)

    @model_validator(mode="after")
    def _normalize(self) -> "WorkspaceSettings":
        """Normalize root path and ensure source keys mirror model names."""

        object.__setattr__(self, "root", self.root.expanduser())

        normalized_sources: dict[str, WorkspaceSourceConfig] = {}
        for name, source in self.sources.items():
            if source.name != name:
                source = source.model_copy(update={"name": name})
            normalized_sources[name] = source
        object.__setattr__(self, "sources", normalized_sources)
        return self

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
    ) -> "WorkspaceSettings":
        """Instantiate settings from a raw mapping payload."""

        if raw is None:
            return cls()

        data = dict(raw)
        sources_raw = data.get("sources", {})
        normalized_sources: dict[str, WorkspaceSourceConfig] = {}
        if isinstance(sources_raw, MappingABC):
            for key, value in sources_raw.items():
                if isinstance(value, WorkspaceSourceConfig):
                    source_model = value
                elif isinstance(value, MappingABC):
                    payload = dict(value)
                    payload.setdefault("name", key)
                    source_model = WorkspaceSourceConfig(**payload)
                else:
                    raise TypeError(
                        "Unsupported source configuration for "
                        f"{key!r}: {value!r}"
                    )
                normalized_sources[key] = source_model
        elif sources_raw:
            raise TypeError(f"Unsupported sources payload: {sources_raw!r}")

        result_data: dict[str, Any] = {"sources": normalized_sources}
        if "root" in data:
            result_data["root"] = data["root"]
        return cls(**result_data)

    def iter_sources(self) -> Iterable[tuple[str, WorkspaceSourceConfig]]:
        """Iterate over registered sources."""

        return self.sources.items()


class ModuleToggle(BaseModel):
    """Toggle controlling whether an optional module is enabled."""

    enabled: bool = Field(
        default=True,
        description="Whether the module is currently active.",
    )
    extras: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Optional dependency extras guarding module availability.",
    )

    model_config = {"frozen": True, "str_strip_whitespace": True}

    @model_validator(mode="after")
    def _normalize(self) -> "ModuleToggle":
        """Normalize extras ordering for deterministic serialization."""

        if self.extras:
            # Maintain tuple type but ensure stable ordering during rendering.
            normalized = tuple(dict.fromkeys(self.extras))
            object.__setattr__(self, "extras", normalized)
        return self

    def is_active(self) -> bool:
        """Return ``True`` if the module is enabled by configuration.

        Example:
            >>> ModuleToggle(enabled=False).is_active()
            False
        """

        return self.enabled


PARSER_MODULE_KEY = "parser"

ParserTokenLimit = int | Literal["auto"] | None
ParserConcurrencyValue = int | Literal["auto"]


class ParserGitignoreBehavior(StrEnum):
    """Supported `.gitignore` precedence modes for the parser."""

    REPO = "repo"
    WORKSPACE = "workspace"
    COMBINED = "combined"


def _normalize_token_cap(
    value: ParserTokenLimit,
    *,
    allow_none: bool,
) -> ParserTokenLimit:
    """Normalize and validate token cap values."""

    if value is None:
        if allow_none:
            return None
        raise ValueError("Token cap cannot be null for this field.")

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized != "auto":
            raise ValueError("Token cap string values must be 'auto'.")
        return "auto"

    if value < 1:
        raise ValueError("Token cap integers must be >= 1.")

    return value


class ParserHandlerSettings(BaseModel):
    """Configuration for an individual parser handler."""

    enabled: bool = Field(
        default=True,
        description="Whether the handler is enabled for dispatch.",
    )
    max_tokens: ParserTokenLimit = Field(
        default=None,
        description=(
            "Maximum tokens for handler output; ``null`` inherits the "
            "general cap and ``'auto'`` defers to handler heuristics."
        ),
    )

    model_config = {
        "frozen": True,
        "str_strip_whitespace": True,
    }

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(
        cls,
        value: ParserTokenLimit,
    ) -> ParserTokenLimit:
        return _normalize_token_cap(value, allow_none=True)


_DEFAULT_PARSER_HANDLER_NAMES: tuple[str, ...] = (
    "text",
    "markdown",
    "python",
    "javascript",
    "typescript",
    "html",
    "css",
)


def _default_parser_handlers() -> dict[str, ParserHandlerSettings]:
    """Return the baseline handler configuration mapping."""

    return {
        name: ParserHandlerSettings() for name in _DEFAULT_PARSER_HANDLER_NAMES
    }


class ParserModuleSettings(ModuleToggle):
    """Extended toggle carrying parser module configuration values."""

    extras: tuple[str, ...] = Field(
        default=("parser",),
        description="Optional dependency extras required for the parser.",
    )
    handlers: dict[str, ParserHandlerSettings] = Field(
        default_factory=_default_parser_handlers,
        description="Per-handler overrides keyed by handler name.",
    )
    general_max_tokens: ParserTokenLimit = Field(
        default=2000,
        description=(
            "Default token cap applied when handlers do not override it."
        ),
    )
    max_concurrency: ParserConcurrencyValue = Field(
        default="auto",
        description=(
            "Number of sources parsed concurrently or 'auto' for dynamic"
            " selection."
        ),
    )
    fail_fast: bool = Field(
        default=False,
        description=(
            "Stop parsing on first handler failure when true; default to "
            "resilient mode."
        ),
    )
    gitignore_behavior: ParserGitignoreBehavior = Field(
        default=ParserGitignoreBehavior.COMBINED,
        description=(
            "How repository and workspace ignore rules are combined during "
            "traversal."
        ),
    )

    model_config = {
        "frozen": True,
        "str_strip_whitespace": True,
    }

    @field_validator("general_max_tokens")
    @classmethod
    def _validate_general_max_tokens(
        cls,
        value: ParserTokenLimit,
    ) -> ParserTokenLimit:
        return _normalize_token_cap(value, allow_none=True)

    @field_validator("max_concurrency")
    @classmethod
    def _validate_max_concurrency(
        cls,
        value: ParserConcurrencyValue,
    ) -> ParserConcurrencyValue:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized != "auto":
                raise ValueError(
                    (
                        "Parser max_concurrency must be a positive integer "
                        "or 'auto'."
                    )
                )
            return "auto"
        if value < 1:
            raise ValueError(
                (
                    "Parser max_concurrency must be >= 1 when provided as "
                    "an integer."
                )
            )
        return value

    @model_validator(mode="after")
    def _normalize_handlers(self) -> "ParserModuleSettings":
        normalized: dict[str, ParserHandlerSettings] = {}
        for name, settings in self.handlers.items():
            key = name.strip()
            if not key:
                raise ValueError("Parser handler names cannot be blank.")
            normalized[key] = settings
        object.__setattr__(self, "handlers", normalized)
        return self


class AppConfig(BaseModel):
    """Root configuration for the :mod:`raggd` application."""

    workspace_settings: WorkspaceSettings = Field(
        default_factory=WorkspaceSettings,
        description="Workspace-level configuration including managed sources.",
        alias="workspace",
    )
    log_level: str = Field(
        default="INFO",
        description="Default logging level for the application runtime.",
    )
    modules: dict[str, ModuleToggle] = Field(
        default_factory=dict,
        description="Per-module toggle configuration keyed by module slug.",
    )
    db: DbSettings = Field(
        default_factory=DbSettings,
        description="Database module configuration values.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "populate_by_name": True,
    }

    @model_validator(mode="after")
    def _post_process(self) -> "AppConfig":
        """Normalize fields after validation."""

        object.__setattr__(self, "log_level", self.log_level.upper())
        return self

    @property
    def workspace(self) -> Path:
        """Return the configured workspace root path."""

        return self.workspace_settings.root

    @property
    def workspace_sources(self) -> dict[str, WorkspaceSourceConfig]:
        """Return registered workspace sources keyed by name."""

        return self.workspace_settings.sources

    @property
    def parser(self) -> ParserModuleSettings:
        """Return parser module settings, falling back to defaults."""

        toggle = self.modules.get(PARSER_MODULE_KEY)
        if isinstance(toggle, ParserModuleSettings):
            return toggle
        if toggle is not None:
            return ParserModuleSettings(**toggle.model_dump())
        return ParserModuleSettings()

    def iter_workspace_sources(
        self,
    ) -> Iterable[tuple[str, WorkspaceSourceConfig]]:
        """Iterate over registered workspace sources."""

        return self.workspace_settings.iter_sources()


DEFAULTS_RESOURCE_NAME = "raggd.defaults.toml"


def read_packaged_defaults_text() -> str:
    """Return the raw packaged defaults TOML content.

    Example:
        >>> text = read_packaged_defaults_text()
        >>> text.startswith("#")
        True
    """

    resource = get_resource(DEFAULTS_RESOURCE_NAME)
    return resource.read_text(encoding="utf-8")


def load_packaged_defaults() -> dict[str, Any]:
    """Load the packaged defaults as a plain dictionary.

    Example:
        >>> defaults = load_packaged_defaults()
        >>> defaults["log_level"]
        'INFO'
    """

    text = read_packaged_defaults_text()
    data: dict[str, Any] = tomllib.loads(text)
    return data


def _deep_merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` returning a new dict."""

    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], MappingABC)
            and isinstance(value, MappingABC)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce_toggle(value: Any) -> ModuleToggle:
    """Convert arbitrary toggle representations to :class:`ModuleToggle`."""

    if isinstance(value, ModuleToggle):
        return value
    if isinstance(value, MappingABC):
        return ModuleToggle(**value)
    if isinstance(value, bool):
        return ModuleToggle(enabled=bool(value))
    raise TypeError(f"Unsupported module toggle value: {value!r}")


def _coerce_parser_module(value: Any) -> ParserModuleSettings:
    """Convert inputs to :class:`ParserModuleSettings`."""

    if isinstance(value, ParserModuleSettings):
        return value
    if isinstance(value, ModuleToggle):
        payload = value.model_dump()
        return ParserModuleSettings(**payload)
    if isinstance(value, MappingABC):
        return ParserModuleSettings(**value)
    if isinstance(value, bool):
        return ParserModuleSettings(enabled=bool(value))
    raise TypeError(f"Unsupported parser module value: {value!r}")


def _normalize_modules(
    raw: Mapping[str, Any] | None,
) -> dict[str, ModuleToggle]:
    """Normalize raw module mapping to ``ModuleToggle`` instances."""

    modules: dict[str, ModuleToggle] = {}
    if not raw:
        return modules

    for name, value in raw.items():
        if name == PARSER_MODULE_KEY:
            modules[name] = _coerce_parser_module(value)
        else:
            modules[name] = _coerce_toggle(value)
    return modules


def _apply_module_overrides(
    modules: dict[str, ModuleToggle],
    overrides: Mapping[str, Any] | None,
) -> dict[str, ModuleToggle]:
    """Apply override values while preserving metadata like extras."""

    if not overrides:
        return modules

    updated = dict(modules)
    for name, override in overrides.items():
        if name == PARSER_MODULE_KEY:
            override_toggle = _coerce_parser_module(override)
        else:
            override_toggle = _coerce_toggle(override)
        current = updated.get(name)
        if current is None:
            updated[name] = override_toggle
            continue

        data = current.model_dump()
        override_data = override_toggle.model_dump()
        if not override_toggle.extras:
            # Preserve existing extras when the override does not provide them.
            override_data.pop("extras", None)
        data.update(override_data)
        target_cls: type[ModuleToggle]
        if name == PARSER_MODULE_KEY:
            target_cls = ParserModuleSettings
        else:
            target_cls = ModuleToggle
        updated[name] = target_cls(**data)
    return updated


def _build_parser_handlers_table(
    toggle: ParserModuleSettings,
) -> tomlkit.table:
    handlers_table = tomlkit.table(is_super_table=True)
    for handler_name in sorted(toggle.handlers):
        handler_settings = toggle.handlers[handler_name]
        handler_entry = tomlkit.table()
        handler_entry["enabled"] = handler_settings.enabled
        if handler_settings.max_tokens is not None:
            handler_entry["max_tokens"] = handler_settings.max_tokens
        handlers_table.add(handler_name, handler_entry)
    return handlers_table


def _render_module_entry(toggle: ModuleToggle) -> tomlkit.table:
    if isinstance(toggle, ParserModuleSettings):
        entry = tomlkit.table()
        entry["enabled"] = toggle.enabled
        if toggle.extras:
            entry["extras"] = list(toggle.extras)
        entry["general_max_tokens"] = toggle.general_max_tokens
        entry["max_concurrency"] = toggle.max_concurrency
        entry["fail_fast"] = toggle.fail_fast
        entry["gitignore_behavior"] = toggle.gitignore_behavior.value

        if toggle.handlers:
            handlers_table = _build_parser_handlers_table(toggle)
            entry.add("handlers", handlers_table)
        return entry

    entry = tomlkit.table()
    entry["enabled"] = toggle.enabled
    if toggle.extras:
        entry["extras"] = list(toggle.extras)
    return entry


def load_config(
    *,
    defaults: Mapping[str, Any],
    user_config: Mapping[str, Any] | None = None,
    env_config: Mapping[str, Any] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    module_overrides: Mapping[str, ModuleToggle] | None = None,
) -> AppConfig:
    """Load configuration according to the precedence stack.

    Args:
        defaults: Packaged defaults shipped with the application.
        user_config: Parsed user ``raggd.toml`` content.
        env_config: Settings derived from environment variables.
        cli_overrides: Settings supplied via CLI flags.
        module_overrides: Final module toggle overrides derived from CLI.

    Returns:
        A validated :class:`AppConfig` instance.

    Raises:
        TypeError: If module override types are unsupported.
    """

    stack = dict(defaults)
    for layer in (user_config, env_config, cli_overrides):
        if layer:
            stack = _deep_merge(stack, layer)

    modules = _normalize_modules(stack.pop("modules", None))
    modules = _apply_module_overrides(modules, module_overrides)
    stack["modules"] = modules

    workspace_raw = stack.pop("workspace", None)
    if isinstance(workspace_raw, WorkspaceSettings):
        workspace_settings = workspace_raw
    elif isinstance(workspace_raw, MappingABC):
        workspace_settings = WorkspaceSettings.from_mapping(workspace_raw)
    elif workspace_raw is None:
        workspace_settings = WorkspaceSettings()
    else:
        workspace_settings = WorkspaceSettings(root=workspace_raw)
    stack["workspace"] = workspace_settings

    db_raw = stack.pop("db", None)
    if isinstance(db_raw, DbSettings):
        db_settings = db_raw
    elif isinstance(db_raw, MappingABC):
        db_settings = DbSettings(**db_raw)
    elif db_raw is None:
        db_settings = DbSettings()
    else:
        raise TypeError(f"Unsupported db configuration payload: {db_raw!r}")
    stack["db"] = db_settings

    return AppConfig(**stack)


def render_user_config(
    config: AppConfig,
    *,
    include_defaults: bool = True,
) -> str:
    """Render a ``raggd.toml`` template for users to customize.

    Args:
        config: Configuration instance to serialize.
        include_defaults: Whether to inline default commentary/values.

    Returns:
        A TOML-formatted string ready to persist for the user.
    """

    document = tomlkit.document()

    if include_defaults:
        document.add(tomlkit.comment("Generated by raggd init"))
        document.add(
            tomlkit.comment(
                "Precedence: CLI flags > env vars > raggd.toml > defaults"
            )
        )
        document.add(tomlkit.comment("Environment overrides:"))
        document.add(tomlkit.comment("  RAGGD_WORKSPACE=/path/to/workspace"))
        document.add(tomlkit.comment("  RAGGD_LOG_LEVEL=info"))
        document.add(tomlkit.nl())

    workspace_table = tomlkit.table()
    workspace_table["root"] = str(config.workspace)

    if config.workspace_sources:
        sources_table = tomlkit.table(is_super_table=True)
        for name, source in sorted(config.iter_workspace_sources()):
            entry = tomlkit.table()
            entry["enabled"] = source.enabled
            entry["path"] = str(source.path)
            if source.target is not None:
                entry["target"] = str(source.target)
            sources_table.add(name, entry)
        workspace_table.add("sources", sources_table)

    document["workspace"] = workspace_table
    document["log_level"] = config.log_level

    db_table = tomlkit.table()
    db_table["manifest_modules_key"] = config.db.manifest_modules_key
    db_table["manifest_db_module_key"] = config.db.manifest_db_module_key
    db_table["manifest_backup_retention"] = config.db.manifest_backup_retention
    db_table["manifest_lock_timeout"] = config.db.manifest_lock_timeout
    db_table["manifest_lock_poll_interval"] = (
        config.db.manifest_lock_poll_interval
    )
    db_table["manifest_lock_suffix"] = config.db.manifest_lock_suffix
    db_table["manifest_backup_suffix"] = config.db.manifest_backup_suffix
    db_table["manifest_strict"] = config.db.manifest_strict
    db_table["manifest_backups_enabled"] = config.db.manifest_backups_enabled
    db_table["migrations_path"] = config.db.migrations_path
    db_table["ensure_auto_upgrade"] = config.db.ensure_auto_upgrade
    db_table["vacuum_max_stale_days"] = config.db.vacuum_max_stale_days
    db_table["vacuum_concurrency"] = config.db.vacuum_concurrency
    db_table["run_allow_outside"] = config.db.run_allow_outside
    db_table["run_autocommit_default"] = config.db.run_autocommit_default
    db_table["drift_warning_seconds"] = config.db.drift_warning_seconds
    document["db"] = db_table

    if config.modules:
        if include_defaults:
            document.add(tomlkit.comment("Module toggles:"))
        modules_table = tomlkit.table()
        for name in sorted(config.modules):
            toggle = config.modules[name]
            entry = _render_module_entry(toggle)
            modules_table.add(name, entry)
        document["modules"] = modules_table

    return tomlkit.dumps(document)


def iter_module_configs(
    config: AppConfig,
) -> Iterable[tuple[str, ModuleToggle]]:
    """Iterate over module toggles for registry evaluation."""

    return config.modules.items()


def iter_workspace_sources(
    config: AppConfig,
) -> Iterable[tuple[str, WorkspaceSourceConfig]]:
    """Iterate over workspace source configurations."""

    return config.iter_workspace_sources()


__all__ = [
    "AppConfig",
    "ModuleToggle",
    "WorkspaceSettings",
    "DbSettings",
    "ParserGitignoreBehavior",
    "ParserHandlerSettings",
    "ParserModuleSettings",
    "PARSER_MODULE_KEY",
    "DEFAULTS_RESOURCE_NAME",
    "iter_module_configs",
    "iter_workspace_sources",
    "load_config",
    "load_packaged_defaults",
    "read_packaged_defaults_text",
    "render_user_config",
]
