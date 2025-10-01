# Style Guides

## Purpose and Scope
- Provide baseline style expectations that travel well across projects and stacks.
- Start from language-community defaults (PEP 8, StandardJS, etc.) and customize only when a project has a clear need.
- Treat the language-specific sections below as illustrative patterns to adapt or replace when onboarding a new stack.

## Core Principles
- Align with the primary community style guides first; document any deliberate deviations.
- Bake enforcement into tooling (linters, formatters, CI checks) committed alongside the codebase.
- Favor clarity and maintainability over cleverness; optimize code and docs for reviewability.
- Cross-link to deeper guidance (`patterns-and-architecture.md`, `engineering-guide.md`, `workflow.md`)
  instead of duplicating content.

## Cross-language Conventions
- Default maximum line length: 120 characters for code and prose; adjust when a language or project requires otherwise.
- Prefer trailing commas in multi-line constructs (where supported) to minimize diff noise.
- Ensure every file ends with a newline and avoid trailing whitespace.
- Indentation defaults: Python uses 4 spaces; most scripts, configs, and markup use 2 spaces;
  defer to language norms when they differ.
- Capture defaults with `.editorconfig` (example below) and override per language as needed.

### EditorConfig (example)
```ini
# .editorconfig
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 2

[*.py]
indent_size = 4

[*.toml]
indent_size = 4

[Makefile]
indent_style = tab
```

## Documentation
- Use semantic line breaks to keep diffs small and reviews approachable.
- Structure Markdown with an H1 title, H2 main sections, and avoid skipping heading levels;
  cap at H4 unless a project states otherwise.
- Layer information from summary to detail so readers can decide how deep to go.
- When deviating from these defaults, document the rationale in the project-specific section below.
- Complement this guidance with `patterns-and-architecture.md` (rationale framing) and
  `workflow.md` (collaboration practices).

### Semantic line breaks
Write sentences with deliberate breaks at natural phrase boundaries—not merely at a fixed column.
This approach keeps diffs focused and easier to review.

```md
# Caching Strategy

We cache GET responses for 5 minutes
to reduce load on the upstream API.
This balances freshness with performance
for typical browsing sessions.
```

### Heading levels and examples
```md
# Feature Toggle Rollout

## Overview

### Goals

#### Metrics

## Implementation
```
- Do not jump from H2 to H4; progress one level at a time.
- Keep headings concise and parallel in structure.

### Layering information
```md
# Background Jobs Strategy

Offload non-blocking work to improve latency.

Key points:
- Queue selection and trade-offs
- Retry and idempotency guidelines
- Monitoring and alerting

Details and examples:
Jobs must be idempotent.
Prefer at-least-once delivery with deduplication keys.
Record metrics for enqueue time, run time, and failures.
...
```

## Language Reference Examples
Language sections illustrate how to capture project decisions. Swap or extend them when the stack changes.

### Python (illustrative)
- Start from PEP 8 (including tests) and layer only the exceptions this project needs.
- Use Google-style docstrings for consistency with type annotations and tooling.
- Favor explicit typing and descriptive names; keep imports grouped by standard library,
  third-party, then local modules.

#### Conventions and example
```python
# Good: imports grouped, typed signatures, 4-space indents, clear names
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable


def chunk(items: Iterable[int], size: int) -> list[list[int]]:
    """Split items into contiguous chunks of at most size.

    Args:
      items: Source integers to group.
      size: Maximum chunk length; must be positive.

    Returns:
      A list of chunks preserving original order.

    Raises:
      ValueError: If size is not positive.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    out: list[list[int]] = []
    buf: list[int] = []
    for x in items:
        buf.append(x)
        if len(buf) == size:
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    return out


@dataclass
class Point:
    x: float
    y: float

    def dist2(self, other: "Point") -> float:
        dx, dy = self.x - other.x, self.y - other.y
        return dx * dx + dy * dy
```

```python
# Bad: ambiguous names, mixed indent, unclear exceptions, no typing
def c(a, b):
  if b<=0: raise Exception('bad')
  r=[]; t=[]
  for i in a: t.append(i);
  if len(t)==b: r.append(t); t=[]
  if t: r.append(t)
  return r
```

