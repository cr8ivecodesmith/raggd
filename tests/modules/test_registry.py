"""Tests for :mod:`raggd.modules.registry`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pytest

from raggd.core.config import ModuleToggle
from raggd.modules.registry import (
    HealthRegistry,
    HealthReport,
    HealthStatus,
    ModuleDescriptor,
    ModuleRegistry,
)


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
            default_toggle=ModuleToggle(
                enabled=True,
                extras=("file-monitoring",),
            ),
        ),
        ModuleDescriptor(
            name="local-embeddings",
            description="Local embedding generation",
            extras=("local-embeddings",),
            default_toggle=ModuleToggle(
                enabled=True,
                extras=("local-embeddings",),
            ),
        ),
        ModuleDescriptor(
            name="rag",
            description="Core retrieval pipeline",
            extras=("rag",),
            default_toggle=ModuleToggle(
                enabled=True,
                extras=("rag",),
            ),
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


def test_registry_records_unknown_modules_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: dict[str | None, list[tuple[str, str, dict[str, Any]]]] = {}

    class Recorder:
        def __init__(self, module: str | None) -> None:
            self.module = module
            records.setdefault(module, [])

        def info(self, event: str, **context: Any) -> None:
            records[self.module].append(("info", event, context))

        def warning(self, event: str, **context: Any) -> None:
            records[self.module].append(("warning", event, context))

        def exception(
            self,
            event: str,
            **context: Any,
        ) -> None:  # pragma: no cover
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
        ModuleRegistry(
            [descriptor, ModuleDescriptor(name="dup", description="copy")]
        )


def test_descriptor_post_init_applies_default_extras() -> None:
    descriptor = ModuleDescriptor(
        name="gamma",
        description="Test descriptor",
        extras=("gamma", "gamma"),
        default_toggle=ModuleToggle(enabled=True),
    )

    assert descriptor.default_toggle.extras == ("gamma",)


def test_descriptor_is_available_handles_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ensure fixture registers without triggering unused warning
    del monkeypatch
    descriptor = ModuleDescriptor(
        name="delta",
        description="Delta module",
        extras=("delta",),
    )

    assert descriptor.is_available({"delta"}) is True
    assert (
        descriptor.is_available(
            None,
            override=ModuleToggle(enabled=True, extras=("delta",)),
        )
        is False
    )

    no_extra = ModuleDescriptor(name="plain", description="Plain module")
    assert no_extra.is_available(None) is True


def test_registry_iter_descriptors_returns_sequence() -> None:
    descriptor = ModuleDescriptor(name="epsilon", description="Epsilon")
    registry = ModuleRegistry([descriptor])

    items = list(registry.iter_descriptors())
    assert items == [descriptor]


def test_health_registry_exposes_hooks_in_declaration_order() -> None:
    def alpha_hook(handle: object) -> Sequence[HealthReport]:
        return ()  # pragma: no cover

    def gamma_hook(handle: object) -> Sequence[HealthReport]:
        return ()  # pragma: no cover

    descriptors = (
        ModuleDescriptor(
            name="alpha",
            description="Alpha",
            health_hook=alpha_hook,
        ),
        ModuleDescriptor(
            name="beta",
            description="Beta",
        ),
        ModuleDescriptor(
            name="gamma",
            description="Gamma",
            health_hook=gamma_hook,
        ),
    )

    registry = ModuleRegistry(descriptors)
    health = registry.health_registry()

    assert isinstance(health, HealthRegistry)
    assert list(health) == ["alpha", "gamma"]
    assert health["alpha"] is alpha_hook
    assert health.get("gamma") is gamma_hook
    assert "beta" not in health
    assert list(health.iter_hooks()) == [
        ("alpha", alpha_hook),
        ("gamma", gamma_hook),
    ]


def test_health_report_normalizes_fields() -> None:
    report = HealthReport(
        name="  demo  ",
        status=HealthStatus.OK,
        summary="  All good ",
        actions=("  act  ",),
    )

    assert report.name == "demo"
    assert report.summary == "All good"
    assert report.actions == ("act",)
