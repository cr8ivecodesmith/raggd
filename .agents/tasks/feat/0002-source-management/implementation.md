# Source Management Tooling — Implementation

## Understanding
- Expand the CLI with a `raggd source` command group covering init/target/refresh/rename/remove/list/enable/disable, persist normalized source definitions under `[workspace.sources]`, materialize per-source directories (with `db.sql` stubs and JSON manifests), and guard lifecycle commands behind health checks that integrate with a new `raggd checkhealth` aggregator writing `.health.json`.
- Assumptions / Open questions: assume existing CLI logging/config/registry seams stay stable; expect to add a `modules.source` toggle default-on without impacting other modules; confirm whether slug normalization can rely on a bundled helper vs introducing `python-slugify` (leaning toward a small internal normalizer unless we need locale-aware behavior); clarify if initial refresh failures during `init --target` should leave partially created directories (plan to keep directory but mark disabled).
- Risks & mitigations: risk of corrupting `raggd.toml` when rewriting nested `[workspace.sources]` tables → use `tomlkit` mutation preserving comments and add fixture-backed round-trip tests; risk of destructive filesystem ops (`refresh`, `remove`) → implement explicit confirmations, honor `--force`, and add safeguards that only remove managed files; risk of inconsistent health gating → centralize check logic so every command calls the same guard and exercise with unit/CLI tests; risk of race conditions refreshing active sources → keep operations synchronous and document lack of concurrency support for now.

## Resources
### Project docs
- `.agents/tasks/feat/0002-source-management/spec.md` — authoritative behavior/DoD.
- `.agents/guides/workflow.md` — delivery cadence, expectations for history/docs.
- `.agents/guides/engineering-guide.md` — seam-first architecture and DI guidance for new services.
- `.agents/guides/patterns-and-architecture.md` — patterns for CLI organization, logging, and module layout.
- `.agents/guides/styleguides.md` — formatting conventions for TOML/JSON updates and CLI messaging.
### External docs
- https://typer.tiangolo.com/typer-cli/ — reference for nested command groups and confirmation prompts.
- https://tomlkit.readthedocs.io/ — preserving TOML comments/ordering while updating `[workspace.sources]` entries.
- https://docs.python.org/3/library/sqlite3.html — ensure `db.sql` handling matches SQLite expectations and safe file resets.
- https://github.com/un33k/python-slugify — fallback if built-in normalization proves insufficient.

## Impact Analysis
### Affected behaviors & tests
- `raggd source init` (name normalization, duplicate detection, optional target validation, manifest bootstrapping) → new CLI integration tests plus service unit tests for config/manifest side effects.
- `raggd source target` (set/clear, validation, refresh trigger, confirmations) → CLI tests covering both `--clear` and refresh-on-change pathways and unit tests for validator helpers.
- `raggd source refresh` (health gating, file pruning, manifest timestamps) → CLI tests with healthy/unhealthy sources and service-level tests for filesystem mutations.
- `raggd source rename/remove` (collision detection, health gating, enablement toggles) → CLI tests for force vs blocked flows and unit tests for directory/config synchronization.
- `raggd source enable/disable` and `list` (status reporting, exit codes) → CLI tests verifying status table, exit codes for degraded/error, and multiple-name toggling behavior.
- `raggd checkhealth [module?]` (aggregator, `.health.json` persistence, read-only hooks) → CLI/integration tests plus unit coverage for aggregator filtering and file output.
- Health auto-disable semantics during command-triggered checks → targeted tests ensuring failing health results flip `enabled` and manifest metadata unless forced.
### Affected source files
- Create: `src/raggd/cli/source.py` (Typer command group), `src/raggd/cli/checkhealth.py` (aggregator CLI surface), `src/raggd/source/__init__.py`, `src/raggd/source/models.py`, `src/raggd/source/service.py`, `src/raggd/source/manifest.py`, `src/raggd/source/health.py`, `src/raggd/source/errors.py`, `src/raggd/source/utils.py` (normalization/validation helpers), `tests/cli/test_source.py`, `tests/cli/test_checkhealth.py`, `tests/source/test_service.py`, `tests/source/test_manifest.py`, `tests/source/test_health.py`.
- Modify: `src/raggd/cli/__init__.py` (register new command groups), `src/raggd/core/config.py` (workspace sources schema, iterators, persistence helpers), `src/raggd/core/paths.py` (add `sources_dir` resolver/utilities), `src/raggd/core/__init__.py` (export new helpers if needed), `src/raggd/modules/registry.py` (register `source` module descriptor & optional health hook reference), `src/raggd/resources/raggd.defaults.toml` (add `[modules.source]` toggle), `pyproject.toml` (optional-dependency group for `source`, add slugifier if required, wire dependency-groups), `MANIFEST.in`/package data if new templates/manifests need inclusion, `docs/` (user guide for source management, logging notes), existing tests touched by new config fields.
- Delete: none anticipated (legacy flows already absent).
- Config/flags: new `[workspace.sources]` table in `raggd.toml`, CLI flags `--target`, `--force`, `--clear`, multi-name enable/disable arguments, module toggle `modules.source`, `.health.json` artifact.
### Security considerations
- Path validation to prevent directory traversal or writing outside workspace; enforce absolute normalized targets within allowed base or explicitly document allowances.
- Controlled deletion: ensure `refresh`/`remove` only touch files we created (manifest, `db.sql`, cached artifacts) and skip user-provided files.
- Health data stored in JSON should avoid logging sensitive paths unless necessary; redact environment-specific details when feasible.
- Confirm SQLite stubs created with restrictive permissions and do not open network handles; no new outbound network traffic.

