"""Markdown handler implementing heading-aware chunking."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Iterable, Sequence

from .base import (
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSymbol,
    ParseContext,
    ParserHandler,
)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FENCE_RE = re.compile(
    r"```(?P<info>[^\n`]*)\n(?P<body>.*?)(?:\r?\n```[ \t]*\r?\n?|```[ \t]*$)",
    re.DOTALL,
)


@dataclass(slots=True)
class _Heading:
    level: int
    title: str
    heading_start: int
    heading_end: int


@dataclass(slots=True)
class _Section:
    heading: _Heading
    section_start: int
    section_end: int
    intro_attached: bool


@dataclass(slots=True)
class _FenceBlock:
    language: str | None
    info: str | None
    code: str
    code_start: int
    code_end: int
    fence_start: int
    fence_end: int


class MarkdownHandler(ParserHandler):
    """Parse Markdown documents into heading-scoped chunks."""

    name = "markdown"
    version = "1.0.0"
    display_name = "Markdown"

    def __init__(self, *, context: ParseContext) -> None:
        self._context = context
        self._parser = self._load_tree_sitter_parser(context)

    @staticmethod
    def _load_tree_sitter_parser(context: ParseContext):
        """Return a cached tree-sitter parser when available."""

        cache_key = "markdown.tree_sitter_parser"

        def _factory():
            try:  # pragma: no cover - optional dependency branch
                from tree_sitter_languages import get_parser
            except Exception:  # pragma: no cover - dependency missing
                return None
            try:
                return get_parser("markdown")
            except Exception:  # pragma: no cover - parser construction failure
                return None

        return context.cache.get(cache_key, _factory)

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
            logger.error("Failed to read markdown file", path=str(path), error=str(exc))
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
                "UTF-8 decode error, skipping markdown handler",
                path=str(path),
                error=str(exc),
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
                    "File is not valid UTF-8; configure a specialized handler or re-encode",
                ),
            )

        file_meta = HandlerFile(
            path=path,
            language=self.name,
            encoding="utf-8",
            checksum=checksum,
            metadata={
                "size_bytes": len(raw),
                "line_count": text.count("\n") + 1,
            },
        )

        if not text:
            return HandlerResult(file=file_meta)

        front_matter, body_start = self._extract_front_matter(text)
        if front_matter is not None:
            meta = dict(file_meta.metadata)
            meta["front_matter"] = front_matter
            file_meta = HandlerFile(
                path=file_meta.path,
                language=file_meta.language,
                encoding=file_meta.encoding,
                checksum=file_meta.checksum,
                metadata=meta,
            )

        byte_offsets = self._byte_offsets(text)
        line_starts = self._line_starts(text)

        warnings: list[str] = []
        if self._parser is not None:
            parse_ok, parse_warning = self._verify_with_tree_sitter(text)
            if not parse_ok and parse_warning:
                warnings.append(parse_warning)

        sections = self._build_sections(text, body_start=body_start)

        chunks: list[HandlerChunk] = []
        symbols: list[HandlerSymbol] = []
        part_index = 0

        if front_matter is not None:
            fm_end_char = body_start
            start_line = 1
            end_line = self._line_for_offset(line_starts, fm_end_char - 1) if fm_end_char else 1
            chunk = HandlerChunk(
                chunk_id=f"{self.name}:front-matter",
                text=front_matter,
                token_count=context.token_encoder.count(front_matter),
                start_offset=0,
                end_offset=byte_offsets[fm_end_char],
                part_index=part_index,
                metadata={
                    "kind": "front_matter",
                    "start_line": start_line,
                    "end_line": end_line,
                    "char_start": 0,
                    "char_end": fm_end_char,
                },
            )
            chunks.append(chunk)
            part_index += 1

        if not sections:
            body = text[body_start:]
            if body:
                start_byte = byte_offsets[body_start]
                end_byte = byte_offsets[len(text)]
                start_line = self._line_for_offset(line_starts, body_start)
                end_line = self._line_for_offset(line_starts, len(text) - 1)
                chunk = HandlerChunk(
                    chunk_id=f"{self.name}:body:{start_byte}:{end_byte}",
                    text=body,
                    token_count=context.token_encoder.count(body),
                    start_offset=start_byte,
                    end_offset=end_byte,
                    part_index=part_index,
                    metadata={
                        "kind": "body",
                        "strategy": "fallback",
                        "start_line": start_line,
                        "end_line": end_line,
                        "char_start": body_start,
                        "char_end": len(text),
                    },
                )
                chunks.append(chunk)
            return HandlerResult(
                file=file_meta,
                symbols=(),
                chunks=tuple(chunks),
                warnings=tuple(warnings),
            )

        symbol_stack: list[tuple[int, str]] = []
        for index, section in enumerate(sections):
            heading = section.heading
            section_text = text[section.section_start : section.section_end]
            token_count = context.token_encoder.count(section_text)

            heading_byte_start = byte_offsets[heading.heading_start]
            heading_byte_end = byte_offsets[heading.heading_end]
            section_byte_start = byte_offsets[section.section_start]
            section_byte_end = byte_offsets[section.section_end]

            heading_line = self._line_for_offset(line_starts, heading.heading_start)
            section_end_line = self._line_for_offset(line_starts, section.section_end - 1)

            symbol_id = f"{self.name}:heading:{heading_byte_start}"

            while symbol_stack and symbol_stack[-1][0] >= heading.level:
                symbol_stack.pop()
            parent_id = symbol_stack[-1][1] if symbol_stack else None
            symbol_stack.append((heading.level, symbol_id))

            symbols.append(
                HandlerSymbol(
                    symbol_id=symbol_id,
                    name=heading.title,
                    kind="heading",
                    start_offset=heading_byte_start,
                    end_offset=heading_byte_end,
                    parent_id=parent_id,
                    metadata={
                        "level": heading.level,
                        "line": heading_line,
                    },
                )
            )

            chunk_metadata = {
                "kind": "section",
                "heading_title": heading.title,
                "heading_level": heading.level,
                "heading_line": heading_line,
                "start_line": self._line_for_offset(line_starts, section.section_start),
                "end_line": section_end_line,
                "char_start": section.section_start,
                "char_end": section.section_end,
            }
            if section.intro_attached:
                chunk_metadata["intro_attached"] = True

            chunks.append(
                HandlerChunk(
                    chunk_id=f"{self.name}:section:{section_byte_start}:{section_byte_end}",
                    text=section_text,
                    token_count=token_count,
                    start_offset=section_byte_start,
                    end_offset=section_byte_end,
                    part_index=part_index,
                    parent_symbol_id=symbol_id,
                    metadata=chunk_metadata,
                )
            )
            part_index += 1

            for fence_index, fence in enumerate(
                self._extract_fences(section_text, base_char=section.section_start)
            ):
                if not fence.code.strip():
                    continue
                code_start_byte = byte_offsets[fence.code_start]
                code_end_byte = byte_offsets[fence.code_end]
                code_start_line = self._line_for_offset(line_starts, fence.code_start)
                code_end_line = self._line_for_offset(line_starts, fence.code_end - 1)
                delegate = fence.language or None

                chunks.append(
                    HandlerChunk(
                        chunk_id=
                        f"{self.name}:fence:{code_start_byte}:{code_end_byte}:{fence_index}",
                        text=fence.code,
                        token_count=context.token_encoder.count(fence.code),
                        start_offset=code_start_byte,
                        end_offset=code_end_byte,
                        part_index=part_index,
                        parent_symbol_id=symbol_id,
                        delegate=delegate,
                        metadata={
                            "kind": "fenced_code",
                            "language": delegate,
                            "heading_symbol": symbol_id,
                            "start_line": code_start_line,
                            "end_line": code_end_line,
                            "char_start": fence.code_start,
                            "char_end": fence.code_end,
                            "fence_info": fence.info,
                        },
                    )
                )
                part_index += 1

        return HandlerResult(
            file=file_meta,
            symbols=tuple(symbols),
            chunks=tuple(chunks),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _extract_front_matter(text: str) -> tuple[str | None, int]:
        if not text.startswith("---"):
            return None, 0
        end = text.find("\n")
        if end == -1:
            return None, 0
        cursor = 0
        lines = text.splitlines(keepends=True)
        if not lines or not lines[0].startswith("---"):
            return None, 0
        collected: list[str] = []
        for index, line in enumerate(lines):
            collected.append(line)
            if index == 0:
                continue
            if line.strip().startswith("---"):
                fm_text = "".join(collected)
                fm_end = sum(len(entry) for entry in lines[: index + 1])
                return fm_text, fm_end
        return None, 0

    @staticmethod
    def _build_sections(text: str, *, body_start: int) -> list[_Section]:
        headings = list(_MarkdownHeadingIterator(text, start=body_start))
        if not headings:
            return []

        sections: list[_Section] = []
        for index, heading in enumerate(headings):
            next_start = headings[index + 1].heading_start if index + 1 < len(headings) else len(text)
            section_start = heading.heading_start
            intro_attached = False
            if index == 0 and body_start < heading.heading_start:
                intro = text[body_start:heading.heading_start]
                if intro.strip():
                    section_start = body_start
                    intro_attached = True
            sections.append(
                _Section(
                    heading=heading,
                    section_start=section_start,
                    section_end=next_start,
                    intro_attached=intro_attached,
                )
            )
        return sections

    @staticmethod
    def _extract_fences(section_text: str, *, base_char: int) -> Iterable[_FenceBlock]:
        for match in _FENCE_RE.finditer(section_text):
            info = match.group("info") or ""
            language = info.strip().split()[0].lower() if info.strip() else None
            code = match.group("body") or ""
            code_start = base_char + match.start("body")
            code_end = base_char + match.end("body")
            fence_start = base_char + match.start()
            fence_end = base_char + match.end()
            yield _FenceBlock(
                language=language or None,
                info=info.strip() or None,
                code=code,
                code_start=code_start,
                code_end=code_end,
                fence_start=fence_start,
                fence_end=fence_end,
            )

    def _verify_with_tree_sitter(self, text: str) -> tuple[bool, str | None]:
        if self._parser is None:
            return True, None
        try:  # pragma: no cover - optional dependency path
            tree = self._parser.parse(text.encode("utf-8"))
        except Exception as exc:
            return False, f"tree-sitter parse failed: {exc}"
        root = getattr(tree, "root_node", None)
        if root is None or getattr(root, "type", None) != "document":
            return False, "tree-sitter returned unexpected root node"
        return True, None

    @staticmethod
    def _byte_offsets(text: str) -> Sequence[int]:
        offsets = [0]
        total = 0
        for char in text:
            total += len(char.encode("utf-8"))
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
        if offset < 0:
            return 1
        low, high = 0, len(starts) - 1
        result = 0
        while low <= high:
            mid = (low + high) // 2
            if starts[mid] <= offset:
                result = mid
                low = mid + 1
            else:
                high = mid - 1
        return result + 1


class _MarkdownHeadingIterator:
    """Iterator yielding headings while skipping fenced code blocks."""

    def __init__(self, text: str, *, start: int) -> None:
        self._text = text
        self._start = start
        self._fence_ranges = tuple(
            (match.start(), match.end())
            for match in _FENCE_RE.finditer(text, pos=start)
        )

    def __iter__(self) -> Iterable[_Heading]:
        for match in _HEADING_RE.finditer(self._text, self._start):
            heading_start = match.start()
            if self._inside_fence(heading_start):
                continue
            hashes, title = match.groups()
            level = len(hashes)
            newline_index = self._text.find("\n", heading_start)
            if newline_index == -1:
                heading_end = len(self._text)
            else:
                heading_end = newline_index + 1
            yield _Heading(
                level=level,
                title=title.strip(),
                heading_start=heading_start,
                heading_end=heading_end,
            )

    def __len__(self) -> int:  # pragma: no cover - convenience
        return sum(1 for _ in self)

    def _inside_fence(self, position: int) -> bool:
        for start, end in self._fence_ranges:
            if start <= position < end:
                return True
        return False
