# raggd db info Table Counts — Implementation

## Understanding
- Extend the `raggd db info` command with a `--counts/--no-counts` option that, when enabled, gathers `COUNT(*)` totals for every user table and surfaces them in both CLI output and structured metadata returned from `DbLifecycleService.info`.
- Assumptions / Open questions:
  - Filter out SQLite internal tables (`sqlite_%`) and module bookkeeping tables (`schema_meta`, `schema_migrations`) so counts focus on user data; confirm stakeholders are fine excluding ledger tables.
  - Introduce a configurable timeout/row limit for counting to avoid blocking on very large tables; need agreement on default (e.g., 1s) and whether to skip tables exceeding it.
  - `--json` mode is referenced in docs but not yet implemented; counts metadata should be future-proof for that interface.
- Risks & mitigations:
  - Long-running `COUNT(*)` queries could stall CLI → use `set_progress_handler` with elapsed deadline to abort and record a warning.
  - Schema changes may break assumptions (e.g., views) → restrict to plain tables from `sqlite_schema`.
  - Added protocol parameter on `DbLifecycleBackend.info` requires updating fakes/tests; ensure all callsites updated and mypy adjusted.

## Resources
### Project docs
- `.agents/guides/engineering-guide.md` — reinforces seam-first updates to the backend/service boundary.
- `docs/api/db.md` — needs flag/documentation changes for operators.
- `src/raggd/modules/db/backend.py` — current info execution path to extend.
### External docs
- https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.set_progress_handler — mechanism to abort long-running counts.
- https://sqlite.org/lang_corefunc.html#count — semantics of `COUNT(*)`.

## Impact Analysis
### Affected behaviors & tests
- CLI behavior: new `--counts/--no-counts` flag, human output includes table counts → update `tests/cli/test_db.py` to cover both enabled/disabled paths.
- Backend/service behavior: `DbLifecycleService.info` returns `table_counts` metadata; counting logic covered by unit tests in `tests/modules/db/test_backend.py` and service boundary tests in `tests/modules/db/test_lifecycle.py`.
- Timeout/skip logging → add assertions in backend tests for warning log or returned sentinel.
### Affected source files
- Create: helper module (if needed) for counting? likely not; inline in backend.
- Modify: `src/raggd/cli/db.py`, `src/raggd/modules/db/lifecycle.py`, `src/raggd/modules/db/backend.py`, `src/raggd/modules/db/settings.py`, `src/raggd/core/config.py` (if exposing new setting), `docs/api/db.md`, relevant tests under `tests/cli`, `tests/modules/db`, config fixtures in `tests/core/test_config.py`.
- Delete: None.
- Config/flags: add `db.info_count_timeout_ms` (or similar) to defaults, expose via `DbModuleSettings`.
### Security considerations
- Row counts reveal dataset scale; ensure logs/CLI messaging notes potential sensitivity and relies on existing workspace permissions. No new secrets handled.

## Solution Plan
- Architecture/pattern choices: compute counts inside `SQLiteLifecycleBackend.info` to keep DB access localized; expose results via `DbInfoOutcome.metadata["table_counts"] = dict[str, int | None]`, where `None` indicates timeout/skipped.
- DI & boundaries: `DbLifecycleService.info` will pass `include_counts` flag through to backend and normalize returned metadata into top-level `table_counts` plus optional warning summary, maintaining existing manifest mutation pattern.
- Stepwise checklist:
  - [x] Extend `DbModuleSettings` (fields + loader + defaults) with count timeout/limits and update config serialization/tests.
  - [x] Update `DbLifecycleBackend.info` protocol + implementations (real + test doubles) to accept an `include_counts` flag and return count metadata.
  - [ ] Implement table counting in `SQLiteLifecycleBackend.info` with internal helper (filters tables, enforces timeout via progress handler, records skipped tables).
  - [ ] Update `DbLifecycleService.info` to surface `table_counts` in payload, track skip reasons, and enrich logging.
  - [ ] Add `--counts/--no-counts` option to CLI command, adjust output formatting to render counts as a nested section, and include note on skipped tables/timeouts.
  - [ ] Refresh docs in `docs/api/db.md` with flag description, sample output, and performance guidance.
  - [ ] Add/adjust tests across CLI, backend, lifecycle, and config to cover new behaviors and edge cases (timeout skip, disabled counts).

## Test Plan
- Unit:
  - backend helper counting tables returns accurate totals, skips `sqlite_%`, handles timeout path.
  - lifecycle service passes through include flags and structures payload as expected.
- Contract: ensure `DbLifecycleBackend` protocol changes reflected in fakes (`RecordingBackend`), maintaining API surface.
- Integration/E2E:
  - CLI invocation with `--counts` shows `table_counts` lines.
  - CLI without flag keeps legacy output (ensuring backward compatibility).
  - Scenario with artificially large table to trigger timeout (via monkeypatched handler) yields warning and sentinel value.
- Manual checks (if needed):
  - Run `raggd db info --counts` against sample workspace to verify UX.

## Operability
- Telemetry (logs/metrics): extend `db-info` log payload with `table_counts` and `table_counts_skipped` to aid observability.
- Dashboard/alert updates: none needed; existing logging suffices.
- Runbooks / revert steps: removing flag and metadata fields reverts feature; ensure docs mention runtime impact to inform rollback if counts cause performance issues.

## History
### 2025-10-12 15:30 UTC
**Summary**
- Authored implementation approach for `raggd db info` table counts.
**Changes**
- Documented understanding, impact, plan, tests, and operability considerations.

### 2025-10-12 16:11 UTC
**Summary**
- Landed configuration scaffolding for db info table counts.
**Changes**
- Added timeout/row limit fields to `DbModuleSettings` and `DbSettings`, refreshed defaults, serialization, and tests (`tests/modules/db/test_settings.py`, `tests/core/test_config.py`).

### 2025-10-12 16:55 UTC
**Summary**
- Updated lifecycle backend info contract for table count flag plumbing.
**Changes**
- Added `include_counts` parameter to backend protocols/implementations, returning placeholder `table_counts` metadata, and refreshed lifecycle tests to cover the new flag.
