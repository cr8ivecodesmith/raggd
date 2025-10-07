"""Tests for the CSS parser handler."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from raggd.core.config import (
    AppConfig,
    ParserHandlerSettings,
    ParserModuleSettings,
)
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules.parser.handlers import ParseContext
from raggd.modules.parser.handlers.css import CSSHandler
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


def _make_context(
    tmp_path: Path, *, max_tokens: int | None = None
) -> ParseContext:
    workspace = _make_workspace(tmp_path)
    config = AppConfig()
    base_settings = ParserModuleSettings()
    if max_tokens is not None:
        handlers = dict(base_settings.handlers)
        handlers["css"] = ParserHandlerSettings(max_tokens=max_tokens)
        settings = base_settings.model_copy(update={"handlers": handlers})
    else:
        settings = base_settings
    token_encoder = TokenEncoder(name="dummy", encoding=_DummyEncoding())
    logger = get_logger("test.css-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_css_handler_emits_rules_and_splits_multi_selectors(
    tmp_path: Path,
) -> None:
    pytest.importorskip("tree_sitter_languages")

    context = _make_context(tmp_path, max_tokens=30)
    handler = CSSHandler(context=context)
    path = tmp_path / "styles.css"
    path.write_text(
        dedent(
            """
            /* Primary theme */
            body {
              margin: 0;
            }

            @media screen and (min-width: 640px) {
              .btn-primary, .btn-secondary {
                padding: 0.75rem 1.25rem;
                display: inline-flex;
              }
            }

            @keyframes fade-in {
              from { opacity: 0; }
              to { opacity: 1; }
            }
            """
        ),
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    assert any(symbol.kind == "stylesheet" for symbol in result.symbols)

    media_symbol = next(
        symbol
        for symbol in result.symbols
        if symbol.kind == "at_rule" and "@media" in symbol.name
    )
    assert "screen" in media_symbol.name

    keyframe_symbol = next(
        symbol for symbol in result.symbols if symbol.kind == "keyframe"
    )
    assert "from" in keyframe_symbol.name

    comment_chunk = next(
        chunk
        for chunk in result.chunks
        if chunk.metadata.get("kind") == "comment"
    )
    assert "Primary theme" in comment_chunk.text

    rule_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.metadata.get("kind") == "rule"
        and chunk.metadata.get("selector")
    ]
    selectors = {chunk.metadata["selector"] for chunk in rule_chunks}
    assert selectors == {".btn-primary", ".btn-secondary"}

    assert not result.warnings
