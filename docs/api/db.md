# Database Module CLI

The `raggd db` command group manages SQLite lifecycle for every workspace
source. Each command delegates to `DbLifecycleService`, which coordinates
migrations, manifest mirroring, and health metadata so other modules only
consume abstractions.

## Commands
- `raggd db ensure [<source> ...]`: Create databases as needed, run pending
  migrations, and refresh manifest snapshots.
- `raggd db upgrade [<source> ...]`: Apply outstanding migrations upward in
  lexicographic order, updating the manifest mirror and schema ledger.
- `raggd db downgrade [<source> ...] [--steps <int>]`: Roll back applied
  migrations, stopping at the bootstrap migration.
- `raggd db info [<source>] [--schema] [--json]`: Report database paths,
  migration head identifiers, ledger checksums, and optionally dump the schema.
- `raggd db vacuum [<source> ...]`: Run SQLite `VACUUM` with concurrency limits
  derived from `config.db.vacuum_concurrency`.
- `raggd db run [<source>] --sql-file <path>`: Execute ad-hoc SQL against a
  source database. Supports absolute file paths when
  `config.db.run_allow_outside = true`.
- `raggd db reset [<source> ...] [--force]`: Remove and recreate databases while
  preserving the bootstrap migration identifier.

## Configuration
- Toggle the module in `raggd.toml` via `[modules.db]` or with CLI flags.
- Default settings ship in `raggd.defaults.toml` under `[db]` (manifest paths,
  backup policy, drift warnings, run safeguards, and vacuum heuristics).
- The optional extra group for this module is named `db`; it does not require
  third-party packages because UUID7 identifiers are generated in
  `raggd.modules.db.uuid7`.

## Assets
Migration SQL files live under
`src/raggd/modules/db/resources/db/migrations`. Packaging includes these assets
so `uv build` bundles them automatically.

## Health Checks
The module registers a health provider surfaced through `raggd checkhealth`.
Checks cover missing databases, manifest drift, stale vacuum timestamps, and
pending migrations.
