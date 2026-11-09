"""Analytics behavior on the disposable PostgreSQL service used by CI."""

import os
import unittest
import uuid
from pathlib import Path

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:
    psycopg = None
    Jsonb = None

if psycopg is not None:
    from warehouse_sync.migrations import apply_migrations_on_connection

DATABASE_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(DATABASE_URL and psycopg, "requires the disposable PostgreSQL service")
class AnalyticsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute((ROOT / "ops/sql/provision_production_roles.sql").read_text())
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_migration_owner")
            apply_migrations_on_connection(connection)

    def test_incremental_updates_backfill_and_retention(self):
        product_id = "analytics-" + uuid.uuid4().hex
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_migration_owner")
            run_id = connection.execute("SELECT warehouse.start_sync_run(1)").fetchone()[0]
            connection.execute("SELECT warehouse.record_page_seen(%s, 1)", (run_id,))

            def record(price, row_hash):
                return connection.execute(
                    """SELECT warehouse.record_product(
                        %s, 1, %s, 'SKU', 'Product', 'Model', '', '', 'Brand',
                        %s, %s, %s)""",
                    (run_id, product_id, Jsonb({"column_06": price}), row_hash * 64, "b" * 64),
                ).fetchone()[0]

            self.assertEqual(record("240.50", "a"), "added")
            self.assertEqual(record("invalid,price", "c"), "changed")
            self.assertEqual(record("invalid,price", "c"), "unchanged")
            connection.execute(
                "SELECT warehouse.finish_sync_run(%s, FALSE, 1, 3, 1, 1, 1, 0)", (run_id,)
            )
            product = connection.execute(
                """SELECT p.product_name, f.selling_price_raw, f.selling_price, f.is_active
                   FROM analytics.dim_product p
                   JOIN analytics.fact_product_current f USING (product_key)
                   WHERE p.source_product_id = %s""",
                (product_id,),
            ).fetchone()
            self.assertEqual(product, ("Product", "invalid,price", None, True))
            observations = connection.execute(
                """SELECT f.source_version_id, f.selling_price_raw, f.selling_price
                   FROM analytics.fact_product_observation f
                   JOIN analytics.dim_product p USING (product_key)
                   WHERE p.source_product_id = %s ORDER BY f.source_version_id""",
                (product_id,),
            ).fetchall()
            self.assertEqual(len(observations), 2)
            self.assertEqual(str(observations[0][2]), "240.50")
            self.assertIsNone(observations[1][2])
            status = connection.execute(
                "SELECT status, rows_seen FROM analytics.fact_sync_run WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            self.assertEqual(status, ("partial", 3))
            connection.execute("CALL analytics.refresh_all()")
            connection.execute("CALL analytics.refresh_all()")
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM analytics.fact_product_observation "
                    "WHERE source_version_id = ANY(%s)",
                    ([row[0] for row in observations],),
                ).fetchone()[0],
                2,
            )
            connection.execute(
                "DELETE FROM warehouse.warehouse_product_versions WHERE version_id = %s",
                (observations[0][0],),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM analytics.fact_product_observation "
                    "WHERE source_version_id = %s",
                    (observations[0][0],),
                ).fetchone()[0],
                0,
            )
            connection.execute(
                "UPDATE warehouse.warehouse_products SET is_active = false "
                "WHERE source_product_id = %s",
                (product_id,),
            )
            self.assertFalse(
                connection.execute(
                    """SELECT f.is_active FROM analytics.fact_product_current f
                   JOIN analytics.dim_product p USING (product_key)
                   WHERE p.source_product_id = %s""",
                    (product_id,),
                ).fetchone()[0]
            )
            connection.rollback()

    def test_writer_has_no_direct_analytics_access(self):
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_sync_writer")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM analytics.fact_product_current")
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
