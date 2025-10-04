# Database Module CLI — Spec

## Summary
Define a first-class `raggd db` command family that owns per-source SQLite lifecycle: creating `db.sqlite3` files, applying migrations (with the bootstrap schema captured as the first migration), exposing operator tooling, and reporting health. The module should encapsulate database concerns behind a clear service boundary so higher-level features (sources, parsers, future vector stores) depend on stable contracts instead of direct file manipulation. Stand up a companion `raggd.modules.manifest` subsystem that centralizes manifest reads/writes, migrations, and health metadata so feature modules plug into a shared infrastructure service instead of bespoke JSON handling.

## Goals
- Deliver a Typer-powered `raggd db <command>` group with `ensure`, `upgrade`, `downgrade`, `info`, `vacuum`, `run`, and `reset` subcommands that mirror the ergonomics of existing CLI modules.
- Standardize per-source database layout to `<workspace>/sources/<name>/db.sqlite3`, treating this as the only supported filename for new workspaces.
- Move all database lifecycle operations (bootstrap, migrations, maintenance) out of the `source` module into a dedicated database service that exposes inversion-friendly hooks (`DbLifecycleService`), keeping the source module dependent on abstractions.
- Treat the first ordered migration (`resources/db/migrations/<shortuuid7>_bootstrap.up.sql`) as the bootstrap schema so new databases are created by running migrations end-to-end instead of applying a separate seed file; record the bootstrap migration identifier in metadata for traceability.
- Keep an idempotent `ensure` entry point for operators while clarifying that the `source` module primarily needs an "ensure" signal via `DbLifecycleService.ensure()` to guarantee database readiness without owning SQLite logic directly.
- Reshape per-source manifests to host module-owned state beneath a shared `modules` map (e.g., `modules.source`, `modules.db`) so future modules can persist authoritative metadata without clobbering the source layout; provide a first-run migration path for existing manifests.
- Promote manifest handling into a reusable infrastructure layer by introducing `raggd.modules.manifest`, giving modules a shared API for discovery, migrations, locking, and atomic writes while the `source` and `db` modules depend on its abstractions.
- Mirror database health metadata (bootstrap identifier, current migration, last vacuum, checksum) into each source manifest's `modules.db` entry through lifecycle hooks so downstream modules can observe state without opening the database file, while retaining the database ledger as the source of truth.
- Provide a lightweight migration runner that discovers files named `<shortuuid7>_<slug>.up.sql` / `<shortuuid7>_<slug>.down.sql`, executes them in order, and records applied migrations for `upgrade`/`downgrade` commands.
- Integrate with `raggd checkhealth` to surface missing databases, schema drift, or stale maintenance signals (vacuum cadence) while allowing future modules to add checks without tight coupling.
- Document operator expectations (when to run `ensure`, `upgrade`, `downgrade`, `reset`, `vacuum`, how to author SQL snippets) while keeping the migration runner lightweight to avoid unnecessary complexity in this greenfield scope.

## Non-Goals
- Maintaining legacy `db.sql` files or honoring pre-existing database layouts; we accept breaking changes because there is no backwards-compatibility requirement.
- Designing vector database generation, synchronization, or query orchestration.
- Building parser ingestion tooling; those consumers will call into the database module through the provided interfaces.
- Introducing an ORM or SQL templating DSL beyond a lightweight loader for `.sql` files.

