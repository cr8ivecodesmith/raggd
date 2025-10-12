# Vector DB CLI

Use the `raggd vdb` command group to provision, populate, inspect, and reset
vector databases for each workspace source. A VDB binds a parser batch to an
embedding model and manages FAISS artifacts plus SQLite metadata so downstream
retrieval flows always have consistent embeddings to query.

The walkthrough below follows the typical lifecycle on a single source. Each
section highlights the command, key flags, and a trimmed example of the output
you should see when the module is healthy.

## Prerequisites
- Parse at least one batch for the source with `raggd parser parse <source>`.
- Configure credentials for your embeddings provider (for OpenAI set
  `OPENAI_API_KEY`). The CLI fails fast when the API key is missing, even when
  you plan a run with `--dry-run`.
- Confirm the workspace contains the optional `rag` extra dependencies so FAISS
  and provider SDKs are available (install with `uv sync --extra rag`).

## Create a VDB
`create` binds a parser batch and embedding model to a new VDB entry. It never
builds vectors or touches the filesystem, so you can re-run it safely.

```sh
$ raggd vdb create docs@latest base --model openai:text-embedding-3-small
✔ Created VDB base for docs batch 42 using openai:text-embedding-3-small (dim 1536)
```

### Things to know
- `@latest` resolves to the newest parser batch. Provide an explicit batch ID to
  lock onto historical data.
- Re-running `create` with the same selector is idempotent. If a VDB name
  already exists with a different batch or model, the CLI exits with guidance on
  using `raggd vdb reset --drop`.
- The command records the model’s embedding dimension in SQLite. Unknown dims
  are resolved during the next `sync`.

## Sync vectors into the index
`sync` materializes chunks, requests embeddings from the provider, writes the
FAISS index, and updates the `vectors` table. Use it after every parser run or
when rotating embedding models.

```sh
$ raggd vdb sync docs --vdb base --missing-only
⟳ Loading VDB docs:base (batch 42, openai:text-embedding-3-small)
⟳ Materialized 1,200 chunks (skipped 0 existing vectors)
⟳ Embedded 1,200 chunks in 24 batches (concurrency auto→4)
✔ Indexed 1,200 vectors at /workspace/sources/docs/vectors/base/index.faiss
```

### Flag highlights
- `--missing-only` only embeds new chunks. Use `--recompute` to rebuild from
  scratch with an atomic swap.
- `--limit` constrains how many chunks sync in one run. Helpful during incident
  response when you want to stage large updates.
- `--dry-run` walks the plan and reports counts without writing. Export
  `OPENAI_API_KEY` first—the provider still initializes during planning and
  fails fast without credentials. Logs include provider throttle warnings if
  rate limits are hit.

## Inspect VDB status
`info` reports selector details, counts, index paths, and health indicators. The
default view lists each matching VDB; `--json` emits the machine-readable
schema.

```sh
$ raggd vdb info docs --vdb base
docs:base • batch 42 • openai:text-embedding-3-small (dim 1536)
  chunks: 1,200  vectors: 1,200  index: 1,200
  faiss: /workspace/sources/docs/vectors/base/index.faiss
  built: 2025-10-10T17:20:35Z  stale vs latest: no
  health: ok – index, vectors, and chunks aligned
```

```sh
$ raggd vdb info docs --vdb base --json
[
  {
    "selector": "docs:base",
    "batch_id": 42,
    "embedding_model": {
      "provider": "openai",
      "name": "text-embedding-3-small",
      "dim": 1536
    },
    "counts": { "chunks": 1200, "vectors": 1200, "index": 1200 },
    "faiss_path": "/workspace/sources/docs/vectors/base/index.faiss",
    "built_at": "2025-10-10T17:20:35Z",
    "stale_relative_to_latest": false,
    "health": [
      { "code": "ok", "level": "info", "message": "healthy" }
    ]
  }
]
```

### Troubleshooting signals
- A missing index or dimension drift surfaces as a `warn` or `error` health
  entry. Follow the remediation hints (`sync --recompute`, `reset --drop`, etc.).
- If `stale_relative_to_latest` is true, re-run `parser parse` and `vdb sync` to
  align with the most recent batch.

## Reset a VDB
`reset` deletes FAISS artifacts and clears related rows from `vectors` and
`chunks`. Use it before re-creating a VDB with a new batch or when recovering
from corruption.

```sh
$ raggd vdb reset docs --vdb base --force
⚠ Purging vectors and FAISS artifacts for docs:base
✔ Cleared vectors (1,200), chunks (1,200), and removed index files
```

### Optional cleanup
- Add `--drop` to remove the `vdbs` row after cleanup. You can then run
  `create` again with a different batch or model.
- Without `--force`, the CLI prompts for confirmation to prevent accidental
  purges.
- After a reset you must run `vdb create` (if dropped) and `vdb sync` before the
  index becomes usable again.

## Next steps
- Automate `parser parse`, `vdb sync`, and `vdb info --json` inside CI to catch
  stale batches or dim mismatches early.
- Review `raggd checkhealth` regularly; VDB health hooks surface missing indexes
  or orphaned vectors alongside remediation advice.

## Troubleshooting & Recovery
- `health: warn missing-index` or `health: error sidecar-missing` — the FAISS
  files are gone or incomplete. Run `raggd vdb sync <source> --vdb <name> --recompute`
  to rebuild the index. If the vectors table also drifted, follow up with
  `raggd checkhealth vdb` to confirm the warning clears.
- `health: error dim-mismatch` — the stored vector dimension differs from the
  provider or sidecar metadata. Use `raggd vdb reset <source> --vdb <name> --drop`
  to clear the conflicting artifacts, re-run `raggd vdb create`, then
  `raggd vdb sync` to reseed vectors.
- `health: warn vector-count-drift` or `health: warn orphaned-vectors` — SQLite
  counts no longer match the FAISS index. Run `raggd vdb sync <source> --vdb <name>`
  to restore missing vectors; if drift persists, execute a full
  `--recompute` or `reset` cycle.
- `stale_relative_to_latest: true` — the parser produced a newer batch. Execute
  `raggd parser parse <source>` (or confirm the latest batch you want), then run
  `raggd vdb sync` so embeddings align with current content.
- Provider errors (rate limits or timeouts) — `sync` already retries with backoff.
  If failures persist, lower `--concurrency`, ensure credentials (e.g.,
  `OPENAI_API_KEY`) are set, and retry once the provider recovers.
- After any reset or recompute, validate the workspace with
  `raggd vdb info --json` and `raggd checkhealth vdb` to ensure remediation
  cleared the warnings before resuming downstream workflows.
