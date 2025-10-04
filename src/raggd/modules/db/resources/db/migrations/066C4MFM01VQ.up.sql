-- uuid7: 018cc251-f400-7775-8df3-60bda8412761
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bootstrap_shortuuid7 TEXT NOT NULL,
    head_migration_uuid7 TEXT NOT NULL,
    head_migration_shortuuid7 TEXT NOT NULL,
    ledger_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_vacuum_at TEXT,
    last_sql_run_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid7 TEXT UNIQUE NOT NULL,
    shortuuid7 TEXT UNIQUE NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('up','down')),
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

