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
  - [ ] Phase 1 — CLI scaffolding & configuration: add `raggd parser` Typer app, load new settings, wire module descriptor, and deprecate legacy command while keeping tests green.
    - Establish dependency injection seams so later phases can plug services without circular imports.
    - Define the full `modules.parser` configuration surface (handlers map with `null` inheritance, `general_max_tokens`, `max_concurrency`, `fail_fast`, `gitignore_behavior`) and expose defaults plus workspace overrides through `info` output.
  - [ ] Phase 2 — Database groundwork: design `chunk_slices` schema, write migrations + SQL files, update DB services and manifests, and migrate fixtures/tests.
    - Confirm migration ordering with `DbLifecycleService` and refresh fixtures via `raggd db run` helpers.
    - Bake in required columns (`chunk_id`, `parent_symbol_id`, `part_index`, overflow metadata, hashes, timestamps) plus constraints so recomposition and tombstone logic align with the spec.
  - [ ] Phase 3 — Core parser services: implement `ParserService`, traversal/hashing utilities, batch orchestration, and handler registry with dependency/health reporting.
    - Reuse `raggd.core` traversal, manifest, and hashing helpers instead of duplicating logic; surface any missing seams for follow-up fixes.
    - Ensure handler registry respects settings toggles and dependency probes before dispatching.
    - Wire traversal to `.gitignore` parsing via `pathspec`, normalize paths, stream hashes, and cache tree-sitter parsers to meet performance notes.
    - Persist manifest metadata (`last_batch_id`, handler versions, warning/error counts) and set health states to `OK/DEGRADED/ERROR` when fallbacks or failures occur.
  - [ ] Phase 4 — Handler implementations: deliver concrete handlers in manageable increments while keeping delegation metadata consistent.
    - [ ] Establish handler protocol scaffolding, dependency probes, and registry wiring so fallbacks land on the text handler by default.
    - [ ] Text handler: implement double-newline paragraph splits with indentation fallback, emitting a single chunk when heuristics fail.
    - [ ] Markdown handler: chunk per heading, attach intro text forward, delegate fenced code blocks (e.g., ```python```), and preserve inline code inside parent chunks.
    - [ ] Python handler: use `libcst` to capture modules/classes/functions, decorators, docstrings, and grouped class attributes with token overflow splitting.
    - [ ] JavaScript/TypeScript handler: use tree-sitter to detect modules, exports, classes, and re-exports; split large classes into constructor/method slices and route `.tsx`/`.jsx` through HTML delegation when configured.
    - [ ] HTML handler: leverage tree-sitter for structural grouping, delegate `<script>` blocks to JS and `<style>` blocks to CSS, and emit metadata linking child delegates.
    - [ ] CSS handler: apply tree-sitter grouping, split large rule blocks by selector group, and ensure delegated metadata stays symmetric with HTML/JS.
    - [ ] Shared delegation utilities: confirm delegated child chunks persist under handler namespaces with parent references ready for recomposition helpers in Phase 5.
  - [ ] Phase 5 — Persistence & recomposition support: implement chunk write pipelines, delegation linkage, recomposition helpers (covering follow-up #2), and unchanged-detection logic with tombstone handling.
    - Persist delegated child chunks under their handler namespaces while storing parent references for recomposition utilities.
  - [ ] Phase 6 — CLI subcommand behaviors: flesh out `info`, `batches`, `remove`, ensuring batch validation, warnings about vector indexes, and concurrency controls respecting follow-up #3.
    - Leverage shared manifest readers from `raggd.core` so CLI output stays consistent with other modules.
    - `info`: surface last batch id (git SHA/uuid7), handler coverage, dependency gaps, and current config overrides.
    - `batches`: list recent batches with file/symbol/chunk counts, timestamps, health flags, and limits per CLI contract.
    - `remove`: protect the latest successful batch unless `--force`, enforce dependency checks, and warn that vector indexes require a later `raggd vdb sync`.
  - [ ] Phase 7 — Concurrency & telemetry hardening: audit DB locks (follow-up #1), add structured logs/metrics, stress tests for parallel parses, and finalize health hook integration.
    - Capture degraded handler states in telemetry when dependency fallbacks trigger.
  - [ ] Phase 8 — Documentation & cleanup: update user docs/config samples, finalize release notes, and remove superseded code/tests.
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
