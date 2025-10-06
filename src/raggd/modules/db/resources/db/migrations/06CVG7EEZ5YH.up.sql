-- uuid7: 0199b81d-cef9-7d18-b4d5-57697d8e0cac
PRAGMA foreign_keys = ON;

-- Table: chunk_slices - Stores parser-emitted chunk parts independent of VDB materialization.
CREATE TABLE IF NOT EXISTS chunk_slices (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    parent_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    chunk_id TEXT NOT NULL,
    handler_name TEXT NOT NULL,
    handler_version TEXT NOT NULL,
    part_index INTEGER NOT NULL CHECK (part_index >= 0),
    part_total INTEGER NOT NULL CHECK (part_total >= 1),
    start_line INTEGER,
    end_line INTEGER,
    start_byte INTEGER,
    end_byte INTEGER,
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    content_hash TEXT NOT NULL,
    content_norm_hash TEXT,
    content_text TEXT NOT NULL,
    overflow_is_truncated INTEGER NOT NULL DEFAULT 0 CHECK (overflow_is_truncated IN (0, 1)),
    overflow_reason TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_seen_batch TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
    last_seen_batch TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
    UNIQUE (batch_id, chunk_id, part_index)
);

CREATE INDEX IF NOT EXISTS idx_chunk_slices_chunk_id
    ON chunk_slices(chunk_id);

CREATE INDEX IF NOT EXISTS idx_chunk_slices_symbol
    ON chunk_slices(symbol_id);

CREATE INDEX IF NOT EXISTS idx_chunk_slices_parent_symbol
    ON chunk_slices(parent_symbol_id);

CREATE INDEX IF NOT EXISTS idx_chunk_slices_file
    ON chunk_slices(file_id);

CREATE INDEX IF NOT EXISTS idx_chunk_slices_last_seen
    ON chunk_slices(last_seen_batch);
