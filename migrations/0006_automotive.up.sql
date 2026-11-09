-- Separate, private application schema. Does not mutate the existing catalog.
CREATE SCHEMA automotive;
REVOKE ALL ON SCHEMA automotive FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA automotive REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA automotive REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA automotive REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE automotive.part_numbers (
    source_product_id TEXT NOT NULL REFERENCES warehouse.warehouse_products(source_product_id)
        ON DELETE CASCADE,
    field TEXT NOT NULL CHECK (field IN ('product_code','original_code','duplicate_code')),
    original TEXT NOT NULL,
    normalized TEXT NOT NULL CHECK (length(normalized) > 0),
    manufacturer TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL,
    source_fingerprint CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_product_id, field, original, parser_version)
);
CREATE INDEX part_numbers_lookup ON automotive.part_numbers(normalized, manufacturer);
CREATE TABLE automotive.catalog_index_state (
    source_product_id TEXT PRIMARY KEY REFERENCES warehouse.warehouse_products(source_product_id)
        ON DELETE CASCADE,
    source_fingerprint CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE automotive.vehicle_configurations (
    configuration_id TEXT PRIMARY KEY,
    configuration JSONB NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE automotive.vin_identifications (
    vin_hash CHAR(64) PRIMARY KEY CHECK (vin_hash ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND NOT payload ? 'VIN'
        AND NOT payload ? 'vin'),
    provider TEXT NOT NULL DEFAULT 'vPIC' CHECK (provider = 'vPIC'),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 days'
);
CREATE TABLE automotive.evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN
        ('fitment','cross_reference','service_specification','product_approval')),
    source_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL DEFAULT 'unknown',
    reuse_permission TEXT NOT NULL CHECK (reuse_permission IN ('owned','authorized','unknown')),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending','approved','rejected')),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    applicability JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(applicability) = 'object'),
    restrictions JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(restrictions) = 'object'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    fingerprint CHAR(64) NOT NULL UNIQUE,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (review_status <> 'approved' OR
        (reviewed_by IS NOT NULL AND length(btrim(reviewed_by)) > 0 AND reviewed_at IS NOT NULL))
);
CREATE TABLE automotive.fitments (
    evidence_id TEXT PRIMARY KEY REFERENCES automotive.evidence(evidence_id) ON DELETE CASCADE,
    part_code TEXT NOT NULL,
    configuration_id TEXT REFERENCES automotive.vehicle_configurations(configuration_id),
    compatible BOOLEAN
);
CREATE INDEX fitments_code ON automotive.fitments(part_code);
CREATE TABLE automotive.cross_references (
    evidence_id TEXT PRIMARY KEY REFERENCES automotive.evidence(evidence_id) ON DELETE CASCADE,
    from_code TEXT NOT NULL,
    to_code TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'reference',
    directional BOOLEAN NOT NULL DEFAULT TRUE CHECK (directional),
    restrictions JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX cross_references_code ON automotive.cross_references(from_code);
CREATE TABLE automotive.service_specifications (
    evidence_id TEXT PRIMARY KEY REFERENCES automotive.evidence(evidence_id) ON DELETE CASCADE,
    configuration_id TEXT REFERENCES automotive.vehicle_configurations(configuration_id),
    category TEXT NOT NULL
);
CREATE TABLE automotive.product_approvals (
    evidence_id TEXT PRIMARY KEY REFERENCES automotive.evidence(evidence_id) ON DELETE CASCADE,
    source_product_id TEXT NOT NULL REFERENCES warehouse.warehouse_products(source_product_id),
    approval TEXT NOT NULL
);
CREATE INDEX product_approvals_product ON automotive.product_approvals(source_product_id);
CREATE TABLE automotive.enrichment_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind = 'index'),
    payload JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed')),
    checkpoint TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_until TIMESTAMPTZ,
    lease_token TEXT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX enrichment_jobs_claim ON automotive.enrichment_jobs(status, lease_until, created_at);

-- Policies use existing role memberships without requiring cluster-wide CREATEROLE
-- for the migration owner. Provision roles with ops/sql/provision_automotive_roles.sql.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['part_numbers','catalog_index_state','vehicle_configurations',
        'vin_identifications','evidence','fitments','cross_references','service_specifications',
        'product_approvals','enrichment_jobs']
    LOOP
        EXECUTE format('ALTER TABLE automotive.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY automotive_read ON automotive.%I FOR SELECT USING '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN '
            || '(''automotive_runtime'',''automotive_enrichment'') '
            || 'AND pg_has_role(current_user, oid, ''member'')))', t);
        IF t <> 'evidence' THEN
        EXECUTE format('CREATE POLICY automotive_enrich ON automotive.%I FOR ALL USING '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ''automotive_enrichment'' '
            || 'AND pg_has_role(current_user, oid, ''member''))) WITH CHECK '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ''automotive_enrichment'' '
            || 'AND pg_has_role(current_user, oid, ''member'')))', t);
        END IF;
    END LOOP;
END $$;
CREATE POLICY automotive_evidence_import ON automotive.evidence FOR INSERT
    WITH CHECK (review_status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL
        AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_enrichment'
            AND pg_has_role(current_user, oid, 'member')));
CREATE POLICY automotive_vin_cache_insert ON automotive.vin_identifications FOR INSERT
    WITH CHECK (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime'
        AND pg_has_role(current_user, oid, 'member')));
CREATE POLICY automotive_vin_cache_update ON automotive.vin_identifications FOR UPDATE
    USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime'
        AND pg_has_role(current_user, oid, 'member')))
    WITH CHECK (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime'
        AND pg_has_role(current_user, oid, 'member')));
REVOKE ALL ON ALL TABLES IN SCHEMA automotive FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA automotive FROM PUBLIC;
COMMENT ON SCHEMA automotive IS 'Private automotive application; never expose through a Data API.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT USAGE ON SCHEMA automotive, warehouse TO automotive_runtime;
        GRANT SELECT ON warehouse.warehouse_products TO automotive_runtime;
        GRANT SELECT ON ALL TABLES IN SCHEMA automotive TO automotive_runtime;
        GRANT INSERT, UPDATE ON automotive.vin_identifications TO automotive_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_enrichment') THEN
        GRANT USAGE ON SCHEMA automotive, warehouse TO automotive_enrichment;
        GRANT SELECT ON warehouse.warehouse_products TO automotive_enrichment;
        GRANT SELECT, INSERT, DELETE ON ALL TABLES IN SCHEMA automotive TO automotive_enrichment;
        GRANT UPDATE ON automotive.part_numbers, automotive.catalog_index_state,
            automotive.vehicle_configurations, automotive.vin_identifications,
            automotive.enrichment_jobs TO automotive_enrichment;
    END IF;
END $$;
