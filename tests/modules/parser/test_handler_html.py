"""Tests for the HTML parser handler."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.parser.handlers import ParseContext
from raggd.modules.parser.handlers.html import HTMLHandler
from raggd.modules.parser.tokenizer import TokenEncoder


class _DummyEncoding:
    def encode(
        self, text: str, *, allowed_special: set[str] | None = None
    ) -> list[int]:
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
    logger = get_logger("test.html-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_html_handler_emits_structural_chunks_and_delegates(
    tmp_path: Path,
) -> None:
    pytest.importorskip("tree_sitter_languages")

    context = _make_context(tmp_path)
    handler = HTMLHandler(context=context)
    path = tmp_path / "index.html"
    path.write_text(
        dedent(
            """
            <!doctype html>
            <html>
              <head>
                <style scoped>
                  body { color: black; }
                </style>
              </head>
              <body>
                <section id="intro" data-role="hero">
                  <h1>Welcome</h1>
                  <script type="module">
                    console.log('hi');
                  </script>
                </section>
              </body>
            </html>
            """
        ),
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    section_symbol = next(
        sym for sym in result.symbols if sym.metadata.get("tag") == "section"
    )
    script_symbol = next(sym for sym in result.symbols if sym.kind == "script")
    style_symbol = next(sym for sym in result.symbols if sym.kind == "style")

    section_chunk = next(
        chunk
        for chunk in result.chunks
        if chunk.metadata.get("tag") == "section"
        and chunk.metadata.get("kind") == "element"
    )
    assert "Welcome" in section_chunk.text
    assert section_chunk.parent_symbol_id == section_symbol.symbol_id

    script_chunk = next(
        chunk for chunk in result.chunks if chunk.delegate == "javascript"
    )
    assert "console.log" in script_chunk.text
    assert script_chunk.parent_symbol_id == script_symbol.symbol_id
    script_shell = next(
        chunk
        for chunk in result.chunks
        if chunk.chunk_id.startswith("html:script:")
        and chunk.parent_symbol_id == script_symbol.symbol_id
    )
    assert script_chunk.chunk_id.startswith(
        "javascript:delegate:html:inline_script:"
    )
    assert script_chunk.metadata["delegate_parent_handler"] == "html"
    assert (
        script_chunk.metadata["delegate_parent_symbol"]
        == script_symbol.symbol_id
    )
    assert (
        script_chunk.metadata["delegate_parent_chunk"] == script_shell.chunk_id
    )

    style_chunk = next(
        chunk for chunk in result.chunks if chunk.delegate == "css"
    )
    assert "body" in style_chunk.text
    assert style_chunk.parent_symbol_id == style_symbol.symbol_id
    style_shell = next(
        chunk
        for chunk in result.chunks
        if chunk.chunk_id.startswith("html:style:")
        and chunk.parent_symbol_id == style_symbol.symbol_id
    )
    assert style_chunk.chunk_id.startswith("css:delegate:html:inline_style:")
    assert style_chunk.metadata["delegate_parent_handler"] == "html"
    assert (
        style_chunk.metadata["delegate_parent_symbol"] == style_symbol.symbol_id
    )
    assert style_chunk.metadata["delegate_parent_chunk"] == style_shell.chunk_id

    assert not result.warnings
