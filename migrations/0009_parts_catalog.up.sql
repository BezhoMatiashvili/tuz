-- Crawled public parts catalogs: a resumable crawl queue, raw page index and the
-- normalized part, vehicle, fitment and cross-reference tables built from them.
-- Crawled assertions are source claims, never reviewed automotive evidence.
CREATE SCHEMA parts_catalog;
REVOKE ALL ON SCHEMA parts_catalog FROM PUBLIC;
COMMENT ON SCHEMA parts_catalog IS
    'Crawled source assertions; never VERIFIED evidence or a Data API schema.';

CREATE TABLE parts_catalog.sources (
    source TEXT PRIMARY KEY CHECK (source ~ '^[a-z0-9_]{2,32}$'),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','blocked')),
    state_reason TEXT,
    daily_budget INTEGER NOT NULL DEFAULT 15000 CHECK (daily_budget >= 0),
    budget_day DATE,
    budget_used INTEGER NOT NULL DEFAULT 0 CHECK (budget_used >= 0),
    breaker_trips INTEGER NOT NULL DEFAULT 0 CHECK (breaker_trips >= 0),
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE parts_catalog.crawl_queue (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL REFERENCES parts_catalog.sources(source),
    url TEXT NOT NULL CHECK (url ~ '^https://' AND length(url) <= 2048),
    kind TEXT NOT NULL CHECK (kind ~ '^[a-z0-9_]{1,40}$'),
    priority INTEGER NOT NULL DEFAULT 100,
    depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
    context JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(context) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','done','failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    http_status INTEGER,
    content_sha CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, url)
);
CREATE INDEX crawl_queue_claim
    ON parts_catalog.crawl_queue(source, priority, next_attempt_at, id)
    WHERE status IN ('pending','running');
CREATE INDEX crawl_queue_status ON parts_catalog.crawl_queue(source, status);

CREATE TABLE parts_catalog.raw_pages (
    queue_id BIGINT PRIMARY KEY REFERENCES parts_catalog.crawl_queue(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    content_sha CHAR(64) NOT NULL CHECK (content_sha ~ '^[0-9a-f]{64}$'),
    storage_path TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    parser_version TEXT,
    parsed_at TIMESTAMPTZ
);
CREATE INDEX raw_pages_reparse ON parts_catalog.raw_pages(source, parser_version);

CREATE TABLE parts_catalog.vehicles (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_vehicle_id TEXT NOT NULL CHECK (length(source_vehicle_id) BETWEEN 1 AND 300),
    make TEXT NOT NULL,
    make_norm TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    year_from INTEGER CHECK (year_from IS NULL OR year_from BETWEEN 1900 AND 2100),
    year_to INTEGER CHECK (year_to IS NULL OR year_to BETWEEN 1900 AND 2100),
    engine TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    raw JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(raw) = 'object'),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_vehicle_id)
);
CREATE INDEX vehicles_lookup ON parts_catalog.vehicles(make_norm, model, year_from);

CREATE TABLE parts_catalog.parts (
    id BIGSERIAL PRIMARY KEY,
    brand TEXT NOT NULL,
    brand_norm TEXT NOT NULL,
    number TEXT NOT NULL,
    normalized TEXT NOT NULL CHECK (length(normalized) BETWEEN 1 AND 105),
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'unknown' CHECK (kind IN ('oe','aftermarket','unknown')),
    first_source TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (brand_norm, normalized)
);
CREATE INDEX parts_normalized ON parts_catalog.parts(normalized);

CREATE TABLE parts_catalog.part_fitments (
    part_id BIGINT NOT NULL REFERENCES parts_catalog.parts(id) ON DELETE CASCADE,
    vehicle_id BIGINT NOT NULL REFERENCES parts_catalog.vehicles(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    position TEXT NOT NULL DEFAULT '',
    quantity TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (part_id, vehicle_id, source, note)
);
CREATE INDEX part_fitments_vehicle ON parts_catalog.part_fitments(vehicle_id);

-- A row is a claim as published by one source, in the direction it was published.
CREATE TABLE parts_catalog.part_cross_refs (
    part_id BIGINT NOT NULL REFERENCES parts_catalog.parts(id) ON DELETE CASCADE,
    other_part_id BIGINT NOT NULL REFERENCES parts_catalog.parts(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'equivalent'
        CHECK (relation IN ('equivalent','oe','replaces','replaced_by')),
    rating INTEGER NOT NULL DEFAULT 1,
    source_url TEXT NOT NULL DEFAULT '',
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (part_id, other_part_id, source, relation),
    CHECK (part_id <> other_part_id)
);
CREATE INDEX part_cross_refs_other ON parts_catalog.part_cross_refs(other_part_id);

CREATE TABLE parts_catalog.crawl_runs (
    id BIGSERIAL PRIMARY KEY,
    sources TEXT[] NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    fetched INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    stats JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(stats) = 'object'),
    stop_reason TEXT
);

INSERT INTO parts_catalog.sources (source, daily_budget) VALUES
    ('sevenzap', 15000), ('catcar', 8000), ('mann', 15000), ('ngk', 15000),
    ('rockauto', 15000), ('realoem', 15000), ('partsouq', 15000),
    ('vpic', 0), ('ymm_csv', 0), ('fapi', 0);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sources','crawl_queue','raw_pages','vehicles','parts',
        'part_fitments','part_cross_refs','crawl_runs'] LOOP
        EXECUTE format('ALTER TABLE parts_catalog.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY crawler_access ON parts_catalog.%I FOR ALL '
            || 'USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''parts_crawler'' '
            || 'AND pg_has_role(current_user,oid,''member''))) WITH CHECK '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''parts_crawler'' '
            || 'AND pg_has_role(current_user,oid,''member'')))', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['sources','vehicles','parts','part_fitments',
        'part_cross_refs'] LOOP
        EXECUTE format('CREATE POLICY runtime_read ON parts_catalog.%I FOR SELECT '
            || 'USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''automotive_runtime'' '
            || 'AND pg_has_role(current_user,oid,''member'')))', t);
    END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA parts_catalog FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA parts_catalog FROM PUBLIC;

-- The crawler reads warehouse part codes only to prioritize its queue.
DO $$
BEGIN
    IF to_regclass('automotive.part_numbers') IS NOT NULL THEN
        CREATE POLICY parts_crawler_read ON automotive.part_numbers FOR SELECT
            USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'parts_crawler'
                AND pg_has_role(current_user, oid, 'member')));
    END IF;
END $$;

-- Safe to rerun from provisioning when roles are created after this migration.
CREATE FUNCTION parts_catalog.provision() RETURNS VOID LANGUAGE plpgsql AS $$
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
SELECT parts_catalog.provision();
