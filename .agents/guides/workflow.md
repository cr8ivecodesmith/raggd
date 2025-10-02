# Workflow

This document describes how the agent will work with the human.

## Overview

The human will direct you on what task or spec you will be working on together.

A typical task development cycle comprises the following steps:

1. Understand the task
2. Gather resources
3. Plan the solution
4. Plan test items
5. Execute plan
6. Update history
7. (Optional) Code review

**Important:**

- The human may ask you to create or improve a task spec first.
- Some steps may require a few rounds of back-and-forth discussion before proceeding.
- Confirm explicit permission from the human before moving on to the next step.
- The human may ask you to skip steps in the implementation plan.
- For large tasks, the human will want to work on one milestone at a time.

### 1. Understand the task
Ideate and articulate how you understand the spec.
List logical milestones for larger tasks to surface sequencing and scope.
This helps identify constraints and opportunities early.
The human may correct or enhance what you write as clarification or additional context.

### 2. Gather resources
With the spec in mind, collect resources you’ll use to plan the solution.
This step improves focus and context management — like a chef preparing every ingredient before cooking.

Substeps:
1. Collect project and external docs
2. Identify affected systems
3. Identify affected source files
4. Identify security implications

### 3. Plan the solution
Design how to put things together.
Draw from `patterns-and-architecture.md` and `engineering-guide.md`.
Implementation should include a step-by-step checklist
covering the automated tests, quality gates, and review artifacts required by the project.

### 4. Plan test items
Decide how to verify the outcomes.
Leverage testing practices from `patterns-and-architecture.md` and conventions from `styleguides.md`.
Document project-specific tooling or coverage expectations in the addendum below when applicable.

### 5. Execute plan
Carry out the implementation and test plan.
Expect several iterations and updates as you progress.

### 6. Update history
Before closing a task or milestone, update the `History` section in the spec or implementation doc.
Use concise, commit-style entries.
Log the time using `date +"%Y-%m-%d %H:%M %Z"`.

### 7. Code review (optional, upon request)
The human may ask you to generate a structured code review.
In this case, create a `codereview.md` file in the task folder using the template in:

```
.agents/guides/workflow-extras/codereview-tpl.md
```

The review should:
- Summarize diffs in natural language
- Check alignment against:
  - `patterns-and-architecture.md`
  - `styleguides.md`
  - `engineering-guide.md`
  - `workflow.md`
- Flag drifts, suggest improvements, or confirm readiness


## Resources

### Task folder structure

All tasks reside in `.agents/tasks`:

```
.agents/tasks/
├── bug/   # bug tickets
├── feat/  # feature tickets
├── misc/  # chores, docs, etc.
│   └── 0000-initialize\_project/
│       ├── spec.md
│       ├── implementation.md
│       └── attachments/
└── patch/ # refactors or upgrades
```

### Templates

Templates are stored in:

```
.agents/guides/workflow-extras/
```

- **Full templates**
  - `spec-tpl.md`
  - `implementation-tpl.md`

- **Mini templates** (use for small chores/bugfixes/patches)
  - `spec-mini-tpl.md`
  - `implementation-mini-tpl.md`

- **Code review template**
  - `codereview-tpl.md`


## Project-specific guidance

- Run all tooling through `uv` (for example `uv run raggd`, `uv run pytest`,
  `uv run ruff check`). These commands write to `~/.cache/uv`, so expect to
  request escalated permissions when running inside restricted sandboxes.
- Before marking a task done, capture a manual smoke run in the task history
  (`uv run raggd init` with default workspace, `--workspace`, `--refresh`, and
  the `RAGGD_WORKSPACE` override). The manual note is part of the DoD.
- Keep the 100% pytest coverage gate enabled and lint with `uv run ruff check`
  before handing work back to the human.
- Workspace refreshes compress the previous state into
  `<workspace>/archives/<timestamp>.zip`; confirm the CLI output references the
  archive path when performing manual checks.
- Module toggles live in `[modules.<name>]` sections of `raggd.toml`. Align the
  extras declared in `pyproject.toml` with these names and document user-facing
  flags whenever new modules land.
- Configuration precedence is `CLI flags` > `RAGGD_*` env vars > user
  `raggd.toml` > the packaged defaults file (`raggd.defaults.toml`). Defaults
  stay in-app: do not copy them into user workspaces during init flows.


## Exceptions and nuances

Depending on the task, some steps may be unnecessary.
The **understanding step** is where the agent should make judgment calls and propose skipping if appropriate.
The human makes the final call.
