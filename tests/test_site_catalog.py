"""Public part search: vehicle text parsing, the in-memory catalog and its routes."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import psycopg

from automotive import categories, name_match, rules, site_catalog, stock
from automotive.rules import Rules
from automotive.site_catalog import (
    MAKES,
    PAGE_SIZE,
    Catalog,
    CatalogStore,
    model_key,
    parse_price,
    vehicle_keys,
)
from automotive.vin import ProviderUnavailable
from tests.test_site_guard import VIN, RecordingBuckets, SiteAppCase

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PUBLIC_KEYS = {"name", "fits", "brand", "price"}
PAD, OIL_FILTER, AIR_KA = "სამუხრუჭე ხუნდი წინა", "ზეთის ფილტრი", "ჰაერის ფილტრი"


def row(sid: str, name: str, model: str, brand: str, price: str | None, store: str | None,
        tuz11: str | None, *, code: str = "", original: str = "", duplicate: str = "",
        seen: datetime | None = NOW) -> dict:
    """One row as SNAPSHOT_SQL returns it."""
    return {"source_product_id": sid, "product_code": code, "product_name": name,
            "product_model": model, "original_code": original, "duplicate_code": duplicate,
            "brand": brand, "last_observed_at": seen, "price_raw": price,
            "stock_store": store, "stock_tuz11": tuz11}


ROWS = [
    row("sid-0001", PAD, "TOYOTA CAMRY 2012-2017", "SATO", "45,50", "3", "0",
        code="ZQ-1234", original="04465-33450"),
    row("sid-0002", OIL_FILTER, "TOYOTA/LEXUS LC200 2008&gt;", "MANN", "0", "0", "5",
        code="W712/95"),
    row("sid-0003", AIR_KA, "HONDA CR-V 2007-2011", "SAKURA", "abc", "2", "", code="A-1858"),
    # Reads exactly like sid-0001 to a customer, but is only in the warehouse.
    row("sid-0004", PAD, "TOYOTA CAMRY 2012-2017", "SATO", "45.5", "0", "7", code="ZQ-1234B"),
    row("sid-0005", "ამორტიზატორი", "MMC L200 2005>", "KYB", "120", "0", "0", code="344300"),
    row("sid-0006", "სანთელი", "UNIVERSAL", "NGK", "12", "x", "y", code="BKR6E"),
    row("sid-0007", "Air filter", "HONDA CRV 2012-2016", "SAKURA", "30", "1", "1",
        code="A-1859"),
    row("sid-0008", "Brake disc front", "BMW E70,71 3,5D", "BREMBO", "850", "0", "2,5",
        code="09.C398.11", duplicate="34116793245"),
]
PRIVATE = ("sid-000", "ZQ-1234", "ZQ1234", "04465", "W712", "A-185", "344300", "09.C398",
           "34116793245", "source_product_id", "product_code", "store", "warehouse", "tuz",
           "stock_", "state", "IN_STORE")


def names(result: dict) -> list[str]:
    return [item["name"] for item in result["items"]]


class VehicleKeyTests(unittest.TestCase):
    def test_makes_and_models_in_the_catalogues_own_spellings(self):
        expected = {
            "TOYOTA/LEXUS LC200 2008>": (("toyota", "lexus"), (("LC200", "LC200"),)),
            "VAG VW GOLF5,JETTA": (("volkswagen",), (("GOLF5", "GOLF5"), ("JETTA", "JETTA"))),
            "VAG A4,A6": (("audi",), (("A4", "A4"), ("A6", "A6"))),
            "MMC L200 2005>": (("mitsubishi",), (("L200", "L200"),)),
            "BMW E70,71 3,5D": (("bmw",), (("E70", "E70"), ("71", "71"))),
            "HONDA CR-V": (("honda",), (("CRV", "CR-V"),)),
            "HONDA CRV": (("honda",), (("CRV", "CRV"),)),
            "HYNDAI SONATA": (("hyundai",), (("SONATA", "SONATA"),)),
            "UNIVERSAL": ((), ()),
            "": ((), ()),
        }
        for text, keys in expected.items():
            with self.subTest(text=text):
                self.assertEqual(vehicle_keys(text), keys)
        self.assertEqual(vehicle_keys(None), ((), ()))

    def test_years_engines_and_drivetrain_words_are_not_models(self):
        for text, models in (
                ("TOYOTA CAMRY 2,5 2012-2017", ("CAMRY",)),
                ("HONDA CR-V 2007-2011", ("CRV",)),
                ("MB W211 02-09", ("W211",)),
                ("VAG AUDI A4 2.0TFSI", ("A4",)),
                ("TOYOTA RAV4 4WD NEW 2019+", ("RAV4",)),
                ("NISSAN X-TRAIL T31 2007>", ("XTRAIL", "T31"))):
            with self.subTest(text=text):
                self.assertEqual(tuple(key for key, _ in vehicle_keys(text)[1]), models)

    def test_group_names_and_typos_resolve_to_one_canonical_make(self):
        for text, makes in (
                ("VAG OCTAVIA,GOLF5", ("skoda", "volkswagen")),
                ("VAG", ("volkswagen", "audi", "skoda", "seat")),  # nothing narrows it
                ("VAG SKODA OCTAVIA", ("skoda",)),
                ("MB W211", ("mercedes-benz",)), ("WV PASSAT", ("volkswagen",)),
                ("HUYNDAI ELANTRA", ("hyundai",)), ("INFINITY FX35", ("infiniti",)),
                ("LAND ROVER DISCOVERY", ("land rover",)), ("KIA/HYUNDAI", ("kia", "hyundai")),
                ("toyota camry", ("toyota",)), ("MAZDA6 2008>", ("mazda",))):
            with self.subTest(text=text):
                self.assertEqual(vehicle_keys(text)[0], makes)
                self.assertLessEqual(set(makes), set(MAKES))
        self.assertEqual(vehicle_keys("LAND ROVER DISCOVERY")[1][0], ("DISCOVERY", "DISCOVERY"))
        self.assertEqual(vehicle_keys("MAZDA6 2008>")[1], (("6", "6"),))

    def test_model_key_folds_punctuation_and_known_misspellings(self):
        for spelling in ("CR-V", "cr v", "CRV", "Cr-V"):
            self.assertEqual(model_key(spelling), "CRV")
        self.assertEqual(model_key("RAV-4"), model_key("rav4"))
        self.assertEqual(model_key("Land Cruiser"), "LC")
        self.assertEqual(model_key("ACORD"), "ACCORD")
        self.assertEqual(model_key(None), "")

    def test_parse_price_reads_plain_amounts_only(self):
        for text, price in (("45,50", 45.5), ("45.5", 45.5), (" 12 ", 12.0), ("0", 0.0),
                            ("1234567", 1234567.0)):
            self.assertEqual(parse_price(text), price)
        for text in (None, "", "abc", "12 GEL", "1 200", "-5", "1.2.3", "12,34567", 45, 45.5):
            self.assertIsNone(parse_price(text))


class CatalogSearchTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)

    def test_public_items_have_exactly_name_fits_brand_and_price(self):
        result = self.catalog.search()
        self.assertEqual(set(result), {"items", "page", "total", "has_more", "freshness"})
        self.assertTrue(result["items"])
        for item in result["items"]:
            self.assertEqual(set(item), PUBLIC_KEYS)
        text = json.dumps([self.catalog.search(), self.catalog.search(query="zq1234"),
                           self.catalog.options(), self.catalog.models("toyota")],
                          ensure_ascii=False)
        for private in PRIVATE:
            self.assertNotIn(private, text)

    def test_only_available_items_are_listed_in_store_first(self):
        result = self.catalog.search()
        # Out-of-stock and unreadable-stock rows are absent; the duplicate pad is shown once,
        # at the position of its in-store line, ahead of everything that is warehouse-only.
        self.assertEqual(names(result), ["Air filter", PAD, AIR_KA, "Brake disc front",
                                         OIL_FILTER])
        self.assertEqual((result["total"], result["page"], result["has_more"]), (5, 1, False))

    def test_within_a_stock_state_priced_items_come_before_price_on_request(self):
        rows = [row("a", "Alpha", "UNIVERSAL", "X", "0", "1", "0"),
                row("b", "Beta", "UNIVERSAL", "X", "10", "1", "0"),
                row("c", "Aardvark", "UNIVERSAL", "X", "5", "0", "3"),
                row("d", "Gamma", "UNIVERSAL", "X", "", "1", "0")]
        # In the store with a price, in the store without one, and only then the warehouse.
        self.assertEqual(names(Catalog.from_rows(rows).search()),
                         ["Beta", "Alpha", "Gamma", "Aardvark"])

    def test_stock_thresholds_come_from_the_rules(self):
        catalog = Catalog.from_rows(ROWS, Rules(min_store=2.0, min_tuz11=5.0))
        # Store counts of 1 and 2 no longer count; neither does a warehouse count of 5.
        self.assertEqual(names(catalog.search()), [PAD])

    def test_zero_and_unreadable_prices_are_price_on_request(self):
        prices = {item["name"]: item["price"] for item in self.catalog.search()["items"]}
        self.assertEqual(prices, {"Air filter": 30.0, PAD: 45.5, AIR_KA: None,
                                  "Brake disc front": 850.0, OIL_FILTER: None})

    def test_rows_that_read_the_same_collapse_but_different_prices_do_not(self):
        rows = [row("a", "Oil 5W-30", "UNIVERSAL", "MOTUL", "50", "1", "0"),
                row("b", "Oil 5W-30", "UNIVERSAL", "MOTUL", "50,0", "2", "0"),
                row("c", "Oil 5W-30", "UNIVERSAL", "MOTUL", "55", "1", "0"),
                row("d", "Oil 5W-30", "UNIVERSAL", "MOTUL", "0", "1", "0"),
                row("e", "Oil 5W-30", "UNIVERSAL", "MOTUL", "n/a", "1", "0"),
                row("f", "Oil  5W-30 ", "UNIVERSAL", "MOTUL", "50", "1", "0")]
        result = Catalog.from_rows(rows).search()
        self.assertEqual(sorted((item["price"] or 0) for item in result["items"]),
                         [0, 50.0, 55.0])
        self.assertEqual(result["total"], 3)

    def test_search_by_make_model_and_year(self):
        search = self.catalog.search
        self.assertEqual(names(search(make="toyota")), [PAD, OIL_FILTER])
        self.assertEqual(names(search(make="lexus")), [OIL_FILTER])
        self.assertEqual(names(search(make="mitsubishi")), [])  # its only item is out of stock
        self.assertEqual(names(search(make="honda", model="CRV")), ["Air filter", AIR_KA])
        self.assertEqual(names(search(make="honda", model="cr-v")), ["Air filter", AIR_KA])
        self.assertEqual(names(search(make="toyota", model="CAMRY", year=2015)), [PAD])
        self.assertEqual(names(search(make="toyota", model="CAMRY", year=2010)), [])
        self.assertEqual(names(search(make="toyota", model="LC200", year=2020)), [OIL_FILTER])
        self.assertEqual(names(search(make="toyota", model="LC200", year=2005)), [])
        # No year range in the text is not a reason to hide the item.
        self.assertEqual(names(search(make="bmw", model="E70", year=2010)),
                         ["Brake disc front"])
        self.assertEqual(names(search(model="71")), ["Brake disc front"])

    def test_year_applies_to_a_model_whatever_way_the_catalogue_spells_it(self):
        # The dropdown and the VIN route hand out the key `CRV`; rows spell it `CR-V` too.
        for model in ("CRV", "CR-V", "cr v"):
            with self.subTest(model=model):
                search = self.catalog.search
                self.assertEqual(names(search(make="honda", model=model, year=2015)),
                                 ["Air filter"])
                self.assertEqual(names(search(make="honda", model=model, year=2009)),
                                 [AIR_KA])
                self.assertEqual(names(search(make="honda", model=model, year=1999)), [])
        # A spelling folded onto another key by an alias: LANDCRUISER is offered as `LC`.
        rows = [row("a", "Filter", "TOYOTA LANDCRUISER 2008>", "MANN", "10", "1", "0"),
                row("b", "Filter old", "TOYOTA LANDCRUISER 98-07", "MANN", "10", "1", "0")]
        catalog = Catalog.from_rows(rows)
        self.assertEqual(names(catalog.search(make="toyota", model="LC")),
                         ["Filter", "Filter old"])
        self.assertEqual(names(catalog.search(make="toyota", model="LC", year=2012)),
                         ["Filter"])
        self.assertEqual(names(catalog.search(make="toyota", model="LC", year=2000)),
                         ["Filter old"])

    def test_search_by_brand_is_exact_and_case_insensitive(self):
        self.assertEqual(names(self.catalog.search(brand="sakura")), ["Air filter", AIR_KA])
        self.assertEqual(names(self.catalog.search(brand="SAKURA", make="honda", model="CRV",
                                                   year=2015)), ["Air filter"])
        self.assertEqual(names(self.catalog.search(brand="SAK")), [])

    def test_search_by_keyword_in_georgian_and_english(self):
        search = self.catalog.search
        self.assertEqual(names(search(query="ხუნდები")), [PAD])
        self.assertEqual(names(search(query="brake pads")), [PAD])  # English finds Georgian
        self.assertEqual(names(search(query="air filter")), ["Air filter", AIR_KA])
        self.assertEqual(names(search(query="ჰაერის ფილტრი")), ["Air filter", AIR_KA])
        self.assertEqual(names(search(query="oil filter")), [OIL_FILTER])
        self.assertEqual(names(search(query="brake disc")), ["Brake disc front"])
        self.assertEqual(names(search(query="  CAMRY   sato ")), [PAD])  # every word, any case
        self.assertEqual(names(search(query="camry mann")), [])
        self.assertEqual(names(search(query="air filter", make="honda", model="CRV",
                                      year=2009)), [AIR_KA])

    def test_search_by_part_code_in_any_of_its_spellings(self):
        search = self.catalog.search
        for query in ("ZQ-1234", "zq1234", " zq 1234 ", "04465-33450", "0446533450"):
            with self.subTest(query=query):
                self.assertEqual(names(search(query=query)), [PAD])
        self.assertEqual(names(search(query="34116793245")), ["Brake disc front"])
        self.assertEqual(names(search(query="09.C398.11")), ["Brake disc front"])
        self.assertEqual(names(search(query="ZQ-1234", make="honda")), [])
        self.assertEqual(names(search(query="344300")), [])  # out of stock stays hidden
        self.assertEqual(names(search(query="ZQ-9999")), [])
        for item in search(query="zq1234")["items"]:
            self.assertEqual(set(item), PUBLIC_KEYS)

    def test_a_code_printed_with_a_slash_is_found_as_printed(self):
        for query in ("W712/95", "w 712/95", "W712"):
            with self.subTest(query=query):
                self.assertEqual(names(self.catalog.search(query=query)), [OIL_FILTER])
        rows = [row("a", "Filter two", "UNIVERSAL", "FILTRON", "10", "1", "0", code="AP104/2"),
                row("b", "Filter three", "UNIVERSAL", "FILTRON", "10", "1", "0",
                    code="AP104/3"),
                row("c", "Pads", "UNIVERSAL", "SATO", "10", "1", "0",
                    original="04465-33450 / 04465-33471")]
        catalog = Catalog.from_rows(rows)
        self.assertEqual(names(catalog.search(query="AP104/2")), ["Filter two"])
        self.assertEqual(names(catalog.search(query="ap104/3")), ["Filter three"])
        self.assertEqual(names(catalog.search(query="AP104")), ["Filter three", "Filter two"])
        self.assertEqual(names(catalog.search(query="04465-33471")), ["Pads"])

    def test_html_entities_from_the_source_are_shown_as_text(self):
        rows = [row("a", "Bosch &amp; Co  &lt;new&gt;", "MMC  L200\t2005&gt;", "A&amp;B",
                    "10", "1", "0")]
        item = Catalog.from_rows(rows).search()["items"][0]
        self.assertEqual(item, {"name": "Bosch & Co <new>", "fits": "MMC L200 2005>",
                                "brand": "A&B", "price": 10.0})
        self.assertEqual(self.catalog.search(make="lexus")["items"][0]["fits"],
                         "TOYOTA/LEXUS LC200 2008>")

    def test_pages_of_twenty(self):
        rows = [row(f"s{index:03}", f"Part {index:03}", "UNIVERSAL", "ACME", "10", "1", "0")
                for index in range(45)]
        catalog = Catalog.from_rows(rows)
        first = catalog.search()
        self.assertEqual((len(first["items"]), first["total"], first["has_more"]),
                         (PAGE_SIZE, 45, True))
        self.assertEqual(names(first)[0], "Part 000")
        third = catalog.search(page=3)
        self.assertEqual((names(third), third["page"], third["has_more"]),
                         ([f"Part {index:03}" for index in range(40, 45)], 3, False))
        self.assertEqual(catalog.search(page=4)["items"], [])
        self.assertEqual(catalog.search(page=0)["page"], 1)

    def test_freshness_follows_the_rules_thresholds(self):
        catalog = self.catalog
        self.assertEqual(catalog.observed_at, NOW)
        for hours, label in ((1, "fresh"), (13, "fresh"), (13.1, "confirm"), (25, "confirm"),
                             (25.1, "stale"), (500, "stale")):
            self.assertEqual(catalog.freshness(NOW + timedelta(hours=hours)), label, hours)
        strict = Catalog.from_rows(ROWS, Rules(stale_hours=2, hide_after_hours=4))
        self.assertEqual(strict.freshness(NOW + timedelta(hours=3)), "confirm")
        self.assertEqual(strict.freshness(NOW + timedelta(hours=5)), "stale")
        # The newest observation counts; a catalogue with none is never called fresh.
        mixed = Catalog.from_rows([row("a", "x", "", "", "1", "1", "0",
                                       seen=NOW - timedelta(days=3)),
                                   row("b", "y", "", "", "1", "1", "0", seen=NOW)])
        self.assertEqual(mixed.freshness(NOW + timedelta(hours=1)), "fresh")
        unknown = Catalog.from_rows([row("a", "x", "", "", "1", "1", "0", seen=None)])
        self.assertEqual(unknown.freshness(NOW), "stale")
        self.assertEqual(site_catalog.EMPTY.freshness(NOW), "stale")
        self.assertEqual(self.catalog.search()["freshness"], self.catalog.freshness())

    def test_options_list_makes_models_and_brands_worth_offering(self):
        rows = [*ROWS,
                row("h1", "Filter a", "HONDA CR-V 2012>", "SAKURA", "10", "1", "0"),
                row("h2", "Filter b", "HONDA CIVIC", "SAKURA", "10", "1", "0"),
                row("h3", "Filter c", "HONDA ACORD", "SAKURA", "10", "1", "0"),
                row("h4", "Filter d", "HONDA ACCORD 2008-2012", "SAKURA", "10", "0", "0")]
        catalog = Catalog.from_rows(rows)
        options = catalog.options()
        self.assertEqual(set(options), {"makes", "brands", "in_stock", "freshness"})
        # Counts are of available stock lines; a make with fewer than three is not offered.
        self.assertEqual(options["makes"], [{"key": "honda", "name": "Honda", "count": 5},
                                            {"key": "toyota", "name": "Toyota", "count": 3}])
        self.assertEqual(options["brands"][0], "SAKURA")
        self.assertEqual(set(options["brands"]), {"SAKURA", "SATO", "MANN", "BREMBO"})
        self.assertEqual(options["in_stock"], 9)
        # CR-V is the spelling used most; a single ACCORD (the other is out of stock) and
        # a single CIVIC are not offered.
        self.assertEqual(catalog.models("honda"), [{"key": "CRV", "count": 3, "name": "CR-V"}])
        self.assertEqual(catalog.models("toyota"), [{"key": "CAMRY", "count": 2,
                                                     "name": "CAMRY"}])
        self.assertEqual(catalog.models("no-such-make"), [])

    def test_resolve_vehicle_maps_a_decoder_answer_onto_catalogue_keys(self):
        resolve = self.catalog.resolve_vehicle
        self.assertEqual(resolve("TOYOTA", "Camry"), ("toyota", "CAMRY"))
        self.assertEqual(resolve("Toyota", "Corolla"), ("toyota", None))  # model not stocked
        self.assertEqual(resolve("toyota", None), ("toyota", None))
        self.assertEqual(resolve("MERCEDES-BENZ", "E-Class"), ("mercedes-benz", None))
        self.assertEqual(resolve("Land Rover", "Discovery"), ("land rover", None))
        self.assertEqual(resolve("VAG", "A4"), (None, None))  # a group is not one make
        for make in (None, "", "UNKNOWN MAKE", "<script>"):
            self.assertEqual(resolve(make, "Camry"), (None, None))
        rows = [row(f"h{index}", f"Part {index}", "HONDA CR-V 2012>", "X", "1", "1", "0")
                for index in range(2)]
        self.assertEqual(Catalog.from_rows(rows).resolve_vehicle("HONDA", "CR-V"),
                         ("honda", "CRV"))

    def test_staff_inventory_shows_codes_quantities_and_every_state(self):
        everything = self.catalog.inventory()
        self.assertEqual((everything["total"], everything["has_more"]), (8, False))
        self.assertEqual(everything["items"][0], {
            "code": "ZQ-1234", "name": PAD, "fits": "TOYOTA CAMRY 2012-2017", "brand": "SATO",
            "price": 45.5, "store": 3.0, "warehouse": 0.0, "state": stock.IN_STORE})
        self.assertEqual({item["state"] for item in everything["items"]},
                         {stock.IN_STORE, stock.WITHIN_HOURS, stock.UNAVAILABLE, stock.UNKNOWN})
        self.assertEqual(self.catalog.inventory(state="available")["total"], 6)
        out = self.catalog.inventory(state="out")
        self.assertEqual([item["code"] for item in out["items"]], ["344300"])
        self.assertEqual([item["code"] for item in
                          self.catalog.inventory(query="zq1234b")["items"]], ["ZQ-1234B"])
        self.assertEqual(self.catalog.inventory(make="honda", brand="sakura")["total"], 2)
        paged = self.catalog.inventory(size=3, page=3)
        self.assertEqual((len(paged["items"]), paged["has_more"]), (2, False))
        self.assertEqual(len(self.catalog.inventory(size=500)["items"]), 8)


class ContractTests(unittest.TestCase):
    def test_snapshot_reads_the_agreed_price_and_stock_columns(self):
        self.assertEqual(site_catalog.SNAPSHOT_COLUMNS,
                         ("column_06", stock.STORE_COLUMN, stock.TUZ11_COLUMN))
        self.assertEqual(site_catalog.PRICE_COLUMN, "column_06")
        aliases = ("price_raw", "stock_store", "stock_tuz11")
        for alias, column in zip(aliases, site_catalog.SNAPSHOT_COLUMNS, strict=True):
            self.assertIn(f"source_columns->>'{column}' AS {alias}", site_catalog.SNAPSHOT_SQL)
        # column_11 ("TUZ 6") has no agreed meaning and is never read.
        self.assertNotIn("column_11", site_catalog.SNAPSHOT_SQL)
        for name in ("source_product_id", "product_code", "product_name", "product_model",
                     "original_code", "duplicate_code", "brand", "last_observed_at"):
            self.assertIn(name, site_catalog.SNAPSHOT_SQL)

    def test_the_snapshot_statements_only_read_active_products(self):
        for statement in (site_catalog.SNAPSHOT_SQL, site_catalog.FINGERPRINT_SQL):
            text = " ".join(statement.split()).upper()
            self.assertTrue(text.startswith("SELECT "))
            self.assertTrue(text.endswith("FROM WAREHOUSE.WAREHOUSE_PRODUCTS WHERE IS_ACTIVE"))
            for word in (";", "INSERT", "UPDATE", "DELETE", "GARAGE."):
                self.assertNotIn(word, text)

    def test_names_borrowed_from_the_staff_modules_exist_and_behave(self):
        borrowed = {
            stock: ("IN_STORE", "STORE_COLUMN", "TUZ11_COLUMN", "UNAVAILABLE", "UNKNOWN",
                    "WITHIN_HOURS", "age_hours", "classify", "parse_count"),
            name_match: ("OUT", "year_fit"),
            categories: ("PATTERNS", "category_for_query", "query_patterns"),
            rules: ("Rules",),
        }
        for module, expected in borrowed.items():
            for name in expected:
                self.assertTrue(hasattr(module, name), f"{module.__name__}.{name}")
        self.assertLessEqual({"min_store", "min_tuz11", "stale_hours", "hide_after_hours"},
                             {field.name for field in fields(Rules)})
        self.assertEqual(stock.classify({"store": 1.0, "tuz11": 0.0}, 0.0, 0.0), stock.IN_STORE)
        self.assertEqual(stock.classify({"store": 0.0, "tuz11": 2.0}, 0.0, 0.0),
                         stock.WITHIN_HOURS)
        self.assertEqual(stock.classify({"store": 0.0, "tuz11": 0.0}, 0.0, 0.0),
                         stock.UNAVAILABLE)
        self.assertEqual(stock.parse_count("2,5"), 2.5)
        self.assertEqual(name_match.year_fit("TOYOTA CAMRY 2012-2017", "CAMRY", 2010),
                         name_match.OUT)
        self.assertEqual(categories.category_for_query("brake pads"), "brake_pads")
        self.assertEqual(len(categories.query_patterns("brake pads")), 2)


class FakeConnection:
    """What CatalogStore.refresh needs from psycopg: two reads and a dict-row cursor."""

    def __init__(self, fingerprint: tuple, rows: list[dict]) -> None:
        self.fingerprint, self.rows = fingerprint, rows
        self.statements: list[str] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement, parameters=None) -> FakeConnection:
        self.statements.append(str(statement))
        return self

    def fetchone(self) -> tuple:
        return self.fingerprint

    def fetchall(self) -> list[dict]:
        return list(self.rows)

    def cursor(self, row_factory=None) -> FakeConnection:
        return self


class CatalogStoreTests(unittest.TestCase):
    def test_refresh_loads_once_and_again_only_when_the_sync_moved(self):
        connection = FakeConnection((8, NOW, NOW), ROWS)
        store = CatalogStore("postgresql://runtime-fixture@127.0.0.1/catalog")
        self.assertIs(store.current(), site_catalog.EMPTY)
        self.assertFalse(store.loaded)
        with patch.object(site_catalog.psycopg, "connect", return_value=connection) as connect:
            self.assertTrue(store.refresh())
            self.assertEqual(connect.call_args.args,
                             ("postgresql://runtime-fixture@127.0.0.1/catalog",))
            first = store.current()
            self.assertEqual((len(first.items), store.loaded), (8, True))
            self.assertFalse(store.refresh())  # same fingerprint: nothing is re-read
            self.assertIs(store.current(), first)
            self.assertEqual(connection.statements.count(site_catalog.SNAPSHOT_SQL), 1)
            connection.fingerprint, connection.rows = (7, NOW, NOW), ROWS[:7]
            self.assertTrue(store.refresh())
            self.assertEqual(len(store.current().items), 7)
        for statement in connection.statements:
            self.assertTrue(statement.lstrip().upper().startswith("SELECT"), statement)

    def test_a_sync_moving_pages_reloads_at_most_every_ten_minutes(self):
        later = NOW + timedelta(minutes=5)
        connection = FakeConnection((8, NOW, NOW), ROWS)
        store = CatalogStore("postgresql://runtime-fixture@127.0.0.1/catalog")
        with patch.object(site_catalog.psycopg, "connect", return_value=connection):
            self.assertTrue(store.refresh())
            first = store.current()
            # The sync looked again and changed nothing: only the snapshot's age moves.
            connection.fingerprint = (8, NOW, later)
            self.assertFalse(store.refresh())
            self.assertIs(store.current(), first)
            self.assertEqual(first.observed_at, later)
            # It changed a product: not reloaded again within ten minutes of the last load...
            connection.fingerprint = (8, later, later)
            self.assertFalse(store.refresh())
            self.assertIs(store.current(), first)
            # ...but a product added or gone reloads at once,
            connection.fingerprint, connection.rows = (7, later, later), ROWS[:7]
            self.assertTrue(store.refresh())
            # and later edits once the ten minutes are over.
            store._loaded_at -= site_catalog.MIN_RELOAD_SECONDS
            connection.fingerprint = (7, later + timedelta(minutes=1), later)
            self.assertTrue(store.refresh())
        self.assertEqual(connection.statements.count(site_catalog.SNAPSHOT_SQL), 3)

    def test_what_a_new_snapshot_needs_is_built_before_anybody_can_read_it(self):
        connection = FakeConnection((8, NOW, NOW), ROWS)
        store = CatalogStore("postgresql://runtime-fixture@127.0.0.1/catalog")
        seen: list[tuple] = []

        def hook(catalog):
            seen.append((len(catalog.items), store.current() is catalog))

        def broken(catalog):
            raise RuntimeError("index failed")

        store.on_load(hook)
        store.on_load(broken)
        with patch.object(site_catalog.psycopg, "connect", return_value=connection), \
                self.assertLogs("automotive.site_catalog", "WARNING"):
            self.assertTrue(store.refresh())
        self.assertEqual(seen, [(8, False)])  # ran before the swap
        self.assertEqual(len(store.current().items), 8)  # a failing hook stops nothing
        # A hook added after the first load runs for the loaded snapshot, off this thread.
        done = threading.Event()
        store.on_load(lambda catalog: done.set())
        self.assertTrue(done.wait(5))

    def test_the_rules_given_to_the_store_shape_the_catalog(self):
        store = CatalogStore("postgresql://runtime-fixture@127.0.0.1/catalog",
                             Rules(min_store=2.0, min_tuz11=5.0))
        with patch.object(site_catalog.psycopg, "connect",
                          return_value=FakeConnection((8, NOW, NOW), ROWS)):
            store.refresh()
        self.assertEqual(names(store.current().search()), [PAD])

    def test_without_a_database_url_nothing_connects_and_no_thread_starts(self):
        store = CatalogStore("  ")
        with patch.object(site_catalog.psycopg, "connect") as connect:
            self.assertFalse(store.refresh())
            store.start()
            store.stop()
        connect.assert_not_called()
        self.assertIsNone(store._thread)
        self.assertIs(store.current(), site_catalog.EMPTY)

    def test_replace_installs_a_prepared_catalog(self):
        store = CatalogStore("")
        catalog = Catalog.from_rows(ROWS)
        store.replace(catalog)
        self.assertIs(store.current(), catalog)
        self.assertTrue(store.loaded)

    def test_the_background_refresh_keeps_the_last_catalog_when_the_database_fails(self):
        store = CatalogStore("postgresql://runtime-fixture@127.0.0.1/catalog",
                             poll_seconds=0.005)
        catalog = Catalog.from_rows(ROWS)
        store.replace(catalog)
        failure = psycopg.OperationalError("password=private-fixture")
        with (patch.object(site_catalog.psycopg, "connect", side_effect=failure) as connect,
              self.assertLogs("automotive.site_catalog", "WARNING") as logs):
            store.start()
            deadline = time.monotonic() + 5
            while connect.call_count < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            store.stop()
            store._thread.join(5)
        self.assertGreaterEqual(connect.call_count, 3)  # it kept trying
        self.assertFalse(store._thread.is_alive())
        self.assertIs(store.current(), catalog)
        self.assertNotIn("private-fixture", "\n".join(logs.output))


class CatalogRouteTests(SiteAppCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = self.serve(catalog=Catalog.from_rows(ROWS), buckets=RecordingBuckets())

    async def test_search_is_public_and_returns_only_the_four_fields(self):
        response = await self.client.get("/api/site/parts/search",
                                         params={"make": "Toyota", "q": "ხუნდები"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"items", "page", "total", "has_more", "freshness"})
        self.assertEqual(body["items"], [{"name": PAD, "fits": "TOYOTA CAMRY 2012-2017",
                                          "brand": "SATO", "price": 45.5}])
        everything = await self.client.get("/api/site/parts/search")
        self.assertEqual(everything.json()["total"], 5)
        by_code = await self.client.get("/api/site/parts/search", params={"q": "ZQ-1234"})
        self.assertEqual(names(by_code.json()), [PAD])
        for response in (everything, by_code):
            for private in PRIVATE:
                self.assertNotIn(private, response.text)

    async def test_search_parameters_narrow_the_result(self):
        cases = (
            ({"make": "honda", "model": "CRV", "year": "2015"}, ["Air filter"]),
            ({"make": "honda", "model": "CRV", "year": "2009"}, [AIR_KA]),
            ({"brand": "sakura"}, ["Air filter", AIR_KA]),
            ({"q": "brake pads"}, [PAD]),
            ({"q": "W712/95"}, [OIL_FILTER]),
            ({"make": "bmw", "page": "2"}, []),
        )
        for params, expected in cases:
            response = await self.client.get("/api/site/parts/search", params=params)
            self.assertEqual((response.status_code, names(response.json())), (200, expected),
                             params)

    async def test_search_refuses_control_characters_and_out_of_range_values(self):
        refused = ({"q": "pads\x00"}, {"q": "pa\x1bds"}, {"make": "toyota\n"},
                   {"model": "CAM\tRY"}, {"brand": "SA\x7fTO"}, {"q": "x" * 81},
                   {"make": "m" * 81}, {"year": "1949"}, {"year": "2101"}, {"year": "soon"},
                   {"page": "0"}, {"page": "201"}, {"page": "-1"})
        for params in refused:
            response = await self.client.get("/api/site/parts/search", params=params)
            self.assertEqual(response.status_code, 422, params)
            self.assertNotIn("items", response.text)
            for value in params.values():
                if len(value) > 3:
                    self.assertNotIn(value, response.text)

    async def test_options_and_models_feed_the_dropdowns(self):
        options = await self.client.get("/api/site/parts/options")
        self.assertEqual(options.status_code, 200)
        body = options.json()
        self.assertEqual(set(body), {"makes", "brands", "in_stock", "freshness"})
        self.assertEqual(body["makes"], [{"key": "toyota", "name": "Toyota", "count": 3}])
        # Brands with more stock lines come first.
        self.assertEqual(set(body["brands"][:2]), {"SATO", "SAKURA"})
        self.assertEqual(set(body["brands"][2:]), {"MANN", "BREMBO"})
        self.assertEqual((body["in_stock"], body["freshness"]),
                         (6, Catalog.from_rows(ROWS).freshness()))
        for private in PRIVATE:
            self.assertNotIn(private, options.text)
        for make in ("toyota", "TOYOTA", " Toyota "):
            models = await self.client.get("/api/site/parts/models", params={"make": make})
            self.assertEqual(models.json(), {"models": [{"key": "CAMRY", "count": 2,
                                                         "name": "CAMRY"}]})
        unknown = await self.client.get("/api/site/parts/models", params={"make": "lada"})
        self.assertEqual(unknown.json(), {"models": []})
        for params in ({}, {"make": ""}, {"make": "m" * 41}):
            response = await self.client.get("/api/site/parts/models", params=params)
            self.assertEqual(response.status_code, 422)

    async def test_vin_returns_only_what_the_form_needs(self):
        response = await self.client.post("/api/site/vin", json={"vin": VIN.lower()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "vehicle": {"make": "TOYOTA", "model": "Camry", "year": 2015,
                        "engine": "2.5 L Gasoline"},
            "make_key": "toyota", "model_key": "CAMRY", "recognized": True})
        self.assertEqual(self.vin.calls, [VIN])  # upper-cased before the decoder sees it
        for private in (VIN, VIN.lower(), "XLE", "private-plant-fixture", "PROVISIONAL",
                        "warnings"):
            self.assertNotIn(private, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_vin_answers_for_cars_the_catalogue_or_the_decoder_does_not_know(self):
        self.vin.result = {"vehicle": {"make": "LADA", "model": "Niva", "model_year": 1999}}
        response = await self.client.post("/api/site/vin", json={"vin": VIN})
        self.assertEqual(response.json(), {
            "vehicle": {"make": "LADA", "model": "Niva", "year": 1999, "engine": None},
            "make_key": None, "model_key": None, "recognized": True})
        for result in ({"vehicle": {}}, {}, {"vehicle": None}, {"vehicle": {"make": "TOYOTA"}}):
            self.vin.result = result
            response = await self.client.post("/api/site/vin", json={"vin": VIN})
            self.assertEqual(response.status_code, 200, result)
            self.assertIs(response.json()["recognized"], False)
            self.assertIsNone(response.json()["model_key"])

    async def test_an_invalid_vin_is_refused_before_the_decoder_or_the_budget(self):
        seventeen = ("1HGCM82633A00435I", "1HGCM82633A00435O", "1HGCM82633A00435Q",
                     "1HGCM82633A00435!", "1HGCM82633A0043 2", "1HGCM82633A00-352")
        for vin in seventeen:
            response = await self.client.post("/api/site/vin", json={"vin": vin})
            self.assertEqual(response.status_code, 422, vin)
            self.assertEqual(response.json(), {"detail": {"code": "INVALID_VIN"}})
        for body in ({"vin": VIN[:-1]}, {"vin": VIN + "1"}, {"vin": 12345678901234567},
                     {"vin": None}, {}, {"vin": VIN, "model_year": 2015}, [VIN]):
            response = await self.client.post("/api/site/vin", json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertNotIn(VIN[:-1], response.text)
        self.assertEqual(self.vin.calls, [])
        self.assertEqual((await self.client.get("/api/site/vin")).status_code, 405)

    async def test_the_daily_vin_budget_stops_decoding_for_everyone(self):
        client = self.serve(replace(self.settings, site_vin_daily_decodes=2),
                            catalog=Catalog.from_rows(ROWS), buckets=RecordingBuckets())
        other = self.connect(peer=("203.0.113.9", 1234))
        invalid = await client.post("/api/site/vin", json={"vin": "1HGCM82633A00435I"})
        self.assertEqual(invalid.status_code, 422)  # costs nothing
        self.assertEqual((await client.post("/api/site/vin", json={"vin": VIN})).status_code,
                         200)
        self.assertEqual((await other.post("/api/site/vin", json={"vin": VIN})).status_code,
                         200)
        for sender in (client, other):
            response = await sender.post("/api/site/vin", json={"vin": VIN})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json(), {"detail": {"code": "VIN_BUDGET_EXHAUSTED"}})
        self.assertEqual(len(self.vin.calls), 2)

    async def test_only_a_few_decodes_run_at_once_and_the_rest_are_not_charged(self):
        client = self.serve(replace(self.settings, site_vin_daily_decodes=5),
                            catalog=Catalog.from_rows(ROWS), buckets=RecordingBuckets())
        release, running = threading.Event(), []
        decode = self.vin.decode

        def slow(vin: str, model_year: int | None = None, *, deadline: float | None = None):
            running.append(vin)
            release.wait(10)
            return decode(vin, model_year, deadline=deadline)

        self.vin.decode = slow
        pending = [asyncio.create_task(client.post("/api/site/vin", json={"vin": VIN}))
                   for _ in range(9)]
        try:
            for _ in range(300):
                if len(running) == 4 and sum(task.done() for task in pending) == 5:
                    break
                await asyncio.sleep(0.01)
            busy = [task.result() for task in pending if task.done()]
            self.assertEqual(len(running), 4)
            self.assertEqual([(response.status_code, response.json()) for response in busy],
                             [(429, {"detail": {"code": "VIN_BUSY"}})] * 5)
            # The pages are still answered while the decoder is slow.
            home = await asyncio.wait_for(client.get("/api/site/parts/options"), timeout=5)
            self.assertEqual(home.status_code, 200)
        finally:
            release.set()
        responses = await asyncio.gather(*pending)
        self.assertEqual(sorted(response.status_code for response in responses),
                         [200] * 4 + [429] * 5)
        # The slots are free again, and of the day's five decodes only the four that ran
        # were charged: one more goes through, and only then is the allowance used up.
        self.assertEqual((await client.post("/api/site/vin", json={"vin": VIN})).status_code,
                         200)
        spent = await client.post("/api/site/vin", json={"vin": VIN})
        self.assertEqual((spent.status_code, spent.json()),
                         (429, {"detail": {"code": "VIN_BUDGET_EXHAUSTED"}}))

    async def test_an_unavailable_decoder_is_reported_without_its_message(self):
        self.vin.error = ProviderUnavailable("private upstream detail")
        response = await self.client.post("/api/site/vin", json={"vin": VIN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": {"code": "VIN_PROVIDER_UNAVAILABLE"}})
        self.assertNotIn("private", response.text)

    async def test_routes_answer_with_an_empty_catalog_before_the_first_load(self):
        client = self.serve()
        response = await client.get("/api/site/parts/search", params={"q": "filter"})
        self.assertEqual(response.json(), {"items": [], "page": 1, "total": 0,
                                           "has_more": False, "freshness": "stale"})
        self.assertEqual((await client.get("/api/site/parts/options")).json()["in_stock"], 0)


if __name__ == "__main__":
    unittest.main()
