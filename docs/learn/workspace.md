# Workspace Bootstrap

Use the `raggd` CLI to create and maintain a local workspace that holds
configuration, logs, and module metadata. The CLI is designed to be
idempotent—running it multiple times only refreshes files when you ask it to.

## Command overview

```console
$ raggd init [OPTIONS]
```

Available options:
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

> Tip: Use `raggd init --help` to see the authoritative set of options.

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
├── logs/                   # Rolling log files (gzip rotated)
└── archives/               # Timestamped ZIP archives created by --refresh
```

The packaged defaults live inside the application bundle as
`raggd.defaults.toml`; they are referenced when rendering `raggd.toml` but are
not copied into the workspace.

Use `--workspace` or `RAGGD_WORKSPACE` to pick a different base directory—the
layout remains the same.

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

## Next steps

Browse the rest of the documentation for deeper dives into individual modules
as they land. The CLI documentation will expand alongside new commands and
options.
