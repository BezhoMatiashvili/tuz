-- Garage 1886 on Facebook: Messenger bot, comment replies, page stats and read-only ad results.
-- New tables in the private garage schema (0012). Three capability roles reach them, each only as
-- far as its job needs:
--   garage_webhook    the public receiver: appends signature-verified webhook events, nothing else.
--   garage_messenger  the local worker: reads those events, runs the bot and the syncs, and reads
--                     the site's brand and services for the shop facts the assistant quotes.
--   garage_site       the dashboard: switches, the Facebook connection, read access to the rest.
-- Access tokens are stored AES-256-GCM encrypted by the application (automotive/meta_crypto.py);
-- the key never reaches the database. No existing table changes shape; read policies are added to
-- garage.site_sections and garage.services, and an append policy to garage.audit_log, for the
-- worker.

-- Verified webhook items, one row each. The receiver may only append; event_key makes Meta's
-- retries and duplicate deliveries harmless.
CREATE TABLE garage.meta_inbox (
    inbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE CHECK (event_key ~ '^[a-z]{1,12}:[0-9a-f]{64}$'),
    field TEXT NOT NULL CHECK (field ~ '^[a-z_]{1,40}$'),
    page_id TEXT NOT NULL CHECK (page_id ~ '^[0-9]{1,30}$'),
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1000),
    outcome TEXT NOT NULL DEFAULT '' CHECK (char_length(outcome) <= 300)
);
CREATE INDEX meta_inbox_pending_idx ON garage.meta_inbox (inbox_id) WHERE processed_at IS NULL;
CREATE INDEX meta_inbox_received_idx ON garage.meta_inbox (received_at);
COMMENT ON TABLE garage.meta_inbox IS
    'Signature-verified Meta webhook items. Appended by the receiver, consumed by the worker.';

-- One row. Read fresh by the worker at claim time and again immediately before every send.
CREATE TABLE garage.meta_settings (
    settings_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (settings_id = 1),
    bot_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- Only customer messages received after this moment are answered (no backlog replies).
    bot_enabled_at TIMESTAMPTZ,
    bot_changed_at TIMESTAMPTZ,
    bot_changed_by TEXT NOT NULL DEFAULT '' CHECK (char_length(bot_changed_by) <= 80),
    -- Set by the worker when the AI provider account fails; cleared by an admin resume.
    bot_paused_reason TEXT NOT NULL DEFAULT ''
        CHECK (bot_paused_reason IN ('', 'ai_provider')),
    bot_paused_at TIMESTAMPTZ,
    -- The admin confirmed Meta Business Suite "Auto reply" is off when switching the bot on.
    auto_reply_confirmed_off BOOLEAN NOT NULL DEFAULT FALSE,
    comments_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    comments_enabled_at TIMESTAMPTZ,
    comments_changed_at TIMESTAMPTZ,
    comments_changed_by TEXT NOT NULL DEFAULT '' CHECK (char_length(comments_changed_by) <= 80),
    max_reply_age_minutes INTEGER NOT NULL DEFAULT 1440
        CHECK (max_reply_age_minutes BETWEEN 5 AND 1440),
    daily_turn_limit INTEGER NOT NULL DEFAULT 150 CHECK (daily_turn_limit BETWEEN 0 AND 5000),
    customer_daily_turn_limit INTEGER NOT NULL DEFAULT 30
        CHECK (customer_daily_turn_limit BETWEEN 0 AND 500),
    comment_public_reply TEXT NOT NULL DEFAULT '' CHECK (char_length(comment_public_reply) <= 300),
    -- Empty means the built-in text; the shop phone from the brand section is appended.
    fallback_ka TEXT NOT NULL DEFAULT '' CHECK (char_length(fallback_ka) <= 500),
    fallback_en TEXT NOT NULL DEFAULT '' CHECK (char_length(fallback_en) <= 500),
    retention_days INTEGER NOT NULL DEFAULT 365 CHECK (retention_days BETWEEN 30 AND 3650),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO garage.meta_settings (settings_id) VALUES (1);

-- One row: the connected page, the app that receives its webhooks, and the subscription state.
CREATE TABLE garage.meta_page (
    page_row SMALLINT PRIMARY KEY DEFAULT 1 CHECK (page_row = 1),
    page_id TEXT NOT NULL CHECK (page_id ~ '^[0-9]{1,30}$'),
    page_name TEXT NOT NULL DEFAULT '' CHECK (char_length(page_name) <= 200),
    app_id TEXT NOT NULL CHECK (app_id ~ '^[0-9]{1,30}$'),
    subscribed_fields TEXT[] NOT NULL DEFAULT '{}',
    subscribed_at TIMESTAMPTZ,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    connected_by TEXT NOT NULL DEFAULT '' CHECK (char_length(connected_by) <= 80),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
);

-- Encrypted access tokens by purpose: 'page' sends and reads for the page, 'user' is the
-- long-lived login token (page stats, ads), 'send_page' is an optional page token minted
-- through another app for sending only, 'ads_user' an optional login token of a separate ads app.
CREATE TABLE garage.meta_tokens (
    purpose TEXT PRIMARY KEY CHECK (purpose IN ('page', 'user', 'send_page', 'ads_user')),
    app_id TEXT NOT NULL CHECK (app_id ~ '^[0-9]{1,30}$'),
    subject_id TEXT NOT NULL CHECK (subject_id ~ '^[0-9]{1,30}$'),
    subject_name TEXT NOT NULL DEFAULT '' CHECK (char_length(subject_name) <= 200),
    token_enc TEXT NOT NULL CHECK (token_enc ~ '^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'
                                   AND char_length(token_enc) <= 4096),
    scopes TEXT[] NOT NULL DEFAULT '{}',
    expires_at TIMESTAMPTZ,              -- NULL: Meta reports no expiry
    data_access_expires_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'broken')),
    status_detail TEXT NOT NULL DEFAULT '' CHECK (char_length(status_detail) <= 300),
    checked_at TIMESTAMPTZ,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    connected_by TEXT NOT NULL DEFAULT '' CHECK (char_length(connected_by) <= 80)
);

