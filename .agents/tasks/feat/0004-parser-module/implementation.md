# Parser Module CLI — Implementation

## Understanding
- **Restate spec**: Build the `raggd parser` command group with `parse`, `info`, `batches`, and `remove` subcommands that drive a handler registry to tokenize, persist, and monitor source batches via the refreshed database schema and manifest metadata, while honoring `.gitignore`, max-token rules, and concurrency settings.
- **Assumptions / Open questions**: Concurrency audit, delegated chunk recomposition helpers, and vector index messaging remain open follow-ups that we must analyze during implementation; tree-sitter grammars supplied by `tree_sitter_languages` are acceptable without re-measuring footprint; database locking helpers already exist but might need extension for chunk slices.
- **Risks & mitigations**: Large schema/migration changes could disrupt existing DB consumers (mitigate with migration gating and fixture updates); handler dependency gaps may degrade UX (surface in health checks and docs); concurrency race conditions could corrupt batches (enforce transactional writes and locking; add stress tests).

## Resources
### Project docs
- `.agents/tasks/feat/0004-parser-module/spec.md` — Source of truth for goals, behaviors, handler rules, and follow-ups.
- `.agents/tasks/feat/0003-db-module/spec.md` — Reference for existing DB abstractions, locking helpers, and attachment schema alignment.
- `src/raggd/modules/db/migrations/` — Current migration history; informs how to introduce `chunk_slices` and manifest updates.
- `.agents/guides/engineering-guide.md` — DI, seam-first guidance for structuring parser services and handler boundaries.
- `.agents/guides/patterns-and-architecture.md` — Module layout and logging conventions to reuse.
- `.agents/guides/workflow.md` & `.agents/guides/workflow-extras/implementation-tpl.md` — Workflow expectations and template followed here.
### External docs
- https://libcst.readthedocs.io/ — API reference for Python handler AST traversal.
- https://github.com/tree-sitter/tree-sitter/tree/master/docs — Grammar usage patterns for JavaScript, Markdown, HTML, CSS handlers.
- https://github.com/openai/tiktoken — Token counting API surface used across handlers.
- https://github.com/cpburnz/python-pathspec — `.gitignore` parsing behavior that we may adopt.

## Impact Analysis
### Affected behaviors & tests
- CLI flows (`parse`, `info`, `batches`, `remove`) → end-to-end Typer CLI tests and manifest assertions.
- Handler chunking/deduplication → unit tests per handler plus shared delegation/hashing cases.
- Database batch lifecycle (`chunk_slices`, tombstones, reuse) → migration tests, integration DB tests, and regression coverage for attachment spec alignment.
- Health/telemetry emission → health hook tests and structured log snapshot coverage.
### Affected source files
- **Create**: `src/raggd/cli/parser.py`, `src/raggd/modules/parser/__init__.py`, handler modules under `src/raggd/modules/parser/handlers/`, SQL files under `src/raggd/modules/parser/sql/`, new migrations under `src/raggd/modules/db/migrations/`.
- **Modify**: `src/raggd/cli/__init__.py` (register sub-app), `src/raggd/modules/__init__.py` (add module descriptor), `src/raggd/modules/db/...` (lock helpers, lifecycle wiring), config defaults (`src/raggd/config/defaults.py`), manifest schemas, and test fixtures.
- **Delete**: Legacy `raggd parse` CLI entry and obsolete parser prototypes/tests.
- **Config/flags**: Introduce `modules.parser.*` settings; ensure defaults and workspace overrides cover handler toggles, max tokens, concurrency, fail-fast, and gitignore behavior.
### Security considerations
- Ensure traversal respects `.gitignore` to avoid ingesting secrets; continue redacting sensitive paths in telemetry logs.
- Validate Typer commands sanitize user-supplied paths; avoid arbitrary SQL by using parameterized queries executed via DB service.
- Confirm new dependencies do not execute arbitrary code (tree-sitter grammars are data files; validate versions in lockfile).

