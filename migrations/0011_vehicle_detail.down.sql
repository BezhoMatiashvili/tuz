-- Rollback order runs this before 0009's down file, so schema parts_catalog still exists.
-- Restore 0009's provisioning function first: it must not name the tables dropped below.
CREATE OR REPLACE FUNCTION parts_catalog.provision() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='parts_crawler') THEN
        GRANT USAGE ON SCHEMA parts_catalog TO parts_crawler;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA parts_catalog
            TO parts_crawler;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA parts_catalog TO parts_crawler;
        IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='automotive') THEN
            GRANT USAGE ON SCHEMA automotive TO parts_crawler;
            GRANT SELECT ON automotive.part_numbers TO parts_crawler;
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='automotive_runtime') THEN
        GRANT USAGE ON SCHEMA parts_catalog TO automotive_runtime;
        GRANT SELECT ON parts_catalog.sources, parts_catalog.vehicles, parts_catalog.parts,
            parts_catalog.part_fitments, parts_catalog.part_cross_refs TO automotive_runtime;
    END IF;
END $$;
REVOKE ALL ON FUNCTION parts_catalog.provision() FROM PUBLIC;

DROP TABLE IF EXISTS parts_catalog.import_checkpoints;
DROP TABLE IF EXISTS parts_catalog.wmi;
DROP TABLE IF EXISTS parts_catalog.vehicle_names;
DROP INDEX IF EXISTS parts_catalog.vehicles_model_code;
DROP INDEX IF EXISTS parts_catalog.vehicles_engine_code;
ALTER TABLE parts_catalog.vehicles
    DROP COLUMN IF EXISTS drive,
    DROP COLUMN IF EXISTS transmission,
    DROP COLUMN IF EXISTS fuel,
    DROP COLUMN IF EXISTS power_kw,
    DROP COLUMN IF EXISTS displacement_cc,
    DROP COLUMN IF EXISTS engine_code,
    DROP COLUMN IF EXISTS model_code;
