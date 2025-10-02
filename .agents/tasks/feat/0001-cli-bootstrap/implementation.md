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
  - [x] Ensure uv packaging includes `src/raggd/resources/**/*` (defaults + templates) in both sdist and wheel artifacts.
  - [x] Scaffold package directories (`cli`, `core`, `modules`, `resources`) with docstring-rich modules and type hints.
  - [x] Implement workspace path resolver + `--workspace`/`RAGGD_WORKSPACE` precedence logic and refresh archiving helper.
  - [x] Add configuration model that loads packaged defaults, overlays user `raggd.toml`, env vars, and CLI flags per precedence, and emits commented templates.
  - [x] Introduce `src/raggd/resources/raggd.defaults.toml` and ensure init seeds both defaults and rendered user config.
  - [x] Configure structlog with rich console handler and rotating file handler, exposing a reusable `get_logger` helper.
  - [x] Implement module registry with `ModuleDescriptor` definitions, dependency availability checks, enable/disable evaluation, and capability `emit()` seam plus logging of decisions.
  - [x] Build Typer CLI app with shared options, `init` command wiring into core utilities, and structured success/error outputs including module summaries.
  - [x] Document CLI usage + env vars + module toggles in README and ensure generated config includes comments.
  - [x] Implement automated tests (unit + integration) covering behaviors listed above, including precedence resolver and module registry toggling under missing/present extras.
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

### 2025-10-02 11:56 UTC
**Summary**
Relaxed CLI error assertion to tolerate rich-formatted output and re-ran the suite.
**Changes**
- Updated `tests/cli/test_cli_app.py` to normalize `CliRunner` output before validating conflicting module overrides.
- Re-ran `uv run pytest`; 40 tests passed with 100% coverage.

### 2025-10-02 11:50 UTC
**Summary**
Executed the full automated test suite to confirm 100% coverage.
**Changes**
- Ran `uv run pytest` with escalated permissions (uv cache) and observed 40 passing tests at 100% coverage.
- Marked the implementation checklist item for automated tests as complete.

### 2025-10-02 06:54 UTC
**Summary**
Documented CLI usage, configuration precedence, and module toggles.
**Changes**
- Authored workspace bootstrap guide in `docs/learn/workspace.md` and updated the docs index.
- Added a minimal README quickstart section that points to the new documentation.
- Marked the implementation checklist documentation item complete; no tests required for doc-only changes.

### 2025-10-02 05:50 UTC
**Summary**
Implemented dependency-aware module registry evaluation.
**Changes**
- Added descriptor lifecycle helpers with availability checks, status reporting, and logging integration.
- Introduced unit tests covering enablement precedence, missing extras, unknown modules, and emit hooks.
- Ran `uv run pytest tests/modules/test_registry.py` (fails overall coverage until remaining CLI stubs are implemented).

### 2025-10-02 06:17 UTC
**Summary**
Wired the Typer-based CLI entry point and covered workspace init flows via integration tests.
**Changes**
- Implemented `create_app`, Typer options/parsing, module status reporting, and console output formatting.
- Added CLI integration tests (including env overrides, refresh/existing notes, module override errors) plus helper/unit coverage for registry/config/path utilities.
- Updated test suite to maintain 100% coverage and exercised `uv run pytest` successfully.

### 2025-10-02 05:37 UTC
**Summary**
Configured structlog/rich logging with rotating file output.
**Changes**
- Implemented logging helpers with gzip-archived rotation and console injection seam.
- Added logging handler unit tests covering configuration and rollover behavior.
- Ran `uv run pytest tests/core/test_logging.py` (fails global coverage until other stubs are implemented).

### 2025-10-02 03:21 UTC
**Summary**
Seeded packaged defaults and ensured workspace init writes both defaults and user config.
**Changes**
- Added bundled defaults resource, loader helpers, and workspace seeding logic.
- Introduced CLI tests for init seeding/refresh plus defaults loader coverage.
- Ran `uv run pytest tests/core/test_config.py tests/cli/test_init.py` (fails overall coverage until remaining stubs land).

### 2025-10-02 02:48 UTC
**Summary**
Implemented configuration loader and renderer with precedence-aware module overrides.
**Changes**
- Added deep-merge stacking for defaults, user config, env vars, and CLI overrides plus module toggle normalization.
- Introduced TOML renderer emitting annotated `raggd.toml` scaffolds and unit tests covering precedence/serialization.
- Ran `uv run pytest tests/core/test_config.py` (fails global coverage due to remaining stubs; expected until later steps).

### 2025-10-02 02:41 UTC
**Summary**
Implemented workspace resolver and archive helper with supporting tests.
**Changes**
- Added precedence-aware path normalization, refresh archiving helper, and unit tests.
- Ran `uv run pytest tests/core/test_paths.py` (fails coverage until remaining stubs are implemented).

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

### 2025-10-01 20:10 UTC
**Summary**
Configured packaging so uv-built sdists/wheels ship the resource templates and defaults.
**Changes**
- Enabled setuptools package-data inclusion for `raggd.resources` and added `MANIFEST.in` to include bundled assets.

### 2025-10-01 20:16 UTC
**Summary**
Scaffolded CLI, core, modules, and resources packages with documented stubs.
**Changes**
- Added docstring-rich module skeletons for CLI, core utilities, module registry, and packaged resources.
