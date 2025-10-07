-- Retrieve chunk slices for a given chunk identifier ordered by part index.
SELECT
    id,
    batch_id,
    file_id,
    symbol_id,
    parent_symbol_id,
    chunk_id,
    handler_name,
    handler_version,
    part_index,
    part_total,
    start_line,
    end_line,
    start_byte,
    end_byte,
    token_count,
    content_hash,
    content_norm_hash,
    content_text,
    overflow_is_truncated,
    overflow_reason,
    metadata_json,
    created_at,
    updated_at,
    first_seen_batch,
    last_seen_batch
FROM chunk_slices
WHERE last_seen_batch = :batch_id
  AND chunk_id = :chunk_id
ORDER BY part_index ASC;
