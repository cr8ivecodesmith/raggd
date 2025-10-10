# Vector DB (VDB) Module — Implementation

## Understanding
- Restate spec: Provide a first-class `raggd vdb` command group that owns vector database lifecycle per source. A VDB binds exactly to one parser `batch_id` and one `embedding_model_id`. The module materializes VDB-specific chunks, generates embeddings through a provider abstraction (OpenAI-first), writes vectors into a local FAISS IDMap index, and persists authoritative metadata in SQLite tables (`embedding_models`, `vdbs`, `chunks`, `vectors`). Operational visibility arrives via `info --json` and health hooks; query/retrieval is explicitly out of scope for this feature.
- Assumptions / Open questions
  - We will reuse the existing schema introduced by prior migrations (tables named above) without further changes for MVP.
  - The “rag” optional dependency group in `pyproject.toml` covers required packages (`faiss-cpu`, `openai`, `tiktoken`). We will not add new dependencies in MVP.
  - Provider dims for OpenAI models are known and stable; when unknown, we defer to provider-reported dim at first `sync` and then lock it in the DB for determinism.
  - Single-writer guarantees for FAISS operations can rely on coarse file locks at the `faiss_path` directory level; SQLite remains authoritative for metadata and can coordinate presence/drift checks.
  - Parser batch selection accepts an alias `latest` resolved to the most recent batch for the source; behavior mirrors the parser module UX.
- Risks & mitigations
  - Index/DB dim drift: Fail-fast checks comparing provider dim, `embedding_models.dim`, and existing index metadata; add repair guidance in error messages and `reset` flows.
  - Partial writes/corruption on rebuild: Use temp file writes and atomic swap for `--recompute`; hold a file lock to ensure single writer; keep `vectors` updates in a transaction.
  - Provider rate limits/timeouts: Implement batched requests, exponential backoff with jitter, and `--concurrency auto` derived from provider caps; surface progress and retries in logs.
  - Secrets exposure: Never log raw API keys or prompt content; redact token payloads and ensure `.gitignore` excludes vectors directories under sources.

## Resources
### Project docs
- `.agents/tasks/feat/0005-vdb-module/spec.md` — Primary functional and UX contract for the module.
- `.agents/tasks/feat/0004-parser-module/spec.md` — Upstream producer of `batches` and chunk slices; informs staleness checks.
- `.agents/tasks/feat/0003-db-module/spec.md` — DB lifecycle and manifest conventions; locking and schema patterns.
- `.agents/guides/workflow.md` — Collaboration, history, and validation expectations.
- `.agents/guides/patterns-and-architecture.md` — Module layout, logging, and anti-patterns to avoid.
- `.agents/guides/engineering-guide.md` — Dependency inversion and seam-first design for providers/services.
- `.agents/guides/styleguides.md` — CLI UX, naming, and docstring conventions.
### External docs
- FAISS Python API (faiss-cpu) — index types, IDMap wrappers, persistence semantics.
- OpenAI Python SDK (>=1.x) — embeddings API usage and batch limits.
- tiktoken — token length estimation for chunk payloads.

## Impact Analysis
### Affected behaviors & tests
- `vdb create` — validates batch/model, derives `faiss_path`, inserts `vdbs` row; no vectors created. Idempotent: re-running with the same `<source>@<batch>` and `embedding_model` results in a no-op. If a VDB with the same `name` exists but points to a different batch/model, fail-fast with a clear remediation message (use `reset --drop` or choose a new name).
- `vdb sync` — materializes `chunks`, generates embeddings, writes FAISS IDMap keyed by `chunk_id`, and records `vectors` rows.
- `vdb info` — reports selector, batch/model/dim, counts, `faiss_path`, last sync, and health notes; `--json` for machine output.
- `vdb reset` — deletes FAISS artifacts and clears `vectors`/`chunks`; optional `--drop` removes `vdbs` row.
- Health integration — contributes checks for missing index, dim mismatches, counts drift, stale relative to latest batch, and orphaned refs. Each health finding should include concise remediation guidance alongside `{ code, level, message }` (e.g., suggest `sync --recompute` on dim drift).
### Affected source files
- Create:
  - `src/raggd/cli/vdb.py` — Typer command group and CLI orchestration.
  - `src/raggd/modules/vdb/__init__.py` — module export and descriptor wiring.
  - `src/raggd/modules/vdb/service.py` — `VdbService` orchestrating create/sync/info/reset.
  - `src/raggd/modules/vdb/providers/__init__.py` — provider protocol and registry.
  - `src/raggd/modules/vdb/providers/openai.py` — OpenAI embedding provider implementation.
  - `src/raggd/modules/vdb/faiss_index.py` — FAISS IDMap wrapper, persistence, and file locking.
  - `src/raggd/modules/vdb/health.py` — health checks exposed to `checkhealth`.
  - `src/raggd/modules/vdb/models.py` — typed views for VDB metadata and info summaries.
