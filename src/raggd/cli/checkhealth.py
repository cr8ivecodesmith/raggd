"""Typer command for aggregating module health status."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import tomllib
import typer

from raggd.core.config import AppConfig, load_config, load_packaged_defaults
from raggd.core.logging import Logger, configure_logging, get_logger
from raggd.core.paths import WorkspacePaths, resolve_workspace
from raggd.health import (
    HealthDocumentError,
    HealthDocumentStore,
    HealthModuleSnapshot,
    build_module_snapshot,
)
from raggd.modules import (
    HealthReport,
    HealthStatus,
    ModuleRegistry,
    WorkspaceHandle,
)


@dataclass(slots=True)
class _CLIWorkspaceHandle:
    """Concrete workspace handle passed into module health hooks."""

    paths: WorkspacePaths
    config: AppConfig


_STATUS_COLORS: dict[HealthStatus, str | None] = {
    HealthStatus.OK: typer.colors.GREEN,
    HealthStatus.UNKNOWN: typer.colors.YELLOW,
    HealthStatus.DEGRADED: typer.colors.BRIGHT_YELLOW,
    HealthStatus.ERROR: typer.colors.RED,
}

_COMMAND_NAME = "checkhealth"

_EXIT_CODES: dict[HealthStatus, int] = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.DEGRADED: 1,
    HealthStatus.ERROR: 2,
}


def _canonical_module_name(raw: str) -> str:
    return raw.strip().lower().replace("_", "-")


def _resolve_workspace(workspace: Path | None) -> WorkspacePaths:
    env_workspace = os.environ.get("RAGGD_WORKSPACE")
    env_override = Path(env_workspace).expanduser() if env_workspace else None
    return resolve_workspace(
        workspace_override=workspace,
        env_override=env_override,
    )


def _load_app_config(paths: WorkspacePaths) -> AppConfig:
    if not paths.config_file.exists():
        config_path = paths.config_file
        raise FileNotFoundError(
            (
                f"Workspace config not found at {config_path}. "
                "Run `raggd init` first."
            )
        )

    defaults = load_packaged_defaults()
    try:
        text = paths.config_file.read_text(encoding="utf-8")
    except OSError as exc:
        config_path = paths.config_file
        raise RuntimeError(
            (f"Failed to read config file {config_path}: {exc}")
        ) from exc

    if text:
        try:
            user_config = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            config_path = paths.config_file
            message = (
                f"Failed to parse config file {config_path}: TOML error: {exc}"
            )
            raise RuntimeError(message) from exc
    else:
        user_config = None

    return load_config(
        defaults=defaults,
        user_config=user_config,
        cli_overrides={"workspace": {"root": str(paths.workspace)}},
    )


def _select_modules(
    registry: ModuleRegistry,
    requested: Iterable[str],
) -> list[str]:
    hooks = registry.health_registry()
    available = list(hooks)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        name = _canonical_module_name(raw)
        if not name or name in seen:
            continue
        if name == _COMMAND_NAME:
            continue
        normalized.append(name)
        seen.add(name)

    if not normalized:
        return available

    unknown = [name for name in normalized if name not in hooks]
    if unknown:
        raise KeyError("Unknown module(s): " + ", ".join(sorted(unknown)))

    ordered = [name for name in available if name in seen]
    return ordered


def _status_color(status: HealthStatus) -> str | None:
    return _STATUS_COLORS.get(status)


def _render_timestamp(value: object) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[call-arg]
    return "never"


def _execute_hook(
    name: str,
    hook: callable[[WorkspaceHandle], Sequence[HealthReport]],
    handle: WorkspaceHandle,
    logger: Logger,
) -> tuple[Sequence[HealthReport], HealthModuleSnapshot]:
    try:
        reports = tuple(hook(handle))
    except Exception as exc:  # pragma: no cover - exercised in CLI integration
        logger.error(
            "checkhealth-hook-error",
            module=name,
            error=str(exc),
        )
        reports = (
            HealthReport(
                name=f"{name}-hook",
                status=HealthStatus.ERROR,
                summary=str(exc),
                actions=("Inspect logs for details.",),
                last_refresh_at=None,
            ),
        )

    snapshot = build_module_snapshot(reports)
    logger.info(
        "checkhealth-hook-complete",
        module=name,
        status=snapshot.status.value,
        detail_count=len(snapshot.details),
    )
    return reports, snapshot


def _emit_module_output(
    name: str,
    snapshot: HealthModuleSnapshot,
) -> None:
    header = f"{name}: {snapshot.status.value}"
    color = _status_color(snapshot.status)
    typer.secho(header, fg=color, bold=True)

    if not snapshot.details:
        typer.echo("  no health entries reported")
        return

    for detail in snapshot.details:
        detail_color = _status_color(detail.status)
        typer.secho(
            f"  - {detail.name}: {detail.status.value}",
            fg=detail_color,
        )
        if detail.summary:
            typer.echo(f"    summary: {detail.summary}")
        if detail.last_refresh_at is not None:
            typer.echo(
                "    last refresh: " + _render_timestamp(detail.last_refresh_at)
            )
        else:
            typer.echo("    last refresh: never")
        if detail.actions:
            typer.echo("    actions:")
            for action in detail.actions:
                typer.echo(f"      - {action}")


def _ensure_workspace_and_config(
    workspace: Path | None,
) -> tuple[WorkspacePaths, AppConfig]:
    try:
        paths = _resolve_workspace(workspace)
    except ValueError as exc:
        typer.secho(f"Workspace error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    try:
        config = _load_app_config(paths)
    except (FileNotFoundError, RuntimeError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    return paths, config


def _setup_cli_logging(config: AppConfig) -> Logger:
    configure_logging(level=config.log_level, workspace_path=config.workspace)
    return get_logger(__name__, command=_COMMAND_NAME)


def _ensure_hooks(
    registry: ModuleRegistry,
) -> dict[str, Callable[[WorkspaceHandle], Sequence[HealthReport]]]:
    hooks = registry.health_registry()
    if not hooks:
        typer.echo("No modules with health hooks are registered.")
        raise typer.Exit(code=0)
    return hooks


def _determine_target_modules(
    registry: ModuleRegistry,
    modules: Sequence[str],
) -> list[str]:
    try:
        return _select_modules(registry, modules)
    except KeyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


def _evaluate_modules(
    module_names: Sequence[str],
    hooks: dict[str, Callable[[WorkspaceHandle], Sequence[HealthReport]]],
    handle: WorkspaceHandle,
    logger: Logger,
) -> tuple[list[tuple[str, HealthModuleSnapshot]], int]:
    results: list[tuple[str, HealthModuleSnapshot]] = []
    highest_exit = 0

    for name in module_names:
        hook = hooks[name]
        _, snapshot = _execute_hook(name, hook, handle, logger)
        results.append((name, snapshot))
        highest_exit = max(highest_exit, _EXIT_CODES[snapshot.status])

    return results, highest_exit


def _emit_run_results(
    results: Sequence[tuple[str, HealthModuleSnapshot]],
) -> None:
    if not results:
        return

    typer.echo()
    for name, snapshot in results:
        _emit_module_output(name, snapshot)
        if snapshot.details:
            typer.echo()


def _persist_health_document(
    paths: WorkspacePaths,
    results: Sequence[tuple[str, HealthModuleSnapshot]],
) -> list[str]:
    store = HealthDocumentStore(paths.workspace / ".health.json")

    try:
        previous = store.load()
    except HealthDocumentError as exc:
        typer.secho(
            f"Failed to load health document: {exc}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc

    updates = {name: snapshot for name, snapshot in results}
    carried_forward = sorted(set(previous.modules()) - set(updates))
    merged = previous.merge(updates)

    try:
        store.write(merged)
    except HealthDocumentError as exc:
        typer.secho(
            f"Failed to write health document: {exc}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc

    return carried_forward


def _log_run_summary(
    logger: Logger,
    modules: Sequence[str],
    results: Sequence[tuple[str, HealthModuleSnapshot]],
    exit_code: int,
    carried_forward: Sequence[str],
) -> None:
    if carried_forward:
        logger.info(
            "checkhealth-carried-forward",
            modules=list(carried_forward),
        )

    logger.info(
        "checkhealth-run",
        modules=list(modules),
        statuses={name: snapshot.status.value for name, snapshot in results},
        exit_code=exit_code,
    )


def _build_checkhealth_command(
    *,
    registry: ModuleRegistry,
) -> Callable[[list[str], Path | None], None]:
    def checkhealth_command(
        modules: list[str] = typer.Argument(
            (),
            metavar="[MODULE]",
            help="Optional module names to evaluate (defaults to all).",
        ),
        workspace: Path | None = typer.Option(
            None,
            "--workspace",
            "-w",
            help=(
                "Override workspace directory "
                "(defaults to RAGGD_WORKSPACE or ~/.raggd)."
            ),
        ),
    ) -> None:
        paths, config = _ensure_workspace_and_config(workspace)
        logger = _setup_cli_logging(config)
        hooks = _ensure_hooks(registry)
        target_modules = _determine_target_modules(registry, modules)

        handle = _CLIWorkspaceHandle(paths=paths, config=config)
        results, highest_exit = _evaluate_modules(
            target_modules,
            hooks,
            handle,
            logger,
        )

        _emit_run_results(results)
        carried_forward = _persist_health_document(paths, results)
        _log_run_summary(
            logger,
            target_modules,
            results,
            highest_exit,
            carried_forward,
        )

        if highest_exit:
            raise typer.Exit(code=highest_exit)

    return checkhealth_command


def register_checkhealth_command(
    app: typer.Typer,
    *,
    registry: ModuleRegistry,
) -> None:
    """Register the ``raggd checkhealth`` command on the Typer app."""

    handler = _build_checkhealth_command(registry=registry)

    app.command(
        "checkhealth",
        help=(
            "Evaluate registered module health hooks and persist results to "
            "<workspace>/.health.json."
        ),
    )(handler)


__all__ = ["register_checkhealth_command"]
