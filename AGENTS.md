# AGENTS Guidelines

## Purpose

- Provide baseline orientation for contributors using the AGENTS workflow.
- Outline canonical references to review before starting any task.
- Point to the directory hierarchy so new projects can adapt and extend it.

## Workflow Reminders

- Before addressing any task, ensure you have read `.agents/guides/workflow.md`
  and the relevant templates in `.agents/guides/workflow-extras/`.
- Always review the task's `spec.md` and `implementation.md` (if present) to understand
  the scope and requirements.
  If these documents are missing or unclear, seek clarification before proceeding.

## Directory Overview

- `.agents/guides/`: Core reference material on engineering practice, patterns, style,
  and workflow.
- `.agents/tasks/feat/`: Feature work specs, each folder numbered with a slug
  and containing `spec.md` plus follow-on artifacts.
- `.agents/tasks/patch/`: Patch or follow-up efforts (refactors, hardening), organized the same way.
- `.agents/tasks/bug/`: Bug fixes, organized the same way.
- `.agents/tasks/misc/`: Miscellaneous tasks and chores, organized the same way.

## Guides

- `.agents/guides/engineering-guide.md`: Micro-design principles covering seam-first architecture,
  dependency injection, testing focus, and other day-to-day engineering defaults.
- `.agents/guides/patterns-and-architecture.md`: Deep dive on organization patterns,
  composition vs. inheritance guidance, module layout, logging, and anti-patterns.
- `.agents/guides/styleguides.md`: Language and tooling conventions, documentation expectations,
  and semantic line break practices.
- `.agents/guides/workflow.md`: Canonical collaboration loop with the human, including task lifecycle,
  templates, and expectations for history updates and reviews.
- `.agents/guides/workflow-extras/`: Template library used by `workflow.md`
  (e.g., `codereview-tpl.md`, `implementation(-mini)-tpl.md`, `spec(-mini)-tpl.md`).

Use these references when scoping new work, reviewing deliverables, or aligning implementation details
with existing expectations.

## Project-specific Guidance

_Add project- or stack-specific process notes here (tooling, branch strategy, roles, etc.)._