## Behavior (BDD-ish)
- Given a workspace with configured sources, when the user runs `raggd db ensure [<name> ...]`, then the command ensures `<source>/db.sqlite3` exists by running migrations from the bootstrap file forward (no standalone seed), writes or refreshes `schema_meta` rows containing `bootstrap_shortuuid7`, `head_migration`, `ledger_checksum`, `created_at`, and `updated_at`, mirrors the latest migration metadata into the source manifest's `modules.db` entry via `ManifestService`, and exits zero if every database is reconciled.
- Given a source database, when the user runs `raggd db upgrade [<name> ...]`, then the CLI reads unapplied migration files (named `<shortuuid7>_<slug>.up.sql`), executes them in order while recording success in a migrations ledger, mirrors the new head migration into the source manifest's `modules.db` entry, and stops with exit code `1` if any migration fails.
- Given a source database, when the user runs `raggd db downgrade [<name> ...]`, then the CLI rolls back the most recent applied migration using the corresponding `.down.sql` file, supports multi-step downgrades via `--steps`, reconciles the `modules.db` manifest entry with the new head migration, and aborts with exit code `1` if the necessary down file is missing or fails.
- Given a source database, when the user runs `raggd db info <name> [--json] [--schema]`, then the CLI reports the database path, `bootstrap_shortuuid7`, last applied migration, `last_vacuum_at`, ledger checksum, size on disk, and notes if the schema differs from the migration chain checksum or if pending migrations exist; `--schema` dumps the current schema (ordered by `sqlite_schema`) so operators can diff against migrations, and exit code `1` signals drift or staleness. The JSON view also surfaces the mirrored `modules.db` snapshot from `manifest.json` for observability.
- Given a source database, when the user runs `raggd db reset <name> [--force]`, then the CLI optionally confirms destructive action, removes the existing file, reruns migrations starting at the bootstrap migration to rebuild the database, refreshes metadata (ledger checksum, timestamps) while preserving the original `bootstrap_shortuuid7`, and updates both `schema_meta` and the mirrored `modules.db` manifest entry; without `--force`, prompt in interactive terminals.
- Given the user runs `raggd db vacuum [<name> ...] [--workers N]`, then the module executes `VACUUM` (and optionally `ANALYZE`) in parallel workers, updates `schema_meta.last_vacuum_at`, aggregates any failures, and emits exit code `1` if any vacuum fails.
- Given the user runs `raggd db run <file.sql> [-p key=value ...] [-q/--quiet] [<name> ...]`, then the CLI loads the SQL (resolving relative to workspace or explicit path), substitutes named parameters safely, echoes the realized SQL unless `--quiet`, executes against each selected source, prints row counts or tabular results, and aggregates any errors before setting exit codes.
- Given the `source` module needs database lifecycle behavior (e.g., ensuring existence during `raggd source enable`), then it delegates to `DbLifecycleService.ensure()` to emit the necessary ensure signal, receives the reconciled `bootstrap_shortuuid7` and head migration metadata, and persists the mirrored details into `manifest.json.modules.db` through `ManifestService` without touching SQLite APIs directly—relying on the shared `config.db.manifest_*` settings to locate the `modules` namespace so keys stay consistent across modules and enabling dependency inversion plus easier testing.
- Given the user runs `raggd checkhealth`, then the health aggregator invokes the database module hook to flag sources missing a database, with schema checksum drift, pending migrations, or with `last_vacuum_at` older than the configured threshold (default 7 days), cross-checking the on-disk database ledger against each source manifest's `modules.db` entry to surface desync and returning actionable remediation hints.
- Given a workspace with a legacy `manifest.json` that lacks the `modules` namespace, when either the `source` or `db` module invokes `ManifestService.migrate()`, then the service creates the `modules` map, relocates existing source metadata under `modules.source`, seeds `modules.db`, rotates a timestamped backup, and stamps a `modules_version` so subsequent runs are idempotent while emitting structured logs for operators.

## Implementation Notes (Engineering Detail)
### Module layout
- Create `src/raggd/modules/manifest/` as an infrastructure package with `service.py` (ManifestService + interfaces), `migrator.py` (legacy to modules layout), and `backups.py` (timestamped backup helpers). Provide a slim `cli.py` only if operators need standalone manifest tooling—otherwise export service bindings via `__init__.py` for importers.
- Create `src/raggd/modules/db/` mirroring other CLI modules: `cli.py` (Typer command group), `service.py` (lifecycle interfaces + adapters), `migrations.py` (runner + file parsing), `health.py` (checkhealth hook), and `manifest.py` (manifest mirror utilities powered by `ManifestService`). Keep the public module export in `__init__.py` minimal so other packages depend on abstractions, not concrete helpers.
- Stage migration assets under `resources/db/migrations/` and expose them via `importlib.resources.files`. Ensure packaging includes this directory.

