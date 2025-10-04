# Workspace Bootstrap

Use the `raggd` CLI to create and maintain a local workspace that holds
configuration, logs, and module metadata. The CLI is designed to be
idempotent—running it multiple times only refreshes files when you ask it to.

## Command overview

Every command ships with Typer-powered help text, so append `--help` whenever
you need the authoritative options or subcommand list.

### Initialize a workspace

```console
$ raggd init --help
```

Key options:
- `--workspace / -w <path>`: Override the workspace directory. Defaults to
  `~/.raggd`, or `RAGGD_WORKSPACE` when the environment variable is set.
- `--refresh`: Archive the current workspace (if present) into
  `archives/<timestamp>.zip` before rebuilding a clean layout.
- `--log-level / -l <level>`: Override the logging level (DEBUG, INFO, WARNING,
  ERROR). Defaults to the value recorded in `raggd.toml`.
- `--enable-module / -E <module>`: Force-enable one or more modules for this
  run, even if disabled in configuration. Provide the flag multiple times to
  enable several modules.
- `--disable-module / -D <module>`: Force-disable modules for this run. Accepts
  multiple flags, just like `--enable-module`.

### Manage sources

```console
$ raggd source --help
```

Sources let you track workspaces, document trees, or other targets you want to
refresh and query. The command group provides:
- `init <raw-name> [--target <dir>]`: Create a normalized source (e.g.,
  `docs` → `docs` or `Product Notes` → `product-notes`), scaffold
  `<workspace>/sources/<name>` with a manifest and `db.sqlite3`, and optionally seed
  the target directory. When a valid `--target` is supplied the source is
  immediately enabled and refreshed; otherwise it stays disabled until you fix
  the target and enable it.
- `target <name> <dir|--clear>`: Point a source at a new directory (or clear
  the association with `--clear`). The CLI validates readability, prompts
  before refreshing unless `--force` is provided, and records `last_refresh_at`.
- `refresh <name>`: Rebuild cached artifacts for a source. Disabled or unhealthy
  sources block refreshes unless you add `--force`, which is useful when you are
  actively fixing an issue the health check found.
- `rename <old> <new>` / `remove <name>`: Rename or delete sources after
  passing the same health guard as other lifecycle commands. Use `--force` to
  perform remediation when the source is currently unhealthy.
- `enable <name>` / `disable <name>`: Toggle the `enabled` flag, optionally
  recording actions suggested by health checks before you re-enable a source.
- `list`: Report every configured source along with enablement, target, and the
  most recent health status. The command exits non-zero if any source is
  degraded or unknown so scripts can detect issues quickly.

### Check workspace health

```console
$ raggd checkhealth --help
```

Runs health hooks registered by enabled modules and writes aggregated findings
to `.health.json`. Provide a module name to limit the update to a single module
(`raggd checkhealth source`, for example) while carrying forward prior results
for untouched modules.

## Configuration precedence

Settings are merged in a predictable order so you can override behavior at the
right layer:

1. CLI flags (`--workspace`, `--log-level`, module enable/disable switches)
2. Environment variables (`RAGGD_WORKSPACE`, `RAGGD_LOG_LEVEL`, future entries)
3. User-managed `raggd.toml` inside the workspace
4. Packaged defaults from `raggd.defaults.toml`

The generated `raggd.toml` includes inline comments describing the available
keys and documenting the environment variables that influence them.

## Workspace layout

Running `raggd init` creates the following structure:

```
~/.raggd/
├── raggd.toml              # Editable user configuration with comments
├── .health.json            # Aggregated health snapshot from raggd checkhealth
├── logs/                   # Rolling log files (gzip rotated)
├── archives/               # Timestamped ZIP archives created by --refresh
└── sources/
    └── <name>/
        ├── db.sqlite3      # SQLite stub reserved for future embeddings
        └── manifest.json   # Target metadata, health status, refresh history
```

The packaged defaults live inside the application bundle as
`raggd.defaults.toml`; they are referenced when rendering `raggd.toml` but are
not copied into the workspace.

Use `--workspace` or `RAGGD_WORKSPACE` to pick a different base directory—the
layout remains the same. Source directories are created on demand when you run
`raggd source init`.

## Environment variables

- `RAGGD_WORKSPACE`: Overrides the workspace directory without editing
  configuration files. Equivalent to passing `--workspace` on the CLI.
- `RAGGD_LOG_LEVEL`: Overrides the default log level (e.g., `DEBUG`, `INFO`).
  CLI flags still take precedence if both are provided.

## Module toggles

Optional capabilities are grouped under the `[modules]` table in
`raggd.toml`. Each module entry shares the same shape:

```toml
[modules.rag]
enabled = true
extras = ["rag"]
```

- `enabled`: Whether the feature should be activated.
- `extras`: Optional dependency groups that must be installed (via `uv` extras)
  for the module to load.

You can enable or disable modules either by editing `raggd.toml`, running the
CLI with `--enable-module` / `--disable-module`, or adding future automation
that writes to the config file. When the CLI runs, it reports which modules are
active and why others are disabled (e.g., missing optional dependencies).

## Source lifecycle and health

The source module is on by default. Each lifecycle command performs a health
check before mutating state, automatically disabling a source if the check
fails so you can remediate without carrying stale data forward. Review
`manifest.json` or `raggd source list` for summaries and suggested follow-up
actions, then rerun `raggd source enable <name>` once the target looks good.

Run `raggd checkhealth` periodically (or from automation) to refresh
`.health.json`. The file keeps the latest status emitted by every module so you
can diff runs, feed the data into dashboards, or debug user reports.

## Next steps

Browse the rest of the documentation for deeper dives into individual modules
as they land. The CLI documentation will expand alongside new commands and
options.
