"""Module registry scaffolding for :mod:`raggd`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, MutableMapping

from raggd.core.config import ModuleToggle
from raggd.core.logging import get_logger


@dataclass(slots=True)
class ModuleDescriptor:
    """Lightweight declaration describing an optional module.

    Args:
        name: Slug used when referencing the module in configuration.
        description: Short human-readable description of the capability.
        extras: Optional dependency group names required for activation.
        default_toggle: Baseline toggle applied when no user config exists.

    Example:
        >>> descriptor = ModuleDescriptor(
        ...     name="mcp",
        ...     description="Model Context Protocol integration",
        ...     extras=("mcp",),
        ...     default_toggle=ModuleToggle(enabled=False, extras=("mcp",)),
        ... )
        >>> descriptor.is_available({"mcp"})
        True
    """

    name: str
    description: str
    extras: tuple[str, ...] = field(default_factory=tuple)
    default_toggle: ModuleToggle = field(
        default_factory=ModuleToggle,
    )

    def __post_init__(self) -> None:
        normalized_extras = tuple(
            dict.fromkeys(
                extra.strip() for extra in self.extras if extra.strip()
            )
        )
        object.__setattr__(self, "extras", normalized_extras)

        if self.extras and not self.default_toggle.extras:
            object.__setattr__(
                self,
                "default_toggle",
                ModuleToggle(
                    enabled=self.default_toggle.enabled,
                    extras=self.extras,
                ),
            )

    def required_extras(
        self,
        override: ModuleToggle | None = None,
    ) -> tuple[str, ...]:
        """Return the extras that must be present for the module.

        Args:
            override: Toggle sourced from configuration that may refine extras.

        Returns:
            A normalized tuple of dependency group names.
        """

        if override and override.extras:
            return override.extras
        return self.default_toggle.extras or self.extras

    def is_available(
        self,
        available_extras: Iterable[str] | None,
        *,
        override: ModuleToggle | None = None,
    ) -> bool:
        """Check whether required extras are present.

        Example:
            >>> descriptor = ModuleDescriptor(name="alpha", description="test")
            >>> descriptor.is_available({"alpha"})
            True
        """

        required = self.required_extras(override)
        if not required:
            return True
        if available_extras is None:
            return False
        available = {extra.lower() for extra in available_extras}
        return all(extra.lower() in available for extra in required)

    def emit(self) -> None:
        """Execute module-specific setup hooks.

        Default descriptors do not perform any work, but concrete modules can
        subclass or wrap this method to register handlers, warm caches, or bind
        background services. Keeping a seam here allows us to slot in a richer
        lifecycle manager (e.g., :mod:`pluggy`) later without changing callers.
        """

        # Intentionally left as a no-op hook.
        return None


class ModuleRegistry:
    """Collection managing module descriptors and enablement state."""

    def __init__(self, descriptors: Iterable[ModuleDescriptor]):
        deduped: list[ModuleDescriptor] = []
        seen: set[str] = set()
        for descriptor in descriptors:
            if descriptor.name in seen:
                raise ValueError(
                    f"Duplicate module descriptor: {descriptor.name!r}"
                )
            deduped.append(descriptor)
            seen.add(descriptor.name)
        self._descriptors: tuple[ModuleDescriptor, ...] = tuple(deduped)
        self._descriptor_index = {
            descriptor.name: descriptor for descriptor in self._descriptors
        }

    def iter_descriptors(self) -> Iterator[ModuleDescriptor]:
        """Iterate over registered descriptors in declaration order."""

        return iter(self._descriptors)

    def evaluate(
        self,
        *,
        toggles: Mapping[str, ModuleToggle],
        available_extras: Iterable[str] | None = None,
        status_sink: MutableMapping[str, str] | None = None,
    ) -> dict[str, bool]:
        """Evaluate descriptor enablement state.

        Example:
            >>> registry = ModuleRegistry([])
            >>> registry.evaluate(toggles={})
            {}

        The registry reports whether each known module should be activated by
        combining descriptor defaults with configuration toggles and optional
        dependency availability checks. A human-readable reason is stored in the
        optional ``status_sink`` for CLI presentation or diagnostics.

        Args:
            toggles: Mapping of module names to configuration toggles.
            available_extras: Collection of installed extras used to validate
                dependency availability. When ``None`` only modules without
                extras are considered available.
            status_sink: Optional mapping populated with status messages keyed
                by module name.

        Returns:
            Mapping from module name to active (True) or inactive (False).
        """

        configured: dict[str, ModuleToggle] = dict(toggles)
        descriptor_names = set(self._descriptor_index)
        results: dict[str, bool] = {}
        available_set = (
            {extra.lower() for extra in available_extras}
            if available_extras is not None
            else set()
        )

        # Flag any unknown configuration entries so users can debug typos.
        unknown_modules = sorted(set(configured) - descriptor_names)
        for name in unknown_modules:
            logger = get_logger(__name__, module=name)
            logger.warning("module-unknown", enabled=False)
            if status_sink is not None:
                status_sink[name] = "unknown module"

        for descriptor in self._descriptors:
            toggle = configured.get(descriptor.name, descriptor.default_toggle)
            required = descriptor.required_extras(toggle)
            missing = tuple(
                extra
                for extra in required
                if extra.lower() not in available_set
            )
            is_available = not required or (
                available_extras is not None and not missing
            )
            is_enabled = toggle.is_active() and is_available

            logger = get_logger(__name__, module=descriptor.name)

            if not toggle.is_active():
                reason = "disabled via configuration"
            elif not is_available:
                if required:
                    reason = "missing extras: " + ", ".join(missing)
                else:
                    reason = "unavailable"  # pragma: no cover - defensive guard
            else:
                reason = "enabled"

            if status_sink is not None:
                status_sink[descriptor.name] = reason

            results[descriptor.name] = is_enabled

            logger.info(
                "module-evaluated",
                enabled=is_enabled,
                configured=toggle.is_active(),
                available=is_available,
                extras=required,
                missing_extras=missing,
                reason=reason,
                explicit=descriptor.name in configured,
            )

            if is_enabled:
                descriptor.emit()

        return results


__all__ = ["ModuleDescriptor", "ModuleRegistry"]
