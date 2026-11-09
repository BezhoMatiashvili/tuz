-- Run as cluster administrator before 0006, or rerun after 0006 if roles were absent.
-- Attach these NOLOGIN roles to separate runtime/enrichment logins out of band.
-- Never give either login warehouse_migration_owner membership.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        CREATE ROLE automotive_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_enrichment') THEN
        CREATE ROLE automotive_enrichment NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'automotive') THEN
        GRANT USAGE ON SCHEMA automotive, warehouse TO automotive_runtime, automotive_enrichment;
        GRANT SELECT ON warehouse.warehouse_products TO automotive_runtime, automotive_enrichment;
        GRANT SELECT ON ALL TABLES IN SCHEMA automotive TO automotive_runtime;
        GRANT INSERT, UPDATE ON automotive.vin_identifications TO automotive_runtime;
        GRANT SELECT, INSERT, DELETE ON ALL TABLES IN SCHEMA automotive TO automotive_enrichment;
        GRANT UPDATE ON automotive.part_numbers, automotive.catalog_index_state,
            automotive.vehicle_configurations, automotive.vin_identifications,
            automotive.enrichment_jobs TO automotive_enrichment;
    END IF;
END $$;
-- Evidence review is a migration/operator-owner capability. The enrichment role
-- deliberately cannot UPDATE evidence or approve its own imported assertions.
