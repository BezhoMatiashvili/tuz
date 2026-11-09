-- Inventory analytics: facts are observations, not sales transactions.
-- The migration creates structure and incremental triggers only. Run the
-- owner-only analytics.refresh_all() separately for the initial backfill.
CREATE SCHEMA analytics;
REVOKE ALL ON SCHEMA analytics FROM PUBLIC;

CREATE TABLE analytics.dim_date (
    date_key date PRIMARY KEY,
    calendar_year smallint NOT NULL,
    calendar_quarter smallint NOT NULL,
    calendar_month smallint NOT NULL,
    day_of_month smallint NOT NULL,
    iso_day_of_week smallint NOT NULL
);
COMMENT ON TABLE analytics.dim_date IS 'UTC calendar dates associated with inventory observations and sync runs.';

CREATE TABLE analytics.dim_product (
    product_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_product_id text NOT NULL UNIQUE,
    product_code text NOT NULL,
    product_name text NOT NULL,
    product_model text NOT NULL,
    original_code text NOT NULL,
    duplicate_code text NOT NULL,
    brand text NOT NULL,
    is_active boolean NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    last_changed_at timestamptz NOT NULL
);
COMMENT ON TABLE analytics.dim_product IS 'Type 1 product dimension: attributes reflect the latest known product, including on historical observations.';

CREATE TABLE analytics.fact_product_current (
    product_key bigint PRIMARY KEY REFERENCES analytics.dim_product(product_key),
    observed_date date NOT NULL REFERENCES analytics.dim_date(date_key),
    last_seen_run_id bigint NOT NULL,
    source_page integer NOT NULL,
    is_active boolean NOT NULL,
    selling_price_raw text,
    selling_price numeric
);
COMMENT ON TABLE analytics.fact_product_current IS 'One current state per product; price is a source listing price, not a sale or revenue.';
COMMENT ON COLUMN analytics.fact_product_current.selling_price IS 'Numeric interpretation of source_columns column_06; source screenshot indicates selling price. NULL when absent or not a simple number. Currency is unknown.';
COMMENT ON COLUMN analytics.fact_product_current.selling_price_raw IS 'Unmodified source column_06, retained for validation and auditing.';

CREATE TABLE analytics.fact_product_observation (
    source_version_id bigint PRIMARY KEY,
    product_key bigint NOT NULL REFERENCES analytics.dim_product(product_key),
    observed_date date NOT NULL REFERENCES analytics.dim_date(date_key),
    observed_at timestamptz NOT NULL,
    observed_run_id bigint NOT NULL,
    source_page integer NOT NULL,
    selling_price_raw text,
    selling_price numeric
);
CREATE INDEX fact_product_observation_product_date_idx
    ON analytics.fact_product_observation (product_key, observed_at);
CREATE INDEX fact_product_observation_date_idx ON analytics.fact_product_observation (observed_date);
COMMENT ON TABLE analytics.fact_product_observation IS 'One row per retained or subsequently captured product version: only new or changed products create observations, not every sync.';
COMMENT ON COLUMN analytics.fact_product_observation.source_version_id IS 'Source version identifier; the fact is removed if the corresponding source version is pruned.';
COMMENT ON COLUMN analytics.fact_product_observation.selling_price IS 'Numeric interpretation of source_columns column_06; currency is unknown.';
COMMENT ON COLUMN analytics.fact_product_observation.selling_price_raw IS 'Unmodified source column_06, retained for validation and auditing.';

CREATE TABLE analytics.fact_sync_run (
    run_id bigint PRIMARY KEY,
    started_date date NOT NULL REFERENCES analytics.dim_date(date_key),
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    expected_pages integer,
    pages_fetched integer NOT NULL,
    rows_seen integer NOT NULL,
    rows_added integer NOT NULL,
    rows_changed integer NOT NULL,
    rows_unchanged integer NOT NULL,
    rows_deactivated integer NOT NULL,
    failure_reason text
);
COMMENT ON TABLE analytics.fact_sync_run IS 'One row per sync run, updated as the run progresses and finishes.';

-- Do not guess how to interpret grouping separators, currency symbols, or
-- non-numeric values. Keep raw text regardless of conversion success.
CREATE FUNCTION analytics.parse_price(p_value text) RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT CASE
        WHEN length(btrim(p_value)) <= 30
         AND btrim(p_value) ~ '^[+-]?[0-9]+([.][0-9]+)?$'
        THEN btrim(p_value)::numeric
        ELSE NULL
    END
$$;

