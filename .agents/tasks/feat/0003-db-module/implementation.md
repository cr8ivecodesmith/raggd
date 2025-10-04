# Database Module CLI — Implementation

## Understanding
- The feature introduces a dedicated `raggd db` command suite and `DbLifecycleService` that own every SQLite lifecycle concern for sources: discovery/creation of `<source>/db.sqlite3`, bootstrap via the first migration, upgrades/downgrades, health reporting, manifest mirroring, vacuum maintenance, ad-hoc SQL execution, and destructive resets. The `source` module should only emit an ensure signal and never touch SQLite directly.
- We also stand up `raggd.modules.manifest` as a shared infrastructure subsystem that handles manifest discovery, migrations into the `modules.*` layout, backup rotation, locking, and atomic writes so both `source` and `db` modules (plus future ones) depend on a single seam instead of bespoke JSON handling.
- The shared `config.db.manifest_*` settings become the canonical way to locate `manifest.json.modules.*`; `ManifestService` will surface helpers around them and the `source` module must consume those helpers instead of hard-coded keys when delegating ensure/reset flows.
- Packaging now calls for a new `db` optional extra in `pyproject.toml` that depends on the `uuid7` package and includes bundled migration SQL; the module registry must expose a `db` descriptor wired to the health hook so toggles/extras activate correctly.
- Assumptions / Open questions
  - The Python `uuid7` helper from the chosen third-party library exposes both generation and sortable-shortening utilities so we can generate `<shortuuid7>` identifiers deterministically in tests.
  - Existing workspaces use legacy manifests scoped to the source module; we will ship a migrator that nests those fields under `modules.source`, seeds `modules.db`, and stamps a `modules_version` to keep subsequent runs idempotent.
  - The configuration layer (`config.db.*`, feature toggles) continues to use the `raggd.defaults.toml` precedence rules without additional plumbing.
  - Manifest operations share the existing workspace locking story; if additional file locks are required they will live inside `ManifestService` so consumers remain oblivious.
- Risks & mitigations
  - Migration runner mistakes could corrupt user databases; mitigate with checksum/ordering tests, idempotent transaction handling, and dry-run checks before committing ledger rows.
  - Allowing `db run` to execute external SQL carries injection/misuse risks; mitigate with explicit opt-in settings, clear logging of executed paths, and limiting parameter parsing to key=value pairs.
  - Manifest/database divergence could strand newer modules; mitigate with atomic writes, rollback on failure, and contract tests that assert failure when manifests cannot be updated.
  - Manifest restructuring introduces risk of data loss if legacy fields are mis-mapped; mitigate with schema introspection, timestamped `.bak` backups, and golden tests covering legacy-to-modules migrations.
  - Concurrency defaults for `vacuum` and multi-source operations could overwhelm systems; mitigate with `auto` heuristics, explicit overrides, and serializer guards around SQLite connections.

## Resources
### Project docs
- `.agents/tasks/feat/0003-db-module/spec.md` — primary source of functional, architectural, and DoD expectations.
- `.agents/guides/workflow.md` — governs implementation planning, history updates, and manual smoke requirements.
- `.agents/guides/patterns-and-architecture.md` — reference for module layout, seam-first design, and CLI composition.
- `.agents/guides/engineering-guide.md` — guidance on dependency inversion, service boundaries, and testing priorities.
- `.agents/guides/styleguides.md` — ensures naming, docstring, and CLI UX consistency.
- `.agents/tasks/feat/0003-db-module/attachments/schema-reference.md` — schema cues that inform the bootstrap migration and ledger table definitions.
### External docs
- `https://pypi.org/project/uuid7/` — confirms API surface for the helper library used to generate sortable identifiers.
- `https://sqlite.org/lang_vacuum.html` — authoritative reference for VACUUM behavior and limitations we must respect when orchestrating concurrency.