-### Manifest service contract
- Provide `ManifestService` as the single entry point for manifest consumers. Core capabilities:
  - `load(source: SourceRef) -> ManifestSnapshot` returning typed accessors for `modules.*` entries and legacy fallbacks.
  - `write(source: SourceRef, mutate: Callable[[ManifestSnapshot], None], *, backup: bool = True) -> ManifestSnapshot` performing locked, atomic writes with automatic `.bak` rotation and checksum verification.
  - `migrate(source: SourceRef, *, dry_run: bool = False) -> ManifestMigrationResult` used by both `source` and `db` paths when restructuring legacy manifests; supports in-memory dry runs for tests.
  - `with_transaction(source: SourceRef)` contextmanager that pairs writes with optional callbacks so `DbLifecycleService` can roll back DB operations when manifest persistence fails.
- Manifest settings helpers (`manifest_root_key()`, `manifest_db_key()`, etc.) live alongside the service so all modules calculate keys consistently.
- Maintain unit tests covering lock behavior, backup rotation limits, migration idempotency, and error propagation. Provide contract tests verifying the `source` module can read/write its payload via `ManifestService` without knowledge of the underlying JSON structure.

### Lifecycle service contract
- Define `DbLifecycleService` as the single entry point consumed by `source` (and future modules). Suggested signature (with dataclasses for clarity):
  - `ensure(source: SourceRef, *, eager_upgrade: bool = True) -> EnsureResult`
  - `upgrade(source: SourceRef, *, steps: int | None = None) -> MigrationResult`
  - `downgrade(source: SourceRef, *, steps: int | None = None) -> MigrationResult`
  - `reset(source: SourceRef, *, force: bool = False) -> EnsureResult`
  - `vacuum(source: SourceRef, *, analyze: bool = False) -> VacuumResult`
  - `run_sql(source: SourceRef, sql: str, params: Mapping[str, Any]) -> SqlRunResult`
- `SourceRef` should encapsulate source name and resolved paths to avoid each caller recomputing layout. `EnsureResult` and friends should include `bootstrap_shortuuid7`, `head_migration`, `ledger_checksum`, and timestamps so manifest mirroring works without extra I/O.

### Migration runner
- Parse `<shortuuid7>_<slug>.<direction>.sql` filenames into `(uuid7_slug, slug, direction)` and validate:
  - `shortuuid7` is a 12-char (Crockford base32) string derived from the third-party `uuid7` library. Persist both the canonical UUID7 and shortened form so we can assert ordering.
  - Add regression tests proving the shortened representation preserves the chronological ordering guarantees of UUID7 across thousands of samples.
  - Require `.up` and `.down` pairs for every slug after bootstrap; the bootstrap migration deliberately omits `.down` so downgrades stop at the initial schema and `reset` remains the only way to drop the database entirely.
- Store migrations in `schema_migrations` with columns: `id INTEGER PK`, `slug TEXT UNIQUE`, `direction TEXT CHECK(direction IN ('up','down'))`, `checksum TEXT NOT NULL`, `applied_at TEXT NOT NULL`, `shortuuid7 TEXT NOT NULL`. Maintain a composite index on `(slug, direction)` for rollback lookups.
- Execute migrations inside a transaction per database. Wrap each migration in `BEGIN; ...; COMMIT;` and fall back to `ROLLBACK` on exceptions while surfacing the error to the CLI.
- Compute migration checksums (e.g., SHA256 of normalized SQL content) when files are loaded; persist in ledger and compare on each run to detect drift.

### Schema metadata
- `schema_meta` should store stable metadata in a single-row table (enforced by `CHECK(id = 1)`): `id INTEGER PRIMARY KEY DEFAULT 1`, `bootstrap_shortuuid7 TEXT NOT NULL`, `head_migration TEXT NOT NULL`, `ledger_checksum TEXT NOT NULL`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `last_vacuum_at TEXT`, `last_sql_run_at TEXT`.
- `bootstrap_shortuuid7` is derived from the bootstrap migration filename so operators can trace lineage without a separate slug field; it does not rotate across resets.
- `ledger_checksum` is the hash of concatenated applied migrations (shortuuid7 + slug + checksum) and is used to detect manual tampering.

