"""Python handler backed by :mod:`libcst`."""

from __future__ import annotations

import hashlib
from bisect import bisect_right
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

__all__ = ["PythonHandler"]


def _load_libcst(context: ParseContext) -> dict[str, Any] | None:
    """Return cached :mod:`libcst` resources when available."""

    cache_key = "python.libcst"

    def _factory() -> dict[str, Any] | None:
        try:
            import libcst as cst  # type: ignore[import]
            from libcst import metadata as cst_metadata  # type: ignore[import]
        except Exception:  # pragma: no cover - optional dependency missing
            return None

        def get_docstring(node: Any, *, clean: bool = True) -> str | None:
            """Return the docstring for ``node`` if supported."""

            attr = getattr(node, "get_docstring", None)
            if attr is None:
                return None

            try:
                return attr(clean=clean)
            except TypeError:
                # Older libcst builds accepted positional ``clean`` or no args.
                try:
                    return attr(clean)
                except TypeError:
                    return attr()

        return {
            "cst": cst,
            "metadata": cst_metadata,
            "get_docstring": get_docstring,
        }

    return context.cache.get(cache_key, _factory)


def _line_offsets(text: str) -> Sequence[int]:
    """Return starting character offsets for each line in ``text``."""

    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def _byte_offsets(text: str) -> Sequence[int]:
    """Return cumulative UTF-8 byte offsets for ``text``."""

    offsets = [0]
    total = 0
    for char in text:
        total += len(char.encode("utf-8"))
        offsets.append(total)
    return offsets


