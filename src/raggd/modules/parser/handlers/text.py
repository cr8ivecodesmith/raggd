"""Baseline text handler placeholder."""

from __future__ import annotations

from pathlib import Path

from .base import HandlerFile, HandlerResult, ParseContext, ParserHandler


class TextHandler(ParserHandler):
    """Minimal text handler placeholder until Phase 4 fleshes it out."""

    name = "text"
    version = "1.0.0"
    display_name = "Plain Text"

    def __init__(self, *, context: ParseContext) -> None:
        self._context = context

    def parse(
        self,
        *,
        path: Path,
        context: ParseContext,
    ) -> HandlerResult:
        file_meta = HandlerFile(
            path=path,
            language=self.name,
            encoding="utf-8",
        )
        return HandlerResult.empty(file=file_meta)
