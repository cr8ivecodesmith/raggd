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
    direction TEXT NOT NULL CHECK (direction IN ('up', 'down')),
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schema_migrations_short
    ON schema_migrations(shortuuid7);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    ref TEXT,
    generated_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS embedding_models (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    UNIQUE (provider, name)
);

CREATE TABLE IF NOT EXISTS vdbs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    embedding_model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE RESTRICT,
    faiss_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name)
);

CREATE INDEX IF NOT EXISTS idx_vdbs_batch
    ON vdbs(batch_id);

CREATE INDEX IF NOT EXISTS idx_vdbs_model
    ON vdbs(embedding_model_id);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    lang TEXT NOT NULL,
    file_sha TEXT NOT NULL,
    mtime_ns INTEGER,
    size_bytes INTEGER,
    UNIQUE (batch_id, repo_path, file_sha)
);

CREATE INDEX IF NOT EXISTS idx_files_batch_path
    ON files(batch_id, repo_path);

CREATE INDEX IF NOT EXISTS idx_files_lang
    ON files(lang);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    symbol_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    symbol_sha TEXT NOT NULL,
    symbol_norm_sha TEXT,
    args_json TEXT,
    returns_json TEXT,
    imports_json TEXT,
    deps_out_json TEXT,
    docstring TEXT,
    summary TEXT,
    tokens INTEGER,
    first_seen_batch TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
    last_seen_batch TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_symbols_span
    ON symbols(file_id, start_line, end_line, symbol_sha);

CREATE INDEX IF NOT EXISTS idx_symbols_symbol_path
    ON symbols(symbol_path);

CREATE INDEX IF NOT EXISTS idx_symbols_kind
    ON symbols(kind);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    vdb_id INTEGER NOT NULL REFERENCES vdbs(id) ON DELETE CASCADE,
    header_md TEXT NOT NULL,
    body_text TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    UNIQUE (symbol_id, vdb_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_vdb
    ON chunks(vdb_id);

CREATE INDEX IF NOT EXISTS idx_chunks_symbol
    ON chunks(symbol_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    header_md,
    body_text,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_fts(rowid, header_md, body_text)
    VALUES (new.id, new.header_md, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, header_md, body_text)
    VALUES ('delete', old.id, old.header_md, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, header_md, body_text)
    VALUES ('delete', old.id, old.header_md, old.body_text);
    INSERT INTO chunk_fts(rowid, header_md, body_text)
    VALUES (new.id, new.header_md, new.body_text);
END;

CREATE TABLE IF NOT EXISTS edges (
    src_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    dst_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (src_chunk_id, dst_chunk_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_edges_src
    ON edges(src_chunk_id);

CREATE INDEX IF NOT EXISTS idx_edges_dst
    ON edges(dst_chunk_id);

CREATE INDEX IF NOT EXISTS idx_edges_kind
    ON edges(kind);

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    vdb_id INTEGER NOT NULL REFERENCES vdbs(id) ON DELETE CASCADE,
    dim INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vectors_vdb
    ON vectors(vdb_id);
