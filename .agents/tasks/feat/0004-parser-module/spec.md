# Parser Module CLI — Spec

## Summary
Establish a `raggd parser` command group that owns parsing workflows end-to-end. The initial `parse` subcommand must walk configured sources, honor `.gitignore`, route files through language-aware handlers (defaulting to text), tokenize output with `tiktoken`, and persist normalized batches into the source database. The parser module becomes the single entry point for chunking, change detection, and parse telemetry so downstream embedding/vector modules inherit consistent, token-aware data.

## Goals
- Ship a Typer-based `raggd parser parse <source ...>` command plus scaffolding for `info`, `batches`, and `remove` helpers so the parser lifecycle is discoverable from one command group.
- Implement a pluggable handler registry inside `raggd.modules.parser` that maps extensions (and shebangs) to handlers, defaulting to `parser.text` when no specialization exists.
- Use `libcst` for Python and `tree-sitter` grammars (via `tree_sitter_languages`) for JavaScript/TypeScript, Markdown, HTML, and CSS; allow plain heuristics for text to minimize dependencies.
- Traverse requested folders/files under each source target, filter using `.gitignore` + workspace defaults, and build a deterministic processing queue compatible with source-level concurrency.
- Normalize handler output into the database schema (see `modules/db` migrations and `feat/0003` attachment schema doc), splitting oversized groups while preserving parent linkage so downstream batches can stitch sequences back together.
- Detect unchanged files/groups via stable hashes and skip database writes when content is identical, emitting telemetry for reused artifacts and pruning retired symbols through `last_seen_batch` tracking.
- Persist parser run metadata (batch id, handler versions, warning counts, errors) into the source manifest under `modules.parser` and surface health status through the module registry.
- Add configuration for handler toggles, global/per-handler max tokens, and a `max_concurrency` setting (`auto` default, minimum value 1) to control how many sources are parsed in parallel without violating database locks.

## Non-Goals
- Generating or syncing vector databases/embeddings (future work will consume parser output).
- Maintaining the legacy `raggd parse` entry point; the old command can be removed in favor of the new group.
- Running background daemons or live file monitoring; parsing remains an on-demand operation.
- Guaranteeing backwards compatibility with existing code and tests may be deleted to reduce bloat.

## Behavior (BDD-ish)
- Given a user runs `raggd parser parse <source>` and the source has a configured target, the CLI walks the target respecting `.gitignore`, selects handlers, persists new/changed entities into `<workspace>/sources/<name>/db.sqlite3`, updates `modules.parser` manifest metadata (last batch id, run timestamps, handler notes), and exits `0` on success.
- Given the user passes additional paths (`raggd parser parse <source> path/to/file.py path/to/dir`), traversal is constrained to those entries (still honoring `.gitignore`), missing paths are warned (not fatal), and remaining files continue to process.
- Given parsing multiple sources (`raggd parser parse src-a src-b`), the runner processes up to `max_concurrency` sources concurrently while serializing writes per source using the database module's lock to avoid cross-source contention.
- Given a handler raises an error, the CLI logs it, appends it to manifest metadata, continues with other files (unless `--fail-fast`), and returns exit status `1` if any file failed.
- Given a file hash and symbol hashes match the previously stored state, existing rows are reused without updates, yet `last_seen_batch` is marked with the current batch to keep retention accurate.
- Given symbols disappear relative to the prior batch, the parser marks `last_seen_batch` accordingly and removes dependent chunk segments so consumers can detect tombstones without stale references.
- Given a group exceeds applicable max tokens, the handler creates sequential parts linked via parent metadata, records the split in logs, and persists each segment with a stable `part_index`.
- Given a user runs `raggd parser info <source>`, the CLI reports the last successful batch id (git SHA preferred, uuid7 fallback), handler coverage, outstanding warnings/errors, and configuration diffs versus defaults.
- Given a user runs `raggd parser batches <source> [--limit N]`, the CLI lists recent batches with timestamps, ref (git SHA/uuid7), counts of files/symbols/chunks, and flags batch health.
- Given a user runs `raggd parser remove <source> <batch> [--force]`, the CLI validates no other modules reference the batch, deletes associated records (`files`, `symbols`, chunk slices), updates manifest metadata, and logs the action. Vector indexes are not removed automatically; the command warns about follow-up `raggd vdb sync <source>` work. Without `--force`, the command refuses if the batch is the latest successful parse.

## Architecture & Implementation Notes
### CLI & Module Surface
- Add a Typer sub-app in `src/raggd/cli/parser.py` with subcommands `parse`, `info`, `batches`, and `remove`. Register it under the main CLI such that `raggd parser ...` is the canonical entry point.
- Introduce `ParserService` (or similar façade) that orchestrates traversal, handler dispatch, database writes, and manifest updates. Keep CLI thin and testable.
- Extend the module registry (`raggd.modules`) with a `ModuleDescriptor` named `parser`, default-enabled, referencing the new optional dependency group `parsers` for packaging clarity. Sub-groups for individual handlers (e.g., `parser.python`, `parser.javascript`) declare their dependencies so users can opt-in/out.
- Reuse `raggd.core` facilities for configuration, logging, concurrency pools, and manifests instead of ad-hoc utilities; only extend them when hard requirements surface.

