from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pytest

from raggd.modules.vdb import (
    EmbedRequestOptions,
    EmbeddingMatrix,
    EmbeddingProviderCaps,
    EmbeddingProviderModel,
    EmbeddingsProvider,
)


@dataclass(slots=True)
class _RecordingStubProvider(EmbeddingsProvider):
    """Stub provider mirroring batching rules for contract tests."""

    model_name: str = "unit-test"
    dim: int = 4
    max_batch_size: int = 3
    max_parallel_requests: int = 2
    max_request_tokens: int = 24
    token_pad: int = 8
    tokens_per_char: int = 1

    def __post_init__(self) -> None:
        self._requests: list[Sequence[str]] = []

    @property
    def requests(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(batch) for batch in self._requests)

    def describe_model(self, model: str) -> EmbeddingProviderModel:
        if model.strip() != self.model_name:
            raise ValueError(f"unknown model {model!r}")
        return EmbeddingProviderModel(
            provider="stub",
            name=self.model_name,
            dim=self.dim,
        )

    def capabilities(
        self,
        *,
        model: str | None = None,
    ) -> EmbeddingProviderCaps:
        if model not in (None, self.model_name):
            raise ValueError(f"unknown model {model!r}")
        return EmbeddingProviderCaps(
            max_batch_size=self.max_batch_size,
            max_parallel_requests=self.max_parallel_requests,
            max_request_tokens=self.max_request_tokens,
        )

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:
        if model != self.model_name:
            raise ValueError(f"unknown model {model!r}")

        self._requests = []

        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0

        limit = min(options.max_batch_size, self.max_batch_size)
        token_ceiling = self.max_request_tokens

        for text in texts:
            tokens = self._estimate_tokens(text)
            if tokens > token_ceiling:
                raise ValueError(f"input too large ({tokens} tokens)")

            would_exceed_batch = len(current) >= limit
            would_exceed_tokens = current_tokens + tokens > token_ceiling

            if current and (would_exceed_batch or would_exceed_tokens):
                batches.append(current)
                current = []
                current_tokens = 0

            current.append(text)
            current_tokens += tokens

        if current:
            batches.append(current)

        self._requests.extend(batches)
        vectors = [self._vector_for(text) for text in texts]
        return tuple(vectors)

    def _estimate_tokens(self, text: str) -> int:
        return self.token_pad + self.tokens_per_char * len(text)

    def _vector_for(self, text: str) -> tuple[float, ...]:
        base = float(sum(ord(char) for char in text) or 1)
        return tuple(
            (base + index) / (self.dim + index + 1)
            for index in range(self.dim)
        )


@pytest.fixture()
def provider() -> _RecordingStubProvider:
    return _RecordingStubProvider()


def test_stub_provider_returns_dimensional_vectors(
    provider: _RecordingStubProvider,
) -> None:
    model = provider.describe_model(provider.model_name)
    assert model.dim == provider.dim

    options = EmbedRequestOptions(max_batch_size=provider.max_batch_size)
    texts = ["alpha", "beta", "gamma"]

    vectors = provider.embed_texts(texts, model=model.name, options=options)

    assert len(vectors) == len(texts)
    assert all(len(vector) == provider.dim for vector in vectors)


def test_stub_provider_batches_by_max_batch_size(
    provider: _RecordingStubProvider,
) -> None:
    options = EmbedRequestOptions(max_batch_size=2)
    texts = ["zero", "one", "two", "three", "four"]

    provider.embed_texts(texts, model=provider.model_name, options=options)

    assert provider.requests == (
        ("zero", "one"),
        ("two", "three"),
        ("four",),
    )


def test_stub_provider_batches_when_token_limit_would_be_exceeded(
    provider: _RecordingStubProvider,
) -> None:
    provider.token_pad = 0
    provider.tokens_per_char = 4
    options = EmbedRequestOptions(max_batch_size=provider.max_batch_size)
    texts = ["mild", "medium", "tiny"]

    provider.embed_texts(texts, model=provider.model_name, options=options)

    assert provider.requests == (
        ("mild",),
        ("medium",),
        ("tiny",),
    )


def test_stub_provider_errors_when_single_input_exceeds_token_limit(
    provider: _RecordingStubProvider,
) -> None:
    provider.tokens_per_char = 16
    options = EmbedRequestOptions(max_batch_size=provider.max_batch_size)

    with pytest.raises(ValueError) as exc_info:
        provider.embed_texts(
            ["oversize"],
            model=provider.model_name,
            options=options,
        )

    assert "input too large" in str(exc_info.value)
