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
- `vdb create` — validates batch/model, derives `faiss_path`, inserts `vdbs` row; no vectors created.
- `vdb sync` — materializes `chunks`, generates embeddings, writes FAISS IDMap keyed by `chunk_id`, and records `vectors` rows.
- `vdb info` — reports selector, batch/model/dim, counts, `faiss_path`, last sync, and health notes; `--json` for machine output.
- `vdb reset` — deletes FAISS artifacts and clears `vectors`/`chunks`; optional `--drop` removes `vdbs` row.
- Health integration — contributes checks for missing index, dim mismatches, counts drift, stale relative to latest batch, orphaned refs.
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
- DI & boundaries
  - Inject config, logger, DB connection factory, provider registry, and `FaissIndex` adapter into `VdbService`.
  - Keep SQL inside repository helpers and keep FAISS isolated behind `faiss_index.py` for testability.
- Stepwise checklist
  - [ ] CLI: scaffold `raggd vdb` with `info/create/sync/reset` commands and shared context.
  - [ ] Models: add typed views for `EmbeddingModel`, `Vdb`, and info summaries.
  - [ ] Provider: implement OpenAI provider and registry; add `--concurrency auto` heuristic.
  - [ ] FAISS: implement IDMap wrapper, persistence, locks, and sidecar metadata.
  - [ ] Service: implement `create` (validate, derive path, insert), `sync` (materialize chunks, embed, persist), `info` (stats + health), `reset` (purge artifacts and rows).
  - [ ] Health: wire `vdb` checks into `checkhealth`.
  - [ ] Docs: update CLI help and add user guide.

## Test Plan
- Unit tests
  - Provider batching, retries, and dim reporting using a stub provider.
  - FAISS adapter: add/update vectors, atomic rebuild, metadata read/write, and locking behavior (use temp dirs in `.tmp/`).
  - Service logic: idempotency for `create`, `sync --missing-only`, and conflict detection.
- Contract tests
  - Provider protocol: ensure any provider conforms to interface and error contracts.
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
  - Sidecar `<faiss_path>.meta.json` stores `vdb_id`, `dim`, `metric`, `built_at`, and optional provider/model identifiers.

## Concurrency & Atomicity
- Single-writer guard via file lock on the vectors directory; retries with backoff if lock is held.
- `--recompute` writes to `index.faiss.tmp` and swaps on success; only then update `vectors`/sidecar.
- `--missing-only` honors existing `vectors` rows and the FAISS index size; cross-checks for drift and repairs as needed.

## Error Handling
- Raise typed exceptions for CLI-friendly messages: `VdbCreateError`, `VdbSyncError`, `VdbResetError`, `VdbInfoError`.
- Include remediation tips on common failures (e.g., mismatched dims → suggest `reset --recompute`).

## Deliverables & Files
- Core module and CLI files listed under Impact Analysis; tests under `tests/cli/test_vdb.py` and `tests/modules/vdb/*`.
- Documentation page `docs/learn/vdb.md` outlining usage and troubleshooting.

## Open Questions & Decisions
- Metric default: cosine similarity with vector normalization in MVP; expose metric enum in sidecar for future choices.
- Multiple VDBs per batch/model per source: allowed by unique VDB `name`; UX guidance recommends meaningful names.
- Provider selection overrides: CLI flag `--model` takes precedence over config defaults.

## History
### 2025-10-10 00:00 UTC
**Summary**
Drafted implementation plan aligning with guides and schema
**Changes**
- Added implementation doc covering architecture, CLI, provider abstraction, FAISS adapter, tests, and operability
- Outlined deliverables, health integration, and open questions

