"""Sales and purchases inferred from stock changes, on the disposable PostgreSQL service."""

import hashlib
import json
import os
import unittest
import uuid
from decimal import Decimal
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


def stock(store, tuz11, price="10", tuz6="0"):
    return {"column_06": price, "column_10": store, "column_11": tuz6, "column_12": tuz11}


@unittest.skipUnless(DATABASE_URL and psycopg, "requires the disposable PostgreSQL service")
class SalesPurchasingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute((ROOT / "ops/sql/provision_production_roles.sql").read_text())
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_migration_owner")
            apply_migrations_on_connection(connection)

    def setUp(self):
        self.connection = psycopg.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)
        self.addCleanup(self.connection.rollback)
        self.connection.execute("SET ROLE warehouse_migration_owner")
        self.run_id = self.start_run()
        # Products first recorded by this run are starting stock unless a test says otherwise.
        self.connection.execute("DELETE FROM trade.baseline")
        self.connection.execute("INSERT INTO trade.baseline (run_id) VALUES (%s)", (self.run_id,))

    def start_run(self):
        run_id = self.connection.execute("SELECT warehouse.start_sync_run(1)").fetchone()[0]
        self.connection.execute("SELECT warehouse.record_page_seen(%s, 1)", (run_id,))
        return run_id

    def record(self, product_id, columns, run_id=None):
        canonical = json.dumps({"id": product_id, "columns": columns}, sort_keys=True)
        return self.connection.execute(
            """SELECT warehouse.record_product(
                %s, 1, %s, 'SKU', 'Product', 'Model', '', '', 'Brand', %s, %s, %s)""",
            (run_id or self.run_id, product_id, Jsonb(columns),
             hashlib.sha256(canonical.encode()).hexdigest(), "b" * 64),
        ).fetchone()[0]

    def sales(self, product_id):
        return self.connection.execute(
            """SELECT quantity, store_before, store_after, tuz11_before, tuz11_after,
                      unit_price_raw, unit_price, amount, detected_run_id
               FROM trade.sales WHERE source_product_id = %s ORDER BY sale_id""",
            (product_id,),
        ).fetchall()

    def purchases(self, product_id):
        return self.connection.execute(
            """SELECT quantity, store_before, store_after, tuz11_before, tuz11_after,
                      is_new_product, period_start IS NULL, detected_run_id
               FROM trade.purchases WHERE source_product_id = %s ORDER BY purchase_id""",
            (product_id,),
        ).fetchall()

    def test_stock_drop_is_a_sale_at_the_previous_price(self):
        product_id = "trade-" + uuid.uuid4().hex
        self.assertEqual(self.record(product_id, stock("5", "3", price="240.50")), "added")
        self.assertEqual(self.record(product_id, stock("4", "1", price="260")), "changed")

        self.assertEqual(self.sales(product_id), [(
            Decimal("3"), Decimal("5"), Decimal("4"), Decimal("3"), Decimal("1"),
            "240.50", Decimal("240.50"), Decimal("721.50"), self.run_id,
        )])
        self.assertEqual(self.purchases(product_id), [])

    def test_stock_rise_is_a_purchase(self):
        product_id = "trade-" + uuid.uuid4().hex
        self.record(product_id, stock("1", "0"))
        self.record(product_id, stock("1", "6"))

        self.assertEqual(self.purchases(product_id), [(
            Decimal("6"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("6"),
            False, False, self.run_id,
        )])
        self.assertEqual(self.sales(product_id), [])

    def test_moves_between_store_and_tuz11_count_only_the_net_change(self):
        transfer_id = "trade-" + uuid.uuid4().hex
        self.record(transfer_id, stock("0", "10"))
        self.assertEqual(self.record(transfer_id, stock("10", "0")), "changed")
        self.assertEqual((self.sales(transfer_id), self.purchases(transfer_id)), ([], []))

        mixed_id = "trade-" + uuid.uuid4().hex
        self.record(mixed_id, stock("2", "5"))
        self.record(mixed_id, stock("5", "0"))
        sale = self.sales(mixed_id)
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale[0][:5], (
            Decimal("2"), Decimal("2"), Decimal("5"), Decimal("5"), Decimal("0"),
        ))

    def test_price_tuz6_and_unchanged_records_create_nothing(self):
        product_id = "trade-" + uuid.uuid4().hex
        self.record(product_id, stock("3", "0", price="10"))
        self.assertEqual(self.record(product_id, stock("3", "0", price="10")), "unchanged")
        self.assertEqual(self.record(product_id, stock("3", "0", price="12")), "changed")
        self.assertEqual(self.record(product_id, stock("3", "0", price="12", tuz6="-4")), "changed")

        self.assertEqual((self.sales(product_id), self.purchases(product_id)), ([], []))

    def test_negative_fractional_and_text_quantities(self):
        product_id = "trade-" + uuid.uuid4().hex
        self.record(product_id, stock("1.5", "-2", price="1,200"))
        self.record(product_id, stock("0,5", "-2"))
        self.record(product_id, stock("abc", "-2"))
        self.record(product_id, stock("4", "-2"))

        self.assertEqual(self.sales(product_id), [(
            Decimal("1"), Decimal("1.5"), Decimal("0.5"), Decimal("-2"), Decimal("-2"),
            "1,200", None, None, self.run_id,
        )])
        self.assertEqual(self.purchases(product_id), [])

    def test_new_product_after_the_baseline_is_a_purchase(self):
        starting_id = "trade-" + uuid.uuid4().hex
        self.record(starting_id, stock("4", "0"))
        self.assertEqual(self.purchases(starting_id), [])

        later_run = self.start_run()
        new_id = "trade-" + uuid.uuid4().hex
        empty_id = "trade-" + uuid.uuid4().hex
        self.assertEqual(self.record(new_id, stock("2", "3"), run_id=later_run), "added")
        self.record(empty_id, stock("0", "0"), run_id=later_run)

        self.assertEqual(self.purchases(new_id), [(
            Decimal("5"), Decimal("0"), Decimal("2"), Decimal("0"), Decimal("3"),
            True, True, later_run,
        )])
        self.assertEqual(self.purchases(empty_id), [])

    def test_first_finished_run_sets_the_baseline(self):
        self.connection.execute("DELETE FROM trade.baseline")
        failed_run = self.start_run()
        self.connection.execute(
            "SELECT warehouse.fail_sync_run(%s, 0, 0, 0, 0, 0, 'sync_error')", (failed_run,)
        )
        self.assertIsNone(self.connection.execute("SELECT run_id FROM trade.baseline").fetchone())

        first_id = "trade-" + uuid.uuid4().hex
        self.record(first_id, stock("7", "0"))
        self.connection.execute(
            "SELECT warehouse.finish_sync_run(%s, FALSE, 1, 1, 1, 0, 0, 0)", (self.run_id,)
        )
        self.assertEqual(self.purchases(first_id), [])

        later_run = self.start_run()
        later_id = "trade-" + uuid.uuid4().hex
        self.record(later_id, stock("1", "0"), run_id=later_run)
        self.connection.execute(
            "SELECT warehouse.finish_sync_run(%s, FALSE, 1, 1, 1, 0, 0, 0)", (later_run,)
        )

        self.assertEqual(
            self.connection.execute("SELECT run_id FROM trade.baseline").fetchone()[0],
            self.run_id,
        )
        self.assertEqual(len(self.purchases(later_id)), 1)

    def test_writer_records_sales_but_cannot_read_them(self):
        product_id = "trade-" + uuid.uuid4().hex
        self.record(product_id, stock("5", "0"))

        self.connection.execute("SET ROLE warehouse_sync_writer")
        self.assertEqual(self.record(product_id, stock("3", "0")), "changed")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connection.transaction():
                self.connection.execute("SELECT * FROM trade.sales")

        self.connection.execute("SET ROLE warehouse_migration_owner")
        self.assertEqual(len(self.sales(product_id)), 1)


if __name__ == "__main__":
    unittest.main()