- Modify:
  - `src/raggd/cli/__init__.py` — register `vdb` sub-app.
  - `src/raggd/modules/__init__.py` and `src/raggd/modules/registry.py` — add vdb module descriptor and health hook registration.
  - `pyproject.toml` — ensure `rag` extra remains accurate and document it in README/docs.
  - `docs/` — add user guide page for `vdb`.
- Delete: None.
- Config/flags:
  - `modules.vdb.*` settings: default provider/model, batching, concurrency, paths, retry policy.
  - Environment: `OPENAI_API_KEY` for OpenAI provider; optional overrides like `OPENAI_BASE_URL`.
### Security considerations
- Secrets: Read provider keys from environment; never persist or log them. Redact sensitive fields in structured logs.
- Data privacy: Embedding payloads may contain proprietary content; document this and provide `--dry-run` plus local-only provider pathway in future.
- Filesystem: Ensure vectors directory uses restrictive permissions and is covered by `.gitignore`.

## Solution Plan
- Architecture/patterns
  - Follow façade/service pattern: `VdbService` coordinates DB access, provider calls, and FAISS operations; CLI remains thin.
  - Provider abstraction defines `embed_texts(texts: list[str], model: str, *, max_batch: int, timeout: float) -> list[list[float]]` and surfaces `dim` and `caps` (max tokens/batch, concurrency hints). A registry resolves providers by key (`openai:<name>`), keeping the contract stable for future providers.
  - FAISS layer wraps an `IndexIDMap` over `IndexFlatIP` (cosine via normalized vectors) or `IndexFlatL2` based on metric; sidecar metadata persists `dim`, `metric`, `built_at`, and `vdb_id`.
  - Provider implementation details: define `EmbeddingsProvider` `Protocol` plus capability/model info dataclasses, load providers via keyed factories for DI seams, and ensure provider outputs stay immutable for testability.
  - OpenAI provider specifics: wrap the 1.x SDK, normalize text with `tiktoken`, batch respecting provider caps, implement exponential backoff with jitter, map HTTP/client errors to typed `Vdb*Error`s, and lock embedding dims into SQLite on first success when unknown.
  - Auto concurrency + logging: compute `min(cpu_count, provider_caps.max_parallel_requests, config_override or 8)` with floor 1, emit the resolved concurrency via structured logs, and surface provider throttle warnings for operability.
- DI & boundaries
  - Inject config, logger, DB connection factory, provider registry, and `FaissIndex` adapter into `VdbService`.
  - Keep SQL inside repository helpers and keep FAISS isolated behind `faiss_index.py` for testability.
