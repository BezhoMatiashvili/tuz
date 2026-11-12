"""CarNumbers and shop_info on fakes: parameters, read-only SQL, notes, caps and failures."""

from __future__ import annotations

import contextlib
import copy
import re
import unittest
from collections import namedtuple

import psycopg

from automotive import assistant_sources as sources
from automotive.assistant_sources import (
    BadArguments,
    CarNumbers,
    SourceUnavailable,
    make_norm,
    numbers_for,
    shop_info,
)
from automotive.repository import DatabaseNotConfigured
from automotive.site_content import SiteContent, default_snapshot

STATEMENTS = {sources.SETUP_SQL: "setup", sources.VEHICLES_SQL: "vehicles",
              sources.VEHICLES_BY_ID_SQL: "vehicles_by_id", sources.PARTS_SQL: "parts",
              sources.CROSS_SQL: "cross", sources.DIAGNOSE_SQL: "diagnose",
              sources.KINDS_SQL: "kinds"}
WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|GRANT|TRUNCATE|COPY)\b",
                    re.IGNORECASE)
Column = namedtuple("Column", "name")
E400 = {"make": "Mercedes-Benz", "model_terms": ["E-Class", " 212 "], "year": 2014,
        "engine_terms": ["M276", "3.0"], "part_terms": ["spark plug"]}


def vehicle(vehicle_id, matched=1, **extra):
    row = {"id": vehicle_id, "source": "ngk", "make": "MERCEDES BENZ", "model": "E CLASS",
           "model_code": "", "engine": "3.0 M276.820 V6 Turbo", "engine_code": "",
           "year_from": 2013, "year_to": 2015, "power_kw": None, "fuel": "",
           "label": "MERCEDES BENZ E CLASS 3.0 E400 207 / 212 SERIES M276.820",
           "matched": matched}
    row.update(extra)
    return row


def part(vehicle_id, part_id, number, name="Spark Plug", position=None, listed=1, **extra):
    row = {"vehicle_id": vehicle_id, "part_id": part_id, "brand": "NGK", "number": number,
           "name": name, "position": name if position is None else position, "note": "",
           "listed": listed}
    row.update(extra)
    return row


def cross(part_id, brand, number, relation="equivalent"):
    return {"part_id": part_id, "brand": brand, "number": number, "relation": relation}


def diagnosis(make_count, model_count=0, year_count=0, **extra):
    row = {"make_count": make_count, "model_count": model_count, "year_count": year_count,
           "first_year": None, "last_year": None, "models": [], "year_ids": []}
    row.update(extra)
    return row


class Cursor:
    def __init__(self, rows, description=None):
        self.rows, self.description = rows, description

    def fetchall(self):
        return list(self.rows)


class Connection:
    """What CarNumbers needs from psycopg: execute, a transaction block and closing. Each
    statement is one of the module's SQL constants; the answer is canned per statement."""

    def __init__(self, answers=None, fail=None):
        self.answers, self.fail = answers or {}, fail or {}
        self.statements = []
        self.transactions = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    @contextlib.contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, statement, params=None):
        name = STATEMENTS[statement]  # a statement that is not a module constant fails here
        self.statements.append((name, statement, params))
        if name in self.fail:
            raise self.fail[name]
        answer = self.answers.get(name, [])
        return answer if isinstance(answer, Cursor) else Cursor(copy.deepcopy(answer))

    def ran(self):
        return [name for name, _, _ in self.statements]

    def params(self, name):
        return [params for found, _, params in self.statements if found == name]


class Repository:
    def __init__(self, connection=None, error=None):
        self.connection, self.error = connection, error
        self.connects = 0

    def _connect(self):
        self.connects += 1
        if self.error is not None:
            raise self.error
        return self.connection


