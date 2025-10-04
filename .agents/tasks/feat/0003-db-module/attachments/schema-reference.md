# RAGGD Source Schema Notes

NOTE: Use as reference only. Take what makes sense, change or discuss what's not. 

# Core ideas

* **One DB per source** → portable.
* **Batches** = versions (git commit / uuid7).
* **VDBs** = named vector indexes (per batch + embedding model).
* **Symbols** (function/class/module) → **Chunks** (embed text) → **FAISS IDs** (1:1 with `chunks.id`).
* **FTS5** mirrors `chunks` for keyword search.
* **Edges** store lightweight relationships for “impact subgraphs.”

---

# DDL (paste into `sqlite3 db.db`)

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Schema versioning
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version','1');

-- Batches: one per ingest run (git commit SHA or uuid7)
CREATE TABLE IF NOT EXISTS batches (
  id            TEXT PRIMARY KEY,          -- e.g., git commit sha / uuid7
  ref           TEXT,                      -- branch/tag/description
  generated_at  TEXT NOT NULL,             -- ISO8601 UTC
  notes         TEXT
);

-- Embedding models config (dim drives FAISS index type)
CREATE TABLE IF NOT EXISTS embedding_models (
  id           INTEGER PRIMARY KEY,
  provider     TEXT NOT NULL,              -- openai | local
  name         TEXT NOT NULL,              -- e.g., text-embedding-3-small | bge-m3
  dim          INTEGER NOT NULL,
  UNIQUE(provider, name)
);

-- VDBs: named vector indexes (points to FAISS files)
CREATE TABLE IF NOT EXISTS vdbs (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,         -- human handle, e.g., "main-openai"
  batch_id          TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  embedding_model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE RESTRICT,
  faiss_path        TEXT NOT NULL,         -- file path to FAISS index
  created_at        TEXT NOT NULL,
  UNIQUE(name)
);

CREATE INDEX IF NOT EXISTS idx_vdbs_batch ON vdbs(batch_id);
CREATE INDEX IF NOT EXISTS idx_vdbs_model ON vdbs(embedding_model_id);

-- Files (versioned by batch)
CREATE TABLE IF NOT EXISTS files (
  id          INTEGER PRIMARY KEY,
  batch_id    TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  repo_path   TEXT NOT NULL,               -- normalized relative path
  lang        TEXT NOT NULL,               -- python|ts|js|css|html|md|...
  file_sha    TEXT NOT NULL,               -- content sha256 OR git blob sha
  mtime_ns    INTEGER,
  size_bytes  INTEGER,
  UNIQUE(batch_id, repo_path, file_sha)
);

CREATE INDEX IF NOT EXISTS idx_files_batch_path ON files(batch_id, repo_path);
CREATE INDEX IF NOT EXISTS idx_files_lang ON files(lang);

-- Symbols (one row per module/class/function/method/etc.)
CREATE TABLE IF NOT EXISTS symbols (
  id               INTEGER PRIMARY KEY,
  file_id          INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,          -- module|class|function|method|css_rule|ts_func|...
  symbol_path      TEXT NOT NULL,          -- e.g. app.api.users:UserService.create
  start_line       INTEGER NOT NULL,
  end_line         INTEGER NOT NULL,
  symbol_sha       TEXT NOT NULL,          -- sha256(code slice)
  symbol_norm_sha  TEXT,                   -- optional normalized sha
  args_json        TEXT,                   -- JSON (list/dicts)
  returns_json     TEXT,
  imports_json     TEXT,
  deps_out_json    TEXT,                   -- coarse outbound deps (names/paths)
  docstring        TEXT,
  summary          TEXT,                   -- AI-enriched short description
  tokens           INTEGER,                -- token count for body_text
  first_seen_batch TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
  last_seen_batch  TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_symbols_span
  ON symbols(file_id, start_line, end_line, symbol_sha);
CREATE INDEX IF NOT EXISTS idx_symbols_symbol_path ON symbols(symbol_path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);

-- Chunks: text fed to embeddings; typically 1:1 with symbol
CREATE TABLE IF NOT EXISTS chunks (
  id          INTEGER PRIMARY KEY,         -- also your FAISS vector ID
  symbol_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  vdb_id      INTEGER NOT NULL REFERENCES vdbs(id) ON DELETE CASCADE, -- allows per-vdb variants
  header_md   TEXT NOT NULL,               -- "path :: Class.method (L45–128)"
  body_text   TEXT NOT NULL,               -- docstring + code (or just code)
  token_count INTEGER NOT NULL,
  UNIQUE(symbol_id, vdb_id)                -- one chunk per symbol per vdb
);

CREATE INDEX IF NOT EXISTS idx_chunks_vdb ON chunks(vdb_id);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(symbol_id);

-- FTS5 view over chunks for keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  header_md, body_text, content='chunks', content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunk_fts(rowid, header_md, body_text)
  VALUES (new.id, new.header_md, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, header_md, body_text)
  VALUES('delete', old.id, old.header_md, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, header_md, body_text)
  VALUES('delete', old.id, old.header_md, old.body_text);
  INSERT INTO chunk_fts(rowid, header_md, body_text)
  VALUES (new.id, new.header_md, new.body_text);
END;

-- Optional: edges for impact/neighbor graphs (imports, same-file, co-change, test-links)
CREATE TABLE IF NOT EXISTS edges (
  src_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  dst_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,              -- import|sibling|cochange|test|doc|route|calls? (approx)
  weight       REAL DEFAULT 1.0,
  PRIMARY KEY (src_chunk_id, dst_chunk_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_chunk_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_chunk_id);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);

-- Optional: mapping table if you need to list which chunk IDs exist in a VDB (FAISS side is file-based)
CREATE TABLE IF NOT EXISTS vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  vdb_id   INTEGER NOT NULL REFERENCES vdbs(id) ON DELETE CASCADE,
  dim      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_vdb ON vectors(vdb_id);
```

---

# Sensible defaults & tuning

Run once per connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-80000;         -- ~80MB page cache
-- Optional if OS allows:
PRAGMA mmap_size=30000000000;
```

---

# Common queries (copy-paste)

**1) Hybrid search: FTS + semantic (FAISS) merge by `vdb`**

