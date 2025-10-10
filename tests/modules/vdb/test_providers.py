from __future__ import annotations

from typing import Mapping, Sequence

import pytest
from structlog import get_logger

from raggd.modules.vdb import (
    EmbedRequestOptions,
    EmbeddingMatrix,
    EmbeddingProviderCaps,
    EmbeddingProviderModel,
    ProviderInitContext,
    ProviderNotRegisteredError,
    ProviderRegistry,
    ProviderRegistryError,
)


class _StubProvider:
    """Minimal provider used to exercise registry wiring."""

    def __init__(self, *, logger, config: Mapping[str, object]) -> None:
        self.logger = logger
        self.config = config

    def describe_model(self, model: str) -> EmbeddingProviderModel:
        return EmbeddingProviderModel(provider="stub", name=model, dim=3)

    def capabilities(
        self,
        *,
        model: str | None = None,
    ) -> EmbeddingProviderCaps:
        return EmbeddingProviderCaps(max_batch_size=16, max_parallel_requests=2)

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:
        return tuple((1.0, 0.0, 0.0) for _ in texts)


def _stub_factory(context: ProviderInitContext) -> _StubProvider:
    return _StubProvider(logger=context.logger, config=context.config)


def test_embedding_provider_model_normalizes_and_computes_key() -> None:
    model = EmbeddingProviderModel(
        provider=" OpenAI ",
        name=" text-embedding-3 ",
    )

    assert model.provider == "openai"
    assert model.name == "text-embedding-3"
    assert model.key == "openai:text-embedding-3"

    with pytest.raises(ValueError):
        EmbeddingProviderModel(provider="", name="ada")
    with pytest.raises(ValueError):
        EmbeddingProviderModel(provider="openai", name="", dim=1024)
    with pytest.raises(ValueError):
        EmbeddingProviderModel(provider="openai", name="ada", dim=0)


def test_embed_request_options_and_caps_validate_inputs() -> None:
    options = EmbedRequestOptions(max_batch_size=8, timeout=30.0)
    assert options.max_batch_size == 8
    assert options.timeout == 30.0

    caps = EmbeddingProviderCaps(max_batch_size=32, max_parallel_requests=4)
    assert caps.max_batch_size == 32
    assert caps.max_parallel_requests == 4

    with pytest.raises(ValueError):
        EmbedRequestOptions(max_batch_size=0)
    with pytest.raises(ValueError):
        EmbedRequestOptions(max_batch_size=4, timeout=0)
    with pytest.raises(ValueError):
        EmbeddingProviderCaps(max_batch_size=0, max_parallel_requests=1)
    with pytest.raises(ValueError):
        EmbeddingProviderCaps(max_batch_size=1, max_parallel_requests=0)


def test_provider_registry_registers_and_creates_instances() -> None:
    registry = ProviderRegistry()
    registry.register("OpenAI", _stub_factory)

    logger = get_logger("test.provider.registry")
    provider = registry.create(
        " openai ",
        logger=logger,
        config={"api_key": "sk-test"},
    )

    assert isinstance(provider, _StubProvider)
    assert provider.logger is logger
    assert provider.config["api_key"] == "sk-test"
    with pytest.raises(TypeError):  # mapping proxy is immutable
        provider.config["api_key"] = "sk-live"  # type: ignore[misc]


def test_provider_registry_prevents_duplicates_and_handles_missing() -> None:
    registry = ProviderRegistry({"stub": _stub_factory})

    with pytest.raises(ProviderRegistryError):
        registry.register("STUB", _stub_factory)

    with pytest.raises(ProviderNotRegisteredError):
        registry.create("missing", logger=get_logger("test"))

    with pytest.raises(ValueError):
        registry.register("   ", _stub_factory)
    with pytest.raises(ValueError):
        registry.create("", logger=get_logger("test"))


def test_provider_registry_snapshot_is_immutable_and_detached() -> None:
    registry = ProviderRegistry()
    registry.register("stub", _stub_factory)

    snapshot = registry.snapshot()
    assert "stub" in snapshot

    with pytest.raises(TypeError):
        snapshot["stub"] = _stub_factory  # type: ignore[index]

    registry.unregister("stub")
    assert "stub" not in registry.snapshot()
    assert "stub" in snapshot