class CarNumbersTests(unittest.TestCase):
    def find(self, answers, args, **options):
        connection = Connection(answers)
        return CarNumbers(Repository(connection), **options).find(args), connection

    def test_make_model_year_and_part_terms_reach_the_query_as_parameters(self):
        result, conn = self.find({"vehicles": [vehicle(67934)],
                                  "parts": [part(67934, 1, "SILZKFR8D7S")]}, E400)
        self.assertEqual(conn.ran(), ["setup", "vehicles", "parts", "cross"])
        params = conn.params("vehicles")[0]
        self.assertEqual(params["make"], "MERCEDESBENZ")  # as the crawler stores make_norm
        self.assertEqual(params["model_terms"], ["E-Class", "212"])
        self.assertEqual(params["year"], 2014)
        self.assertEqual(params["engine_terms"], ["M276", "3.0"])
        self.assertEqual((params["words"], params["owners"]), (["spark", "plug"], [0, 0]))
        self.assertEqual(params["limit"], 8)
        self.assertEqual(conn.params("parts")[0]["ids"], [67934])
        self.assertEqual(conn.params("parts")[0]["limit"], 40)
        self.assertEqual(conn.params("cross")[0]["relations"], list(sources.RELATIONS))
        self.assertEqual(result["vehicles"][0]["years"], "2013-2015")
        self.assertEqual(result["vehicles"][0]["engine"], "3.0 M276.820 V6 Turbo")
        self.assertEqual(result["parts"][0]["number"], "SILZKFR8D7S")
        self.assertEqual(result["note"], "")

    def test_statements_only_read_and_user_terms_stay_out_of_the_sql(self):
        args = {"make": "Zqmake", "model_terms": ["Qwmodel"], "year": 2014,
                "engine_terms": ["Xyengine"], "part_terms": ["Vbpart"]}
        _, conn = self.find({"diagnose": [diagnosis(3, 2, 2, year_ids=[1])]}, args,
                            statement_ms=1500)
        self.assertEqual(conn.ran(), ["setup", "vehicles", "diagnose", "kinds"])
        self.assertEqual(conn.statements[0][2], ("1500",))  # timeout and read-only first
        self.assertIn("transaction_read_only", conn.statements[0][1])
        self.assertEqual((conn.transactions, conn.closed), (1, True))
        for _, statement, params in conn.statements:
            self.assertRegex(statement.lstrip(), r"^(SELECT|WITH)\b")
            self.assertIsNone(WRITES.search(statement), statement)
            for word in ("Zqmake", "ZQMAKE", "Qwmodel", "Xyengine", "Vbpart"):
                self.assertNotIn(word.casefold(), statement.casefold())
        self.assertEqual(conn.params("vehicles")[0]["words"], ["Vbpart"])

    def test_vehicle_ids_restrict_the_search_to_those_vehicles(self):
        result, conn = self.find(
            {"vehicles_by_id": [vehicle(5, matched=2), vehicle(7, matched=2)],
             "parts": [part(5, 1, "W 712/75", name="Oil Filter")]},
            {"make": "Opel", "model_terms": ["Astra"], "vehicle_ids": [5, "7", 5, 9.0],
             "part_terms": ["oil filter"]})
        self.assertNotIn("vehicles", conn.ran())
        self.assertEqual(conn.params("vehicles_by_id")[0]["ids"], [5, 7, 9])
        self.assertEqual(conn.params("parts")[0]["ids"], [5, 7])
        self.assertEqual(conn.params("parts")[0]["words"], ["oil", "filter"])
        self.assertEqual([item["id"] for item in result["vehicles"]], [5, 7])
        self.assertIn("Unknown vehicle_ids: 9.", result["note"])

    def test_vehicle_ids_beyond_the_limit_are_named(self):
        result, conn = self.find({"vehicles_by_id": [vehicle(1), vehicle(2)]},
                                 {"vehicle_ids": [1, 2, 3]}, vehicle_limit=2)
        self.assertEqual(conn.params("vehicles_by_id")[0]["ids"], [1, 2])
        self.assertIn("Only the first 2 vehicle_ids were read.", result["note"])
        self.assertIn("These vehicles have no listed parts.", result["note"])

    def test_part_terms_become_the_words_of_their_own_term(self):
        _, conn = self.find({}, {"make": "Toyota", "part_terms": [
            "oil filter", "PLUG, SPARK", "--", "(AKPP) gasket"]})
        params = conn.params("vehicles")[0]
        self.assertEqual(params["words"], ["oil", "filter", "PLUG", "SPARK", "AKPP", "gasket"])
        self.assertEqual(params["owners"], [0, 0, 1, 1, 2, 2])

    def test_parts_carry_their_cross_numbers_notes_and_vehicle_order(self):
        answers = {
            "vehicles": [vehicle(1, matched=2), vehicle(2, matched=2)],
            # Vehicles take turns in the query; the result groups them again.
            "parts": [part(2, 20, "HU 712/8 x", name="", position="Oil Filter", listed=3,
                           brand="MANN-FILTER", note="Up to engine no. 19MA9234"),
                      part(1, 10, "W 712/75", name="Oil Filter", brand="MANN-FILTER",
                           listed=3),
                      part(2, 10, "W 712/75", name="Oil Filter", brand="MANN-FILTER",
                           listed=3)],
            "cross": [cross(10, "GENERAL MOTORS", "9192425", "oe"),
                      cross(20, "MANN-FILTER", "HU 712 x", "replaces"),
                      cross(10, "BOSCH", "F 026 407 015")],
        }
        result, conn = self.find(answers, {"make": "Opel", "model_terms": ["Astra G"],
                                           "part_terms": ["oil filter"]})
        self.assertEqual(conn.params("cross")[0]["ids"], [20, 10])
        self.assertEqual(conn.params("cross")[0]["order"], list(sources.CROSS_ORDER))
        parts = result["parts"]
        self.assertEqual([(item["vehicle_id"], item["number"]) for item in parts],
                         [(1, "W 712/75"), (2, "HU 712/8 x"), (2, "W 712/75")])
        self.assertEqual(parts[1]["part"], "Oil Filter")  # no name: the position names it
        self.assertEqual(parts[1]["note"], "Up to engine no. 19MA9234")
        self.assertEqual(parts[1]["cross"], [{"brand": "MANN-FILTER", "number": "HU 712 x",
                                              "relation": "replaces"}])
        self.assertEqual([entry["number"] for entry in parts[0]["cross"]],
                         ["9192425", "F 026 407 015"])
        parts[0]["cross"].clear()  # every row has its own list
        self.assertEqual(len(parts[2]["cross"]), 2)
        self.assertEqual((result["vehicles_matched"], result["parts_listed"]), (2, 3))

    def test_numbers_for_lists_own_numbers_then_cross_numbers_once(self):
        result = {"parts": [
            {"vehicle_id": 1, "number": "W 712/75", "cross": [
                {"brand": "GM", "number": "9192425", "relation": "oe"},
                {"brand": "MANN-FILTER", "number": "HU 712/8 x", "relation": "equivalent"}]},
            {"vehicle_id": 2, "number": "HU712/8X", "cross": [
                {"brand": "BOSCH", "number": "F 026 407 015", "relation": "equivalent"},
                {"brand": "", "number": " ", "relation": "oe"}]},
        ]}
        self.assertEqual(numbers_for(result),
                         ["W 712/75", "HU712/8X", "9192425", "F 026 407 015"])
        self.assertEqual(numbers_for(result, ["2"]), ["HU712/8X", "F 026 407 015"])
        self.assertEqual(numbers_for(result, [1]), ["W 712/75", "9192425", "HU 712/8 x"])
        self.assertEqual(numbers_for({"parts": []}), [])

    def test_empty_result_note_names_the_filter_that_emptied_it(self):
        cases = [
            (diagnosis(0), {"make": "VW", "model_terms": ["Golf"]},
             ["No crawled vehicle with listed parts has the make 'VW'"]),
            (diagnosis(317, models=["E CLASS", "A-Class (W169), B-Class (W245)"]),
             {"make": "Mercedes-Benz", "model_terms": ["Ekstraklasa"]},
             ["317 crawled vehicles", 'none matches a model term ("Ekstraklasa")',
              "E CLASS; A-Class (W169), B-Class (W245)", "longer or other spelling"]),
            (diagnosis(317, 43, 0, first_year=2009, last_year=9999),
             {"make": "Mercedes-Benz", "model_terms": ["E-Class"], "year": 1960},
             ["43 of them match a model term", "none covers 1960 (they cover 2009-)"]),
        ]
        for answer, args, expected in cases:
            with self.subTest(args=args):
                result, conn = self.find({"diagnose": [answer]}, args)
                self.assertEqual(conn.ran(), ["setup", "vehicles", "diagnose"])
                self.assertEqual((result["vehicles"], result["parts"]), ([], []))
                for text in expected:
                    self.assertIn(text, result["note"])

    def test_empty_result_note_names_the_part_kinds_when_part_terms_emptied_it(self):
        result, conn = self.find(
            {"diagnose": [diagnosis(317, 43, 5, year_ids=[67933, 67934])],
             "kinds": [{"kind": "Spark Plug", "vehicles": 5}]},
            {**E400, "part_terms": ["oil filter"]})
        self.assertEqual(conn.ran(), ["setup", "vehicles", "diagnose", "kinds"])
        self.assertEqual(conn.params("kinds")[0]["ids"], [67933, 67934])
        diagnose = conn.params("diagnose")[0]
        self.assertEqual((diagnose["make"], diagnose["year"]), ("MERCEDESBENZ", 2014))
        for text in ("5 cover 2014", 'none lists a part named like "oil filter"',
                     "Part kinds they list: Spark Plug", "do not cover it"):
            self.assertIn(text, result["note"])

    def test_caps_are_visible_in_counts_and_the_note(self):
        answers = {"vehicles": [vehicle(1, matched=5), vehicle(2, matched=5)],
                   "parts": [part(1, 10, "A1", listed=9), part(2, 20, "B1", listed=9),
                             part(1, 11, "A2", listed=9)],
                   "cross": [cross(10, "X", "X1"), cross(20, "Y", "Y1"), cross(11, "Z", "Z1")]}
        result, conn = self.find(answers, E400, vehicle_limit=2, part_limit=3, cross_limit=2)
        self.assertEqual(conn.params("cross")[0]["limit"], 3)  # one more tells a cut
        self.assertEqual((result["vehicles_matched"], result["parts_listed"]), (5, 9))
        self.assertTrue(result["cross_truncated"])
        self.assertEqual(sum(len(item["cross"]) for item in result["parts"]), 2)
        for text in ("Showing 2 of 5 matching vehicles", "Showing 3 of 9 listed parts",
                     "Cross numbers stop at 2."):
            self.assertIn(text, result["note"])

    def test_terms_without_letters_or_digits_cannot_match_everything(self):
        _, conn = self.find({}, {"make": "Mercedes", "model_terms": ["-", "E-Class", "e-class"],
                                 "engine_terms": ["/"]})
        params = conn.params("vehicles")[0]
        self.assertEqual(params["model_terms"], ["E-Class"])
        self.assertEqual(params["engine_terms"], [])
        for args in ({"make": "Mercedes", "model_terms": ["-", "/"]},
                     {"make": "Mercedes", "part_terms": ["..."]}):
            with self.subTest(args=args), self.assertRaises(BadArguments):
                CarNumbers(Repository(Connection())).find(args)

    def test_bad_arguments_say_what_to_fix_before_any_connection(self):
        cases = ["not an object", {"part_terms": ["spark plug"]}, {"make": "Тойота"},
                 {"make": 7}, {"make": "Toyota", "year": "2014"},
                 {"make": "Toyota", "year": 14}, {"make": "Toyota", "year": True},
                 {"make": "Toyota", "year": 2014.5}, {"make": "Toyota", "model_terms": "Auris"},
                 {"make": "Toyota", "model_terms": ["Auris", 3]}, {"vehicle_ids": ["abc"]},
                 {"vehicle_ids": [True]}, {"vehicle_ids": [-4]}, {"vehicle_ids": 5}]
        repository = Repository(error=AssertionError("must not connect"))
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(BadArguments) as raised:
                    CarNumbers(repository).find(args)
                self.assertTrue(raised.exception.fix)
                self.assertEqual(raised.exception.payload()["ok"], False)
        self.assertEqual(repository.connects, 0)

    def test_database_failures_become_source_unavailable(self):
        connect_errors = [psycopg.OperationalError("connection refused"),
                          DatabaseNotConfigured("Automotive database is not configured"),
                          OSError("network down")]
        for error in connect_errors:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(SourceUnavailable) as raised:
                    CarNumbers(Repository(error=error)).find(E400)
                self.assertIn("cannot be reached", raised.exception.error)
        statement_errors = [
            ("vehicles", psycopg.errors.QueryCanceled("statement timeout"), "in time"),
            ("parts", psycopg.errors.UndefinedTable("no parts_catalog.parts"), "not installed"),
            ("cross", psycopg.errors.InsufficientPrivilege("denied"), "cannot be read"),
            ("setup", psycopg.OperationalError("server closed"), "cannot be read"),
        ]
        for statement, error, text in statement_errors:
            with self.subTest(statement=statement):
                conn = Connection({"vehicles": [vehicle(1)], "parts": [part(1, 10, "A1")]},
                                  fail={statement: error})
                with self.assertRaises(SourceUnavailable) as raised:
                    CarNumbers(Repository(conn)).find(E400)
                self.assertIn(text, raised.exception.error)
                self.assertTrue(raised.exception.fix)
                self.assertTrue(conn.closed)
        self.assertTrue(issubclass(SourceUnavailable, RuntimeError))

    def test_tuple_rows_from_a_connection_factory_are_read_by_column_name(self):
        columns = ("id", "source", "make", "model", "model_code", "engine", "engine_code",
                   "year_from", "year_to", "power_kw", "fuel", "label", "matched")
        row = vehicle(125110, source="catcar", make="TOYOTA", model="AURIS/HYBRID (UKP)",
                      year_from=2015, year_to=None, engine_code="1NRFE",
                      label="ЕВРОПА / AURIS/HYBRID (UKP) / 1NRFE")
        conn = Connection({"vehicles": Cursor([tuple(row[name] for name in columns)],
                                              [Column(name) for name in columns])})
        result = CarNumbers(lambda: conn).find({"make": "Toyota", "model_terms": ["Auris"]})
        self.assertEqual(result["vehicles"][0]["id"], 125110)
        self.assertEqual(result["vehicles"][0]["years"], "2015-")
        self.assertEqual(result["vehicles"][0]["engine_code"], "1NRFE")

    def test_needs_a_repository_or_a_connection_factory(self):
        with self.assertRaises(TypeError):
            CarNumbers(object())
        with self.assertRaises(ValueError):
            CarNumbers(Repository(), relations=("similar",))
        narrowed = CarNumbers(Repository(), relations=("equivalent", "oe"))
        self.assertEqual(narrowed.relations, ["equivalent", "oe"])

    def test_make_is_normalised_like_the_crawler_stores_it(self):
        samples = ["Mercedes-Benz", "MERCEDES BENZ", "Škoda", "Citroën", "Land Rover",
                   "DAF Trucks", "Тойота", ""]
        self.assertEqual([make_norm(item) for item in samples],
                         ["MERCEDESBENZ", "MERCEDESBENZ", "SKODA", "CITROEN", "LANDROVER",
                          "DAFTRUCKS", "", ""])
        try:
            from parts_crawler.records import normalize_brand
        except ImportError:  # pragma: no cover - the crawler is not installed alongside
            return
        self.assertEqual([make_norm(item) for item in samples],
                         [normalize_brand(item) for item in samples])


