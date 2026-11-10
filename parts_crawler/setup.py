"""Local-only setup: migrate through 0011 and create a dedicated parts_crawler login."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.local_setup import LocalSetupError, _require_catalog, local_admin_url
from parts_crawler.config import ROOT
from warehouse_sync.migrations import apply_migrations_on_connection

CRAWLER_MIGRATION = "0011_vehicle_detail"
ENV_KEY = "PARTS_CRAWLER_DATABASE_URL"


def _new_login(connection: psycopg.Connection, admin_url: str) -> str:
    name = "parts_local_crawler_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(48)
    verifier = connection.pgconn.encrypt_password(
        password.encode("ascii"), name.encode("ascii"), b"scram-sha-256").decode("ascii")
    connection.execute(sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS "
        "INHERIT PASSWORD {}").format(sql.Identifier(name), sql.Literal(verifier)))
    connection.execute(sql.SQL("GRANT parts_crawler TO {}").format(sql.Identifier(name)))
    info = conninfo_to_dict(admin_url)
    return make_conninfo(**{
        key: info[key] for key in ("host", "hostaddr", "port", "dbname", "sslmode") if key in info
    }, user=name, password=password, connect_timeout=5)


def admin_url(env_path: Path | None = None) -> str:
    env_path = env_path or ROOT / ".env"
    if not env_path.is_file():
        raise LocalSetupError("Copy .env.example to .env and configure local PostgreSQL first.")
    return local_admin_url(dotenv_values(env_path))


def configure_local(env_path: Path | None = None) -> bool:
    """Return True when a new login was created; existing credentials are preserved."""
    env_path = env_path or ROOT / ".env"
    url = admin_url(env_path)
    existing = dotenv_values(env_path).get(ENV_KEY)
    try:
        with psycopg.connect(url) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(1728036741)")
            _require_catalog(connection)
            connection.execute((ROOT / "ops/sql/provision_automotive_roles.sql").read_text())
            apply_migrations_on_connection(connection, through=CRAWLER_MIGRATION)
            connection.execute("SELECT parts_catalog.provision()")
            created = None if existing else _new_login(connection, url)
    except psycopg.Error:
        raise LocalSetupError(
            "Local PostgreSQL setup failed. Check DATABASE_URL, the running postgres service "
            "and local admin privileges. Credentials and server errors were withheld."
        ) from None
    if created:
        os.chmod(env_path, 0o600)
        set_key(str(env_path), ENV_KEY, created)
    return bool(created)
