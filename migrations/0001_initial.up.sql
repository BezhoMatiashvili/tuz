CREATE TABLE IF NOT EXISTS warehouse_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE warehouse_sync_runs (
    run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    expected_pages INTEGER,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_changed INTEGER NOT NULL DEFAULT 0,
    rows_unchanged INTEGER NOT NULL DEFAULT 0,
    rows_deactivated INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    failure_reason TEXT
);

CREATE TABLE warehouse_products (
    source_product_id TEXT PRIMARY KEY,
    product_code TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    product_model TEXT NOT NULL DEFAULT '',
    original_code TEXT NOT NULL DEFAULT '',
    duplicate_code TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    source_columns JSONB NOT NULL,
    source_row_html TEXT NOT NULL,
    row_hash CHAR(64) NOT NULL,
    source_page INTEGER NOT NULL CHECK (source_page > 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen_run_id BIGINT NOT NULL REFERENCES warehouse_sync_runs(run_id),
    last_seen_run_id BIGINT NOT NULL REFERENCES warehouse_sync_runs(run_id)
);

CREATE INDEX warehouse_products_product_code_idx ON warehouse_products (product_code);
CREATE INDEX warehouse_products_active_idx ON warehouse_products (is_active) WHERE is_active;
CREATE INDEX warehouse_products_last_seen_run_idx ON warehouse_products (last_seen_run_id);

CREATE TABLE warehouse_product_versions (
    version_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_product_id TEXT NOT NULL REFERENCES warehouse_products(source_product_id),
    observed_run_id BIGINT NOT NULL REFERENCES warehouse_sync_runs(run_id),
    source_page INTEGER NOT NULL CHECK (source_page > 0),
    source_columns JSONB NOT NULL,
    source_row_html TEXT NOT NULL,
    row_hash CHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX warehouse_product_versions_product_time_idx
    ON warehouse_product_versions (source_product_id, observed_at DESC);
