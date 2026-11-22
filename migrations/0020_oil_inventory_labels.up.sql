-- Labels are computed from each current inventory row. Inserts, edits and restocks
-- are visible after the stock transaction commits.
CREATE VIEW automotive.oil_inventory WITH (security_invoker = true) AS
WITH patterns AS (
    SELECT
        '(?:^|[^0-9A-Z])(0|5|10|15|20|25)[ ]*W[- /]?[ ]*(08|8|12|16|20|30|40|50|60)(?![0-9])'
            AS multigrade,
        '\mSAE[ -]+(20|30|40|50|60)\M' AS monograde,
        '\m(engine|motor)[ -]+oils?\M|ძრავის\s+ზეთი\M|ზეთი\s+ძრავის\M' AS engine_name,
        '\moil\M|ძრავის\s+ზეთ|ზეთი|ზეთები' AS oil_name,
        '\m(filters?|transmission|gear|brake|steering|hydraulic|axle|atf|cvt|coolant|antifreeze'
            '|grease|sensor|radiator|pump|gasket|seal|cooler)\M|ფილტრ|ფილ\.'
            '|ტრანსმისი|გადაცემ|სამუხრუჭ|ჰიდრავლ|რედუქტორ|ანტიფრიზ|საცხი|კოლოფის'
            '|სენსორ|რადიატორ|ტუმბო|მანჟეტ|შუასადებ|საფენ|ჩობალ|კორპ' AS excluded_name,
        'ზეთის\s+(წნევ|ხუფ|საცობ|სარჭ|შუპ|დონის|საზომ|მარეგულირ|რად\.|პომპ|გამოსაშვ'
            '|ჩასასხმ|ავზ|ბოლვის|ფილტრ|მილ|სარქველ|გამანაწილ|გამაგრილ|გაგრ|გამოსარეცხ)'
            '|საჭის\s+გამაძლ|ამორტიზატორ|კომპრესორ|კომპრეს\.|შესაზეთი\s+გამფრქვევ'
            '|თხევადი\s+საპონი|უკანა\s+ხიდი|ხიდის\s+ზეთ|კოლოფის\s+ზეთ'
            '|\m(ATF|CVTF?|DEXRON|MERCON|DPSF|CORENA|GETRIEBEOEL|SINTOFLUID|SINTOPOID'
            '|NUTO|TUTELA|RENOLIN|MOTYLGEAR)\M|\moil\s+(filter|cap|pump|seal|sensor)\M'
            '|\m(suspension\s+AHC|DIFF\.\s+GO|engine\s+flush|additive|stop\s+smoke'
            '|gear\s+oil|compressor\s+oil|hydraulic\s+oil)\M' AS other_product,
        '\m(MOTORCYCLE|JASO[ -]+MA2?|[24]T|CYLINDER\s+LUB)\M' AS special_application
)
SELECT p.source_product_id, p.product_code, p.product_name, p.product_model,
       p.original_code, p.duplicate_code, p.brand, p.last_observed_at, p.row_hash,
       p.source_columns->>'column_06' AS price_raw,
       p.source_columns->>'column_10' AS stock_store,
       p.source_columns->>'column_12' AS stock_tuz11,
       grades.sae_grades,
       CASE
           WHEN p.product_name ~* patterns.other_product THEN 'OTHER_PRODUCT'
           WHEN p.product_name ~* patterns.special_application THEN 'SPECIAL_APPLICATION_REVIEW'
           WHEN cardinality(grades.sae_grades) = 1 THEN 'NEEDS_APPROVAL_REVIEW'
           WHEN p.product_name ~* patterns.engine_name THEN 'NEEDS_GRADE_AND_APPROVAL_REVIEW'
           ELSE 'NEEDS_TYPE_REVIEW'
       END AS catalog_review_status
FROM warehouse.warehouse_products p
CROSS JOIN patterns
CROSS JOIN LATERAL (
    SELECT ARRAY(
        SELECT value FROM (
            SELECT g[1] || 'W-' || lpad(g[2], 2, '0') AS value
            FROM regexp_matches(p.product_name, patterns.multigrade, 'gi') AS g
            UNION
            SELECT 'SAE ' || g[1]
            FROM regexp_matches(p.product_name, patterns.monograde, 'gi') AS g
        ) grades_found ORDER BY value
    ) AS sae_grades
) grades
WHERE p.is_active AND (
    (concat_ws(' ', p.product_name, p.product_model) ~* patterns.oil_name
     AND concat_ws(' ', p.product_name, p.product_model) !~* patterns.excluded_name)
    OR p.product_name ~* patterns.engine_name
    OR p.product_name ~* patterns.multigrade
);

COMMENT ON VIEW automotive.oil_inventory IS
    'Live oil triage labels from the synchronized catalog. Vehicle compatibility uses reviewed evidence.';
COMMENT ON COLUMN automotive.oil_inventory.catalog_review_status IS
    'Automatic label from catalog text. The staff oil view also checks current formal approvals.';
REVOKE ALL ON automotive.oil_inventory FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT SELECT ON automotive.oil_inventory TO automotive_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_enrichment') THEN
        GRANT SELECT ON automotive.oil_inventory TO automotive_enrichment;
    END IF;
END $$;