-- One row per Messenger customer (page-scoped id). engine_state is the assistant's own state
-- for this chat, restored after a restart; it never reaches the customer.
CREATE TABLE garage.messenger_threads (
    psid TEXT PRIMARY KEY CHECK (psid ~ '^[0-9]{1,30}$'),
    page_id TEXT NOT NULL CHECK (page_id ~ '^[0-9]{1,30}$'),
    customer_name TEXT NOT NULL DEFAULT '' CHECK (char_length(customer_name) <= 200),
    conversation_id TEXT NOT NULL DEFAULT ''
        CHECK (conversation_id = '' OR conversation_id ~ '^t_[0-9A-Za-z_-]{1,120}$'),
    bot_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    -- Admin accounts may send '#bot off|on|status'.
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT '' CHECK (source IN ('', 'ads', 'shortlink', 'comment', 'other')),
    source_ad_id TEXT NOT NULL DEFAULT '' CHECK (source_ad_id = '' OR source_ad_id ~ '^[0-9]{1,30}$'),
    engine_state JSONB CHECK (engine_state IS NULL OR (jsonb_typeof(engine_state) = 'object'
                              AND octet_length(engine_state::text) <= 262144)),
    engine_state_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_customer_at TIMESTAMPTZ,
    last_shop_at TIMESTAMPTZ,
    last_bot_at TIMESTAMPTZ,
    needs_attention BOOLEAN NOT NULL DEFAULT FALSE,
    attention_reason TEXT NOT NULL DEFAULT '' CHECK (char_length(attention_reason) <= 120),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
);
CREATE INDEX messenger_threads_recent_idx
    ON garage.messenger_threads (last_customer_at DESC NULLS LAST);
CREATE INDEX messenger_threads_ad_idx ON garage.messenger_threads (source_ad_id)
    WHERE source_ad_id <> '';