## Impact Analysis
### Affected behaviors & tests
- `db ensure` end-to-end creation/migration → CLI functional tests with fresh and existing workspaces.
- `db upgrade`/`db downgrade` multi-step flows → migration runner unit tests plus CLI regression suites covering success/failure paths.
- `db info --schema/--json` reporting → CLI tests asserting structured output and exit codes on drift.
- `db run` execution (single/multi-source, parameters, quiet mode) → functional tests with fixture SQL scripts and manifest synchronization assertions.
- `db vacuum` concurrency and staleness handling → unit tests for concurrency selection and integration tests ensuring vacuum marks manifest metadata.
- `db reset` destructive rebuild → CLI tests ensuring confirmations, re-bootstrap, and metadata rewrite.
- Manifest mirroring and `DbLifecycleService.ensure()` integration → contract tests between `source` module and database service mocks.
- Legacy manifest migration into the `modules` layout → unit tests around a `ManifestMigrator`, plus CLI coverage ensuring backups are created and manifests rewrite safely.
- `raggd checkhealth` integration for db checks → health command tests verifying warnings/errors for missing dbs, drift, and stale vacuum timestamps.
- Telemetry/structured logging → lightweight assertions that command handlers log key events without leaking sensitive data.
-### Affected source files
- Create: `src/raggd/modules/manifest/__init__.py`, `src/raggd/modules/manifest/service.py`, `src/raggd/modules/manifest/migrator.py`, `src/raggd/modules/manifest/backups.py`, plus supporting fixtures/tests; `src/raggd/modules/db/__init__.py`, `src/raggd/modules/db/cli.py`, `src/raggd/modules/db/service.py`, `src/raggd/modules/db/migrations_runner.py`, `src/raggd/modules/db/manifest_sync.py`, `src/raggd/modules/db/uuid7.py`, `src/raggd/modules/db/config.py`, migration resource package under `src/raggd/modules/db/resources/migrations/`.
- Modify: `src/raggd/cli.py` (wire command group), `src/raggd/cli/checkhealth.py` (register db health checks), `src/raggd/source/service.py` & `src/raggd/source/config.py` (delegate ensure/reset via the lifecycle service and adopt the shared manifest service), `src/raggd/config/defaults.py` & `raggd.defaults.toml` (new settings), `src/raggd/modules/registry.py` (add db descriptor + health hook), `src/raggd/modules/__init__.py` (if exposing manifest helpers), `pyproject.toml` (uuid7 dependency, `db` extra, include group updates), packaging metadata for including migration SQL.
- Tests: create `tests/modules/manifest/test_service.py`, `tests/modules/manifest/test_migrator.py`, `tests/modules/db/test_service.py`, `tests/modules/db/test_cli.py`, `tests/modules/db/test_manifest_sync.py`, extend `tests/source/test_service.py`, `tests/cli/test_checkhealth.py`, add fixture migrations under `tests/fixtures/db_migrations/` and manifest fixtures under `tests/fixtures/manifests/`.
- Docs: update `docs/cli/db.md` (new), extend `docs/operators/source-management.md`, document the manifest `modules` layout and migration expectations (possibly `docs/learn/workspace.md`), adjust README CLI matrix.
### Security considerations
- Validate SQL file paths for `db run` to avoid directory traversal into non-user-approved locations when `config.db.allow_external_sql` is disabled.
- Ensure manifest writes happen via safe, atomic temp-file swaps to avoid partial JSON updates.
- Create timestamped manifest backups before rewriting into the `modules` layout and respect existing file permissions/ownership.
- Guard logging so that credentials or SQL parameter values are not printed at info level.
- Confirm destructive commands (`reset`, `downgrade`) require explicit confirmation or `--force` flags to avoid accidental data loss.

## Solution Plan
- Architecture/pattern choices — follow the modular patterns described in `patterns-and-architecture.md`: encapsulate CLI commands in Typer submodules, expose services through dependency inversion, and keep filesystem interactions behind seam-aware abstractions.
- DI & boundaries — `DbLifecycleService` lives behind an interface consumed by CLI and `source` modules; dependencies include `MigrationRunner`, `ManifestMirror`, and `Config` objects injected via the app container per `engineering-guide.md` recommendations.
- Stepwise checklist:
- **Phase 1 — Manifest subsystem**
  - [x] Scaffold `raggd.modules.manifest` package with `ManifestService`, migrator, backups, locking helpers, and config adapters surfaced through a lean public API.
  - [x] Implement legacy-manifest migration into the `modules.*` layout (including `.bak` rotation and `modules_version` stamping) with accompanying unit + golden tests.
  - [x] Provide manifest fixtures and contract tests to ensure the `source` module can read/write via the service without touching raw JSON.
  - [x] Expose shared helpers for `modules.db` key calculation and manifest settings; document usage for consuming modules.
