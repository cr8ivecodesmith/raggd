"""Command-line interface primitives for :mod:`raggd`.

This module exposes the Typer application behind the ``raggd`` console script
and wires the `init` command into the core workspace/bootstrap helpers.

Example:
    >>> import typer
    >>> from raggd.cli import create_app
    >>> app = create_app()
    >>> isinstance(app, typer.Typer)
    True
"""

from __future__ import annotations

import os
from importlib import util as importlib_util
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import typer

from raggd.cli.init import init_workspace
from raggd.cli.source import create_source_app
from raggd.core.config import AppConfig, ModuleToggle, DEFAULTS_RESOURCE_NAME
from raggd.core.logging import configure_logging, get_logger
from raggd.core.paths import resolve_workspace
from raggd.modules.registry import ModuleDescriptor, ModuleRegistry

_app_help = (
    "Modular Retrieval-Augmented Generation toolkit."
    "\n\n"
    "Use `raggd init` to bootstrap a workspace and populate `raggd.toml`."
)


_DEFAULT_MODULE_DESCRIPTORS: tuple[ModuleDescriptor, ...] = (
    ModuleDescriptor(
        name="source",
        description="Workspace source management commands and services.",
        default_toggle=ModuleToggle(enabled=True),
    ),
    ModuleDescriptor(
        name="file-monitoring",
        description="File system change detection watchers.",
        extras=("file-monitoring",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("file-monitoring",),
        ),
    ),
    ModuleDescriptor(
        name="local-embeddings",
        description=(
            "Generate embeddings locally via ONNX/SentenceTransformers."
        ),
        extras=("local-embeddings",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("local-embeddings",),
        ),
    ),
    ModuleDescriptor(
        name="mcp",
        description="Model Context Protocol integration (client).",
        extras=("mcp",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("mcp",),
        ),
    ),
    ModuleDescriptor(
        name="mcp-rest",
        description="REST bridge for Model Context Protocol servers.",
        extras=("mcp-rest",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("mcp-rest",),
        ),
    ),
    ModuleDescriptor(
        name="parsers",
        description="Rich document and source parsing utilities.",
        extras=("parsers",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("parsers",),
        ),
    ),
    ModuleDescriptor(
        name="rag",
        description="Core retrieval-augmented generation capabilities.",
        extras=("rag",),
        default_toggle=ModuleToggle(
            enabled=False,
            extras=("rag",),
        ),
    ),
)


_EXTRA_SENTINELS: Mapping[str, tuple[str, ...]] = {
    "source": (),
    "file-monitoring": ("watchdog",),
    "local-embeddings": ("onnxruntime", "sentence_transformers"),
    "mcp": ("mcp",),
    "mcp-rest": ("fastapi", "uvicorn"),
    "parsers": (
        "libcst",
        "markdown_it",
        "tree_sitter",
        "tree_sitter_languages",
    ),
    "rag": ("faiss", "openai", "rapidfuzz", "tiktoken"),
}


def _canonical_module_name(raw: str) -> str:
    """Normalize CLI-supplied module names.

    Example:
        >>> _canonical_module_name(" File_Monitoring ")
        'file-monitoring'
    """

    return raw.strip().lower().replace("_", "-")


def _build_module_overrides(
    enables: Sequence[str] | None,
    disables: Sequence[str] | None,
) -> dict[str, bool]:
    """Translate CLI enable/disable requests into config overrides."""

    overrides: dict[str, bool] = {}
    for collection, value in ((enables, True), (disables, False)):
        if not collection:
            continue
        for name in collection:
            canonical = _canonical_module_name(name)
            if not canonical:
                continue
            overrides[canonical] = value
    return overrides


def _detect_available_extras() -> set[str]:
    """Report optional dependency groups that are importable."""

    available: set[str] = set()
    for extra, sentinels in _EXTRA_SENTINELS.items():
        if not sentinels:
            available.add(extra)
            continue
        if all(
            importlib_util.find_spec(sentinel) is not None
            for sentinel in sentinels
        ):
            available.add(extra)
    return available


def _summarize_modules(
    config: AppConfig,
    *,
    registry: ModuleRegistry,
    available_extras: Iterable[str],
) -> tuple[dict[str, bool], dict[str, str]]:
    """Evaluate module enablement and return status alongside human text."""

    status: dict[str, str] = {}
    active = registry.evaluate(
        toggles=config.modules,
        available_extras=available_extras,
        status_sink=status,
    )
    return active, status


def _render_module_line(
    name: str,
    active: Mapping[str, bool],
    status: Mapping[str, str],
) -> str:
    """Return a formatted status line for the module summary."""

    reason = status.get(name, "unknown")
    if reason == "unknown module":
        state = "unknown"
    elif active.get(name, False):
        state = "enabled"
    else:
        state = "disabled"

    detail = reason if reason and reason not in {state, "enabled"} else ""
    suffix = f" - {detail}" if detail else ""
    return f"  - {name}: {state}{suffix}"


def _handle_conflicts(
    enables: Sequence[str] | None,
    disables: Sequence[str] | None,
) -> None:
    """Reject cases where a module is both enabled and disabled."""

    enable_set = {_canonical_module_name(name) for name in enables or ()}
    disable_set = {_canonical_module_name(name) for name in disables or ()}
    conflict = sorted(enable_set & disable_set)
    if conflict:
        joined = ", ".join(conflict)
        raise typer.BadParameter(
            f"Modules cannot be both enabled and disabled: {joined}",
            param_hint="--enable-module/--disable-module",
        )


