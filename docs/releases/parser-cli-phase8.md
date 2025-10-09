# Parser Module CLI — Phase 8 Release Notes

Phase 8 brings the parser module to a ship-ready state with a stable CLI
surface, comprehensive telemetry, and refreshed guidance for operators.

## CLI maturity
- The `raggd parser` group now offers production-ready `parse`, `info`,
  `batches`, and `remove` flows backed by the handler registry and manifest
  lifecycle.
- Workspaces inherit deterministic traversal, handler fallbacks defaulting to
  text, and manifest updates that record warnings plus per-run notes.
- Parser configuration defaults ship with toggles for handler activation,
  concurrency, fail-fast behavior, and gitignore handling so teams can tune
  throughput without patching code.

## Telemetry and health
- Parser runs emit structured metrics for handler runtimes, queue depth, fallback
  counts, and database lock contention to aid capacity planning.
- Health hooks surface the telemetry through `raggd checkhealth`, highlighting
  lock wait thresholds and degraded handler signals alongside remediation
  guidance.
- Concurrency thresholds in `raggd.defaults.toml` and the workspace guide now
  document warning and error levels for lock wait seconds and contention counts.

## Documentation and runbooks
- The parser user guide outlines the CLI flows, handler selection rules, and
  recomposition guarantees for downstream integrations.
- The parser runbook explains how to monitor telemetry, investigate fallbacks,
  and respond to lock contention alerts.
- Workspace docs describe parser configuration precedence, covering how
  `modules.parser.*` settings interact with environment overrides and defaults.

## Upgrade checklist
- Install parser extras (`uv pip install .[parser]`) to enable specialized
  handlers before running the CLI in production.
- Review workspace `raggd.toml` overrides to confirm handler toggles, max token
  limits, and concurrency thresholds align with the packaged defaults.
- Capture a fresh `raggd parser parse` run followed by `raggd checkhealth` to
  verify telemetry and manifest health look correct prior to launch.
