"""Tests for handler scaffolding utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from raggd.core.config import AppConfig, ParserHandlerSettings, ParserModuleSettings
from raggd.core.logging import get_logger
from raggd.core.paths import WorkspacePaths
from raggd.modules import HealthStatus
from raggd.modules.parser.handlers import (
    HandlerCache,
    ParseContext,
)
from raggd.modules.parser.handlers.base import HandlerFile, HandlerResult
from raggd.modules.parser.handlers.text import TextHandler
from raggd.modules.parser.registry import (
    HandlerFactoryError,
    build_default_registry,
    import_dependency_probe,
)
from raggd.modules.parser.tokenizer import TokenEncoder


class _DummyEncoding:
    def encode(self, text: str, *, allowed_special: set[str] | None = None) -> list[int]:
        return [0] * len(text)


def _make_workspace(tmp_path: Path) -> WorkspacePaths:
    workspace = tmp_path / "workspace"
    config_file = workspace / "raggd.toml"
    logs_dir = workspace / "logs"
    archives_dir = workspace / "archives"
    sources_dir = workspace / "sources"
    return WorkspacePaths(
        workspace=workspace,
        config_file=config_file,
        logs_dir=logs_dir,
        archives_dir=archives_dir,
        sources_dir=sources_dir,
    )


def test_handler_cache_memoizes_values() -> None:
    cache = HandlerCache()
    calls = 0

    def factory() -> object:
        nonlocal calls
        calls += 1
        return object()

    first = cache.get("alpha", factory)
    second = cache.get("alpha", factory)
    assert first is second
    assert calls == 1

    cache.set("alpha", "override")
    assert cache.get("alpha", factory) == "override"

    cache.clear()
    reset = cache.get("alpha", factory)
    assert reset is not first
    assert reset is not second
    assert calls == 2


def test_parse_context_handler_max_tokens(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    config = AppConfig()
    settings = ParserModuleSettings()
    token_encoder = TokenEncoder(name="dummy", encoding=_DummyEncoding())
    logger = get_logger("test.parse-context")

    context = ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=logger,
    )

    assert context.handler_max_tokens("text") == settings.general_max_tokens

    overrides = dict(settings.handlers)
    overrides["text"] = ParserHandlerSettings(max_tokens="auto")
    updated_settings = settings.model_copy(update={"handlers": overrides})

    context = ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=updated_settings,
        token_encoder=token_encoder,
        logger=logger,
    )

    assert context.handler_max_tokens("text") == "auto"


def test_import_dependency_probe_reports_missing() -> None:
    probe = import_dependency_probe("definitely_missing_module_123")
    result = probe()
    assert result.status is HealthStatus.ERROR
    assert "Missing dependency" in (result.summary or "")


def test_default_registry_resolves_text_handler_factory(tmp_path: Path) -> None:
    settings = ParserModuleSettings()
    registry = build_default_registry(settings)
    factory = registry.handler_factory("text")

    workspace = _make_workspace(tmp_path)
    config = AppConfig()
    token_encoder = TokenEncoder(name="dummy", encoding=_DummyEncoding())
    context = ParseContext(
        source="alpha",
        root=tmp_path,
        workspace=workspace,
        config=config,
        settings=settings,
        token_encoder=token_encoder,
        logger=get_logger("test.text-handler"),
    )

    handler = factory(context)
    assert isinstance(handler, TextHandler)
    result = handler.parse(path=tmp_path / "placeholder.txt", context=context)
    assert isinstance(result, HandlerResult)
    assert isinstance(result.file, HandlerFile)
    assert result.file.language == "text"

    with pytest.raises(HandlerFactoryError):
        registry.handler_factory("unknown")