## Solution Plan
- Architecture/pattern choices: keep CLI thin by delegating to a `SourceService` living in `raggd/source/service.py`, modeled after seam-first guidance; reuse `structlog` logging and `typer.confirm` for UX consistency; expose health checks through an explicit `HealthRegistry` wired off `modules.registry` so each module registers a read-only callable returning typed `HealthReport` records (fields: `name`, `status`, `summary`, `actions`, `last_refresh_at`), keeping `raggd checkhealth` side-effect free while allowing service flows to do the mutating work, aligned with `patterns_and_architecture.md` recommendations for module seams. Define the shared contract as `ModuleHealthHook = Callable[[WorkspaceHandle], Sequence[HealthReport]]` and surface it via an optional `health_hook` attribute on `ModuleDescriptor`, letting the registry assemble deterministic module-order iteration for the aggregator.
- DI & boundaries: inject workspace paths/config into services instead of performing global reads; let `SourceConfigStore` wrap `core.config.AppConfig` so mutations travel through the existing config facade; expose file operations behind helper methods for testability; register health hooks via the shared `HealthRegistry` attached to `ModuleDescriptor` metadata so the aggregator can enumerate modules without each command constructing hooks ad hoc, with hooks required to remain side-effect free. Reference `engineering-guide.md` to keep dependencies flowing inward.
- Stepwise checklist:
  - [x] Extend `WorkspacePaths` with `sources_dir` (and helper factories) plus unit coverage.
  - [x] Model `WorkspaceSourceConfig`/`SourceManifest` Pydantic dataclasses and JSON schema helpers.
  - [x] Introduce `SourceConfigStore` utilities that compose with `core.config.AppConfig` (wrapping its load/update helpers) to keep a single config model, use `tomlkit` for structure-aware edits, and persist changes through atomic temp-file writes followed by `os.replace`; if either the temp write or replace fails, emit structured errors, keep the on-disk config untouched, and surface the failure so the CLI can abort without partial state.
  - [x] Implement slug normalization + path validation helpers (and determine whether to vendor `python-slugify`).
  - [ ] Scaffold `SourceService` with init/target/refresh/rename/remove/list/enable/disable methods wired to config + filesystem, ensuring `init` creates the source directory + `db.sql` stub, writes config/manifest entries, and auto-enables + refreshes when a validated `--target` is provided (leaving new sources disabled otherwise). Refresh clears managed artifacts, recreates `db.sql`, stamps manifests, and respects confirmation/force semantics; rename/remove keep manifests/config in sync; enable runs the health check to report current status without auto-disabling on `degraded`/`error`, while target/refresh/rename/remove flows toggle `enabled=false` when non-forced checks fail.
  - [ ] Build health evaluation routines that run per-source checks (target existence/readability, manifest freshness, disabled markers), update manifest status metadata when invoked from mutating flows, and leave the read-only `HealthHook` path untouched. Mutating flows record a `last_health` block with status/summary/actions stamps every time they trigger checks; successful refresh/target changes also update `last_refresh_at`, while failed checks preserve the previous refresh timestamp but capture the degraded/error status for auditability.
  - [ ] Wire command-triggered health gating/auto-disable semantics into service methods with shared guard logic.
  - [ ] Create Typer command group for `raggd source`, mapping CLI options to service operations and confirmations, validating mutually exclusive `target` inputs (`--clear` vs `<dir>`), and enforcing exit codes (e.g., `list` returns non-zero when any source status is `unknown`/`degraded`/`error`).
  - [ ] Update module registry/defaults to include a `source` module toggle and dependency extras.
  - [ ] Extend the modules registry to publish a `HealthRegistry` view that exposes the `ModuleDescriptor.health_hook` contract for `checkhealth`.
  - [ ] Design `.health.json` aggregator format and persistence helpers (read/merge/write with timestamps) that emit the payload shape defined in `spec.md` (`{"sources": {"checked_at": iso8601, "status": enum, "details": [{"name": str, "status": enum, "summary": str|None, "actions": [str], "last_refresh_at": iso8601|None}]}}`), derive the module-level `status` as the highest-severity entry in `details`, and persist via temp-file + `os.replace` to avoid partial writes; per-run outputs replace the entire module block for modules that provided data while preserving untouched module sections from the previous file, and modules are responsible for returning their full canonical payload so overlapping fields never interleave across modules. Failed writes keep the prior file intact and surface structured errors.
  - [ ] Implement `raggd checkhealth [module]` CLI entry using the health registry, ensuring filtered runs only update the requested module keys and logging when data is carried forward unchanged for others.
  - [ ] Ensure logging captures key actions (success/failure, enablement toggles, forced operations).
  - [ ] Add CLI + unit tests for all new behaviors, including error paths and force overrides.
  - [ ] Refresh documentation (workspace guide, CLI help strings) and update DoD artifacts.

