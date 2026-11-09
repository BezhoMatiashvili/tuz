-- Irreversible security migration. Run after a tested backup; 0002 removes raw
-- source HTML by design. Production executes this file as warehouse_migration_owner.

CREATE SCHEMA IF NOT EXISTS warehouse;

ALTER TABLE public.warehouse_sync_runs SET SCHEMA warehouse;
ALTER TABLE public.warehouse_products SET SCHEMA warehouse;
ALTER TABLE public.warehouse_product_versions SET SCHEMA warehouse;

ALTER TABLE warehouse.warehouse_products
    DROP COLUMN IF EXISTS source_row_html,
    ADD COLUMN IF NOT EXISTS integrity_tag CHAR(64);

ALTER TABLE warehouse.warehouse_product_versions
    DROP COLUMN IF EXISTS source_row_html,
    ADD COLUMN IF NOT EXISTS integrity_tag CHAR(64);

ALTER TABLE warehouse.warehouse_products
    ADD CONSTRAINT warehouse_products_integrity_tag_format
    CHECK (integrity_tag IS NULL OR integrity_tag ~ '^[0-9a-f]{64}$') NOT VALID;

ALTER TABLE warehouse.warehouse_product_versions
    ADD CONSTRAINT warehouse_product_versions_integrity_tag_format
    CHECK (integrity_tag IS NULL OR integrity_tag ~ '^[0-9a-f]{64}$') NOT VALID;

CREATE TABLE warehouse.warehouse_sync_seen_pages (
    run_id BIGINT NOT NULL REFERENCES warehouse.warehouse_sync_runs(run_id) ON DELETE CASCADE,
    page INTEGER NOT NULL CHECK (page > 0),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, page)
);

-- The migration preserves old observations, which cannot be signed without an
-- off-database HMAC key. New writes are required to have a valid tag by the
-- security-definer function below. Sign old rows before validating these checks.

CREATE OR REPLACE FUNCTION warehouse.start_sync_run(p_expected_pages INTEGER)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
DECLARE
    v_run_id BIGINT;
BEGIN
    IF p_expected_pages IS NULL OR p_expected_pages < 1 THEN
        RAISE EXCEPTION 'invalid expected page count' USING ERRCODE = '22023';
    END IF;
    INSERT INTO warehouse.warehouse_sync_runs (status, expected_pages)
    VALUES ('running', p_expected_pages)
    RETURNING run_id INTO v_run_id;
    RETURN v_run_id;
END;
$$;

