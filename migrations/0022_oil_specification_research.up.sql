-- Some vehicle manuals allow an oil that meets a published specification.
-- Keep this evidence distinguishable from formal-approval matches.
ALTER TABLE automotive.oil_vehicle_research
    ADD COLUMN allows_manufacturer_specification BOOLEAN NOT NULL DEFAULT FALSE;

CREATE VIEW automotive.oil_research_specification_matches WITH (security_invoker = true) AS
SELECT p.source_product_id, p.product_name, v.make, v.model, v.model_year,
       v.engine_code, v.market, v.trim, v.approval AS specification,
       p.documented_grade, v.conditions, p.note AS product_note,
       p.source_urls AS product_sources, v.source_urls AS vehicle_sources
FROM automotive.oil_inventory_research p
JOIN automotive.oil_vehicle_research v ON v.approval = ANY(p.specifications)
WHERE p.research_status IN ('APPROVAL_DOCUMENTED', 'SPECIFICATION_DOCUMENTED')
  AND v.allows_manufacturer_specification
  AND cardinality(p.source_urls) > 0
  AND p.documented_grade = ANY(p.sae_grades)
  AND p.documented_grade = ANY(v.sae_grades)
  AND NOT (v.approval = ANY(p.approvals));

COMMENT ON VIEW automotive.oil_research_specification_matches IS
    'Manufacturer specification claims for applications whose manuals accept meeting the spec.';
REVOKE ALL ON automotive.oil_research_specification_matches FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT SELECT ON automotive.oil_research_specification_matches TO automotive_runtime;
    END IF;
END $$;
