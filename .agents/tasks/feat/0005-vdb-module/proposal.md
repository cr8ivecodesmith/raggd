# Feature Concept — Vector DB (VDB)

Builds on the existing `parser → db` flow. Parsed slice parts are already
persisted and recomposable; the VDB module will materialize model-specific
chunks, generate embeddings, and maintain external vector indexes for RAG.

The schema for `embedding_models`, `vdbs`, `chunks`, and `vectors` already
exists in migrations; this concept aligns the CLI and configuration to that
design and to parser UX that references `vdb sync` and `vdb reset`.

**CLI Usage**

```
raggd vdb <command>
```

**Commands (MVP)**

- `info [<source>] [--vdb <name>] [--json]`
  - Report VDBs for a source: model, dim, batch id, counts (chunks/vectors),
    index path, and last sync time. With no source, list all configured VDBs.
  - Mirrors `raggd db info` output style and JSON switch.
- `create <source>@<batch> <name> --model <provider:name|id>`
  - Register a VDB bound to a required parser batch and embedding model.
  - `batch` is required to preserve versioned state. Accepts literal batch IDs
    or the convenience alias `latest` (resolved at runtime).
  - The external index path is derived deterministically (see Storage Layout).
    Does not build embeddings.
- `sync <source> [--vdb <name>] [--recompute|--missing-only] [--limit <n>] [--concurrency <n>] [--dry-run]`
  - Materialize chunks for the target VDB(s) and generate embeddings using the
    configured provider. Writes vectors to the external index and records
    metadata in `vectors`.
  - Parser already surfaces a reminder: “run `raggd vdb sync <source>`”.
- `reset <source> [--vdb <name>] [--drop] [--force]`
  - Remove vector artifacts for a VDB (delete external index, clear `chunks`
    and `vectors` rows). With `--drop`, also delete the `vdbs` row.
  - Enables parser batch removal when it is currently blocked by VDB refs.

Notes
- Future extensions (management-only): `models` (list/add/remove embedding
  models), `export`/`import` (index portability), and `rebind` (change VDB to a
  different batch) if needed for advanced workflows.
  Retrieval/search is explicitly owned by a separate `query` module.

**Out of Scope (MVP)**

- Query/retrieval, hybrid search, reranking, and prompt orchestration.
- Local/offline embedding providers (planned as phase 2).
- Cross-VDB ensemble operations and federated search.
- Rebinding a VDB to a different batch (perform `reset` + `create`).
- Distributed/multi-tenant index backends; FAISS file indexes only.
- Automatic sync-on-parse; sync remains an explicit user action.

**Storage Layout**

- Index artifacts live under the source directory; the path is derived as:
  - `<workspace>/sources/<source>/vectors/<vdb_name>/index.faiss`
  - The computed path is stored in `vdbs.faiss_path` and used by both `sync`
    (writer) and `query` (reader). No CLI override in MVP to keep UX simple.
  - Sidecar metadata file: `<faiss_path>.meta.json` recorded at build time.
  - Directories are created lazily during `sync` if missing.
- SQLite remains the source of truth for metadata; external index contains the
  raw vector data only.

**Configuration**

- Embedding provider abstraction with a registry (OpenAI-first):
  - `openai` (primary for MVP): API key, model, batching, rate limits, retries.
  - `local-embeddings` (phase 2): ONNX/SentenceTransformers when offline or for
    cost control; must match model dims declared in `embedding_models`.
- VDB settings (per-source defaults, overridable per VDB):
  - `index_type` (e.g., `faiss:ivf_flat`, `faiss:hnsw`), `metric` (`cosine`/`l2`).
  - `batch_size`, `concurrency`, and `normalize` (unit-length vectors).
  - Optional chunking overrides for this VDB (token cap, headers-on/off).
  - Index path derivation is fixed in MVP (default subdir; no CLI override).
- Configuration lives alongside existing module toggles in `raggd.toml`.

Sample config sketch (OpenAI-first)

```
[modules.vdb]
enabled = true
extras = ["vdb"]
default_provider = "openai"
default_index_type = "faiss:hnsw"
metric = "cosine"
batch_size = 128
concurrency = "auto"
normalize = true
max_input_tokens = 8192   # truncate before embedding if exceeded

[modules.vdb.providers.openai]
enabled = true
model = "text-embedding-3-small"  # fast, 1536-dim
timeout_seconds = 30
max_batch = 128
max_retries = 5
initial_backoff = 0.5
max_backoff = 8.0
api_key_env = "OPENAI_API_KEY"

# Phase 2, optional local provider
[modules.vdb.providers.local]
enabled = false
model = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim
onnx = true
device = "cpu"
```

Packaging & Extras
- Create a dedicated `vdb` extra in `pyproject.toml` for management-only vector
  operations (index build/sync/reset). Suggested dependencies:
  - `faiss-cpu`, `openai`, `tiktoken`
- Keep `local-embeddings` extra for phase 2 (`onnxruntime`, `sentence-transformers`).
- Plan a separate `query` extra for the future query module (read-only usage):
  - `faiss-cpu`, `rapidfuzz`, `tiktoken` (plus any reranker deps later)
- Rationale: explicit ownership per module makes installs predictable and docs
  clearer; duplication across extras is acceptable to convey intent.
- Consider re-working `rag` as a top-level extra that pulls in both `vdb` and
  `query` for end-to-end RAG users.

**Data Model Expectations**

- `embedding_models(provider, name, dim)` declares available models.
- `vdbs(name, batch_id, embedding_model_id, faiss_path, created_at)` binds a
  human-friendly name to a specific parser batch and model.
- `chunks(symbol_id, vdb_id, header_md, body_text, token_count)` stores
  recomposed text for embedding, one row per symbol per VDB.
