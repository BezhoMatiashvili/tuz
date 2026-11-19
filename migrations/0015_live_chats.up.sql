-- Garage 1886, dashboard "Live chats": alarms for staff, and the website chat as staff can read it.
-- Schema garage (0012, 0013, 0014).
--
-- * garage.live_alerts: one row per open alarm of a chat and kind. A partial unique index keeps it at
--   one open row per (channel, chat, kind); a repeat merges into it (automotive.live_store). Two
--   logins write here: the Messenger worker (garage_messenger) and the web process (garage_site,
--   the dashboard's own login, for the website chat); the dashboard marks alarms done.
--   kind: part_not_found, visit_request, service_booking, needs_staff. An alarm may carry the phone
--   number a customer typed for the shop to call. That is personal data: the dashboard's "Delete
--   data" (MetaStore.delete_customer) removes a Messenger customer's alarms, and the daily purge and
--   the website recorder's prune remove old ones.
-- * garage.live_web_chats and garage.live_web_messages: what a website visitor and the bot said, as the
--   visitor saw it, so staff can read it live and switch the bot off for one visitor. chat_key is the
--   first 32 hex digits of SHA-256(conversation id), the key the website chat log already uses; the
--   raw id (a bearer secret) is never stored. Only garage_site reaches these tables. The recorder
--   prunes them after the website chat log's retention (30 days).
-- * garage.meta_provision() is redefined whole: 0014's body plus the grants for these tables.

CREATE TABLE garage.live_alerts (
    alert_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('messenger', 'web')),
    chat_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('part_not_found', 'visit_request', 'service_booking', 'needs_staff')),
    reason TEXT NOT NULL DEFAULT '' CHECK (reason IN
        ('', 'nothing_found', 'sold_out', 'partial', 'handoff', 'failed', 'phone')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    summary TEXT NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 300),
    items JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(items) = 'array' AND octet_length(items::text) <= 8192),
    occurrences INTEGER NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
    car_label TEXT NOT NULL DEFAULT '' CHECK (char_length(car_label) <= 80),
    phone TEXT CHECK (phone IS NULL OR phone ~ '^\+?[0-9]{7,15}$'),
    phone_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_by TEXT NOT NULL DEFAULT '' CHECK (char_length(handled_by) <= 64),
    handled_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT live_alerts_chat_key_shape CHECK (
        (channel = 'messenger' AND chat_key ~ '^[0-9]{1,30}$')
        OR (channel = 'web' AND chat_key ~ '^[0-9a-f]{32}$')),
    CONSTRAINT live_alerts_handled CHECK ((status = 'done') = (handled_at IS NOT NULL))
);
CREATE UNIQUE INDEX live_alerts_one_open ON garage.live_alerts (channel, chat_key, kind)
    WHERE status = 'open';
CREATE INDEX live_alerts_status_updated ON garage.live_alerts (status, updated_at DESC);
CREATE INDEX live_alerts_chat ON garage.live_alerts (channel, chat_key);
COMMENT ON TABLE garage.live_alerts IS
    'Alarms for staff: a part the bot could not find, a visit or service request, a bot that needs a human.';

CREATE TABLE garage.live_web_chats (
    chat_key TEXT PRIMARY KEY CHECK (chat_key ~ '^[0-9a-f]{32}$'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_customer_at TIMESTAMPTZ,
    last_bot_at TIMESTAMPTZ,
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    language TEXT NOT NULL DEFAULT '' CHECK (language IN ('', 'ka', 'en')),
    car_label TEXT NOT NULL DEFAULT '' CHECK (char_length(car_label) <= 80),
    bot_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    muted_by TEXT NOT NULL DEFAULT '' CHECK (char_length(muted_by) <= 64),
    muted_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX live_web_chats_activity ON garage.live_web_chats (last_message_at DESC);
COMMENT ON TABLE garage.live_web_chats IS
    'Website chat conversations as staff see them, keyed by a hash of the conversation id.';

CREATE TABLE garage.live_web_messages (
    message_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_key TEXT NOT NULL REFERENCES garage.live_web_chats (chat_key) ON DELETE CASCADE,
    sender TEXT NOT NULL CHECK (sender IN ('customer', 'bot')),
    body TEXT NOT NULL DEFAULT '' CHECK (char_length(body) <= 4000),
    offered JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(offered) = 'array' AND octet_length(offered::text) <= 8192),
    outcome TEXT NOT NULL DEFAULT '' CHECK (char_length(outcome) <= 20),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX live_web_messages_chat ON garage.live_web_messages (chat_key, message_id);
CREATE INDEX live_web_messages_stored ON garage.live_web_messages (stored_at);
COMMENT ON TABLE garage.live_web_messages IS
    'What a website visitor wrote and what the bot answered, as the visitor saw it.';

-- Row-level security by role membership, as 0013 and 0014 do for every table they create. The
-- worker reaches the alarms only; the website tables belong to the dashboard's login.
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
    ALTER TABLE garage.live_alerts ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY worker_access ON garage.live_alerts FOR ALL USING ('
        || worker || ') WITH CHECK (' || worker || ')';
    EXECUTE 'CREATE POLICY site_access ON garage.live_alerts FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.live_alerts FOR SELECT USING ('
        || backup || ')';
    ALTER TABLE garage.live_web_chats ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY site_access ON garage.live_web_chats FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.live_web_chats FOR SELECT USING ('
        || backup || ')';
    ALTER TABLE garage.live_web_messages ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY site_access ON garage.live_web_messages FOR ALL USING ('
        || site || ') WITH CHECK (' || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.live_web_messages FOR SELECT USING ('
        || backup || ')';
END $$;
REVOKE ALL ON garage.live_alerts, garage.live_web_chats, garage.live_web_messages FROM PUBLIC;

-- 0014's function with the grants for the three tables above. Provisioning reruns it
-- (ops/sql/provision_automotive_roles.sql, messenger_setup --setup), so the grants hold for a role
-- created after this migration too. The identity columns need the sequence USAGE both logins
-- already get on every sequence of the schema.
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
                      updated_at, version) ON garage.live_alerts TO garage_messenger;
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
        -- Live chats: the dashboard marks alarms done, switches a website visitor's bot and removes a
        -- deleted customer's alarms; this login also records the website chat and raises its alarms.
        GRANT SELECT, INSERT, DELETE ON garage.live_alerts, garage.live_web_chats,
            garage.live_web_messages TO garage_site;
        GRANT UPDATE (reason, status, summary, items, occurrences, car_label, phone, phone_at,
                      updated_at, handled_by, handled_at, version)
            ON garage.live_alerts TO garage_site;
        GRANT UPDATE (last_customer_at, last_bot_at, last_message_at, language, car_label,
                      bot_enabled, muted_by, muted_at, version, updated_at)
            ON garage.live_web_chats TO garage_site;
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
