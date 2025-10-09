# Parser CLI

Use the `raggd parser` command group to tokenize workspace sources and persist
chunk slices ready for downstream embedding workflows. The group provides a
single entry point for parsing, inspecting batch history, and pruning stale
artifacts while surfacing health metrics alongside manifest metadata.

## Command summary
- `raggd parser parse <source ...>` walks each enabled source target (respecting
  `.gitignore` rules) and persists new or updated chunks. Provide optional paths
  to limit traversal to specific files or directories. The command records
  handler warnings and fallbacks in the source manifest and returns a non-zero
  exit code when any file fails to parse.
- `raggd parser info <source>` reports the latest batch identifier, handler
  coverage, fallback notes, and configuration deltas versus packaged defaults.
  Use this command when validating handler upgrades or confirming recomposition
  metadata.
- `raggd parser batches <source> [--limit N]` lists recent parser batches with
  file, chunk, and reuse counts plus health flags. Attach `--limit` to focus on
  the most recent batches during investigations.
- `raggd parser remove <source> <batch> [--force]` removes a batch after
  verifying no other modules reference it. The command warns about follow-up
  vector index cleanup so the workspace stays consistent.

## Configuration
Parser toggles live under `[modules.parser]` in `raggd.toml` with packaged
defaults shipped in `raggd.defaults.toml`. Update these fields when tuning
token limits or concurrency:

- `general_max_tokens` provides the default token cap for handlers; override a
  specific handler under `[modules.parser.handlers.<name>]`.
- `max_concurrency` governs how many sources parse in parallel. The default
  `"auto"` defers to runtime heuristics documented in the engineering guide.
- `fail_fast` switches between resilient runs (default) and exiting on the first
  handler failure.
- `gitignore_behavior` controls whether workspace ignore rules, repository
  `.gitignore`, or both feed traversal.
- `lock_wait_*` and `lock_contention_*` define the database lock thresholds that
  trigger parser health alerts. Monitor these metrics via `raggd checkhealth`
  before widening concurrency.
- Handler tables inherit the enabling structure described in the
  [workspace configuration guide](workspace.md). Disable a handler to force the
  registry to fall back to text while keeping overrides close to the default.

## Handler selection and fallbacks
Handlers are resolved through the parser registry using file extensions and
shebang hints. Language-specific handlers (Markdown, Python, JavaScript/TypeScript,
HTML, CSS) run whenever their dependencies are installed and the handler stays
enabled in configuration. When a specialized handler is disabled, unhealthy, or
missing dependencies, the registry falls back to the text handler so parsing can
continue. Each fallback increments `modules.parser.metrics.fallbacks`, logs a
`parser-handler-fallback` event with the reason, and appends a note to the
manifest.

Inspect fallbacks with `raggd parser info <source>` or by reviewing structured
logs emitted during the parse. Persistent fallbacks usually point to missing
extras (install via `uv pip install .[parser]`, for example) or misconfigured
handler toggles in `raggd.toml`.

## Recomposition guarantees
Chunks are stored as ordered slice parts with deterministic `part_index` and
`part_total` fields. Overflow slices include metadata describing whether tokens
were truncated and why, ensuring downstream services can rebuild the original
text. The recomposition helpers in `raggd.modules.parser.recomposition` return
`RecomposedChunk` objects that stitch together slice parts, preserve delegate
relationships, and expose combined text with byte and line spans. Use the helper
when debugging downstream consumers or building integrations that need the full
chunk tree.

## Health signals
Parser runs publish metrics under `modules.parser` in each source manifest. Key
fields include `files_parsed`, `files_reused`, `chunks_emitted`, and database
lock telemetry (`lock_wait_seconds`, `lock_contention_events`). Fallback counts
and handler runtime totals surface alongside the metrics so you can gauge when a
specialized handler stopped running.

Run `raggd checkhealth parser` (or `raggd checkhealth` for all modules) to
refresh `.health.json`. The parser health hook validates manifest metadata,
ensures concurrency thresholds are respected, and reports remediation guidance
if fallbacks or handler errors persist across runs.

## FAQ
- **What happens when a handler fails mid-run?** The parser logs the error,
  records it in the manifest, continues parsing other files, and exits with a
  non-zero status unless you add `--fail-fast`. Failed files remain eligible for
  retry on the next run.
- **How can I tell which files used a fallback handler?** Review the parse logs
  for `parser-handler-fallback` entries or inspect
  `modules.parser.last_run_notes` via `raggd parser info <source>`. Each note
  records the handler name and fallback reason so you can install missing
  dependencies or re-enable the handler.
- **Can I audit the stored chunks for a single file?** Start with
  `raggd parser batches <source> --limit 1` to confirm the latest batch details,
  then load the `ChunkRecomposer` helper from `raggd.modules.parser` inside a
  Python REPL. The helper reassembles slice parts in order and exposes
  parent-child links when chunks were delegated during token overflow handling.
