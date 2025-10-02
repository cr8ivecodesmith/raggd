"""Tests for the :mod:`raggd.__main__` entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from raggd.__main__ import main


def test_main_invokes_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    monkeypatch.setenv("RAGGD_WORKSPACE", str(workspace))
    monkeypatch.setenv("RAGGD_LOG_LEVEL", "warning")
    monkeypatch.setattr(sys, "argv", ["raggd", "init"])
    monkeypatch.setattr("raggd.cli._detect_available_extras", lambda: set())

    configured: dict[str, object] = {}

    def fake_configure_logging(*, level: str, workspace_path: Path, console=None) -> None:
        configured["level"] = level
        configured["workspace"] = workspace_path

    monkeypatch.setattr("raggd.cli.configure_logging", fake_configure_logging)

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0

    assert workspace.exists()
    assert configured["level"] == "WARNING"
    assert configured["workspace"] == workspace
