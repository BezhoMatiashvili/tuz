"""Configure local automotive roles without copying or changing warehouse products.

Run from the project directory: python -m automotive --setup-local
This operation only accepts a loopback DATABASE_URL and an existing migrated catalog.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from warehouse_sync.migrations import MIGRATIONS_DIRECTORY, apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
AUTOMOTIVE_MIGRATION = "0006_automotive"


class LocalSetupError(RuntimeError):
    """A safe, actionable operator message containing no connection details."""


def local_admin_url(values: dict[str, Any]) -> str:
    """Reject remote hosts and libpq service/hostaddr overrides before connecting."""
    value = values.get("DATABASE_URL") or ""
    try:
        info = conninfo_to_dict(value)
    except psycopg.Error:
        raise LocalSetupError("DATABASE_URL is invalid; check local .env.") from None
    if (not value or info.get("host") not in {"127.0.0.1", "localhost", "::1"}
            or info.get("hostaddr") or info.get("service")
            or not info.get("dbname") or not info.get("user")):
        raise LocalSetupError("Local setup requires an explicit loopback DATABASE_URL in .env.")
    # Do not inherit ambient service/role/database overrides for an admin operation.
    info["host"] = "127.0.0.1" if info["host"] == "localhost" else info["host"]
    return make_conninfo(**{
        key: info[key] for key in ("host", "port", "dbname", "user", "password", "sslmode")
        if key in info
    }, hostaddr=info["host"], connect_timeout=5,
        options="-c application_name=automotive-local-setup -c lock_timeout=5000")


def _require_catalog(connection: Any) -> None:
    if not connection.execute(
        "SELECT to_regclass('warehouse.warehouse_products') IS NOT NULL "
        "AND to_regclass('public.warehouse_schema_migrations') IS NOT NULL"
    ).fetchone()[0]:
        raise LocalSetupError(
            "Local warehouse is not migrated. Run `uv run python -m warehouse_sync --migrate` "
            "with the local migration-owner connection first."
        )
    applied = {row[0] for row in connection.execute(
        "SELECT version FROM public.warehouse_schema_migrations"
    )}
    required = {
        path.name.removesuffix(".up.sql") for path in MIGRATIONS_DIRECTORY.glob("*.up.sql")
        if path.name.removesuffix(".up.sql") < AUTOMOTIVE_MIGRATION
    }
    if not required.issubset(applied):
        raise LocalSetupError(
            "Earlier warehouse migrations are pending. Complete the warehouse migration "
            "procedure first; automotive setup will not modify existing warehouse tables."
        )


def _new_login(connection: Any, capability: str, admin_url: str) -> str:
    name = "auto_local_" + ("reader_" if capability == "automotive_runtime" else "enrich_")
    name += secrets.token_hex(6)
    password = secrets.token_urlsafe(48)
    verifier = connection.pgconn.encrypt_password(
        password.encode("ascii"), name.encode("ascii"), b"scram-sha-256",
    ).decode("ascii")
    connection.execute(sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS "
        "INHERIT PASSWORD {}"
    ).format(sql.Identifier(name), sql.Literal(verifier)))
    connection.execute(sql.SQL("GRANT {} TO {}").format(
        sql.Identifier(capability), sql.Identifier(name),
    ))
    info = conninfo_to_dict(admin_url)
    return make_conninfo(**{
        key: info[key] for key in ("host", "hostaddr", "port", "dbname", "sslmode") if key in info
    }, user=name, password=password, connect_timeout=5)


def configure_local(env_path: Path | None = None) -> dict[str, bool]:
    env_path = env_path or ROOT / ".env"
    if not env_path.is_file():
        raise LocalSetupError("Copy .env.example to .env and configure local PostgreSQL first.")
    values = dotenv_values(env_path)
    admin_url = local_admin_url(values)
    existing = [bool(values.get(key)) for key in (
        "AUTOMOTIVE_DATABASE_URL", "AUTOMOTIVE_ENRICHMENT_DATABASE_URL",
    )]
    if any(existing):
        if not all(existing):
            raise LocalSetupError(
                "Partial automotive database configuration exists; complete both URLs manually. "
                "Existing credentials were preserved."
            )
        raise LocalSetupError(
            "Automotive database URLs are already configured; existing credentials were preserved."
        )
    try:
        with psycopg.connect(admin_url) as connection:
            # Keep setup transactional and serialize concurrent invocations in this DB.
            connection.execute("SELECT pg_advisory_xact_lock(1728036741)")
            _require_catalog(connection)
            provision = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text()
            connection.execute(provision)
            apply_migrations_on_connection(connection, through=AUTOMOTIVE_MIGRATION)
            runtime_url = _new_login(connection, "automotive_runtime", admin_url)
            enrichment_url = _new_login(connection, "automotive_enrichment", admin_url)
    except psycopg.Error:
        raise LocalSetupError(
            "Local PostgreSQL setup failed. Check DATABASE_URL, the running postgres service, "
            "and local admin privileges. Credentials and server errors were withheld."
        ) from None
    # Values never go through stdout, shell arguments or application logs.
    os.chmod(env_path, 0o600)
    updates = {
        "AUTOMOTIVE_DATABASE_URL": runtime_url,
        "AUTOMOTIVE_ENRICHMENT_DATABASE_URL": enrichment_url,
    }
    for key in ("AUTOMOTIVE_VIN_HMAC_KEY", "AUTOMOTIVE_OPERATOR_TOKEN"):
        if not values.get(key):
            updates[key] = secrets.token_urlsafe(48)
    for key, value in updates.items():
        set_key(str(env_path), key, value)
    return {"database_configured": True, "anthropic_configured": bool(values.get(
        "ANTHROPIC_API_KEY"
    ))}
