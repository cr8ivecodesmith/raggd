-- Update the last_seen batch marker for a given chunk within a file.
UPDATE chunk_slices
SET
    last_seen_batch = :last_seen_batch,
    updated_at = :updated_at
WHERE chunk_id = :chunk_id
  AND file_id = :file_id
  AND last_seen_batch <> :last_seen_batch;
