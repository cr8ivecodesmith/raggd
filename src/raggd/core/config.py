"""Configuration models and loaders for :mod:`raggd`."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from pathlib import Path
from typing import Any, Iterable, Mapping

import tomllib
import tomlkit
from pydantic import BaseModel, Field, model_validator

from raggd.resources import get_resource


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


class AppConfig(BaseModel):
    """Root configuration for the :mod:`raggd` application."""

    workspace: Path = Field(
        default_factory=lambda: Path("~/.raggd").expanduser(),
        description="Absolute path to the workspace root.",
    )
    log_level: str = Field(
        default="INFO",
        description="Default logging level for the application runtime.",
    )
    modules: dict[str, ModuleToggle] = Field(
        default_factory=dict,
        description="Per-module toggle configuration keyed by module slug.",
    )

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }

    @model_validator(mode="after")
    def _post_process(self) -> "AppConfig":
        """Normalize fields after validation."""

        object.__setattr__(self, "workspace", self.workspace.expanduser())
        object.__setattr__(self, "log_level", self.log_level.upper())
        return self


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


def _normalize_modules(
    raw: Mapping[str, Any] | None,
) -> dict[str, ModuleToggle]:
    """Normalize raw module mapping to ``ModuleToggle`` instances."""

    modules: dict[str, ModuleToggle] = {}
    if not raw:
        return modules

    for name, value in raw.items():
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
        updated[name] = ModuleToggle(**data)
    return updated


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

    document["workspace"] = str(config.workspace)
    document["log_level"] = config.log_level

    if config.modules:
        if include_defaults:
            document.add(tomlkit.comment("Module toggles:"))
        modules_table = tomlkit.table()
        for name in sorted(config.modules):
            toggle = config.modules[name]
            entry = tomlkit.table()
            entry["enabled"] = toggle.enabled
            if toggle.extras:
                entry["extras"] = list(toggle.extras)
            modules_table.add(name, entry)
        document["modules"] = modules_table

    return tomlkit.dumps(document)


def iter_module_configs(
    config: AppConfig,
) -> Iterable[tuple[str, ModuleToggle]]:
    """Iterate over module toggles for registry evaluation."""

    return config.modules.items()


__all__ = [
    "AppConfig",
    "ModuleToggle",
    "DEFAULTS_RESOURCE_NAME",
    "iter_module_configs",
    "load_config",
    "load_packaged_defaults",
    "read_packaged_defaults_text",
    "render_user_config",
]
