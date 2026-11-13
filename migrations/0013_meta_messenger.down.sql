-- Destructive: removes the Facebook connection (encrypted tokens), every stored Messenger chat,
-- comment, page statistic and ad result, and the bot switches. Export garage.meta_* and
-- garage.messenger_* first if any of it must survive. Leaves 0012's tables as they were.
DROP POLICY IF EXISTS worker_read ON garage.site_sections;
DROP POLICY IF EXISTS worker_read ON garage.services;
DROP POLICY IF EXISTS worker_append ON garage.audit_log;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_messenger') THEN
        REVOKE SELECT ON garage.site_sections, garage.services FROM garage_messenger;
        REVOKE INSERT ON garage.audit_log FROM garage_messenger;
        REVOKE USAGE ON ALL SEQUENCES IN SCHEMA garage FROM garage_messenger;
        REVOKE USAGE ON SCHEMA garage FROM garage_messenger;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_webhook') THEN
        REVOKE USAGE ON SCHEMA garage FROM garage_webhook;
    END IF;
END $$;
DROP TABLE IF EXISTS garage.meta_ad_daily, garage.meta_ads, garage.meta_ad_accounts,
    garage.meta_posts, garage.meta_images, garage.meta_page_daily, garage.meta_comments,
    garage.messenger_replies, garage.messenger_messages, garage.messenger_threads,
    garage.meta_tokens, garage.meta_page, garage.meta_settings, garage.meta_inbox,
    garage.meta_sync_runs, garage.meta_job_requests;
DROP FUNCTION IF EXISTS garage.meta_provision();