CREATE FUNCTION analytics.ensure_date(p_date date) RETURNS void
LANGUAGE sql
SET search_path = pg_catalog, analytics
AS $$
    INSERT INTO analytics.dim_date
        (date_key, calendar_year, calendar_quarter, calendar_month, day_of_month, iso_day_of_week)
    VALUES (p_date, extract(year FROM p_date)::smallint, extract(quarter FROM p_date)::smallint,
        extract(month FROM p_date)::smallint, extract(day FROM p_date)::smallint,
        extract(isodow FROM p_date)::smallint)
    ON CONFLICT (date_key) DO NOTHING
$$;

CREATE FUNCTION analytics.sync_product(p warehouse.warehouse_products) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, analytics
AS $$
DECLARE
    v_product_key bigint;
    v_date date := (p.last_observed_at AT TIME ZONE 'UTC')::date;
    v_raw_price text := p.source_columns ->> 'column_06';
BEGIN
    PERFORM analytics.ensure_date(v_date);
    INSERT INTO analytics.dim_product
        (source_product_id, product_code, product_name, product_model, original_code,
         duplicate_code, brand, is_active, first_seen_at, last_observed_at, last_changed_at)
    VALUES (p.source_product_id, p.product_code, p.product_name, p.product_model, p.original_code,
         p.duplicate_code, p.brand, p.is_active, p.first_seen_at, p.last_observed_at, p.last_changed_at)
    ON CONFLICT (source_product_id) DO UPDATE SET
        product_code = EXCLUDED.product_code, product_name = EXCLUDED.product_name,
        product_model = EXCLUDED.product_model, original_code = EXCLUDED.original_code,
        duplicate_code = EXCLUDED.duplicate_code, brand = EXCLUDED.brand,
        is_active = EXCLUDED.is_active, first_seen_at = EXCLUDED.first_seen_at,
        last_observed_at = EXCLUDED.last_observed_at, last_changed_at = EXCLUDED.last_changed_at
    RETURNING product_key INTO v_product_key;

    INSERT INTO analytics.fact_product_current
        (product_key, observed_date, last_seen_run_id, source_page, is_active, selling_price_raw, selling_price)
    VALUES (v_product_key, v_date, p.last_seen_run_id, p.source_page, p.is_active,
        v_raw_price, analytics.parse_price(v_raw_price))
    ON CONFLICT (product_key) DO UPDATE SET
        observed_date = EXCLUDED.observed_date, last_seen_run_id = EXCLUDED.last_seen_run_id,
        source_page = EXCLUDED.source_page, is_active = EXCLUDED.is_active,
        selling_price_raw = EXCLUDED.selling_price_raw, selling_price = EXCLUDED.selling_price;
END;
$$;

CREATE FUNCTION analytics.sync_observation(v warehouse.warehouse_product_versions) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, analytics
AS $$
DECLARE
    v_product_key bigint;
    v_date date := (v.observed_at AT TIME ZONE 'UTC')::date;
    v_raw_price text := v.source_columns ->> 'column_06';
    p warehouse.warehouse_products%ROWTYPE;
BEGIN
    SELECT product_key INTO v_product_key FROM analytics.dim_product
    WHERE source_product_id = v.source_product_id;
    IF v_product_key IS NULL THEN
        SELECT * INTO STRICT p FROM warehouse.warehouse_products
        WHERE source_product_id = v.source_product_id;
        PERFORM analytics.sync_product(p);
        SELECT product_key INTO STRICT v_product_key FROM analytics.dim_product
        WHERE source_product_id = v.source_product_id;
    END IF;
    PERFORM analytics.ensure_date(v_date);
    INSERT INTO analytics.fact_product_observation
        (source_version_id, product_key, observed_date, observed_at, observed_run_id,
         source_page, selling_price_raw, selling_price)
    VALUES (v.version_id, v_product_key, v_date, v.observed_at, v.observed_run_id,
        v.source_page, v_raw_price, analytics.parse_price(v_raw_price))
    ON CONFLICT (source_version_id) DO UPDATE SET
        product_key = EXCLUDED.product_key, observed_date = EXCLUDED.observed_date,
        observed_at = EXCLUDED.observed_at, observed_run_id = EXCLUDED.observed_run_id,
        source_page = EXCLUDED.source_page, selling_price_raw = EXCLUDED.selling_price_raw,
        selling_price = EXCLUDED.selling_price;
END;
$$;

CREATE FUNCTION analytics.sync_run(r warehouse.warehouse_sync_runs) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, analytics
AS $$
DECLARE
    v_date date := (r.started_at AT TIME ZONE 'UTC')::date;
