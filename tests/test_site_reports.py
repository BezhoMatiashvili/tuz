"""Dashboard figures: inventory overview, sync health, inferred trade and chat volume."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from automotive import chat_log, site_catalog
from automotive.chat_log import ChatLogger
from automotive.site_catalog import Catalog
from automotive.site_reports import (
    CATEGORY_LABELS,
    PRICE_BINS,
    chat_report,
    inventory_overview,
    sync_health,
    trade_report,
)
from tests.test_site_catalog import NOW, ROWS, row

MARKER = "private-marker-9f3c"


def counts(rows: list[dict]) -> dict[str, int]:
    return {entry["key"]: entry["count"] for entry in rows}


class InventoryOverviewTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)
        self.overview = inventory_overview(self.catalog)

    def test_tiles_count_items_states_and_units(self):
        self.assertEqual(self.overview["tiles"], {
            "items": 8, "available": 6, "in_store": 3, "warehouse_only": 3, "out_of_stock": 1,
            "store_units": 6.0, "warehouse_units": 15.5, "unpriced": 2})

    def test_stock_value_is_units_times_price_for_priced_available_items(self):
        # 3 x 45.50 in the store, 7 x 45.50 and 2.5 x 850 in the warehouse, 1 + 1 at 30.
        # The out-of-stock item, the one with unreadable stock and the two unpriced
        # ones add nothing.
        self.assertEqual(self.overview["stock_value"], 2640.0)

    def test_brands_makes_and_categories_are_counted_over_available_items(self):
        self.assertEqual(counts(self.overview["brands"]),
                         {"SATO": 2, "SAKURA": 2, "MANN": 1, "BREMBO": 1})
        self.assertEqual(counts(self.overview["makes"]),
                         {"toyota": 3, "honda": 2, "lexus": 1, "bmw": 1})
        self.assertEqual(self.overview["makes"][0],
                         {"key": "toyota", "count": 3, "name": "Toyota"})
        self.assertEqual(counts(self.overview["categories"]),
                         {"brake_pads": 2, "air_filter": 2, "oil_filter": 1, "brake_discs": 1})
        self.assertEqual(self.overview["uncategorised"], 0)
        for entry in self.overview["categories"]:
            self.assertEqual(entry["label"], dict(zip(("ka", "en"),
                                                      CATEGORY_LABELS[entry["key"]],
                                                      strict=True)))

    def test_price_bins_cover_every_priced_item_once(self):
        prices = self.overview["prices"]
        self.assertEqual([(entry["from"], entry["to"]) for entry in prices],
                         [(0, 25), (25, 50), (50, 100), (100, 200), (200, 400), (400, 800),
                          (800, None)])
        self.assertEqual([entry["count"] for entry in prices], [0, 3, 0, 0, 0, 0, 1])
        self.assertEqual(sum(entry["count"] for entry in prices) + 2, 6)  # plus the unpriced
        edges = Catalog.from_rows([
            row(f"e{index}", f"Part {index}", "", "ACME", price, "1", "0")
            for index, price in enumerate(("24.99", "25", "799.99", "800"))])
        self.assertEqual([entry["count"] for entry in inventory_overview(edges)["prices"]],
                         [1, 1, 0, 0, 0, 1, 1])
        self.assertEqual(PRICE_BINS, (25, 50, 100, 200, 400, 800))

    def test_rankings_keep_the_top_entries_and_fold_the_rest_into_other(self):
        rows = [row(f"b{index}", f"Widget {index}", "UNIVERSAL", f"BRAND{index:02}", "10", "1",
                    "0") for index in range(12)]
        rows += [row("x1", "Widget x1", "UNIVERSAL", "BRAND00", "10", "1", "0"),
                 row("x2", "Widget x2", "UNIVERSAL", "BRAND00", "10", "1", "0"),
                 row("x3", "Widget x3", "UNIVERSAL", "BRAND01", "10", "1", "0"),
                 row("x4", "Widget x4", "UNIVERSAL", "", "10", "1", "0")]
        overview = inventory_overview(Catalog.from_rows(rows))
        brands = overview["brands"]
        self.assertEqual(len(brands), 11)
        self.assertEqual(brands[:2], [{"key": "BRAND00", "count": 3},
                                      {"key": "BRAND01", "count": 2}])
        self.assertEqual(brands[-1], {"key": "__other__", "count": 3})
        self.assertEqual(sum(entry["count"] for entry in brands), 16)  # nothing is hidden
        self.assertEqual(overview["uncategorised"], 16)
        self.assertEqual((overview["makes"], overview["categories"]), ([], []))
        makes = [row(f"m{index}", "Widget", f"{make.upper()} X{index}", "ACME", "1", "1", "0")
                 for index, make in enumerate(list(site_catalog.MAKES)[:14])
                 if " " not in make and "-" not in make]
        ranked = inventory_overview(Catalog.from_rows(makes))["makes"]
        self.assertEqual(ranked[-1], {"key": "__other__", "count": len(makes) - 12, "name": ""})

    def test_the_overview_is_computed_once_per_catalog(self):
        self.assertIs(inventory_overview(self.catalog), self.overview)
        other = Catalog.from_rows(ROWS[:3])
        self.assertIsNot(inventory_overview(other), self.overview)
        self.assertEqual(inventory_overview(other)["tiles"]["items"], 3)
        self.assertEqual(self.overview["tiles"]["items"], 8)

    def test_freshness_and_an_empty_catalog(self):
        self.assertEqual(self.overview["observed_at"], NOW)
        self.assertEqual(self.overview["freshness"], self.catalog.freshness())
        empty = inventory_overview(Catalog.from_rows([]))
        self.assertEqual(empty["tiles"], {
            "items": 0, "available": 0, "in_store": 0, "warehouse_only": 0, "out_of_stock": 0,
            "store_units": 0.0, "warehouse_units": 0.0, "unpriced": 0})
        self.assertEqual((empty["stock_value"], empty["brands"], empty["observed_at"],
                          empty["freshness"]), (0.0, [], None, "stale"))

    def test_no_code_or_product_id_is_part_of_the_overview(self):
        text = json.dumps(self.overview, default=str, ensure_ascii=False)
        for private in ("sid-000", "ZQ-1234", "W712", "04465"):
            self.assertNotIn(private, text)


class Reports:
    """The repository methods the reports call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.runs: list[dict] = []
        self.daily: dict | None = {"sales": [], "purchases": [], "since": None}
        self.sellers: list[dict] | None = []
        self.error: Exception | None = None

    def _call(self, *call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    def sync_runs(self):
        self._call("sync_runs")
        return self.runs

    def trade_daily(self, days):
        self._call("trade_daily", days)
        return self.daily

    def best_sellers(self, days):
        self._call("best_sellers", days)
        return self.sellers


class SyncAndTradeTests(unittest.TestCase):
    def setUp(self):
        self.repository = Reports()
        self.catalog = Catalog.from_rows(ROWS)
        self.finished = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)

    def test_sync_health_reports_the_last_finished_run(self):
        self.repository.runs = [
            {"run_id": 9, "status": "running", "finished_at": None},
            {"run_id": 8, "status": "failed", "finished_at": self.finished},
            {"run_id": 7, "status": "succeeded", "finished_at": self.finished - timedelta(1)}]
        self.assertEqual(sync_health(self.repository), {
            "available": True, "runs": self.repository.runs,
            "last_finished_at": self.finished, "last_status": "failed"})
        self.repository.runs = [{"run_id": 9, "status": "running", "finished_at": None}]
        health = sync_health(self.repository)
        self.assertEqual((health["available"], health["last_finished_at"],
                          health["last_status"]), (True, None, None))
        self.repository.runs = []
        self.assertEqual(sync_health(self.repository)["available"], True)

    def test_sync_health_degrades_when_the_repository_cannot_answer(self):
        for error in (RuntimeError("password=private-fixture"), KeyError("run_id")):
            self.repository.error = error
            self.assertEqual(sync_health(self.repository), {"available": False, "runs": []})
        self.assertEqual(sync_health(object()), {"available": False, "runs": []})

    def test_trade_report_names_best_sellers_from_the_catalog(self):
        day = datetime(2026, 11, 9).date()
        self.repository.daily = {
            "sales": [{"day": day, "units": Decimal("5"), "amount": Decimal("227.50")}],
            "purchases": [{"day": day, "units": Decimal("12")}], "since": self.finished}
        self.repository.sellers = [
            {"source_product_id": "sid-0001", "units": Decimal("4"), "amount": Decimal("182")},
            {"source_product_id": "sid-gone", "units": Decimal("1"), "amount": None}]
        report = trade_report(self.repository, self.catalog, 90)
        self.assertEqual(report, {
            "available": True, "days": 90, "since": self.finished,
            "sales": self.repository.daily["sales"],
            "purchases": self.repository.daily["purchases"],
            "best_sellers": [
                {"name": "სამუხრუჭე ხუნდი წინა", "brand": "SATO",
                 "fits": "TOYOTA CAMRY 2012-2017", "units": Decimal("4"),
                 "amount": Decimal("182")},
                {"name": "", "brand": "", "fits": "", "units": Decimal("1"), "amount": None}]})
        self.assertEqual(self.repository.calls, [("trade_daily", 90), ("best_sellers", 90)])
        self.assertNotIn("sid-", json.dumps(report, default=str))

    def test_trade_report_degrades_when_trade_is_missing_or_unreadable(self):
        self.repository.error = RuntimeError("relation trade.sales does not exist")
        self.assertEqual(trade_report(self.repository, self.catalog), {"available": False})
        self.repository.error = None
        self.repository.daily = None
        self.assertEqual(trade_report(self.repository, self.catalog), {"available": False})
        self.repository.daily, self.repository.sellers = {"sales": [], "purchases": [],
                                                           "since": None}, None
        self.assertEqual(trade_report(self.repository, self.catalog), {"available": False})
        self.assertEqual(trade_report(object(), self.catalog), {"available": False})


class ChatReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "chat_log.sqlite3"
        self.now = time.time()

    def settings(self, staff: str = "", site: str = "") -> SimpleNamespace:
        return SimpleNamespace(chat_log_path=staff, site_chat_log_path=site)

    def write(self, turns: list[tuple]) -> None:
        """(seconds ago, status, category) rows in a file with the chat log's own schema."""
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(chat_log.SCHEMA)
            for index, (ago, status, category) in enumerate(turns):
                connection.execute(
                    "INSERT INTO turns (session_id, turn_index, started_at, duration_ms, "
                    "language, question, answer, status, category) "
                    "VALUES (?, ?, ?, 900, 'ka', ?, ?, ?, ?)",
                    (f"session-{index}", index, self.now - ago, f"question {MARKER}",
                     f"answer {MARKER}", status, category))
            connection.commit()
        finally:
            connection.close()

    def tbilisi_day(self, ago: float) -> str:
        moment = datetime.fromtimestamp(self.now - ago, UTC) + timedelta(hours=4)
        return moment.date().isoformat()

    def test_turns_are_counted_by_day_topic_and_status(self):
        day = 86_400
        turns = [(60, "COMPLETE", f"front brake pads {MARKER}"),
                 (120, "COMPLETE", "ხუნდები"), (2 * day, "FAILED", "ზეთის ფილტრი"),
                 (3 * day, "COMPLETE", f"something unusual {MARKER}"), (5 * day, "PARTIAL", ""),
                 (40 * day, "COMPLETE", "air filter")]
        self.write(turns)
        report = chat_report(self.settings(staff=str(self.path)), 30)
        self.assertEqual(set(report), {"days", "staff", "site"})
        self.assertEqual((report["days"], report["site"]), (30, None))
        staff = report["staff"]
        self.assertEqual(set(staff), {"daily", "topics", "statuses"})
        expected = Counter(self.tbilisi_day(ago) for ago, _, _ in turns[:5])
        self.assertEqual(staff["daily"], [{"day": name, "turns": expected[name]}
                                          for name in sorted(expected)])
        self.assertEqual(staff["statuses"], {"COMPLETE": 3, "FAILED": 1, "PARTIAL": 1})
        self.assertEqual(counts(staff["topics"]),
                         {"brake_pads": 2, "oil_filter": 1, "__other__": 1})
        self.assertEqual(staff["topics"][0]["label"], {"ka": "სამუხრუჭე ხუნდები",
                                                       "en": "Brake pads"})
        # The 40-day-old turn only counts in a longer window.
        longer = chat_report(self.settings(staff=str(self.path)), 60)["staff"]
        self.assertEqual(sum(entry["turns"] for entry in longer["daily"]), 6)
        self.assertEqual(counts(longer["topics"])["air_filter"], 1)

    def test_questions_answers_and_category_text_are_never_returned(self):
        self.write([(60, "COMPLETE", f"brake pads {MARKER}"), (90, "FAILED", MARKER)])
        report = chat_report(self.settings(staff=str(self.path), site=str(self.path)), 30)
        self.assertEqual(report["site"], report["staff"])
        text = json.dumps(report, ensure_ascii=False)
        for private in (MARKER, "question", "answer", "session-"):
            self.assertNotIn(private, text)

    def test_days_are_tbilisi_days(self):
        # 21:30 UTC is already the next day in Tbilisi (UTC+4).
        midnight_utc = (int(self.now) // 86_400) * 86_400
        ago = self.now - (midnight_utc - 86_400 * 2 + 21.5 * 3600)
        self.write([(ago, "COMPLETE", "")])
        utc_day = datetime.fromtimestamp(self.now - ago, UTC).date()
        staff = chat_report(self.settings(staff=str(self.path)), 30)["staff"]
        self.assertEqual(staff["daily"],
                         [{"day": (utc_day + timedelta(days=1)).isoformat(), "turns": 1}])

    def test_the_log_is_only_read(self):
        self.write([(60, "COMPLETE", "brake pads")])
        before = self.path.read_bytes()
        chat_report(self.settings(staff=str(self.path), site=str(self.path)), 30)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["chat_log.sqlite3"])

    def test_missing_or_unreadable_logs_are_reported_as_absent(self):
        (self.directory / "not-a-database.sqlite3").write_bytes(b"this is not sqlite" * 100)
        empty = self.directory / "empty.sqlite3"
        sqlite3.connect(empty).close()
        empty.touch()
        for path in ("", str(self.directory / "missing.sqlite3"), str(self.directory),
                     str(self.directory / "not-a-database.sqlite3"), str(empty)):
            with self.subTest(path=path[-24:]):
                self.assertEqual(chat_report(self.settings(staff=path, site=path), 30),
                                 {"days": 30, "staff": None, "site": None})
        self.assertEqual(chat_report(SimpleNamespace(), 7), {"days": 7, "staff": None,
                                                             "site": None})

    def test_it_reads_what_the_chat_logger_writes_while_the_logger_is_open(self):
        logger = ChatLogger(self.path, hmac_key="x" * 32)
        self.addCleanup(logger.close)
        for question, status, category in (("front pads?", "COMPLETE", "front brake pads"),
                                           ("oil?", "FAILED", "engine oil 5W-30")):
            logger.turn("c" * 43, question=f"{question} {MARKER}", status=status,
                        language="en", started_at=self.now - 30, duration_ms=800,
                        payload={"answer": f"answer {MARKER}", "category": category})
        logger.flush()
        staff = chat_report(self.settings(staff=str(self.path)), 7)["staff"]
        self.assertEqual(staff["statuses"], {"COMPLETE": 1, "FAILED": 1})
        self.assertEqual(counts(staff["topics"]), {"brake_pads": 1, "engine_oil": 1})
        self.assertEqual(sum(entry["turns"] for entry in staff["daily"]), 2)
        self.assertNotIn(MARKER, json.dumps(staff, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
