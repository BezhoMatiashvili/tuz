-- Destructive: removes every crawled page index, queue entry and catalog row.
-- Raw page files under data/crawl/ are not touched.
DO $$
BEGIN
    IF to_regclass('automotive.part_numbers') IS NOT NULL THEN
        DROP POLICY IF EXISTS parts_crawler_read ON automotive.part_numbers;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'parts_crawler') THEN
            REVOKE SELECT ON automotive.part_numbers FROM parts_crawler;
        END IF;
    END IF;
END $$;
DROP SCHEMA IF EXISTS parts_catalog CASCADE;
