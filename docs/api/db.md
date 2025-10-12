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
- `raggd db info [<source>] [--schema] [--counts/--no-counts] [--json]`: Report database paths,
  migration head identifiers, ledger checksums, and optionally dump the schema.
  Row counts are enabled by default; pass `--no-counts` to skip running `COUNT(*)`
  queries when you only need metadata.
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
  backup policy, drift warnings, run safeguards, vacuum heuristics, and info table
  counts). Tune `info_count_timeout_ms` (default `1000`) to cap each `COUNT(*)`
  query's runtime in milliseconds, and `info_count_row_limit` (default `500_000`)
  to skip reporting counts once the limit is exceeded.
- The optional extra group for this module is named `db`; it does not require
  third-party packages because UUID7 identifiers are generated in
  `raggd.modules.db.uuid7`.

## Assets
Migration SQL files live under
`src/raggd/modules/db/resources/db/migrations`. Packaging includes these assets
so `uv build` bundles them automatically.

## Row Counts
`raggd db info` gathers per-table row totals when `--counts` is active (the default).
Tables skipped because of timeouts or row limits are surfaced alongside a condensed
summary so operators can decide whether to adjust settings or retry. Sample output:

```console
$ raggd db info demo
Database info for demo
  database_path: /workspaces/demo/.raggd/db/demo.sqlite
  head_migration_shortuuid7: 01J5DWCZG5C21NYT5W2RP9J9TE
  table_counts:
    customers: 1245
    orders: 973
    webhook_events: skipped
  table_counts_skipped:
    - webhook_events (timeout; timeout_ms=1000, elapsed_ms=1012)
  table_counts_skipped_summary:
    timeout: 1
  counts note: Some table counts were skipped (timeout: 1)
```

When `--no-counts` is used the command omits table count sections. Structured
consumers receive the same `table_counts`, `table_counts_skipped`, and
`table_counts_skipped_summary` fields within the metadata payload, which keeps the
CLI and future `--json` responses consistent.

## Health Checks
The module registers a health provider surfaced through `raggd checkhealth`.
Checks cover missing databases, manifest drift, stale vacuum timestamps, and
pending migrations.
