-- Garage 1886, dashboard "Prompts": the owner's edits of the prompts the live bot sends to its model.
-- Schema garage (0012-0018).
--
-- * garage.llm_prompts: one row per edited prompt (automotive.prompt_library.PROMPTS, editable
--   keys only). No row means the packaged text in the code, so a release's new prompt applies to
--   every prompt the owner has not changed. version guards concurrent edits as site_sections does.
--   updated_by is the dashboard username (kept when the account is deleted; the audit log has the
--   same).
-- * garage.llm_prompt_history: every save and reset, newest first on the dashboard, so an earlier
--   text can be loaded back. Never pruned: a few rows a month.
-- * Logins: garage_site (the dashboard and the website chat) reads and writes both; garage_messenger
--   (the Messenger worker) reads the key and text of the edits and nothing else.
-- * garage.meta_provision() is redefined whole: 0018's body plus the grants for these tables.

CREATE TABLE garage.llm_prompts (
    prompt_key TEXT PRIMARY KEY CHECK (prompt_key ~ '^[a-z][a-z0-9_.]{1,63}$'),
    body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 60000 AND btrim(body) <> ''),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL DEFAULT '' CHECK (char_length(updated_by) <= 32)
);
COMMENT ON TABLE garage.llm_prompts IS
    'The owner''s edits of the prompts the live bot sends to its model; no row = the packaged text.';

CREATE TABLE garage.llm_prompt_history (
    history_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_key TEXT NOT NULL CHECK (prompt_key ~ '^[a-z][a-z0-9_.]{1,63}$'),
    action TEXT NOT NULL CHECK (action IN ('saved', 'reset')),
    body TEXT NOT NULL DEFAULT '' CHECK (char_length(body) <= 60000),
    version INTEGER NOT NULL CHECK (version >= 1),
    saved_by TEXT NOT NULL DEFAULT '' CHECK (char_length(saved_by) <= 32),
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT llm_prompt_history_body CHECK ((action = 'saved') = (body <> ''))
);
CREATE INDEX llm_prompt_history_key ON garage.llm_prompt_history (prompt_key, history_id DESC);
COMMENT ON TABLE garage.llm_prompt_history IS 'Every save and reset of a prompt edit.';

-- Row-level security by role membership, as 0012-0015 do for every table they create.
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
    ALTER TABLE garage.llm_prompts ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY site_access ON garage.llm_prompts FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY worker_read ON garage.llm_prompts FOR SELECT USING (' || worker || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.llm_prompts FOR SELECT USING (' || backup || ')';
    ALTER TABLE garage.llm_prompt_history ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY site_access ON garage.llm_prompt_history FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.llm_prompt_history FOR SELECT USING ('
        || backup || ')';
END $$;
REVOKE ALL ON garage.llm_prompts, garage.llm_prompt_history FROM PUBLIC;

-- 0018's function with the grants for the two tables above. Provisioning reruns it
-- (ops/sql/provision_automotive_roles.sql, messenger_setup --setup), so the grants hold for a role
-- created after this migration too.
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
        -- Alarms from the bot's turns. Only what a repeat or a phone number changes: never the chat,
        -- the kind, or who handled it (closing an alarm is the dashboard login's job).
        GRANT SELECT, INSERT, DELETE ON garage.live_alerts TO garage_messenger;
        GRANT UPDATE (reason, summary, items, occurrences, car_label, phone, phone_at,
                      updated_at, version, detail) ON garage.live_alerts TO garage_messenger;
        -- The owner's prompt edits: the bot reads them, only the dashboard writes them.
        GRANT SELECT (prompt_key, body) ON garage.llm_prompts TO garage_messenger;
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
                      version, waiting_dismissed_id, bot_tester)
        ON garage.messenger_threads TO garage_site;
        GRANT SELECT ON garage.meta_comments, garage.meta_page_daily, garage.meta_images,
            garage.meta_posts, garage.meta_ad_accounts, garage.meta_ads, garage.meta_ad_daily,
            garage.meta_sync_runs TO garage_site;
        GRANT UPDATE (is_hidden, public_reply_id, updated_at) ON garage.meta_comments
            TO garage_site;
        -- A customer's data deletion: their comments, and the hashed ids that keep them out.
        GRANT DELETE ON garage.meta_comments TO garage_site;
        GRANT SELECT, INSERT, UPDATE ON garage.meta_forgotten TO garage_site;
        GRANT SELECT, INSERT ON garage.meta_job_requests TO garage_site;
        -- Live chats: the dashboard marks alarms done, switches a website visitor's bot and removes a
        -- deleted customer's alarms; this login also records the website chat and raises its alarms.
        GRANT SELECT, INSERT, DELETE ON garage.live_alerts, garage.live_web_chats,
            garage.live_web_messages TO garage_site;
        GRANT UPDATE (reason, status, summary, items, occurrences, car_label, phone, phone_at,
                      updated_at, handled_by, handled_at, version, detail)
            ON garage.live_alerts TO garage_site;
        GRANT UPDATE (last_customer_at, last_bot_at, last_message_at, language, car_label,
                      bot_enabled, muted_by, muted_at, version, updated_at)
            ON garage.live_web_chats TO garage_site;
        -- Dashboard > Prompts: the owner edits the live bot's prompts and keeps their history.
        GRANT SELECT, INSERT, UPDATE, DELETE ON garage.llm_prompts TO garage_site;
        GRANT SELECT, INSERT ON garage.llm_prompt_history TO garage_site;
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