### Manifest synchronization
- Reshape each source `manifest.json` to introduce a top-level `"modules"` object that namespaces ownership. Existing source fields migrate into `modules.source`, while this feature adds a dedicated `modules.db` payload:
  ```json
  {
    "modules": {
      "source": {
        "... existing source manifest fields ..."
      },
      "db": {
        "bootstrap_shortuuid7": "01HX8F7N9D0K",
        "head_migration": "01HX8F7N9D0K_bootstrap",
        "ledger_checksum": "sha256:...",
        "last_vacuum_at": "2025-10-04T18:30:00Z",
        "last_ensure_at": "2025-10-04T18:30:00Z",
        "pending_migrations": []
      }
    }
  }
  ```
  Additional modules (e.g., future vector stores) will follow the same convention (`modules.<module_name>`). Preserve any non-module metadata (timestamps, provenance) at the top level.
- On first run, `raggd.modules.manifest.ManifestService` performs a manifest migration when it detects legacy manifests without `modules`: create the `modules` map, move known source fields under `modules.source`, seed `modules.db`, and record the migration version to avoid repeated transforms. Provide a dry-run mode for tests and log the transition for operators.
- `DbLifecycleService` should depend on `ManifestService` so writes happen transactionally with database updates. Treat manifest persistence as canonical: if a manifest write fails, roll back the database transaction, emit telemetry, and fail the command unless `config.db.manifest_strict` is overridden.
- The `source` module must route all manifest reads/writes (ensure, reset, health sync) through `ManifestService`, replacing any direct JSON manipulation with calls to shared helpers. Extend existing source tests to assert the delegation and surface a TODO list for modules that have yet to adopt the new service.

### CLI concurrency and multi-source handling
- `ensure`, `upgrade`, and `downgrade` should default to serial execution per source to keep migration ordering deterministic. Allow `--workers` for advanced operators but gate behind feature flag until we can guarantee thread-safe SQLite access.
- `vacuum` supports a worker pool via `concurrent.futures.ProcessPoolExecutor` to avoid GIL contention. `config.db.vacuum_concurrency` accepts `"auto"` (map to `min(cpu_count(), 4)` for now) or an explicit integer ≥ 1.
- When commands target multiple sources, aggregate partial failures: collect per-source results, print a summary table, and exit `1` if any source failed while still continuing for the rest.

### `db run` execution
- Resolve the SQL file relative to the current workspace by default, but allow absolute paths (including locations outside the workspace) since these scripts are ad-hoc; guard with a `db.run.allow_outside` setting that defaults to `true` and emit a warning when crossing workspaces.
- Support multi-statement scripts via `executescript`, but pipe each statement through the same connection transaction to ensure atomicity. Provide a `--autocommit` escape hatch for long-running scripts (default `False`, overridable via config) so maintenance jobs can stream changes when needed.
- Parameter substitution uses `sqlite3` named parameters. Parse `--params key=value` into a dictionary; coerce JSON-looking values (`{}`, `[]`, numbers) automatically for convenience, and surface the realized parameter map in verbose mode (mask values if `--quiet`).
- For `SELECT` statements, render a tabular preview (limit configurable via `--limit` default 20). For DML, report affected row counts.

### Health integration
- Implement `DbHealthProvider.check(source: SourceRef) -> list[HealthIssue]` that compares the on-disk database with manifest data. Issues to emit: missing database file, unapplied migrations, ledger checksum drift, vacuum staleness (configurable threshold), manifest mismatch (`bootstrap_shortuuid7`/head).
- Register the provider with the existing health aggregator so `raggd checkhealth` automatically includes database findings alongside other modules.

### Configuration defaults
- Add `db` section to `raggd.defaults.toml`:
  ```toml
  [db]
  migrations_path = "resources/db/migrations"
  manifest_modules_key = "modules"
  manifest_db_module_key = "db"
  vacuum_max_stale_days = 7
  vacuum_concurrency = "auto" # accepts "auto" or integer >= 1
  ensure_auto_upgrade = true
  run_allow_outside = true
  run_autocommit_default = false
  manifest_strict = true
  drift_warning_seconds = 0
  ```