### Handler Framework
- Implement a registry keyed by canonical language identifiers with hooks for extension mapping, shebang detection, and handler enable/disable checks.
- Provide a shared `ParseContext` object (source metadata, settings, token encoder, logger, tree-sitter parser cache) passed to handlers to avoid recomputing.
- Python handler: use `libcst` to build an AST, extract module-, class-, and function-level symbols, attach docstrings, and compute stable symbol paths (`module.Class.method`).
- JavaScript/TypeScript handler: use `tree-sitter-javascript` grammar to identify modules, classes, functions, exports, and top-level doc comments. Account for both `.js` and `.ts` (with configuration to disable TS until validated).
- Markdown handler: rely on `markdown-it-py` for structure, but also feed the document through `tree-sitter-markdown` to detect headings, code fences, and lists. Create chunks per heading section with optional code-block delegation to language-specific handlers when annotated with fences (e.g., ```python```).
- HTML handler: use `tree-sitter-html`; extract `<script>` and `<style>` blocks for delegation to JS/CSS handlers while maintaining parent-child metadata to reconnect inline code to the containing element.
- CSS handler: use `tree-sitter-css` to group rulesets, keyframes, and custom properties. Provide fallbacks for syntax errors by chunking text blocks.
- Text handler: implement simple heuristics (paragraph/sentence splitting) with optional indentation-aware grouping for config files. Acts as default for unsupported extensions.
- Ensure each handler returns a normalized structure (`file`, `symbols`, `chunks`) with token counts, docstrings, parent IDs, and stable hashes. When handlers delegate (e.g., Markdown to Python), persist delegated chunks under the child handler’s namespace while recording linkage back to the parent symbol so the reader can recompose the group later.

### Tokenization & Grouping
- Standardize on `tiktoken` encoders (default `cl100k_base`) for token counting. Allow per-handler overrides if downstream models demand alternative encodings.
- Max token rules: `general_max_tokens` acts as default cap, per-handler overrides take precedence, `auto` means accept handler-native groupings. When splitting, generate deterministic `chunk_key` values (`symbol_sha:part_index`).
- Record split metadata in the database for health reporting (e.g., part count, overflow reason) to help tune settings later.

### Traversal, Hashing & Caching
- Use existing workspace services plus `platformdirs`/`pathlib` utilities for cross-platform path normalization. Incorporate `.gitignore` filters via `pathspec` (add if absent) or reuse existing ignore utilities.
- Hash files with a streaming approach (e.g., SHA256) to avoid loading large files fully when not needed. Combine file hash + handler version to detect when re-parse is necessary.
- Cache tree-sitter parsers and compiled grammars per process to avoid repeated initialization overhead.

### Batches, Database & Schema Work
- Batch IDs default to git SHA (detected from target repo). When unavailable, generate uuid7 (see `modules/db/uuid7`). Store both the raw ref and derived short id in Manifest + `batches` table.
- The current `chunks` table requires a `vdb_id`, which blocks storing raw chunk text before embeddings exist. Introduce a new `chunk_slices` table decoupled from `vdbs` so parser output persists independently, updating existing migrations to eliminate tight coupling. Fix or delete impacted tests as part of the migration refresh and sync the attachment schema doc.
- Ensure parent-child linking for chunk splits is supported (e.g., add `part_index` and `parent_symbol_id` columns to the new table). Flag if additional manifest metadata is needed to expose this linkage.
- Keep parser-owned SQL in dedicated `*.sql` files (mirroring current db conventions) and execute them via the database service or `raggd db run` so query plans stay inspectable.
- Batch CRUD where possible: stage inserts/updates per table, verify alignment (e.g., orphan detection) before applying, and wrap operations in transactions to minimize lock churn.
- Leverage the database module’s locking helpers to serialize writes per source. Confirm the lock covers chunk-slice inserts and manifest updates to avoid race conditions when `max_concurrency > 1`.
- Reuse `DbLifecycleService` for migrations, but allow the parser to request schema upgrades if chunk tables change. Document that parser runs may trigger db migrations on first run.

### Logging, Telemetry & Error Handling
- Emit structured logs (via `structlog`) with fields for source, handler, batch, file, symbol counts, and warnings. Promote consistent log levels for recoverable parsing issues.
- Collect run metrics (files scanned, reused, skipped, split counts, handler durations) and store summary in `modules.parser.health` for health checks and future dashboards.
- Provide graceful degradation when optional handler dependencies are missing: keep the handler marked `enabled` by default, log a warning, record the dependency gap in the health manifest, fall back to the text handler for affected files, and mark health as `DEGRADED` rather than failing the entire run (unless the handler was explicitly enabled).

