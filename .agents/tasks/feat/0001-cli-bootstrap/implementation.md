# Bootstrapping CLI & Core — Implementation

## Understanding
- Stand up an initial Typer-driven `raggd` CLI exposing `init`, backed by core utilities that resolve the workspace (default `$HOME/.raggd` with `--workspace` flag and `RAGGD_WORKSPACE` env override), emit structured logs (structlog + rich + rotation), and generate a seeded `raggd.toml` plus folders/log archives sourced from a packaged defaults file.
- Assumptions: `pyproject.toml` will adopt Typer, structlog, rich, tomlkit, pydantic-settings, and platformdirs; no extra questions outstanding.
- Risks: cross-platform path handling and permissions (mitigate with `platformdirs` + `pathlib` and unit coverage), accidental destructive refresh (require explicit `--refresh` and archive before wipe), log rotation correctness (lean on well-tested handlers and add integration checks).
- Opportunities: module registry is intentionally seam-first through `ModuleDescriptor` records and capability `emit()` hooks, keeping us flexible if we later replace the homegrown registry with `pluggy`.

## Resources
### Project docs
- `.agents/tasks/feat/0001-cli-bootstrap/spec.md` — canonical scope, decisions, DoD.
- `.agents/guides/workflow.md` — delivery workflow guardrails.
- `.agents/guides/engineering-guide.md` — seam-first DI & testing defaults for core utilities.
- `.agents/guides/patterns-and-architecture.md` — guidance on module layout and logging patterns.
### External docs
- https://typer.tiangolo.com/ — CLI patterns and testing helpers.
- https://www.structlog.org/en/stable/ — structured logging configuration references.
- https://platformdirs.readthedocs.io/en/latest/ — cross-platform user data directories.
- https://pydantic-docs.helpmanual.io/latest/concepts/pydantic_settings/ — env/config merge behavior.
- https://docs.astral.sh/uv/ — package/dependency management for `uv`-driven builds.

## Impact Analysis
### Affected behaviors & tests
- Workspace bootstrap (`raggd init`) → integration tests via Typer runner validating scaffold creation and messaging.
- Existing workspace rerun (no refresh) → integration test asserting idempotent result and safe messaging.
- `--refresh` flow → integration test ensuring archive + clean rehydration.
- Custom path via flag/env → unit or integration tests for resolver precedence.
- Logging level selection → unit test on structlog configuration capturing console/file handlers.
- Config loading from packaged defaults + `raggd.toml` + env + CLI → unit tests for override precedence.
- Module registry enablement → unit tests confirming descriptors honor config flags and dependency availability (extras missing/present) with proper logging.
### Affected source files
- Create: `src/raggd/__main__.py`, `src/raggd/cli/__init__.py`, `src/raggd/cli/init.py`, `src/raggd/core/__init__.py`, `src/raggd/core/config.py`, `src/raggd/core/logging.py`, `src/raggd/core/paths.py`, `src/raggd/modules/__init__.py`, `src/raggd/modules/registry.py`, `src/raggd/resources/templates/raggd.toml.j2`, `src/raggd/resources/raggd.defaults.toml`, `tests/cli/test_init.py`, `tests/core/test_config.py`, `tests/core/test_paths.py`, `tests/core/test_logging.py`, `tests/modules/test_registry.py`.
- Modify: `pyproject.toml` (dependencies, entry point, optional extras, uv resource includes), `README.md` (getting started, env vars), `src/raggd/__init__.py` (version metadata + load helpers if needed), `.gitignore` (workspace/log artifacts), `docs/` index if present.
- Delete: none.
- Config/flags: introduce `RAGGD_WORKSPACE`, `RAGGD_LOG_LEVEL`, CLI `--workspace`, `--refresh`, `--log-level`; document defaults in generated config comments and expose module enable toggles under `[modules]` with per-module extras references.
### Security considerations
- Validate that workspace paths resolve under user-owned directories; reject traversal attempts.
- Set restrictive permissions (e.g., `0o750`) when creating workspace/log files.
- Ensure archived logs compress without leaking secrets and avoid shipping sample secrets in `raggd.toml`.

## Solution Plan
- Follow modular layout from `patterns_and_architecture.md`: `raggd.core` stays dependency-light and focused on seams (config/logging/path) while `raggd.modules` exposes registry hooks for optional packs.
- Apply seam-first DI per `engineering-guide.md`: expose workspace resolver + config loader as injectable functions/classes for future overrides and testing via fixtures.
- Stepwise checklist:
  - [x] Update `pyproject.toml` (uv-managed) with runtime deps, CLI entry point, extras for future modules, and optional dependency groups referenced by module descriptors.
  - [ ] Ensure uv packaging includes `src/raggd/resources/**/*` (defaults + templates) in both sdist and wheel artifacts.
  - [ ] Scaffold package directories (`cli`, `core`, `modules`, `resources`) with docstring-rich modules and type hints.
  - [ ] Implement workspace path resolver + `--workspace`/`RAGGD_WORKSPACE` precedence logic and refresh archiving helper.
  - [ ] Add configuration model that loads packaged defaults, overlays user `raggd.toml`, env vars, and CLI flags per precedence, and emits commented templates.
  - [ ] Introduce `src/raggd/resources/raggd.defaults.toml` and ensure init seeds both defaults and rendered user config.
  - [ ] Configure structlog with rich console handler and rotating file handler, exposing a reusable `get_logger` helper.
  - [ ] Implement module registry with `ModuleDescriptor` definitions, dependency availability checks, enable/disable evaluation, and capability `emit()` seam plus logging of decisions.
  - [ ] Build Typer CLI app with shared options, `init` command wiring into core utilities, and structured success/error outputs including module summaries.
  - [ ] Document CLI usage + env vars + module toggles in README and ensure generated config includes comments.
  - [ ] Implement automated tests (unit + integration) covering behaviors listed above, including precedence resolver and module registry toggling under missing/present extras.
  - [ ] Perform manual verification of CLI flows and capture notes for future runbook.

## Test Plan
- Unit: `core.paths` resolver precedence, refresh archiving behavior (with tmp dirs); `core.config` defaults + user file + env + CLI merge; `core.logging` structlog factories (assert handler types/levels) using temporary directories; `modules.registry` descriptor enablement + availability handling.
- Contract: none initially (no external providers yet).
- Integration/E2E: invoke Typer app via `CliRunner` to cover happy path, existing workspace, refresh, custom path, log level override, module enable/disable edge cases, and ensure config/log files materialize along with module status reporting.
- Manual checks: run `uv run raggd init` locally, inspect workspace tree, confirm log rotation, verify env var override, and observe module registry status output/logs.

## Operability
- Telemetry: ensure log records include structured fields (`module`, `event`) and note location of archived logs; stub hook for future metrics emitter.
- Dashboard/alert updates: none required for bootstrap; document log file locations for future observability wiring.
- Runbooks / revert steps: removing the workspace directory reverts bootstrap; include note about `--refresh` recovering from corruption.

## History
### 2025-10-02 02:09 PST
**Summary**
Initial implementation plan drafted per approved spec.
**Changes**
- Added implementation blueprint, dependencies, testing, and operability notes.

### 2025-10-01 20:06 UTC
**Summary**
Marked dependency setup step complete after landing `pyproject.toml` CLI/dependency updates.
**Changes**
- Checked off dependency update task and noted uv-managed additions in history.

### 2025-10-01 11:38 PDT
**Summary**
Expanded implementation plan for defaults precedence and module registry lifecycle.
**Changes**
- Added packaged defaults tasks, registry availability testing, and module descriptor wiring.
