# Engineering Guidelines – Micro‑Design Principles

A living guide for everyday decisions that keep the codebase evolvable, testable, and pleasant to work in.

## Audience & Scope

- Audience: Project contributors.
- Scope: Micro‑level decisions (functions, classes, modules, small features).
  Pairs with the Architecture doc (macro boundaries).
- Default stance: Design Mode (production‑ready).

## North‑Star Principles

- Seams over Singletons – prefer interfaces/abstractions and dependency seams to enable swapping and testing.
- Small, Focused Units – one purpose per function/module; fewer than ~40 lines per function is a good heuristic.
- Pure Core, Dirty Edges – keep pure logic isolated; push I/O and side‑effects to the boundaries.
- Make Change Easy – design for the likely next change; prefer composition, not inheritance.
- Readable First – code is for humans; optimize for clarity before cleverness.

> Rule of thumb: if you can’t write a 1‑sentence purpose for a function, it’s doing too much.

## Layering & Boundaries (Micro)

- Keep clear layers: UI (CLI/TUI/WEB/GUI) → Commands/Use-Cases → Services/Repos → Infra (databases, external services,
  file systems, network).
- Each layer talks only to the next layer down; no skipping layers or circular dependencies.
- Prefer contracts (protocols/ABCs) for boundaries; avoid concrete types leaking across layers.


## Function & Class Design

- Function size: aim for ≤ 40 LOC; break by intent (parse → validate → act → format).
- Arguments: prefer small, typed parameter lists.
  Reach for lightweight schema helpers (dataclasses, attrs, pydantic, or equivalents)
  when inputs need structure.
- Return types: return data, not print; UI decides presentation.
- Classes: use for stateful workflows or cohesive behavior; otherwise keep it functional.


**Template**

```
def do_thing(input: InputModel, deps: Deps) -> OutputModel:
    """Parse → validate → compute → persist → format."""
    parsed = parse(input)
    ensure(valid(parsed))
    result = core_compute(parsed, deps)
    persist(result, deps)
    return format_out(result)
```


## Error Handling & Results

- Business errors: raise domain exceptions (e.g., RecordNotFound, ValidationFailed).
  Catch at the command boundary to map to user-friendly messages.
- Expected fallible ops: consider Result[Ok, Err] pattern (typed container) when it improves clarity.
- Logging on edges: log context at boundaries; avoid logging deep in pure core.


**Do**

```
try:
    run_job(job_id)
except JobAlreadyRunning as e:
    return warn(str(e))
```

Avoid swallowing exceptions or returning None for error states.


## Dependency Injection (Practical)

- Contracts first: define protocols/ABCs in core (e.g., DataStore, Clock, SecretsProvider).
- Impls in adapters: keep concrete classes in adapters/infra packages that match the boundary
  (db/, messaging/, identity/, etc.).
- Bootstrap composes: wire dependencies in a bootstrap() or factories; pass them explicitly into commands/services.
- Runtime switches: feature flags or configuration select impls; avoid globals/singletons.
- Anti‑pattern: from my_impl import GlobalClient; GlobalClient.do() in business code.


## Side‑Effects & I/O

- Boundary functions should be small and thin (open file, call API, emit event).
- Core functions should be deterministic and easy to unit test (pure in → pure out).
- Idempotency: where feasible, make commands safe to retry.


## Naming & Structure

- Modules: verbs for commands (process_order.py, reconcile_accounts.py), nouns for models (invoice.py, report.py).
- Functions: imperative verbs (calculate_totals, attach_metadata).
- Events: past-tense facts (invoice.sent, job.failed).


## Testing Guide (Fast Feedback)

- Unit: pure core logic; no DB/network.
- Contract: adapters honor interfaces (fake + real); snapshot/golden tests for UIs where they add value.
- Integration: end-to-end workflows (capture input → process data → deliver output).
- Fakes over Mocks: prefer simple fakes/stubs; mock only hard edges.

**Checklist**

- Unit tests for core logic
- Contract tests for providers/repos
- Golden UI snapshots updated intentionally
- Error paths tested (not just happy paths)


## Async/Concurrency

- Prefer sync unless there’s real parallel I/O.
- Use a dedicated job runner or scheduler for background/long-running tasks; keep the choice swappable.
- Keep async localized; don’t leak async into pure core.


## Logging & Telemetry

- Levels: DEBUG (developer), INFO (state changes), WARNING (recoverable issues),
  ERROR (failures), CRITICAL (system down).
- Structure logs (key=value) at boundaries; avoid noisy logs in tight loops.
- Respect privacy: redact secrets; opt‑in telemetry only.


## Data & Schema Micro‑Rules

- Migrations are append‑only; never edit old migrations.
- Prefer opaque, sortable identifiers (e.g., ULIDs, UUIDv7); avoid meaning-laden keys.
- Keep derived data denormalized only when measured wins exist.


## Performance Micro‑Heuristics

- First make it correct & clear. Optimize when it hurts and is measured.
- Prefer streaming/iterators for large files; avoid loading entire PDFs/audio into memory.


## Security Basics

Where applicable:

- Use project-approved storage/encryption layers; never bypass them for convenience.
- Centralize key material through the designated secrets manager; no custom crypto.
- Treat all credentials and tokens as secrets; never log or store in plaintext.


## PR & Code Review Checklist

- Function/module has a single clear purpose.
- Dependencies injected at edges; no hidden globals.
- Errors handled at command boundaries; domain exceptions used.
- Tests: unit + contract; golden snapshots intentional.
- Naming consistent with conventions.
- No gratuitous async; jobs used for long tasks.
- Logs are actionable; no secrets.


> Blocker labels: “Leaky boundary”, “God function”, “Hidden global”, “Adapter drift”.


## Examples (Before → After)

**Monolithic**

```
def run():
    # parses args, queries data source, calls external API, writes file, prints UI
    ...
```

**Refactored**

```
def command_run(args: Args, deps: Deps) -> ExitCode:
    data = load(args.source, deps.repo)
    items = transform_data(data, deps.processor)
    save(items, deps.repo)
    return present(items, deps.ui)
```


## Project-Specific Practices

- _Add project-tailored guidelines here (tech stacks, workflows, compliance notes)._
  _Keep the main sections above tool-agnostic._


## Living Document

- Update this guide when a rule causes friction.
- Record exceptions via lightweight ADR notes in PRs ("we deviated because …").
- Shortlink mantra: Small seams, pure core, dirty edges.