## Handler Rules
- **text**: Split on double newlines, limit paragraphs by max tokens, emit fallback single chunk when heuristics fail, track original offsets for reassembly.
- **markdown**: Chunk per heading (H1-H6); include preceding intro text with the next heading when necessary. Treat fenced code blocks with language hints as delegations—store metadata linking sub-chunks to parent heading and ensure inline code (` ` ) stays within the parent chunk.
- **python**: Use `libcst` to enumerate modules, classes, functions (including nested). Capture decorator names, docstrings, and type hints. Group class-level attributes into a single chunk when below token cap; otherwise split per method or attribute block.
- **javascript**: Use tree-sitter to capture modules, exports, functions, classes, and top-level constants. Split large classes into constructor/method groups, noting re-exports. Support both `.js` and `.ts`; add configuration to treat `.tsx`/`.jsx` as HTML handler delegations for embedded markup.
- **html**: Group by top-level structural regions (`<head>`, `<body>` sections, major container elements). Extract inline `<script>`/`<style>` tags and hand them to delegated handlers while keeping a parent shell chunk that references child chunk identifiers.
- **css**: Group by rule blocks (selectors, media queries, keyframes). Normalize whitespace for hashing and surface cascade comments. When splitting large media queries, retain parent metadata linking subparts to the outer rule.

## Settings
- `modules.parser.enabled`: master toggle (default `true`).
- `modules.parser.handlers`: map of handler name → `{enabled: bool, max_tokens: int|"auto"|null}`; `null` means “inherit the general cap”. Handlers stay enabled by default (except experimental ones); if runtime dependencies are missing we warn, degrade health, and temporarily route those files through the text handler until dependencies are installed.
- `modules.parser.general_max_tokens`: default token cap (default `2000`, accepts integer or `"auto"`).
- `modules.parser.max_concurrency`: integer ≥1 or `"auto"` (default). `auto` selects `min(cpu_count, len(sources))`, but never below 1.
- `modules.parser.fail_fast`: optional boolean to stop on first handler failure; default `false` for resiliency.
- `modules.parser.gitignore_behavior`: enum (`"repo"`, `"workspace"`, `"combined"`) to clarify ignore precedence; default `"combined"` to merge repo `.gitignore` with workspace-level ignores.
- Persist settings in default config and per-workspace overrides; expose via `raggd parser info` to aid debugging.

## Health Checks
- Register parser health hook per source:
  - `status=ERROR` when the most recent parser run failed, the manifest reports missing batch metadata, or schema migrations are pending.
  - `status=DEGRADED` when optional handlers fell back to text, chunk splits exceeded warning thresholds, or dependencies are missing.
  - `status=OK` when last success is recent (within configurable staleness window, default 7 days) and no outstanding warnings.
- Verify manifest/db alignment: ensure `modules.parser.last_batch_id` matches the latest entry in `batches` for that source.
- Validate chunk-slice integrity: confirm part indices form contiguous ranges and that delegated child chunks reference existing parents.
- Surface metrics: files parsed, reused percentage, handler durations, warning counts, and concurrency used. Store summary under `modules.parser.health` for `raggd checkhealth` consumption.

## Dependencies & Constraints
- Optional dependency group `parsers` already includes `libcst`, `markdown-it-py`, `tree-sitter`, `tree_sitter_languages`; ensure packaging splits handler extras (`parser.javascript`, `parser.markdown`, etc.) so users can trim install size. Rely on upstream size guidance (~80MB compressed for `tree_sitter_languages`) rather than re-measuring, and flag packaging complaints if they surface.
- Continue using `platformdirs` and standard `pathlib` utilities for cross-platform path handling. Introduce `pathspec` (if missing) to interpret `.gitignore` patterns consistently across OSes.
- Tree-sitter grammars can increase startup time; cache parser instances and guard against multi-threaded race conditions during initialization.
- SQLite access must reuse database module abstractions (`DbLifecycleService`, transaction context managers) to benefit from existing locking and vacuum strategies. Confirm the lock applies when chunk schema changes.
- Tokenization via `tiktoken` requires models to be installed; provide a clear error when the encoder is unavailable and allow configuration to pick alternative encoders if future embedding modules require them.

## Follow-ups & Risks
1. Concurrency audit: investigate the database locking layer under `max_concurrency` before implementation closes, document any starvation/deadlock risks, and propose mitigations if found.
2. Recomposing delegated chunks: prototype or at least outline reader utilities that can stitch delegated child chunks back to their parent groups using the new metadata so downstream modules do not lose structure.
3. Batch removal gap: highlight in docs/CLI help that `parser remove` leaves vector indexes in place until future `raggd vdb sync <source>` tooling exists; track user feedback in case we need interim scripts.

## History
### 2025-10-06 01:05 PST
**Summary** - Incorporated schema decoupling and CLI follow-up decisions
**Changes**
- Locked in `chunk_slices` migration strategy, SQL file handling, and batch CRUD expectations.
- Updated handler delegation rules, `parser remove` behavior, and dependency notes per new guidance.
- Reframed follow-ups toward concurrency auditing, recomposition tooling, and vector cleanup messaging.
### 2025-10-06 00:23 PST
**Summary** — Backfilled parser CLI restructure decisions
**Changes**
- Captured the `raggd parser` command group scope and handler registry architecture.
- Recorded concurrency tuning, settings surface, and schema split questions.
- Highlighted open follow-ups for delegation persistence and vector batch cascades.
