"""OpenAI embeddings provider implementation."""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

try:  # pragma: no cover - optional dependency guard
    import tiktoken
except ImportError:  # pragma: no cover - extras should provide this
    tiktoken = None  # type: ignore[assignment]

from raggd.core.logging import Logger
from raggd.modules.vdb.errors import (
    VdbProviderConfigurationError,
    VdbProviderDimMismatchError,
    VdbProviderInputTooLargeError,
    VdbProviderRateLimitError,
    VdbProviderRequestError,
    VdbProviderRetryExceededError,
    VdbProviderRetryableError,
)

from . import (
    DEFAULT_AUTO_CONCURRENCY,
    EmbedRequestOptions,
    EmbeddingMatrix,
    EmbeddingProviderCaps,
    EmbeddingProviderModel,
    EmbeddingVector,
    EmbeddingsProvider,
    ProviderInitContext,
)

__all__ = [
    "OpenAIEmbeddingsProvider",
    "openai_provider_factory",
]

_DEFAULT_TIMEOUT = 30.0
_TOKEN_PAD = 8
_BACKOFF_BASE = 0.5
_BACKOFF_MULTIPLIER = 2.0
_BACKOFF_CAP = 8.0
_JITTER_RATIO = 0.2
_MAX_ATTEMPTS = 5
_DIMENSION_PROBE_TEXT = "__RAGGD_DIMENSION_PROBE__"


@dataclass(frozen=True, slots=True)
class _OpenAIModelMetadata:
    name: str
    dim: int | None
    max_batch_size: int
    max_parallel_requests: int
    max_request_tokens: int
    max_input_tokens: int | None = None
    tokenizer: str | None = None


_OPENAI_MODELS: Mapping[str, _OpenAIModelMetadata] = {
    "text-embedding-3-small": _OpenAIModelMetadata(
        name="text-embedding-3-small",
        dim=1536,
        max_batch_size=128,
        max_parallel_requests=4,
        max_request_tokens=8_191,
        max_input_tokens=8_191,
        tokenizer="text-embedding-3-small",
    ),
    "text-embedding-3-large": _OpenAIModelMetadata(
        name="text-embedding-3-large",
        dim=3_072,
        max_batch_size=64,
        max_parallel_requests=4,
        max_request_tokens=8_191,
        max_input_tokens=8_191,
        tokenizer="text-embedding-3-large",
    ),
    "text-embedding-ada-002": _OpenAIModelMetadata(
        name="text-embedding-ada-002",
        dim=1_536,
        max_batch_size=128,
        max_parallel_requests=4,
        max_request_tokens=8_191,
        max_input_tokens=8_191,
        tokenizer="text-embedding-ada-002",
    ),
}


def _normalize_model_name(model: str) -> str:
    normalized = model.strip()
    if not normalized:
        raise ValueError("model cannot be blank")
    return normalized


def _resolve_timeout(
    config: Mapping[str, object] | None,
) -> float | httpx.Timeout:
    raw_env = os.environ.get("OPENAI_TIMEOUT_SECONDS")
    raw_config = None
    if config:
        candidate = config.get("timeout")
        if isinstance(candidate, (float, int)):
            raw_config = float(candidate)
    value = raw_env or raw_config
    if value is None:
        return _DEFAULT_TIMEOUT
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "OPENAI_TIMEOUT_SECONDS must be a number when provided.",
        ) from exc
    if parsed <= 0:
        raise ValueError("OPENAI_TIMEOUT_SECONDS must be positive.")
    return parsed