def _emit_workspace_summary(
    *,
    config: AppConfig,
    modules_active: Mapping[str, bool],
    module_status: Mapping[str, str],
    refresh: bool,
    existing: bool,
) -> None:
    """Print a human-friendly summary of bootstrap results."""

    typer.secho("Workspace initialized", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  workspace: {config.workspace}")
    typer.echo(f"  config: {config.workspace / 'raggd.toml'}")
    typer.echo(f"  defaults: packaged resource ({DEFAULTS_RESOURCE_NAME})")
    typer.echo(f"  log level: {config.log_level}")

    if existing and not refresh:
        typer.echo("  note: existing workspace detected; files left untouched")
    elif refresh:
        typer.echo("  note: archived previous workspace before refresh")

    typer.echo("Modules:")
    for name in sorted(module_status):
        typer.echo(_render_module_line(name, modules_active, module_status))


def create_app() -> "typer.Typer":
    """Return the Typer application powering the ``raggd`` CLI.

    Example:
        >>> import typer
        >>> from raggd.cli import create_app
        >>> cli = create_app()
        >>> isinstance(cli, typer.Typer)
        True

    Returns:
        A configured Typer application ready to be invoked by ``raggd``.
    """

    app = typer.Typer(
        help=_app_help,
        no_args_is_help=True,
        rich_markup_mode="rich",
        invoke_without_command=False,
        cls=typer.core.TyperGroup,
    )

    app.add_typer(create_source_app(), name="source")

    registry = ModuleRegistry(_DEFAULT_MODULE_DESCRIPTORS)

    @app.callback()
    def main_callback() -> None:
        """Top-level CLI callback ensuring subcommands are dispatched.

        Example:
            >>> from raggd.cli import create_app
            >>> app = create_app()
            >>> callback = app.registered_callback
            >>> callback is not None
            True
        """

        return None

    @app.command(
        "init",
        help="Bootstrap a workspace and seed configuration files.",
    )
    def init_command(  # noqa: PLR0913 - CLI surface area intentionally explicit
        workspace: Path | None = typer.Option(
            None,
            "--workspace",
            "-w",
            help=(
                "Override the workspace directory (defaults to $HOME/.raggd "
                "or RAGGD_WORKSPACE)."
            ),
        ),
        refresh: bool = typer.Option(
            False,
            "--refresh",
            help=(
                "Archive existing workspace contents before regenerating a "
                "clean layout."
            ),
        ),
        log_level: str | None = typer.Option(
            None,
            "--log-level",
            "-l",
            help="Override the logging level (DEBUG/INFO/WARNING/ERROR).",
        ),
        enable_module: list[str] = typer.Option(
            None,
            "--enable-module",
            "-E",
            metavar="MODULE",
            help="Force-enable a module regardless of config defaults.",
        ),
        disable_module: list[str] = typer.Option(
            None,
            "--disable-module",
            "-D",
            metavar="MODULE",
            help="Force-disable a module regardless of config defaults.",
        ),
    ) -> None:
        """Initialize (or refresh) the local workspace.

        Example:
            >>> from typer.testing import CliRunner
            >>> runner = CliRunner()
            >>> app = create_app()
            >>> result = runner.invoke(app, ["init", "--help"])
            >>> result.exit_code
            0
        """

        _handle_conflicts(enable_module, disable_module)

        env_workspace = os.environ.get("RAGGD_WORKSPACE")
        env_log_level = os.environ.get("RAGGD_LOG_LEVEL")

        env_workspace_path = (
            Path(env_workspace).expanduser() if env_workspace else None
        )

        try:
            paths = resolve_workspace(
                workspace_override=workspace,
                env_override=env_workspace_path,
            )
        except ValueError as exc:  # pragma: no cover
            typer.secho(f"Workspace error: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        env_overrides = {"log_level": env_log_level} if env_log_level else None
        module_overrides = _build_module_overrides(
            enable_module,
            disable_module,
        )

        workspace_exists = paths.workspace.exists()

        try:
            config = init_workspace(
                workspace=paths.workspace,
                refresh=refresh,
                log_level=log_level,
                module_overrides=module_overrides or None,
                env_overrides=env_overrides,
            )
        except Exception as exc:  # pragma: no cover
            message = f"Failed to initialize workspace: {exc}"
            typer.secho(message, fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        configure_logging(
            level=config.log_level,
            workspace_path=config.workspace,
        )
        logger = get_logger(__name__, command="init")

        available_extras = _detect_available_extras()
        active_modules, status_messages = _summarize_modules(
            config,
            registry=registry,
            available_extras=available_extras,
        )

        logger.info(
            "init-complete",
            workspace=str(config.workspace),
            refresh=refresh,
            modules=active_modules,
            available_extras=sorted(available_extras),
        )

        _emit_workspace_summary(
            config=config,
            modules_active=active_modules,
            module_status=status_messages,
            refresh=refresh,
            existing=workspace_exists,
        )

    return app


__all__ = ["create_app"]