- Stepwise checklist
  - [x] CLI: scaffold `raggd vdb` with `info/create/sync/reset` commands and shared context.
  - [x] Models: add typed views for `EmbeddingModel`, `Vdb`, and info summaries.
  - [x] Provider: implement OpenAI provider and registry; add `--concurrency auto` heuristic.
    - [x] Lock provider protocol + registry design in `providers/__init__.py` using seam-first DI per engineering guide.
    - [x] Document OpenAI provider behavior (batching, retries, dim resolution, token estimation, error mapping).
    - [x] Capture `auto` concurrency heuristic + logging aligned with config defaults and caps surfaced by providers.
    - [x] Outline provider-focused unit/contract tests leveraging stub provider seams.
  - [ ] FAISS: implement IDMap wrapper, persistence, locks, and sidecar metadata.
    - [ ] Wrap FAISS interactions in a `FaissIndex` adapter that hides `IndexIDMap` setup and exposes add/query/remove seams.
    - [ ] Persist the index file plus sidecar metadata (`dim`, `metric`, `built_at`, `vdb_id`) under the vectors directory with atomic writes.
    - [ ] Guard index rebuilds and writes with file locks to avoid concurrent corruption across CLI commands.
    - [ ] Implement load/validation flow that reads metadata, verifies dimensions/metric, and surfaces typed errors on mismatch.
  - [ ] Service: implement `create` (validate, derive path, insert), `sync` (materialize chunks, embed, persist), `info` (stats + health), `reset` (purge artifacts and rows).
  - [ ] Health: wire `vdb` checks into `checkhealth`.
  - [ ] Docs: update CLI help and add user guide.

## Test Plan
- Unit tests
  - Provider batching, retries, dim reporting, error translation, and auto concurrency resolution using stubbed provider caps and fake OpenAI responses.
  - FAISS adapter: add/update vectors, atomic rebuild, metadata read/write, and locking behavior (use temp dirs in `.tmp/`).
  - Service logic: idempotency for `create`, `sync --missing-only`, and conflict detection.
- Contract tests
  - Provider protocol: ensure any provider conforms to interface and error contracts, including capability reporting and dim persistence expectations.
- Integration/CLI tests
  - `raggd vdb create/sync/info/reset` end-to-end with a temporary workspace and seeded DB (fixtures).
  - Health aggregator includes vdb checks and flags mismatches/stale status.
- Manual checks
  - Run `uv run raggd vdb create` and `sync` against a small parsed source; verify FAISS files appear under `sources/<source>/vectors/<vdb>/` and `info --json` fields are sane.

## Operability
- Telemetry
  - Structured logs: action (`create|sync|reset|info`), source, vdb name, batch, model, dim, counts, durations, retries, and warnings.
  - Progress logs for long `sync` runs with periodic batch counters.
- Health
  - Contribute `vdb` checks to `checkhealth`: missing index, count drift, dim mismatch, stale vs latest batch, orphaned refs.
- Runbooks
  - Recovery: use `vdb reset --recompute` to rebuild a corrupted index; verify with `vdb info --json` and health checks.

## CLI Surface
- `raggd vdb create <source>@<batch|latest> <name> --model <provider:name|id>`
  - Validates batch and embedding model; records `dim` if known; derives `faiss_path` at `sources/<source>/vectors/<name>/index.faiss`; inserts into `vdbs`.
- `raggd vdb sync <source> [--vdb <name>] [--missing-only|--recompute] [--limit N] [--concurrency N|auto] [--dry-run]`
  - Materializes `chunks` per VDB, generates embeddings, writes FAISS IDMap keyed by `chunk_id`, records `vectors` rows, updates sidecar metadata; atomic swap for `--recompute`.
- `raggd vdb info [<source>] [--vdb <name>] [--json]`
  - Reports selector `<source>:<name>`, model/dim, batch, counts, `faiss_path`, last sync, and health notes; emits JSON when requested.
- `raggd vdb reset <source> [--vdb <name>] [--drop] [--force]`
  - Deletes FAISS artifacts and clears `vectors` and `chunks` for the VDB; with `--drop`, removes `vdbs` entry; `--force` bypasses confirmation.

## Data Model & Storage
- SQLite remains the source of truth:
  - `embedding_models(provider, name, dim)` — unique by provider+name; dim is authoritative post-resolution.
  - `vdbs(name, batch_id, embedding_model_id, faiss_path, created_at)` — unique name; binds to a single batch and model.
  - `chunks(id, symbol_id, vdb_id, header_md, body_text, token_count)` — one per symbol per VDB.
  - `vectors(chunk_id, vdb_id, dim)` — records presence/shape for external vectors.
- FAISS index
  - Uses `IndexIDMap` so vector payloads are stored externally keyed by `chunk_id`.
  - Sidecar `<faiss_path>.meta.json` — see Sidecar Metadata for required fields and behavior.

