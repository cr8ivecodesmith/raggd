"""Configuration models and loaders for :mod:`raggd`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field


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
        NotImplementedError: Until the stacking logic is implemented.
    """

    raise NotImplementedError(
        "Configuration loading will be implemented in a subsequent step."
    )


def render_user_config(config: AppConfig, *, include_defaults: bool = True) -> str:
    """Render a ``raggd.toml`` template for users to customize.

    Args:
        config: Configuration instance to serialize.
        include_defaults: Whether to inline default commentary/values.

    Returns:
        A TOML-formatted string ready to persist for the user.

    Raises:
        NotImplementedError: Until the renderer is implemented.
    """

    raise NotImplementedError(
        "Configuration rendering will be implemented in a subsequent step."
    )


def iter_module_configs(config: AppConfig) -> Iterable[tuple[str, ModuleToggle]]:
    """Iterate over module toggles for registry evaluation."""

    return config.modules.items()


__all__ = [
    "AppConfig",
    "ModuleToggle",
    "iter_module_configs",
    "load_config",
    "render_user_config",
]
