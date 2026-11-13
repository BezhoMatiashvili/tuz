-- Garage 1886 on Facebook: a customer's data deletion reaches their comments, for good, and the
-- public receiver loses a sequence privilege it never needed. Schema garage (0012, 0013).
--
-- * The dashboard's "Delete data" (MetaStore.delete_customer, login garage_site) now deletes the
--   customer's stored comments too, in the same transaction as their chat, as the public
--   /data-deletion page promises. 0013 let that login only read comments.
-- * garage.meta_forgotten keeps what makes such a deletion last: the 30-day comment backfill and
--   a late webhook would otherwise store the same comments again. A row is the SHA-256 of
--   'author:<id>' or 'comment:<id>' (automotive.meta_store.forgotten_key), never the id itself,
--   and the moment of the deletion. Only comments written at or before that moment are kept out,
--   so a comment the person writes later is stored as any other. The worker's daily purge drops
--   rows older than the backfill's reach (31 days), after which they can stop nothing.
-- * garage_webhook loses USAGE on the inbox's identity sequence: an identity column needs no
--   sequence privilege to be filled, and with it the receiver's login could call nextval().

CREATE TABLE garage.meta_forgotten (
    subject_key CHAR(64) PRIMARY KEY CHECK (subject_key ~ '^[0-9a-f]{64}$'),
    forgotten_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX meta_forgotten_at_idx ON garage.meta_forgotten (forgotten_at);
COMMENT ON TABLE garage.meta_forgotten IS
    'Hashed author and comment ids of deleted customers; their earlier comments are never stored again.';

-- Row-level security by role membership, as 0013 does for every table it creates.
DO $$
DECLARE
    site CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_site'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    worker CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_messenger'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    backup CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles '
        || 'WHERE rolname=''warehouse_backup_restore_operator'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
BEGIN
    ALTER TABLE garage.meta_forgotten ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY worker_access ON garage.meta_forgotten FOR ALL USING ('
        || worker || ') WITH CHECK (' || worker || ')';
    EXECUTE 'CREATE POLICY site_access ON garage.meta_forgotten FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.meta_forgotten FOR SELECT USING ('
        || backup || ')';
END $$;
REVOKE ALL ON garage.meta_forgotten FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_webhook') THEN
        EXECUTE format('REVOKE USAGE ON SEQUENCE %s FROM garage_webhook',
                       pg_get_serial_sequence('garage.meta_inbox', 'inbox_id'));
    END IF;
END $$;

-- 0013's function with three changes: no sequence grant for garage_webhook, garage_site may
-- delete comments, and both logins reach garage.meta_forgotten. Provisioning reruns it
-- (ops/sql/provision_automotive_roles.sql, messenger_setup --setup), so the grants hold for a
-- role created after this migration too.
CREATE OR REPLACE FUNCTION garage.meta_provision() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_webhook') THEN
        GRANT USAGE ON SCHEMA garage TO garage_webhook;
        GRANT INSERT (event_key, field, page_id, payload) ON garage.meta_inbox TO garage_webhook;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_messenger') THEN
        GRANT USAGE ON SCHEMA garage TO garage_messenger;
        GRANT SELECT, UPDATE, DELETE ON garage.meta_inbox TO garage_messenger;
        GRANT SELECT, UPDATE ON garage.meta_settings, garage.meta_page, garage.meta_tokens
            TO garage_messenger;
        GRANT SELECT, INSERT, UPDATE, DELETE ON garage.messenger_threads,
            garage.messenger_messages, garage.messenger_replies, garage.meta_comments,
            garage.meta_page_daily, garage.meta_images, garage.meta_posts,
            garage.meta_ad_accounts, garage.meta_ads, garage.meta_ad_daily,
            garage.meta_sync_runs, garage.meta_forgotten TO garage_messenger;
        GRANT SELECT, UPDATE, DELETE ON garage.meta_job_requests TO garage_messenger;
        GRANT SELECT ON garage.site_sections, garage.services TO garage_messenger;
        GRANT INSERT ON garage.audit_log TO garage_messenger;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA garage TO garage_messenger;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_site') THEN
        GRANT SELECT, UPDATE ON garage.meta_settings TO garage_site;
        GRANT SELECT, INSERT, UPDATE, DELETE ON garage.meta_page, garage.meta_tokens TO garage_site;
        GRANT SELECT (inbox_id, field, received_at, processed_at, attempts, outcome)
            ON garage.meta_inbox TO garage_site;
        GRANT SELECT, DELETE ON garage.messenger_threads, garage.messenger_messages,
            garage.messenger_replies TO garage_site;
        GRANT UPDATE (bot_enabled, is_admin, needs_attention, attention_reason, updated_at,
                      version) ON garage.messenger_threads TO garage_site;
        GRANT SELECT ON garage.meta_comments, garage.meta_page_daily, garage.meta_images,
            garage.meta_posts, garage.meta_ad_accounts, garage.meta_ads, garage.meta_ad_daily,
            garage.meta_sync_runs TO garage_site;
        GRANT UPDATE (is_hidden, public_reply_id, updated_at) ON garage.meta_comments
            TO garage_site;
        -- A customer's data deletion: their comments, and the hashed ids that keep them out.
        GRANT DELETE ON garage.meta_comments TO garage_site;
        GRANT SELECT, INSERT, UPDATE ON garage.meta_forgotten TO garage_site;
        GRANT SELECT, INSERT ON garage.meta_job_requests TO garage_site;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA garage TO garage_site;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_backup_restore_operator') THEN
        GRANT USAGE ON SCHEMA garage TO warehouse_backup_restore_operator;
        GRANT SELECT ON ALL TABLES IN SCHEMA garage TO warehouse_backup_restore_operator;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA garage
            TO warehouse_backup_restore_operator;
    END IF;
END $$;
REVOKE ALL ON FUNCTION garage.meta_provision() FROM PUBLIC;
SELECT garage.meta_provision();