- Surface these values through the existing settings loader so CLI and services share one source of truth—no separate `settings.db` artifact required—and make the `source` module read/write manifest fields via the same `manifest_*` settings to avoid drift between modules.
- Expose manifest-oriented knobs (e.g., backup retention count, file lock timeout) via either the existing `[db]` block or a future `[manifest]` section, but implement them inside `ManifestService` so feature modules never parse raw config keys themselves.

### Packaging & module registration
- Update `pyproject.toml` so the runtime dependency on the `uuid7` helper (`uuid7>=0.1`) is declared. Provide a `db` optional dependency/extra that lists `uuid7>=0.1` and ensure the `[dependency-groups].modules` bundle includes the new group so module toggles can request the extra when necessary.
- Ship migration SQL files with the wheel by extending `tool.setuptools.package-data` (or equivalent) to cover `raggd/modules/db/resources/migrations/**` so upgrades/downgrades work out of the box.
- Add a `ModuleDescriptor` entry for the database module in the module registry with `name="db"`, a human-readable description, default toggle enabled, extras referencing the new `db` group, and the database health hook registered so `ModuleRegistry.evaluate()` exposes it alongside existing modules.

### Testing scaffolding
- Provide pytest fixtures that spin up ephemeral workspaces under `.tmp/db-module-tests/<test-name>` and install sample migrations (bootstrap + one incremental) so integration tests remain deterministic, alongside a `manifest_service` fixture that can emit both legacy and `modules.*` layouts for reuse across `source` and `db` test suites.
- Write contract tests against `DbLifecycleService` using an in-memory SQLite file via `sqlite3.connect(f"file:{path}?mode=rwc", uri=True)` to simulate disk behavior without polluting the repo.
- For CLI tests, reuse the `CliRunner` harness, intercept manifest writes via a temporary file, and assert on telemetry events. Include downgrade edge cases (missing `.down` file) and vacuum concurrency scenarios with a fake executor.

## Constraints & Dependencies
- Constraints: rely on bundled SQLite; commands operate offline; multi-process vacuum must default to a conservative worker count (`min(4, cpu_count())`); CLI output follows existing Typer formatting (rich tables + structured logs).
- Migrations live in `resources/db/migrations/` and are ordered lexicographically by their `<shortuuid7>_<slug>` prefix; the runner must guard against duplicate slugs and checksum drift between `.up`/`.down` pairs.
- Architecture: the database module exposes interfaces (`DbLifecycleService`, `SqlRunner`, `DbHealthProvider`) registered with the module registry. The `source` module depends on these abstractions to maintain DIP, adopts the shared `manifest_*` settings when reading/writing `modules.source`, and allows swapping implementations for tests. The new `raggd.modules.manifest` package owns manifest IO plumbing (`ManifestService`, migrator, backups) so both modules interact through its seam instead of reimplementing JSON semantics.
- Bootstrap migration lives in `resources/db/migrations/<shortuuid7>_bootstrap.up.sql`; checksum or hash used for drift detection should be reproducible across platforms and shared between the migrations ledger and manifest mirror.
- `manifest.json` remains the authoritative per-source manifest; the database module contributes a `modules.db` payload (`bootstrap_shortuuid7`, head migration UUID7, last vacuum timestamp, ledger checksum, pending migrations) via lifecycle hooks while guarding against other modules editing those fields directly.
- Maintain a migrations ledger table (`schema_migrations`) that stores applied `<shortuuid7>_<slug>`, applied direction, checksum, and applied timestamp for auditability.

## Security & Privacy
- Databases live within the user workspace; no external network access.
- `db run` masks parameter values in logs when `--quiet` is set, avoids positional string interpolation, and emits telemetry warnings when executing SQL sourced outside the workspace.
- `reset` requires confirmation to avoid accidental data loss.

## Telemetry & Operability
- Emit structured events (`db-ensure`, `db-upgrade`, `db-downgrade`, `db-reset`, `db-vacuum`, `db-run`) with source name, duration, worker id where relevant, and outcome status.
- Record `created_at`, `last_vacuum_at`, optional `last_sql_run_at`, `bootstrap_shortuuid7`, and last applied migration in `schema_meta` (or equivalent view) for health introspection; expose the same data via `raggd db info --json` and mirror the values into `manifest.json.modules.db` for consumers that rely on manifest introspection.
- Provide exit codes (`0` success, `1` degraded/partial failure, `2` fatal error) and document them for automation.

