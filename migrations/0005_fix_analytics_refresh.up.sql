-- The original procedure used PL/pgSQL record names p, v, and r that also
-- appeared as SQL aliases in its reconciliation queries. PostgreSQL can
-- reject those references as ambiguous. Use distinct record names and aliases.
CREATE OR REPLACE PROCEDURE analytics.refresh_all()
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, analytics
AS $$
DECLARE
    source_product warehouse.warehouse_products%ROWTYPE;
    source_version warehouse.warehouse_product_versions%ROWTYPE;
    source_run warehouse.warehouse_sync_runs%ROWTYPE;
BEGIN
    LOCK TABLE warehouse.warehouse_sync_runs, warehouse.warehouse_products,
        warehouse.warehouse_product_versions IN SHARE ROW EXCLUSIVE MODE;
    DELETE FROM analytics.fact_product_observation fact WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_product_versions versions
         WHERE versions.version_id = fact.source_version_id);
    DELETE FROM analytics.fact_product_current fact WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_products products
         JOIN analytics.dim_product dimension ON dimension.source_product_id = products.source_product_id
         WHERE dimension.product_key = fact.product_key);
    DELETE FROM analytics.fact_sync_run fact WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_sync_runs runs WHERE runs.run_id = fact.run_id);
    UPDATE analytics.dim_product dimension SET is_active = false WHERE NOT EXISTS
        (SELECT 1 FROM warehouse.warehouse_products products
         WHERE products.source_product_id = dimension.source_product_id);
    FOR source_product IN SELECT * FROM warehouse.warehouse_products LOOP
        PERFORM analytics.sync_product(source_product);
    END LOOP;
    FOR source_version IN SELECT * FROM warehouse.warehouse_product_versions LOOP
        PERFORM analytics.sync_observation(source_version);
    END LOOP;
    FOR source_run IN SELECT * FROM warehouse.warehouse_sync_runs LOOP
        PERFORM analytics.sync_run(source_run);
    END LOOP;
END;
$$;
