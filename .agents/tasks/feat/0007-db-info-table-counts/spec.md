# raggd db info Table Counts — Spec

## Summary
Expand `raggd db info` so operators can see per-table row counts alongside existing manifest and migration metadata, making it easier to gauge database size without running ad-hoc SQL.

## Goals
- Add an opt-in flag (e.g., `--counts` / `--no-counts`) that toggles collection of `COUNT(*)` totals for all user tables in a source database.
- Surface row-count data consistently in both the human-readable CLI output and the structured payload returned to callers (future `--json` mode, service consumers, tests).
- Ensure the lifecycle service and backend expose a durable contract for row-count metadata so downstream tooling can reuse it.

## Non-Goals
- Estimating or sampling counts for performance; rely on exact `COUNT(*)`.
- Computing size metrics beyond row totals (e.g., page/byte stats).
- Changing manifest schemas or migration planning logic.

## Behavior (BDD-ish)
- Given a workspace with a populated source database, when `raggd db info source --counts` is executed, then the CLI prints existing metadata plus a `table_counts` section listing each table name and its row total, and logs the same data.
- Given a large table and the same command, when `--counts` is used, then the CLI completes without hanging indefinitely and emits a note when the aggregate query exceeds a configurable timeout threshold.
- Given the command is run without `--counts`, then it mirrors today’s output and does not execute row-count queries.

## Constraints & Dependencies
- Constraints: guard long-running count queries via timeout/PRAGMA limits; ensure counts respect the existing database lock to avoid concurrency issues.
- Upstream/Downstream: coordinate with docs in `docs/api/db.md` and any consumers expecting `DbLifecycleService.info(...).metadata`.

## Security & Privacy
- Row totals do not expose row contents but may hint at dataset size; document that sensitive aggregates can appear when using `--counts`.

## Telemetry & Operability
- Extend existing `db-info` logging payloads with `table_counts` when counts are enabled so operators can observe the data in log aggregators.
- Emit a structured warning log when a table count query times out or is skipped.

## Rollout / Revert
- Feature flag is the new CLI option (`--counts` defaulting to enabled unless `--no-counts` is provided).
- Revert path: drop the flag handling and metadata additions; no schema changes.

## Definition of Done
- [ ] Behavior verified via unit and CLI integration tests covering with/without counts and timeout handling.
- [ ] Docs updated (`docs/api/db.md` + examples) to describe the new flag and performance considerations.
- [ ] Logging includes `table_counts` when applicable, with truncation or fallback documented.
- [ ] Configuration surface documented for any timeout/limit constants.
- [ ] Existing manifests untouched; backward compatibility validated.

## Ownership
- Owner: @tbd
- Reviewers: @codex
- Stakeholders: @matt, @ops

## Links
- Related: Future follow-up to `.agents/tasks/feat/0006-db-run-debug-output`.

## History
### 2025-10-12 16:20 UTC
**Summary** — Drafted spec for table count enhancement
**Changes**
- Captured goals, behavior, and DoD for adding table count reporting to `raggd db info`.