## Decisions & Follow-ups
- Adopt the third-party `uuid7` library for migration IDs; shorten to 12-character Crockford base32 strings, persist the canonical UUID7 alongside the slug, and add regression tests proving ordering is preserved after shortening.
- Drop the dedicated schema slug identifier; rely on the bootstrap migration identifier, head migration, and ledger checksum for correlation.
- Treat the bootstrap migration as the floor for downgrades: `.down` files are optional for bootstrap, `downgrade` stops at that point, and destructive resets flow through `db reset`.
- Allow `db run` to execute SQL from outside the workspace when the operator points to an absolute path; gate with `config.db.run_allow_outside` (default `true`) and retain `--autocommit` for long-running scripts with a configurable default.
- Manifest writes are canonical: on failure, roll back database changes and fail the operation by default (`config.db.manifest_strict = true`).
- Manifests now namespace module state beneath `modules.<module>`; record the migration version (`manifest.modules_version`) after restructuring so repeated runs are idempotent and add tests for legacy manifests transforming in-place.
- Vacuum concurrency honors `config.db.vacuum_concurrency`, accepting `'auto'` (maps to `min(cpu_count(), 4)`) or explicit integers ≥ 1.
- `DbLifecycleService.ensure()` auto-applies pending migrations when `config.db.ensure_auto_upgrade` is `true` (default); operators can opt out by setting it to `false` and invoking `upgrade` manually.
- Surface health thresholds (vacuum staleness, manifest drift) via `raggd.defaults.toml` so operators can override per environment; default behavior fails immediately on manifest drift (`drift_warning_seconds = 0`).

## Rollout / Revert
- Ship behind a `modules.db` feature toggle in `raggd.defaults.toml`, defaulting to enabled in development once validated and gating the manifest restructuring logic behind the same switch.
- Rollout consists of introducing the new module, migrating existing manifests into the `modules` layout during the first `ensure` (take a timestamped `.bak` backup before rewriting), and updating the `source` module to depend on the database service abstractions.
- To revert, disable the feature toggle, restore backups of manifests (or run a provided rollback helper that flattens `modules` back to the legacy layout), and re-enable the previous `source` module behavior that inlines database creation (via Git revert).

## Definition of Done
- [ ] `raggd db` command group implements `ensure`, `upgrade`, `downgrade`, `info`, `vacuum`, `run`, and `reset` behaviors with consistent logging and parameter handling.
- [ ] Workspace sources exclusively use `<source>/db.sqlite3`; the database module owns creation, migrations, and destructive operations via `DbLifecycleService`, and the source module delegates through the abstraction.
- [ ] Bootstrap migration (first migration file), checksum drift detection, and migration application (including short UUID7 slug validation + shortened-ordering tests) are covered by unit tests and documented for contributors.
- [ ] `raggd db info --schema` and manifest strictness behaviors are verified by CLI/contract tests, including failure paths when manifest writes fail under the default configuration.
- [ ] Health integration surfaces missing databases, schema drift, manifest/database desync, and stale vacuum timestamps with CLI/health tests verifying exit codes and messaging.
- [ ] `manifest.json.modules.db` persists authoritative database metadata (`bootstrap_shortuuid7`, head migration UUID7, ledger checksum, last vacuum timestamp, pending migrations) via lifecycle hooks, with regression tests ensuring the `source` module consumes the ensure signal without bypassing the service, uses the shared `manifest_*` settings when reading/writing manifests, and that legacy manifests are migrated into the `modules` layout.
- [ ] `raggd.modules.manifest` provides a documented, tested service abstraction for discovery, migrations, locking, backups, and atomic writes. Both the `source` and `db` modules call only into this service for manifest interactions, with contract tests demonstrating delegation and failure handling.
- [ ] Developer docs updated to cover new commands, bootstrap migration expectations, and maintenance workflows; manual smoke notes captured per workflow.
- [ ] Test coverage includes unit tests for lifecycle services, functional tests for CLI subcommands (with `.tmp` workspaces), migration upgrade/downgrade flows, and concurrency coverage for vacuum operations.
- [ ] Packaging and activation paths updated: `pyproject.toml` declares the `uuid7` dependency and `db` optional extra, migrations are included in package data, and the module registry exposes a `db` descriptor wired to the health aggregator.