```sql
-- FTS top-N for lexical recall (fast)
SELECT c.id, c.header_md, c.body_text
FROM chunk_fts f
JOIN chunks c ON c.id = f.rowid
WHERE c.vdb_id = :vdb_id AND f MATCH :query
LIMIT 20;
```

(Semantics: run FAISS separately with the same `vdb_id`, then merge/rerank in Python.)

**2) Join a FAISS hit back to metadata**

```sql
SELECT
  c.id, c.header_md, c.token_count,
  s.symbol_path, s.kind, s.start_line, s.end_line,
  f.repo_path, f.lang
FROM chunks c
JOIN symbols s ON s.id = c.symbol_id
JOIN files   f ON f.id = s.file_id
WHERE c.id IN (:id1, :id2, :id3);
```

**3) Expand neighbors for impact graph**

```sql
SELECT e.dst_chunk_id, e.kind, e.weight,
       c.header_md, s.symbol_path, f.repo_path
FROM edges e
JOIN chunks c ON c.id = e.dst_chunk_id
JOIN symbols s ON s.id = c.symbol_id
JOIN files   f ON f.id = s.file_id
WHERE e.src_chunk_id IN (:seed1, :seed2)
ORDER BY e.weight DESC
LIMIT 50;
```

**4) Diff between two batches (same source)**

```sql
-- show symbols whose code slice changed between two batches
WITH a AS (
  SELECT s.symbol_path, s.symbol_sha
  FROM symbols s JOIN files f ON f.id = s.file_id
  WHERE f.batch_id = :batchA
),
b AS (
  SELECT s.symbol_path, s.symbol_sha
  FROM symbols s JOIN files f ON f.id = s.file_id
  WHERE f.batch_id = :batchB
)
SELECT COALESCE(a.symbol_path, b.symbol_path) AS symbol_path,
       a.symbol_sha AS sha_a, b.symbol_sha AS sha_b
FROM a FULL OUTER JOIN b USING(symbol_path); -- emulate via UNION/LEFT JOINs in SQLite
```

*(SQLite doesn’t have FULL OUTER JOIN; implement with two LEFT JOINs + UNION ALL.)*

---

# ID & FAISS strategy

* `chunks.id` is the **canonical** integer ID → use it as FAISS vector ID (`add_with_ids`).
* One FAISS file per `vdb` (e.g., `faiss/main-openai.index`), store its path in `vdbs.faiss_path`.
* Re-embedding: `remove_ids([chunk_id])` then `add_with_ids([chunk_id])`.

---

# Write patterns (fast + safe)

* Batch all inserts/updates inside a **single transaction**.
* Use a **file lock** when writing FAISS and SQLite together.
* For atomic upgrades: write to `db.sql.tmp` and `faiss.tmp`, then rename both.

---

# Why this layout works

* **Fast recall**: FAISS for semantics, FTS5 for exact/keyword; you combine both.
* **Explainability**: joins to files/symbols show path + lines + kind.
* **Version-aware**: `batches` and `vdbs` give you multi-version, multi-model.
* **Impact graphs**: `edges` let you render subgraphs (imports, co-change, tests).
* **Low ops**: one DB file + FAISS file(s), easy to ship and back up.

