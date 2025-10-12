# Vector DB (VDB) Module — Spec

## Summary
Introduce a first-class `raggd vdb` command group that manages per-source vector databases bound to parser batches and embedding models. The module materializes chunks from parsed artifacts, generates embeddings via a provider abstraction (OpenAI-first), and builds/maintains an external FAISS index. Metadata is persisted in the existing schema tables (`embedding_models`, `vdbs`, `chunks`, `vectors`), while the FAISS index stores only vector payloads. The module aligns UX with the parser’s guidance (“run `raggd vdb sync <source>`”) and exposes clear management operations without query/search concerns (owned by a future `query` module).

## Status
- Approved with stakeholder sign-off (stakeholder-spec-feedback-02) on 2025-10-10.
- Feedback reference: `.agents/tasks/feat/0005-vdb-module/feedback/stakeholder-spec-feedback-02.json`.

## Goals
- Deliver `raggd vdb <command>` with MVP subcommands: `info`, `create`, `sync`, `reset`.
- Bind each VDB to exactly one parser `batch_id` and one `embedding_model_id`; enforce dimension compatibility.
- Deterministically derive index path under the source: `<workspace>/sources/<source>/vectors/<vdb_name>/index.faiss`, plus sidecar `<faiss_path>.meta.json`.
- Implement a provider abstraction and registry (OpenAI-first) supporting batching, rate limits, retries, and token-length handling; keep the contract stable for future local/offline providers.
- Persist chunk materialization and vector presence via `chunks(symbol_id, vdb_id, header_md, body_text, token_count)` and `vectors(chunk_id, vdb_id, dim)`; use FAISS ID-mapped index keyed by `chunk_id`.
- Provide health/status visibility (via `info --json` and `checkhealth` integration) for index existence, counts drift, dimension mismatches, and staleness relative to latest parser batch.
- Keep SQLite authoritative for metadata; ensure idempotent operations for `create`, `sync --missing-only`, and `reset`.
- Expose a stable selector format `<source>:<vdb-name>` (shown in `info --json`) to be consumed by the future `query` module.

## Non-Goals
- Query/retrieval, hybrid search, reranking, prompt orchestration (owned by a future `query` module).
- Local/offline embedding providers (deferred to phase 2).
- Cross-VDB ensembles/federation, distributed/multi-tenant index backends.
- Rebinding a VDB to another batch (perform `reset` + `create`).
- Customizable index file paths (fixed derivation in MVP).

## Behavior (BDD-ish)
- Given a workspace with sources and a parsed batch, when the operator runs `raggd vdb create <source>@<batch> <name> --model <provider:name|id>`, then the CLI resolves the batch (accepting `latest` alias), resolves the embedding model, and creates/validates an `embedding_models` entry. If the model's embedding dimension is known (e.g., OpenAI), record it; otherwise, defer resolution to first `sync` call. Any conflicting dimension between a pre-existing model record, provider-reported dim, or an existing index causes a fail-fast with remediation guidance. The command computes `faiss_path`, inserts a `vdbs` row, and exits without creating vectors or index files.
- Given a VDB exists, when the operator runs `raggd vdb sync <source> [--vdb <name>] [--missing-only|--recompute] [--limit N] [--concurrency N|auto] [--dry-run]`, then the CLI materializes per-VDB chunks, batches text through the configured provider to obtain embeddings, normalizes vectors when the metric is cosine, writes to a FAISS IDMap index using `chunk_id` as the external ID, records presence/shape in `vectors`, and updates the sidecar metadata file. Index rebuilds under `--recompute` are atomic (write to a temp path then swap) and concurrency-safe (single-writer guard with file locks). `--missing-only` skips rows already present, `--recompute` overwrites vectors/refreshes the index, `--dry-run` reports planned actions without writing. `--concurrency auto` respects provider caps.
- Given a VDB exists, when the operator runs `raggd vdb info [<source>] [--vdb <name>] [--json]`, then the CLI lists matching VDBs with: selector `<source>:<vdb-name>`, embedding model and `dim`, bound `batch_id` (and note if it’s older than latest), counts (`chunks`/`vectors`/index size), `faiss_path`, `built_at`/last sync timestamp, and health notes; `--json` emits a machine-readable summary.
- Given a VDB exists, when the operator runs `raggd vdb reset <source> [--vdb <name>] [--drop] [--force]`, then the CLI deletes external index artifacts and clears `vectors` and `chunks` rows for that VDB; with `--drop`, it also removes the `vdbs` record. `--force` suppresses interactive confirmation.
- Given a workspace under `raggd checkhealth`, when the health aggregator runs, then the VDB module contributes checks for: missing `faiss_path`, `dim` mismatches, counts drift (`chunks` vs `vectors` vs index size), stale VDB relative to parser’s latest batch, and orphaned refs (VDBs pointing to missing batches/models).