## Test Plan
- Unit: source model normalization, config store read/write round-trip, manifest serialization, health evaluator status mapping, slug/path validators, `.health.json` writer.
- Contract: ensure `SourceService` methods respect interface contracts (e.g., return structures consumed by CLI) via focused tests; simulate ModuleRegistry + health registry interaction to ensure descriptors expose hooks as expected.
- Integration/E2E: Typer runner tests for `raggd source …` commands (including confirmations, force flags, exit codes) and `raggd checkhealth` (full run + filtered by module) with temporary workspaces.
- Manual checks (if needed): exercise `raggd source init --target` against a sample project to validate logging, and run `raggd checkhealth` to inspect `.health.json`; verify refresh removes cached files but preserves manifest.

## Operability
- Telemetry: extend structured logs with events like `source-init`, `source-refresh`, `source-health-failed`, `checkhealth-run`; record status, source name, and force flag usage.
- Dashboard/alert updates: document how operators can tail workspace logs or parse `.health.json`; no automated alerting yet but leave hooks for future metrics exporters.
- Runbooks / revert steps: capture manual steps to disable/remove a problematic source, delete `.health.json`, and roll back module toggle; note that disabling the `modules.source` toggle removes CLI registration after revert.

## History
### 2025-10-04 00:50 PST
**Summary**
Confirmed the WorkspacePaths sources directory helpers landed and marked the checklist item complete.
**Changes**
- Verified paths, CLI init scaffolding, and tests cover the new sources directory support.
- Updated the stepwise checklist to reflect completion.

### 2025-10-04 11:15 PST
**Summary**
Initial implementation blueprint drafted from feat/0002 spec.
**Changes**
- Captured requirements understanding, risks, and dependencies for source management tooling.
- Outlined architecture plan, stepwise checklist, and comprehensive test/operability strategy.

### 2025-10-04 14:10 PST
**Summary**
Added source configuration and manifest models with supporting tests.
**Changes**
- Created Pydantic models for workspace source config, manifest data, and health snapshots plus JSON schema helpers.
- Introduced unit tests covering default normalization behavior and schema structure for the new models.

### 2025-10-04 18:40 PST
**Summary**
Implemented workspace source config store and reshaped core config schema for sources.
**Changes**
- Refactored `core.config` to manage `WorkspaceSettings` with nested sources, updated rendering, and ensured existing CLI callers integrate transparently.
- Added `SourceConfigStore` with atomic writes, error handling, and comprehensive unit coverage for legacy and fresh config scenarios.
- Expanded config tests to exercise new schema helpers, iteration utilities, and rendering paths while preserving 100% coverage.

### 2025-10-04 21:05 PST
**Summary**
Added slug normalization and path validation helpers with full test coverage.
**Changes**
- Introduced `raggd.source.utils` providing slug normalization, workspace path guards, and target resolution utilities with dedicated errors.
- Exported the helpers via `raggd.source` and exercised edge cases in new `tests/source/test_utils.py`.
- Updated the implementation checklist to record completion of the slug/path validation milestone.
