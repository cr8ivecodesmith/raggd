-- uuid7: 018cc778-5000-730a-b8e0-d0be029acc0a
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migrations_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_shortuuid7 TEXT NOT NULL,
    status TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

