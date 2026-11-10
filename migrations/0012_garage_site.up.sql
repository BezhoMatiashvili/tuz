-- Garage 1886 public site: staff accounts, browser sessions, editable site content,
-- services, gallery images and an audit trail. Private application schema: it holds
-- password hashes and session digests, so it is never a Data API schema, and the chat
-- runtime role (automotive_runtime) receives nothing here. Does not alter existing tables.
CREATE SCHEMA garage;
REVOKE ALL ON SCHEMA garage FROM PUBLIC;
COMMENT ON SCHEMA garage IS
    'Garage 1886 site: staff authentication and editable content. Private; never a Data API schema.';

CREATE TABLE garage.staff_users (
    user_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'),
    display_name TEXT NOT NULL DEFAULT '' CHECK (char_length(display_name) <= 80),
    role TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
    password_hash TEXT NOT NULL
        CHECK (password_hash ~ '^scrypt\$' AND char_length(password_hash) <= 200),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
COMMENT ON TABLE garage.staff_users IS
    'Dashboard accounts. Accounts are deactivated, never deleted, so audit rows keep their actor.';

-- Only the SHA-256 of the cookie token is stored; a database read cannot replay a session.
CREATE TABLE garage.staff_sessions (
    token_hash CHAR(64) PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id BIGINT NOT NULL REFERENCES garage.staff_users (user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX staff_sessions_user_idx ON garage.staff_sessions (user_id);
CREATE INDEX staff_sessions_expires_idx ON garage.staff_sessions (expires_at);

-- One validated JSON document per landing-page section; the application owns the shape.
CREATE TABLE garage.site_sections (
    section_key TEXT PRIMARY KEY CHECK (section_key ~ '^[a-z][a-z0-9_]{1,31}$'),
    document JSONB NOT NULL
        CHECK (jsonb_typeof(document) = 'object' AND octet_length(document::text) <= 32768),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by BIGINT REFERENCES garage.staff_users (user_id) ON DELETE SET NULL
);

CREATE TABLE garage.services (
    service_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icon TEXT NOT NULL DEFAULT 'wrench' CHECK (icon ~ '^[a-z0-9-]{1,40}$'),
    title_ka TEXT NOT NULL CHECK (char_length(title_ka) BETWEEN 1 AND 60),
    title_en TEXT NOT NULL CHECK (char_length(title_en) BETWEEN 1 AND 60),
    summary_ka TEXT NOT NULL DEFAULT '' CHECK (char_length(summary_ka) <= 200),
    summary_en TEXT NOT NULL DEFAULT '' CHECK (char_length(summary_en) <= 200),
    price_from NUMERIC(10, 2) CHECK (price_from IS NULL OR price_from >= 0),
    position INTEGER NOT NULL DEFAULT 0,
    is_published BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX services_position_idx ON garage.services (position, service_id);

-- Gallery images, addressed by content hash. Bytes are re-encoded in the dashboard before
-- upload; the application checks the magic bytes and the size again.
CREATE TABLE garage.media (
    media_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    data BYTEA NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 1572864),
    width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 4096),
    height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 4096),
    alt_ka TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_ka) <= 160),
    alt_en TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_en) <= 160),
    position INTEGER NOT NULL DEFAULT 0,
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by BIGINT REFERENCES garage.staff_users (user_id) ON DELETE SET NULL
);
ALTER TABLE garage.media ALTER COLUMN data SET STORAGE EXTERNAL;
CREATE INDEX media_position_idx ON garage.media (position, media_id);

-- Append-only for the application role: it may insert and read, never change or delete.
CREATE TABLE garage.audit_log (
    audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id BIGINT,
    actor_username TEXT NOT NULL DEFAULT '' CHECK (char_length(actor_username) <= 32),
    action TEXT NOT NULL CHECK (action ~ '^[a-z][a-z0-9_.]{1,47}$'),
    target TEXT NOT NULL DEFAULT '' CHECK (char_length(target) <= 120),
    detail JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 4096),
    client_ip INET
);
CREATE INDEX audit_log_occurred_idx ON garage.audit_log (occurred_at DESC);

-- Policies test role membership instead of naming a role, so this migration succeeds
-- before the capability roles exist (same approach as 0006, 0008 and 0009).
DO $$
DECLARE
    t TEXT;
    site CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''garage_site'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
    backup CONSTANT TEXT := 'EXISTS (SELECT 1 FROM pg_roles '
        || 'WHERE rolname=''warehouse_backup_restore_operator'' '
        || 'AND pg_has_role(current_user,oid,''member''))';
BEGIN
    FOREACH t IN ARRAY ARRAY['staff_users','staff_sessions','site_sections','services','media']
    LOOP
        EXECUTE format('ALTER TABLE garage.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY site_access ON garage.%I FOR ALL USING (', t)
            || site || ') WITH CHECK (' || site || ')';
        EXECUTE format('CREATE POLICY backup_read ON garage.%I FOR SELECT USING (', t)
            || backup || ')';
    END LOOP;
    ALTER TABLE garage.audit_log ENABLE ROW LEVEL SECURITY;
    EXECUTE 'CREATE POLICY site_read ON garage.audit_log FOR SELECT USING (' || site || ')';
    EXECUTE 'CREATE POLICY site_append ON garage.audit_log FOR INSERT WITH CHECK ('
        || site || ')';
    EXECUTE 'CREATE POLICY backup_read ON garage.audit_log FOR SELECT USING (' || backup || ')';
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA garage FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA garage FROM PUBLIC;

-- Safe to rerun from provisioning when the role is created after this migration. It never
-- changes role membership. The site role reads sync runs for the dashboard's sync-health
-- tile and, when the trade module is installed, its inferred sales for the admin-only
-- reports; neither is reachable from an unauthenticated route.
CREATE FUNCTION garage.provision() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'garage_site') THEN
        GRANT USAGE ON SCHEMA garage TO garage_site;
        GRANT SELECT, INSERT, UPDATE ON garage.staff_users, garage.site_sections TO garage_site;
        GRANT SELECT, INSERT, UPDATE, DELETE ON garage.staff_sessions, garage.services,
            garage.media TO garage_site;
        GRANT SELECT, INSERT ON garage.audit_log TO garage_site;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA garage TO garage_site;
        IF to_regclass('warehouse.warehouse_sync_runs') IS NOT NULL THEN
            GRANT USAGE ON SCHEMA warehouse TO garage_site;
            GRANT SELECT ON warehouse.warehouse_sync_runs TO garage_site;
        END IF;
        IF to_regclass('trade.sales') IS NOT NULL
                AND to_regclass('trade.purchases') IS NOT NULL THEN
            GRANT USAGE ON SCHEMA trade TO garage_site;
            GRANT SELECT ON trade.sales, trade.purchases TO garage_site;
        END IF;
    END IF;
    -- Site content and accounts cannot be rebuilt from the source, so backups include them.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_backup_restore_operator') THEN
        GRANT USAGE ON SCHEMA garage TO warehouse_backup_restore_operator;
        GRANT SELECT ON ALL TABLES IN SCHEMA garage TO warehouse_backup_restore_operator;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA garage
            TO warehouse_backup_restore_operator;
    END IF;
END $$;
REVOKE ALL ON FUNCTION garage.provision() FROM PUBLIC;
SELECT garage.provision();
