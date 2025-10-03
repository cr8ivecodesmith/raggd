# Source Management Tooling — Spec

## Summary
Introduce CLI-managed "sources" representing workspace project or documentation contexts, enabling initialization, target management, refresh, rename, removal, listing, and enable/disable flows, with state stored in workspace config and guarded by health checks integrated into the system-wide `raggd checkhealth` tool.

## Goals
- Deliver a `raggd source` command family (init, target, refresh, rename, remove, list, enable, disable) that normalizes source names and enforces confirmations/force flags consistently with existing CLI UX, including optional target seeding during initialization.
- Persist source metadata (`enabled`, `name`, `path`, and optional `target`) in the workspace config as the source of truth while creating source directories and SQLite stubs under `<workspace>/sources/<name>`. Operational health remains outside the config, captured by manifests and the shared `.health.json` document.
- Block lifecycle operations when a source is disabled or fails command-triggered health checks (which also toggle the source off), surfacing actionable remediation guidance via CLI responses and manifest data.
- Hook the new source module into a `raggd checkhealth` aggregator that enumerates registered modules and reports status/errors.
- Establish logging, configuration toggles, dependency grouping, and documentation updates required to make the feature maintainable and discoverable.

## Non-Goals
- Implementing vector database refreshes beyond place-holder cleanup hooks (future integration only).
- Managing remote repositories, synchronization, or multi-workspace orchestration.
- Building GUI/web experiences on top of the CLI flows described here.
- Preserving legacy source management code paths or tests; superseded logic will be removed.

## Behavior (BDD-ish)
- Given a user runs `raggd source init <raw-name> [--target <dir>]`, when the CLI normalizes the name to lowercase kebab and creates `<workspace>/sources/<name>` with an empty `db.sql`, then it records the source in the workspace config with normalized name and path, failing with a helpful message if the normalized name already exists. The new source defaults to `enabled=false` when no target is configured. When `--target` is supplied and passes the same validation used by `raggd source target`, the CLI enables the source immediately, seeds the manifest/config with the target, performs the initial refresh (respecting confirmation semantics), stamps `last_refresh_at`, and logs that subsequent lifecycle operations remain health-gated. If the target is omitted or fails validation, the source stays disabled and the user must run `raggd source enable <name>` after fixing the issue.
- Given an existing source in config, when the user runs `raggd source target <name> <dir> [--force --clear]`, then the CLI requires `<dir>` unless the caller explicitly provides `--clear` (sources may share duplicate targets, so validation must not enforce uniqueness across other sources). The CLI rejects invocations that supply both `<dir>` and `--clear`. Supplying `--clear` removes the target reference, prompts for confirmation unless `--force` is provided, and leaves enablement governed by the standard health/disable rules. With `<dir>` present, the command validates the directory (accepting flexible input shapes but resolving to an absolute path via the core path utility and requiring the target to exist and be readable), warns/asks for confirmation before refreshing unless forced, updates config and the source manifest at `<workspace>/sources/<name>/manifest.json` (including the target metadata and `last_refresh_at`), triggers a refresh of cached artifacts, and logs the change.
- Given the user runs `raggd source refresh <name> [--force]`, then the command blocks if the source is disabled or unhealthy unless `--force` is supplied (with guidance to rerun using force for remediation). When permitted to proceed, it clears files in the source directory (except config and `manifest.json`), resets `db.sql`, stubs future vector DB cleanup, updates the manifest `last_refresh_at` to the execution timestamp, and prompts unless forced.
- Given the user runs `raggd source rename <old> <new> [--force]`, then the CLI normalizes `<new>`, checks for collisions, and refuses to proceed when the source is disabled or unhealthy unless `--force` is supplied for remediation scenarios. When allowed, it renames the directory, updates manifest/config entries, and reruns health checks before enabling operations.
- Given the user runs `raggd source remove <name> [--force]`, then the command confirms deletion (unless forced), removes directory artifacts, deletes config/manifest entries, and logs the operation. If the source is disabled or unhealthy, the CLI blocks the removal with guidance to rerun using `--force`.
- Given the user runs `raggd source list`, then the CLI loads `[workspace.sources]`, reporting each source name, enabled state, normalized path, and most recent health summary. The command exits `0` only when every reported status is `ok`; encountering `unknown`, `degraded`, or `error` produces a non-zero exit code so scripting can detect issues quickly.
- Given the user runs `raggd source enable <name> [<name> ...]` or `raggd source disable <name> [<name> ...]`, then the CLI updates the corresponding entries in `[workspace.sources]`, writes manifests, and confirms status changes with logging. The enable flow runs the health check to report current status without mutating other fields; if the post-enable check reports `degraded` or `error`, the CLI keeps the source enabled, surfaces warnings, and reminds the operator to remediate manually. Lifecycle commands that mutate source assets (`target`, `refresh`, `rename`, `remove`) disable the source automatically by toggling `enabled=false` when their health checks return non-`ok` statuses and the operator declines to force the action.
- Given the new `raggd checkhealth [module]` command runs, then it enumerates registered module hooks (including the source module) and reports per-source status with remediation guidance and logs, persisting the aggregate results to `<workspace>/.health.json`. Hooks follow a common, read-only interface—accepting a workspace handle and returning a collection of health records shaped as `{"name": str, "status": "ok"|"degraded"|"error"|"unknown", "summary": str, "actions": [str], "last_refresh_at": str|None}`. The aggregator merges the responses into the JSON document under keys matching the module name (e.g., `"sources"`) where each module contributes an object shaped like `{ "checked_at": <ISO8601 string>, "status": <enum>, "details": [...] }`. Modules must not mutate workspace state while running these hooks; they log findings, emit recommended actions, and leave any remediation (including toggling enablement) to follow-up CLI flows. The sources module's hook therefore only reports the latest per-source payloads (status, summary, actions, `last_refresh_at`) in `details`, ensuring `raggd checkhealth` itself never flips enablement.
- Given a source is disabled (`enabled=false`)—including cases where earlier `raggd source` commands auto-disabled it after a failed health check—or its current health evaluation returns non-`ok`, when any lifecycle command (other than explicit health inspection, `source list`, or `source enable/disable`) runs, then the CLI blocks the operation with guidance on fixes or re-enabling. Operators may supply the documented `--force` overrides (e.g., `refresh --force`, `rename --force`, `remove --force`) to proceed, in which case the tooling logs a prominent warning that the action is bypassing health gates and records that the command forced execution despite a failing check. Freshly initialized sources that were auto-enabled because their target validated are not subject to this guard until a subsequent disablement occurs.

