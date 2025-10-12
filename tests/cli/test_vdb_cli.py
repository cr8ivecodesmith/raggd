from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import typer

from raggd.cli import vdb as vdb_cli
from raggd.core.paths import WorkspacePaths
from raggd.source.config import SourceConfigError


class StubLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.events.append(("warning", {"message": message, **kwargs}))

    def info(self, message: str, **kwargs: Any) -> None:
        self.events.append(("info", {"message": message, **kwargs}))

    def debug(self, message: str, **kwargs: Any) -> None:
        self.events.append(("debug", {"message": message, **kwargs}))

    def error(self, message: str, **kwargs: Any) -> None:
        self.events.append(("error", {"message": message, **kwargs}))

    def bind(self, **kwargs: Any) -> "StubLogger":
        # Typer wiring binds component names on the logger; reuse the same stub.
        return self


class DummyContext:
    def __init__(self, obj: Any | None = None) -> None:
        self.obj = obj


def _build_context(tmp_path, service: Any) -> tuple[DummyContext, StubLogger]:
    paths = WorkspacePaths(
        workspace=tmp_path,
        config_file=tmp_path / "raggd.toml",
        logs_dir=tmp_path / "logs",
        archives_dir=tmp_path / "archives",
        sources_dir=tmp_path / "sources",
    )
    config = SimpleNamespace(log_level="INFO", workspace=paths.workspace)
    store = SimpleNamespace()
    logger = StubLogger()
    ctx = DummyContext()
    ctx.obj = vdb_cli.VdbCLIContext(
        paths=paths,
        config=config,
        store=store,
        service=service,
        logger=logger,
    )
    return ctx, logger


