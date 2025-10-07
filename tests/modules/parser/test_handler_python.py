"""Tests for the Python parser handler."""

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
from raggd.modules.parser.handlers.python import PythonHandler
from raggd.modules.parser.tokenizer import TokenEncoder


class _DummyEncoding:
    def encode(
        self, text: str, *, allowed_special: set[str] | None = None
    ) -> list[int]:
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


def _make_context(
    tmp_path: Path,
    *,
    handler_settings: ParserHandlerSettings | None = None,
) -> ParseContext:
    workspace = _make_workspace(tmp_path)
    config = AppConfig()
    settings = ParserModuleSettings()
    if handler_settings is not None:
        overrides = dict(settings.handlers)
        overrides["python"] = handler_settings
        settings = settings.model_copy(update={"handlers": overrides})
    token_encoder = TokenEncoder(name="dummy", encoding=_DummyEncoding())
    logger = get_logger("test.python-handler")

    return ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )


def test_python_handler_reports_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "raggd.modules.parser.handlers.python._load_libcst",
        lambda context: None,
    )

    context = _make_context(tmp_path)
    handler = PythonHandler(context=context)
    path = tmp_path / "example.py"
    path.write_text("print('hello')\n", encoding="utf-8")

    result = handler.parse(path=path, context=context)

    assert not result.chunks
    assert result.errors
    message = " ".join(result.errors)
    assert "libcst" in message.lower() or "parser" in message.lower()


def test_python_handler_extracts_symbols(tmp_path: Path) -> None:
    pytest.importorskip("libcst")

    context = _make_context(tmp_path)
    handler = PythonHandler(context=context)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    path = pkg_dir / "sample.py"
    path.write_text(
        dedent(
            '''
            """Module docstring."""

            import os


            class Foo(Base):
                """Class docstring."""

                @decorator
                def method(self, value: int) -> str:
                    """Method docstring."""
                    return str(value)


            def top_func():
                """Function docstring."""
                return None
            '''
        ),
        encoding="utf-8",
    )

    result = handler.parse(path=path, context=context)

    assert result.file.metadata.get("module_name") == "pkg.sample"
    assert result.file.metadata.get("docstring") == "Module docstring."

    module_symbol = next(sym for sym in result.symbols if sym.kind == "module")
    class_symbol = next(sym for sym in result.symbols if sym.kind == "class")
    method_symbol = next(sym for sym in result.symbols if sym.name == "method")
    function_symbol = next(
        sym for sym in result.symbols if sym.name == "top_func"
    )

    assert class_symbol.parent_id == module_symbol.symbol_id
    assert function_symbol.parent_id == module_symbol.symbol_id
    assert method_symbol.parent_id == class_symbol.symbol_id

    assert class_symbol.docstring == "Class docstring."
    assert method_symbol.docstring == "Method docstring."

    method_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.parent_symbol_id == method_symbol.symbol_id
    ]
    assert method_chunks
    assert any("def method" in chunk.text for chunk in method_chunks)

    module_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.metadata.get("kind") == "module_docstring"
    ]
    assert module_chunks
    assert "Module docstring." in module_chunks[0].text


def test_python_handler_splits_long_functions(tmp_path: Path) -> None:
    pytest.importorskip("libcst")

    handler_settings = ParserHandlerSettings(max_tokens=40)
    context = _make_context(tmp_path, handler_settings=handler_settings)
    handler = PythonHandler(context=context)
    path = tmp_path / "overflow.py"

    long_body = "\n".join(["    value += %d" % index for index in range(20)])
    docstring = '    """Docstring"""'
    contents = "\n".join(
        [
            "def huge(value: int) -> int:",
            docstring,
            long_body,
            "    return value",
            "",
        ]
    )
    path.write_text(contents, encoding="utf-8")

    result = handler.parse(path=path, context=context)

    func_symbol = next(sym for sym in result.symbols if sym.name == "huge")
    func_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.parent_symbol_id == func_symbol.symbol_id
    ]

    assert len(func_chunks) > 1
    assert all(chunk.metadata.get("overflow") for chunk in func_chunks)
    assert {chunk.part_index for chunk in func_chunks} == set(
        range(len(func_chunks))
    )
    assert any("split into" in warning for warning in result.warnings)