## Solution Plan
- **Architecture/pattern choices**: Follow façade + handler strategy aligning with `patterns-and-architecture.md`, using `ParserService` to orchestrate IO and delegating parsing to language-specific handlers that implement a shared protocol. Keep data persistence behind repository abstractions to maintain seam-first design.
- **DI & boundaries**: Use constructor injection for services (config, token encoder, DB lifecycle) following `engineering-guide.md`. Handlers receive a `ParseContext` object so they stay stateless; Typer layer resolves services via module registry.
- **Stepwise checklist**:
  - [x] Phase 1 — CLI scaffolding & configuration (see Phase 1 notes below).
  - [x] Phase 2 — Database groundwork (see Phase 2 notes below).
  - [x] Phase 3 — Core parser services (see Phase 3 notes below).
  - [ ] Phase 4 — Handler implementations (see Phase 4 notes below).
  - [ ] Phase 5 — Persistence & recomposition support (see Phase 5 notes below).
  - [ ] Phase 6 — CLI subcommand behaviors (see Phase 6 notes below).
  - [ ] Phase 7 — Concurrency & telemetry hardening (see Phase 7 notes below).
  - [ ] Phase 8 — Documentation & cleanup (see Phase 8 notes below).

### Phase 1 — CLI scaffolding & configuration
- Add `raggd parser` Typer app, load settings, wire module descriptor, and deprecate the legacy command while keeping tests green.
- Establish dependency injection seams so later phases can plug services without circular imports.
- Define the full `modules.parser` configuration surface (handlers map with `null` inheritance, `general_max_tokens`, `max_concurrency`, `fail_fast`, `gitignore_behavior`) and expose defaults plus workspace overrides through `info` output.

### Phase 2 — Database groundwork
- Design the `chunk_slices` schema, write migrations + SQL files, update DB services and manifests, and migrate fixtures/tests.
- Confirm migration ordering with `DbLifecycleService` and refresh fixtures via `raggd db run` helpers.
- Bake in required columns (`chunk_id`, `parent_symbol_id`, `part_index`, overflow metadata, hashes, timestamps) plus constraints so recomposition and tombstone logic align with the spec.
- Author parser-owned CRUD statements in dedicated `src/raggd/modules/parser/sql/*.sql` files and exercise them through `raggd db run` (or the db service wrapper) so we keep query plans inspectable per spec.

### Phase 3 — Core parser services
- Implement `ParserService`, traversal/hashing utilities, batch orchestration, and handler registry with dependency/health reporting.
- Reuse `raggd.core` traversal, manifest, and hashing helpers instead of duplicating logic; surface any missing seams for follow-up fixes.
- Define extension/shebang mapping tables and hot-plug hooks so handler selection mirrors the spec’s precedence rules (explicit path overrides > shebang > extension > default text).
- Ensure handler registry respects settings toggles and dependency probes before dispatching.
- Wire traversal to `.gitignore` parsing via `pathspec`, normalize paths, stream hashes, and cache tree-sitter parsers to meet performance notes.
- Scope traversal to CLI-provided files/directories when present before walking the full source target so `parse` honors fine-grained inputs.
- Standardize token counting on `tiktoken`’s `cl100k_base` encoder (configurable override) and expose encoder selection via the context shared with handlers.
- Incorporate handler version identifiers into file/symbol hashing so unchanged detection bumps when handler heuristics evolve.
- Persist manifest metadata (`last_batch_id`, run timestamps, handler versions, handler notes, warning/error counts) and set health states to `OK/DEGRADED/ERROR` when fallbacks or failures occur.

### Phase 4 — Handler implementations
- [x] Establish handler protocol scaffolding, dependency probes, and registry wiring so fallbacks land on the text handler by default.
- [x] Text handler: implement double-newline paragraph splits with indentation fallback, emit stable byte/line offsets, and collapse to a single chunk when heuristics fail.
- [x] Markdown handler: combine a fast heading splitter with tree-sitter verification, attach intro text forward, delegate fenced code blocks (e.g., ```python```), retain front-matter + inline metadata, and stamp chunk offsets for recomposition.
- [x] Python handler: use `libcst` to capture modules/classes/functions, decorators, docstrings, grouped class attributes, and emit overflow slices with parent linkage + metadata.
- [x] JavaScript/TypeScript handler: use tree-sitter to detect modules, exports, classes, re-exports; honor configuration toggles for TS/TSX routing, split large classes into constructor/method slices, and route `.tsx`/`.jsx` segments into HTML delegation when enabled.
- [x] HTML handler: leverage tree-sitter for structural grouping, delegate `<script>` blocks to JS and `<style>` blocks to CSS, normalize whitespace while preserving offsets, and emit metadata linking child delegates.
- [x] CSS handler: apply tree-sitter grouping, maintain cascade context/whitespace rules, split large rule blocks by selector group, and ensure delegated metadata stays symmetric with HTML/JS.
- [x] Shared delegation utilities: confirm delegated child chunks persist under handler namespaces with parent references ready for recomposition helpers in Phase 5.

