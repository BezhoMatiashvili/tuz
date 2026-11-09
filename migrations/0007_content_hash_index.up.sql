-- Code-index freshness that ignores the hourly stock columns.
-- warehouse_products.row_hash covers every source column, so each stock change made an
-- indexed product look stale. content_hash() hashes the descriptive columns only
-- (stock columns 10-12 removed); row_hash_seen remembers which row_hash was last checked.
CREATE FUNCTION automotive.content_hash(columns JSONB) RETURNS CHAR(64)
LANGUAGE sql IMMUTABLE PARALLEL SAFE COST 200
AS $$
    SELECT encode(sha256(convert_to(
        (columns - 'column_10' - 'column_11' - 'column_12')::text, 'UTF8')), 'hex')::char(64)
$$;
ALTER TABLE automotive.catalog_index_state ADD COLUMN row_hash_seen CHAR(64);
REVOKE ALL ON FUNCTION automotive.content_hash(JSONB) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT EXECUTE ON FUNCTION automotive.content_hash(JSONB) TO automotive_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_enrichment') THEN
        GRANT EXECUTE ON FUNCTION automotive.content_hash(JSONB) TO automotive_enrichment;
    END IF;
END $$;
