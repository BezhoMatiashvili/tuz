-- Vehicle detail columns, multilingual make/model names, a WMI lookup and importer
-- checkpoints. Every row stays a crawled or imported source assertion, never reviewed
-- automotive evidence. Nothing here may reference schema vpic: import-vpic drops and
-- rebuilds it.

ALTER TABLE parts_catalog.vehicles
    ADD COLUMN model_code TEXT NOT NULL DEFAULT '',
    ADD COLUMN engine_code TEXT NOT NULL DEFAULT '',
    ADD COLUMN displacement_cc INTEGER
        CHECK (displacement_cc IS NULL OR displacement_cc BETWEEN 1 AND 100000),
    ADD COLUMN power_kw INTEGER CHECK (power_kw IS NULL OR power_kw BETWEEN 1 AND 10000),
    ADD COLUMN fuel TEXT NOT NULL DEFAULT '',
    ADD COLUMN transmission TEXT NOT NULL DEFAULT '',
    ADD COLUMN drive TEXT NOT NULL DEFAULT '';
COMMENT ON COLUMN parts_catalog.vehicles.model_code IS
    'Frame, chassis or full model code as the source prints it, e.g. NDE180L-DEFDXW.';
COMMENT ON COLUMN parts_catalog.vehicles.engine_code IS
    'Engine code as the source prints it, e.g. 1ND-TV. Empty when not stated.';
COMMENT ON COLUMN parts_catalog.vehicles.displacement_cc IS 'Engine displacement in cm3.';
COMMENT ON COLUMN parts_catalog.vehicles.power_kw IS 'Engine power in kW.';
COMMENT ON COLUMN parts_catalog.vehicles.fuel IS 'Fuel as the source names it.';
COMMENT ON COLUMN parts_catalog.vehicles.transmission IS 'Gearbox as the source names it.';
COMMENT ON COLUMN parts_catalog.vehicles.drive IS 'Driven axle as the source names it.';
CREATE INDEX vehicles_engine_code ON parts_catalog.vehicles(make_norm, engine_code)
    WHERE engine_code <> '';
CREATE INDEX vehicles_model_code ON parts_catalog.vehicles(make_norm, model_code)
    WHERE model_code <> '';

CREATE TABLE parts_catalog.vehicle_names (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 300),
    kind TEXT NOT NULL CHECK (kind IN ('make','model')),
    make TEXT NOT NULL DEFAULT '',
    make_norm TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL CHECK (language ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})?$'),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 300),
    name_norm TEXT NOT NULL CHECK (length(name_norm) > 0),
    name_kind TEXT NOT NULL DEFAULT 'label' CHECK (name_kind IN ('label','alias')),
    year_from INTEGER CHECK (year_from IS NULL OR year_from BETWEEN 1800 AND 2100),
    year_to INTEGER CHECK (year_to IS NULL OR year_to BETWEEN 1800 AND 2100),
    raw JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(raw) = 'object'),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_ref, language, name_kind, name)
);
COMMENT ON TABLE parts_catalog.vehicle_names IS
    'Make and model names per language (labels and aliases), e.g. from Wikidata.';
COMMENT ON COLUMN parts_catalog.vehicle_names.source_ref IS
    'The source''s own identifier for the make or model, e.g. a Wikidata QID.';
COMMENT ON COLUMN parts_catalog.vehicle_names.make IS
    'For a model: the maker''s English name. For a make: its own English name.';
COMMENT ON COLUMN parts_catalog.vehicle_names.name_norm IS
    'Case-folded letters and digits of the name in any script, for lookups.';
CREATE INDEX vehicle_names_lookup ON parts_catalog.vehicle_names(name_norm, kind);
CREATE INDEX vehicle_names_make ON parts_catalog.vehicle_names(make_norm) WHERE kind = 'model';

CREATE TABLE parts_catalog.wmi (
    wmi TEXT NOT NULL CHECK (wmi ~ '^[A-Z0-9]{3,6}$'),
    make TEXT NOT NULL DEFAULT '',
    make_norm TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    vehicle_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'vpic',
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (wmi, make_norm)
);
COMMENT ON TABLE parts_catalog.wmi IS
    'World Manufacturer Identifier (first VIN characters) to make, manufacturer and country.';

CREATE TABLE parts_catalog.import_checkpoints (
    name TEXT PRIMARY KEY CHECK (name ~ '^[a-z0-9_:./-]{1,200}$'),
    state JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(state) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE parts_catalog.import_checkpoints IS
    'Where each bulk importer stopped, committed with its last batch so a rerun continues.';

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['vehicle_names','wmi','import_checkpoints'] LOOP
        EXECUTE format('ALTER TABLE parts_catalog.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY crawler_access ON parts_catalog.%I FOR ALL '
            || 'USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''parts_crawler'' '
            || 'AND pg_has_role(current_user,oid,''member''))) WITH CHECK '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''parts_crawler'' '
            || 'AND pg_has_role(current_user,oid,''member'')))', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['vehicle_names','wmi'] LOOP
        EXECUTE format('CREATE POLICY runtime_read ON parts_catalog.%I FOR SELECT '
            || 'USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''automotive_runtime'' '
            || 'AND pg_has_role(current_user,oid,''member'')))', t);
    END LOOP;
END $$;
REVOKE ALL ON parts_catalog.vehicle_names, parts_catalog.wmi,
    parts_catalog.import_checkpoints FROM PUBLIC;

-- Same rerunnable provisioning as 0009, with the two new runtime-readable tables.
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
            parts_catalog.part_fitments, parts_catalog.part_cross_refs,
            parts_catalog.vehicle_names, parts_catalog.wmi TO automotive_runtime;
    END IF;
END $$;
REVOKE ALL ON FUNCTION parts_catalog.provision() FROM PUBLIC;
SELECT parts_catalog.provision();
