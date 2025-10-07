"""Tests for the JavaScript and TypeScript parser handlers."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.parser.handlers import ParseContext
from raggd.modules.parser.handlers.javascript import (
    JavaScriptHandler,
    TypeScriptHandler,
)
from raggd.modules.parser.tokenizer import TokenEncoder


class _DummyEncoding:
    def encode(self, text: str, *, allowed_special: set[str] | None = None) -> list[int]:
        return [0] * max(len(text), 1)


def _make_workspace(tmp_path: Path) -> WorkspacePaths:
    workspace = tmp_path / "workspace"
    return WorkspacePaths(
        workspace=workspace,
        config_file=workspace / "raggd.toml",
        logs_dir=workspace / "logs",
        archives_dir=workspace / "archives",
        sources_dir=workspace / "sources",
    )


def _make_context(tmp_path: Path) -> ParseContext:
    workspace = _make_workspace(tmp_path)
    config = AppConfig()
    settings = ParserModuleSettings()
    token_encoder = TokenEncoder(name="dummy", encoding=_DummyEncoding())
    logger = get_logger("test.javascript-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_javascript_handler_extracts_exports(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_languages")

    context = _make_context(tmp_path)
    handler = JavaScriptHandler(context=context)
    path = tmp_path / "module.js"
    path.write_text(
        dedent(
            """
            // Module header
            export const PI = 3.14;

            export function compute(value) {
              return value * PI;
            }

            class Helper {
              value = 0;
            }

            export class Greeter {
              constructor(name) {
                this.name = name;
              }

              greet() {
                return `Hello, ${this.name}!`;
              }
            }

            export { Helper as HelperAlias };
            export { compute as default } from "./math.js";
            """
        ),
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    module_symbol = next(sym for sym in result.symbols if sym.kind == "module")
    class_symbol = next(sym for sym in result.symbols if sym.name.endswith("Greeter"))
    const_symbol = next(sym for sym in result.symbols if sym.metadata.get("kind") == "const")
    reexports = [sym for sym in result.symbols if sym.metadata.get("kind") == "reexport"]

    assert module_symbol.metadata.get("module_name") == "module"
    assert class_symbol.metadata["exported"] is True
    assert const_symbol.metadata["exported"] is True
    assert any("compute" in sym.name for sym in reexports)

    class_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.parent_symbol_id == class_symbol.symbol_id
        and chunk.metadata.get("kind") == "class_method"
    ]
    assert class_chunks, "Class methods should produce chunks"


def test_typescript_handler_emits_jsx_delegate(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_languages")

    context = _make_context(tmp_path)
    handler = TypeScriptHandler(context=context)
    path = tmp_path / "Component.tsx"
    path.write_text(
        dedent(
            """
            export const Component = () => {
              return (
                <section>
                  <h1>Hello</h1>
                </section>
              );
            };
            """
        ),
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    jsx_chunks = [chunk for chunk in result.chunks if chunk.delegate == "html"]
    assert jsx_chunks, "TSX files should emit delegated HTML chunks"
    first_chunk = jsx_chunks[0]
    assert "<section>" in first_chunk.text
    module_symbol = next(sym for sym in result.symbols if sym.kind == "module")
    assert first_chunk.chunk_id.startswith("html:delegate:typescript:jsx:")
    assert first_chunk.metadata["delegate_parent_handler"] == "typescript"
    assert first_chunk.metadata["delegate_parent_symbol"] == module_symbol.symbol_id
    assert "delegate_parent_chunk" not in first_chunk.metadata
