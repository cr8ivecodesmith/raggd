from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Iterable, Sequence

import pytest
from structlog import get_logger

pytest.importorskip("openai")

import httpx  # noqa: E402  (import after skip guard)
from openai import RateLimitError  # noqa: E402  (import after skip guard)

from raggd.modules.vdb.errors import (
    VdbProviderInputTooLargeError,
    VdbProviderRateLimitError,
)
from raggd.modules.vdb.providers import EmbedRequestOptions
from raggd.modules.vdb.providers.openai import OpenAIEmbeddingsProvider


class _FakeEmbeddingsAPI:
    """Stub embeddings API returning scripted responses."""

    def __init__(self, script: Iterable[Sequence[Sequence[float]] | Exception]):
        self._script = list(script)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def create(
        self,
        *,
        model: str,
        input: Sequence[str],
    ) -> SimpleNamespace:
        self.calls.append((model, tuple(input)))
        if not self._script:
            raise AssertionError("unexpected OpenAI call")
        next_item = self._script.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        data = [SimpleNamespace(embedding=list(vector)) for vector in next_item]
        return SimpleNamespace(data=data)


class _FakeOpenAIClient:
    """Container exposing an embeddings API attribute."""

    def __init__(self, script: Iterable[Sequence[Sequence[float]] | Exception]):
        self.embeddings = _FakeEmbeddingsAPI(script)


def _vector(value: float) -> tuple[float, ...]:
    return tuple(value for _ in range(1_536))


def _patch_token_estimator(
    provider: OpenAIEmbeddingsProvider,
    values: Iterable[int],
) -> None:
    iterator = iter(values)

    def _estimate(
        self: OpenAIEmbeddingsProvider,
        *,
        model: str,
        text: str,
    ) -> int:
        try:
            return next(iterator)
        except StopIteration:
            return 1

    provider._estimate_tokens = MethodType(_estimate, provider)


def test_openai_provider_returns_embeddings_and_batches() -> None:
    script = [
        (_vector(0.0),),
        (_vector(1.0),),
    ]
    client = _FakeOpenAIClient(script)
    provider = OpenAIEmbeddingsProvider(
        logger=get_logger("test.openai.provider"),
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: 0.0,
    )
    _patch_token_estimator(provider, [1, 1])

    vectors = provider.embed_texts(
        ["alpha", "beta"],
        model="text-embedding-3-small",
        options=EmbedRequestOptions(max_batch_size=1),
    )

    assert vectors == (
        _vector(0.0),
        _vector(1.0),
    )
    assert client.embeddings.calls == [
        ("text-embedding-3-small", ("alpha",)),
        ("text-embedding-3-small", ("beta",)),
    ]


def test_openai_provider_splits_batches_by_token_limit() -> None:
    script = [
        (_vector(0.0),),
        (_vector(1.0), _vector(2.0)),
    ]
    client = _FakeOpenAIClient(script)
    provider = OpenAIEmbeddingsProvider(
        logger=get_logger("test.openai.provider"),
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: 0.0,
    )
    _patch_token_estimator(provider, [5000, 4000, 1000])

    vectors = provider.embed_texts(
        ["alpha", "beta", "gamma"],
        model="text-embedding-3-small",
        options=EmbedRequestOptions(max_batch_size=3),
    )

    assert len(vectors) == 3
    # First two exceed token_limit together so each is its own batch.
    assert client.embeddings.calls == [
        ("text-embedding-3-small", ("alpha",)),
        ("text-embedding-3-small", ("beta", "gamma")),
    ]


def test_openai_provider_errors_when_input_exceeds_token_limit() -> None:
    client = _FakeOpenAIClient([])
    provider = OpenAIEmbeddingsProvider(
        logger=get_logger("test.openai.provider"),
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: 0.0,
    )
    _patch_token_estimator(provider, [10_000])

    with pytest.raises(VdbProviderInputTooLargeError) as exc_info:
        provider.embed_texts(
            ["oversize"],
            model="text-embedding-3-small",
            options=EmbedRequestOptions(max_batch_size=1),
        )

    assert "token limit" in str(exc_info.value).lower()


def test_openai_provider_retries_then_raises_rate_limit() -> None:
    request = httpx.Request("POST", "https://example.com/embeddings")
    response = httpx.Response(status_code=429, request=request)
    script = [
        RateLimitError(message="slow down", response=response, body=None)
        for _ in range(5)
    ]
    client = _FakeOpenAIClient(script)
    provider = OpenAIEmbeddingsProvider(
        logger=get_logger("test.openai.provider"),
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: 0.0,
    )
    _patch_token_estimator(provider, [1])

    with pytest.raises(VdbProviderRateLimitError):
        provider.embed_texts(
            ["alpha"],
            model="text-embedding-3-small",
            options=EmbedRequestOptions(max_batch_size=1),
        )

    assert provider.stats["retries"] >= 4