#### Docstrings
```python
def normalize_email(email: str) -> str:
    """Normalize an email address to lowercase without surrounding spaces.

    Args:
      email: The input address which may contain spaces or mixed case.

    Returns:
      A lowercase, trimmed email address.
    """
    return email.strip().lower()
```
- Modules: add a brief module-level docstring covering purpose and key concepts.
- Classes: include a short summary and an Attributes section for key fields.
- Properties: document the getter when computed values or side effects exist.
- For organizing rationale narratives, see `patterns-and-architecture.md`.

#### Ruff configuration (example)
```toml
# pyproject.toml
[tool.ruff]
line-length = 80
target-version = "py311"
select = [
  "E",  # pycodestyle
  "F",  # pyflakes
  "I",  # import order
  "UP", # pyupgrade
  "D",  # pydocstyle (Google style via convention below)
]
ignore = [
  "D203", # one-blank-line-before-class; prefer D211
]

[tool.ruff.pydocstyle]
convention = "google"
```
- Treat this configuration as a starting point; tune line length, rule sets, or ignores per project.

#### Pytest conventions (example)
- Structure: `tests/` with `unit/` and `integration/` subfolders when useful.
- Naming: files `test_*.py`; functions `test_<unit>_<behavior>`; fixtures in `conftest.py`.
- Fixtures: prefer factory-style fixtures; scope narrowly; avoid `autouse` except for environment setup.
- Marks: use `@pytest.mark.slow`, `@pytest.mark.integration`, etc.; register them in config to silence warnings.
- Assertions: use bare `assert`; prefer `pytest.raises` for exception checks.

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
markers = [
  "slow: long-running tests",
  "integration: crosses process or network boundaries",
]
```

### JavaScript and TypeScript (illustrative)
- Default to modern ECMAScript/TypeScript community standards (e.g., StandardJS, ts-standard, ESLint with Prettier)
  unless the project specifies others.
- Prefer absolute imports from the project root using path aliases; avoid deep relative paths.
- Order imports by standard library/built-ins, third-party packages, and internal modules;
  separate groups with blank lines.
- Use named exports by default to clarify module contracts; reserve default exports for singular responsibilities.

```js
// 2 spaces, no semicolons, single quotes, spacing around keywords
export function sum (xs) {
  if (!Array.isArray(xs)) return 0
  return xs
    .filter(x => typeof x === 'number')
    .reduce((acc, x) => acc + x, 0)
}
```

```ts
export interface User {
  id: string
  email: string
  active: boolean
}

export function activate (u: User): User {
  if (u.active) return u
  return { ...u, active: true }
}

export async function fetchUser (id: string): Promise<User | null> {
  const res = await fetch(`/api/users/${id}`)
  if (!res.ok) return null
  return await res.json() as User
}
```

#### Async and error handling
- Wrap `fetch`/HTTP clients with helpers for base URLs, timeouts, and consistent JSON handling.
- Catch errors at boundaries (UI handlers, API adapters); keep inner logic mostly error-agnostic.
- Prefer discriminated unions or `Result`-style types for recoverable errors.

```ts
// Minimal fetch wrapper with timeout and JSON
export async function http<T>(input: RequestInfo, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10_000)
  try {
    const res = await fetch(input, {
      ...init,
      signal: controller.signal,
      headers: {
        'content-type': 'application/json',
        ...(init.headers ?? {})
      }
    })
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }
    return await res.json() as T
  } finally {
    clearTimeout(timeout)
  }
}
```

#### Minimal TypeScript config (example)
```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noImplicitAny": true,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "lib": ["ES2020", "DOM"],
    "baseUrl": ".",
    "paths": {
      "@app/*": ["src/*"]
    }
  },
  "include": ["src"]
}
```
- Use this as a template; adjust compiler options (module targets, libs, paths) per project requirements.

#### Common pitfalls
- Avoid `any` in TypeScript; type public surfaces.
- Keep barrels (`index.ts`) focused to prevent circular dependencies.
- Document any custom ESLint/Prettier rules in the project-specific section.

## Project-specific Guidance
_Add repository or team-specific style rules here (tool versions, formatting exceptions, language additions, etc.)._
