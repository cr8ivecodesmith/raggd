-- uuid7: 018cc778-5000-730a-b8e0-d0be029acc0a
PRAGMA foreign_keys = ON;

-- Table: sources - Workspace sources mirrored into the database ledger.
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL
);

-- Table: migrations_audit - Audit trail of lifecycle operations per source.
CREATE TABLE IF NOT EXISTS migrations_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    applied_shortuuid7 TEXT NOT NULL REFERENCES schema_migrations(shortuuid7) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('ensure', 'upgrade', 'downgrade', 'reset')),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    executed_at TEXT NOT NULL,
    details TEXT,
    UNIQUE (source_id, applied_shortuuid7, operation, executed_at)
);

CREATE INDEX IF NOT EXISTS idx_sources_last_batch
    ON sources(last_batch_id);

CREATE INDEX IF NOT EXISTS idx_migrations_audit_source
    ON migrations_audit(source_id);

CREATE INDEX IF NOT EXISTS idx_migrations_audit_short
    ON migrations_audit(applied_shortuuid7);

CREATE INDEX IF NOT EXISTS idx_migrations_audit_executed_at
    ON migrations_audit(executed_at);
