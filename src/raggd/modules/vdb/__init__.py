"""Vector database (VDB) module primitives."""

from __future__ import annotations

from .models import (
    EmbeddingModel,
    Vdb,
    VdbHealthEntry,
    VdbInfoCounts,
    VdbInfoSummary,
)
from .providers import (
    EmbedRequestOptions,
    EmbeddingMatrix,
    EmbeddingProviderCaps,
    EmbeddingProviderModel,
    EmbeddingVector,
    EmbeddingsProvider,
    ProviderFactory,
    ProviderInitContext,
    ProviderNotRegisteredError,
    ProviderRegistry,
    ProviderRegistryError,
)

__all__ = [
    "EmbeddingModel",
    "Vdb",
    "VdbHealthEntry",
    "VdbInfoCounts",
    "VdbInfoSummary",
    "EmbedRequestOptions",
    "EmbeddingMatrix",
    "EmbeddingProviderCaps",
    "EmbeddingProviderModel",
    "EmbeddingVector",
    "EmbeddingsProvider",
    "ProviderFactory",
    "ProviderInitContext",
    "ProviderNotRegisteredError",
    "ProviderRegistry",
    "ProviderRegistryError",
]
