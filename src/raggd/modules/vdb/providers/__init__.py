"""Embedding provider abstractions and registry for the VDB module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from raggd.core.logging import Logger

__all__ = [
    "EmbeddingVector",
    "EmbeddingMatrix",
    "EmbedRequestOptions",
    "EmbeddingProviderCaps",
    "EmbeddingProviderModel",
    "EmbeddingsProvider",
    "ProviderFactory",
    "ProviderInitContext",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderNotRegisteredError",
    "resolve_sync_concurrency",
    "OpenAIEmbeddingsProvider",
    "openai_provider_factory",
    "register_builtin_providers",
    "create_default_provider_registry",
]

DEFAULT_AUTO_CONCURRENCY = 8

# Embedding vector aliases keep typing concise across provider implementations.
EmbeddingVector = tuple[float, ...]
EmbeddingMatrix = tuple[EmbeddingVector, ...]


@dataclass(frozen=True, slots=True)
class EmbedRequestOptions:
    """Request tuning options shared across embedding providers."""

    max_batch_size: int
    timeout: float | None = None

    def __post_init__(self) -> None:  # pragma: no cover - defensive invariants
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be >= 1")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive when provided")


@dataclass(frozen=True, slots=True)
class EmbeddingProviderCaps:
    """Capability metadata surfaced by providers for planning."""

    max_batch_size: int
    max_parallel_requests: int
    max_input_tokens: int | None = None
    max_request_tokens: int | None = None

    def __post_init__(self) -> None:  # pragma: no cover - defensive invariants
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be >= 1")
        if self.max_parallel_requests < 1:
            raise ValueError("max_parallel_requests must be >= 1")
        if self.max_input_tokens is not None and self.max_input_tokens < 1:
            raise ValueError("max_input_tokens must be >= 1 when set")
        if self.max_request_tokens is not None and self.max_request_tokens < 1:
            raise ValueError("max_request_tokens must be >= 1 when set")


@dataclass(frozen=True, slots=True)
class EmbeddingProviderModel:
    """Model descriptor returned from providers when resolving dims."""

    provider: str
    name: str
    dim: int | None = None

    def __post_init__(self) -> None:  # pragma: no cover - defensive invariants
        provider = self.provider.strip().lower()
        if not provider:
            raise ValueError("provider cannot be empty")
        object.__setattr__(self, "provider", provider)

        name = self.name.strip()
        if not name:
            raise ValueError("model name cannot be empty")
        object.__setattr__(self, "name", name)

        if self.dim is not None and self.dim < 1:
            raise ValueError("dim must be >= 1 when provided")

    @property
    def key(self) -> str:
        """Return the canonical provider:model key."""

        return f"{self.provider}:{self.name}"


@runtime_checkable
class EmbeddingsProvider(Protocol):
    """Boundary contract for embedding providers."""

    def describe_model(self, model: str) -> EmbeddingProviderModel:
        """Return provider metadata for ``model``.

        Dimension can remain unset until the first successful sync.
        """

    def capabilities(
        self,
        *,
        model: str | None = None,
    ) -> EmbeddingProviderCaps:
        """Return provider-level or model-specific capability hints."""

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:
        """Embed ``texts`` using ``model`` honoring ``options`` constraints."""


def _parse_limit(
    value: int | str | None,
    *,
    fallback: int,
    field_name: str,
) -> tuple[int, str]:
    """Normalize concurrency limit inputs returning a value and mode."""

    if value is None:
        return fallback, "default"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            message = f"{field_name} cannot be blank"
            raise ValueError(message)
        lowered = stripped.lower()
        if lowered == "auto":
            return fallback, "auto"
        try:
            parsed = int(stripped)
        except ValueError as exc:
            message = (
                f"{field_name} must be a positive integer or 'auto' "
                f"(got {value!r})."
            )
            raise ValueError(message) from exc
        if parsed < 1:
            raise ValueError(
                f"{field_name} must be >= 1 when provided as an integer."
            )
        return parsed, "fixed"
    if value < 1:
        raise ValueError(
            f"{field_name} must be >= 1 when provided as an integer."
        )
    return int(value), "fixed"


def resolve_sync_concurrency(
    *,
    requested: int | str | None,
    provider_caps: EmbeddingProviderCaps,
    config_value: int | str | None,
    logger: Logger,
    default_limit: int = DEFAULT_AUTO_CONCURRENCY,
) -> int:
    """Resolve VDB sync concurrency honoring provider caps and settings."""

    config_limit, config_mode = _parse_limit(
        config_value,
        fallback=default_limit,
        field_name="config.modules.vdb.max_concurrency",
    )

    if requested is None:
        base_limit = config_limit
        base_source = "config"
        base_mode = config_mode
        requested_raw: int | str | None = config_value
    else:
        base_limit, base_mode = _parse_limit(
            requested,
            fallback=config_limit,
            field_name="--concurrency",
        )
        base_source = "override"
        requested_raw = requested

    cpu_limit = max(1, os.cpu_count() or 1)
    provider_limit = max(1, provider_caps.max_parallel_requests)
    resolved = max(1, min(base_limit, provider_limit, cpu_limit))

    limiters: list[str] = []
    if resolved == cpu_limit:
        limiters.append("cpu")
    if resolved == provider_limit:
        limiters.append("provider")
    if resolved == base_limit:
        limiters.append("config" if base_source == "config" else "override")

    logger.info(
        "vdb-concurrency-resolved",
        resolved=resolved,
        requested=requested_raw,
        config_value=config_value,
        mode=f"{base_source}-{base_mode}",
        cpu_limit=cpu_limit,
        provider_limit=provider_limit,
        config_limit=config_limit,
        base_limit=base_limit,
        limiters=tuple(sorted(set(limiters))),
        clamped=resolved < base_limit,
        default_limit=default_limit,
    )

    return resolved


@dataclass(frozen=True, slots=True)
class ProviderInitContext:
    """Construction context supplied to provider factories."""

    logger: Logger
    config: Mapping[str, object] | None = None

    def __post_init__(self) -> None:  # pragma: no cover - defensive invariants
        config = dict(self.config or {})
        object.__setattr__(self, "config", MappingProxyType(config))


ProviderFactory = Callable[[ProviderInitContext], EmbeddingsProvider]
"""Factory callable responsible for instantiating providers."""


class ProviderRegistryError(RuntimeError):
    """Base error type raised when interacting with the provider registry."""


class ProviderNotRegisteredError(ProviderRegistryError):
    """Raised when a provider lookup fails for the requested key."""


class ProviderRegistry:
    """Mutable registry mapping provider keys to factory callables."""

    def __init__(
        self,
        factories: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        if factories:
            for key, factory in factories.items():
                self.register(key, factory)

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized = key.strip().lower()
        if not normalized:
            raise ValueError("provider key cannot be empty")
        return normalized

    def register(self, key: str, factory: ProviderFactory) -> None:
        """Register ``factory`` under ``key``; errors if key already present."""

        normalized = self._normalize_key(key)
        if normalized in self._factories:
            raise ProviderRegistryError(
                f"Provider {normalized!r} already registered",
            )
        self._factories[normalized] = factory

    def unregister(self, key: str) -> None:
        """Remove the factory registered under ``key`` if it exists."""

        normalized = self._normalize_key(key)
        self._factories.pop(normalized, None)

    def get_factory(self, key: str) -> ProviderFactory:
        """Return the factory registered for ``key`` or raise."""

        normalized = self._normalize_key(key)
        try:
            return self._factories[normalized]
        except KeyError as exc:  # pragma: no cover - defensive branch
            raise ProviderNotRegisteredError(
                f"No provider registered under key {normalized!r}",
            ) from exc

    def create(
        self,
        key: str,
        *,
        logger: Logger,
        config: Mapping[str, object] | None = None,
    ) -> EmbeddingsProvider:
        """Instantiate the provider registered under ``key``."""

        factory = self.get_factory(key)
        context = ProviderInitContext(logger=logger, config=config)
        return factory(context)

    def snapshot(self) -> Mapping[str, ProviderFactory]:
        """Return an immutable view of registered provider factories."""

        return MappingProxyType(dict(self._factories))


if TYPE_CHECKING:  # pragma: no cover - type checker imports only
    from .openai import OpenAIEmbeddingsProvider, openai_provider_factory


def __getattr__(name: str) -> object:
    if name in {"OpenAIEmbeddingsProvider", "openai_provider_factory"}:
        from .openai import (
            OpenAIEmbeddingsProvider,
            openai_provider_factory,
        )

        exports = {
            "OpenAIEmbeddingsProvider": OpenAIEmbeddingsProvider,
            "openai_provider_factory": openai_provider_factory,
        }
        return exports[name]

    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


def _load_openai_factory() -> ProviderFactory:
    from .openai import openai_provider_factory

    return openai_provider_factory


def register_builtin_providers(
    registry: ProviderRegistry,
) -> ProviderRegistry:
    """Register built-in embedding providers on ``registry``."""

    if "openai" not in registry.snapshot():
        registry.register("openai", _load_openai_factory())
    return registry


def create_default_provider_registry() -> ProviderRegistry:
    """Return a provider registry populated with built-in providers."""

    registry = ProviderRegistry()
    register_builtin_providers(registry)
    return registry