## Concurrency & Atomicity
- Single-writer guard via file lock on the vectors directory; retries with backoff if lock is held.
- `--recompute` ordering (no partial index exposure):
  1. Build new index and sidecar in a temp dir.
  2. Begin DB transaction: replace `vectors` for the VDB; commit.
  3. Atomically swap temp index dir into place; write final sidecar alongside.
  4. Release lock.
- `--missing-only` honors existing `vectors` rows and the FAISS index size; cross-checks for drift and repairs as needed.

## Interfaces & Contracts
### CLI UX (frozen)
- Commands: `info`, `create`, `sync`, `reset`.
- Create: `raggd vdb create <source>@<batch> <name> --model <provider:model|id>`
  - Example: `raggd vdb create docs@latest base --model openai:text-embedding-3-small`
  - Notes: resolves `latest`; validates model/dimension; writes only `vdbs`. Idempotent when the target VDB already exists with the same batch/model. Mismatches fail fast with remediation guidance.
- Sync: `raggd vdb sync <source> [--vdb <name>] [--missing-only|--recompute] [--limit N] [--concurrency N|auto] [--dry-run]`
  - `--missing-only`: embed only rows without vectors.
  - `--recompute`: rebuild embeddings and index atomically (see above).
  - `--concurrency`: integer or `auto` (see Concurrency & Config).
  - `--dry-run`: plan only; no DB/filesystem writes.
- Info: `raggd vdb info [<source>] [--vdb <name>] [--json]`
  - Lists VDBs; with `--json` prints schema below.
- Reset: `raggd vdb reset <source> [--vdb <name>] [--drop] [--force]`
  - Clears external artifacts and `vectors`/`chunks`; `--drop` removes `vdbs`.

### Info --json schema (stable)
- Per VDB object: `id`, `source_id`, `selector` (`<source>:<vdb-name>`), `name`, `batch_id`,
  `embedding_model` (`{ id, provider, name, dim }`), `metric`, `index_type`,
  `counts` (`{ chunks, vectors, index }`), `faiss_path`, `sidecar_path`,
  `built_at`, `last_sync_at`, `stale_relative_to_latest`, `health` (`[{ code, level, message }]`).

Example:
```
{
  "id": 42,
  "source_id": 7,
  "selector": "docs:base",
  "name": "base",
  "batch_id": 13,
  "embedding_model": { "id": 3, "provider": "openai", "name": "text-embedding-3-small", "dim": 1536 },
  "metric": "cosine",
  "index_type": "IDMap,Flat",
  "counts": { "chunks": 1200, "vectors": 1200, "index": 1200 },
  "faiss_path": "/workspace/sources/docs/vectors/base/index.faiss",
  "sidecar_path": "/workspace/sources/docs/vectors/base/index.faiss.meta.json",
  "built_at": "2025-10-10T17:20:35Z",
  "last_sync_at": "2025-10-10T17:20:35Z",
  "stale_relative_to_latest": false,
  "health": [ { "code": "ok", "level": "info", "message": "healthy" } ]
}
```

## Sidecar Metadata (required)
- Path: `<faiss_path>.meta.json`.
- Purpose: file-adjacent metadata enabling health checks and safe migrations.
- Fields:
  - `version` (int), `provider` (str), `model_id` (int), `model_name` (str),
    `dim` (int), `metric` (str), `index_type` (str), `vector_count` (int),
    `built_at` (ISO-8601), `checksum` (hex SHA-256 of index bytes).

Example:
```
{
  "version": 1,
  "provider": "openai",
  "model_id": 3,
  "model_name": "text-embedding-3-small",
  "dim": 1536,
  "metric": "cosine",
  "index_type": "IDMap,Flat",
  "vector_count": 1200,
  "built_at": "2025-10-10T17:20:35Z",
  "checksum": "1f1d4c...e9a"
}
```