def _derive_module_name(path: Path, root: Path) -> str:
    """Return a dotted module-like name for ``path``."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path.name
        stemmed = Path(relative).with_suffix("")
        parts = [part for part in stemmed.parts if part not in ("", ".")]
    else:
        stemmed = relative.with_suffix("")
        parts = [part for part in stemmed.parts if part not in ("", ".")]

    if not parts:
        return path.with_suffix("").name
    return ".".join(parts)


class PythonHandler(ParserHandler):
    """Parse Python files using :mod:`libcst`."""

    name = "python"
    version = "1.0.0"
    display_name = "Python"

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
            logger.error("Failed to read python file", path=str(path), error=str(exc))
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
                "UTF-8 decode error, skipping python handler",
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
                    "File is not valid UTF-8; install a specialized handler or re-encode",
                ),
            )

        base_metadata: dict[str, Any] = {
            "size_bytes": len(raw),
            "line_count": text.count("\n") + 1,
        }

        resources = _load_libcst(context)
        if resources is None:
            logger.warning(
                "libcst dependency missing; python handler cannot parse file",
                path=str(path),
            )
            file_meta = HandlerFile(
                path=path,
                language=self.name,
                encoding="utf-8",
                checksum=checksum,
                metadata=base_metadata,
            )
            return HandlerResult.empty(
                file=file_meta,
                errors=(
                    "Python handler requires the 'parser' extras (libcst).",
                ),
            )

        cst = resources["cst"]
        metadata = resources["metadata"]
        get_docstring = resources["get_docstring"]

        try:
            module = cst.parse_module(text)
        except Exception as exc:  # pragma: no cover - libcst parse failure
            logger.error(
                "libcst failed to parse python source",
                path=str(path),
                error=str(exc),
            )
            file_meta = HandlerFile(
                path=path,
                language=self.name,
                encoding="utf-8",
                checksum=checksum,
                metadata=base_metadata,
            )
            return HandlerResult.empty(
                file=file_meta,
                errors=(f"libcst parse error: {exc}",),
            )

        if not text.strip():
            file_meta = HandlerFile(
                path=path,
                language=self.name,
                encoding="utf-8",
                checksum=checksum,
                metadata=base_metadata,
            )
            return HandlerResult(file=file_meta)

        line_offsets = _line_offsets(text)
        byte_offsets = _byte_offsets(text)
        token_cap = self._resolve_token_cap(context)

        class _Collector(cst.CSTVisitor):
            METADATA_DEPENDENCIES = (
                metadata.PositionProvider,
                metadata.WhitespaceInclusivePositionProvider,
            )

            def __init__(
                self,
                *,
                module_node: Any,
                cst_module: Any,
                path: Path,
                root: Path,
                text: str,
                line_offsets: Sequence[int],
                byte_offsets: Sequence[int],
                token_encoder,
                token_cap: int | None,
                handler_name: str,
                get_docstring_fn,
                logger,
            ) -> None:
                self._module_node = module_node
                self._cst = cst_module
                self._path = path
                self._root = root
                self._text = text
                self._line_offsets = line_offsets
                self._byte_offsets = byte_offsets
                self._token_encoder = token_encoder
                self._token_cap = token_cap if token_cap and token_cap > 0 else None
                self._handler_name = handler_name
                self._get_docstring = get_docstring_fn
                self._logger = logger
                self.symbols: list[HandlerSymbol] = []
                self.chunks: list[HandlerChunk] = []
                self.warnings: list[str] = []
                self.module_docstring: str | None = None
                self.module_symbol_id: str | None = None
                self.module_name: str = _derive_module_name(path, root)
                self._symbol_prefix = f"{handler_name}:{path.as_posix()}"
                self._chunk_prefix = self._symbol_prefix
                self._scope_stack: list[str] = []
                self._symbol_stack: list[str] = []

            # ----------------------------------------------------------
            # Visitor helpers
            # ----------------------------------------------------------
            def visit_Module(self, node: Any) -> bool:
                start_char, end_char = self._node_span(node)
                symbol_id = f"{self._symbol_prefix}::{self.module_name}"
                self.module_symbol_id = symbol_id
                docstring = self._get_docstring(node, clean=True)
                symbol = HandlerSymbol(
                    symbol_id=symbol_id,
                    name=self.module_name,
                    kind="module",
                    start_offset=self._byte_offset(start_char),
                    end_offset=self._byte_offset(end_char),
                    docstring=docstring,
                    parent_id=None,
                    metadata={
                        "kind": "module",
                        "qualified_name": self.module_name,
                        "path": self._path.as_posix(),
                    },
                )
                self.symbols.append(symbol)
                self.module_docstring = docstring
                self._scope_stack.append(self.module_name)
                self._symbol_stack.append(symbol_id)
                self._emit_module_docstring(node, symbol_id)
                return True

            def leave_Module(self, node: Any) -> None:
                if self._scope_stack:
                    self._scope_stack.pop()
                if self._symbol_stack:
                    self._symbol_stack.pop()

            def visit_ClassDef(self, node: Any) -> bool:
                symbol_id = self._emit_definition(node, kind="class")
                self._scope_stack.append(node.name.value)
                self._symbol_stack.append(symbol_id)
                return True

            def leave_ClassDef(self, node: Any) -> None:
                if self._scope_stack:
                    self._scope_stack.pop()
                if self._symbol_stack:
                    self._symbol_stack.pop()

            def visit_FunctionDef(self, node: Any) -> bool:
                symbol_id = self._emit_definition(node, kind="function", is_async=False)
                self._scope_stack.append(node.name.value)
                self._symbol_stack.append(symbol_id)
                return True

            def leave_FunctionDef(self, node: Any) -> None:
                if self._scope_stack:
                    self._scope_stack.pop()
                if self._symbol_stack:
                    self._symbol_stack.pop()

            def visit_AsyncFunctionDef(self, node: Any) -> bool:
                symbol_id = self._emit_definition(node, kind="function", is_async=True)
                self._scope_stack.append(node.name.value)
                self._symbol_stack.append(symbol_id)
                return True

            def leave_AsyncFunctionDef(self, node: Any) -> None:
                if self._scope_stack:
                    self._scope_stack.pop()
                if self._symbol_stack:
                    self._symbol_stack.pop()

            # ------------------------------------------------------
            # Definition helpers
            # ------------------------------------------------------
            def _emit_definition(
                self,
                node: Any,
                *,
                kind: str,
                is_async: bool = False,
            ) -> str:
                name = node.name.value
                qualified_name = self._qualified_name(name)
                parent_id = self._symbol_stack[-1] if self._symbol_stack else None
                start_char, end_char = self._node_span(node)
                docstring = self._get_docstring(node, clean=True)
                decorators = tuple(
                    str(decorator.decorator).strip()
                    for decorator in getattr(node, "decorators", [])
                    if decorator is not None
                )
                metadata: dict[str, Any] = {
                    "kind": kind,
                    "qualified_name": qualified_name,
                }
                if decorators:
                    metadata["decorators"] = decorators
                if kind == "class":
                    bases = [
                        str(base.value).strip()
                        for base in getattr(node, "bases", [])
                        if base is not None
                    ]
                    if bases:
                        metadata["bases"] = tuple(bases)
                else:
                    metadata["async"] = is_async
                    params = getattr(node, "params", None)
                    if params is not None:
                        metadata["parameters"] = str(params).strip()
                    returns = getattr(node, "returns", None)
                    if returns is not None and returns.annotation is not None:
                        metadata["return_annotation"] = str(returns.annotation).strip()

                symbol_id = f"{self._symbol_prefix}::{qualified_name}"
                symbol = HandlerSymbol(
                    symbol_id=symbol_id,
                    name=name,
                    kind=kind,
                    start_offset=self._byte_offset(start_char),
                    end_offset=self._byte_offset(end_char),
                    docstring=docstring,
                    parent_id=parent_id,
                    metadata=metadata,
                )
                self.symbols.append(symbol)

                self._emit_chunks(
                    symbol_id=symbol_id,
                    qualified_name=qualified_name,
                    kind=kind,
                    start_char=start_char,
                    end_char=end_char,
                    is_async=is_async,
                    decorators=decorators,
                )
                return symbol_id

            # ------------------------------------------------------
            # Chunk helpers
            # ------------------------------------------------------
            def _emit_chunks(
                self,
                *,
                symbol_id: str,
                qualified_name: str,
                kind: str,
                start_char: int,
                end_char: int,
                is_async: bool,
                decorators: tuple[str, ...],
            ) -> None:
                if start_char >= end_char:
                    return

                ranges = self._split_ranges(start_char, end_char)
                if not ranges:
                    return

                part_total = len(ranges)
                for index, (segment_start, segment_end) in enumerate(ranges):
                    text_segment = self._text[segment_start:segment_end]
                    if not text_segment.strip():
                        continue
                    start_byte = self._byte_offset(segment_start)
                    end_byte = self._byte_offset(segment_end)
                    start_line = self._line_for_char(segment_start)
                    end_line = self._line_for_char(segment_end - 1)
                    token_count = self._token_encoder.count(text_segment)
                    metadata: dict[str, Any] = {
                        "kind": kind,
                        "qualified_name": qualified_name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "char_start": segment_start,
                        "char_end": segment_end,
                        "part_total": part_total,
                    }
                    if part_total > 1:
                        metadata["overflow"] = True
                    if is_async:
                        metadata["async"] = True
                    if decorators:
                        metadata["decorators"] = decorators

                    chunk = HandlerChunk(
                        chunk_id=f"{self._chunk_prefix}:{segment_start}:{segment_end}:{index}",
                        text=text_segment,
                        token_count=token_count,
                        start_offset=start_byte,
                        end_offset=end_byte,
                        part_index=index,
                        parent_symbol_id=symbol_id,
                        metadata=metadata,
                    )
                    self.chunks.append(chunk)

                if part_total > 1:
                    self.warnings.append(
                        f"{qualified_name} split into {part_total} chunks due to token cap"
                    )
                    self._logger.debug(
                        "Split python symbol due to token cap",
                        qualified_name=qualified_name,
                        parts=part_total,
                        token_cap=self._token_cap,
                    )

            def _split_ranges(self, start_char: int, end_char: int) -> list[tuple[int, int]]:
                if self._token_cap is None:
                    return [(start_char, end_char)]

                segment_text = self._text[start_char:end_char]
                if not segment_text:
                    return []

                total_tokens = self._token_encoder.count(segment_text)
                if total_tokens <= self._token_cap:
                    return [(start_char, end_char)]

                ranges: list[tuple[int, int]] = []
                lines = segment_text.splitlines(keepends=True)
                if not lines:
                    return [(start_char, end_char)]

                current_start = start_char
                consumed = 0
                current_tokens = 0
                for line in lines:
                    line_tokens = self._token_encoder.count(line)
                    proposed = current_tokens + line_tokens
                    line_length = len(line)
                    if ranges or current_tokens:
                        exceeds = proposed > self._token_cap
                    else:
                        exceeds = False

                    if exceeds and current_tokens > 0:
                        ranges.append((current_start, start_char + consumed))
                        current_start = start_char + consumed
                        current_tokens = line_tokens
                    else:
                        current_tokens = proposed

                    consumed += line_length

                final_end = start_char + consumed
                ranges.append((current_start, final_end))
                if not ranges:
                    ranges.append((start_char, end_char))
                return ranges

            def _emit_module_docstring(self, node: Any, symbol_id: str) -> None:
                if not self.module_docstring:
                    return
                if not getattr(node, "body", None):
                    return
                first = node.body[0]
                simple_stmt = getattr(self._cst, "SimpleStatementLine", None)
                if simple_stmt is None or not isinstance(first, simple_stmt):
                    return
                start_char, end_char = self._node_span(first)
                doc_text = self._text[start_char:end_char]
                if not doc_text.strip():
                    return
                start_line = self._line_for_char(start_char)
                end_line = self._line_for_char(end_char - 1)
                chunk = HandlerChunk(
                    chunk_id=f"{self._chunk_prefix}:{start_char}:{end_char}:module-doc",
                    text=doc_text,
                    token_count=self._token_encoder.count(doc_text),
                    start_offset=self._byte_offset(start_char),
                    end_offset=self._byte_offset(end_char),
                    part_index=0,
                    parent_symbol_id=symbol_id,
                    metadata={
                        "kind": "module_docstring",
                        "qualified_name": self.module_name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "char_start": start_char,
                        "char_end": end_char,
                    },
                )
                self.chunks.append(chunk)

            # ------------------------------------------------------
            # Utility helpers
            # ------------------------------------------------------
            def _qualified_name(self, name: str) -> str:
                scopes = [scope for scope in self._scope_stack if scope]
                if scopes:
                    return ".".join((*scopes, name))
                return name

            def _node_span(self, node: Any) -> tuple[int, int]:
                code_range = self.get_metadata(metadata.PositionProvider, node)
                start_char = self._char_from_position(code_range.start)
                try:
                    inclusive_range = self.get_metadata(
                        metadata.WhitespaceInclusivePositionProvider, node
                    )
                except KeyError:  # pragma: no cover - optional provider gaps
                    pass
                else:
                    start_char = min(
                        start_char, self._char_from_position(inclusive_range.start)
                    )
                end_char = self._char_from_position(code_range.end)
                return start_char, end_char

            def _char_from_position(self, position: Any) -> int:
                line = getattr(position, "line", 0)
                column = getattr(position, "column", 0)
                index = max(line - 1, 0)
                if index < len(self._line_offsets):
                    return self._line_offsets[index] + column
                return column

            def _byte_offset(self, char_index: int) -> int:
                if char_index < 0:
                    return 0
                if char_index >= len(self._byte_offsets):
                    return self._byte_offsets[-1]
                return self._byte_offsets[char_index]

            def _line_for_char(self, char_index: int) -> int:
                if char_index < 0:
                    return 1
                return bisect_right(self._line_offsets, char_index) or 1

        wrapper = metadata.MetadataWrapper(module, unsafe_skip_copy=True)
        collector = _Collector(
            module_node=module,
            cst_module=cst,
            path=path,
            root=context.root,
            text=text,
            line_offsets=line_offsets,
            byte_offsets=byte_offsets,
            token_encoder=context.token_encoder,
            token_cap=token_cap,
            handler_name=self.name,
            get_docstring_fn=get_docstring,
            logger=logger,
        )
        wrapper.visit(collector)

        file_metadata = dict(base_metadata)
        file_metadata["module_name"] = collector.module_name
        if collector.module_docstring:
            file_metadata["docstring"] = collector.module_docstring

        file_meta = HandlerFile(
            path=path,
            language=self.name,
            encoding="utf-8",
            checksum=checksum,
            metadata=file_metadata,
        )

        return HandlerResult(
            file=file_meta,
            symbols=tuple(collector.symbols),
            chunks=tuple(collector.chunks),
            warnings=tuple(collector.warnings),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
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