## Constraints & Dependencies
- Constraints: workspace config remains the single source of truth; manifests are CLI-owned operational records (not safe to regenerate externally) and must never conflict with config. Follow seam-first architecture and existing CLI patterns. Use a lightweight slug/name sanitation helper (e.g., `python-slugify`) if standard library tooling is insufficient, and leave seams for a forthcoming database management module to own locking/concurrency beyond resetting the stub file.
- Config schema: represent each source under a table keyed by its normalized name:
  ```toml
  [workspace.sources]

    [workspace.sources.alpha-source]
    enabled = false
    path = "<workspace>/sources/alpha-source"
    target = "/absolute/project/path"
  ```
  `enabled` defaults false until the operator runs `raggd source enable <name>`. `target` is optional and omitted when unset. Health state is tracked outside the workspace config so it stays focused on declarative settings; the latest checkhealth payload lives alongside manifests and the aggregated `.health.json`. Operational timestamps such as `last_refresh_at` live in manifests so the config remains declarative.
  Per-source manifests stored at `<workspace>/sources/<name>/manifest.json` mirror operational data in JSON form:
  ```json
  {
    "name": "alpha-source",
    "path": "<workspace>/sources/alpha-source",
    "target": "/absolute/project/path",
    "enabled": false,
    "last_refresh_at": null,
    "last_health": {"status": "unknown", "checked_at": null, "summary": null, "actions": []}
  }
  ```
  Manifests are append-only operational records managed exclusively by the CLI; they carry the authoritative operational fields (`last_refresh_at`, `last_health`) and must not be regenerated externally even though the config stays canonical.
