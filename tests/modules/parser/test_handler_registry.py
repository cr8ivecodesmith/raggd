"""Tests for the parser handler registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from raggd.core.config import ParserHandlerSettings, ParserModuleSettings
from raggd.modules import HealthStatus
from raggd.modules.parser.registry import (
    HandlerProbeResult,
    HandlerRegistry,
    ParserHandlerDescriptor,
    normalize_shebang,
)


def _build_registry(settings: ParserModuleSettings) -> HandlerRegistry:
    descriptors = (
        ParserHandlerDescriptor(
            name="text",
            version="test",
            display_name="Text",
        ),
        ParserHandlerDescriptor(
            name="python",
            version="test",
            display_name="Python",
            extensions=("py",),
            shebangs=("python", "python3"),
            probe=lambda: HandlerProbeResult(status=HealthStatus.OK),
        ),
        ParserHandlerDescriptor(
            name="markdown",
            version="test",
            display_name="Markdown",
            extensions=("md",),
            probe=lambda: HandlerProbeResult(status=HealthStatus.OK),
        ),
    )
    return HandlerRegistry(descriptors=descriptors, settings=settings)


def test_shebang_normalization_handles_env_variants() -> None:
    assert normalize_shebang("#!/usr/bin/env python3 -m") == "python3"
    assert normalize_shebang("#! /usr/bin/python") == "python"
    assert normalize_shebang("#!/bin/bash") == "bash"
    assert normalize_shebang("not-a-shebang") == "not-a-shebang"


def test_registry_selection_precedence(tmp_path: Path) -> None:
    settings = ParserModuleSettings()
    registry = _build_registry(settings)

    target = tmp_path / "project" / "script.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('hello')", encoding="utf-8")

    registry.register_path_override(target, "markdown")

    selection = registry.resolve(target)
    assert selection.handler.name == "markdown"
    assert selection.resolved_via == "override"

    selection = registry.resolve(target, explicit="python")
    assert selection.handler.name == "python"
    assert selection.resolved_via == "explicit"

    registry.remove_path_override(target)
    selection = registry.resolve(target, shebang="#!/usr/bin/env python3")
    assert selection.handler.name == "python"
    assert selection.resolved_via.startswith("shebang")

    selection = registry.resolve(tmp_path / "readme.md")
    assert selection.handler.name == "markdown"
    assert selection.resolved_via.startswith("extension")


def test_registry_fallback_when_handler_disabled(tmp_path: Path) -> None:
    settings = ParserModuleSettings()
    overrides = dict(settings.handlers)
    overrides["python"] = ParserHandlerSettings(enabled=False)
    settings = settings.model_copy(update={"handlers": overrides})

    registry = _build_registry(settings)
    path = tmp_path / "main.py"
    selection = registry.resolve(path)

    assert selection.handler.name == "text"
    assert selection.fallback is True
    assert selection.resolved_via == "fallback:disabled"


def test_registry_fallback_when_dependency_missing(tmp_path: Path) -> None:
    settings = ParserModuleSettings()

    descriptors = (
        ParserHandlerDescriptor(
            name="text",
            version="test",
            display_name="Text",
        ),
        ParserHandlerDescriptor(
            name="markdown",
            version="test",
            display_name="Markdown",
            extensions=("md",),
            probe=lambda: HandlerProbeResult(
                status=HealthStatus.ERROR,
                summary="tree-sitter missing",
            ),
        ),
    )

    registry = HandlerRegistry(descriptors=descriptors, settings=settings)
    path = tmp_path / "README.md"
    selection = registry.resolve(path)
    assert selection.handler.name == "text"
    assert selection.fallback is True
    assert selection.resolved_via == "fallback:dependency"


def test_registry_unknown_explicit_handler() -> None:
    settings = ParserModuleSettings()
    registry = _build_registry(settings)
    with pytest.raises(KeyError):
        registry.resolve(Path("irrelevant.txt"), explicit="unknown")