- Lifecycle:
  - `create`: not written.
  - `sync --missing-only`: update `vector_count`, `built_at`, `checksum` after writes.
  - `sync --recompute`: sidecar written in temp dir; moved alongside on swap.
  - Deterministic pathing: `faiss_path` and sidecar location are derived deterministically from `<workspace>/sources/<source>/vectors/<vdb_name>/`.
  - Health reads sidecar to validate `dim`, counts, and detect drift via `checksum`.

## Concurrency & Config
- Auto concurrency:
  - Provider exposes `{ max_batch_size, max_parallel_requests }`.
  - `auto = min(cpu_count, max_parallel_requests, 8)` with floor 1.
  - Backoff with jitter on 429/5xx; batch size respects provider caps and token limits.
- Config keys (`raggd.defaults.toml` → override in `raggd.toml`):
  - `modules.vdb.provider` (default: `"openai"`)
  - `modules.vdb.model` (default: `"text-embedding-3-small"`)
  - `modules.vdb.metric` (default: `"cosine"`)
  - `modules.vdb.index_type` (default: `"IDMap,Flat"`)
  - `modules.vdb.batch_size` (default: `auto`)
  - `modules.vdb.concurrency` (default: `auto`)
  - `modules.vdb.paths.base` (default: `<workspace>/sources/<source>/vectors/<vdb_name>/`)
  - `modules.vdb.retry.initial_backoff_ms` (default: `500`)
  - `modules.vdb.retry.max_backoff_ms` (default: `5000`)
  - `modules.vdb.retry.max_retries` (default: `5`)
- Env: `OPENAI_API_KEY` required when `provider=openai` (never logged).

## Acceptance Criteria Mapping
- Info `--json` emits the documented schema, including `selector`, ids, counts, dim, paths, timestamps, and health entries.
- `create` is idempotent; `sync` supports `--missing-only` and `--recompute` with atomic behavior; `reset` clears `vectors/chunks` and optionally drops the `vdbs` row.
- Dimension compatibility is enforced across provider, DB, and FAISS index with fail-fast errors.
- `faiss_path` is deterministic; sidecar is written/updated during `sync` and includes a checksum.

## Error Handling
- Raise typed exceptions for CLI-friendly messages: `VdbCreateError`, `VdbSyncError`, `VdbResetError`, `VdbInfoError`.
- Include remediation tips on common failures (e.g., mismatched dims → suggest `reset --recompute`).

## Documentation & Help Sync
- Keep CLI `--help` strings in `src/raggd/cli/vdb.py` synchronized with `docs/learn/vdb.md` and examples in this implementation. Update both when flags/UX change.

## Deliverables & Files
- Core module and CLI files listed under Impact Analysis; tests under `tests/cli/test_vdb.py` and `tests/modules/vdb/*`.
- Documentation page `docs/learn/vdb.md` outlining usage and troubleshooting.

## Open Questions & Decisions
- Metric default: cosine similarity with vector normalization in MVP; expose metric enum in sidecar for future choices.
- Multiple VDBs per batch/model per source: allowed by unique VDB `name`; UX guidance recommends meaningful names.
- Provider selection overrides: CLI flag `--model` takes precedence over config defaults.

## History

### 2025-10-11 18:50 UTC
**Summary**
Provider contract tests scaffolded via stub provider
**Changes**
— Added `_RecordingStubProvider` contract tests in `tests/modules/vdb/test_provider_contracts.py` covering batching limits, token ceilings, and dimensional output checks.
— Ran `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=$PWD/.tmp/vdb-provider-contract uv run pytest --no-cov tests/modules/vdb/test_provider_contracts.py` and `UV_CACHE_DIR=.tmp/uv-cache uv run ruff check`.
— Checked off the provider test outline subitem in this implementation plan.

### 2025-10-11 16:20 UTC
**Summary**
Auto concurrency heuristic and logging implemented
**Changes**
— Introduced VDB module settings defaults (including concurrency controls) in `src/raggd/core/config.py` and `src/raggd/resources/raggd.defaults.toml` with supporting config tests.
— Added `resolve_sync_concurrency` helper with structured logging in `src/raggd/modules/vdb/providers/__init__.py` and exported via `src/raggd/modules/vdb/__init__.py`.
— Extended `tests/core/test_config.py` and `tests/modules/vdb/test_providers.py` to cover VDB settings validation and concurrency resolution behavior.
— Ran `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=$PWD/.tmp/vdb-concurrency uv run pytest --no-cov tests/core/test_config.py tests/modules/vdb/test_providers.py` and `UV_CACHE_DIR=.tmp/uv-cache uv run ruff check`.

