"""HTML parser handler backed by tree-sitter."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
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
    ts_point_row,
)
from .delegation import delegated_chunk_id, delegated_metadata

__all__ = ["HTMLHandler"]


@dataclass(slots=True)
class _ParserResources:
    """Container holding the tree-sitter parser instance."""

    parser: Any


class _MissingDependencyError(RuntimeError):
    """Raised when required tree-sitter resources are unavailable."""


class HTMLHandler(ParserHandler):
    """Parse HTML documents and delegate embedded blocks."""

    name = "html"
    version = "1.0.0"
    display_name = "HTML"

    _STRUCTURAL_TAGS: frozenset[str] = frozenset(
        {
            "body",
            "head",
            "header",
            "footer",
            "section",
            "article",
            "aside",
            "main",
            "nav",
            "div",
            "p",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tr",
            "td",
            "form",
            "fieldset",
            "legend",
            "figure",
            "figcaption",
            "details",
            "summary",
        }
    )

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
            logger.error(
                "Failed to read HTML source", path=str(path), error=str(exc)
            )
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
                "UTF-8 decode error, skipping HTML handler",
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
                    "File is not valid UTF-8; configure a specialized handler "
                    "or re-encode",
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
            logger.error(
                "tree-sitter failed to parse HTML",
                path=str(path),
                error=str(exc),
            )
            return HandlerResult.empty(
                file=file_meta,
                errors=(f"tree-sitter parse error: {exc}",),
            )

        byte_offsets = self._byte_offsets(text)
        module_name = self._derive_module_name(path, context.root)
        module_symbol_id = f"{self.name}:{path.as_posix()}::{module_name}"

        file_meta = HandlerFile(
            path=file_meta.path,
            language=file_meta.language,
            encoding=file_meta.encoding,
            checksum=file_meta.checksum,
            metadata=file_meta.metadata | {"module_name": module_name},
        )

        module_symbol = HandlerSymbol(
            symbol_id=module_symbol_id,
            name=module_name,
            kind="document",
            start_offset=0,
            end_offset=len(source_bytes),
            parent_id=None,
            metadata={"module_name": module_name},
        )

        collector = _HTMLCollector(
            handler=self,
            context=context,
            path=path,
            text=text,
            source_bytes=source_bytes,
            tree=tree,
            byte_offsets=byte_offsets,
            module_symbol_id=module_symbol_id,
        )

        payload = collector.collect()

        symbols = (module_symbol,) + payload.symbols
        chunks = payload.chunks
        warnings = payload.warnings

        return HandlerResult(
            file=file_meta,
            symbols=symbols,
            chunks=chunks,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _load_parser(self, context: ParseContext) -> _ParserResources:
        cache_key = "parser::html"

        def _factory() -> _ParserResources:
            try:
                from tree_sitter_languages import get_parser  # type: ignore[import]
            except Exception as exc:  # pragma: no cover - dependency missing
                raise _MissingDependencyError(
                    "HTML handler requires the 'parser' extras "
                    "(tree_sitter_languages)."
                ) from exc

            try:
                parser = get_parser("html")
            except Exception as exc:  # pragma: no cover
                # tree-sitter parser creation failure
                raise _MissingDependencyError(
                    f"tree-sitter parser for 'html' is unavailable: {exc}"
                ) from exc

            return _ParserResources(parser=parser)

        try:
            resources = context.cache.get(cache_key, _factory)
        except _MissingDependencyError:
            raise
        except Exception as exc:  # pragma: no cover
            # Unexpected caching failure when storing parser resources
            raise _MissingDependencyError(str(exc)) from exc

        if not isinstance(resources, _ParserResources):
            raise _MissingDependencyError(
                "tree-sitter parser cache returned invalid payload"
            )
        return resources

    @staticmethod
    def _derive_module_name(path: Path, root: Path) -> str:
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

    @staticmethod
    def _handler_enabled(context: ParseContext, handler: str) -> bool:
        settings = context.settings.handlers.get(handler)
        if settings is None:
            return True
        return settings.enabled


@dataclass(slots=True)
class _CollectorResult:
    symbols: tuple[HandlerSymbol, ...]
    chunks: tuple[HandlerChunk, ...]
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _HTMLCollector:
    handler: HTMLHandler
    context: ParseContext
    path: Path
    text: str
    source_bytes: bytes
    tree: Any
    byte_offsets: Sequence[int]
    module_symbol_id: str
    _symbols: list[HandlerSymbol] = field(init=False, default_factory=list)
    _chunks: list[HandlerChunk] = field(init=False, default_factory=list)
    _warnings: list[str] = field(init=False, default_factory=list)
    _part_index: int = field(init=False, default=0)

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
        node_type = getattr(node, "type", "")
        if node_type in {"script_element", "style_element"}:
            self._handle_script_or_style(node, parent_symbol_id)
            return
        if node_type == "element":
            tag_name = self._tag_name(node)
            if tag_name and tag_name in self.handler._STRUCTURAL_TAGS:
                symbol = self._emit_element_symbol(
                    node, tag_name, parent_symbol_id
                )
                self._emit_element_chunk(node, tag_name, symbol.symbol_id)
                for child in getattr(node, "named_children", []) or []:
                    self._visit(child, parent_symbol_id=symbol.symbol_id)
                return
            for child in getattr(node, "named_children", []) or []:
                self._visit(child, parent_symbol_id=parent_symbol_id)
            return
        if node_type in {"comment", "text"}:
            self._emit_loose_content(node, parent_symbol_id)
            return
        for child in getattr(node, "named_children", []) or []:
            self._visit(child, parent_symbol_id=parent_symbol_id)

    # ------------------------------------------------------------------
    # Element handling
    # ------------------------------------------------------------------
    def _emit_element_symbol(
        self,
        node: Any,
        tag_name: str,
        parent_symbol_id: str,
    ) -> HandlerSymbol:
        start_offset = node.start_byte
        end_offset = node.end_byte
        symbol_id = (
            f"{self.handler.name}:{self.path.as_posix()}::"
            f"{tag_name}:{start_offset}:{end_offset}"
        )
        metadata = {
            "tag": tag_name,
            "start_line": ts_point_row(node.start_point) + 1,
            "end_line": ts_point_row(node.end_point) + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }
        attributes = self._attributes(node)
        if attributes:
            metadata["attributes"] = attributes
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=tag_name,
            kind="element",
            start_offset=start_offset,
            end_offset=end_offset,
            parent_id=parent_symbol_id,
            metadata=metadata,
        )
        self._symbols.append(symbol)
        return symbol

    def _emit_element_chunk(
        self,
        node: Any,
        tag_name: str,
        parent_symbol_id: str,
    ) -> HandlerChunk:
        raw = self._slice(node.start_byte, node.end_byte)
        text = self._normalize_markup(raw)
        if not text:
            return
        metadata = {
            "kind": "element",
            "tag": tag_name,
            "start_line": ts_point_row(node.start_point) + 1,
            "end_line": ts_point_row(node.end_point) + 1,
            "char_start": self._char_index(node.start_byte),
            "char_end": self._char_index(node.end_byte),
        }
        attributes = self._attributes(node)
        if attributes:
            metadata["attributes"] = attributes
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:{tag_name}:{node.start_byte}:{node.end_byte}",
            text=text,
            token_count=self.context.token_encoder.count(text),
            start_offset=node.start_byte,
            end_offset=node.end_byte,
            part_index=self._next_part_index(),
            parent_symbol_id=parent_symbol_id,
            metadata=metadata,
        )
        self._chunks.append(chunk)
        return chunk

    def _emit_loose_content(self, node: Any, parent_symbol_id: str) -> None:
        text = self._slice(node.start_byte, node.end_byte)
        normalized = self._normalize_text(text)
        if not normalized:
            return
        metadata = {
            "kind": node.type,
            "start_line": ts_point_row(node.start_point) + 1,
            "end_line": ts_point_row(node.end_point) + 1,
            "char_start": self._char_index(node.start_byte),
            "char_end": self._char_index(node.end_byte),
        }
        chunk = HandlerChunk(
            chunk_id=f"{self.handler.name}:{node.type}:{node.start_byte}:{node.end_byte}",
            text=normalized,
            token_count=self.context.token_encoder.count(normalized),
            start_offset=node.start_byte,
            end_offset=node.end_byte,
            part_index=self._next_part_index(),
            parent_symbol_id=parent_symbol_id,
            metadata=metadata,
        )
        self._chunks.append(chunk)

    # ------------------------------------------------------------------
    # Script/style handling
    # ------------------------------------------------------------------
    def _handle_script_or_style(self, node: Any, parent_symbol_id: str) -> None:
        tag_name = self._tag_name(node) or (
            "script" if node.type == "script_element" else "style"
        )
        kind = "script" if node.type == "script_element" else "style"
        symbol = self._emit_special_symbol(
            node, tag_name, kind, parent_symbol_id
        )
        shell_chunk = self._emit_element_chunk(node, tag_name, symbol.symbol_id)
        raw_node = self._raw_text_node(node)
        if raw_node is None:
            return
        raw_text = self._slice(raw_node.start_byte, raw_node.end_byte)
        normalized = self._normalize_code_block(raw_text)
        if not normalized:
            return
        delegate = "javascript" if kind == "script" else "css"
        if not HTMLHandler._handler_enabled(self.context, delegate):
            warning = (
                "Inline "
                f"{kind} block could not delegate because {delegate} handler "
                "is disabled"
            )
            self._warnings.append(warning)
            metadata = {
                "kind": kind,
                "delegate": delegate,
                "start_line": ts_point_row(raw_node.start_point) + 1,
                "end_line": ts_point_row(raw_node.end_point) + 1,
                "char_start": self._char_index(raw_node.start_byte),
                "char_end": self._char_index(raw_node.end_byte),
                "handler_enabled": False,
            }
            chunk = HandlerChunk(
                chunk_id=f"{self.handler.name}:{kind}:inline:{raw_node.start_byte}:{raw_node.end_byte}",
                text=normalized,
                token_count=self.context.token_encoder.count(normalized),
                start_offset=raw_node.start_byte,
                end_offset=raw_node.end_byte,
                part_index=self._next_part_index(),
                parent_symbol_id=symbol.symbol_id,
                metadata=metadata,
            )
            self._chunks.append(chunk)
            return
        chunk_id = delegated_chunk_id(
            delegate=delegate,
            parent_handler=self.handler.name,
            component=f"inline_{kind}",
            start_offset=raw_node.start_byte,
            end_offset=raw_node.end_byte,
        )
        metadata = delegated_metadata(
            delegate=delegate,
            parent_handler=self.handler.name,
            parent_symbol_id=symbol.symbol_id,
            parent_chunk_id=shell_chunk.chunk_id,
            extra={
                "kind": kind,
                "start_line": ts_point_row(raw_node.start_point) + 1,
                "end_line": ts_point_row(raw_node.end_point) + 1,
                "char_start": self._char_index(raw_node.start_byte),
                "char_end": self._char_index(raw_node.end_byte),
            },
        )
        chunk = HandlerChunk(
            chunk_id=chunk_id,
            text=normalized,
            token_count=self.context.token_encoder.count(normalized),
            start_offset=raw_node.start_byte,
            end_offset=raw_node.end_byte,
            part_index=self._next_part_index(),
            parent_symbol_id=symbol.symbol_id,
            delegate=delegate,
            metadata=metadata,
        )
        self._chunks.append(chunk)

    def _emit_special_symbol(
        self,
        node: Any,
        tag_name: str,
        kind: str,
        parent_symbol_id: str,
    ) -> HandlerSymbol:
        start_offset = node.start_byte
        end_offset = node.end_byte
        symbol_id = (
            f"{self.handler.name}:{self.path.as_posix()}::"
            f"{tag_name}:{start_offset}:{end_offset}"
        )
        metadata = {
            "tag": tag_name,
            "kind": kind,
            "start_line": ts_point_row(node.start_point) + 1,
            "end_line": ts_point_row(node.end_point) + 1,
            "char_start": self._char_index(start_offset),
            "char_end": self._char_index(end_offset),
        }
        attributes = self._attributes(node)
        if attributes:
            metadata["attributes"] = attributes
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=tag_name,
            kind=kind,
            start_offset=start_offset,
            end_offset=end_offset,
            parent_id=parent_symbol_id,
            metadata=metadata,
        )
        self._symbols.append(symbol)
        return symbol

    # ------------------------------------------------------------------
    # Node inspection helpers
    # ------------------------------------------------------------------
    def _tag_name(self, node: Any) -> str | None:
        start_tag = (
            node.child_by_field_name("start_tag")
            if hasattr(node, "child_by_field_name")
            else None
        )
        if start_tag is None:
            start_tag = self._first_child_of_type(node, "start_tag")
        target = None
        if start_tag is not None:
            target = start_tag.child_by_field_name("name")
            if target is None:
                target = self._first_child_of_type(start_tag, "tag_name")
        if target is None:
            target = self._first_child_of_type(node, "tag_name")
        if target is None:
            return None
        return self._slice(target.start_byte, target.end_byte).strip().lower()

    def _attributes(self, node: Any) -> dict[str, str]:  # noqa: C901 - HTML attr cases
        start_tag = (
            node.child_by_field_name("start_tag")
            if hasattr(node, "child_by_field_name")
            else None
        )
        if start_tag is None:
            start_tag = self._first_child_of_type(node, "start_tag")
        if start_tag is None:
            return {}
        attributes: dict[str, str] = {}
        for child in getattr(start_tag, "named_children", []) or []:
            if getattr(child, "type", "") != "attribute":
                continue
            name_node = child.child_by_field_name("name")
            if name_node is None:
                name_node = self._first_child_of_type(child, "attribute_name")
            if name_node is None:
                continue
            value_node = child.child_by_field_name("value")
            if value_node is None:
                value_node = self._first_child_of_type(
                    child, "quoted_attribute_value"
                )
            name = self._slice(name_node.start_byte, name_node.end_byte).strip()
            if not name:
                continue
            if value_node is None:
                attributes[name] = ""
                continue
            raw = self._slice(
                value_node.start_byte, value_node.end_byte
            ).strip()
            if raw.startswith(("'", '"')) and raw.endswith(raw[0]):
                raw = raw[1:-1]
            attributes[name] = raw.replace("\r\n", "\n").replace("\r", "\n")
        return attributes

    def _raw_text_node(self, node: Any) -> Any | None:
        for child in getattr(node, "children", []) or []:
            if getattr(child, "type", "") in {"raw_text", "text"}:
                return child
        return None

    def _first_child_of_type(self, node: Any, type_name: str) -> Any | None:
        for child in getattr(node, "children", []) or []:
            if getattr(child, "type", "") == type_name:
                return child
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------
    def _normalize_markup(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def _normalize_text(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        collapsed = " ".join(
            segment for segment in normalized.split() if segment
        )
        return collapsed

    def _normalize_code_block(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return ""
        indent = min(len(line) - len(line.lstrip()) for line in non_empty)
        trimmed = [
            line[indent:] if len(line) >= indent else line for line in lines
        ]
        return "\n".join(line.rstrip() for line in trimmed).strip("\n")

    def _slice(self, start: int, end: int) -> str:
        return self.source_bytes[start:end].decode("utf-8", errors="ignore")

    def _char_index(self, byte_offset: int) -> int:
        return max(0, bisect_right(self.byte_offsets, byte_offset) - 1)

    def _next_part_index(self) -> int:
        value = self._part_index
        self._part_index += 1
        return value