-- Every message of a chat: the customer's, the bot's, and the shop's own (echoes typed in
-- Business Suite or sent by Meta's automations). sent_at is Meta's timestamp.
CREATE TABLE garage.messenger_messages (
    message_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mid TEXT NOT NULL UNIQUE CHECK (char_length(mid) BETWEEN 1 AND 200),
    psid TEXT NOT NULL REFERENCES garage.messenger_threads (psid) ON DELETE CASCADE,
    sender TEXT NOT NULL CHECK (sender IN ('customer', 'bot', 'shop')),
    body TEXT NOT NULL DEFAULT '' CHECK (char_length(body) <= 4000),
    attachments JSONB NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(attachments) = 'array' AND octet_length(attachments::text) <= 8192),
    quick_reply TEXT NOT NULL DEFAULT '' CHECK (char_length(quick_reply) <= 1000),
    referral_ad_id TEXT NOT NULL DEFAULT ''
        CHECK (referral_ad_id = '' OR referral_ad_id ~ '^[0-9]{1,30}$'),
    app_id TEXT NOT NULL DEFAULT '' CHECK (app_id = '' OR app_id ~ '^[0-9]{1,30}$'),
    sent_at TIMESTAMPTZ NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messenger_messages_thread_idx ON garage.messenger_messages (psid, sent_at, message_id);

-- One row per reply attempt; trigger_key is the newest customer message of the burst (or the
-- comment id), so the same burst is never answered twice.
CREATE TABLE garage.messenger_replies (
    reply_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('messenger', 'comment', 'command', 'notice')),
    psid TEXT CHECK (psid IS NULL OR psid ~ '^[0-9]{1,30}$'),
    trigger_key TEXT NOT NULL CHECK (char_length(trigger_key) BETWEEN 1 AND 200),
    trigger_keys TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'sent', 'skipped_off', 'skipped_chat_off', 'skipped_old',
        'skipped_shop_answered', 'skipped_superseded', 'skipped_cap', 'skipped_paused',
        'failed', 'blocked_by_meta', 'fallback_sent', 'paused_provider', 'abandoned')),
    error_code INTEGER,
    error_subcode INTEGER,
    error_text TEXT NOT NULL DEFAULT '' CHECK (char_length(error_text) <= 300),
    engine_status TEXT NOT NULL DEFAULT '' CHECK (char_length(engine_status) <= 40),
    sent_mids TEXT[] NOT NULL DEFAULT '{}',
    usage JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(usage) = 'object' AND octet_length(usage::text) <= 4096),
    cost_usd NUMERIC(10, 5) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    UNIQUE (channel, trigger_key)
);
CREATE INDEX messenger_replies_started_idx ON garage.messenger_replies (started_at DESC);
CREATE INDEX messenger_replies_psid_idx ON garage.messenger_replies (psid, started_at DESC);

