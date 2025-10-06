"""Parser module service surface."""

from __future__ import annotations

from .hashing import (
    DEFAULT_HASH_ALGORITHM,
    hash_file,
    hash_stream,
    hash_text,
)
from .registry import (
    HandlerAvailability,
    HandlerProbe,
    HandlerProbeResult,
    HandlerRegistry,
    HandlerSelection,
    ParserHandlerDescriptor,
    build_default_registry,
)
from .tokenizer import (
    DEFAULT_ENCODER,
    TokenEncoder,
    TokenEncoderError,
    get_token_encoder,
)
from .traversal import (
    TraversalResult,
    TraversalScope,
    TraversalService,
)

__all__ = [
    "DEFAULT_ENCODER",
    "DEFAULT_HASH_ALGORITHM",
    "HandlerAvailability",
    "HandlerProbe",
    "HandlerProbeResult",
    "HandlerRegistry",
    "HandlerSelection",
    "ParserHandlerDescriptor",
    "TokenEncoder",
    "TokenEncoderError",
    "TraversalResult",
    "TraversalScope",
    "TraversalService",
    "build_default_registry",
    "get_token_encoder",
    "hash_file",
    "hash_stream",
    "hash_text",
]