## Ownership
- Owner: @matt
- Reviewers: @codex
- Stakeholders: @docs, @ops

## Links
- Related: .agents/tasks/feat/0003-db-module/attachments/schema-reference.md

## History
### 2025-10-04 16:19 PST
**Summary** — Draft spec for database module CLI
**Changes**
- Created initial spec outlining CLI surface, migration strategy, query wrappers, health integration, and rollout plan

### 2025-10-04 17:27 PST
**Summary** — Bootstrap via migrations and manifest mirroring
**Changes**
- Replaced the standalone schema seed with a bootstrap migration and updated reset/ensure flows accordingly
- Clarified the `ensure` signal contract between the CLI and `DbLifecycleService` while keeping the operator-facing command
- Added manifest mirroring requirements so database health metadata is shared with the source manifest and health checks

### 2025-10-04 18:05 PST
**Summary** — Re-scoped spec for greenfield database module
**Changes**
- Removed migration lifecycle in favor of single schema seed and `db.sqlite3` standardization
- Shifted responsibility to a dedicated lifecycle service to enforce dependency inversion with the source module
- Added commands/behaviors for ensure/info/reset and updated DoD, telemetry, and rollout sections accordingly

### 2025-10-04 19:16 PST
**Summary** — Reintroduced migrations and full DB delegation
**Changes**
- Added `upgrade`/`downgrade` commands with `<shortuuid7>_<slug>.<up/down>.sql` naming convention and ledger expectations
- Clarified that all lifecycle operations leave the `source` module and flow through `DbLifecycleService`
- Updated goals, behaviors, telemetry, constraints, and DoD to cover migration runner responsibilities

### 2025-10-04 20:42 PST
**Summary** — Added engineering detail and surfaced open questions
**Changes**
- Documented module layout, service contracts, migration runner expectations, manifest sync flow, and testing scaffolding
- Added configuration guidance and detailed command execution notes for multi-source handling and `db run`
- Captured outstanding decisions around short UUID7 generation, bootstrap downgrade strategy, manifest error handling, and concurrency defaults

### 2025-10-04 21:58 PST
**Summary** — Captured stakeholder decisions and tightened operator settings
**Changes**
- Committed to the third-party `uuid7` library with shortened-ordering tests and clarified downgrade floor at the bootstrap migration
- Allowed `db run` to execute outside-workspace SQL (configurable), added `info --schema`, and made manifest writes fail-safe by default
- Expanded configuration defaults and DoD items to reflect new settings for concurrency, auto-upgrade, manifest drift, and CLI schema reporting

### 2025-10-04 18:53 PST
**Summary** — Manifest restructuring for multi-module state
**Changes**
- Updated goals, behaviors, and manifest synchronization guidance to introduce a shared `modules` namespace with `modules.db` metadata and legacy-manifest migration
- Adjusted configuration defaults, decisions, rollout plan, and DoD to cover manifest backups, migration versioning, and cross-module extensibility

### 2025-10-04 22:30 PST
**Summary** — Clarified source adoption of manifest settings
**Changes**
- Documented that the `source` module reads/writes manifests via the shared `config.db.manifest_*` settings when delegating to the database service
- Updated configuration, constraints, behavior, and DoD text so cross-module manifest handling stays aligned

### 2025-10-04 23:05 PST
**Summary** — Captured packaging and module registry expectations
-**Changes**
- Added guidance for `pyproject.toml` (`uuid7` dependency, `db` optional extra, module bundle) and shipping migration SQL with the distribution
- Documented module registry requirements and updated the DoD to verify packaging + activation paths

### 2025-10-04 23:42 PST
**Summary** — Elevated manifest handling into shared infrastructure
**Changes**
- Introduced `raggd.modules.manifest` as the canonical manifest subsystem with service contract, migrator, and testing guidance
- Updated goals, behaviors, architecture, configuration, and DoD to ensure both `source` and `db` modules depend on the shared manifest service
- Added module layout, fixture guidance, and documentation expectations covering manifest delegation and backup semantics
