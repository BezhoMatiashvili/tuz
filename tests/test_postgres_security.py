"""Runs in CI against an ephemeral PostgreSQL service, never a shared database."""

import os
import unittest
from pathlib import Path

try:
    import psycopg
    from psycopg import errors
    from psycopg.types.json import Jsonb
except ImportError:  # The local unittest suite has no mandatory third-party install.
    psycopg = None
    errors = None
    Jsonb = None

if psycopg is not None:
    from warehouse_sync.migrations import apply_migrations_on_connection
else:
    apply_migrations_on_connection = None


ADMIN_DATABASE_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    ADMIN_DATABASE_URL and psycopg is not None,
    "requires psycopg and the disposable CI PostgreSQL service",
)
class PostgresPrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert ADMIN_DATABASE_URL is not None
        with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as connection:
            connection.execute((ROOT / "ops/sql/provision_production_roles.sql").read_text())
            connection.execute("GRANT warehouse_sync_writer TO CURRENT_USER")

        with psycopg.connect(ADMIN_DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_migration_owner")
            assert apply_migrations_on_connection is not None
            apply_migrations_on_connection(connection)
            connection.commit()

    def _writer_connection(self):
        assert ADMIN_DATABASE_URL is not None
        connection = psycopg.connect(ADMIN_DATABASE_URL, autocommit=True)
        connection.execute("SET ROLE warehouse_sync_writer")
        return connection

    def test_writer_cannot_perform_ddl_read_tables_or_delete_current_products(self) -> None:
        with self._writer_connection() as connection:
            assert errors is not None
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE warehouse.writer_must_not_create (id integer)")
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM warehouse.warehouse_products")
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM public.warehouse_schema_migrations")
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("DELETE FROM warehouse.warehouse_products")

    def test_writer_can_only_write_through_scoped_functions(self) -> None:
        with self._writer_connection() as connection:
            run_id = connection.execute("SELECT warehouse.start_sync_run(1)").fetchone()[0]
            connection.execute("SELECT warehouse.record_page_seen(%s, 1)", (run_id,))
            outcome = connection.execute(
                """
                SELECT warehouse.record_product(
                    %s, 1, 'source-1', 'code', 'name', 'model', 'original', 'duplicate', 'brand',
                    %s, %s, %s
                )
                """,
                (run_id, Jsonb({"column_01": "code"}), "a" * 64, "b" * 64),
            ).fetchone()[0]
            self.assertEqual(outcome, "added")
            connection.execute(
                "SELECT warehouse.finish_sync_run(%s, TRUE, 1, 1, 1, 0, 0, 0)", (run_id,)
            )
