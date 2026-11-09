"""Small, explicit forward-only SQL migration runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent.parent / "migrations"


def apply_migrations_on_connection(connection: Any, *, through: str | None = None) -> list[str]:
    """Apply immutable migrations through an already-authorized connection.

    `through` exists for controlled legacy upgrades that must stop after a
    preparatory migration (for example, signing historical rows before a later
    NOT NULL constraint). Normal production deployments omit it.
    """
    migration_paths = sorted(MIGRATIONS_DIRECTORY.glob("*.up.sql"))
    applied_now: list[str] = []
    with connection.transaction():
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS warehouse_schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    for migration_path in migration_paths:
        version = migration_path.name.removesuffix(".up.sql")
        if through is not None and version > through:
            break
        with connection.transaction():
            already_applied = connection.execute(
                "SELECT 1 FROM warehouse_schema_migrations WHERE version = %s", (version,)
            ).fetchone()
            if already_applied:
                continue
            connection.execute(migration_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO warehouse_schema_migrations (version) VALUES (%s)", (version,)
            )
            applied_now.append(version)
    return applied_now


def apply_migrations(database_url: str, *, through: str | None = None) -> list[str]:
    """Apply each immutable *.up.sql file exactly once and return applied versions."""
    with psycopg.connect(database_url) as connection:
        return apply_migrations_on_connection(connection, through=through)
