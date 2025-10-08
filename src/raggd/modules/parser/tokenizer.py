"""Token counting helpers with graceful fallback when :mod:`tiktoken` is absent."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from typing import Protocol

from raggd.core.logging import get_logger

try:  # pragma: no cover - exercised through fallback tests
    import tiktoken  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dependency guard
    tiktoken = None  # type: ignore[assignment]

DEFAULT_ENCODER = "cl100k_base"

__all__ = [
    "TokenEncoder",
    "TokenEncoderProtocol",
    "TokenEncoderError",
    "DEFAULT_ENCODER",
    "get_token_encoder",
]


class TokenEncoderProtocol(Protocol):
    """Lightweight protocol covering the subset used by handlers."""

    def encode(
        self, text: str, *, allowed_special: set[str] | None = None
    ) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class TokenEncoder:
    """Wrapper exposing convenience helpers for token counting."""

    name: str
    encoding: TokenEncoderProtocol

    def count(self, text: str) -> int:
        """Return the number of tokens required to represent ``text``."""

        if hasattr(self.encoding, "count"):
            count = self.encoding.count(text)  # type: ignore[attr-defined]
            return int(count)
        tokens = self.encoding.encode(text, allowed_special=set())
        return len(tokens)


class TokenEncoderError(RuntimeError):
    """Raised when a token encoder cannot be loaded."""


_LOGGER = get_logger(__name__, component="parser-tokenizer")
_FALLBACK_NOTICE_EMITTED = False


class _ApproximateEncoding:
    """Approximate encoder used when `tiktoken` is unavailable."""

    def __init__(self, *, name: str, characters_per_token: int = 4) -> None:
        self.name = name
        self._characters_per_token = max(1, characters_per_token)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, ceil(len(text) / self._characters_per_token))

    def encode(
        self, text: str, *, allowed_special: set[str] | None = None
    ) -> list[int]:
        count = self.count(text)
        return [0] * count


def _emit_fallback_notice(name: str) -> None:
    global _FALLBACK_NOTICE_EMITTED
    if _FALLBACK_NOTICE_EMITTED:
        return
    _LOGGER.warning(
        "token-encoder-fallback",
        requested=name,
        reason="tiktoken-missing",
    )
    _FALLBACK_NOTICE_EMITTED = True


def _build_fallback_encoding(name: str) -> TokenEncoderProtocol:
    _emit_fallback_notice(name)
    return _ApproximateEncoding(name=name)


@lru_cache(maxsize=8)
def _load_encoding(name: str) -> TokenEncoderProtocol:
    if tiktoken is None:
        if name != DEFAULT_ENCODER:
            raise TokenEncoderError(
                f"Token encoder {name!r} unavailable without tiktoken"
            )
        return _build_fallback_encoding(name)
    try:
        return tiktoken.get_encoding(name)
    except KeyError as exc:
        raise TokenEncoderError(f"Unknown token encoder: {name}") from exc


def get_token_encoder(name: str = DEFAULT_ENCODER) -> TokenEncoder:
    """Return a cached :class:`TokenEncoder` for ``name``."""

    encoding = _load_encoding(name)
    return TokenEncoder(name=name, encoding=encoding)
