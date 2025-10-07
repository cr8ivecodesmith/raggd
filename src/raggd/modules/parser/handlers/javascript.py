"""JavaScript and TypeScript parser handlers backed by tree-sitter."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Iterable, Sequence

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

__all__ = ["JavaScriptHandler", "TypeScriptHandler"]


@dataclass(slots=True)
class _ParserResources:
    """Container for tree-sitter parser resources."""

    parser: Any
    language: str


class _MissingDependencyError(RuntimeError):
    """Raised when tree-sitter resources are unavailable."""


class _JavaScriptBaseHandler(ParserHandler):
    """Shared implementation for JavaScript-oriented handlers."""

    name = "javascript"
    version = "1.0.0"
    display_name = "JavaScript"
    _parser_language: str = "javascript"
    _jsx_language: str | None = "tsx"

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
                "Failed to read JavaScript source",
                path=str(path),
                error=str(exc),
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
                "UTF-8 decode error, skipping JavaScript handler",
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
            resources = self._load_parser(path, context=context)
        except _MissingDependencyError as exc:
            logger.warning(str(exc), path=str(path))
            return HandlerResult.empty(
                file=file_meta,
                errors=(str(exc),),
            )

        if resources.parser is None:  # pragma: no cover - defensive branch
            message = "tree-sitter parser could not be constructed"
            logger.error(message, path=str(path))
            return HandlerResult.empty(
                file=file_meta,
                errors=(message,),
            )

        source_bytes = text.encode("utf-8")

        try:
            tree = resources.parser.parse(source_bytes)
        except Exception as exc:  # pragma: no cover - tree-sitter failure
            logger.error(
                "tree-sitter failed to parse JavaScript",
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

        module_doc = self._module_docstring(tree.root_node, source_bytes)
        module_docstring: str | None = None
        module_doc_range: tuple[int, int] | None = None
        if module_doc is not None:
            module_docstring, module_doc_range = module_doc
        module_metadata = {
            "module_name": module_name,
            "language": resources.language,
        }
        if module_docstring:
            module_metadata["docstring"] = module_docstring

        file_meta = HandlerFile(
            path=file_meta.path,
            language=file_meta.language,
            encoding=file_meta.encoding,
            checksum=file_meta.checksum,
            metadata=file_meta.metadata | module_metadata,
        )

        module_symbol = HandlerSymbol(
            symbol_id=module_symbol_id,
            name=module_name,
            kind="module",
            start_offset=0,
            end_offset=len(source_bytes),
            docstring=module_docstring,
            parent_id=None,
            metadata=module_metadata,
        )

        collector = _JavaScriptCollector(
            handler=self,
            context=context,
            path=path,
            text=text,
            source_bytes=source_bytes,
            byte_offsets=byte_offsets,
            tree=tree,
            module_symbol=module_symbol,
            module_symbol_id=module_symbol_id,
            module_name=module_name,
            module_doc_range=module_doc_range,
            token_cap=self._resolve_token_cap(context),
            html_delegate_enabled=self._is_handler_enabled(context, "html"),
        )

        result = collector.collect()

        symbols = (module_symbol,) + result.symbols
        chunks = result.chunks
        warnings = result.warnings

        return HandlerResult(
            file=file_meta,
            symbols=symbols,
            chunks=chunks,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _load_parser(
        self, path: Path, *, context: ParseContext
    ) -> _ParserResources:
        language = self._language_for_path(path)

        cache_key = f"parser::{self.name}::{language}"

        def _factory() -> _ParserResources:
            try:
                from tree_sitter_languages import get_parser  # type: ignore[import]
            except Exception as exc:  # pragma: no cover - dependency missing
                raise _MissingDependencyError(
                    "JavaScript handler requires the 'parser' extras "
                    "(tree_sitter_languages)."
                ) from exc

            try:
                parser = get_parser(language)
            except Exception as exc:  # pragma: no cover
                # tree-sitter parser creation failure
                raise _MissingDependencyError(
                    f"tree-sitter parser for {language!r} is unavailable: {exc}"
                ) from exc

            return _ParserResources(parser=parser, language=language)

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

    def _language_for_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".ts", ".cts", ".mts"}:
            return "typescript"
        if suffix in {".tsx", ".jsx"} and self._jsx_language:
            return self._jsx_language
        return self._parser_language

    def _resolve_token_cap(self, context: ParseContext) -> int | None:
        value = context.handler_max_tokens(self.name)
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "auto":
                return None
            try:
                value = int(normalized)
            except ValueError:
                return None
        if isinstance(value, int) and value > 0:
            return value
        return None

    @staticmethod
    def _derive_module_name(path: Path, root: Path) -> str:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path.name
            parts = Path(relative).with_suffix("").parts
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
    def _module_docstring(
        root: Any,
        source_bytes: bytes,
    ) -> tuple[str, tuple[int, int]] | None:
        comments: list[str] = []
        start_byte: int | None = None
        end_byte: int | None = None
        for child in root.children:
            if child.type == "hash_bang_line":
                continue
            if child.type != "comment":
                break
            raw = source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8"
            )
            normalized = _normalize_comment(raw)
            if normalized:
                comments.append(normalized)
                if start_byte is None:
                    start_byte = child.start_byte
                end_byte = child.end_byte
        if not comments:
            return None
        doc = "\n".join(comments).strip()
        if not doc:
            return None
        if start_byte is None or end_byte is None:
            start_byte = 0
            end_byte = len(source_bytes)
        return doc, (start_byte, end_byte)

    @staticmethod
    def _is_handler_enabled(context: ParseContext, handler: str) -> bool:
        settings = context.settings.handlers.get(handler)
        if settings is None:
            return True
        return settings.enabled


class JavaScriptHandler(_JavaScriptBaseHandler):
    """Concrete handler for JavaScript and JSX sources."""

    name = "javascript"
    version = "1.0.0"
    display_name = "JavaScript"
    _parser_language = "javascript"
    _jsx_language = "tsx"


class TypeScriptHandler(_JavaScriptBaseHandler):
    """Concrete handler for TypeScript and TSX sources."""

    name = "typescript"
    version = "1.0.0"
    display_name = "TypeScript"
    _parser_language = "typescript"
    _jsx_language = "tsx"


@dataclass(slots=True)
class _CollectorResult:
    symbols: tuple[HandlerSymbol, ...]
    chunks: tuple[HandlerChunk, ...]
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _ClassMembers:
    """Partitioned class body nodes."""

    methods: list[Any]
    fields: list[Any]


class _JavaScriptCollector:
    """Collect symbols and chunks from a parsed JavaScript tree."""

    _CLASS_NODES = {
        "class_declaration",
        "abstract_class_declaration",
    }
    _FUNCTION_NODES = {
        "function_declaration",
        "generator_function_declaration",
        "function",
    }
    _VARIABLE_NODES = {
        "lexical_declaration",
        "variable_declaration",
    }
    _TS_DECLARATIONS = {
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }
    _EXPORT_NODES = {
        "export_statement",
        "export_default_declaration",
        "export_declaration",
        "export_assignment",
    }
    _JSX_NODES = {
        "jsx_element",
        "jsx_fragment",
        "jsx_self_closing_element",
    }

    def __init__(
        self,
        *,
        handler: _JavaScriptBaseHandler,
        context: ParseContext,
        path: Path,
        text: str,
        source_bytes: bytes,
        byte_offsets: Sequence[int],
        tree: Any,
        module_symbol: HandlerSymbol,
        module_symbol_id: str,
        module_name: str,
        module_doc_range: tuple[int, int] | None,
        token_cap: int | None,
        html_delegate_enabled: bool,
    ) -> None:
        self._handler = handler
        self._context = context
        self._path = path
        self._text = text
        self._source_bytes = source_bytes
        self._byte_offsets = byte_offsets
        self._root = tree.root_node
        self._module_symbol = module_symbol
        self._module_symbol_id = module_symbol_id
        self._module_name = module_name
        self._module_doc_range = module_doc_range
        self._token_cap = token_cap
        self._html_delegate_enabled = html_delegate_enabled

        self.symbols: list[HandlerSymbol] = []
        self.chunks: list[HandlerChunk] = []
        self.warnings: list[str] = []
        self._part_index = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def collect(self) -> _CollectorResult:
        if self._module_symbol.docstring:
            self._emit_module_docstring_chunk()

        for child in self._root.named_children:
            self._visit_top_level(child)

        if self._html_delegate_enabled and self._uses_jsx():
            self._emit_jsx_chunks()

        return _CollectorResult(
            symbols=tuple(self.symbols),
            chunks=tuple(self.chunks),
            warnings=tuple(self.warnings),
        )

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------
    def _visit_top_level(self, node: Any) -> None:
        if node.type in {"comment", "import_statement"}:
            return
        if node.type in self._EXPORT_NODES or node.type == "export_statement":
            self._handle_export(node)
            return
        if node.type in self._CLASS_NODES:
            self._handle_class(node, exported=False, is_default=False)
            return
        if node.type in self._FUNCTION_NODES:
            self._handle_function(node, exported=False, is_default=False)
            return
        if node.type in self._VARIABLE_NODES:
            self._handle_variable(node, exported=False, is_default=False)
            return
        if node.type in self._TS_DECLARATIONS:
            self._handle_ts_declaration(node, exported=False, is_default=False)
            return
        if node.type in self._EXPORT_NODES:
            self._handle_export(node)
            return

    # ------------------------------------------------------------------
    # Emitters
    # ------------------------------------------------------------------
    def _emit_module_docstring_chunk(self) -> None:
        text = self._module_symbol.docstring or ""
        if not text.strip():
            return
        if self._module_doc_range is not None:
            start, end = self._module_doc_range
        else:
            start = 0
            end = len(self._source_bytes)
        chunk = HandlerChunk(
            chunk_id=f"{self._handler.name}:module-doc:{start}:{end}",
            text=text,
            token_count=self._context.token_encoder.count(text),
            start_offset=start,
            end_offset=end,
            part_index=self._next_part_index(),
            parent_symbol_id=self._module_symbol_id,
            metadata={
                "kind": "module_docstring",
                "module": self._module_symbol.name,
            },
        )
        self.chunks.append(chunk)

    def _handle_export(self, node: Any) -> None:
        text = self._slice(node.start_byte, node.end_byte)
        if (
            self._handle_export_assignment(node, text)
            or self._handle_export_default_declaration(node)
            or self._handle_export_declaration(node, text)
            or self._handle_export_clause(node)
            or self._handle_namespace_export(node, text)
        ):
            return

        # Fallback: treat the export statement as a chunk without symbol.
        self._emit_text_chunk(node, parent=self._module_symbol_id)

    def _handle_export_assignment(self, node: Any, text: str) -> bool:
        if node.type != "export_assignment":
            return False

        name = self._extract_identifier(node) or "default"
        metadata = {
            "kind": "reexport",
            "exported": True,
            "default_export": True,
            "assignment": text.strip(),
        }
        symbol = self._emit_symbol(
            name=name,
            kind="reexport",
            node=node,
            exported=True,
            is_default=True,
            metadata=metadata,
        )
        self._emit_text_chunk(node, parent=symbol.symbol_id)
        return True

    def _handle_export_default_declaration(self, node: Any) -> bool:
        if node.type != "export_default_declaration":
            return False

        declaration = node.child_by_field_name("declaration")
        if declaration is None and node.named_children:
            declaration = node.named_children[0]
        if declaration is None:
            return False

        self._dispatch_declaration(
            declaration,
            exported=True,
            is_default=True,
        )
        return True

    def _handle_export_declaration(self, node: Any, text: str) -> bool:
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            declaration = self._first_matching_child(
                node,
                self._CLASS_NODES
                | self._FUNCTION_NODES
                | self._VARIABLE_NODES
                | self._TS_DECLARATIONS,
            )

        if declaration is None:
            return False

        self._dispatch_declaration(
            declaration,
            exported=True,
            is_default="export default" in text,
        )
        return True

    def _handle_export_clause(self, node: Any) -> bool:
        clause = node.child_by_field_name("clause")
        if clause is None:
            clause = self._first_child_of_type(node, "export_clause")
        if clause is None:
            return False

        source = node.child_by_field_name("source")
        if source is None:
            source = self._first_child_of_type(node, "string")
        self._emit_export_clause(clause, source)
        return True

    def _handle_namespace_export(self, node: Any, text: str) -> bool:
        if "*" not in text:
            return False

        metadata = {
            "kind": "reexport",
            "exported": True,
            "default_export": False,
            "namespace": text.strip(),
        }
        symbol = self._emit_symbol(
            name="*",
            kind="reexport",
            node=node,
            exported=True,
            is_default=False,
            metadata=metadata,
        )
        self._emit_text_chunk(node, parent=symbol.symbol_id)
        return True

    def _dispatch_declaration(
        self,
        node: Any,
        *,
        exported: bool,
        is_default: bool,
    ) -> None:
        if node.type in self._CLASS_NODES:
            self._handle_class(node, exported=exported, is_default=is_default)
        elif node.type in self._FUNCTION_NODES:
            self._handle_function(
                node, exported=exported, is_default=is_default
            )
        elif node.type in self._VARIABLE_NODES:
            self._handle_variable(
                node, exported=exported, is_default=is_default
            )
        elif node.type in self._TS_DECLARATIONS:
            self._handle_ts_declaration(
                node, exported=exported, is_default=is_default
            )
        else:
            self._emit_text_chunk(node, parent=self._module_symbol_id)

    def _handle_class(
        self,
        node: Any,
        *,
        exported: bool,
        is_default: bool,
    ) -> None:
        name = self._extract_identifier(node) or "anonymous"
        header = self._slice(node.start_byte, node.end_byte)
        metadata = self._class_metadata(
            header_text=header,
            exported=exported,
            is_default=is_default,
        )
        docstring = self._leading_comment(node)
        symbol = self._emit_symbol(
            name=name,
            kind="class",
            node=node,
            exported=exported,
            is_default=is_default,
            metadata=metadata,
            docstring=docstring,
        )
        body = self._class_body(node)
        if body is None:
            self._emit_text_chunk(node, parent=symbol.symbol_id)
            return

        members = self._collect_class_members(body)
        self._emit_class_methods(
            members.methods,
            parent_symbol=symbol.symbol_id,
        )
        self._emit_class_fields(
            name=name,
            fields=members.fields,
            parent_symbol=symbol.symbol_id,
        )

    def _class_metadata(
        self,
        *,
        header_text: str,
        exported: bool,
        is_default: bool,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "kind": "class",
            "exported": exported,
            "default_export": is_default,
        }
        extends = self._class_extends_clause(header_text)
        if extends:
            metadata["extends"] = extends
        return metadata

    def _class_extends_clause(self, header_text: str) -> str | None:
        if "extends" not in header_text:
            return None
        candidate = header_text.split("extends", 1)[1].split("{", 1)[0].strip()
        return candidate or None

    def _class_body(self, node: Any) -> Any | None:
        return self._first_child_of_type(node, "class_body")

    def _collect_class_members(self, body: Any) -> _ClassMembers:
        methods: list[Any] = []
        fields: list[Any] = []
        for child in body.named_children:
            if child.type == "method_definition":
                methods.append(child)
            elif child.type != "comment":
                fields.append(child)
        return _ClassMembers(methods=methods, fields=fields)

    def _emit_class_methods(
        self, methods: Iterable[Any], *, parent_symbol: str
    ) -> None:
        for method in methods:
            self._emit_method_chunk(method, parent_symbol=parent_symbol)

    def _emit_class_fields(
        self,
        *,
        name: str,
        fields: Sequence[Any],
        parent_symbol: str,
    ) -> None:
        if not fields:
            return

        start = fields[0].start_byte
        end = fields[-1].end_byte
        text = self._slice(start, end)
        segments = self._split_for_token_cap(text)
        overflow = len(segments) > 1
        char_start = self._char_index(start)
        char_end = self._char_index(end)
        start_line = ts_point_row(fields[0].start_point) + 1
        end_line = ts_point_row(fields[-1].end_point) + 1

        for index, segment in enumerate(segments):
            chunk = HandlerChunk(
                chunk_id=(
                    f"{self._handler.name}:class-field:{start}:{end}:{index}"
                ),
                text=segment,
                token_count=self._context.token_encoder.count(segment),
                start_offset=start,
                end_offset=end,
                part_index=self._next_part_index(),
                parent_symbol_id=parent_symbol,
                metadata={
                    "kind": "class_fields",
                    "class": name,
                    "overflow": overflow,
                    "start_line": start_line,
                    "end_line": end_line,
                    "char_start": char_start,
                    "char_end": char_end,
                },
            )
            self.chunks.append(chunk)

        if overflow:
            warning = (
                "Class "
                f"{name!r} fields split into {len(segments)} parts "
                "due to token cap"
            )
            self.warnings.append(warning)

    def _emit_method_chunk(self, node: Any, *, parent_symbol: str) -> None:
        name = self._extract_identifier(node) or "anonymous"
        text = self._slice(node.start_byte, node.end_byte)
        segments = self._split_for_token_cap(text)
        overflow = len(segments) > 1
        for index, segment in enumerate(segments):
            start = node.start_byte
            end = node.end_byte
            metadata = {
                "kind": "class_method",
                "method": name,
                "start_line": ts_point_row(node.start_point) + 1,
                "end_line": ts_point_row(node.end_point) + 1,
                "char_start": self._char_index(start),
                "char_end": self._char_index(end),
            }
            if overflow:
                metadata["overflow"] = True
                metadata["split_index"] = index
            chunk = HandlerChunk(
                chunk_id=f"{self._handler.name}:method:{start}:{end}:{index}",
                text=segment,
                token_count=self._context.token_encoder.count(segment),
                start_offset=start,
                end_offset=end,
                part_index=self._next_part_index(),
                parent_symbol_id=parent_symbol,
                metadata=metadata,
            )
            self.chunks.append(chunk)
        if overflow:
            self.warnings.append(
                "Method "
                f"{name!r} split into {len(segments)} parts due to token cap",
            )

    def _handle_function(
        self,
        node: Any,
        *,
        exported: bool,
        is_default: bool,
    ) -> None:
        name = self._extract_identifier(node) or (
            "default" if is_default else "anonymous"
        )
        metadata = {
            "kind": "function",
            "exported": exported,
            "default_export": is_default,
        }
        docstring = self._leading_comment(node)
        symbol = self._emit_symbol(
            name=name,
            kind="function",
            node=node,
            exported=exported,
            is_default=is_default,
            metadata=metadata,
            docstring=docstring,
        )
        text = self._slice(node.start_byte, node.end_byte)
        segments = self._split_for_token_cap(text)
        for index, segment in enumerate(segments):
            metadata = {
                "kind": "function",
                "name": name,
                "overflow": len(segments) > 1,
                "split_index": index,
                "start_line": ts_point_row(node.start_point) + 1,
                "end_line": ts_point_row(node.end_point) + 1,
                "char_start": self._char_index(node.start_byte),
                "char_end": self._char_index(node.end_byte),
            }
            chunk = HandlerChunk(
                chunk_id=f"{self._handler.name}:function:{node.start_byte}:{node.end_byte}:{index}",
                text=segment,
                token_count=self._context.token_encoder.count(segment),
                start_offset=node.start_byte,
                end_offset=node.end_byte,
                part_index=self._next_part_index(),
                parent_symbol_id=symbol.symbol_id,
                metadata=metadata,
            )
            self.chunks.append(chunk)
        if len(segments) > 1:
            self.warnings.append(
                "Function "
                f"{name!r} split into {len(segments)} parts due to token cap",
            )

    def _handle_variable(
        self,
        node: Any,
        *,
        exported: bool,
        is_default: bool,
    ) -> None:
        keyword = (
            self._slice(node.start_byte, node.end_byte)
            .lstrip()
            .split(None, 1)[0]
        )
        declarators = [
            child
            for child in node.named_children
            if child.type == "variable_declarator"
        ]
        if keyword != "const" or not declarators:
            self._emit_text_chunk(node, parent=self._module_symbol_id)
            return

        for declarator in declarators:
            identifier = declarator.child_by_field_name("name")
            if identifier is None:
                identifier = self._first_child_of_type(declarator, "identifier")
            name = (
                self._slice(identifier.start_byte, identifier.end_byte)
                if identifier
                else "const"
            )
            metadata = {
                "kind": "const",
                "exported": exported,
                "default_export": is_default,
                "keyword": keyword,
            }
            symbol = self._emit_symbol(
                name=name,
                kind="const",
                node=declarator,
                exported=exported,
                is_default=is_default,
                metadata=metadata,
                parent=self._module_symbol_id,
            )
            self._emit_text_chunk(declarator, parent=symbol.symbol_id)

    def _handle_ts_declaration(
        self,
        node: Any,
        *,
        exported: bool,
        is_default: bool,
    ) -> None:
        name = self._extract_identifier(node) or "anonymous"
        metadata = {
            "kind": node.type,
            "exported": exported,
            "default_export": is_default,
        }
        symbol = self._emit_symbol(
            name=name,
            kind=node.type,
            node=node,
            exported=exported,
            is_default=is_default,
            metadata=metadata,
        )
        self._emit_text_chunk(node, parent=symbol.symbol_id)

    def _emit_export_clause(self, clause: Any, source: Any | None) -> None:
        source_text = None
        if source is not None:
            source_text = self._slice(source.start_byte, source.end_byte).strip(
                "'\""
            )
        for spec in clause.named_children:
            if spec.type != "export_specifier":
                continue
            spec_text = self._slice(spec.start_byte, spec.end_byte)
            local_name, exported_name = _parse_export_specifier(spec_text)
            metadata = {
                "kind": "reexport",
                "exported": True,
                "default_export": exported_name == "default",
                "local": local_name,
                "export": exported_name,
            }
            if source_text:
                metadata["source"] = source_text
            display_name = (
                exported_name if exported_name != "default" else local_name
            )
            symbol = self._emit_symbol(
                name=display_name,
                kind="reexport",
                node=spec,
                exported=True,
                is_default=exported_name == "default",
                metadata=metadata,
            )
            self._emit_text_chunk(spec, parent=symbol.symbol_id)

    # ------------------------------------------------------------------
    # JSX delegation
    # ------------------------------------------------------------------
    def _uses_jsx(self) -> bool:
        return self._find_first(self._root, self._JSX_NODES) is not None

    def _emit_jsx_chunks(self) -> None:
        seen: set[tuple[int, int]] = set()
        for node in self._iterate_nodes(self._root):
            if node.type not in self._JSX_NODES:
                continue
            key = (node.start_byte, node.end_byte)
            if key in seen:
                continue
            seen.add(key)
            text = self._slice(node.start_byte, node.end_byte)
            chunk_id = delegated_chunk_id(
                delegate="html",
                parent_handler=self._handler.name,
                component="jsx",
                start_offset=node.start_byte,
                end_offset=node.end_byte,
            )
            metadata = delegated_metadata(
                delegate="html",
                parent_handler=self._handler.name,
                parent_symbol_id=self._module_symbol_id,
                extra={
                    "kind": "jsx",
                    "start_line": ts_point_row(node.start_point) + 1,
                    "end_line": ts_point_row(node.end_point) + 1,
                    "char_start": self._char_index(node.start_byte),
                    "char_end": self._char_index(node.end_byte),
                },
            )
            chunk = HandlerChunk(
                chunk_id=chunk_id,
                text=text,
                token_count=self._context.token_encoder.count(text),
                start_offset=node.start_byte,
                end_offset=node.end_byte,
                part_index=self._next_part_index(),
                parent_symbol_id=self._module_symbol_id,
                delegate="html",
                metadata=metadata,
            )
            self.chunks.append(chunk)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _emit_symbol(
        self,
        *,
        name: str,
        kind: str,
        node: Any,
        exported: bool,
        is_default: bool,
        metadata: dict[str, Any],
        docstring: str | None = None,
        parent: str | None = None,
    ) -> HandlerSymbol:
        start = node.start_byte
        end = node.end_byte
        char_start = self._char_index(start)
        char_end = self._char_index(end)
        symbol_id = (
            f"{self._handler.name}:{self._path.as_posix()}::"
            f"{self._module_name}.{name}:{start}:{end}"
        )
        symbol = HandlerSymbol(
            symbol_id=symbol_id,
            name=name,
            kind=kind,
            start_offset=start,
            end_offset=end,
            docstring=docstring,
            parent_id=parent or self._module_symbol_id,
            metadata=metadata
            | {
                "char_start": char_start,
                "char_end": char_end,
                "exported": exported,
                "default_export": is_default,
            },
        )
        self.symbols.append(symbol)
        return symbol

    def _emit_text_chunk(self, node: Any, *, parent: str) -> None:
        text = self._slice(node.start_byte, node.end_byte)
        start = node.start_byte
        end = node.end_byte
        chunk = HandlerChunk(
            chunk_id=f"{self._handler.name}:chunk:{start}:{end}:{self._part_index}",
            text=text,
            token_count=self._context.token_encoder.count(text),
            start_offset=start,
            end_offset=end,
            part_index=self._next_part_index(),
            parent_symbol_id=parent,
            metadata={
                "start_line": ts_point_row(node.start_point) + 1,
                "end_line": ts_point_row(node.end_point) + 1,
                "char_start": self._char_index(start),
                "char_end": self._char_index(end),
            },
        )
        self.chunks.append(chunk)

    def _split_for_token_cap(self, text: str) -> list[str]:
        if not text:
            return [text]
        if self._token_cap is None:
            return [text]
        lines = text.splitlines(keepends=True)
        if not lines:
            return [text]
        segments: list[str] = []
        current: list[str] = []
        for line in lines:
            candidate = "".join(current + [line])
            if (
                candidate
                and self._context.token_encoder.count(candidate)
                > self._token_cap
                and current
            ):
                segments.append("".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            segments.append("".join(current))
        return segments or [text]

    def _extract_identifier(self, node: Any) -> str | None:
        direct = node.child_by_field_name("name")
        if direct is not None:
            return self._slice(direct.start_byte, direct.end_byte).strip()
        identifier = self._first_child_of_type(node, "identifier")
        if identifier is not None:
            return self._slice(
                identifier.start_byte, identifier.end_byte
            ).strip()
        prop = node.child_by_field_name("property")
        if prop is not None:
            return self._slice(prop.start_byte, prop.end_byte).strip()
        return None

    def _leading_comment(self, node: Any) -> str | None:
        cursor = node.prev_sibling
        comments: list[str] = []
        while cursor is not None:
            if cursor.type != "comment":
                break
            text = _normalize_comment(
                self._slice(cursor.start_byte, cursor.end_byte),
            )
            if text:
                comments.insert(0, text)
            cursor = cursor.prev_sibling
        if not comments:
            return None
        return "\n".join(comments).strip() or None

    def _char_index(self, byte_offset: int) -> int:
        return max(0, bisect_right(self._byte_offsets, byte_offset) - 1)

    def _slice(self, start: int, end: int) -> str:
        return self._source_bytes[start:end].decode("utf-8", errors="ignore")

    def _next_part_index(self) -> int:
        value = self._part_index
        self._part_index += 1
        return value

    def _first_matching_child(
        self, node: Any, types: Iterable[str]
    ) -> Any | None:
        for child in node.named_children:
            if child.type in types:
                return child
        return None

    @staticmethod
    def _first_child_of_type(node: Any, type_name: str) -> Any | None:
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _iterate_nodes(self, node: Any) -> Iterable[Any]:
        yield node
        for child in getattr(node, "children", []) or []:
            yield from self._iterate_nodes(child)

    def _find_first(self, node: Any, types: set[str]) -> Any | None:
        for candidate in self._iterate_nodes(node):
            if candidate.type in types:
                return candidate
        return None


def _normalize_comment(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("/*"):
        body = stripped[2:-2] if stripped.endswith("*/") else stripped[2:]
        lines = [line.strip(" *") for line in body.splitlines()]
        return "\n".join(line for line in lines if line.strip())
    if stripped.startswith("//"):
        return stripped[2:].strip()
    return stripped


def _parse_export_specifier(text: str) -> tuple[str, str]:
    cleaned = text.strip().removeprefix("{").removesuffix("}")
    cleaned = cleaned.strip()
    if " as " in cleaned:
        local, exported = cleaned.split(" as ", 1)
        return local.strip(), exported.strip()
    return cleaned, cleaned