CREATE OR REPLACE FUNCTION warehouse.record_product(
    p_run_id BIGINT,
    p_source_page INTEGER,
    p_source_product_id TEXT,
    p_product_code TEXT,
    p_product_name TEXT,
    p_product_model TEXT,
    p_original_code TEXT,
    p_duplicate_code TEXT,
    p_brand TEXT,
    p_source_columns JSONB,
    p_row_hash CHAR(64),
    p_integrity_tag CHAR(64)
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
DECLARE
    v_previous_hash CHAR(64);
BEGIN
    IF p_run_id IS NULL
       OR p_source_page IS NULL OR p_source_page < 1
       OR p_source_product_id IS NULL OR btrim(p_source_product_id) = ''
       OR jsonb_typeof(p_source_columns) <> 'object'
       OR p_row_hash IS NULL OR p_row_hash !~ '^[0-9a-f]{64}$'
       OR p_integrity_tag IS NULL OR p_integrity_tag !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid warehouse product payload' USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM warehouse.warehouse_sync_seen_pages
    WHERE run_id = p_run_id AND page = p_source_page;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source page has not been recorded' USING ERRCODE = '55000';
    END IF;

    PERFORM 1
    FROM warehouse.warehouse_sync_runs
    WHERE run_id = p_run_id AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'sync run is not writable' USING ERRCODE = '55000';
    END IF;

    SELECT row_hash INTO v_previous_hash
    FROM warehouse.warehouse_products
    WHERE source_product_id = p_source_product_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO warehouse.warehouse_products (
            source_product_id, product_code, product_name, product_model, original_code,
            duplicate_code, brand, source_columns, row_hash, integrity_tag, source_page,
            first_seen_run_id, last_seen_run_id
        ) VALUES (
            p_source_product_id, p_product_code, p_product_name, p_product_model, p_original_code,
            p_duplicate_code, p_brand, p_source_columns, p_row_hash, p_integrity_tag, p_source_page,
            p_run_id, p_run_id
        );
        INSERT INTO warehouse.warehouse_product_versions (
            source_product_id, observed_run_id, source_page, source_columns, row_hash, integrity_tag
        ) VALUES (
            p_source_product_id, p_run_id, p_source_page, p_source_columns, p_row_hash, p_integrity_tag
        );
        RETURN 'added';
    END IF;

    IF v_previous_hash IS DISTINCT FROM p_row_hash THEN
        UPDATE warehouse.warehouse_products
        SET product_code = p_product_code,
            product_name = p_product_name,
            product_model = p_product_model,
            original_code = p_original_code,
            duplicate_code = p_duplicate_code,
            brand = p_brand,
            source_columns = p_source_columns,
            row_hash = p_row_hash,
            integrity_tag = p_integrity_tag,
            source_page = p_source_page,
            is_active = TRUE,
            last_observed_at = now(),
            last_changed_at = now(),
            last_seen_run_id = p_run_id
        WHERE source_product_id = p_source_product_id;
        INSERT INTO warehouse.warehouse_product_versions (
            source_product_id, observed_run_id, source_page, source_columns, row_hash, integrity_tag
        ) VALUES (
            p_source_product_id, p_run_id, p_source_page, p_source_columns, p_row_hash, p_integrity_tag
        );
        RETURN 'changed';
    END IF;

    UPDATE warehouse.warehouse_products
    SET last_observed_at = now(),
        last_seen_run_id = p_run_id,
        source_page = p_source_page,
        integrity_tag = p_integrity_tag,
        is_active = TRUE
    WHERE source_product_id = p_source_product_id;
    RETURN 'unchanged';
END;
$$;

CREATE OR REPLACE FUNCTION warehouse.record_page_seen(
    p_run_id BIGINT,
    p_page INTEGER
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
DECLARE
    v_expected_pages INTEGER;
BEGIN
    SELECT expected_pages INTO v_expected_pages
    FROM warehouse.warehouse_sync_runs
    WHERE run_id = p_run_id AND status = 'running'
    FOR UPDATE;
    IF NOT FOUND OR p_page IS NULL OR p_page < 1 OR p_page > v_expected_pages THEN
        RAISE EXCEPTION 'source page is not writable' USING ERRCODE = '55000';
    END IF;
    INSERT INTO warehouse.warehouse_sync_seen_pages (run_id, page)
    VALUES (p_run_id, p_page)
    ON CONFLICT (run_id, page) DO NOTHING;
END;
$$;

CREATE OR REPLACE FUNCTION warehouse.finish_sync_run(
    p_run_id BIGINT,
    p_complete BOOLEAN,
    p_pages_fetched INTEGER,
    p_rows_seen INTEGER,
    p_rows_added INTEGER,
    p_rows_changed INTEGER,
    p_rows_unchanged INTEGER,
    p_rows_deactivated INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
DECLARE
    v_deactivated INTEGER := 0;
    v_expected_pages INTEGER;
    v_seen_pages INTEGER;
BEGIN
    SELECT expected_pages INTO v_expected_pages FROM warehouse.warehouse_sync_runs
    WHERE run_id = p_run_id AND status = 'running'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'sync run is not finishable' USING ERRCODE = '55000';
    END IF;
    IF p_complete THEN
        SELECT count(*) INTO v_seen_pages
        FROM warehouse.warehouse_sync_seen_pages
        WHERE run_id = p_run_id;
        IF p_pages_fetched <> v_expected_pages OR v_seen_pages <> v_expected_pages THEN
            RAISE EXCEPTION 'full run has unobserved pages' USING ERRCODE = '55000';
        END IF;
        UPDATE warehouse.warehouse_products
        SET is_active = FALSE, last_observed_at = now()
        WHERE is_active = TRUE AND last_seen_run_id <> p_run_id;
        GET DIAGNOSTICS v_deactivated = ROW_COUNT;
    END IF;
    UPDATE warehouse.warehouse_sync_runs
    SET status = CASE WHEN p_complete THEN 'succeeded' ELSE 'partial' END,
        pages_fetched = GREATEST(p_pages_fetched, 0),
        rows_seen = GREATEST(p_rows_seen, 0),
        rows_added = GREATEST(p_rows_added, 0),
        rows_changed = GREATEST(p_rows_changed, 0),
        rows_unchanged = GREATEST(p_rows_unchanged, 0),
        rows_deactivated = CASE WHEN p_complete THEN v_deactivated ELSE GREATEST(p_rows_deactivated, 0) END,
        finished_at = now()
    WHERE run_id = p_run_id;
    RETURN v_deactivated;
END;
$$;

CREATE OR REPLACE FUNCTION warehouse.fail_sync_run(
    p_run_id BIGINT,
    p_pages_fetched INTEGER,
    p_rows_seen INTEGER,
    p_rows_added INTEGER,
    p_rows_changed INTEGER,
    p_rows_unchanged INTEGER,
    p_failure_code TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
BEGIN
    IF p_failure_code IS NULL OR p_failure_code !~ '^[a-z_]{3,64}$' THEN
        RAISE EXCEPTION 'invalid failure code' USING ERRCODE = '22023';
    END IF;
    UPDATE warehouse.warehouse_sync_runs
    SET status = 'failed',
        pages_fetched = GREATEST(p_pages_fetched, 0),
        rows_seen = GREATEST(p_rows_seen, 0),
        rows_added = GREATEST(p_rows_added, 0),
        rows_changed = GREATEST(p_rows_changed, 0),
        rows_unchanged = GREATEST(p_rows_unchanged, 0),
        failure_reason = p_failure_code,
        finished_at = now()
    WHERE run_id = p_run_id AND status = 'running';
END;
$$;

CREATE OR REPLACE FUNCTION warehouse.prune_product_versions()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, warehouse, pg_temp
AS $$
DECLARE
    v_deleted INTEGER;
BEGIN
    WITH expired AS (
        SELECT version_id
        FROM warehouse.warehouse_product_versions
        WHERE observed_at < now() - INTERVAL '24 months'
        ORDER BY version_id
        LIMIT 10000
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM warehouse.warehouse_product_versions AS v
        USING expired
        WHERE v.version_id = expired.version_id
        RETURNING 1
    )
    SELECT count(*) INTO v_deleted FROM deleted;
    RETURN v_deleted;
END;
$$;

REVOKE ALL ON SCHEMA warehouse FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA warehouse FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA warehouse FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA warehouse FROM PUBLIC;
REVOKE ALL ON TABLE public.warehouse_schema_migrations FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA warehouse REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA warehouse REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA warehouse REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_sync_writer') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA warehouse TO warehouse_sync_writer';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.start_sync_run(INTEGER) TO warehouse_sync_writer';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.record_page_seen(BIGINT, INTEGER) TO warehouse_sync_writer';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.record_product(BIGINT, INTEGER, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, CHAR(64), CHAR(64)) TO warehouse_sync_writer';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.finish_sync_run(BIGINT, BOOLEAN, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER) TO warehouse_sync_writer';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.fail_sync_run(BIGINT, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, TEXT) TO warehouse_sync_writer';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_backup_restore_operator') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA warehouse TO warehouse_backup_restore_operator';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA warehouse TO warehouse_backup_restore_operator';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA warehouse TO warehouse_backup_restore_operator';
        EXECUTE 'GRANT EXECUTE ON FUNCTION warehouse.prune_product_versions() TO warehouse_backup_restore_operator';
    END IF;
END;
$$;

COMMENT ON SCHEMA warehouse IS 'Private synchronisation data. Do not add this schema to Supabase Data API exposed schemas.';
COMMENT ON FUNCTION warehouse.prune_product_versions() IS 'Deletes at most 10,000 versions older than 24 months per call.';