### Phase 5 — Persistence & recomposition support
- Implement chunk write pipelines, delegation linkage, recomposition helpers (covering follow-up #2), and unchanged-detection logic with tombstone handling.
- Derive deterministic `chunk_key` values (batch id + handler namespace + path + offsets) and ensure overflow metadata is logged and stored for diagnostics.
- Stage file/symbol/chunk CRUD in transactions, leveraging `DbLifecycleService` locks per batch before committing, while persisting delegated child chunks under their handler namespaces with parent references for recomposition utilities.
- Treat `chunk_slices` as the canonical parser artifact. Plan a follow-up migration that introduces a `chunk_assemblies` (name TBD) join table mapping stable `chunk_id` values to one-or-many slice rows, then have the existing `chunks` table reference assemblies instead of storing duplicate text. Edges/vectors can migrate to the same assembly key, keeping VDB materialization decoupled from slice storage.

### Phase 6 — CLI subcommand behaviors
- Flesh out `parse`, `info`, `batches`, `remove`, ensuring batch validation, warnings about vector indexes, and concurrency controls respecting follow-up #3.
- Leverage shared manifest readers from `raggd.core` so CLI output stays consistent with other modules.
- `parse`: surface a `--fail-fast` flag that flips the parser settings for the run, pass through explicit file/directory arguments to the traversal layer, and ensure logs report when scope filtering occurs.
- `info`: surface last batch id (git SHA/uuid7), handler coverage, dependency gaps, and current config overrides.
- `batches`: list recent batches with file/symbol/chunk counts, timestamps, health flags, and limits per CLI contract.
- `remove`: protect the latest successful batch unless `--force`, enforce dependency checks, and warn that vector indexes require a later `raggd vdb sync`.

### Phase 7 — Concurrency & telemetry hardening
- Audit DB locks (follow-up #1), add structured logs/metrics, stress tests for parallel parses, and finalize health hook integration.
- Capture degraded handler states in telemetry when dependency fallbacks trigger.
- Verify health checks ensure manifest/db alignment (`modules.parser.last_batch_id` vs `batches`) and enforce chunk-slice integrity (contiguous part indices, valid delegated parent references) before surfacing `OK` status.

### Phase 8 — Documentation & cleanup
- Update user docs/config samples, finalize release notes, and remove superseded code/tests.
- Highlight handler fallback behavior and recomposition guarantees in docs for downstream consumers.

## Test Plan
- **Unit**: handler chunking/token splitting, hashing utilities, manifest serialization, recomposition helpers, configuration parsing, CLI option validation.
- **Contract**: persistence repository tests covering SQL queries executed via `raggd db run` harness, ensuring chunk slices obey schema contracts.
- **Integration/E2E**: CLI runs against fixture sources (single/multi-source, handler fallback, delegations, large file splits), migrations applied end-to-end, concurrency stress scenario using temporary repos.
- **Manual**: run `raggd parser parse` on sample workspace with mixed languages, verify `info`/`batches` output, exercise `remove` warning about vector indexes, inspect logs for dependency degradation cases.

## Operability
- **Telemetry**: structured logs via `structlog` for batch lifecycle, metrics stored in `modules.parser.health` (files parsed, reused ratio, split counts, handler durations, warnings).
- **Dashboard/alerts**: extend existing health checks to surface parser status; plan follow-up dashboard cards once metrics accumulate.
- **Runbooks / revert steps**: document migration rollback path (SQLite snapshot + migration down), handler dependency installation guidance, and vector sync follow-up when removing batches.

## History
### 2025-10-07 14:53 PST
**Summary**
Implemented shared delegation utilities so delegated chunks adopt child handler namespaces while preserving parent linkage for recomposition.

**Testing**
- `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=.tmp/test-workspace uv run pytest --no-cov tests/modules/parser/test_handler_markdown.py tests/modules/parser/test_handler_html.py tests/modules/parser/test_handler_javascript.py` *(HTML/JS suites skipped: optional tree_sitter_languages dependency unavailable in sandbox)*

**Changes**
- Added a delegation helper module producing normalized chunk identifiers and metadata for delegated handlers.
- Updated Markdown, HTML, and JavaScript/TypeScript handlers to emit delegated chunks under child namespaces with parent handler references.
- Extended handler unit tests to assert delegation metadata and shared chunk identifiers.

**Notes**
- Re-run the handler suites once the `parser` dependency extras install so tree-sitter powered delegations execute instead of skipping.

### 2025-10-07 18:45 PST
**Summary**
Completed HTML handler for Phase 4 item 5 with tree-sitter-backed structural grouping and delegation.

### 2025-10-07 20:25 PST
**Summary**
Completed CSS handler for Phase 4 by adding tree-sitter grouping, cascade-aware metadata, selector-based splitting, and comment coverage.

### 2025-10-07 15:18 PST
**Summary**
Pinned tree-sitter to a version compatible with `tree_sitter_languages` and updated HTML/CSS/JS handlers to work with tuple-based point APIs so the tree-sitter suites execute instead of skipping.

**Testing**
- `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=/home/matt/Projects/matt/raggd/.tmp/test-workspace uv run pytest --no-cov tests/modules/parser/test_handler_markdown.py tests/modules/parser/test_handler_html.py tests/modules/parser/test_handler_css.py tests/modules/parser/test_handler_javascript.py`

**Changes**
- Restricted the parser extra to `tree-sitter>=0.21,<0.22`, regenerated the lockfile, and resynced the environment via `uv sync --group parser`.
- Normalized HTML/CSS/JS collectors for slot initialization and tuple point handling, expanded CSS to accept `*_statement` nodes and continue splitting selectors under token caps.
- Tweaked JavaScript re-export symbol naming so default exports preserve their local identifiers in symbol metadata.

**Testing**
- `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=.tmp/test-workspace uv run pytest --no-cov tests/modules/parser/test_handler_css.py` *(skipped: optional tree_sitter_languages dependency absent in sandbox)*

**Changes**
- Added `CSSHandler` with rule/at-rule/keyframe traversal, selector splitting under token caps, and fallback handling for syntax errors.
- Wired the handler into parser exports and registry plus introduced focused CSS unit coverage scaffold.

**Notes**
- Shared delegation utilities remain for the next Phase 4 substep.
- Re-run the parser handler suites once `tree_sitter_languages` extras install so CSS/HTML/JS paths execute without skips.
**Changes**
- Added `HTMLHandler` with structural element chunking, inline script/style delegation, and normalized offset metadata.
- Registered the handler factory/export in the parser registry for `.html`/`.htm` sources.
- Introduced `tests/modules/parser/test_handler_html.py` covering structural chunks and delegated inline blocks.
**Testing**
- `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser/test_handler_html.py` *(skipped: `tree_sitter_languages` optional dependency absent in sandbox)*.
**Notes**
- CSS handler and shared delegation utilities remain open for Phase 4 completion.
### 2025-10-07 15:30 PST
**Summary**
WIP: Python handler landed for Phase 4 item 4.

### 2025-10-07 02:26 PST
**Summary**
Completed JavaScript/TypeScript handler for Phase 4 substep with tree-sitter exports, class slicing, and JSX delegation.

**Testing**
- `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=.tmp/test-workspace uv run pytest --no-cov tests/modules/parser/test_handler_javascript.py` *(skipped: `tree_sitter_languages` optional dependency absent in sandbox)*
**Changes**
- Implemented the libcst-backed Python handler with docstring/metadata capture, module docstring chunking, and token-cap splitting plus depot fallback when dependencies are absent.
- Registered the handler factory/export and added `tests/modules/parser/test_handler_python.py` covering dependency errors, symbol extraction, and overflow splitting heuristics.
- Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser` (two Python handler tests skipped when `libcst` is unavailable, tokenizer skip unchanged).
**Notes**
- Remaining Phase 4 work includes JavaScript/TypeScript, HTML, CSS handlers, and shared delegation utilities.
- Consider expanding Python handler coverage once parser extras are installed to exercise full libcst behavior in CI.

### 2025-10-07 01:29 PST
**Summary**
WIP: Markdown handler heuristics landed for Phase 4 item 3.
**Changes**
- Added a Markdown handler with heading-aware chunking, intro attachment, front-matter capture, and fenced code delegation stubs.
- Registered the handler factory and introduced `tests/modules/parser/test_handler_markdown.py` covering front-matter, heading hierarchy, fenced code delegation, and fallback behavior.
- Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser` (tokenizer test still skipped without `tiktoken`).
**Notes**
- Remaining Phase 4 work covers Python, JS/TS, HTML, CSS handlers, and shared delegation utilities.

### 2025-10-06 23:05 PST
**Summary**
WIP: Text handler heuristics landed as part of Phase 4 item 2.
**Changes**
- Implemented paragraph and indentation chunking in the text handler with byte/line metadata and fallback coverage.
- Added `tests/modules/parser/test_handler_text.py` to cover paragraph splits, indentation fallback, and single-chunk collapse.
- Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser` (one optional tokenizer test still skipped without `tiktoken`).
**Notes**
- Remaining Phase 4 work covers Markdown, Python, JS/TS, HTML, CSS handlers, and shared delegation utilities.

### 2025-10-06 14:09 PST
**Summary**
Finished Phase 2 database groundwork with the chunk slices schema and parser SQL resource pack.
**Changes**
- Added migration `06CVG7EEZ5YH` introducing the `chunk_slices` table plus supporting indexes and teardown script.
- Extended database manifest snapshots to capture `last_sql_run_at` and ensured the lifecycle backend mirrors the value.
- Packaged parser SQL statements (`upsert`/`select`/`delete`) under `modules/parser/sql` with resource helpers and targeted tests.
- Updated packaged data configuration and database/backend tests; new parser SQL tests exercise chunk slice CRUD roundtrips. Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest tests/modules/db tests/modules/parser` (coverage threshold not met because only the focused suites were executed).
### 2025-10-06 02:45 PST
**Summary**
Closed architect feedback about handler alignment and persistence details.
**Changes**
- Added Phase 3 items for extension/shebang mapping and handler-version-aware hashing.
### 2025-10-06 17:00 PST
**Summary**
Closed out Phase 3 by validating the parser service stack and documenting completion.
**Changes**
- Confirmed handler registry, traversal, hashing, and manifest orchestration behave as expected; no additional code changes required.
- Marked the Phase 3 checklist item complete and noted outstanding work for later phases.
- Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser` (one optional tokenizer test skipped when `tiktoken` is absent).
### 2025-10-06 16:46 PST
**Summary**
Landed ParserService planning/manifest scaffolding for Phase 3.
**Changes**
- Added parser run/manifest models and `ParserService` composing registry, traversal, hashing, and manifest updates.
- Exposed new service types via module exports and built focused unit coverage for planning, fallbacks, and manifest health.
- Ran `UV_CACHE_DIR=.tmp/uv-cache uv run pytest --no-cov tests/modules/parser/test_service.py`.
- Expanded handler bullets with offset guarantees, Markdown dual parsing, JS/TS toggles, and CSS cascade expectations.
- Clarified Phase 5 on deterministic chunk keys, overflow logging, and staged transactional CRUD.
### 2025-10-06 02:30 PST
**Summary**
Tightened plan to close spec-alignment gaps.
**Changes**
- Added Phase 2 work for dedicated SQL files executed via `raggd db run`.
- Extended parser services to honor explicit path scopes, default to `cl100k_base`, and persist expanded manifest metadata.
- Expanded CLI plan to include `parse` subcommand fail-fast flag wiring and Phase 7 health validations covering manifest/db and chunk-slice integrity.
### 2025-10-06 12:08 PST
**Summary**
Completed Phase 1 CLI scaffolding with parser configuration defaults.
**Changes**
- Added parser-specific configuration models, defaults, and serialization updates.
- Introduced the `raggd parser` Typer app with stub subcommands and legacy alias messaging.
- Renamed the parser module descriptor/extras and refreshed tests covering config and CLI output.
### 2025-10-06 02:06 PST
**Summary**
Added per-phase subheadings to the solution plan.
**Changes**
- Introduced detailed phase notes referenced by the stepwise checklist for easier expansion.
- Retained granular handler checklists while grouping supporting work under dedicated headings.
### 2025-10-06 02:00 PST
**Summary**
Aligned plan details with parser spec requirements.
**Changes**
- Expanded phases to call out configuration keys, schema linkage, traversal caching, and manifest health wiring.
- Clarified `info`/`batches`/`remove` responsibilities so CLI work mirrors documented behavior.
### 2025-10-06 01:58 PST
**Summary**
Split Phase 4 into granular handler sub-tasks.
**Changes**
- Added checkbox sub-items covering handler protocol setup, per-language implementations, and shared delegation utilities.
- Emphasized incremental delivery ahead of persistence work reviews.
### 2025-10-06 01:55 PST
**Summary**
Expanded phase checklist to nail handler heuristics and core service reuse.
**Changes**
- Broke down phases with sub-bullets covering `raggd.core` integration and language-specific parsing rules.
- Clarified telemetry/documentation follow-through around dependency fallbacks and recomposition.
### 2025-10-06 01:32 PST
**Summary**
Initial implementation plan drafted from parser spec.
**Changes**
- Captured understanding, impacts, phased execution plan, tests, and operability considerations aligned with workflow template.
