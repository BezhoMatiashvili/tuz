-- For a legacy upgrade, first run 0002 only, sign historical rows with
-- `warehouse_sync --sign-legacy-versions`, then apply this immutable gate.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM warehouse.warehouse_products WHERE integrity_tag IS NULL
    ) OR EXISTS (
        SELECT 1 FROM warehouse.warehouse_product_versions WHERE integrity_tag IS NULL
    ) THEN
        RAISE EXCEPTION 'unsigned legacy warehouse observations remain';
    END IF;
END;
$$;

ALTER TABLE warehouse.warehouse_products
    VALIDATE CONSTRAINT warehouse_products_integrity_tag_format;
ALTER TABLE warehouse.warehouse_product_versions
    VALIDATE CONSTRAINT warehouse_product_versions_integrity_tag_format;
ALTER TABLE warehouse.warehouse_products
    ALTER COLUMN integrity_tag SET NOT NULL;
ALTER TABLE warehouse.warehouse_product_versions
    ALTER COLUMN integrity_tag SET NOT NULL;
