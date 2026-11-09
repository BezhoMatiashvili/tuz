-- Removes only the content-hash helper; indexed codes are rebuilt by the worker.
ALTER TABLE automotive.catalog_index_state DROP COLUMN IF EXISTS row_hash_seen;
DROP FUNCTION IF EXISTS automotive.content_hash(JSONB);
