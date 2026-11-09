"""Apply migrations or backfill analytics in the local Compose database."""

from __future__ import annotations

import argparse
import sys

import psycopg
from dotenv import load_dotenv

from db_ui.__main__ import connect
from warehouse_sync.migrations import apply_migrations_on_connection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate", "refresh"))
    args = parser.parse_args()
    load_dotenv(override=False)
    try:
        with connect() as connection:
            if args.action == "migrate":
                applied = apply_migrations_on_connection(connection)
                message = "Applied migrations: " + (", ".join(applied) if applied else "none")
            else:
                connection.execute("CALL analytics.refresh_all()")
                message = "Analytics refresh completed."
        print(message)
    except psycopg.Error as exc:
        # SQLSTATE and the driver exception class identify database errors without
        # printing connection strings or server details that may contain row data.
        error_code = exc.sqlstate or "unavailable"
        print(
            f"Analytics operation failed: {type(exc).__name__} (SQLSTATE {error_code}). "
            "Check the database, migration state and role permissions.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError):
        print(
            "Analytics operation failed. Check the database, migration state and role permissions.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