- **Phase 2 — Source module integration**
  - [x] Refactor `src/raggd/source/*` to depend on `ManifestService` and `DbLifecycleService.ensure()` for database readiness, replacing direct SQLite + manifest file manipulation.
  - [x] Update source module tests to cover delegation, manifest migration triggers, and failure rollback semantics.
  - [x] Wire manifest helpers/settings into the source module configuration layer.
- **Phase 3 — Database module delivery**
  - [x] Scaffold `raggd.modules.db` package with Typer command group exposed in the root CLI and backed by `DbLifecycleService`.
  - [ ] Implement lifecycle commands (`ensure`, `upgrade`, `downgrade`, `info`, `vacuum`, `run`, `reset`) with manifest mirroring via the shared service and cohesive error handling.
  - [ ] Build `MigrationRunner`, ledger schema, and `uuid7` helper wrapper with ordering + checksum validation while persisting both canonical UUID7 and shortened forms into `schema_meta`, the ledger, and manifest mirrors.
  - [ ] Seed migration resources (bootstrap + exemplar) and ensure packaging includes SQL assets.
  - [ ] Register the database module and health provider in `modules/registry.py`; integrate with `raggd checkhealth` and settings defaults (`config.db.*`).
  - [ ] Update `pyproject.toml` (`uuid7` dependency, `db` extra, package data), `raggd.defaults.toml`, CLI docs, and capture manual smoke verifications.
  - [ ] Finalize test matrix across unit, contract, CLI, and packaging validations per the test plan.

## Test Plan
- Unit: `ManifestService` locking/backup/atomic-write operations, `ManifestMigrator` legacy-to-modules transforms, `DbLifecycleService` behaviors (ensure/upgrade/downgrade/vacuum), `MigrationRunner` ordering and failure handling, `uuid7` shortening utility, manifest sync rollback, and schema-meta/manifest persistence of canonical + short UUID7 values.
- Contract: interface between `source` module and `DbLifecycleService`, ensuring `ensure` triggers migrations, consumes the shared manifest settings, and handles manifest migrations/mirroring without direct SQLite manipulation.
- Integration/E2E: CLI tests for `db` subcommands across single/multiple sources, legacy manifest upgrade flows (backup + rewrite), health command outputs, vacuum concurrency across `auto` and explicit values, downgrade boundary at bootstrap, and manifest snapshots showing both canonical and short UUID7 metadata.
- Manual checks: Create a workspace in `.tmp`, run `uv run raggd init`, capture the pre-upgrade manifest, then exercise `raggd db ensure`, `upgrade`, `downgrade`, `info --schema`, `vacuum`, `run`, `reset`, confirm the manifest migrates into `modules.*` with a `.bak` backup, and finish with `uv run raggd checkhealth`; document results per DoD.
- Packaging validation: verify a local build (e.g., `uv build`) advertises the `uuid7` dependency, includes `db` in the modules dependency group, and bundles migration SQL files.

## Operability
- Telemetry (logs/metrics): emit structured logs around migration application, manifest migrations/backups, manifest writes, vacuum concurrency decisions, and manual SQL execution (with file path + target sources).
- Dashboard/alert updates: note requirement to add db health checks to existing monitoring dashboards once telemetry pipeline is in place; expose metrics hooks for future observability work.
- Runbooks / revert steps: document operator tasks in `docs/cli/db.md` including rollback instructions (`downgrade`, `reset`), how to restore manifest backups or run a flattening helper when disabling the module, and how to toggle `modules.db.enabled = false` if needed.

## History
### 2025-10-04 18:47 PST
**Summary**
Initial implementation plan drafted per workflow guidance
**Changes**
- Created `implementation.md` outlining understanding, impact analysis, solution/test plans, operability, and follow-on work

### 2025-10-04 18:55 PST
**Summary**
Plan updated for manifest restructuring requirements
**Changes**
- Refined assumptions, risks, impact analysis, solution steps, tests, and operability to cover legacy manifest migration, backups, and the new `modules.*` layout

