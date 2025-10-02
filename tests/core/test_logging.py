"""Tests for :mod:`raggd.core.logging`."""

from __future__ import annotations

import gzip
import io
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest
from rich.console import Console
from rich.logging import RichHandler

from raggd.core.logging import configure_logging, get_logger


def _clear_root_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive guard
            pass


@pytest.fixture(autouse=True)
def reset_logging_state():
    """Ensure each test runs with a clean logging configuration."""

    _clear_root_handlers()
    yield
    _clear_root_handlers()


def _build_console() -> Console:
    """Return a console that writes to an in-memory buffer for tests."""

    buffer = io.StringIO()
    return Console(file=buffer, width=120, record=True)


def test_configure_logging_installs_console_and_file_handlers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    configure_logging(level="debug", workspace_path=workspace, console=_build_console())

    root = logging.getLogger()
    rich_handlers = [h for h in root.handlers if isinstance(h, RichHandler)]
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]

    assert len(rich_handlers) == 1, "Expected a single Rich console handler"
    assert len(file_handlers) == 1, "Expected a file handler for workspace logs"

    file_handler = file_handlers[0]
    log_file = Path(file_handler.baseFilename)
    logger = get_logger(__name__, feature="bootstrap")
    logger.info("workspace-init", action="create")

    for handler in root.handlers:
        handler.flush()

    assert log_file.exists()

    contents = log_file.read_text(encoding="utf-8").strip()
    payload = json.loads(contents)

    assert payload["event"] == "workspace-init"
    assert payload["feature"] == "bootstrap"
    assert payload["action"] == "create"


def test_configure_logging_without_workspace_omits_file_handler() -> None:
    configure_logging(level="info", console=_build_console())

    root = logging.getLogger()
    assert any(isinstance(h, RichHandler) for h in root.handlers)
    assert all(
        not isinstance(h, TimedRotatingFileHandler) for h in root.handlers
    ), "No file handler should be registered when workspace_path is absent"


def test_configure_logging_rejects_unknown_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        configure_logging(
            level="invalid",
            workspace_path=tmp_path / "workspace",
            console=_build_console(),
        )


def test_configure_logging_rotates_with_compression(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    configure_logging(level="warning", workspace_path=workspace, console=_build_console())
    root = logging.getLogger()
    file_handler = next(
        h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)
    )

    logger = get_logger("rotate", task="rotation")
    logger.warning("pre-rotation", sample=True)

    for handler in root.handlers:
        handler.flush()

    # Force a rollover and ensure gzip compression occurs.
    file_handler.doRollover()

    gz_files = sorted((workspace / "logs").glob("raggd.log.*.gz"))
    assert gz_files, "Expected a compressed log archive after rollover"

    with gzip.open(gz_files[-1], "rt", encoding="utf-8") as fh:
        archived = fh.read()

    assert "pre-rotation" in archived
    assert "task" in archived
