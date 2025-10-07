-- Retrieve all chunk slices for a file regardless of last-seen batch.
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
WHERE file_id = :file_id
ORDER BY chunk_id ASC, last_seen_batch DESC, part_index ASC;