### 2025-10-10 10:30 UTC
**Summary**
OpenAI provider documentation landed
**Changes**
— Added contributor runbook covering batching, retries, dim resolution, token estimation, and error mapping in `docs/contribute/openai-embeddings-provider.md`.
— Linked the runbook from `docs/contribute/index.md`.
— Ran `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=$PWD/.tmp/vdb-doc-sandbox uv run pytest --no-cov tests/modules/vdb/test_providers.py` and `UV_CACHE_DIR=.tmp/uv-cache RAGGD_WORKSPACE=$PWD/.tmp/vdb-doc-sandbox uv run ruff check`.
— Marked the provider documentation checklist item complete in this implementation plan.

### 2025-10-11 08:15 UTC
**Summary**
Provider protocol seam locked in
**Changes**
— Added provider interfaces, capability metadata, and registry scaffolding in `src/raggd/modules/vdb/providers/__init__.py`.
— Exported provider abstractions via `src/raggd/modules/vdb/__init__.py` for downstream wiring.
— Added provider registry unit tests in `tests/modules/vdb/test_providers.py` covering validation, factory wiring, and snapshot immutability.
— Marked provider protocol checklist item complete in this implementation plan.
### 2025-10-10 09:56 UTC
**Summary**
Provider design consolidated and checklist expanded
**Changes**
— Broke provider checklist into seam-first subitems and marked provider step complete.
— Expanded solution plan with provider protocol specifics, OpenAI behavior, and concurrency heuristics aligned with guide docs.
— Updated test expectations to cover provider capability contracts and error translation.
### 2025-10-10 17:43 PST
**Summary**
Typed VDB models and tests landed
**Changes**
— Added `src/raggd/modules/vdb/models.py` with embedding model, VDB record, and info summary dataclasses plus validation helpers.
— Registered exports in `src/raggd/modules/vdb/__init__.py` for downstream wiring.
— Created `tests/modules/vdb/test_models.py` exercising validation, serialization, and health normalization with `uv run pytest --override-ini "addopts=--cov=raggd.modules.vdb --cov-report=term-missing --cov-report=xml --cov-fail-under=100 -q" tests/modules/vdb/test_models.py`.
### 2025-10-10 19:05 UTC
**Summary**
CLI scaffold completed
**Changes**
— Expanded `src/raggd/cli/vdb.py` to include `info`, `create`, `sync`, and `reset` commands with shared context and argument parsing per spec.
— Added stub service protocol to enforce thin CLI and prepare for service wiring; unimplemented paths log consistent placeholder messaging.
— CLI now enforces option guards (`--missing-only` vs `--recompute`) and standard logging patterns.
— Checklist item “CLI scaffold” marked complete.
### 2025-10-10 18:45 UTC
**Summary**
Initial CLI scaffold landed
**Changes**
— Added `src/raggd/cli/vdb.py` with Typer group and `info` placeholder.
— Registered sub-app in `src/raggd/cli/__init__.py` (`raggd vdb ...` now available).
— Followed CLI patterns from `db.py`/`source.py` (context, logging, workspace resolution).
— Next: flesh out `info --json` and wire service/models per plan.
### 2025-10-10 18:15 UTC
**Summary**
Architect feedback 02 approved and incorporated
**Changes**
— Clarified `create` idempotency and mismatch failure mode with remediation.
— Added health remediation guidance requirement in health outputs.
— Documented deterministic sidecar pathing and acceptance criteria mapping.
— Noted CLI help and docs synchronization requirement.
### 2025-10-10 00:00 UTC
**Summary**
Drafted implementation plan aligning with guides and schema
**Changes**
- Added implementation doc covering architecture, CLI, provider abstraction, FAISS adapter, tests, and operability
- Outlined deliverables, health integration, and open questions
