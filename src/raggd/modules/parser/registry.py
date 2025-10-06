"""Handler registry and selection logic for the parser module."""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from raggd.core.config import ParserModuleSettings, ParserHandlerSettings
from raggd.modules import HealthStatus

__all__ = [
    "HandlerProbe",
    "HandlerProbeResult",
    "ParserHandlerDescriptor",
    "HandlerAvailability",
    "HandlerSelection",
    "HandlerRegistry",
    "build_default_registry",
]


HandlerProbe = Callable[[], "HandlerProbeResult"]


@dataclass(frozen=True, slots=True)
class HandlerProbeResult:
    """Result returned by handler dependency probes."""

    status: HealthStatus
    summary: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:  # pragma: no cover - dataclass invariants
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))


@dataclass(frozen=True, slots=True)
class ParserHandlerDescriptor:
    """Descriptor describing a parser handler implementation."""

    name: str
    version: str
    display_name: str
    extensions: tuple[str, ...] = ()
    shebangs: tuple[str, ...] = ()
    probe: HandlerProbe | None = None

    def __post_init__(self) -> None:  # pragma: no cover - defensive cleanup
        object.__setattr__(
            self,
            "extensions",
            tuple({ext.lower().lstrip(".") for ext in self.extensions if ext}),
        )
        object.__setattr__(
            self,
            "shebangs",
            tuple({normalize_shebang(sh) for sh in self.shebangs if sh}),
        )


@dataclass(frozen=True, slots=True)
class HandlerAvailability:
    """Snapshot of handler enablement and dependency health."""

    name: str
    enabled: bool
    status: HealthStatus
    summary: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HandlerSelection:
    """Decision returned when selecting a handler for a given path."""

    handler: ParserHandlerDescriptor
    resolved_via: str
    fallback: bool
    probe: HandlerProbeResult


