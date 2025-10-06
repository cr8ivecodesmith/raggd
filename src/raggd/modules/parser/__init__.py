"""Parser module service surface."""

from __future__ import annotations

from .hashing import (
    DEFAULT_HASH_ALGORITHM,
    hash_file,
    hash_stream,
    hash_text,
)
from .models import (
    ParserManifestState,
    ParserRunMetrics,
    ParserRunRecord,
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
from .service import (
    ParserBatchPlan,
    ParserPlanEntry,
    ParserService,
    ParserModuleDisabledError,
    ParserSourceNotConfiguredError,
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
    "ParserManifestState",
    "ParserRunMetrics",
    "ParserRunRecord",
    "ParserBatchPlan",
    "ParserPlanEntry",
    "ParserService",
    "ParserModuleDisabledError",
    "ParserSourceNotConfiguredError",
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
