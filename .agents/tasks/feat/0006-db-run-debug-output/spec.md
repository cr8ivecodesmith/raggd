# raggd db run Debug Output — Spec

## Summary
Introduce an opt-in debugging experience for `raggd db run` so operators can inspect result sets from SQL scripts without leaving the CLI, while keeping the default behavior quiet for automation.

## Goals
- Add a `--debug` (and `--no-debug`) flag to `raggd db run` that streams result sets for statements returning rows.
- Emit structured, row-limited output (e.g., up to 50 rows per statement, 256 characters per cell) through the existing logger at DEBUG level and surface a friendly stdout summary when the flag is set.
- Replace the current `executescript` call with statement-aware execution so we can detect and log `SELECT` / `PRAGMA` / `EXPLAIN` statements while preserving transactional semantics and existing options (e.g., `--autocommit`).
- Cover the new behavior with CLI/service tests exercising quiet vs. debug modes and large result truncation.

## Non-Goals
- Building a full interactive SQL shell or streaming every row for arbitrarily large result sets.
- Adding an external SQL parsing dependency if a lightweight tokenizer meets our needs.
- Changing default logging verbosity for other `raggd db` subcommands.

## Behavior (BDD-ish)
- Given a workspace and a SQL script that ends with `SELECT COUNT(*) FROM files`, when the user runs `raggd db run sample1 script.sql`, then the command executes silently (aside from success/failure messaging) and preserves today’s behavior.
- Given the same script, when the user runs `raggd db run --debug sample1 script.sql`, then the CLI logs each statement at DEBUG with the SQL text, row/sample counts, and prints a capped table of returned rows to stdout before the usual “Executed …” message.
- Given a script with multiple statements including DDL, when `--debug` is supplied, only statements that return rows emit tabular output; DDL updates still log success without rows.
- Given a script that returns more than the configured row limit, when `--debug` is supplied, the CLI emits the first N rows and indicates truncation so operators know more rows exist.

## Constraints & Dependencies
- Constraints: Keep memory usage bounded when printing large result sets; avoid tight coupling to SQLite internals beyond `sqlite3`.
- Upstream/Downstream: No external service dependencies; ensure `checkhealth` and manifest updates remain untouched.

## Security & Privacy
- Warn in docs/logs that `--debug` may surface sensitive row data; inherit existing workspace permissions and do not redact by default.

## Telemetry & Operability
- Leverage existing structured logging (`logger.debug("db-run-statement", …)`) to capture SQL text (truncated) and row counts when `--debug` is enabled.
- Consider emitting a follow-up log when truncation occurs so operators can detect potential data loss in the preview.

## Rollout / Revert
- Feature flag is the `--debug` CLI option; default remains disabled.
- No migrations or data backfills; revert by removing the flag and helper utilities if needed.

## Definition of Done
- [ ] Behavior verified via automated tests (unit + CLI integration).
- [ ] User/operator docs updated (`docs/cli/db.md` or equivalent) with usage examples and cautions.
- [ ] Structured logging added for debug output with sensible truncation.
- [ ] Row and column truncation constants documented/configurable.
- [ ] Existing `--autocommit` flow covered to ensure statement splitting respects transaction boundaries.

## Ownership
- Owner: @tbd
- Reviewers: @codex
- Stakeholders: @matt, @ops

## Links
- Related: `.tmp/sql/check-parser-data.sql` (motivating script lacking direct output)

## History
### 2025-10-12 15:05 UTC
**Summary** — Drafted spec for `raggd db run` debug output
**Changes**
- Captured goals/non-goals, behavior, and DoD for the new CLI flag so implementation can be scoped in a follow-up task.