class HandlerRegistry:
    """Registry mapping files to parser handlers based on heuristics."""

    def __init__(
        self,
        *,
        descriptors: Iterable[ParserHandlerDescriptor],
        settings: ParserModuleSettings,
        default_handler: str = "text",
    ) -> None:
        self._settings = settings
        self._descriptors: dict[str, ParserHandlerDescriptor] = {
            descriptor.name: descriptor for descriptor in descriptors
        }
        if default_handler not in self._descriptors:
            raise ValueError(
                f"Default handler {default_handler!r} not registered."
            )
        self._default_handler = default_handler
        self._extensions: dict[str, str] = {}
        self._shebangs: dict[str, str] = {}
        self._path_overrides: dict[str, str] = {}
        self._probe_cache: dict[str, HandlerProbeResult] = {}
        for descriptor in descriptors:
            for extension in descriptor.extensions:
                self._extensions[extension] = descriptor.name
            for shebang in descriptor.shebangs:
                self._shebangs[shebang] = descriptor.name

    # ------------------------------------------------------------------
    # Registration and configuration helpers
    # ------------------------------------------------------------------
    def register_path_override(self, path: Path | str, handler: str) -> None:
        """Register an explicit handler override for ``path``."""

        normalized = _normalize_path_key(path)
        if handler not in self._descriptors:
            raise KeyError(f"Unknown handler {handler!r} for override")
        self._path_overrides[normalized] = handler

    def remove_path_override(self, path: Path | str) -> None:
        """Remove a previously registered path override."""

        normalized = _normalize_path_key(path)
        self._path_overrides.pop(normalized, None)

    def descriptors(self) -> Mapping[str, ParserHandlerDescriptor]:
        """Return a read-only view of registered descriptors."""

        return dict(self._descriptors)

    # ------------------------------------------------------------------
    # Dependency probe helpers
    # ------------------------------------------------------------------
    def refresh_probe(self, handler: str) -> HandlerProbeResult:
        """Force-refresh a handler probe cache entry."""

        self._probe_cache.pop(handler, None)
        return self._probe(handler)

    def availability(self) -> tuple[HandlerAvailability, ...]:
        """Return availability snapshots for all handlers."""

        snapshots: list[HandlerAvailability] = []
        for name, descriptor in sorted(self._descriptors.items()):
            enabled = self._is_enabled(name)
            probe = self._probe(name)
            snapshots.append(
                HandlerAvailability(
                    name=name,
                    enabled=enabled,
                    status=probe.status if enabled else HealthStatus.UNKNOWN,
                    summary=probe.summary,
                    warnings=probe.warnings,
                )
            )
        return tuple(snapshots)

    # ------------------------------------------------------------------
    # Handler selection
    # ------------------------------------------------------------------
    def resolve(
        self,
        path: Path,
        *,
        explicit: str | None = None,
        shebang: str | None = None,
    ) -> HandlerSelection:
        """Select the most appropriate handler for ``path``."""

        explicit = explicit.strip() if explicit else None
        candidate_name = None
        resolved_via = "default"

        if explicit:
            if explicit in self._descriptors:
                candidate_name = explicit
                resolved_via = "explicit"
            else:
                raise KeyError(f"Unknown handler {explicit!r}")

        if candidate_name is None:
            override = self._lookup_override(path)
            if override is not None:
                candidate_name = override
                resolved_via = "override"

        if candidate_name is None and shebang:
            normalized_shebang = normalize_shebang(shebang)
            if normalized_shebang and normalized_shebang in self._shebangs:
                candidate_name = self._shebangs[normalized_shebang]
                resolved_via = f"shebang:{normalized_shebang}"

        if candidate_name is None:
            extension = _infer_extension(path)
            if extension and extension in self._extensions:
                candidate_name = self._extensions[extension]
                resolved_via = f"extension:{extension}"

        if candidate_name is None:
            candidate_name = self._default_handler

        return self._finalize_selection(
            candidate_name,
            resolved_via=resolved_via,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _lookup_override(self, path: Path) -> str | None:
        normalized = _normalize_path_key(path)
        return self._path_overrides.get(normalized)

    def _is_enabled(self, handler: str) -> bool:
        settings = self._settings.handlers.get(handler)
        if settings is None:
            return True
        if isinstance(settings, ParserHandlerSettings):
            return settings.enabled
        # Defensive fallback for unexpected config payloads.
        try:  # pragma: no cover - defensive branch
            return bool(settings.get("enabled", True))
        except AttributeError:  # pragma: no cover - defensive branch
            return True

    def _finalize_selection(
        self,
        handler_name: str,
        *,
        resolved_via: str,
    ) -> HandlerSelection:
        descriptor = self._descriptors[handler_name]
        probe = self._probe(handler_name)
        enabled = self._is_enabled(handler_name)

        if enabled and probe.status is HealthStatus.OK:
            return HandlerSelection(
                handler=descriptor,
                resolved_via=resolved_via,
                fallback=False,
                probe=probe,
            )

        fallback_reason = "disabled" if not enabled else "dependency"
        fallback_probe = probe

        # Attempt fallback to default handler.
        if handler_name != self._default_handler:
            default_probe = self._probe(self._default_handler)
            default_descriptor = self._descriptors[self._default_handler]
            if self._is_enabled(self._default_handler) and (
                default_probe.status is HealthStatus.OK
            ):
                return HandlerSelection(
                    handler=default_descriptor,
                    resolved_via=f"fallback:{fallback_reason}",
                    fallback=True,
                    probe=default_probe,
                )

        return HandlerSelection(
            handler=descriptor,
            resolved_via=f"unhealthy:{fallback_reason}",
            fallback=False,
            probe=fallback_probe,
        )

    def _probe(self, handler: str) -> HandlerProbeResult:
        if handler in self._probe_cache:
            return self._probe_cache[handler]
        descriptor = self._descriptors[handler]
        try:
            probe = descriptor.probe
            if probe is None:
                result = HandlerProbeResult(status=HealthStatus.OK)
            else:
                result = probe()
        except Exception as exc:  # pragma: no cover - defensive fallback
            result = HandlerProbeResult(
                status=HealthStatus.ERROR,
                summary=str(exc),
            )
        self._probe_cache[handler] = result
        return result


def _normalize_path_key(path: Path | str) -> str:
    value = Path(path)
    try:
        value = value.resolve()
    except OSError:
        value = value.absolute()
    return value.as_posix()


_DOT_SPLIT_RE = re.compile(r"^.+?\.([^.]+)$")


def _infer_extension(path: Path) -> str | None:
    suffix = path.suffix
    if not suffix and path.name.startswith("."):
        match = _DOT_SPLIT_RE.match(path.name[1:])
        if match:
            return match.group(1).lower()
        return None
    if not suffix:
        return None
    return suffix.lstrip(".").lower()


_SHEBANG_SPLIT_RE = re.compile(r"\s+")


def normalize_shebang(text: str) -> str:
    """Normalize a shebang declaration for lookup purposes."""

    payload = text.strip()
    if not payload:
        return ""
    if payload.startswith("#!"):
        payload = payload[2:]
    payload = payload.strip()
    if not payload:
        return ""
    parts = _SHEBANG_SPLIT_RE.split(payload)
    if not parts:
        return ""
    command = parts[0]
    if command.endswith("env") and len(parts) > 1:
        command = parts[1]
    command = command.strip()
    if not command:
        return ""
    return Path(command).name.lower()


def _probe_import(module: str) -> HandlerProbeResult:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        return HandlerProbeResult(
            status=HealthStatus.ERROR,
            summary=f"Missing dependency: {module}",
            warnings=(str(exc),),
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        return HandlerProbeResult(
            status=HealthStatus.ERROR,
            summary=str(exc),
        )
    return HandlerProbeResult(status=HealthStatus.OK)


def build_default_registry(settings: ParserModuleSettings) -> HandlerRegistry:
    """Build a registry with baseline handler descriptors."""

    descriptors = (
        ParserHandlerDescriptor(
            name="text",
            version="1.0.0",
            display_name="Plain Text",
            extensions=("txt", "log", "ini", "toml", "cfg"),
        ),
        ParserHandlerDescriptor(
            name="markdown",
            version="1.0.0",
            display_name="Markdown",
            extensions=(
                "md",
                "markdown",
                "mdown",
                "mkdn",
                "mkd",
            ),
            probe=lambda: _probe_import("tree_sitter_languages"),
        ),
        ParserHandlerDescriptor(
            name="python",
            version="1.0.0",
            display_name="Python",
            extensions=("py", "pyw", "pyi"),
            shebangs=("python", "python3", "python2"),
            probe=lambda: _probe_import("libcst"),
        ),
        ParserHandlerDescriptor(
            name="javascript",
            version="1.0.0",
            display_name="JavaScript",
            extensions=("js", "cjs", "mjs", "jsx"),
            shebangs=("node",),
            probe=lambda: _probe_import("tree_sitter_languages"),
        ),
        ParserHandlerDescriptor(
            name="typescript",
            version="1.0.0",
            display_name="TypeScript",
            extensions=("ts", "tsx", "cts", "mts"),
            probe=lambda: _probe_import("tree_sitter_languages"),
        ),
        ParserHandlerDescriptor(
            name="html",
            version="1.0.0",
            display_name="HTML",
            extensions=("html", "htm"),
            probe=lambda: _probe_import("tree_sitter_languages"),
        ),
        ParserHandlerDescriptor(
            name="css",
            version="1.0.0",
            display_name="CSS",
            extensions=("css", "scss", "less"),
            probe=lambda: _probe_import("tree_sitter_languages"),
        ),
    )
    return HandlerRegistry(descriptors=descriptors, settings=settings)

