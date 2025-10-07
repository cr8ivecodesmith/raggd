"""Tests for the Markdown parser handler."""

from __future__ import annotations

from pathlib import Path

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.parser.handlers import ParseContext
from raggd.modules.parser.handlers.markdown import MarkdownHandler
from raggd.modules.parser.tokenizer import TokenEncoder


class _DummyEncoding:
    def encode(self, text: str, *, allowed_special: set[str] | None = None) -> list[int]:
        return [0] * len(text)


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
    logger = get_logger("test.markdown-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_markdown_front_matter_and_headings(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = MarkdownHandler(context=context)
    path = tmp_path / "doc.md"
    path.write_text(
        """---
title: Demo
tags: [alpha, beta]
---

Intro paragraph.

# Heading One
Content under heading one.

## Child Heading
Child section content.
""",
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    assert result.file.metadata.get("front_matter", "").startswith("---")

    assert result.chunks[0].metadata.get("kind") == "front_matter"

    section_chunks = [chunk for chunk in result.chunks if chunk.metadata.get("kind") == "section"]
    assert len(section_chunks) == 2

    first_section = section_chunks[0]
    assert "Intro paragraph." in first_section.text
    assert first_section.metadata.get("intro_attached") is True

    assert len(result.symbols) == 2
    root_symbol = result.symbols[0]
    child_symbol = result.symbols[1]
    assert child_symbol.parent_id == root_symbol.symbol_id
    assert section_chunks[1].parent_symbol_id == child_symbol.symbol_id


def test_markdown_fenced_code_delegation(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = MarkdownHandler(context=context)
    path = tmp_path / "code.md"
    path.write_text(
        """# Heading

```python
print('hi')
```

Trailing text.
""",
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    code_chunks = [chunk for chunk in result.chunks if chunk.metadata.get("kind") == "fenced_code"]
    assert len(code_chunks) == 1
    code_chunk = code_chunks[0]
    assert code_chunk.delegate == "python"
    assert "print('hi')" in code_chunk.text

    section_chunk = next(chunk for chunk in result.chunks if chunk.metadata.get("kind") == "section")
    assert code_chunk.chunk_id.startswith("python:delegate:markdown:fenced_code:")
    assert code_chunk.metadata["delegate_parent_handler"] == "markdown"
    assert code_chunk.metadata["delegate_parent_symbol"] == result.symbols[0].symbol_id
    assert code_chunk.metadata["delegate_parent_chunk"] == section_chunk.chunk_id

    assert result.symbols
    assert code_chunk.parent_symbol_id == result.symbols[0].symbol_id


def test_markdown_no_headings_falls_back(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = MarkdownHandler(context=context)
    path = tmp_path / "plain.md"
    path.write_text("Just a paragraph with no headings.", encoding="utf-8")

    result = handler.parse(path=path, context=context)

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.metadata.get("strategy") == "fallback"
    assert chunk.metadata.get("kind") == "body"
    assert not result.symbols
