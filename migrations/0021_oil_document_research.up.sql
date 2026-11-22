-- Readable research labels for staff. The existing evidence store controls verification.
CREATE TABLE automotive.oil_product_research (
    source_product_id TEXT PRIMARY KEY REFERENCES warehouse.warehouse_products(source_product_id),
    catalog_fingerprint CHAR(64) NOT NULL CHECK (catalog_fingerprint ~ '^[a-f0-9]{64}$'),
    formulation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'APPROVAL_DOCUMENTED', 'SPECIFICATION_DOCUMENTED', 'IDENTITY_DOCUMENTED',
        'PACKAGE_CHECK_REQUIRED', 'PRIMARY_SOURCE_UNRESOLVED', 'SPECIAL_APPLICATION'
    )),
    sae_grade TEXT NOT NULL,
    source_urls TEXT[] NOT NULL,
    approvals TEXT[] NOT NULL,
    specifications TEXT[] NOT NULL,
    note TEXT NOT NULL,
    checked_on DATE NOT NULL,
    CHECK (status <> 'APPROVAL_DOCUMENTED'
           OR (cardinality(approvals) > 0 AND cardinality(source_urls) > 0))
);

CREATE TABLE automotive.oil_vehicle_research (
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    model_year INTEGER NOT NULL CHECK (model_year BETWEEN 1980 AND 2100),
    engine_code TEXT NOT NULL,
    market TEXT NOT NULL,
    trim TEXT NOT NULL,
    approval TEXT NOT NULL,
    sae_grades TEXT[] NOT NULL,
    source_urls TEXT[] NOT NULL CHECK (cardinality(source_urls) > 0),
    conditions TEXT NOT NULL,
    PRIMARY KEY (make, model, model_year, engine_code, market, trim, approval)
);

CREATE VIEW automotive.oil_inventory_research WITH (security_invoker = true) AS
SELECT p.source_product_id, p.product_code, p.product_name, p.brand,
       p.stock_store, p.stock_tuz11, p.sae_grades, p.catalog_review_status,
       COALESCE(r.status, 'RESEARCH_PENDING') AS research_status,
       r.formulation, r.sae_grade AS documented_grade,
       r.approvals, r.specifications, r.source_urls, r.note, r.checked_on
FROM automotive.oil_inventory p
LEFT JOIN automotive.oil_product_research r
    ON r.source_product_id = p.source_product_id
    AND r.catalog_fingerprint = automotive.content_hash(jsonb_build_object(
        'name',p.product_name, 'model',p.product_model, 'brand',p.brand,
        'code',p.product_code, 'original',p.original_code, 'duplicate',p.duplicate_code))
WHERE p.catalog_review_status <> 'OTHER_PRODUCT';

CREATE VIEW automotive.oil_research_vehicle_matches WITH (security_invoker = true) AS
SELECT p.source_product_id, p.product_name, v.make, v.model, v.model_year,
       v.engine_code, v.market, v.trim, v.approval, p.documented_grade, v.conditions,
       p.note AS product_note, p.source_urls AS product_sources, v.source_urls AS vehicle_sources
FROM automotive.oil_inventory_research p
JOIN automotive.oil_vehicle_research v ON v.approval = ANY(p.approvals)
WHERE p.research_status = 'APPROVAL_DOCUMENTED'
  AND p.documented_grade = ANY(p.sae_grades)
  AND ((cardinality(v.sae_grades) = 0 AND v.conditions <> '')
       OR p.documented_grade = ANY(v.sae_grades));

COMMENT ON VIEW automotive.oil_inventory_research IS
    'Dated manufacturer document lookups for active catalog products.';
COMMENT ON VIEW automotive.oil_research_vehicle_matches IS
    'Document-based matches for researched configurations. Read the market and conditions.';

REVOKE ALL ON automotive.oil_product_research, automotive.oil_vehicle_research,
    automotive.oil_inventory_research, automotive.oil_research_vehicle_matches FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT SELECT ON automotive.oil_product_research, automotive.oil_vehicle_research,
            automotive.oil_inventory_research, automotive.oil_research_vehicle_matches
        TO automotive_runtime;
    END IF;
END $$;
