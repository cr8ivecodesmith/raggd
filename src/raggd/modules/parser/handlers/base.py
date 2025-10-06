"""Shared scaffolding for language-specific parser handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from raggd.core.config import (
    AppConfig,
    ParserHandlerSettings,
    ParserModuleSettings,
)
from raggd.core.logging import Logger
from raggd.core.paths import WorkspacePaths

from ..tokenizer import TokenEncoder

__all__ = [
    "HandlerCache",
    "ParseContext",
    "HandlerFile",
    "HandlerSymbol",
    "HandlerChunk",
    "HandlerResult",
    "ParserHandler",
]


@dataclass(slots=True)
class HandlerCache:
    """Mutable cache shared across handlers during a parser run."""

    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, factory: Callable[[], Any]) -> Any:
        """Return a cached value for ``key`` creating it via ``factory``."""

        if key not in self.data:
            self.data[key] = factory()
        return self.data[key]

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``."""

        self.data[key] = value

    def clear(self) -> None:
        """Remove all cached values."""

        self.data.clear()


@dataclass(slots=True)
class ParseContext:
    """Execution context shared with handlers during parsing."""

    source: str
    root: Path
    workspace: WorkspacePaths
    config: AppConfig
    settings: ParserModuleSettings
    token_encoder: TokenEncoder
    logger: Logger
    cache: HandlerCache = field(default_factory=HandlerCache)

    def handler_settings(self, handler: str) -> ParserHandlerSettings:
        """Return per-handler settings for ``handler`` or defaults."""

        return self.settings.handlers.get(handler, ParserHandlerSettings())

    def handler_max_tokens(self, handler: str) -> int | str | None:
        """Return the effective token cap for ``handler``."""

        override = self.handler_settings(handler).max_tokens
        if override is not None:
            return override
        return self.settings.general_max_tokens

    def scoped_logger(self, handler: str) -> Logger:
        """Return a logger bound to ``handler`` for structured context."""

        return self.logger.bind(handler=handler)


@dataclass(frozen=True, slots=True)
class HandlerFile:
    """Metadata describing the file being parsed by a handler."""

    path: Path
    language: str
    encoding: str = "utf-8"
    checksum: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandlerSymbol:
    """Symbol extracted by a handler (e.g., function, class, heading)."""

    symbol_id: str
    name: str
    kind: str
    start_offset: int
    end_offset: int
    docstring: str | None = None
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandlerChunk:
    """Chunk emitted by a handler ready for persistence."""

    chunk_id: str
    text: str
    token_count: int | None
    start_offset: int
    end_offset: int
    part_index: int = 0
    parent_symbol_id: str | None = None
    delegate: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """Normalized structure returned by handlers after parsing."""

    file: HandlerFile
    symbols: tuple[HandlerSymbol, ...] = ()
    chunks: tuple[HandlerChunk, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @classmethod
    def empty(
        cls,
        *,
        file: HandlerFile,
        warnings: tuple[str, ...] | None = None,
        errors: tuple[str, ...] | None = None,
    ) -> "HandlerResult":
        """Return an empty result for ``file`` with optional messages."""

        return cls(
            file=file,
            symbols=(),
            chunks=(),
            warnings=tuple(warnings or ()),
            errors=tuple(errors or ()),
        )


class ParserHandler(Protocol):
    """Protocol that concrete parser handlers must follow."""

    name: str
    version: str
    display_name: str

    def parse(
        self,
        *,
        path: Path,
        context: ParseContext,
    ) -> HandlerResult:
        """Parse ``path`` returning a normalized handler result."""

