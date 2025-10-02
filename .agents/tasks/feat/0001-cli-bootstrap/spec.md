# Bootstrapping CLI & Core — Spec

## Summary
Establish the initial `raggd` CLI scaffold, default workspace bootstrap flow, and `core`/`modules` packages that centralize shared utilities while enabling optional feature packs.

## Goals
- Ship a Typer-powered CLI entrypoint (`raggd`) exposing the `init` command with shared option handling.
- Provide a reproducible workspace initialization routine that provisions `raggd.toml`, log directories, and default folders under a configurable root (default `$HOME/.raggd`).
- Stand up the `raggd.core` package covering configuration loading, logging setup with multiple levels, filesystem helpers, and future-ready extension hooks, plus a sibling `raggd.modules` registry scaffold for pluggable features.
- Document all public modules, functions, and classes with Google-style docstrings containing `Example:` snippets to support autodoc tooling and AI agents.
- Establish configuration precedence (`CLI flags > env vars > user config > packaged defaults`) and module toggle semantics so optional features stay disabled unless configured and dependency-available.

## Non-Goals
- Building ingestion, retrieval, or RAG pipeline functionality.
- Implementing module-specific business logic (e.g., MCP, embeddings, file monitoring).
- Shipping installers, release automation, or cloud deployment scripts.
- Finalizing the full module toggle semantics beyond scaffolding for configuration and dependency checks.

## Behavior (BDD-ish)
- Given a clean environment, when a user runs `raggd init`, then the CLI resolves the target workspace, creates the directory tree, generates a default `raggd.toml`, and exits with a success message.
- Given an existing workspace, when the user reruns `raggd init`, then the command reports status, avoids destructive overwrites, and offers a safe regeneration path.
- Given an existing workspace, when the user appends `--refresh`, then the CLI archives or removes previous config/log artifacts before recreating the workspace from scratch.
- Given `raggd init --workspace <custom path>` or `RAGGD_WORKSPACE` is set, when the path is valid, then the workspace is created there and the config reflects the override.
- Given a configured logging level, when commands run, then console and file logs respect the chosen verbosity (DEBUG/INFO/WARNING/ERROR).
- Given module enablement flags across defaults, config, env, or CLI, when the registry resolves modules, then it honors the precedence order, reports availability, and only initializes modules that are both enabled and dependency-satisfied.

## Configuration & Module Registry
- Precedence order: CLI flags override environment variables, which override the user `raggd.toml`, which in turn overlays packaged defaults shipped as `raggd.defaults.toml`. Generated configs comment on each layer for discoverability.
- Packaged defaults live under `src/raggd/resources/raggd.defaults.toml` and stay bundled with the application—they inform the generated `raggd.toml` but are not copied into the workspace.
- User config structure reserves a flat `[modules]` table where each module key exposes `enabled`, dependency/extra names, and module-specific settings. Example:
  ```toml
  [modules]
  [modules.mcp]
  enabled = false
  requires = "mcp"
  ```
- The registry defines a `ModuleDescriptor` (module name, extra/dependency hints, default enabled flag, `is_available()` check, `emit()` hook) and a simple lifecycle: descriptors register themselves, the registry evaluates enablement + availability, logs capability decisions, and returns the active modules. The registry itself lives under `raggd.modules` and does not reserve a config namespace, keeping module toggles easy to scan. This seam keeps us framework-agnostic today while making a later `pluggy` adoption straightforward if richer plugin orchestration becomes necessary.

## Constraints & Dependencies
- Constraints: Python 3.12+, cross-platform filesystem semantics, adhere to repository styleguides.
- Build tooling: managed through `uv` with `pyproject.toml`, so packaged resources (defaults/templates) must be declared for both source and wheel distributions.
- Upstream/Downstream: Typer, pydantic-settings, platformdirs, tomlkit, structlog + rich for console/file logging, optional dependency groups defined in `pyproject.toml`.

## Security & Privacy
- Store workspace under the current user’s home with restricted permissions.
- Validate user-supplied paths to prevent directory traversal or permission issues.
- Keep configuration data local, human-readable, and free of secrets.

## Telemetry & Operability
- Provide console and file logging with level controls and colorized output via `rich` + `structlog` adaptation.
- Archive log files (e.g., time-based rollover to compressed files) to prevent unbounded growth while retaining history.
- Include structured log context (module names) and a hook for future metrics emission.
- Document all supported environment variables (including `RAGGD_WORKSPACE`) in generated config comments for quick discovery.

## Rollout / Revert
- `raggd init` ships with the default CLI entrypoint; no separate flag gating required.
- Re-running `init` remains idempotent and safe.
- Rollback is manual removal of the workspace directory and associated cache artifacts.

## Definition of Done
- [x] Behavior verified via CLI smoke tests for `raggd init` (happy path, existing workspace, `--refresh`, custom path, log-level override).
- [x] Docs updated (README quick start, inline docstrings, comments in `raggd.toml`).
- [x] Tests added for config parsing, workspace path resolution, and logging setup.
- [x] Flags defaulted per precedence stack, with module toggles documented and defaults sourced from packaged config.
- [x] Module registry logs availability decisions and only activates enabled, dependency-satisfied descriptors.
- [x] Monitoring hooks (log file + console) validated manually.

## Ownership
- Owner: @matt
- Reviewers: @software-architect, @cli-lead
- Stakeholders: @docs, @platform

## Links
- Related: _tbd_

## Decisions
- Introduce `raggd init --refresh` to archive prior workspace artifacts before regenerating a clean layout.
- Adopt `structlog` layered on stdlib logging + rich console handler, with compressed archival of rotated log files.
- Keep the module registry in a top-level sibling namespace (`raggd.modules`) to avoid deep nesting.
- Support a `RAGGD_WORKSPACE` environment variable that mirrors the CLI flag and is surfaced prominently in generated config comments.
- Load configuration via the precedence stack `CLI flag > environment variable > user raggd.toml > packaged defaults`, documenting each layer in generated comments.
- Model modules through lightweight `ModuleDescriptor` records that expose availability checks and a capability `emit()` seam, deferring a transition to `pluggy` until optional modules grow beyond simple toggles.

## Open Questions
_None._

## History

### 2025-10-02 15:35 UTC
**Summary** — Definition of Done achieved
**Changes**
- Confirmed CLI smoke runs (`uv run raggd init` variants) and manual monitoring checks, completing the remaining DoD items.
- Marked all Definition of Done checkboxes as satisfied.

### 2025-10-02 14:50 UTC
**Summary** — Clarified packaged defaults handling
**Changes**
- Noted that `raggd.defaults.toml` remains bundled and is not copied into the workspace.

### 2025-10-01 11:38 PDT
**Summary** — Documented configuration precedence and module lifecycle seams
**Changes**
- Added configuration/module registry section with packaged defaults and toggle structure.
- Updated DoD and decisions for precedence validation and descriptor lifecycles.

### 2025-10-02 02:07 PST
**Summary** — Draft bootstrap spec for CLI/core scaffolding
**Changes**
- Captured logging, workspace override, and module registry decisions for reviewer sign-off.
