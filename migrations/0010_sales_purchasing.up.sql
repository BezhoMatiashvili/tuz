-- Sales and purchases inferred from stock changes between sync observations.
-- Stock is the store (source column_10, titled "1") plus TUZ 11 (column_12);
-- TUZ 6 (column_11) has no agreed meaning and is ignored. Moving items between
-- the store and TUZ 11 does not change the total and is not recorded. A drop in
-- the total is recorded as a sale and a rise as a purchase; the source has no
-- transactions, so returns, write-offs and corrections look the same.
CREATE SCHEMA trade;
REVOKE ALL ON SCHEMA trade FROM PUBLIC;
COMMENT ON SCHEMA trade IS
    'Sales and purchases inferred from stock changes. Private; not a Data API schema.';

CREATE TABLE trade.baseline (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    run_id BIGINT NOT NULL,
    established_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE trade.baseline IS
    'The sync run whose products are starting stock. Products first seen in a later run count as purchases.';
COMMENT ON COLUMN trade.baseline.singleton IS 'Always true; keeps this table to a single row.';
COMMENT ON COLUMN trade.baseline.run_id IS
    'Products first seen in this run or earlier are starting stock, not purchases.';
COMMENT ON COLUMN trade.baseline.established_at IS 'When the baseline was recorded.';

CREATE TABLE trade.sales (
    sale_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_product_id TEXT NOT NULL,
    detected_run_id BIGINT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    store_before NUMERIC NOT NULL,
    store_after NUMERIC NOT NULL,
    tuz11_before NUMERIC NOT NULL,
    tuz11_after NUMERIC NOT NULL,
    unit_price_raw TEXT,
    unit_price NUMERIC,
    amount NUMERIC GENERATED ALWAYS AS (quantity * unit_price) STORED,
    CHECK (period_end >= period_start),
    CHECK (quantity = (store_before + tuz11_before) - (store_after + tuz11_after))
);
CREATE INDEX sales_period_end_idx ON trade.sales (period_end);
CREATE INDEX sales_product_period_idx ON trade.sales (source_product_id, period_end);
COMMENT ON TABLE trade.sales IS
    'One row per observed drop in a product''s store + TUZ 11 stock. Sales and restocks within one sync interval cancel out.';
COMMENT ON COLUMN trade.sales.sale_id IS 'Unique identifier of the inferred sale.';
COMMENT ON COLUMN trade.sales.source_product_id IS 'Product identifier in the source system.';
COMMENT ON COLUMN trade.sales.detected_run_id IS 'Sync run that observed the drop.';
COMMENT ON COLUMN trade.sales.period_start IS 'Previous observation of the product; the sale happened after this.';
COMMENT ON COLUMN trade.sales.period_end IS 'Observation that showed the drop; the sale happened before this.';
COMMENT ON COLUMN trade.sales.quantity IS 'Units sold: the drop in store + TUZ 11 stock.';
COMMENT ON COLUMN trade.sales.store_before IS 'Store quantity (source column "1", column_10) before the drop.';
COMMENT ON COLUMN trade.sales.store_after IS 'Store quantity (source column "1", column_10) after the drop.';
COMMENT ON COLUMN trade.sales.tuz11_before IS 'TUZ 11 quantity (column_12) before the drop.';
COMMENT ON COLUMN trade.sales.tuz11_after IS 'TUZ 11 quantity (column_12) after the drop.';
COMMENT ON COLUMN trade.sales.unit_price_raw IS 'Unmodified selling price (column_06) listed before the drop.';
COMMENT ON COLUMN trade.sales.unit_price IS
    'Numeric selling price; NULL when not a simple dot-decimal number. Currency is unknown.';
COMMENT ON COLUMN trade.sales.amount IS 'quantity * unit_price; NULL when the price is unknown.';

CREATE TABLE trade.purchases (
    purchase_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_product_id TEXT NOT NULL,
    detected_run_id BIGINT NOT NULL,
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    store_before NUMERIC NOT NULL,
    store_after NUMERIC NOT NULL,
    tuz11_before NUMERIC NOT NULL,
    tuz11_after NUMERIC NOT NULL,
    is_new_product BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (period_start IS NULL OR period_end >= period_start),
    CHECK ((period_start IS NULL) = is_new_product),
    CHECK (quantity = (store_after + tuz11_after) - (store_before + tuz11_before))
);
CREATE INDEX purchases_period_end_idx ON trade.purchases (period_end);
CREATE INDEX purchases_product_period_idx ON trade.purchases (source_product_id, period_end);
COMMENT ON TABLE trade.purchases IS
    'One row per observed rise in a product''s store + TUZ 11 stock, or a new product that arrived with stock. No purchase cost is available.';
COMMENT ON COLUMN trade.purchases.purchase_id IS 'Unique identifier of the inferred purchase.';
COMMENT ON COLUMN trade.purchases.source_product_id IS 'Product identifier in the source system.';
COMMENT ON COLUMN trade.purchases.detected_run_id IS 'Sync run that observed the rise.';
COMMENT ON COLUMN trade.purchases.period_start IS
    'Previous observation of the product; the purchase arrived after this. NULL for a new product.';
COMMENT ON COLUMN trade.purchases.period_end IS 'Observation that showed the rise; the purchase arrived before this.';
COMMENT ON COLUMN trade.purchases.quantity IS 'Units bought: the rise in store + TUZ 11 stock.';
COMMENT ON COLUMN trade.purchases.store_before IS 'Store quantity (source column "1", column_10) before the rise; 0 for a new product.';
COMMENT ON COLUMN trade.purchases.store_after IS 'Store quantity (source column "1", column_10) after the rise.';
COMMENT ON COLUMN trade.purchases.tuz11_before IS 'TUZ 11 quantity (column_12) before the rise; 0 for a new product.';
COMMENT ON COLUMN trade.purchases.tuz11_after IS 'TUZ 11 quantity (column_12) after the rise.';
COMMENT ON COLUMN trade.purchases.is_new_product IS 'True when the product first appeared after the baseline with stock.';

-- Same plain-number rule as automotive/stock.py, plus negative counts the source
-- reports for oversold items. Anything else is not a quantity.
CREATE FUNCTION trade.parse_quantity(p_value TEXT) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT CASE
        WHEN length(btrim(p_value)) <= 30
         AND btrim(p_value) ~ '^-?[0-9]+([.,][0-9]+)?$'
        THEN replace(btrim(p_value), ',', '.')::numeric
        ELSE NULL
    END
$$;

CREATE FUNCTION trade.stock_total(p_columns JSONB) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT trade.parse_quantity(p_columns ->> 'column_10')
         + trade.parse_quantity(p_columns ->> 'column_12')
$$;
COMMENT ON FUNCTION trade.stock_total(JSONB) IS
    'Store (column_10) plus TUZ 11 (column_12) stock; NULL when either is not a plain number.';

CREATE FUNCTION trade.on_stock_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, trade
AS $$
DECLARE
    v_store_before NUMERIC := trade.parse_quantity(OLD.source_columns ->> 'column_10');
    v_store_after NUMERIC := trade.parse_quantity(NEW.source_columns ->> 'column_10');
    v_tuz11_before NUMERIC := trade.parse_quantity(OLD.source_columns ->> 'column_12');
    v_tuz11_after NUMERIC := trade.parse_quantity(NEW.source_columns ->> 'column_12');
    v_change NUMERIC := (v_store_after + v_tuz11_after) - (v_store_before + v_tuz11_before);
    v_price_raw TEXT := OLD.source_columns ->> 'column_06';
BEGIN
    IF v_change IS NULL OR v_change = 0 THEN
        RETURN NULL;
    END IF;
    IF v_change < 0 THEN
        INSERT INTO trade.sales
            (source_product_id, detected_run_id, period_start, period_end, quantity,
             store_before, store_after, tuz11_before, tuz11_after, unit_price_raw, unit_price)
        VALUES (NEW.source_product_id, NEW.last_seen_run_id, OLD.last_observed_at,
            NEW.last_observed_at, -v_change, v_store_before, v_store_after, v_tuz11_before,
            v_tuz11_after, v_price_raw, analytics.parse_price(v_price_raw));
    ELSE
        INSERT INTO trade.purchases
            (source_product_id, detected_run_id, period_start, period_end, quantity,
             store_before, store_after, tuz11_before, tuz11_after)
        VALUES (NEW.source_product_id, NEW.last_seen_run_id, OLD.last_observed_at,
            NEW.last_observed_at, v_change, v_store_before, v_store_after, v_tuz11_before,
            v_tuz11_after);
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION trade.on_new_product() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, trade
AS $$
DECLARE
    v_store NUMERIC := trade.parse_quantity(NEW.source_columns ->> 'column_10');
    v_tuz11 NUMERIC := trade.parse_quantity(NEW.source_columns ->> 'column_12');
BEGIN
    IF v_store + v_tuz11 > 0 AND EXISTS (
        SELECT 1 FROM trade.baseline b WHERE NEW.first_seen_run_id > b.run_id
    ) THEN
        INSERT INTO trade.purchases
            (source_product_id, detected_run_id, period_start, period_end, quantity,
             store_before, store_after, tuz11_before, tuz11_after, is_new_product)
        VALUES (NEW.source_product_id, NEW.first_seen_run_id, NULL, NEW.last_observed_at,
            v_store + v_tuz11, 0, v_store, 0, v_tuz11, TRUE);
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION trade.on_run_finished() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, trade
AS $$
BEGIN
    INSERT INTO trade.baseline (run_id) VALUES (NEW.run_id)
    ON CONFLICT (singleton) DO NOTHING;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trade_stock_change
AFTER UPDATE OF source_columns ON warehouse.warehouse_products
FOR EACH ROW WHEN (OLD.source_columns IS DISTINCT FROM NEW.source_columns)
EXECUTE FUNCTION trade.on_stock_change();
CREATE TRIGGER trade_new_product
AFTER INSERT ON warehouse.warehouse_products
FOR EACH ROW EXECUTE FUNCTION trade.on_new_product();
CREATE TRIGGER trade_baseline_run
AFTER UPDATE OF status ON warehouse.warehouse_sync_runs
FOR EACH ROW WHEN (OLD.status = 'running' AND NEW.status IN ('partial', 'succeeded'))
EXECUTE FUNCTION trade.on_run_finished();

-- An existing catalog is starting stock up to the newest run, including one in
-- progress. A new database gets its baseline when its first run finishes.
INSERT INTO trade.baseline (run_id)
SELECT max(run_id) FROM warehouse.warehouse_sync_runs
HAVING max(run_id) IS NOT NULL AND EXISTS (SELECT 1 FROM warehouse.warehouse_products);

REVOKE ALL ON ALL TABLES IN SCHEMA trade FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA trade FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA trade FROM PUBLIC;

-- Inferred history cannot be rebuilt once old product versions are pruned, so
-- backups must include it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_backup_restore_operator') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA trade TO warehouse_backup_restore_operator';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA trade TO warehouse_backup_restore_operator';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA trade TO warehouse_backup_restore_operator';
    END IF;
END;
$$;
