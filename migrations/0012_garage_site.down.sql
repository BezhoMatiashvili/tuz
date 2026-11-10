-- Destructive: removes every dashboard account, session, edited site text, service,
-- uploaded gallery image and audit row. Export garage.* first if any of it must survive.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_site') THEN
        IF to_regclass('warehouse.warehouse_sync_runs') IS NOT NULL THEN
            REVOKE SELECT ON warehouse.warehouse_sync_runs FROM garage_site;
            REVOKE USAGE ON SCHEMA warehouse FROM garage_site;
        END IF;
        IF to_regclass('trade.sales') IS NOT NULL
                AND to_regclass('trade.purchases') IS NOT NULL THEN
            REVOKE SELECT ON trade.sales, trade.purchases FROM garage_site;
            REVOKE USAGE ON SCHEMA trade FROM garage_site;
        END IF;
    END IF;
END $$;
DROP SCHEMA IF EXISTS garage CASCADE;