class OpenAIEmbeddingsProvider(EmbeddingsProvider):
    """Embed texts via the OpenAI embeddings API."""

    def __init__(
        self,
        *,
        logger: Logger,
        config: Mapping[str, object] | None = None,
        client: OpenAI | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.logger = logger
        self._config = dict(config or {})
        self._sleep = sleep
        self._now = now
        self._token_cache: dict[tuple[str, int], int] = {}
        self._dim_cache: dict[str, int] = {}
        self._stats = {"requests": 0, "retries": 0, "failures": 0}
        self._client = client or self._build_client()

    def _configured_max_input_tokens(self) -> int | None:
        value = self._config.get("max_input_tokens")
        if isinstance(value, int):
            if value < 1:
                return None
            return value
        return None

    @property
    def stats(self) -> Mapping[str, int]:
        """Return counters captured during the provider lifetime."""

        return dict(self._stats)

    # ------------------------------------------------------------------#
    # Provider interface
    # ------------------------------------------------------------------#
    def describe_model(self, model: str) -> EmbeddingProviderModel:
        name = _normalize_model_name(model)
        metadata = _OPENAI_MODELS.get(name)

        if metadata and metadata.dim is not None:
            return EmbeddingProviderModel(
                provider="openai",
                name=metadata.name,
                dim=metadata.dim,
            )

        cached = self._dim_cache.get(name)
        if cached is not None:
            return EmbeddingProviderModel(
                provider="openai",
                name=name,
                dim=cached,
            )

        dimension = self._probe_dimension(model=name)
        return EmbeddingProviderModel(
            provider="openai",
            name=name,
            dim=dimension,
        )

    def capabilities(
        self,
        *,
        model: str | None = None,
    ) -> EmbeddingProviderCaps:
        if model is None:
            batch_ceiling = max(
                metadata.max_batch_size for metadata in _OPENAI_MODELS.values()
            )
            parallel_ceiling = max(
                metadata.max_parallel_requests
                for metadata in _OPENAI_MODELS.values()
            )
            token_ceiling = max(
                metadata.max_request_tokens
                for metadata in _OPENAI_MODELS.values()
            )
            config_limit = self._configured_max_input_tokens()
            if config_limit is not None:
                token_ceiling = min(token_ceiling, config_limit)
            return EmbeddingProviderCaps(
                max_batch_size=batch_ceiling,
                max_parallel_requests=parallel_ceiling,
                max_request_tokens=token_ceiling,
                max_input_tokens=config_limit,
            )

        name = _normalize_model_name(model)
        metadata = _OPENAI_MODELS.get(name)
        if metadata is None:
            return EmbeddingProviderCaps(
                max_batch_size=128,
                max_parallel_requests=DEFAULT_AUTO_CONCURRENCY,
                max_request_tokens=8_191,
            )

        config_limit = self._configured_max_input_tokens()
        max_request_tokens = metadata.max_request_tokens
        max_input_tokens = metadata.max_input_tokens
        if config_limit is not None:
            if max_request_tokens is None:
                max_request_tokens = config_limit
            else:
                max_request_tokens = min(max_request_tokens, config_limit)
            if max_input_tokens is None:
                max_input_tokens = config_limit
            else:
                max_input_tokens = min(max_input_tokens, config_limit)

        return EmbeddingProviderCaps(
            max_batch_size=metadata.max_batch_size,
            max_parallel_requests=metadata.max_parallel_requests,
            max_request_tokens=max_request_tokens,
            max_input_tokens=max_input_tokens,
        )

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str,
        options: EmbedRequestOptions,
    ) -> EmbeddingMatrix:
        if not texts:
            return ()

        name = _normalize_model_name(model)
        caps = self.capabilities(model=name)
        limit = min(options.max_batch_size, caps.max_batch_size)
        request_limit = (
            caps.max_request_tokens or caps.max_input_tokens or 8_191
        )
        if options.max_input_tokens is not None:
            request_limit = min(request_limit, options.max_input_tokens)

        normalized_texts = [self._normalize_text(text) for text in texts]
        token_counts = [
            self._estimate_tokens(model=name, text=normalized)
            for normalized in normalized_texts
        ]

        batches = self._chunk_batches(
            normalized_texts,
            token_counts,
            limit=limit,
            token_limit=request_limit,
            model=name,
        )

        results: list[EmbeddingVector] = []
        for batch in batches:
            embeddings = self._invoke_with_retries(
                model=name,
                batch=batch.texts,
                token_count=batch.tokens,
            )
            for vector in embeddings:
                dimension = len(vector)
                metadata = _OPENAI_MODELS.get(name)
                expected = metadata.dim if metadata else None
                if expected is not None and dimension != expected:
                    raise VdbProviderDimMismatchError(
                        "Embedding dimension mismatch in OpenAI response.",
                        provider="openai",
                        model=name,
                        expected=expected,
                        actual=dimension,
                    )
            results.extend(
                tuple(float(value) for value in vector) for vector in embeddings
            )

        return tuple(results)

    # ------------------------------------------------------------------#
    # Internal helpers
    # ------------------------------------------------------------------#
    def _build_client(self) -> OpenAI:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise VdbProviderConfigurationError(
                "OPENAI_API_KEY must be set to use the OpenAI provider.",
                provider="openai",
                model="*",
            )

        base_url = os.environ.get("OPENAI_BASE_URL")
        org_id = os.environ.get("OPENAI_ORG_ID")
        timeout = _resolve_timeout(self._config)

        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=org_id,
            timeout=timeout,
        )

    @dataclass(slots=True)
    class _Batch:
        texts: tuple[str, ...]
        tokens: int

    def _chunk_batches(
        self,
        texts: Sequence[str],
        token_counts: Sequence[int],
        *,
        limit: int,
        token_limit: int,
        model: str,
    ) -> tuple[_Batch, ...]:
        batches: list[OpenAIEmbeddingsProvider._Batch] = []
        current: list[str] = []
        current_tokens = 0

        for text, tokens in zip(texts, token_counts):
            if tokens > token_limit:
                raise VdbProviderInputTooLargeError(
                    (
                        "Input text exceeds OpenAI token limit "
                        f"({tokens} > {token_limit})."
                    ),
                    provider="openai",
                    model=model,
                    token_count=tokens,
                    limit=token_limit,
                )

            would_exceed_batch = len(current) >= limit
            would_exceed_tokens = current_tokens + tokens > token_limit

            if current and (would_exceed_batch or would_exceed_tokens):
                batches.append(
                    OpenAIEmbeddingsProvider._Batch(
                        texts=tuple(current),
                        tokens=current_tokens,
                    )
                )
                current = []
                current_tokens = 0

            current.append(text)
            current_tokens += tokens

        if current:
            batches.append(
                OpenAIEmbeddingsProvider._Batch(
                    texts=tuple(current),
                    tokens=current_tokens,
                )
            )

        return tuple(batches)

    def _probe_dimension(self, *, model: str) -> int | None:
        batch = (_DIMENSION_PROBE_TEXT,)
        embeddings = self._invoke_with_retries(
            model=model,
            batch=batch,
            token_count=self._estimate_tokens(model=model, text=batch[0]),
            is_probe=True,
        )
        if not embeddings:
            return None
        dimension = len(embeddings[0])
        self._dim_cache[model] = dimension
        return dimension

    def _estimate_tokens(self, *, model: str, text: str) -> int:
        key = (model, len(text))
        cached = self._token_cache.get(key)
        if cached is not None:
            return cached

        if tiktoken is None:  # pragma: no cover - extras should be present
            estimate = _TOKEN_PAD + len(text) * 4
        else:
            try:
                encoding = tiktoken.encoding_for_model(model)
            except Exception:
                encoding = tiktoken.get_encoding("cl100k_base")
            try:
                estimate = _TOKEN_PAD + len(encoding.encode(text))
            except Exception:
                self.logger.warning(
                    "openai-token-estimate-fallback",
                    provider="openai",
                    model=model,
                )
                estimate = _TOKEN_PAD + len(text) * 4

        self._token_cache[key] = estimate
        return estimate

    def _normalize_text(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.strip()

    def _invoke_with_retries(
        self,
        *,
        model: str,
        batch: Sequence[str],
        token_count: int,
        is_probe: bool = False,
    ) -> list[list[float]]:
        attempts = 0
        jitter_source = random.Random()

        while attempts < _MAX_ATTEMPTS:
            attempts += 1
            start = self._now()
            try:
                response = self._client.embeddings.create(
                    model=model,
                    input=list(batch),
                )
                elapsed = self._now() - start
                self._stats["requests"] += 1
                self.logger.info(
                    "openai-embed-request",
                    provider="openai",
                    model=model,
                    batch_size=len(batch),
                    token_count=token_count,
                    latency=elapsed,
                    attempts=attempts,
                    is_probe=is_probe,
                    recovered=attempts > 1,
                )
                return [list(item.embedding) for item in response.data]
            except Exception as exc:  # pragma: no branch - handled below
                retryable = self._is_retryable(exc)
                status, request_id = self._extract_context(exc)
                should_retry = retryable and attempts < _MAX_ATTEMPTS
                if not should_retry:
                    self._stats["failures"] += 1
                    error = self._translate_exception(
                        exc,
                        attempts=attempts,
                        provider="openai",
                        model=model,
                        status=status,
                        request_id=request_id,
                    )
                    raise error from exc

                delay = self._compute_backoff(
                    attempt=attempts,
                    rng=jitter_source,
                )
                self.logger.warning(
                    "openai-embed-retry",
                    provider="openai",
                    model=model,
                    attempt=attempts,
                    max_attempts=_MAX_ATTEMPTS,
                    retry_delay=delay,
                    error_type=exc.__class__.__name__,
                    status_code=status,
                    request_id=request_id,
                    recovered=False,
                    is_probe=is_probe,
                )
                self._stats["retries"] += 1
                self._sleep(delay)

        raise VdbProviderRetryExceededError(
            "Failed to embed texts after multiple attempts.",
            provider="openai",
            model=model,
            attempts=attempts,
        )

    def _compute_backoff(
        self,
        *,
        attempt: int,
        rng: random.Random,
    ) -> float:
        if attempt <= 1:
            return 0.0
        base = _BACKOFF_BASE * (_BACKOFF_MULTIPLIER ** (attempt - 2))
        base = min(base, _BACKOFF_CAP)
        jitter = 1.0 + rng.uniform(-_JITTER_RATIO, _JITTER_RATIO)
        delay = base * jitter
        return round(delay, 2)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                RateLimitError,
                APITimeoutError,
                APIConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
            ),
        ):
            return True
        if isinstance(exc, APIStatusError):
            status, _ = OpenAIEmbeddingsProvider._extract_context(exc)
            return status is not None and status >= 500
        return False

    @staticmethod
    def _extract_context(exc: Exception) -> tuple[int | None, str | None]:
        status: int | None = None
        request_id: str | None = None

        if isinstance(exc, APIStatusError):
            value = getattr(exc, "status", None)
            if isinstance(value, int):
                status = value
        if hasattr(exc, "status_code"):
            try:
                status_value = getattr(exc, "status_code")
                if status_value is not None:
                    status = int(status_value)
            except (TypeError, ValueError):
                pass

        if hasattr(exc, "request_id"):
            value = getattr(exc, "request_id")
            if isinstance(value, str):
                request_id = value

        return status, request_id

    @staticmethod
    def _translate_exception(
        exc: Exception,
        *,
        attempts: int,
        provider: str,
        model: str,
        status: int | None,
        request_id: str | None,
    ) -> VdbProviderRequestError:
        message = str(exc) or exc.__class__.__name__
        if isinstance(exc, RateLimitError):
            return VdbProviderRateLimitError(
                message,
                provider=provider,
                model=model,
                status_code=status,
                request_id=request_id,
            )
        if isinstance(
            exc,
            (APITimeoutError, APIConnectionError, httpx.HTTPError),
        ):
            return VdbProviderRetryableError(
                message,
                provider=provider,
                model=model,
                status_code=status,
                request_id=request_id,
            )
        if isinstance(exc, APIStatusError) and status and status >= 500:
            return VdbProviderRetryableError(
                message,
                provider=provider,
                model=model,
                status_code=status,
                request_id=request_id,
            )
        if attempts >= _MAX_ATTEMPTS:
            summary = (
                "Exceeded retry attempts when calling OpenAI embeddings API."
            )
            error = VdbProviderRetryExceededError(
                summary,
                provider=provider,
                model=model,
                status_code=status,
                request_id=request_id,
                attempts=attempts,
            )
            return error
        if isinstance(exc, BadRequestError):
            return VdbProviderRequestError(
                message,
                provider=provider,
                model=model,
                status_code=status,
                request_id=request_id,
            )
        return VdbProviderRequestError(
            message,
            provider=provider,
            model=model,
            status_code=status,
            request_id=request_id,
        )


def openai_provider_factory(
    context: ProviderInitContext,
) -> OpenAIEmbeddingsProvider:
    """Factory registered with the provider registry."""

    return OpenAIEmbeddingsProvider(
        logger=context.logger,
        config=context.config,
    )
