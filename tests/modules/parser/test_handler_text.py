"""Tests for the plain text parser handler."""

from __future__ import annotations

from pathlib import Path

from raggd.core.config import AppConfig, ParserModuleSettings
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.parser.handlers import ParseContext
from raggd.modules.parser.handlers.text import TextHandler
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
    logger = get_logger("test.text-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_paragraph_splitting(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = TextHandler(context=context)
    path = tmp_path / "sample.txt"
    path.write_text(
        "Paragraph one\n\nParagraph two\n\nParagraph three",
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    assert len(result.chunks) == 3

    texts = [chunk.text for chunk in result.chunks]
    assert texts[0].endswith("\n\n")
    assert texts[1].endswith("\n\n")
    assert texts[2] == "Paragraph three"

    assert result.chunks[0].metadata["strategy"] == "paragraph"
    assert result.chunks[0].metadata["start_line"] == 1
    assert result.chunks[0].metadata["end_line"] == 2


def test_indentation_fallback(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = TextHandler(context=context)
    path = tmp_path / "config.txt"
    path.write_text(
        "section_a:\n  key: 1\n  nested: true\nsection_b:\n  key: 2\n",
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    assert len(result.chunks) == 2
    assert all(chunk.metadata["strategy"] == "indentation" for chunk in result.chunks)

    first, second = result.chunks
    assert "section_a:" in first.text
    assert "section_b:" in second.text
    assert first.metadata["start_line"] == 1
    assert second.metadata["start_line"] > first.metadata["start_line"]


def test_single_chunk_when_heuristics_fail(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    handler = TextHandler(context=context)
    path = tmp_path / "single.txt"
    path.write_text("no obvious chunk markers", encoding="utf-8")

    result = handler.parse(path=path, context=context)

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.text == "no obvious chunk markers"
    assert chunk.metadata["strategy"] == "fallback"
    assert chunk.start_offset == 0
    assert chunk.end_offset == len("no obvious chunk markers")
