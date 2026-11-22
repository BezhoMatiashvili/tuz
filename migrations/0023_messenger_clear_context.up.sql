-- Garage 1886 on Facebook: "Clear bot memory" for a tester's Messenger chat. A tester starts a new
-- test without the bot remembering the last one (the car, the VIN, what was asked and shown).
--
-- * garage.messenger_threads.context_cleared_at: when the chat's bot memory was last cleared, NULL
--   when never. The bot reads no customer message sent at or before it
--   (MetaStore.unanswered_customer_messages), and a turn that was running when the chat was
--   cleared neither sends its reply nor saves its state (the worker compares this value with the
--   one it read: automotive.messenger_worker). The messages themselves stay in
--   garage.messenger_messages: staff still see the whole chat on the dashboard.
-- * garage.messenger_clear_context(psid, version): empties engine_state and stamps the column, only
--   for a chat marked as a tester. It returns the chat's new version, NULL when there is no such
--   tester chat or (version given) the chat changed since it was read. It also marks everything
--   written so far as needing no reply (waiting_dismissed_id, 0016), so a message the bot will now
--   never answer does not ring as "waiting" on Live chats. SECURITY DEFINER: the dashboard login
--   has no UPDATE on engine_state, and gets none here; it can only empty a tester's.
-- * garage.meta_provision() is redefined whole: 0019's body plus EXECUTE on the function.

ALTER TABLE garage.messenger_threads ADD COLUMN context_cleared_at TIMESTAMPTZ;
COMMENT ON COLUMN garage.messenger_threads.context_cleared_at IS
    'When the bot''s memory of this chat was last cleared; the bot reads nothing sent before it.';

CREATE FUNCTION garage.messenger_clear_context(p_psid TEXT, p_version INTEGER DEFAULT NULL)
RETURNS INTEGER
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    UPDATE garage.messenger_threads t
       SET engine_state = NULL,
           -- Stamped like a save, so a comment reply that read the old state does not put it back
           -- (automotive.meta_comments.CommentStore.save_state compares this value).
           engine_state_at = clock_timestamp(),
           context_cleared_at = clock_timestamp(),
           waiting_dismissed_id = COALESCE(
               (SELECT max(m.message_id) FROM garage.messenger_messages m WHERE m.psid = t.psid),
               t.waiting_dismissed_id),
           updated_at = now(),
           version = t.version + 1
     WHERE t.psid = p_psid AND t.bot_tester AND (p_version IS NULL OR t.version = p_version)
 RETURNING t.version;
$$;
REVOKE ALL ON FUNCTION garage.messenger_clear_context(TEXT, INTEGER) FROM PUBLIC;

-- 0019's function with EXECUTE on the function above for the worker and the dashboard logins.
-- Provisioning reruns it (ops/sql/provision_automotive_roles.sql, messenger_setup --setup), so the
-- grants hold for a role created after this migration too.
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
        -- Clearing what the bot remembers of a tester's chat (0023).
        GRANT EXECUTE ON FUNCTION garage.messenger_clear_context(TEXT, INTEGER)
            TO garage_messenger;
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
        -- Dashboard > Facebook > Chats, "Clear bot memory" on a tester's chat. The login cannot
        -- write engine_state itself: the function only empties it, and only for a tester.
        GRANT EXECUTE ON FUNCTION garage.messenger_clear_context(TEXT, INTEGER)
            TO garage_site;
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
