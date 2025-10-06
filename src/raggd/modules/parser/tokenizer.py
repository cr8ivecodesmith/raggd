"""Token counting helpers built on top of :mod:`tiktoken`."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

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

    def encode(self, text: str, *, allowed_special: set[str] | None = None) -> list[int]:
        ...


@dataclass(frozen=True, slots=True)
class TokenEncoder:
    """Wrapper exposing convenience helpers for token counting."""

    name: str
    encoding: TokenEncoderProtocol

    def count(self, text: str) -> int:
        """Return the number of tokens required to represent ``text``."""

        tokens = self.encoding.encode(text, allowed_special=set())
        return len(tokens)


class TokenEncoderError(RuntimeError):
    """Raised when a token encoder cannot be loaded."""


@lru_cache(maxsize=8)
def _load_encoding(name: str) -> TokenEncoderProtocol:
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise TokenEncoderError("tiktoken is required for token counting") from exc

    try:
        return tiktoken.get_encoding(name)
    except KeyError as exc:
        raise TokenEncoderError(f"Unknown token encoder: {name}") from exc


def get_token_encoder(name: str = DEFAULT_ENCODER) -> TokenEncoder:
    """Return a cached :class:`TokenEncoder` for ``name``."""

    encoding = _load_encoding(name)
    return TokenEncoder(name=name, encoding=encoding)

