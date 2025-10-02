"""Tests for :mod:`raggd.modules.registry`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from raggd.core.config import ModuleToggle
from raggd.modules.registry import ModuleDescriptor, ModuleRegistry


@dataclass(slots=True)
class TrackingDescriptor(ModuleDescriptor):
    """Descriptor variant that records whether ``emit`` was invoked."""

    emitted: bool = field(default=False, init=False)

    def emit(self) -> None:  # pragma: no cover - exercised indirectly
        object.__setattr__(self, "emitted", True)


def test_registry_evaluate_applies_toggles_and_extras() -> None:
    descriptors = [
        TrackingDescriptor(
            name="file-monitoring",
            description="File system watchers",
            extras=("file-monitoring",),
            default_toggle=ModuleToggle(enabled=True, extras=("file-monitoring",)),
        ),
        ModuleDescriptor(
            name="local-embeddings",
            description="Local embedding generation",
            extras=("local-embeddings",),
            default_toggle=ModuleToggle(enabled=True, extras=("local-embeddings",)),
        ),
        ModuleDescriptor(
            name="rag",
            description="Core retrieval pipeline",
            extras=("rag",),
            default_toggle=ModuleToggle(enabled=True, extras=("rag",)),
        ),
    ]

    registry = ModuleRegistry(descriptors)

    status: dict[str, str] = {}
    results = registry.evaluate(
        toggles={
            "file-monitoring": ModuleToggle(
                enabled=True, extras=("file-monitoring",)
            ),
            "local-embeddings": ModuleToggle(
                enabled=False, extras=("local-embeddings",)
            ),
            "rag": ModuleToggle(enabled=True, extras=("rag",)),
        },
        available_extras={"file-monitoring"},
        status_sink=status,
    )

    assert results["file-monitoring"] is True
    assert descriptors[0].emitted is True
    assert status["file-monitoring"] == "enabled"

    assert results["local-embeddings"] is False
    assert status["local-embeddings"] == "disabled via configuration"

    assert results["rag"] is False
    assert status["rag"] == "missing extras: rag"


def test_registry_records_unknown_modules_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    records: dict[str | None, list[tuple[str, str, dict[str, Any]]]] = {}

    class Recorder:
        def __init__(self, module: str | None) -> None:
            self.module = module
            records.setdefault(module, [])

        def info(self, event: str, **context: Any) -> None:
            records[self.module].append(("info", event, context))

        def warning(self, event: str, **context: Any) -> None:
            records[self.module].append(("warning", event, context))

        def exception(self, event: str, **context: Any) -> None:  # pragma: no cover
            records[self.module].append(("exception", event, context))

    loggers: dict[str | None, Recorder] = {}

    def fake_get_logger(name: str | None = None, **context: Any) -> Recorder:
        module = context.get("module")
        logger = loggers.get(module)
        if logger is None:
            logger = Recorder(module)
            loggers[module] = logger
        return logger

    monkeypatch.setattr("raggd.modules.registry.get_logger", fake_get_logger)

    descriptor = ModuleDescriptor(name="alpha", description="Test descriptor")
    registry = ModuleRegistry([descriptor])

    status: dict[str, str] = {}
    results = registry.evaluate(
        toggles={"ghost": ModuleToggle(enabled=True)},
        available_extras=set(),
        status_sink=status,
    )

    assert results["alpha"] is True
    assert status["alpha"] == "enabled"
    assert status["ghost"] == "unknown module"

    ghost_events = records.get("ghost")
    assert ghost_events is not None
    assert ("warning", "module-unknown", {"enabled": False}) in ghost_events

    alpha_events = records.get("alpha")
    assert alpha_events is not None
    assert any(event == "module-evaluated" for _, event, _ in alpha_events)


def test_registry_rejects_duplicate_descriptors() -> None:
    descriptor = ModuleDescriptor(name="dup", description="duplicate")
    with pytest.raises(ValueError):
        ModuleRegistry([descriptor, ModuleDescriptor(name="dup", description="copy")])
