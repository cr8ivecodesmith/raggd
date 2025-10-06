-- Remove all chunk slices generated for a specific batch identifier.
DELETE FROM chunk_slices
WHERE batch_id = :batch_id;