- Dependencies: update `pyproject.toml` extras to group module dependencies (add a `source` extra), declare any slug/manifest helpers, and wire module toggles under `[modules.source]` in `raggd.toml` defaults. Integrate with existing dependency injection and CLI registration for health checks. Surface health status in `<workspace>/.health.json` and persist per-source manifests at `<workspace>/sources/<name>/manifest.json`. Removing legacy flows may require coordination with other ongoing branches to avoid drift.
- Decisions: Model sources as entries under `[workspace.sources]`, ensuring CLI list/enable/disable flows can iterate deterministically. Skip migration tooling because the project is still in scaffolding; communicate that existing manual directories require manual adoption.

## Security & Privacy
- Validate user-provided paths to prevent traversal outside allowed workspace roots. Sanitization must reject unexpected characters and collisions. Ensure SQLite files inherit workspace permissions and never store secrets. The tooling must not introduce new external network calls or leak sensitive paths in logs.

## Telemetry & Operability
- Emit structured logs for command start/success/failure including source name and outcome; integrate with existing logging configuration. Health checks should expose status, the manifest-managed `last_refresh_at` timestamp, and errors via CLI and manifest output, and log whenever a `raggd source` command automatically disables a source after a failed check. Leave stubs/hooks for future metrics integration. Document manual health verification and troubleshooting steps for operators.

## Rollout / Revert
- Introduce a `modules.source` toggle default-on for new workspaces and document how to disable it. Rollout entails new CLI commands, config schemas, and documentation updates (no automated migrations; existing workspaces adopt manually). Reverting removes the module toggle, CLI command group, config entries/manifests, and ensures commands fail gracefully if the module is disabled. Provide cleanup guidance for leftover directories.

## Definition of Done
- [ ] `raggd source` command group implements described flows (init, target, refresh, rename, remove) with normalization, confirmations, and force overrides
- [ ] `raggd source list|enable|disable` commands respect config toggles and health gating semantics (mutating flows auto-disable on health failures; enable remains advisory)
- [ ] Health gating and `raggd checkhealth` integration covered by unit/CLI tests and manual smoke notes, verifying that command-triggered checks toggle enablement while the standalone health tool remains read-only
- [ ] Workspace config schema, manifest format, and user/dev docs updated (including logging guidance)
- [ ] Legacy source code/tests removed or refactored to match new workflows
- [ ] Dependency grouping and configuration toggles verified in `pyproject.toml` and defaults

## Ownership
- Owner: @matt
- Reviewers: @codex
- Stakeholders: @docs, @ops

## Links
- Related: .agents/guides/workflow.md

## History
### 2025-10-03 17:39 PST
**Summary** — Draft spec for source management tooling
**Changes**
- Created initial spec outlining CLI, config, health, and rollout considerations

### 2025-10-03 17:53 PST
**Summary** — Clarify manifests and config questions
**Changes**
- Locked in health manifest locations and noted future DB tooling
- Expanded config-schema open question with trade-offs for decisions

### 2025-10-03 18:02 PST
**Summary** — Capture list/enable/disable commands and config decisions
**Changes**
- Added CLI behaviors for `source list|enable|disable`
- Recorded `[workspace.sources]` model and noted no migrations required

### 2025-10-03 18:08 PST
**Summary** — Rename per-source manifest file
**Changes**
- Updated manifest path references to `manifest.json` (non-hidden)

### 2025-10-03 18:09 PST
**Summary** — Consolidate on singular `raggd source` CLI naming
**Changes**
- Switched list/enable/disable flows to live under the `raggd source` command group

### 2025-10-03 18:20 PST
**Summary** — Clarify init targeting and health auto-disable
**Changes**
- Added `--target` option for `source init` with enablement rules
- Documented automatic disablement on failed health checks and related logging/DoD updates

### 2025-10-03 18:37 PST
**Summary** — Nail down target clearing, health storage, and enable semantics
**Changes**
- Clarified shared-target allowance, `--clear` UX, and path validation requirements
- Documented `.health.json` record shape and removed config health mutation
- Clarified `source enable` health check behavior

### 2025-10-03 18:48 PST
**Summary** — Clarify checkhealth contract and manifest handling
**Changes**
- Made `raggd checkhealth` explicitly read-only for module hooks and detailed the hook interface expectations
- Documented that command-triggered health failures drive auto-disable behavior and updated exit-code semantics for `source list`
- Noted manifests are CLI-owned operational records (not safe to regenerate) and refreshed telemetry/DoD language
