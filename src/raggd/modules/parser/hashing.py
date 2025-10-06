"""Hashing helpers for parser invariants."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Iterator

__all__ = [
    "DEFAULT_HASH_ALGORITHM",
    "hash_stream",
    "hash_file",
    "hash_text",
]


DEFAULT_HASH_ALGORITHM = "sha256"
_DELIMITER = b"\x00"


def _normalize_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("hash chunks must be bytes")
        if chunk:
            yield bytes(chunk)


def hash_stream(
    *,
    handler_version: str,
    chunks: Iterable[bytes],
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    extra: Iterable[bytes] = (),
) -> str:
    """Compute a stable hash incorporating the handler version."""

    digest = hashlib.new(algorithm)
    digest.update(handler_version.encode("utf-8"))
    digest.update(_DELIMITER)

    for payload in extra:
        digest.update(payload)
        digest.update(_DELIMITER)

    for chunk in _normalize_chunks(chunks):
        digest.update(chunk)

    return digest.hexdigest()


def hash_file(
    path: Path,
    *,
    handler_version: str,
    chunk_size: int = 1024 * 128,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    extra: Iterable[bytes] = (),
) -> str:
    """Hash a file's contents using streaming IO."""

    with path.open("rb") as stream:
        chunks = iter(lambda: stream.read(chunk_size), b"")
        return hash_stream(
            handler_version=handler_version,
            chunks=chunks,
            algorithm=algorithm,
            extra=extra,
        )


def hash_text(
    text: str,
    *,
    handler_version: str,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    extra: Iterable[str] = (),
) -> str:
    """Hash a text payload after normalizing to UTF-8 bytes."""

    byte_chunks = [text.encode("utf-8")]
    extra_chunks = (value.encode("utf-8") for value in extra)
    return hash_stream(
        handler_version=handler_version,
        chunks=byte_chunks,
        algorithm=algorithm,
        extra=extra_chunks,
    )
