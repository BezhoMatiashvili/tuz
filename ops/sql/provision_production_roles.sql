-- Run once as the Supabase/Lightsail database administrator, before 0002.
-- It creates capability roles only; passwords and LOGIN grants are provisioned
-- separately through the secrets process, never in this repository.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_migration_owner') THEN
        CREATE ROLE warehouse_migration_owner LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_sync_writer') THEN
        CREATE ROLE warehouse_sync_writer LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_backup_restore_operator') THEN
        CREATE ROLE warehouse_backup_restore_operator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

DO $$
BEGIN
    EXECUTE format('GRANT warehouse_migration_owner TO %I', current_user);
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO warehouse_migration_owner, warehouse_sync_writer, warehouse_backup_restore_operator',
        current_database()
    );
    EXECUTE format('GRANT CREATE ON DATABASE %I TO warehouse_migration_owner', current_database());
END;
$$;

ALTER ROLE warehouse_sync_writer SET search_path TO warehouse, pg_catalog;
ALTER ROLE warehouse_backup_restore_operator SET search_path TO warehouse, pg_catalog;
GRANT USAGE, CREATE ON SCHEMA public TO warehouse_migration_owner;

-- Login attributes and a distinct randomly generated password are set only on
-- the appropriate deployment identity after this script. Do not grant either
-- worker role membership in warehouse_migration_owner.
