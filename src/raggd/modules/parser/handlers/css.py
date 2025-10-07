"""CSS parser handler backed by tree-sitter."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Sequence

from .base import (
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSymbol,
    ParseContext,
    ParserHandler,
)

__all__ = ["CSSHandler"]


@dataclass(slots=True)
class _ParserResources:
    """Container holding the tree-sitter parser instance."""

    parser: Any


class _MissingDependencyError(RuntimeError):
    """Raised when required tree-sitter resources are unavailable."""


class CSSHandler(ParserHandler):
    """Parse CSS documents and emit rule-based chunks."""

    name = "css"
    version = "1.0.0"
    display_name = "CSS"

    def __init__(self, *, context: ParseContext) -> None:
        self._context = context

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
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
            logger.error("Failed to read CSS source", path=str(path), error=str(exc))
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
                "UTF-8 decode error, skipping CSS handler",
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

        try:
            resources = self._load_parser(context)
        except _MissingDependencyError as exc:
            logger.warning(str(exc), path=str(path))
            return HandlerResult.empty(
                file=file_meta,
                errors=(str(exc),),
            )

        source_bytes = text.encode("utf-8")

        try:
            tree = resources.parser.parse(source_bytes)
        except Exception as exc:  # pragma: no cover - tree-sitter failure
            logger.error("tree-sitter failed to parse CSS", path=str(path), error=str(exc))
            return HandlerResult.empty(
                file=file_meta,
                errors=(f"tree-sitter parse error: {exc}",),
            )

        stylesheet_name = self._derive_stylesheet_name(path, context.root)
        byte_offsets = self._byte_offsets(text)
        token_cap = self._resolve_token_cap(context)

        module_symbol_id = f"{self.name}:{path.as_posix()}::{stylesheet_name}"
        module_symbol = HandlerSymbol(
            symbol_id=module_symbol_id,
            name=stylesheet_name,
            kind="stylesheet",
            start_offset=0,
            end_offset=len(source_bytes),
            parent_id=None,
            metadata={"stylesheet_name": stylesheet_name},
        )

        collector = _CSSCollector(
            handler=self,
            context=context,
            path=path,
            text=text,
            source_bytes=source_bytes,
            tree=tree,
            byte_offsets=byte_offsets,
            module_symbol_id=module_symbol_id,
            token_cap=token_cap,
        )

        payload = collector.collect()

        file_meta = HandlerFile(
            path=file_meta.path,
            language=file_meta.language,
            encoding=file_meta.encoding,
            checksum=file_meta.checksum,
            metadata=file_meta.metadata | {"stylesheet_name": stylesheet_name},
        )

        symbols = (module_symbol,) + payload.symbols

        return HandlerResult(
            file=file_meta,
            symbols=symbols,
            chunks=payload.chunks,
            warnings=payload.warnings,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_parser(self, context: ParseContext) -> _ParserResources:
        cache_key = "parser::css"

        def _factory() -> _ParserResources:
            try:
                from tree_sitter_languages import get_parser  # type: ignore[import]
            except Exception as exc:  # pragma: no cover - dependency missing
                raise _MissingDependencyError(
                    "CSS handler requires the 'parser' extras (tree_sitter_languages)."
                ) from exc

            try:
                parser = get_parser("css")
            except Exception as exc:  # pragma: no cover - parser creation failure
                raise _MissingDependencyError(
                    f"tree-sitter parser for 'css' is unavailable: {exc}"
                ) from exc

            return _ParserResources(parser=parser)

        try:
            resources = context.cache.get(cache_key, _factory)
        except _MissingDependencyError:
            raise
        except Exception as exc:  # pragma: no cover - unexpected caching failure
            raise _MissingDependencyError(str(exc)) from exc

        if not isinstance(resources, _ParserResources):
            raise _MissingDependencyError("tree-sitter parser cache returned invalid payload")
        return resources

    @staticmethod
    def _derive_stylesheet_name(path: Path, root: Path) -> str:
        try:
            relative = path.relative_to(root)
        except ValueError:
            parts = Path(path.name).with_suffix("").parts
        else:
            parts = relative.with_suffix("").parts
        filtered = [part for part in parts if part not in ("", ".")]
        if not filtered:
            return path.with_suffix("").name
        return ".".join(filtered)

    @staticmethod
    def _byte_offsets(text: str) -> Sequence[int]:
        offsets = [0]
        total = 0
        for char in text:
            total += len(char.encode("utf-8"))
            offsets.append(total)
        return offsets

    def _resolve_token_cap(self, context: ParseContext) -> int | None:
        cap = context.handler_max_tokens(self.name)
        if cap == "auto":
            general = context.settings.general_max_tokens
            if isinstance(general, int) and general > 0:
                return general
            return 2000
        if isinstance(cap, int) and cap > 0:
            return cap
        return None


@dataclass(slots=True)
class _CollectorResult:
    symbols: tuple[HandlerSymbol, ...]
    chunks: tuple[HandlerChunk, ...]
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _SelectorSegment:
    text: str
    start: int
    end: int


@dataclass(slots=True)
class _CSSCollector:
    handler: CSSHandler
    context: ParseContext
    path: Path
    text: str
    source_bytes: bytes
    tree: Any
    byte_offsets: Sequence[int]
    module_symbol_id: str
    token_cap: int | None

    def __post_init__(self) -> None:
        self._symbols: list[HandlerSymbol] = []
        self._chunks: list[HandlerChunk] = []
        self._warnings: list[str] = []
        self._part_index = 0
        self._cascade: list[str] = []

    @property
    def symbols(self) -> list[HandlerSymbol]:
        return self._symbols

    @property
    def chunks(self) -> list[HandlerChunk]:
        return self._chunks

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def collect(self) -> _CollectorResult:
        root = self.tree.root_node
        for child in getattr(root, "named_children", []) or []:
            self._visit(child, parent_symbol_id=self.module_symbol_id)
        return _CollectorResult(
            symbols=tuple(self._symbols),
            chunks=tuple(self._chunks),
            warnings=tuple(self._warnings),
        )

    # ------------------------------------------------------------------
    # Tree traversal
    # ------------------------------------------------------------------
    def _visit(self, node: Any, *, parent_symbol_id: str) -> None:
        if getattr(node, "is_missing", False):
            return
        if getattr(node, "has_error", False) and getattr(node, "type", "") != "ERROR":
            self._emit_error_chunk(node, parent_symbol_id)
            return

        node_type = getattr(node, "type", "")

        if node_type == "comment":
            self._emit_comment_chunk(node, parent_symbol_id)
            return
        if node_type in {"rule_set", "qualified_rule"}:
            self._emit_rule_set(node, parent_symbol_id)
            return
        if node_type == "at_rule":
            self._emit_at_rule(node, parent_symbol_id)
            return
        if node_type == "keyframe_block":
            self._emit_keyframe_block(node, parent_symbol_id)
            return
        if node_type == "ERROR":
            self._emit_error_chunk(node, parent_symbol_id)
            return

        for child in getattr(node, "named_children", []) or []:
            self._visit(child, parent_symbol_id=parent_symbol_id)

    # ------------------------------------------------------------------
    # Emission helpers
    # ------------------------------------------------------------------
    def _emit_rule_set(self, node: Any, parent_symbol_id: str) -> None:
        selector_node = self._selector_node(node)
        block_node = node.child_by_field_name("block")
        selector_segments = (
            self._extract_selectors(selector_node) if selector_node else ()
        )
        if not selector_segments and selector_node is not None:
            selector_segments = (
                _SelectorSegment(
                    text=self._slice(selector_node.start_byte, selector_node.end_byte).strip(),
                    start=selector_node.start_byte,
                    end=selector_node.end_byte,
                ),
            )
        selector_texts = [segment.text for segment in selector_segments]

        start_offset = node.start_byte
        end_offset = node.end_byte

        metadata = {
            "kind": "rule",
            "selectors": selector_texts or None,
            "cascade": tuple(self._cascade),
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }

        symbol_id = (
            f"{self.handler.name}:{self.path.as_posix()}::"
            f"rule:{start_offset}:{end_offset}"
        )
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=", ".join(selector_texts or ("<anonymous>",)),
            kind="rule",
            start_offset=start_offset,
            end_offset=end_offset,
            parent_id=parent_symbol_id,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )
        self._symbols.append(symbol)

        block_text = self._slice(block_node.start_byte, block_node.end_byte) if block_node else ""
        rule_text = self._slice(start_offset, end_offset)
        normalized_rule_text = self._normalize_rule(rule_text)

        if (
            self.token_cap is not None
            and selector_segments
            and self.context.token_encoder.count(normalized_rule_text) > self.token_cap
            and len(selector_segments) > 1
            and block_text
        ):
            normalized_block = self._normalize_block(block_text)
            selector_count = len(selector_segments)
            for index, segment in enumerate(selector_segments):
                combined = f"{segment.text} {normalized_block}".strip()
                end_marker = block_node.end_byte if block_node else segment.end
                chunk_metadata = {
                    "kind": "rule",
                    "selector": segment.text,
                    "selector_index": index,
                    "selector_count": selector_count,
                    "cascade": tuple(self._cascade),
                    "start_line": node.start_point.row + 1,
                    "end_line": node.end_point.row + 1,
                    "char_start": self._char_index(segment.start),
                    "char_end": self._char_index(end_marker),
                }
                chunk = HandlerChunk(
                    chunk_id=f"{self.handler.name}:rule:{segment.start}:{end_marker}",
                    text=combined,
                    token_count=self.context.token_encoder.count(combined),
                    start_offset=segment.start,
                    end_offset=end_marker,
                    part_index=self._next_part_index(),
                    parent_symbol_id=symbol.symbol_id,
                    metadata=chunk_metadata,
                )
                self._chunks.append(chunk)
            return

        chunk_metadata = {
            "kind": "rule",
            "selectors": selector_texts or None,
            "cascade": tuple(self._cascade),
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:rule:{start_offset}:{end_offset}",
            text=normalized_rule_text,
            token_count=self.context.token_encoder.count(normalized_rule_text),
            start_offset=start_offset,
            end_offset=end_offset,
            part_index=self._next_part_index(),
            parent_symbol_id=symbol.symbol_id,
            metadata={k: v for k, v in chunk_metadata.items() if v is not None},
        )
        self._chunks.append(chunk)

        if block_node is not None:
            for child in getattr(block_node, "named_children", []) or []:
                if getattr(child, "type", "") not in {"declaration", "comment"}:
                    self._visit(child, parent_symbol_id=symbol.symbol_id)

    def _emit_at_rule(self, node: Any, parent_symbol_id: str) -> None:
        name_node = node.child_by_field_name("name")
        prelude_node = node.child_by_field_name("prelude")
        block_node = node.child_by_field_name("block")

        name = self._slice(name_node.start_byte, name_node.end_byte).strip() if name_node else ""
        prelude = self._slice(prelude_node.start_byte, prelude_node.end_byte).strip() if prelude_node else ""
        cascade_label = f"@{name}" if name else "@unknown"
        if prelude:
            cascade_label = f"{cascade_label} {prelude}"

        start_offset = node.start_byte
        end_offset = node.end_byte

        metadata = {
            "kind": "at_rule",
            "name": name or None,
            "prelude": prelude or None,
            "cascade": tuple(self._cascade),
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }

        symbol_id = (
            f"{self.handler.name}:{self.path.as_posix()}::"
            f"at:{start_offset}:{end_offset}"
        )
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=cascade_label,
            kind="at_rule",
            start_offset=start_offset,
            end_offset=end_offset,
            parent_id=parent_symbol_id,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )
        self._symbols.append(symbol)

        header_end = block_node.start_byte if block_node else end_offset
        header_text = self._normalize_rule(self._slice(start_offset, header_end))
        chunk_metadata = {
            "kind": "at_rule",
            "name": name or None,
            "prelude": prelude or None,
            "cascade": tuple(self._cascade),
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(header_end),
            "has_block": bool(block_node),
        }
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:at:{start_offset}:{header_end}",
            text=header_text,
            token_count=self.context.token_encoder.count(header_text),
            start_offset=start_offset,
            end_offset=header_end,
            part_index=self._next_part_index(),
            parent_symbol_id=symbol.symbol_id,
            metadata={k: v for k, v in chunk_metadata.items() if v is not None},
        )
        self._chunks.append(chunk)

        if block_node is None:
            return

        self._cascade.append(cascade_label)
        for child in getattr(block_node, "named_children", []) or []:
            self._visit(child, parent_symbol_id=symbol.symbol_id)
        self._cascade.pop()

    def _emit_keyframe_block(self, node: Any, parent_symbol_id: str) -> None:
        selectors = []
        block_node = None
        for child in getattr(node, "named_children", []) or []:
            child_type = getattr(child, "type", "")
            if child_type in {"from", "to", "percentage"}:
                selectors.append(self._slice(child.start_byte, child.end_byte).strip())
            elif child_type == "block":
                block_node = child

        start_offset = node.start_byte
        end_offset = node.end_byte

        metadata = {
            "kind": "keyframe",
            "selectors": selectors or None,
            "cascade": tuple(self._cascade),
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }

        name = ", ".join(selectors) if selectors else "<keyframe>"
        symbol_id = (
            f"{self.handler.name}:{self.path.as_posix()}::"
            f"keyframe:{start_offset}:{end_offset}"
        )
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=name,
            kind="keyframe",
            start_offset=start_offset,
            end_offset=end_offset,
            parent_id=parent_symbol_id,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )
        self._symbols.append(symbol)

        text = self._normalize_rule(self._slice(start_offset, end_offset))
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:keyframe:{start_offset}:{end_offset}",
            text=text,
            token_count=self.context.token_encoder.count(text),
            start_offset=start_offset,
            end_offset=end_offset,
            part_index=self._next_part_index(),
            parent_symbol_id=symbol.symbol_id,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )
        self._chunks.append(chunk)

    def _emit_comment_chunk(self, node: Any, parent_symbol_id: str) -> None:
        text = self._normalize_comment(self._slice(node.start_byte, node.end_byte))
        if not text:
            return
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:comment:{node.start_byte}:{node.end_byte}",
            text=text,
            token_count=self.context.token_encoder.count(text),
            start_offset=node.start_byte,
            end_offset=node.end_byte,
            part_index=self._next_part_index(),
            parent_symbol_id=parent_symbol_id,
            metadata={
                "kind": "comment",
                "cascade": tuple(self._cascade),
                "start_line": node.start_point.row + 1,
                "end_line": node.end_point.row + 1,
                "char_start": self._char_index(node.start_byte),
                "char_end": self._char_index(node.end_byte),
            },
        )
        self._chunks.append(chunk)

    def _emit_error_chunk(self, node: Any, parent_symbol_id: str) -> None:
        text = self._slice(node.start_byte, node.end_byte)
        normalized = self._normalize_rule(text)
        if not normalized:
            return
        message = "CSS handler encountered a syntax error; emitted fallback chunk."
        self._warnings.append(message)
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:error:{node.start_byte}:{node.end_byte}",
            text=normalized,
            token_count=self.context.token_encoder.count(normalized),
            start_offset=node.start_byte,
            end_offset=node.end_byte,
            part_index=self._next_part_index(),
            parent_symbol_id=parent_symbol_id,
            metadata={
                "kind": "error",
                "cascade": tuple(self._cascade),
                "start_line": node.start_point.row + 1,
                "end_line": node.end_point.row + 1,
                "char_start": self._char_index(node.start_byte),
                "char_end": self._char_index(node.end_byte),
            },
        )
        self._chunks.append(chunk)

    # ------------------------------------------------------------------
    # Selector helpers
    # ------------------------------------------------------------------
    def _selector_node(self, node: Any) -> Any | None:
        for field in ("selector_group", "selectors", "selector_list", "prelude"):
            candidate = node.child_by_field_name(field)
            if candidate is not None:
                return candidate
        for child in getattr(node, "named_children", []) or []:
            if "selector" in getattr(child, "type", ""):
                return child
        return None

    def _extract_selectors(self, node: Any) -> tuple[_SelectorSegment, ...]:
        raw = self._slice(node.start_byte, node.end_byte)
        if not raw:
            return ()
        segments: list[_SelectorSegment] = []
        depth = 0
        start_index = 0
        characters = list(raw)
        for idx, char in enumerate(characters):
            if char in {"(", "["}:
                depth += 1
            elif char in {")","]"}:
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                segment_text = raw[start_index:idx].strip()
                if segment_text:
                    seg_start = node.start_byte + len(raw[:start_index].encode("utf-8"))
                    seg_end = node.start_byte + len(raw[:idx].encode("utf-8"))
                    segments.append(
                        _SelectorSegment(text=segment_text, start=seg_start, end=seg_end)
                    )
                start_index = idx + 1
        tail = raw[start_index:].strip()
        if tail:
            seg_start = node.start_byte + len(raw[:start_index].encode("utf-8"))
            seg_end = node.start_byte + len(raw.encode("utf-8"))
            segments.append(
                _SelectorSegment(text=tail, start=seg_start, end=seg_end)
            )
        return tuple(segments)

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------
    def _slice(self, start: int, end: int) -> str:
        return self.source_bytes[start:end].decode("utf-8", errors="ignore")

    def _normalize_rule(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def _normalize_block(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def _normalize_comment(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in normalized.split("\n") if line.strip())

    def _char_index(self, byte_offset: int) -> int:
        return max(0, bisect_right(self.byte_offsets, byte_offset) - 1)

    def _next_part_index(self) -> int:
        value = self._part_index
        self._part_index += 1
        return value
