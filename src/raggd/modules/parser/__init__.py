"""Parser module service surface."""

from __future__ import annotations

from .artifacts import ChunkSlice
from .hashing import (
    DEFAULT_HASH_ALGORITHM,
    hash_file,
    hash_stream,
    hash_text,
)
from .handlers import (
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSymbol,
    ParseContext,
    ParserHandler,
    ParserHandlerFactory,
)
from .models import (
    ParserManifestState,
    ParserRunMetrics,
    ParserRunRecord,
)
from .registry import (
    HandlerAvailability,
    HandlerFactoryError,
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
from .recomposition import (
    ChunkRecomposer,
    ChunkSlicePart,
    RecomposedChunk,
    recompose_chunk_slices,
)
from .traversal import (
    TraversalResult,
    TraversalScope,
    TraversalService,
)


from .staging import (
    FileStageOutcome,
    ParserPersistenceTransaction,
    parser_transaction,
)
__all__ = [
    "DEFAULT_ENCODER",
    "DEFAULT_HASH_ALGORITHM",
    "ChunkSlice",
    "HandlerChunk",
    "HandlerAvailability",
    "HandlerFile",
    "HandlerProbe",
    "HandlerProbeResult",
    "HandlerRegistry",
    "HandlerSelection",
    "HandlerResult",
    "HandlerSymbol",
    "ParseContext",
    "ParserHandlerDescriptor",
    "ParserHandler",
    "ParserHandlerFactory",
    "HandlerFactoryError",
    "ChunkRecomposer",
    "ChunkSlicePart",
    "RecomposedChunk",
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
    "recompose_chunk_slices",
    "ParserPersistenceTransaction",
    "FileStageOutcome",
    "parser_transaction",
    "hash_file",
    "hash_stream",
    "hash_text",
]