### 2025-10-04 22:32 PST
**Summary**
Aligned plan with shared manifest settings adoption
**Changes**
- Clarified that the `source` module consumes the shared `manifest_*` configuration via helpers when delegating to the database service
- Corrected impacted file paths/tests and updated steps, test plan, and understanding accordingly

### 2025-10-04 23:05 PST
**Summary**
Integrated packaging and module registry scope
**Changes**
- Captured `pyproject.toml` updates (`uuid7` dependency, `db` optional extra, package data) across understanding, impact, and solution sections
- Added module registry descriptor work and packaging validation to the plan and test strategy

### 2025-10-04 23:44 PST
**Summary**
Layered plan around manifest → source → db workflow
**Changes**
- Added `raggd.modules.manifest` subsystem to understanding, assumptions, impact analysis, and test coverage
- Reworked the stepwise checklist into phased execution (manifest, then source, then db) with aligned packaging/docs items
- Expanded affected files/tests to cover manifest service scaffolding and fixtures

### 2025-10-04 20:46 PST
**Summary**
Scaffolded manifest subsystem foundations
**Changes**
- Implemented manifest service, migrator, backups, locks, and config adapters with transactional semantics
- Added manifest module tests exercising locking, backups, migrations, serialization, and error handling

### 2025-10-04 21:28 PST
**Summary**
Completed legacy manifest migration with backups and version stamping
**Changes**
- Expanded `ManifestMigrator` to nest legacy payloads under `modules.source`, seed `modules.db`, stamp `modules_version`, and rotate backups
- Added golden fixtures and regression tests validating migration idempotency and backup persistence

### 2025-10-04 22:42 PST
**Summary**
Wrapped Phase 1 manifest fixtures and helper work
**Changes**
- Marked the manifest fixture and helper checklist items complete after verifying Source ↔ Manifest coverage
- Confirmed no additional code changes are required for this step
**Tests**
- `UV_CACHE_DIR=.tmp/uv-cache uv run pytest`
- `UV_CACHE_DIR=.tmp/uv-cache uv run ruff check`

### 2025-10-05 00:15 PST
**Summary**
Refactored the source service to delegate database readiness to `DbLifecycleService`
**Changes**
- Added a stub `DbLifecycleService` that scaffolds per-source databases via the manifest service
- Updated `SourceService` to use the lifecycle service instead of touching SQLite files directly
- Extended source tests with a recording lifecycle stub and added db-module unit coverage for argument validation
**Tests**
- `UV_CACHE_DIR=.tmp/uv-cache uv run ruff check`
- `UV_CACHE_DIR=.tmp/uv-cache uv run pytest`

### 2025-10-05 01:45 PST
**Summary**
Expanded source module tests for delegation, manifest migration, and ensure rollback coverage
**Changes**
- Added SourceService tests verifying `DbLifecycleService.ensure()` usage during target updates and legacy manifest migrations
- Introduced a failing lifecycle double to assert manifests stay unchanged when `ensure` raises
- Leveraged shared manifest fixtures to exercise migration into the `modules.*` namespace from legacy payloads
**Tests**
- `UV_CACHE_DIR=.tmp/uv-cache uv run pytest`

### 2025-10-04 23:25 PST
**Summary**
Aligned source configuration with manifest helpers/settings and extended regression coverage
**Changes**
- Added `SourceConfigStore.manifest_settings()` exposure and ensured SourceService consumes config-derived manifest settings
- Updated tests for config store overrides and SourceService manifest wiring
- Stored manifest settings on SourceService to drive namespace resolution
**Tests**
- `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/source/test_config_store.py tests/source/test_service.py`

### 2025-10-04 23:40 PST
**Summary**
Sketched the `raggd db` CLI surface and lifecycle placeholders
**Changes**
- Introduced `src/raggd/cli/db.py` providing Typer-backed `db` commands, shared context wiring, and root CLI registration with targeted tests
- Added lifecycle error scaffolding plus stubbed command handlers on `DbLifecycleService` pending Phase 3 implementations
- Swapped workspace database filenames to `db.sqlite3` and refreshed path/documentation expectations
**Tests**
- `uv run pytest --no-cov tests/cli/test_db.py tests/modules/db/test_lifecycle.py tests/core/test_paths.py`
