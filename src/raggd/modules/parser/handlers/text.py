"""Plain text handler implementing paragraph and indentation heuristics."""

from __future__ import annotations

from bisect import bisect_right
import hashlib
import re
from pathlib import Path
from typing import Iterable, Sequence

from .base import HandlerChunk, HandlerFile, HandlerResult, ParseContext, ParserHandler


class TextHandler(ParserHandler):
    """Parse plain text files into paragraph-aligned chunks."""

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
        logger = context.scoped_logger(self.name)

        try:
            raw = path.read_bytes()
        except OSError as exc:  # pragma: no cover - filesystem failure edge
            logger.error("Failed to read text file", path=str(path), error=str(exc))
            file_meta = HandlerFile(
                path=path,
                language=self.name,
                encoding="utf-8",
            )
            return HandlerResult.empty(
                file=file_meta,
                errors=(f"Failed to read file: {exc}",),
            )

        checksum = hashlib.sha256(raw).hexdigest()

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning(
                "UTF-8 decode error, skipping text handler", path=str(path), error=str(exc)
            )
            file_meta = HandlerFile(
                path=path,
                language=self.name,
                encoding="utf-8",
                checksum=checksum,
            )
            return HandlerResult.empty(
                file=file_meta,
                errors=(
                    "File is not valid UTF-8; install a specialized handler or re-encode",
                ),
            )

        file_meta = HandlerFile(
            path=path,
            language=self.name,
            encoding="utf-8",
            checksum=checksum,
            metadata={"size_bytes": len(raw), "line_count": text.count("\n") + 1},
        )

        if not text:
            return HandlerResult(file=file_meta)

        spans, strategy = self._compute_spans(text)

        if not spans:
            # Collapse to a single chunk when heuristics find nothing useful.
            spans = ((0, len(text)),)
            strategy = "fallback"

        line_starts = self._line_starts(text)
        byte_offsets = self._byte_offsets(text)

        chunks: list[HandlerChunk] = []
        for index, (start, end) in enumerate(spans):
            segment = text[start:end]
            if not segment.strip():
                continue

            start_byte = byte_offsets[start]
            end_byte = byte_offsets[end]

            start_line = self._line_for_offset(line_starts, start)
            end_offset = max(start, end - 1)
            end_line = self._line_for_offset(line_starts, end_offset)

            token_count = context.token_encoder.count(segment)

            chunk = HandlerChunk(
                chunk_id=f"{self.name}:{start_byte}:{end_byte}",
                text=segment,
                token_count=token_count,
                start_offset=start_byte,
                end_offset=end_byte,
                part_index=index,
                metadata={
                    "strategy": strategy,
                    "start_line": start_line,
                    "end_line": end_line,
                    "char_start": start,
                    "char_end": end,
                },
            )
            chunks.append(chunk)

        if not chunks:
            # Ensure at least one chunk representing the entire file to avoid
            # losing data when the file contains only whitespace.
            token_count = context.token_encoder.count(text)
            chunks.append(
                HandlerChunk(
                    chunk_id=f"{self.name}:0:{byte_offsets[-1]}",
                    text=text,
                    token_count=token_count,
                    start_offset=0,
                    end_offset=byte_offsets[-1],
                    part_index=0,
                    metadata={
                        "strategy": strategy,
                        "start_line": 1,
                        "end_line": self._line_for_offset(line_starts, len(text) - 1)
                        if text
                        else 1,
                        "char_start": 0,
                        "char_end": len(text),
                    },
                )
            )

        return HandlerResult(
            file=file_meta,
            symbols=(),
            chunks=tuple(chunks),
        )

    @staticmethod
    def _compute_spans(text: str) -> tuple[Sequence[tuple[int, int]], str]:
        """Return chunk spans and the heuristic strategy used."""

        paragraph_spans = tuple(TextHandler._paragraph_spans(text))
        if len(paragraph_spans) > 1:
            return paragraph_spans, "paragraph"

        indent_spans = tuple(TextHandler._indentation_spans(text))
        if len(indent_spans) > 1:
            return indent_spans, "indentation"

        if paragraph_spans:
            only = paragraph_spans[0]
            if only != (0, len(text)):
                return paragraph_spans, "paragraph"

        if indent_spans:
            only = indent_spans[0]
            if only != (0, len(text)):
                return indent_spans, "indentation"

        return (), "fallback"

    @staticmethod
    def _paragraph_spans(text: str) -> Iterable[tuple[int, int]]:
        pattern = re.compile(r"\n\s*\n", re.MULTILINE)
        last = 0
        for match in pattern.finditer(text):
            start = last
            end = match.end()
            segment = text[start:match.start()]
            if segment.strip():
                yield (start, end)
            last = match.end()
        if last < len(text):
            segment = text[last:]
            if segment.strip():
                yield (last, len(text))

    @staticmethod
    def _indentation_spans(text: str) -> Iterable[tuple[int, int]]:
        lines = text.splitlines(keepends=True)
        if not lines:
            return ()

        non_blank = [line for line in lines if line.strip()]
        if not non_blank:
            return ()

        base_indent = min(TextHandler._indent_width(line) for line in non_blank)
        if base_indent != 0:
            return ()

        offsets = TextHandler._line_offsets(lines)
        spans: list[tuple[int, int]] = []
        current_start = 0
        have_block = False
        previous_end = 0

        for index, line in enumerate(lines):
            line_start = offsets[index]
            line_end = offsets[index + 1]
            if not line.strip():
                previous_end = line_end
                continue

            indent = TextHandler._indent_width(line)

            if not have_block:
                current_start = 0
                have_block = True
            elif indent == base_indent and line_start != current_start:
                spans.append((current_start, previous_end))
                current_start = line_start

            previous_end = line_end

        if have_block:
            spans.append((current_start, len(text)))

        return tuple(span for span in spans if text[span[0] : span[1]].strip())

    @staticmethod
    def _indent_width(line: str) -> int:
        stripped = line.lstrip(" \t")
        return len(line) - len(stripped)

    @staticmethod
    def _line_offsets(lines: Sequence[str]) -> Sequence[int]:
        offsets = [0]
        total = 0
        for line in lines:
            total += len(line)
            offsets.append(total)
        return offsets

    @staticmethod
    def _line_starts(text: str) -> Sequence[int]:
        starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                starts.append(index + 1)
        return starts

    @staticmethod
    def _line_for_offset(starts: Sequence[int], offset: int) -> int:
        if not starts:
            return 1
        return bisect_right(starts, offset) or 1

    @staticmethod
    def _byte_offsets(text: str) -> Sequence[int]:
        offsets = [0]
        total = 0
        for char in text:
            total += len(char.encode("utf-8"))
            offsets.append(total)
        return offsets
