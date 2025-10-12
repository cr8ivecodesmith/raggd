# OpenAI Embeddings Provider

This runbook explains the contract implemented by
`raggd.modules.vdb.providers.openai.OpenAIEmbeddingsProvider`. Follow these
rules whenever you change the provider so the CLI, tests, and health checks stay
aligned.

## Quick reference
- Depends on the OpenAI Python SDK (>=1.0) and `tiktoken`.
- Requires `OPENAI_API_KEY` for all CLI entry points, including `--dry-run`
  planning flows; optional overrides:
  - `OPENAI_BASE_URL` (e.g., Azure or private proxy endpoints).
  - `OPENAI_TIMEOUT_SECONDS` (float, defaults to 30s).
  - `OPENAI_ORG_ID` for multi-tenant accounts.
- Provider key is registered as `"openai"`; models are addressed by the
  provider-local name (e.g., `text-embedding-3-small`).
- Capabilities and dimensions come from a static model table kept in
  `openai.py`. Update the table when OpenAI publishes new models or limits.
- The provider is synchronous; concurrency is orchestrated by `VdbService` via
  worker pools using the caps the provider reports.

## Batching semantics
- `embed_texts` accepts any `Sequence[str]` and chunks it into batches before
  calling the OpenAI API.
- The provider enforces two ceilings for each request:
  1. `options.max_batch_size` (from CLI/config) clamped to the provider caps.
  2. Total token count per request (estimated via `tiktoken`) must stay under
     `caps.max_request_tokens`.
- Text normalization trims whitespace and collapses Windows newlines to `\n`
  before batching so token estimates are stable across platforms.
- The batching loop greedily adds inputs until either ceiling would be exceeded.
  If a single chunk is larger than `caps.max_request_tokens`, the provider raises
  `VdbProviderInputTooLargeError` with remediation advice; `VdbService` surfaces
  that message to the operator.
- When `--concurrency auto` is selected, the CLI asks the provider for caps and
  resolves: `min(os.cpu_count() or 1, caps.max_parallel_requests,
  config.modules.vdb.max_concurrency or 8)`. The resolved concurrency is logged
  once per sync run.

## Retry and backoff
- Requests are retried on these exceptions:
  - `openai.RateLimitError`
  - `openai.APITimeoutError`
  - `openai.APIConnectionError`
  - `openai.APIStatusError` with HTTP status >= 500
  - Transport-level `httpx` exceptions raised by the SDK
- Retries use truncated exponential backoff: base delay 0.5 seconds, multiplier
  2, capped at 8 seconds. Jitter is applied by sampling ±20% of the delay and
  rounding to the nearest 10ms.
- The provider attempts up to 5 tries (initial attempt + 4 retries). Once the
  retries are exhausted, a `VdbProviderRetryExceededError` is raised with the
  collected error context (status, request id, retry count).
- Retries carry a structured log entry that includes `provider`, `model`,
  `attempt`, `max_attempts`, and the root exception class.

## Dimension resolution
- `describe_model` consults the static model table. Known models return a
  populated `EmbeddingProviderModel` with `dim` set.
- If the model is absent from the table or the dimension is `None`, the provider
  performs a one-off `embed_texts` call with a sentinel string to learn the
  dimension from the response payload. The sentinel request respects the same
  retry/backoff rules and is cached for the lifetime of the provider instance.
- The first successful sync writes the resolved dimension into
  `embedding_models.dim` via `VdbService`. Subsequent runs validate the stored
  dimension against provider responses and raise `VdbProviderDimMismatchError`
  when they differ.

## Token estimation
- Token lengths are estimated with `tiktoken.encoding_for_model(model)`. When
  the OpenAI SDK does not recognize the model, the provider falls back to
  `tiktoken.get_encoding("cl100k_base")`.
- Estimates include a +8 token safety pad to cover request metadata the API may
  introduce. This pad is accounted for when enforcing
  `caps.max_request_tokens`.
- Token counts are memoized per unique text length during a sync run to reduce
  repeated encoder calls. Memoization is cleared between runs to control memory
  pressure.
- If the encoder raises (e.g., no vocabulary available), the provider logs the
  failure at warning level and assumes the worst case (`len(text) * 4`) before
  falling back to chunk-level truncation.

## Error translation
- The provider never surfaces raw OpenAI exceptions. Everything is mapped to the
  `raggd.modules.vdb.errors` hierarchy:
  - 429 / rate-limit -> `VdbProviderRateLimitError`
  - 408 / gateway timeouts / connection resets -> `VdbProviderRetryableError`
  - 5xx -> `VdbProviderRetryableError`
  - 4xx other than 429 -> `VdbProviderRequestError`
  - Input too large -> `VdbProviderInputTooLargeError`
  - Exhausted retries -> `VdbProviderRetryExceededError`
- Error instances carry `provider`, `model`, `request_id`, `status_code`, and a
  short operator-facing message. Nested OpenAI exceptions are attached as
  `__cause__` for debugging.
- `VdbService` treats `VdbProviderRetryableError` as retryable at the chunk
  batching layer; other subclasses bubble up immediately.

## Observability
- Each API call logs `provider=openai`, `model`, `batch_size`,
  `token_count`, and latency in seconds.
- Retry attempts include `retry_delay` in the log event; successful retries emit
  a `recovered=true` marker.
- The provider exposes an internal `stats` mapping (used only in tests) that
  tracks `requests`, `retries`, and `failures` counters; reset after each sync.

## Manual verification checklist
1. Export `OPENAI_API_KEY` (or configure a test double) and set
   `RAGGD_WORKSPACE=$PWD/.tmp/vdb-openai-demo`.
2. Run `uv run raggd vdb sync demo --dry-run --model openai:text-embedding-3-small`
   to confirm batching logs and dimension resolution.
3. Use the failure injection flag `RAGGD_VDB_FAKE_RATE_LIMITS=1` (test helper) to
   verify retries surface operator guidance without exposing raw SDK errors.
4. Inspect `.tmp/vdb-openai-demo/logs` for structured entries documenting
   batching and retry behavior.
