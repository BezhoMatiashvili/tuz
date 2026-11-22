-- Back to 0018: the prompt edits and their history are dropped, so the bot sends the packaged
-- prompts again (a running process notices within its refresh time, 30 s), and 0018's provisioning
-- is restored. Export any edit worth keeping from the dashboard before running this.
DROP TABLE IF EXISTS garage.llm_prompt_history;
DROP TABLE IF EXISTS garage.llm_prompts;

-- 0018's function, unchanged.
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