## Constraints & Dependencies
- Tech constraints: FAISS file indexes (CPU) only for MVP; single-tenant, local files. Provider support is restricted to OpenAI embeddings initially.
- Upstream: parser must have produced a valid `batch_id` for the source; db module provides per-source SQLite and schema tables referenced.
- Downstream: future `query` module will consume the FAISS index and metadata; CLI UX and selector format `<source>:<vdb-name>` should remain stable.
- Performance: honor provider batch/concurrency caps; implement backoff with jitter on 429/5xx; apply `max_input_tokens` truncation prior to embedding.
- Index integrity: FAISS IDMap indexes must map exactly to `chunk_id`; any re-index must be atomic (write to a temp path and swap on success) and concurrency-safe (single-writer guard + file locks to prevent corruption).
- No sync-on-parse automation in MVP; operators trigger `sync` explicitly.
- No CLI override for index path in MVP; path is derived deterministically under the source.

## Configuration
- Defaults live in `raggd.defaults.toml` under `modules.vdb` (provider defaults, `index_type`, `metric`, batching/concurrency, `normalize`, `max_input_tokens`).
- Users override via `raggd.toml` in the workspace; environment variables (e.g., `OPENAI_API_KEY`) take precedence for secrets.
- `--concurrency auto` uses provider-aware limits; explicit numeric values are validated against provider caps.
- Index path derivation is fixed (no CLI overrides) to ensure determinism and portability in the MVP.

## Security & Privacy
- Read provider credentials from environment by default (e.g., `OPENAI_API_KEY`) with optional override in config. Never log raw keys; mask sensitive headers in debug logs.
- Do not persist raw provider responses; only embeddings and minimal metadata needed for operability.
- Respect provider content and usage policies; expose a clear way to disable the module if compliance requires.

## Telemetry & Operability
- CLI emits structured JSON on `--json`, including counts processed, batch/model identifiers, tokens used (estimated), and cost estimates where available.
- Sidecar metadata `<faiss_path>.meta.json` includes: `version`, `vdb_name`, `vdb_id`, `batch_id`, `model`, `dim`, `metric`, `normalized`, `built_at`.
- Health integration surfaces status in `raggd checkhealth vdb` and can mirror concise status into the source manifest (notes re: “vector sync required” cleared on successful sync).
- Logging: progress by batches with rate-limit/backoff notices; clear warnings for truncation events and dimension mismatches.

## Rollout / Revert
- Ship behind `modules.vdb` toggle in `raggd.defaults.toml` (enabled in dev once validated). Add a dedicated `vdb` optional dependency group in `pyproject.toml` for management-only usage (`faiss-cpu`, `openai`, `tiktoken`).
- Revert by disabling the feature toggle; existing `vdbs` records and index artifacts remain on disk (no automatic destructive cleanup).
- Future-proof by keeping the provider interface stable so a `local-embeddings` provider can be added without UX change.
- Consider adding a `query` extra later and an umbrella `rag` extra that aggregates `vdb` + `query` for convenience.

## Definition of Done
- [x] `raggd vdb` implements `info`, `create`, `sync`, `reset` with consistent flags and messaging.
- [x] Deterministic storage layout is implemented; sidecar metadata is written and read for compatibility checks.
- [x] Provider abstraction with OpenAI implementation: batching, concurrency, truncation, retries/backoff, and `dim` consistency enforcement.
- [x] Chunk materialization and FAISS IDMap writing wired to `vectors` presence tracking; supports `--missing-only`, `--recompute`, `--limit`, and `--dry-run`.
- [x] Health checks for index existence, counts drift, `dim` mismatches, and staleness wired into `checkhealth`; `info --json` reflects the same signals.
- [x] Config keys defined in `raggd.defaults.toml` under `modules.vdb` (provider defaults, index type/metric, batching/concurrency, normalize, max input tokens) with user overrides in `raggd.toml`, and passed through to services.
- [x] Tests: CLI happy paths, stale index detection, dimension mismatch, resumable sync, reset (including `--drop`), and atomic index rebuild behavior.
- [x] Docs: user-facing CLI docs, operator notes (OpenAI API key handling, cost/limits), and developer notes on provider/index adapters.
- [x] Packaging: `vdb` extra declared; module registered and toggle respected.

## Open Items
- Clarify explicit model registration vs. auto-create policy and error messaging for dimension mismatches. Owner: Architect. Due: 2025-10-17.
- Decide whether to persist a `.health.json` alongside sidecar metadata. Owner: Architect. Due: 2025-10-17.
- Confirm default index type (e.g., `faiss:hnsw`) and `--concurrency auto` policy per provider caps. Owner: Architect. Due: 2025-10-17.

## Next Check-in
- 2025-10-17

## Ownership
- Owner: @matt
- Reviewers: @codex
- Stakeholders: @docs, @ops

## Links
- Proposal: .agents/tasks/feat/0005-vdb-module/proposal.md
- Reference (db module spec): .agents/tasks/feat/0003-db-module/spec.md
- Stakeholder Feedback (sign-off): .agents/tasks/feat/0005-vdb-module/feedback/stakeholder-spec-feedback-02.json

## History
### 2025-10-10 16:00 PST
- Drafted VDB module spec from proposal, aligned with workflow templates and existing module conventions.
### 2025-10-10 17:00 UTC
- Stakeholder approval and sign-off recorded; spec aligned to clarifications on model registration/dimension enforcement, atomic rebuild + concurrency safety, defaults vs overrides, and fixed index path in MVP.