def test_require_context_without_obj_exits(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = DummyContext()

    with pytest.raises(typer.Exit) as excinfo:
        vdb_cli._require_context(ctx)

    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert "context not initialized" in captured.out


def test_handle_not_implemented_logs_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = StubLogger()

    vdb_cli._handle_not_implemented("sync", logger=logger)

    captured = capsys.readouterr()
    assert "not implemented yet" in captured.out
    assert any(level == "warning" for level, _ in logger.events)


def test_configure_vdb_commands_handles_workspace_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = DummyContext()

    def raise_value_error(workspace):
        raise ValueError("invalid workspace")

    monkeypatch.setattr(vdb_cli, "_resolve_workspace_override", raise_value_error)

    with pytest.raises(typer.Exit) as excinfo:
        vdb_cli.configure_vdb_commands(ctx, workspace=None, log_level=None)

    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Workspace error" in captured.out


def test_configure_vdb_commands_requires_config_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = DummyContext()
    paths = WorkspacePaths(
        workspace=tmp_path,
        config_file=tmp_path / "missing.toml",
        logs_dir=tmp_path / "logs",
        archives_dir=tmp_path / "archives",
        sources_dir=tmp_path / "sources",
    )
    monkeypatch.setattr(vdb_cli, "_resolve_workspace_override", lambda workspace: paths)

    with pytest.raises(typer.Exit) as excinfo:
        vdb_cli.configure_vdb_commands(ctx, workspace=None, log_level=None)

    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Workspace config not found" in captured.out


def test_configure_vdb_commands_reports_load_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = DummyContext()
    config_file = tmp_path / "raggd.toml"
    config_file.write_text("# dummy config\n", encoding="utf-8")
    paths = WorkspacePaths(
        workspace=tmp_path,
        config_file=config_file,
        logs_dir=tmp_path / "logs",
        archives_dir=tmp_path / "archives",
        sources_dir=tmp_path / "sources",
    )
    monkeypatch.setattr(vdb_cli, "_resolve_workspace_override", lambda workspace: paths)

    def raise_source_error(self):
        raise SourceConfigError("broken config")

    monkeypatch.setattr(vdb_cli.SourceConfigStore, "load", raise_source_error, raising=False)

    with pytest.raises(typer.Exit) as excinfo:
        vdb_cli.configure_vdb_commands(ctx, workspace=None, log_level=None)

    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Failed to load workspace config" in captured.out


def test_info_command_not_implemented(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class NotImplementedInfoService:
        def info(self, *, source, vdb):
            raise NotImplementedError("pending")

    service = NotImplementedInfoService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.info_vdb(ctx, source="demo", vdb=None, json_output=False)

    captured = capsys.readouterr()
    assert "not implemented yet" in captured.out
    assert any(level == "warning" for level, _ in logger.events)
    assert any(level == "debug" for level, _ in logger.events)


def test_info_command_renders_records(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def info(self, *, source, vdb):
            self.calls.append(("info", (source, vdb)))
            return (
                {
                    "selector": "demo:primary",
                    "status": "ok",
                    "vectors": 42,
                },
            )

    service = RecordingService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.info_vdb(ctx, source="demo", vdb=None, json_output=False)

    captured = capsys.readouterr()
    assert "VDB demo:primary" in captured.out
    assert "status: ok" in captured.out
    assert ("info", {"message": "vdb-info", "source": "demo", "vdb": None, "json": False, "count": 1}) in logger.events
    assert service.calls == [("info", ("demo", None))]


def test_info_command_json_output(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class JsonService:
        def info(self, *, source, vdb):
            return ({"selector": "demo:primary"},)

    service = JsonService()
    ctx, _logger = _build_context(tmp_path, service)

    vdb_cli.info_vdb(ctx, source=None, vdb=None, json_output=True)

    captured = capsys.readouterr()
    assert '"demo:primary"' in captured.out


def test_create_command_not_implemented(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class NotImplementedCreateService:
        def create(self, *, selector, name, model):
            raise NotImplementedError("pending")

    service = NotImplementedCreateService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.create_vdb(
        ctx,
        selector="demo@batch-001",
        name="primary",
        model="stub:model-a",
    )

    captured = capsys.readouterr()
    assert "not implemented yet" in captured.out
    assert any(level == "warning" for level, _ in logger.events)


def test_create_command_reports_success(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def create(self, *, selector, name, model):
            self.calls.append(("create", (selector, name, model)))

    service = RecordingService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.create_vdb(
        ctx,
        selector="demo@batch-001",
        name="primary",
        model="stub:model-a",
    )

    captured = capsys.readouterr()
    assert "Created VDB primary for demo@batch-001" in captured.out
    assert service.calls == [("create", ("demo@batch-001", "primary", "stub:model-a"))]
    assert any(level == "info" for level, _ in logger.events)


def test_sync_command_bad_parameter(tmp_path) -> None:
    ctx = DummyContext()

    with pytest.raises(typer.BadParameter):
        vdb_cli.sync_vdb(
            ctx,
            source="demo",
            vdb=None,
            missing_only=True,
            recompute=True,
            limit=None,
            concurrency=None,
            dry_run=False,
        )


def test_sync_command_not_implemented(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class NotImplementedSyncService:
        def sync(
            self,
            *,
            source,
            vdb,
            missing_only,
            recompute,
            limit,
            concurrency,
            dry_run,
        ):
            raise NotImplementedError("pending")

    service = NotImplementedSyncService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.sync_vdb(
        ctx,
        source="demo",
        vdb=None,
        missing_only=False,
        recompute=False,
        limit=None,
        concurrency=None,
        dry_run=False,
    )

    captured = capsys.readouterr()
    assert "not implemented yet" in captured.out
    assert any(level == "warning" for level, _ in logger.events)


def test_sync_command_renders_summary(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def sync(
            self,
            *,
            source,
            vdb,
            missing_only,
            recompute,
            limit,
            concurrency,
            dry_run,
        ):
            self.calls.append(
                (
                    "sync",
                    (
                        source,
                        vdb,
                        missing_only,
                        recompute,
                        limit,
                        concurrency,
                        dry_run,
                    ),
                )
            )
            return {"source": source, "vdb": vdb or "all"}

    service = RecordingService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.sync_vdb(
        ctx,
        source="demo",
        vdb="primary",
        missing_only=False,
        recompute=False,
        limit=None,
        concurrency="auto",
        dry_run=True,
    )

    captured = capsys.readouterr()
    assert "VDB sync complete" in captured.out
    assert "source: demo" in captured.out
    assert service.calls == [
        ("sync", ("demo", "primary", False, False, None, "auto", True))
    ]
    assert any(level == "info" for level, _ in logger.events)


def test_reset_command_not_implemented(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class NotImplementedResetService:
        def reset(self, *, source, vdb, drop, force):
            raise NotImplementedError("pending")

    service = NotImplementedResetService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.reset_vdb(
        ctx,
        source="demo",
        vdb=None,
        drop=False,
        force=False,
    )

    captured = capsys.readouterr()
    assert "not implemented yet" in captured.out
    assert any(level == "warning" for level, _ in logger.events)


def test_reset_command_renders_summary(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def reset(self, *, source, vdb, drop, force):
            self.calls.append(("reset", (source, vdb, drop, force)))
            return {"source": source, "drop": drop, "force": force}

    service = RecordingService()
    ctx, logger = _build_context(tmp_path, service)

    vdb_cli.reset_vdb(
        ctx,
        source="demo",
        vdb="primary",
        drop=True,
        force=True,
    )

    captured = capsys.readouterr()
    assert "VDB reset complete" in captured.out
    assert "force: True" in captured.out
    assert service.calls == [("reset", ("demo", "primary", True, True))]
    assert any(level == "info" for level, _ in logger.events)