BEGIN
    PERFORM analytics.ensure_date(v_date);
    INSERT INTO analytics.fact_sync_run
        (run_id, started_date, status, started_at, finished_at, expected_pages, pages_fetched,
         rows_seen, rows_added, rows_changed, rows_unchanged, rows_deactivated, failure_reason)
    VALUES (r.run_id, v_date, r.status, r.started_at, r.finished_at, r.expected_pages, r.pages_fetched,
        r.rows_seen, r.rows_added, r.rows_changed, r.rows_unchanged, r.rows_deactivated, r.failure_reason)
    ON CONFLICT (run_id) DO UPDATE SET
        started_date = EXCLUDED.started_date, status = EXCLUDED.status,
        started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
        expected_pages = EXCLUDED.expected_pages, pages_fetched = EXCLUDED.pages_fetched,
        rows_seen = EXCLUDED.rows_seen, rows_added = EXCLUDED.rows_added,
        rows_changed = EXCLUDED.rows_changed, rows_unchanged = EXCLUDED.rows_unchanged,
        rows_deactivated = EXCLUDED.rows_deactivated, failure_reason = EXCLUDED.failure_reason;
END;
$$;

CREATE FUNCTION analytics.on_product_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, analytics
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM analytics.fact_product_current WHERE product_key =
            (SELECT product_key FROM analytics.dim_product WHERE source_product_id = OLD.source_product_id);
        UPDATE analytics.dim_product SET is_active = false WHERE source_product_id = OLD.source_product_id;
        RETURN OLD;
    END IF;
    PERFORM analytics.sync_product(NEW);
    RETURN NEW;
END;
$$;

CREATE FUNCTION analytics.on_observation_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, analytics
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM analytics.fact_product_observation WHERE source_version_id = OLD.version_id;
        RETURN OLD;
    END IF;
    PERFORM analytics.sync_observation(NEW);
    RETURN NEW;
END;
$$;

CREATE FUNCTION analytics.on_run_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, analytics
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM analytics.fact_sync_run WHERE run_id = OLD.run_id;
        RETURN OLD;
    END IF;
    PERFORM analytics.sync_run(NEW);
    RETURN NEW;
END;
$$;

CREATE TRIGGER analytics_product_change AFTER INSERT OR UPDATE OR DELETE
ON warehouse.warehouse_products FOR EACH ROW EXECUTE FUNCTION analytics.on_product_change();
CREATE TRIGGER analytics_observation_change AFTER INSERT OR UPDATE OR DELETE
ON warehouse.warehouse_product_versions FOR EACH ROW EXECUTE FUNCTION analytics.on_observation_change();
CREATE TRIGGER analytics_run_change AFTER INSERT OR UPDATE OR DELETE
ON warehouse.warehouse_sync_runs FOR EACH ROW EXECUTE FUNCTION analytics.on_run_change();

-- An explicit owner-only backfill. Locks ensure a consistent cutover while
-- source triggers and this idempotent refill cannot race to overwrite state.
CREATE PROCEDURE analytics.refresh_all()
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, analytics
AS $$
DECLARE
    p warehouse.warehouse_products%ROWTYPE;
    v warehouse.warehouse_product_versions%ROWTYPE;
    r warehouse.warehouse_sync_runs%ROWTYPE;
BEGIN
    LOCK TABLE warehouse.warehouse_sync_runs, warehouse.warehouse_products,
        warehouse.warehouse_product_versions IN SHARE ROW EXCLUSIVE MODE;
    DELETE FROM analytics.fact_product_observation f WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_product_versions v WHERE v.version_id = f.source_version_id);
    DELETE FROM analytics.fact_product_current f WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_products p JOIN analytics.dim_product d
         ON d.source_product_id = p.source_product_id WHERE d.product_key = f.product_key);
    DELETE FROM analytics.fact_sync_run f WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_sync_runs r WHERE r.run_id = f.run_id);
    UPDATE analytics.dim_product d SET is_active = false WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_products p WHERE p.source_product_id = d.source_product_id);
    FOR p IN SELECT * FROM warehouse.warehouse_products LOOP
        PERFORM analytics.sync_product(p);
    END LOOP;
    FOR v IN SELECT * FROM warehouse.warehouse_product_versions LOOP
        PERFORM analytics.sync_observation(v);
    END LOOP;
    FOR r IN SELECT * FROM warehouse.warehouse_sync_runs LOOP
        PERFORM analytics.sync_run(r);
    END LOOP;
END;
$$;
COMMENT ON PROCEDURE analytics.refresh_all() IS 'Owner-only idempotent initial backfill and refill. Blocks source writes until completion.';

REVOKE ALL ON ALL TABLES IN SCHEMA analytics FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA analytics FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA analytics FROM PUBLIC;
REVOKE EXECUTE ON ALL PROCEDURES IN SCHEMA analytics FROM PUBLIC;
