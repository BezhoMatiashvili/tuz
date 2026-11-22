-- Garage 1886 on Facebook: test mode for the Messenger bot. The bot answers only the chats an admin
-- marked as testers; every other customer is left to the shop, as with the bot off.
--
-- * garage.meta_settings.bot_test_mode: the main switch is in test mode. Stored apart from
--   bot_enabled, which keeps meaning "answers everyone": code that knows only bot_enabled (an older
--   worker, a rollback) reads test mode as off and answers nobody. Never both
--   (meta_settings_one_bot_mode); turning the bot off ends test mode.
-- * garage.meta_settings.bot_test_at: when the bot began answering testers (off -> test or
--   off -> on). Kept while the bot stays on in either mode, so a tester's reply under way survives
--   test -> on. bot_enabled_at still stamps each change to on: customers who wrote during a test
--   are never bulk-answered (automotive.meta_store.answered_since).
-- * garage.messenger_threads.bot_tester: an admin marked this chat as a tester (dashboard,
--   Facebook > Chats). It goes with the chat ("Delete data").
-- * garage.messenger_replies.status: one more value, skipped_not_tester (test mode, not a tester).
-- * garage.meta_provision() is redefined whole: 0017's body, with bot_tester in the dashboard
--   login's UPDATE grant on garage.messenger_threads.

ALTER TABLE garage.meta_settings
    ADD COLUMN bot_test_mode BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN bot_test_at TIMESTAMPTZ,
    ADD CONSTRAINT meta_settings_one_bot_mode CHECK (NOT (bot_enabled AND bot_test_mode));
-- A bot already on for everyone answers testers since the same moment.
UPDATE garage.meta_settings SET bot_test_at = bot_enabled_at WHERE bot_enabled;

ALTER TABLE garage.messenger_threads ADD COLUMN bot_tester BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE garage.messenger_replies DROP CONSTRAINT messenger_replies_status_check,
    ADD CONSTRAINT messenger_replies_status_check CHECK (status IN (
        'pending', 'sent', 'skipped_off', 'skipped_chat_off', 'skipped_old',
        'skipped_shop_answered', 'skipped_superseded', 'skipped_cap', 'skipped_paused',
        'failed', 'blocked_by_meta', 'fallback_sent', 'paused_provider', 'abandoned',
        'skipped_not_tester'));

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