class FakeSite:
    def __init__(self, snapshot=None, error=None):
        self._snapshot, self.error = snapshot, error

    def snapshot(self):
        if self.error is not None:
            raise self.error
        return self._snapshot


def filled_snapshot():
    snapshot = default_snapshot()
    snapshot["sections"]["brand"].update(
        phone="+995 555 12 34 56", email="shop@example.ge", map_url="https://maps.example/x",
        address={"ka": "თბილისი, აღმაშენებლის 1", "en": "1 Agmashenebeli Ave, Tbilisi"},
        hours=[{"days": {"ka": "ორშ-შაბ", "en": "Mon-Sat"}, "time": "10:00-19:00"},
               {"days": {"ka": "კვირა", "en": "Sunday"}, "time": ""}],
        instagram="https://instagram.com/garage1886")
    snapshot["services"][0]["price_from"] = 30.0
    snapshot["services"][1]["is_published"] = False
    return snapshot


class ShopInfoTests(unittest.TestCase):
    def test_filled_content_in_one_language_with_empty_values_left_out(self):
        info = shop_info(FakeSite(filled_snapshot()), "en")
        self.assertEqual(info["phone"], "+995 555 12 34 56")
        self.assertEqual(info["address"], "1 Agmashenebeli Ave, Tbilisi")
        self.assertEqual(info["map_url"], "https://maps.example/x")
        self.assertEqual(info["hours"], [{"day": "Mon-Sat", "time": "10:00-19:00"},
                                         {"day": "Sunday"}])
        self.assertEqual(info["instagram"], "https://instagram.com/garage1886")
        self.assertEqual(info["currency"], "₾")
        self.assertNotIn("whatsapp", info)
        self.assertEqual(info["services"][0], {
            "title": "Diagnostics", "price_from": 30.0,
            "summary": "Computer and hands-on diagnostics before any part is replaced."})
        self.assertNotIn("Oil and filters", [item["title"] for item in info["services"]])
        self.assertNotIn("price_from", info["services"][1])
        self.assertEqual(info["missing"], ["whatsapp", "facebook", "tiktok"])
        self.assertEqual(shop_info(FakeSite(filled_snapshot()), "ka")["address"],
                         "თბილისი, აღმაშენებლის 1")

    def test_a_language_the_site_has_no_text_in_reads_english(self):
        info = shop_info(FakeSite(filled_snapshot()), "ru")
        self.assertEqual(info["address"], "1 Agmashenebeli Ave, Tbilisi")
        self.assertEqual(info["hours"][0]["day"], "Mon-Sat")

    def test_empty_content_names_what_is_missing(self):
        info = shop_info(SiteContent(None), "ka")  # no site database: packaged defaults
        self.assertEqual(info["missing"], ["phone", "whatsapp", "email", "address", "map_url",
                                           "hours", "facebook", "instagram", "tiktok",
                                           "service_prices"])
        self.assertEqual(len(info["services"]), len(default_snapshot()["services"]))
        self.assertTrue(all("price_from" not in item for item in info["services"]))
        self.assertNotIn("phone", info)

    def test_unavailable_content_never_raises(self):
        for site in (FakeSite(error=RuntimeError("database down")), FakeSite({}), None):
            with self.subTest(site=site), \
                    self.assertLogs("automotive.assistant_sources", "WARNING"):
                info = shop_info(site, "en")
                self.assertEqual(info["missing"], ["all"])
                self.assertIn("do not guess", info["note"])


if __name__ == "__main__":
    unittest.main()