- `vectors(chunk_id, vdb_id, dim)` records presence/shape of an external
  vector; the external index stores the actual float data.

VDB–Batch Association
- A VDB is bound to exactly one source and one parser `batch_id`.
- Rationale: a batch represents a precise source state (git tag/sha). Queries
  can then target an exact revision by selecting a VDB for that batch.
- Consequence: if you parse a new batch and want vectors for it, create a new
  VDB (e.g., `create <source>@latest <name>` + `sync`) rather than reusing/rebinding.

OpenAI model presets (guidance)
- `openai:text-embedding-3-small` → dim 1536 (cost-efficient, fast; default)
- `openai:text-embedding-3-large` → dim 3072 (higher quality, higher cost)
Register chosen model in `embedding_models` so `vdbs.embedding_model_id`
resolves correctly during `create`.

**Health & Manifest**

- `raggd checkhealth vdb` surfaces:
  - Missing index file at `faiss_path`.
  - Dimension mismatches between model and index/vectors.
  - Count drift: `chunks` vs `vectors` vs external index size.
  - Staleness: VDB batch older than latest parser batch for the source.
- Orphans: `vdbs` referencing nonexistent batches/models.
- Source manifest mirrors per-VDB status: name, model, batch, counts, last
  sync timestamp, and notes. Parser already appends a “vector sync required”
  note after parse; VDB should clear/update it on successful sync.

OpenAI-specific health and ops
- Backoff on HTTP 429/5xx with jitter; respect rate-limit headers.
- Per-run token/cost accounting surfaced in JSON output and `.health.json`.
- Detect input-length violations; apply truncation policy and log notes.
- Ensure dimension consistency between selected model and `vectors.dim`.

Security
- Read OpenAI key from `OPENAI_API_KEY` by default, with optional override in
  `raggd.toml` only when users explicitly opt-in. Never log full keys. Mask
  sensitive headers in debug logs.

**Answers to the Open Questions**

- Is “ingest” the right term? Prefer `sync` to align with parser guidance and
  to convey idempotent refresh behavior; avoid ambiguity with parser “ingest”.
- What other commands? MVP covers `info`, `create`, `sync`, and `reset`. Next:
  `export`/`import`, and optional `rebind`.
- What configuration to expose? Provider selection and model, vector index
  params (type/metric), throughput knobs (batch, concurrency), and path derivation
  (documented and fixed for MVP).
- Is this where we need an AI service? Yes—use OpenAI now for speed-to-value
  (better throughput, less setup). Keep the provider interface so that
  `local-embeddings` can be added later without changing UX.
- Should the AI client be standalone? Yes—create a shared `EmbeddingProvider`
  interface used by VDB now and RAG later, with a provider registry.
- Sensible checkhealth/manifest? See Health & Manifest above—focus on existence,
  shape, freshness, and consistency checks, and mirror concise status to the
  source manifest.
- Other considerations? Concurrency-safe writes to the index, atomic replace on
  rebuild, resumable sync (`--missing-only`), and CLI dry-runs for safety.

**Separation of Concerns**

- VDB module responsibilities (management):
  - Define and persist VDBs (`vdbs` rows) bound to batches and models.
  - Materialize chunks and generate embeddings; build and maintain indexes.
  - Provide status/health and reset operations; export/import formats.
- Query module responsibilities (usage):
  - Perform retrieval (KNN/semantic filtering) against existing indexes.
  - Apply rerankers or hybrid search; compose prompts for downstream RAG.
  - Never mutates VDB state; read-only access to metadata and index files.

**Consumer Contracts (for Query Module)**

- Catalog: `vdbs` exposes `name`, `embedding_model_id`, `batch_id`, `faiss_path`.
- Models: `embedding_models` provides `provider`, `name`, `dim` and must match
  the stored index dimension and metric normalization.
- Index format: FAISS ID-mapped index using `chunks.id` as the external ID.
  - Guarantees search results return `chunk_id` labels for lookup.
  - Metric must match `metric` config; vectors are normalized when `metric`
    is cosine to ensure dot-product equivalence.
- Sidecar metadata file: `<faiss_path>.meta.json` with fields: `version`,
  `vdb_name`, `vdb_id`, `batch_id`, `model`, `dim`, `metric`, `normalized`, `built_at`.
  - Read-only consumers use this to verify compatibility before searching.
- Mapping: `vectors(chunk_id, vdb_id, dim)` asserts presence/shape; FAISS IDMap
  stores the authoritative mapping, so query can use returned IDs directly.

Identifiers and Selectors
- Canonical selector for consumers: `<source>:<vdb-name>`
  - `vdb-name` is unique per source (per-source DB) and implicitly binds the
    batch via the VDB record.
- The VDB module will surface this selector in `info` output so the `query`
  module (and users) can reference the correct state without inspecting tables.
  For display, include the bound `batch_id` to show revision context.

**Usage Examples (OpenAI-first)**

```
# Create a VDB bound to the latest parser batch using OpenAI small model
raggd vdb create demo@06ABCD1234 code-small --model openai:text-embedding-3-small

# Build embeddings for that VDB (missing only, auto-batch + concurrency)
raggd vdb sync demo --vdb code-small --missing-only

# Inspect status as JSON (includes selector like `demo:code-small`)
raggd vdb info demo --vdb code-small --json

# Reset the VDB and drop its index (to unblock a parser batch removal)
raggd vdb reset demo --vdb code-small --drop --force
```

**Action Items**

- Finalize CLI flags and defaults (`sync`, `reset`, storage paths).
- Add `spec.md` detailing success criteria, error states, and health signals.
- Add `implementation.md` covering provider interface and index adapters.
- Define config keys in `raggd.defaults.toml`; wire toggles to the registry.
- Add tests: CLI happy-path, stale index detection, reset blocking removal.