-- Comments on the page's posts and ads. reply_status records the bot's private reply.
CREATE TABLE garage.meta_comments (
    comment_id TEXT PRIMARY KEY CHECK (comment_id ~ '^[0-9_]{1,80}$'),
    post_id TEXT NOT NULL CHECK (post_id ~ '^[0-9_]{1,80}$'),
    parent_id TEXT NOT NULL DEFAULT '' CHECK (parent_id = '' OR parent_id ~ '^[0-9_]{1,80}$'),
    author_id TEXT NOT NULL DEFAULT '' CHECK (author_id = '' OR author_id ~ '^[0-9]{1,30}$'),
    author_name TEXT NOT NULL DEFAULT '' CHECK (char_length(author_name) <= 200),
    body TEXT NOT NULL DEFAULT '' CHECK (char_length(body) <= 4000),
    created_time TIMESTAMPTZ NOT NULL,
    from_page BOOLEAN NOT NULL DEFAULT FALSE,
    is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
    permalink TEXT NOT NULL DEFAULT '' CHECK (char_length(permalink) <= 500),
    source TEXT NOT NULL CHECK (source IN ('webhook', 'backfill')),
    reply_status TEXT NOT NULL DEFAULT '' CHECK (char_length(reply_status) <= 40),
    replied_psid TEXT NOT NULL DEFAULT '' CHECK (replied_psid = '' OR replied_psid ~ '^[0-9]{1,30}$'),
    public_reply_id TEXT NOT NULL DEFAULT '' CHECK (char_length(public_reply_id) <= 80),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX meta_comments_created_idx ON garage.meta_comments (created_time DESC);
CREATE INDEX meta_comments_post_idx ON garage.meta_comments (post_id, created_time DESC);

-- Page statistics, one row per day. metrics holds whatever Meta's current insights metrics return.
CREATE TABLE garage.meta_page_daily (
    day DATE PRIMARY KEY,
    followers INTEGER CHECK (followers IS NULL OR followers >= 0),
    metrics JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(metrics) = 'object' AND octet_length(metrics::text) <= 16384),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Images the dashboard shows (post pictures, ad thumbnails), cached because the dashboard's CSP
-- only allows same-origin images. image_key is the SHA-256 of the source object's identity.
CREATE TABLE garage.meta_images (
    image_key CHAR(64) PRIMARY KEY CHECK (image_key ~ '^[0-9a-f]{64}$'),
    content_type TEXT NOT NULL
        CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp', 'image/gif')),
    data BYTEA NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 1048576),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE garage.meta_images ALTER COLUMN data SET STORAGE EXTERNAL;

CREATE TABLE garage.meta_posts (
    post_id TEXT PRIMARY KEY CHECK (post_id ~ '^[0-9_]{1,80}$'),
    created_time TIMESTAMPTZ NOT NULL,
    message TEXT NOT NULL DEFAULT '' CHECK (char_length(message) <= 2000),
    permalink TEXT NOT NULL DEFAULT '' CHECK (char_length(permalink) <= 500),
    image_key CHAR(64) REFERENCES garage.meta_images (image_key) ON DELETE SET NULL,
    reactions INTEGER NOT NULL DEFAULT 0 CHECK (reactions >= 0),
    comments INTEGER NOT NULL DEFAULT 0 CHECK (comments >= 0),
    shares INTEGER NOT NULL DEFAULT 0 CHECK (shares >= 0),
    metrics JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(metrics) = 'object' AND octet_length(metrics::text) <= 16384),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX meta_posts_created_idx ON garage.meta_posts (created_time DESC);

-- Read-only ad results: every ad account the owner's login can read, and the ads in them that
-- promote the Garage page.
CREATE TABLE garage.meta_ad_accounts (
    account_id TEXT PRIMARY KEY CHECK (account_id ~ '^[0-9]{1,30}$'),
    name TEXT NOT NULL DEFAULT '' CHECK (char_length(name) <= 200),
    currency TEXT NOT NULL DEFAULT '' CHECK (currency = '' OR currency ~ '^[A-Z]{3}$'),
    timezone_name TEXT NOT NULL DEFAULT '' CHECK (char_length(timezone_name) <= 80),
    account_status INTEGER,
    garage_ads INTEGER NOT NULL DEFAULT 0 CHECK (garage_ads >= 0),
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE garage.meta_ads (
    ad_id TEXT PRIMARY KEY CHECK (ad_id ~ '^[0-9]{1,30}$'),
    account_id TEXT NOT NULL REFERENCES garage.meta_ad_accounts (account_id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '' CHECK (char_length(name) <= 400),
    status TEXT NOT NULL DEFAULT '' CHECK (char_length(status) <= 40),
    effective_status TEXT NOT NULL DEFAULT '' CHECK (char_length(effective_status) <= 40),
    campaign_id TEXT NOT NULL DEFAULT '' CHECK (campaign_id = '' OR campaign_id ~ '^[0-9]{1,30}$'),
    campaign_name TEXT NOT NULL DEFAULT '' CHECK (char_length(campaign_name) <= 400),
    objective TEXT NOT NULL DEFAULT '' CHECK (char_length(objective) <= 60),
    adset_id TEXT NOT NULL DEFAULT '' CHECK (adset_id = '' OR adset_id ~ '^[0-9]{1,30}$'),
    adset_name TEXT NOT NULL DEFAULT '' CHECK (char_length(adset_name) <= 400),
    creative_id TEXT NOT NULL DEFAULT '' CHECK (creative_id = '' OR creative_id ~ '^[0-9]{1,30}$'),
    title TEXT NOT NULL DEFAULT '' CHECK (char_length(title) <= 400),
    body TEXT NOT NULL DEFAULT '' CHECK (char_length(body) <= 4000),
    story_id TEXT NOT NULL DEFAULT '' CHECK (story_id = '' OR story_id ~ '^[0-9_]{1,80}$'),
    image_key CHAR(64) REFERENCES garage.meta_images (image_key) ON DELETE SET NULL,
    created_time TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX meta_ads_account_idx ON garage.meta_ads (account_id);

CREATE TABLE garage.meta_ad_daily (
    ad_id TEXT NOT NULL REFERENCES garage.meta_ads (ad_id) ON DELETE CASCADE,
    day DATE NOT NULL,
    spend NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (spend >= 0),
    impressions BIGINT NOT NULL DEFAULT 0 CHECK (impressions >= 0),
    reach BIGINT NOT NULL DEFAULT 0 CHECK (reach >= 0),
    clicks BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0),
    messaging_started INTEGER NOT NULL DEFAULT 0 CHECK (messaging_started >= 0),
    actions JSONB NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(actions) = 'array' AND octet_length(actions::text) <= 16384),
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ad_id, day)
);
CREATE INDEX meta_ad_daily_day_idx ON garage.meta_ad_daily (day);

-- Worker job runs, and jobs the dashboard asks the worker to run now.
CREATE TABLE garage.meta_sync_runs (
    run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job TEXT NOT NULL CHECK (job ~ '^[a-z_]{1,40}$'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error', 'skipped')),
    records INTEGER NOT NULL DEFAULT 0 CHECK (records >= 0),
    detail TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 500)
);
CREATE INDEX meta_sync_runs_job_idx ON garage.meta_sync_runs (job, started_at DESC);

CREATE TABLE garage.meta_job_requests (
    request_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job TEXT NOT NULL CHECK (job ~ '^[a-z_]{1,40}$'),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by TEXT NOT NULL DEFAULT '' CHECK (char_length(requested_by) <= 80),
    taken_at TIMESTAMPTZ
);
CREATE INDEX meta_job_requests_open_idx ON garage.meta_job_requests (request_id)
    WHERE taken_at IS NULL;

-- Row-level security tests role membership instead of naming a role, so this migration succeeds
-- before the capability roles exist (same approach as 0012).
DO $$
DECLARE
    t TEXT;
    site CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_site'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    worker CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_messenger'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    hook CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_webhook'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    backup CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles '
        || 'WHERE rolname=''warehouse_backup_restore_operator'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
BEGIN
    FOREACH t IN ARRAY ARRAY['meta_inbox', 'meta_settings', 'meta_page', 'meta_tokens',
                             'messenger_threads', 'messenger_messages', 'messenger_replies',
                             'meta_comments', 'meta_page_daily', 'meta_images', 'meta_posts',
                             'meta_ad_accounts', 'meta_ads', 'meta_ad_daily', 'meta_sync_runs',
                             'meta_job_requests']
    LOOP
        EXECUTE format('ALTER TABLE garage.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY worker_access ON garage.%I FOR ALL USING (', t)
            || worker || ') WITH CHECK (' || worker || ')';
        EXECUTE format('CREATE POLICY site_access ON garage.%I FOR ALL USING (', t)
            || site || ') WITH CHECK (' || site || ')';
        EXECUTE format('CREATE POLICY backup_read ON garage.%I FOR SELECT USING (', t)
            || backup || ')';
    END LOOP;
    EXECUTE 'CREATE POLICY webhook_append ON garage.meta_inbox FOR INSERT WITH CHECK ('
        || hook || ')';
    -- The worker reads the brand and services the assistant quotes, and records #bot commands.
    EXECUTE 'CREATE POLICY worker_read ON garage.site_sections FOR SELECT USING (' || worker || ')';
    EXECUTE 'CREATE POLICY worker_read ON garage.services FOR SELECT USING (' || worker || ')';
    EXECUTE 'CREATE POLICY worker_append ON garage.audit_log FOR INSERT WITH CHECK ('
        || worker || ')';
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA garage FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA garage FROM PUBLIC;

-- Safe to rerun from provisioning when a role is created after this migration. It never changes
-- role membership. Privileges are the second fence after the policies above: the dashboard can
-- read the inbox's bookkeeping columns but never a payload, and can change only the switch columns
-- of a chat.
CREATE FUNCTION garage.meta_provision() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_webhook') THEN
        GRANT USAGE ON SCHEMA garage TO garage_webhook;
        GRANT INSERT (event_key, field, page_id, payload) ON garage.meta_inbox TO garage_webhook;
        EXECUTE format('GRANT USAGE ON SEQUENCE %s TO garage_webhook',
                       pg_get_serial_sequence('garage.meta_inbox', 'inbox_id'));
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
            garage.meta_sync_runs TO garage_messenger;
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
