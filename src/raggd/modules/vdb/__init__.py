"""Vector database (VDB) module primitives."""

from __future__ import annotations

from .faiss_index import (
    FaissIndex,
    FaissIndexError,
    FaissIndexMetric,
    FaissIndexRemoveError,
)
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
    resolve_sync_concurrency,
)

__all__ = [
    "FaissIndex",
    "FaissIndexError",
    "FaissIndexMetric",
    "FaissIndexRemoveError",
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
    "resolve_sync_concurrency",
]
