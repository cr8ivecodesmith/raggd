# Parser Module Runbook

This guide covers day-to-day operations for the parser module, including
monitoring hooks, alert thresholds, and remediation steps for concurrency
regressions.

## Monitoring
- Run `raggd checkhealth parser` to refresh `.health.json` and surface the
  latest parser health reports. The hook records one entry per workspace source.
- Inspect structured logs for `parser-handler-*`, `parser-stage-lock-wait`, and
  `parser-session-*` events. These emit queue depth and lock metrics for
  downstream dashboards.
- Metrics and outcomes for the most recent run live in the source manifest under
  `modules.parser`. The `metrics` payload mirrors `ParserRunMetrics` fields.

## Telemetry
- `lock_wait_seconds`: cumulative seconds spent waiting on the database lock
  during staging.
- `lock_contention_events`: number of times lock waits exceeded the configured
  threshold.
- `queue_depth`: number of files in the staged plan (useful when correlating
  contention with workload size).
- Handler counters (`handlers_invoked`, `handler_runtime_seconds`) highlight hot
  paths when diagnosing slow runs.

## Alerts
Health reports promote parser status when concurrency metrics exceed
configurable thresholds:

| Severity | Condition | Threshold keys |
|----------|-----------|----------------|
| Warning  | `lock_wait_seconds` ≥ `modules.parser.lock_wait_warning_seconds` | `lock_wait_warning_seconds` |
| Warning  | `lock_contention_events` ≥ `modules.parser.lock_contention_warning` | `lock_contention_warning` |
| Error    | `lock_wait_seconds` ≥ `modules.parser.lock_wait_error_seconds` | `lock_wait_error_seconds` |
| Error    | `lock_contention_events` ≥ `modules.parser.lock_contention_error` | `lock_contention_error` |

Defaults warn at 5 seconds / 3 events and error at 30 seconds / 10 events. Tune
these values in `raggd.toml` when the workload justifies higher throughput.

## Remediation
1. Confirm no other long-running CLI sessions are parsing the same source.
2. Review log entries for `parser-stage-lock-wait` and adjacent structured
   context to identify slow handlers or outsized queues.
3. Lower `modules.parser.max_concurrency`, stagger source runs, or split the
   workload. Record follow-up actions in the manifest notes for shared visibility.
4. After remediation, rerun `raggd parser parse <source>` and `raggd checkhealth
   parser` to verify the health status returns to `ok`.
