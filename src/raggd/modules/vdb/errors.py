"""Typed error hierarchy for VDB embedding providers."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "VdbProviderError",
    "VdbProviderConfigurationError",
    "VdbProviderRequestError",
    "VdbProviderRetryableError",
    "VdbProviderRateLimitError",
    "VdbProviderRetryExceededError",
    "VdbProviderInputTooLargeError",
    "VdbProviderDimMismatchError",
]


@dataclass(slots=True)
class VdbProviderError(RuntimeError):
    """Base error raised by embedding providers."""

    message: str
    provider: str
    model: str
    request_id: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)


@dataclass(slots=True)
class VdbProviderConfigurationError(VdbProviderError):
    """Raised when the provider configuration is invalid."""


@dataclass(slots=True)
class VdbProviderRequestError(VdbProviderError):
    """Raised for non-retryable request errors."""


@dataclass(slots=True)
class VdbProviderRetryableError(VdbProviderError):
    """Raised for retryable transport or server-side errors."""


@dataclass(slots=True)
class VdbProviderRateLimitError(VdbProviderRetryableError):
    """Raised when the provider returns a rate limiting response."""


@dataclass(slots=True)
class VdbProviderRetryExceededError(VdbProviderError):
    """Raised when retry attempts are exhausted."""

    attempts: int = 0


@dataclass(slots=True)
class VdbProviderInputTooLargeError(VdbProviderError):
    """Raised when a single input exceeds provider token limits."""

    token_count: int | None = None
    limit: int | None = None


@dataclass(slots=True)
class VdbProviderDimMismatchError(VdbProviderError):
    """Raised when the provider returns vectors with unexpected dimension."""

    expected: int | None = None
    actual: int | None = None
