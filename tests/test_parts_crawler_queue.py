"""Durable queue semantics and least-privilege grants on a disposable PostgreSQL database."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from parts_crawler.records import CrossRef, Fitment, Link, ParseResult, Part, PartRef, Vehicle

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class CrawlQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from warehouse_sync.migrations import apply_migrations_on_connection

        cls.psycopg = psycopg
        cls.dbname = "crawl_test_" + uuid.uuid4().hex
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.dbname)))
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.dbname
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as conn:
            conn.execute((ROOT / "ops/sql/provision_automotive_roles.sql").read_text())
            conn.execute("GRANT automotive_runtime, automotive_enrichment, parts_crawler "
                         "TO CURRENT_USER")
            apply_migrations_on_connection(conn)

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql

        cls.admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
            sql.Identifier(cls.dbname)))
        cls.admin.close()

    def setUp(self):
        from parts_crawler.store import Store

        self.store = Store(self.url)
        self.source = "t" + uuid.uuid4().hex[:10]
        self.store.ensure_source(self.source, 3)

    def tearDown(self):
        self.store.close()

    def link(self, name: str, priority: int = 100) -> Link:
        return Link(f"https://example.com/{name}", "page", priority, {"trail": [name]})

    def test_enqueue_is_idempotent_and_claim_follows_priority(self):
        self.assertEqual(self.store.enqueue(self.source, [self.link("b", 5), self.link("a", 1)]),
                         2)
        self.assertEqual(self.store.enqueue(self.source, [self.link("a", 1)]), 0)
        first = self.store.claim(self.source, "w1", 300)
        second = self.store.claim(self.source, "w2", 300)
        self.assertEqual((first.url, first.attempts, first.context), (
            "https://example.com/a", 1, {"trail": ["a"]}))
        self.assertEqual(second.url, "https://example.com/b")
        self.assertIsNone(self.store.claim(self.source, "w3", 300))

    def test_retry_backoff_exhaustion_and_requeue(self):
        self.store.enqueue(self.source, [self.link("x")])
        item = self.store.claim(self.source, "w", 300)
        self.assertEqual(self.store.retry_later(item, "SERVER_ERROR", 3600, 6, 503), "pending")
        self.assertIsNone(self.store.claim(self.source, "w", 300))
        self.assertGreater(self.store.next_due_seconds(self.source), 3000)
        self.store.conn.execute("UPDATE parts_catalog.crawl_queue SET next_attempt_at = now() "
                                "WHERE source = %s", (self.source,))
        item = self.store.claim(self.source, "w", 300)
        self.assertEqual(item.attempts, 2)
        self.assertEqual(self.store.retry_later(item, "SERVER_ERROR", 1, 2), "failed")
        self.assertIsNone(self.store.next_due_seconds(self.source))
        self.assertEqual(self.store.requeue_failed(self.source), 1)
        self.assertEqual(self.store.claim(self.source, "w", 300).attempts, 1)

    def test_expired_lease_resumes_and_release_restores_attempts(self):
        self.store.enqueue(self.source, [self.link("crash")])
        crashed = self.store.claim(self.source, "dead-worker", 300)
        self.store.conn.execute("UPDATE parts_catalog.crawl_queue SET lease_until = now() - "
                                "interval '1 second' WHERE id = %s", (crashed.id,))
        resumed = self.store.claim(self.source, "new-worker", 300)
        self.assertEqual((resumed.id, resumed.attempts), (crashed.id, 2))
        self.store.mark(crashed, "done")  # stale lease token cannot finish the item
        self.store.release(resumed)
        again = self.store.claim(self.source, "w", 300)
        self.assertEqual((again.id, again.attempts), (crashed.id, 2))

    def test_claim_can_take_turns_between_two_priority_bands(self):
        self.store.enqueue(self.source, [self.link("taxonomy", 95), self.link("parts", 20)])
        first = self.store.claim(self.source, "w", 300, below=False)
        self.assertEqual(first.url, "https://example.com/taxonomy")
        self.assertIsNone(self.store.claim(self.source, "w", 300, below=False))
        second = self.store.claim(self.source, "w", 300, below=True)
        self.assertEqual(second.url, "https://example.com/parts")
        self.assertEqual(list(self.store.iter_vehicles(self.source)), [])

    def test_daily_budget(self):
        self.assertTrue(all(self.store.consume_budget(self.source) for _ in range(3)))
        self.assertFalse(self.store.consume_budget(self.source))

    def test_save_page_writes_catalog_once_and_queues_children(self):
        self.store.enqueue(self.source, [self.link("leaf")], depth=2)
        item = self.store.claim(self.source, "w", 300)
        part = Part("ACME", "AB-123/4", "Oil Filter", "aftermarket")
        result = ParseResult(
            links=[self.link("child")], parts=[part],
            vehicles=[Vehicle("car-1", "Toyota", "Camry", 2015, 2015)],
            fitments=[Fitment(part.ref, "car-1", position="Oil Filter")],
            cross_refs=[CrossRef(part.ref, PartRef("TOYOTA", "04152-YZZA1"), "oe")])
        counts = self.store.save_page(item, url=item.url, http_status=200,
                                      content_type="text/html", sha="a" * 64,
                                      storage_path="x.gz", size=10, parser_version="1",
                                      result=result)
        self.assertEqual(counts, {"links": 1, "parts": 2, "vehicles": 1, "fitments": 1,
                                  "xrefs": 1})
        self.assertEqual(self.store.write_result(self.source, result, item.url),
                         {"parts": 0, "vehicles": 0, "fitments": 0, "xrefs": 0})
        child = self.store.claim(self.source, "w", 300)
        self.assertEqual((child.url, child.depth), ("https://example.com/child", 3))
        self.assertEqual(self.store.queue_counts()[self.source]["done"], 1)

    def test_vehicle_detail_is_kept_and_odd_numbers_do_not_fail_the_write(self):
        full = Vehicle("car-9", "Toyota", "Corolla", 2013, 2016, model_code="NDE180L-DEFDXW",
                       engine_code="1NDTV", displacement_cc=1400, power_kw=66, fuel="diesel",
                       transmission="MTM", drive="FWD")
        self.store.write_result(self.source, ParseResult(vehicles=[full]), "https://x/1")
        # A later page that knows less must not blank what is stored; absurd numbers
        # become NULL instead of violating the column checks.
        sparse = Vehicle("car-9", "Toyota", "Corolla", 30000, None, displacement_cc=10 ** 7)
        self.store.write_result(self.source, ParseResult(vehicles=[sparse]), "https://x/2")
        row = self.store.conn.execute(
            "SELECT * FROM parts_catalog.vehicles WHERE source = %s", (self.source,)).fetchone()
        self.assertEqual((row["model_code"], row["engine_code"], row["displacement_cc"],
                          row["power_kw"], row["fuel"], row["transmission"], row["drive"]),
                         ("NDE180L-DEFDXW", "1NDTV", 1400, 66, "diesel", "MTM", "FWD"))
        self.assertEqual((row["year_from"], row["year_to"]), (2013, 2016))
        [found] = self.store.vehicles_for_seed(self.source, make="toyota", code="%NDE180%",
                                               year=2014)
        self.assertEqual(found["source_vehicle_id"], "car-9")
        self.assertEqual(self.store.vehicles_for_seed(self.source, year=1999), [])

    def test_rejected_records_fail_one_page_and_keep_it_for_reparse(self):
        self.store.enqueue(self.source, [self.link("bad")])
        item = self.store.claim(self.source, "w", 300)
        bad = ParseResult(links=[Link("https://example.com/next", "Bad Kind")],
                          vehicles=[Vehicle("car-1", "Toyota", "Camry")])
        counts = self.store.save_page(item, url=item.url, http_status=200,
                                      content_type="text/html", sha="b" * 64,
                                      storage_path="y.gz", size=5, parser_version="1",
                                      result=bad)
        self.assertEqual(counts["store_errors"], 1)
        row = self.store.conn.execute(
            "SELECT q.status, q.last_error, r.parser_version FROM parts_catalog.crawl_queue q "
            "JOIN parts_catalog.raw_pages r ON r.queue_id = q.id WHERE q.id = %s",
            (item.id,)).fetchone()
        self.assertEqual((row["status"], row["last_error"], row["parser_version"]),
                         ("failed", "STORE_ERROR:CheckViolation", None))
        self.assertEqual(self.store.catalog_counts().get(self.source, {}), {})
        self.assertEqual(len(self.store.pages_for_reparse(self.source, "2", 0)), 1)
        self.assertEqual(self.store.enqueue(self.source, [self.link("after")]), 1)

    def test_resume_helpers(self):
        self.assertTrue(self.store.schema_ready())
        self.assertTrue(self.store.lock_source(self.source))
        from parts_crawler.store import Store

        other = Store(self.url)
        try:
            self.assertFalse(other.lock_source(self.source))  # a second process stays out
        finally:
            other.close()
        self.store.enqueue(self.source, [self.link("left-running")])
        crashed = self.store.claim(self.source, "dead", 300)
        self.assertEqual(self.store.release_unfinished(self.source), 1)
        again = self.store.claim(self.source, "new", 300)
        # The attempt stays counted, so a page that kills the worker runs out of tries.
        self.assertEqual((again.id, again.attempts), (crashed.id, 2))
        self.assertEqual(self.store.checkpoint("t/" + self.source), {})
        self.store.set_checkpoint("t/" + self.source, {"rows": 4000, "sha": "abc"})
        self.store.set_checkpoint("t/" + self.source, {"rows": 6000, "sha": "abc"})
        self.assertEqual(self.store.checkpoint("t/" + self.source)["rows"], 6000)
        self.store.clear_checkpoint("t/" + self.source)
        self.assertEqual(self.store.checkpoint("t/" + self.source), {})

    def test_refresh_requeues_a_finished_url_with_new_context(self):
        self.store.enqueue(self.source, [self.link("vehicle")])
        item = self.store.claim(self.source, "w", 300)
        self.store.mark(item, "done")
        again = Link(item.url, "step", 5, {"filter": "brake"})
        self.assertEqual(self.store.enqueue(self.source, [again]), 0)
        self.assertEqual(self.store.enqueue(self.source, [again], refresh=True), 1)
        claimed = self.store.claim(self.source, "w", 300)
        self.assertEqual((claimed.kind, claimed.context, claimed.attempts),
                         ("step", {"filter": "brake"}, 1))
        self.store.mark(claimed, "skipped")
        self.assertEqual(self.store.requeue_failed(self.source), 0)
        self.assertEqual(self.store.requeue_failed(self.source, include_skipped=True), 1)
        self.assertEqual(self.store.reset_queue(self.source), 1)

    def test_bulk_writes_names_and_purge(self):
        from parts_crawler.records import VehicleName

        tag = self.source[1:9].upper()  # part numbers no other test uses
        self.assertEqual(self.store.bulk_parts(self.source, [
            ("Subaru", f"66{tag}", "FLYWHEEL"), ("SUBARU", f"66-{tag}", "duplicate"),
            ("Subaru", "no digits", "")], kind="oe"), 1)
        self.assertEqual(self.store.bulk_parts(self.source, [("Subaru", f"66{tag}", "x")]), 0)
        names = [VehicleName("Q1", "model", "ka", "ტოიოტა კოროლა", make="Toyota",
                             year_from=1966),
                 VehicleName("Q1", "model", "en", "Toyota Corolla", make="Toyota"),
                 VehicleName("Q1", "model", "en", " - ", make="Toyota"),
                 VehicleName("Q1", "model", "EN!", "bad language")]
        self.assertEqual(self.store.upsert_vehicle_names(self.source, names), 2)
        self.assertEqual(self.store.upsert_vehicle_names(self.source, names), 2)
        row = self.store.conn.execute(
            "SELECT count(*) AS n, max(make_norm) AS make FROM parts_catalog.vehicle_names "
            "WHERE source = %s", (self.source,)).fetchone()
        self.assertEqual((row["n"], row["make"]), (2, "TOYOTA"))
        part = Part("ACME", f"77{tag}", "Oil Filter", "aftermarket")
        self.store.write_result(self.source, ParseResult(
            parts=[part], cross_refs=[CrossRef(part.ref, PartRef("TOYOTA", f"88{tag}"),
                                               "oe")]), "https://x/3")
        preview = self.store.purge_source(self.source)
        self.assertEqual((preview["xrefs"], preview["names"], preview["parts"]), (1, 2, 3))
        self.assertEqual(self.store.catalog_counts()[self.source]["parts"], 3)
        self.store.purge_source(self.source, apply=True)
        self.assertNotIn(self.source, self.store.catalog_counts())
        self.assertEqual(self.store.purge_source(self.source)["names"], 0)

    def test_grants_follow_least_privilege(self):
        with self.psycopg.connect(self.url) as conn:
            conn.execute("SET ROLE automotive_runtime")
            conn.execute("SELECT count(*) FROM parts_catalog.vehicle_names").fetchone()
            conn.execute("SELECT count(*) FROM parts_catalog.wmi").fetchone()
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM parts_catalog.import_checkpoints")
        with self.psycopg.connect(self.url) as conn:
            conn.execute("SET ROLE automotive_runtime")
            conn.execute("SELECT count(*) FROM parts_catalog.parts").fetchone()
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM parts_catalog.crawl_queue")
        with self.psycopg.connect(self.url) as conn:
            conn.execute("SET ROLE parts_crawler")
            conn.execute("SELECT count(*) FROM parts_catalog.crawl_queue").fetchone()
            conn.execute("SELECT count(*) FROM automotive.part_numbers").fetchone()
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM warehouse.warehouse_products")


if __name__ == "__main__":
    unittest.main()
