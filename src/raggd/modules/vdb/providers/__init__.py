"""Embedding provider abstractions and registry for the VDB module."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

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
]

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
