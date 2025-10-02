"""Module registry scaffolding for :mod:`raggd`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, MutableMapping

from raggd.core.config import ModuleToggle


@dataclass(slots=True)
class ModuleDescriptor:
    """Lightweight declaration describing an optional module."""

    name: str
    description: str
    extras: tuple[str, ...] = field(default_factory=tuple)
    default_toggle: ModuleToggle = field(
        default_factory=ModuleToggle,
    )

    def emit(self) -> None:
        """Execute module-specific setup hooks.

        Raises:
            NotImplementedError: Until the descriptor lifecycle is implemented.
        """

        raise NotImplementedError(
            "Module lifecycle hooks will be implemented in a later step."
        )


class ModuleRegistry:
    """Collection managing module descriptors and enablement state."""

    def __init__(self, descriptors: Iterable[ModuleDescriptor]):
        self._descriptors: tuple[ModuleDescriptor, ...] = tuple(descriptors)

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

        Raises:
            NotImplementedError: Until evaluation logic is implemented.
        """

        raise NotImplementedError(
            "Module evaluation will be implemented in a subsequent step."
        )


__all__ = ["ModuleDescriptor", "ModuleRegistry"]
