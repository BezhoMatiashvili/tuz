"""The assistant's catalogue search: string mechanics, counting and ranking by stock.

The rows are synthetic but written the way the shop writes them: chassis codes instead of
model names, abbreviated Georgian part names in two word orders, misspelt models, year
marks, slash-separated cross numbers. Nothing here reads a database.
"""

from __future__ import annotations

import gc
import hashlib
import inspect
import json
import re
import threading
import unittest
import weakref
from datetime import UTC, datetime

from automotive import assistant_catalog, stock
from automotive.assistant_catalog import COLUMNS, CatalogIndex, Row, SearchError, index_for
from automotive.rules import Rules
from automotive.site_catalog import Catalog, CatalogStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PAD_FRONT, PAD_REAR = "სამუხრუჭე ხუნდი წნ", "სამუხრუჭე ხუნდი უკ"
AIR, AIR_REVERSED = "ფილტრი ჰაერის", "ჰაერის ფილტრი"
PUMP, LINK, TIE_ROD = "წყლის ტუმბო", "სტაბ. კრონშტეინი წნ", "წევის დაბოლოება"
ONE_LETTER = ("`vehicle_terms` contains 'E' (1 letter), which matches almost every row.",
              "Use a longer piece such as a chassis code or the model word.")


def row(sid: str, name: str, vehicle: str, brand: str, code: str = "", cross: str = "", *,
        price: str | None = "50", store: str | None = "0", tuz11: str | None = "0",
        shop: str | None = None) -> dict:
    """One row as SNAPSHOT_SQL returns it."""
    return {"source_product_id": sid, "product_code": shop or f"2040{int(sid):06d}",
            "product_name": name, "product_model": vehicle, "original_code": code,
            "duplicate_code": cross, "brand": brand, "last_observed_at": NOW,
            "price_raw": price, "stock_store": store, "stock_tuz11": tuz11}


MB_PAD_CROSS = "GDB1737/PN0395/D1342-8453/PN0395W"
ROWS = [
    # Mercedes pads are filed by chassis code; the words "E-Class" are rare and vary.
    row("15897", "სამუხრუჭე ხუნდი წნ.", "MB W204,W211,W212", "SATO", "PN0395W", MB_PAD_CROSS,
        price="115", store="4"),
    row("28278", PAD_FRONT, "MB W204,W211,W212", "BREMBO", "P50069",
        f"{MB_PAD_CROSS}/13.0460-2734.2", price="180", store="1", tuz11="1"),
    row("7425", PAD_FRONT, "MB W204,W211,W212", "ABS", "37586",
        f"{MB_PAD_CROSS}/13.0460-2734.2", price="90", tuz11="1"),
    row("3199", "სამუხრუჭე ხუნდი უკ.", "MB CLS W218, W212", "SB", "BP31344W",
        "GDB1546/PN0344/D986-7888/PN0344W", price="85", tuz11="1"),
    row("30790", PAD_REAR, "MB CLS W218, W212", "ATE", "13.0460-7259.2",
        "GDB1546/PN0344/D986-7888/PN0344W", price="160"),
    row("40001", PAD_FRONT, "MB C205 W213 E CLASSE", "SATO", "PN0500"),
    row("40002", "სამუხრუჭე ხუნდი ხელის", "MB W212", "FERODO", "FSB4026"),
    row("40003", "სამუხრ.ხუნდი წინა", "MB E-CLASS W211", "TRW", "GDB1542", store="x",
        tuz11="y"),
    row("40004", PAD_FRONT, "MB E CLASS 2003-2009", "TRW", "GDB1543"),
    # Toyota pads: the model word, a year mark, one HTML-escaped cell.
    row("6675", PAD_FRONT, "TOYOTA/LEXUS CAMRY 2006->", "SATO", "PN1521",
        "D1222-8331/GDB3429/KD2765/PN1521", price="85", store="6", tuz11="5"),
    row("14458", PAD_FRONT, "TOYOTA/LEXUS CAMRY 2017&gt;", "SATO", "PN1857",
        "PN1857/04465-33480/SP4275", price="100", store="5", tuz11="2"),
    row("3226", PAD_FRONT, "TOYOTA/LEXUS CAMRY 2006->", "SB", "BP21521",
        "PN1521/GDB3429/KD2765", price="60", tuz11="3"),
    row("10354", PAD_FRONT, "TOYOTA/LEXUS CAMRY 2006->", "ABS", "37677", "PN1521/GDB3429",
        price="55"),
    row("50050", "სამუხრუჭე ხუნდი წნ/აზიური", "TOYOTA/LEXUS CAMRY", "SATO", "PN0828",
        "KD2765", price="80"),
    row("50051", "სამუხრუჭე დისკი წნ", "TOYOTA COROLLA", "SATO", "ZZ-1", shop="GDB3429"),
    # One model written three ways, and its neighbours.
    row("13420", PUMP, "HONDA CR-V 2.0", "GMB", "GWHO40A", "19200-P72-A01/GWHO-40A",
        price="110", store="1", tuz11="1"),
    row("8070", PUMP, "HONDA CR-V II 2.0", "NWP", "H50", "19200-RFE-003", price="150",
        tuz11="1"),
    row("10012", PUMP, "HONDA CRV 2001>", "ASHIKA", "35-04-403", "GWHO51A", price="150",
        tuz11="3"),
    row("13581", PUMP, "HONDA ACCORD CR V", "NPW", "H52", "19200-RAA-A01", price="130",
        tuz11="1"),
    row("50001", PUMP, "HONDA CRV III 2,4", "AISIN", "WPH-047", price="140"),
    row("45903", "წყლის ტუმბო TOYOTA LC90", "", "AISIN", "WPT-113", price="0"),
    row("50010", f"{TIE_ROD} მჯ", "TOYOTA/LEXUS RAV-4 2005->", "JIKIU", "ET21221R", store="1"),
    row("50011", f"{TIE_ROD} მც", "TOYOTA RAV 4 2019>", "TEKNOROT", "T-1071"),
    row("50012", TIE_ROD, "TOYOTA RAV4 IV 2015>", "CTR", "CE0900", tuz11="2"),
    # Air filters in both word orders, with tokens that contain shorter ones.
    row("50020", AIR, "BMW 5 SERIES F10 3.0 N52", "HENGST", "E1039L",
        "A0485/C30003/13717590597", price="80"),
    row("50021", AIR_REVERSED, "BMW F10", "ASHIKA", "FA-0112JM", "13717800151/A0526",
        price="75", store="1"),
    row("50022", AIR, "BMW E60 2.5", "MANN-FILTER", "C 30 005", price="60"),
    row("50023", AIR, "MB E600 W124", "MANN-FILTER", "C 2256", price="60"),
    row("50024", AIR, "VAG AUDI A4 B8", "FILTRON", "AP139/4", price="45", store="2"),
    row("50025", AIR, "TOYOTA GA4 COROLLA", "FILTRON", "AP104/2", "17801-15070", price="45"),
    row("50026", AIR, "MAZDA 3 1.6", "FILTRON", "AP104/3", price="45", tuz11="1"),
    row("50027", AIR, "BMW 3 SERIES E90 2.5", "FILTRON", "AP 051/2", price="45"),
    # A misspelt model hides its row from the right spelling.
    row("50030", "მორგვი უკ", "HONDA ACCORD 2013", "SATO", "42200-T2A-A51", price="200",
        store="1"),
    row("50031", "მორგვი უკ", "HONDA ACCROD 2013>", "SATO", "42200-TWA-A52", price="200",
        tuz11="1"),
    # Stabiliser links: an abbreviated name, numbers in brackets, shared cross numbers.
    row("4386", LINK, "HYUNDAI ELANTRA 06-> OPEL VECTRA B,CO +", "JIKIU", "LS13001",
        "LS13001/CLKH23/CLKH30/LS11090/LS25001", price="40", store="2", tuz11="146"),
    row("4658", LINK, "HYUNDAI ELANTRA 06-> OPEL VECTRA B,CO +", "CTR", "CL0275(CLKH30)",
        "CLKH23/CLKH30/LS11090/LS25001/LS13001", price="27", store="2", tuz11="2"),
    row("4659", LINK, "HYUNDAI ELANTRA 06->", "TEKNOROT", "HY-376",
        "CLKH23/CLKH30/LS11090/LS25001", price="30", store="2", tuz11="1"),
    row("4629", LINK, "HYUNDAI/KIA ELANTRA 2015>", "CTR", "CL0311(CLKH65)",
        "54830-F2000/LS11029", price="40", store="2"),
    row("4746", "სტაბ.კრონშტეინი წნ", "HYUNDAI/KIA ELANTRA 2015>", "JIKIU", "LS11029",
        "54830-F2000/LS11029", price="40", store="1"),
    # Numbers written with the brand's name, a pack suffix, two numbers in one cell.
    row("48972", "სტაბ. მილისა წნ", "MB W204", "FEBI", "26096 FEBI", price="20"),
    row("48973", "სტაბ. მილისა წნ", "MB W204", "MEYLE", "0140320100", "26096/A2043231865",
        price="20", store="1"),
    row("50040", "ამორტიზატორი წნ", "MMC L200 2005>", "KYB", "343313/343272", price="45,50"),
    row("50041", "ამორტიზატორი წნ", "MMC L200 2005>", "FEBI", "44994 01", "LS&amp;K100"),
    row("50060", "ანთების სანთელი", "", "NGK", "2743"),
    row("50061", "ანთების სანთელი", "", "NGK", "4589", store="3"),
]
INDEX = CatalogIndex.from_rows(ROWS)
MB_PADS = {"15897", "28278", "7425", "3199", "30790", "40001", "40002", "40003", "40004"}
CAMRY_PADS = {"6675", "14458", "3226", "10354", "50050"}
CR_V = {"13420", "8070", "10012", "13581", "50001"}
RAV4 = {"50010", "50011", "50012"}
AIR_FILTERS = {"50020", "50021", "50022", "50023", "50024", "50025", "50026", "50027"}
LINKS = {"4386", "4658", "4659", "4629", "4746"}


def ids(result: dict) -> list[str]:
    return [found[0] for found in result["rows"]]


def found(**terms) -> set[str]:
    """Ids of every row a search returns; these catalogues fit in one result."""
    result = INDEX.search(limit=60, **terms)
    assert result["total"] <= 60 and not result["other_vehicle_texts"]
    return set(ids(result))


def matched(result: list[dict]) -> list[tuple[str, str, str]]:
    return [(hit["row"][0], hit["matched"], hit["field"]) for hit in result]


class UnstockedMatchingTests(unittest.TestCase):
    def test_stock_and_other_cars_do_not_consume_the_bridge_limit(self):
        rows = [row(str(number), AIR, "HONDA CIVIC", "JS", store="1")
                for number in range(1, 71)]
        rows += [row("71", AIR, "HONDA CIVIC", "JS"),
                 row("72", AIR, "HONDA ACCORD", "JS"),
                 row("73", AIR + " REAR", "HONDA CIVIC", "JS"),
                 row("74", AIR, "CIVIC", "JS"),
                 row("75", PUMP, "HONDA CIVIC", "JS")]
        index = CatalogIndex.from_rows(rows)
        args = {"make_terms": ["HONDA"], "vehicle_terms": ["CIVIC"],
                "part_terms": [AIR], "exclude_terms": ["REAR"]}
        self.assertEqual([item.id for item in index.out_of_stock_matches(**args, limit=1)],
                         ["71"])
        self.assertEqual([item.id for item in index.out_of_stock_matches(
            **{**args, "make_terms": []}, limit=60)], ["71", "74"])
        self.assertEqual(index.out_of_stock_matches(), [])

    def test_unstocked_candidate_selection_stays_bounded(self):
        index = CatalogIndex.from_rows(
            [row(str(number), AIR, "HONDA CIVIC", "JS") for number in range(1, 101)])
        self.assertEqual(len(index.out_of_stock_matches(vehicle_terms=["CIVIC"], limit=1000)),
                         60)


class WordMatchingTests(unittest.TestCase):
    def test_a_name_is_found_in_either_word_order(self):
        for term in ("ფილტრი ჰაერის", "ჰაერის ფილტრი", "ჰაერის  ფილტრ"):
            with self.subTest(term=term):
                self.assertEqual(found(part_terms=[term]), AIR_FILTERS)
        self.assertEqual(found(part_terms=["ფილტრი ზეთის"]), set())

    def test_a_georgian_word_matches_the_start_of_a_word(self):
        pads = MB_PADS | CAMRY_PADS
        self.assertEqual(found(part_terms=["ხუნდ"]), pads)
        self.assertEqual(found(part_terms=["ხუნდი"]), pads)
        # A stem also matches after a full stop: `სამუხრ.ხუნდი`.
        self.assertEqual(found(part_terms=["სამუხრ"]), pads | {"50051"})
        # The full word also finds the shop's shortening of it: `სამუხრ.ხუნდი` (40003).
        self.assertEqual(found(part_terms=["სამუხრუჭე ხუნდ"]), pads)
        self.assertEqual(found(part_terms=["უნდი"]), set())  # not the middle of a word
        self.assertEqual(found(part_terms=["ხუნდები"]), set())  # longer than what is written

    def test_capital_georgian_letters_fold_onto_the_same_word(self):
        capitals = "ხუნდ".upper()
        self.assertNotEqual(capitals, "ხუნდ")  # another Unicode block; the reason for casefold
        self.assertEqual(found(part_terms=[capitals]), MB_PADS | CAMRY_PADS)

    def test_a_latin_word_equals_a_token_however_it_is_spaced(self):
        cases = (
            (("CR-V", "CRV", "CR V", "cr-v", "Cr V"), CR_V),
            (("RAV4", "RAV-4", "RAV 4", "rav4"), RAV4),
            (("E-CLASS", "E CLASS", "ECLASS"), {"40003", "40004"}),
            (("W212",), {"15897", "28278", "7425", "3199", "30790", "40002"}),
            (("X-TRAIL",), set()),
        )
        for terms, expected in cases:
            for term in terms:
                with self.subTest(term=term):
                    self.assertEqual(found(vehicle_terms=[term]), expected)

    def test_a_latin_word_never_matches_inside_a_longer_token(self):
        for term, expected in (("E60", {"50022"}), ("E600", {"50023"}), ("A4", {"50024"}),
                               ("GA4", {"50025"}), ("W21", set()), ("21", set()),
                               ("CAMR", set()), ("RAV", {"50010", "50011"}),
                               ("CLASS", {"40003", "40004"}), ("CLASSE", {"40001"})):
            with self.subTest(term=term):
                self.assertEqual(found(vehicle_terms=[term]), expected)

    def test_a_number_of_three_digits_also_finds_the_code_that_carries_it(self):
        # Customers say "Mercedes 212" and "Prado 150"; the catalogue writes W212 and J150.
        w212 = {"15897", "28278", "7425", "3199", "30790", "40002"}
        self.assertEqual(found(vehicle_terms=["212"]), w212)
        self.assertEqual(found(vehicle_terms=["W212"]), w212)
        self.assertEqual(found(vehicle_terms=["600"]), {"50023"})  # the row writes E600
        # Two digits are no code, and letters after the digits make another word.
        self.assertEqual(found(vehicle_terms=["60"]), set())
        self.assertEqual(found(vehicle_terms=["12"]), set())
        index = CatalogIndex.from_rows([
            row("1", PAD_REAR, "TOYOTA/LEXUS / GX470 J150", "SATO", "PN1243"),
            row("2", PAD_REAR, "TOYOTA LC 120, 150", "ATE", "13.0460-5744.2"),
            row("3", PAD_REAR, "TOYOTA COROLLA 150D", "ABS", "37001"),
            row("4", PAD_REAR, "TOYOTA LAND CRUISER GRJ150", "TRW", "GDB3426"),
            row("5", PAD_REAR, "TOYOTA HILUX KUN1500", "TRW", "GDB0001"),
        ])
        ids = {entry[0] for entry in index.search(vehicle_terms=["150"])["rows"]}
        self.assertEqual(ids, {"1", "2", "4"})
        ids = {entry[0] for entry in index.search(vehicle_terms=["prado 150", "J150"])["rows"]}
        self.assertEqual(ids, {"1"})

    def test_a_trailing_star_matches_the_start_of_a_token(self):
        self.assertEqual(found(vehicle_terms=["CAMR*"]), CAMRY_PADS)
        self.assertEqual(found(vehicle_terms=["RAV*"]), RAV4)
        self.assertEqual(found(vehicle_terms=["W21*"]), MB_PADS - {"40004"})
        self.assertEqual(found(vehicle_terms=["CR-V*"]), CR_V)  # the plain word, and more
        self.assertEqual(found(vehicle_terms=["ACC*"]), {"13581", "50030", "50031"})

    def test_a_decimal_is_one_number_whichever_mark_it_is_written_with(self):
        self.assertEqual(found(vehicle_terms=["2.4"]), {"50001"})  # the row writes `2,4`
        self.assertEqual(found(vehicle_terms=["2,4"]), {"50001"})
        self.assertEqual(found(vehicle_terms=["2.0"]), {"13420", "8070"})
        self.assertEqual(found(vehicle_terms=["2.5", "3.0"]), {"50020", "50022", "50027"})
        index = CatalogIndex.from_rows([
            row("1", PUMP, "VAG GOLF 2.0TDI 2005>", "SKF", "VKPC-0001"),
            row("2", PUMP, "TOYOTA HILUX 2,5D", "AISIN", "WPT-0002"),
            row("3", PUMP, "MMC OUTLANDER 4B12 2.4L 200", "GMB", "GWM-0003")])
        # The number, the letters written onto it, and both together are all findable.
        for term, expected in (("2.0", ["1"]), ("TDI", ["1"]), ("2.0TDI", ["1"]),
                               ("2.0 TDI", ["1"]), ("2.5", ["2"]), ("2.5D", ["2"]),
                               ("2,5D", ["2"]), ("2.4", ["3"]), ("2.4L", ["3"]), ("200", ["3"]),
                               ("L200", []), ("0TDI", []), ("5D", ["2"])):
            with self.subTest(term=term):
                self.assertEqual(ids(index.search(vehicle_terms=[term])), expected)
        # A lone digit is the model, never half of an engine size.
        self.assertEqual(found(vehicle_terms=["5 SERIES"]), {"50020"})
        self.assertEqual(found(vehicle_terms=["3 SERIES"]), {"50027"})
        self.assertEqual(found(make_terms=["MAZDA"], vehicle_terms=["3"]), {"50026"})
        self.assertEqual(found(vehicle_terms=["MAZDA 3"]), {"50026"})
        self.assertEqual(found(vehicle_terms=["MAZDA 6"]), set())

    def test_every_word_of_a_term_must_be_in_the_row_in_any_order(self):
        self.assertEqual(found(part_terms=["სტაბ კრონ"]), LINKS)
        self.assertEqual(found(part_terms=["კრონ სტაბ"]), LINKS)
        self.assertEqual(found(part_terms=["სტაბ მილისა"]), {"48972", "48973"})
        self.assertEqual(found(part_terms=["სტაბ"]), LINKS | {"48972", "48973"})
        self.assertEqual(found(vehicle_terms=["ELANTRA HYUNDAI"]), LINKS)
        self.assertEqual(found(vehicle_terms=["KIA ELANTRA"]), {"4629", "4746"})
        self.assertEqual(found(vehicle_terms=["TOYOTA/LEXUS CAMRY"]), CAMRY_PADS)
        self.assertEqual(found(vehicle_terms=["ELANTRA CAMRY"]), set())
        # Words and codes of one term may sit in the name, the vehicle text or the brand.
        self.assertEqual(found(part_terms=["ხუნდ BREMBO W212"]), {"28278"})

    def test_one_georgian_letter_among_other_words_is_a_whole_word(self):
        index = CatalogIndex.from_rows([
            row("1", "სამუხრუჭე სუპორტის ს/კ წნ", "TOYOTA", "SATO", "A-1000"),
            row("2", "სამუხრუჭე სუპორტის დგუში", "TOYOTA", "SATO", "A-2000")])
        self.assertEqual(ids(index.search(part_terms=["სუპორტის ს/კ"])), ["1"])
        self.assertEqual(ids(index.search(part_terms=["სუპორტის ს"])), ["1"])
        self.assertEqual(set(ids(index.search(part_terms=["სუპორტის სა"]))), {"1", "2"})

    def test_a_car_written_only_in_the_name_is_found(self):
        result = INDEX.search(make_terms=["TOYOTA"], vehicle_terms=["LC90"],
                              part_terms=["წყლის ტუმბო"])
        self.assertEqual(ids(result), ["45903"])
        self.assertEqual((result["total"], result["matched_vehicle"]), (1, 1))
        self.assertEqual(result["rows"][0][1:3], ["წყლის ტუმბო TOYOTA LC90", ""])
        # When it is one of the rows not shown, it is counted under what its name says.
        result = INDEX.search(make_terms=["TOYOTA", "HONDA"], vehicle_terms=["CR-V"],
                              part_terms=["წყლის ტუმბო"], limit=2)
        self.assertEqual(ids(result), ["13420", "8070"])
        self.assertEqual(result["other_vehicle_texts"],
                         [{"text": "TOYOTA LC90", "rows": 1, "available": 0}])
        result = INDEX.search(part_terms=["სანთელი"], limit=1)
        self.assertEqual(ids(result), ["50061"])
        self.assertEqual(result["other_vehicle_texts"],
                         [{"text": "(no vehicle text)", "rows": 1, "available": 0}])


class SearchResultTests(unittest.TestCase):
    def test_groups_are_anded_terms_in_a_group_are_ored(self):
        self.assertEqual(found(make_terms=["MB", "MERCEDES"], part_terms=["ხუნდ"]), MB_PADS)
        self.assertEqual(found(make_terms=["MB", "TOYOTA"], part_terms=["ხუნდ"]),
                         MB_PADS | CAMRY_PADS)
        self.assertEqual(found(make_terms=["MB"], part_terms=["ხუნდ", "მილისა"]),
                         MB_PADS | {"48972", "48973"})
        self.assertEqual(found(make_terms=["HONDA"], part_terms=["ხუნდ"]), set())
        self.assertEqual(found(make_terms="MB", part_terms="ხუნდ"), MB_PADS)  # bare strings

    def test_exclude_terms_remove_rows(self):
        self.assertEqual(found(make_terms=["MB"], part_terms=["ხუნდ"], exclude_terms=["ხელის"]),
                         MB_PADS - {"40002"})
        self.assertEqual(found(make_terms=["MB"], part_terms=["ხუნდ"],
                               exclude_terms=["ხელის", "უკ", "W213"]),
                         {"15897", "28278", "7425", "40003", "40004"})
        result = INDEX.search(make_terms=["MB"], part_terms=["ხელის"], exclude_terms=["ხუნდ"])
        self.assertEqual((result["total"], result["rows"]), (0, []))
        self.assertEqual(result["why"], {"make_terms_alone": 12, "part_terms_alone": 1,
                                         "removed_by_exclude_terms": 1})

    def test_a_small_result_returns_every_row_with_vehicle_matches_first(self):
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W212"], part_terms=["ხუნდ"])
        self.assertEqual(ids(result), ["15897", "28278", "3199", "7425", "30790", "40002",
                                       "40003", "40001", "40004"])
        self.assertEqual((result["total"], result["available"], result["matched_vehicle"]),
                         (9, 4, 6))
        self.assertEqual((result["other_vehicle_texts"], result["note"], result["why"]),
                         ([], "", {}))

    def test_rows_are_ordered_by_stock_then_matched_terms_then_id(self):
        result = INDEX.search(make_terms=["MB"], part_terms=["ხუნდ"])
        # In the store, only in the warehouse, stock unknown, unavailable; ids in number order.
        self.assertEqual(ids(result), ["15897", "28278", "3199", "7425", "40003", "30790",
                                       "40001", "40002", "40004"])
        states = [INDEX.row(product).state for product in ids(result)]
        self.assertEqual(states, [stock.IN_STORE] * 2 + [stock.WITHIN_HOURS] * 2
                         + [stock.UNKNOWN] + [stock.UNAVAILABLE] * 4)
        # 7425 names both chassis codes and 3199 one, so it comes first within its stock state.
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W212", "W204"],
                              part_terms=["ხუნდ"])
        self.assertEqual(ids(result), ["15897", "28278", "7425", "3199", "30790", "40002",
                                       "40003", "40001", "40004"])

    def test_a_maker_number_finds_its_other_revisions_never_other_numbers(self):
        index = CatalogIndex.from_rows([
            row("201", PAD_FRONT, "VAG WV", "SATO", "PN0001", "CX0001/D0001/7QX445566B",
                store="1"),
            row("202", PAD_FRONT, "VAG", "TRW", "GDB0001", "7QX445566", store="0"),
            row("203", PAD_FRONT, "VAG", "ATE", "13.0000-1111.2", "7QX445567B"),
            row("204", PAD_FRONT, "TOYOTA", "SATO", "PN1111", "11111-22222")])
        self.assertEqual(index.revisions("7QX445566D"),
                         [("201", "7QX445566B"), ("202", "7QX445566")])
        self.assertEqual(index.revisions("7QX445566B"), [("202", "7QX445566")])
        self.assertEqual(index.revisions("11111-22223"), [])  # ends in a digit: no revision
        self.assertEqual(index.revisions("7QX4D"), [])  # too short to carry a revision

    def test_an_fmsi_number_finds_the_rows_that_write_it_with_its_plate(self):
        index = CatalogIndex.from_rows([
            row("301", PAD_FRONT, "VW GOLF V", "SATO", "PN0003", "GDB0003/D0003-0003",
                store="1"),
            row("302", PAD_FRONT, "VW", "TRW", "GDB0003", "D0003", store="1"),
            row("303", PAD_FRONT, "VW", "ATE", "13.0000-3333.2", "D00031-0003")])
        hits = index.by_codes(["D0003"])["D0003"]
        self.assertEqual([(hit["row"][0], hit["matched"]) for hit in hits],
                         [("302", "D0003"), ("301", "D0003-0003")])
        hits = index.by_codes(["D0003-0003"])["D0003-0003"]
        self.assertEqual([hit["row"][0] for hit in hits], ["301"])

    def test_a_part_word_ranks_its_own_rows_before_rows_of_a_longer_word(self):
        # `ზეთი` (oil) also matches `ზეთის` (of oil): among rows matching as many terms, the
        # oil itself comes before the oil filters, whatever their ids.
        index = CatalogIndex.from_rows([
            row("101", "ზეთის ფილტრი", "BMW E90", "MANN", store="2"),
            row("102", "ფილტრი ზეთის", "BMW F30", "FILTRON", store="2"),
            row("103", "ზეთის ფილტრი", "BMW", "HENGST", store="2"),
            row("65812", "ზეთი BMW TWIN POWER TURBO 5W30 LL01 1L", "UNIVERSAL", "BMW",
                store="2")])
        result = index.search(vehicle_terms=["BMW"], part_terms=["ზეთი"], limit=2)
        self.assertEqual(ids(result)[0], "65812")
        self.assertEqual(result["matched_vehicle"], 4)

    def test_a_large_result_returns_vehicle_matches_and_counts_the_other_texts(self):
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W212"], part_terms=["ხუნდ"],
                              limit=3)
        self.assertEqual(ids(result), ["15897", "28278", "3199"])
        self.assertEqual((result["total"], result["available"], result["matched_vehicle"]),
                         (9, 4, 6))
        self.assertEqual(result["other_vehicle_texts"], [
            {"text": "MB C205 W213 E CLASSE", "rows": 1, "available": 0},
            {"text": "MB E CLASS 2003-2009", "rows": 1, "available": 0},
            {"text": "MB E-CLASS W211", "rows": 1, "available": 0}])
        self.assertEqual(result["note"], "Showing 3 of 6 rows that match a vehicle term; add "
                                         "more words to a term or exclude_terms to narrow the "
                                         "result.")
        # Every vehicle match fits: they are all shown and only the others are counted.
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W212"], part_terms=["ხუნდ"],
                              limit=7)
        self.assertEqual(ids(result), ["15897", "28278", "3199", "7425", "30790", "40002"])
        self.assertEqual(len(result["other_vehicle_texts"]), 3)
        self.assertEqual(result["note"], "Rows that match the other terms but no vehicle "
                         "term: 3; other_vehicle_texts counts how they name their cars.")

    def test_texts_that_begin_like_a_vehicle_term_lead_the_other_texts(self):
        index = CatalogIndex.from_rows([
            row("1", PAD_FRONT, "VAG A4", "SATO", "PN-0001", store="1"),
            row("2", PAD_FRONT, "VAG A4", "SB", "PN-0002"),
            row("3", PAD_REAR, "VAG A4", "ABS", "PN-0003"),
            row("4", PAD_FRONT, "VAG PASSAT", "SATO", "PN-0004"),
            row("5", PAD_REAR, "VAG PASSAT", "SATO", "PN-0005"),
            row("6", PAD_FRONT, "VAG VW GOLF5,JETTA", "SATO", "PN-0006", tuz11="2"),
            row("7", PAD_FRONT, "VAG GOLF 7", "SATO", "PN-0007"),
            row("8", PAD_FRONT, "VAG GOLF 7", "SB", "PN-0008")])
        result = index.search(make_terms=["VAG"], vehicle_terms=["GOLF"], part_terms=["ხუნდ"],
                              limit=2)
        # `GOLF` is not `GOLF5`, but that text is the first thing to read about the rest.
        self.assertEqual((ids(result), result["matched_vehicle"]), (["7", "8"], 2))
        self.assertEqual(result["other_vehicle_texts"], [
            {"text": "VAG VW GOLF5,JETTA", "rows": 1, "available": 1},
            {"text": "VAG A4", "rows": 3, "available": 1},
            {"text": "VAG PASSAT", "rows": 2, "available": 0}])
        self.assertEqual(ids(index.search(make_terms=["VAG"], vehicle_terms=["GOLF*"],
                                          part_terms=["ხუნდ"], limit=3)), ["6", "7", "8"])

    def test_no_vehicle_match_shows_how_the_catalogue_names_the_cars(self):
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W999", "E-KLASSE"],
                              part_terms=["ხუნდ"], limit=3)
        self.assertEqual((result["rows"], result["total"], result["matched_vehicle"]),
                         ([], 9, 0))
        self.assertEqual(result["other_vehicle_texts"][:2], [
            {"text": "MB W204,W211,W212", "rows": 3, "available": 3},
            {"text": "MB CLS W218, W212", "rows": 2, "available": 1}])
        self.assertEqual(len(result["other_vehicle_texts"]), 6)
        self.assertEqual(result["unmatched_terms"], ["W999", "E-KLASSE"])
        self.assertEqual((result["why"], result["other_part_names"]), ({}, []))
        self.assertTrue(result["note"].startswith("No row matches a vehicle term;"))

    def test_without_vehicle_terms_a_large_result_is_cut_and_the_rest_counted(self):
        index = CatalogIndex.from_rows([
            row(str(1000 + number), "ღვედი", "UNIVERSAL", "DAYCO", f"6PK{1000 + number}")
            for number in range(70)])
        result = index.search(part_terms=["ღვედი"], limit=1000)  # clamped to 60
        self.assertEqual((len(result["rows"]), result["total"]), (60, 70))
        self.assertEqual(ids(result)[:3], ["1000", "1001", "1002"])
        self.assertEqual(result["other_vehicle_texts"],
                         [{"text": "UNIVERSAL", "rows": 10, "available": 0}])
        self.assertEqual(result["note"], "Showing 60 of 70 rows; add vehicle_terms, more words "
                                         "to a term or exclude_terms to narrow the result.")
        for limit, shown in ((0, 1), (-5, 1), (1, 1), (2.9, 2), (60, 60), (61, 60),
                             ("7", 7), ("many", 40), (None, 40)):
            with self.subTest(limit=limit):
                self.assertEqual(len(index.search(part_terms=["ღვედი"], limit=limit)["rows"]),
                                 shown)
        self.assertEqual(len(index.search(part_terms=["ღვედი"])["rows"]), 40)

    def test_only_vehicle_terms_filter_by_them(self):
        result = INDEX.search(vehicle_terms=["W212"])
        self.assertEqual(ids(result), ["15897", "28278", "3199", "7425", "30790", "40002"])
        self.assertEqual((result["total"], result["available"], result["matched_vehicle"]),
                         (6, 4, 6))
        self.assertEqual(INDEX.search(vehicle_terms=["W212"], exclude_terms=["უკ"])["total"], 4)

    def test_in_stock_only_hides_the_rest_but_still_counts_them(self):
        result = INDEX.search(make_terms=["MB"], part_terms=["ხუნდ"], in_stock_only=True)
        self.assertEqual(ids(result), ["15897", "28278", "3199", "7425"])
        self.assertEqual((result["total"], result["available"]), (9, 4))
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W213"], part_terms=["ხუნდ"],
                              in_stock_only=True)
        self.assertEqual((ids(result)[:2], result["matched_vehicle"]), (["15897", "28278"], 0))
        result = INDEX.search(make_terms=["MB"], part_terms=["ხელის"], in_stock_only=True)
        self.assertEqual((result["rows"], result["total"], result["available"]), ([], 1, 0))
        self.assertEqual(result["note"], "None of the matching rows (1) is in stock; set "
                                         "in_stock_only to false to see them.")
        self.assertEqual(result["why"], {})

    def test_zero_rows_say_what_each_group_matches_alone(self):
        result = INDEX.search(make_terms=["TOYOTA"], vehicle_terms=["CAMRY"],
                              part_terms=["კალოტკა"])
        self.assertEqual((result["total"], result["available"], result["rows"]), (0, 0, []))
        self.assertEqual(result["why"], {"make_terms_alone": 11, "vehicle_terms_alone": 5,
                                         "part_terms_alone": 0})
        self.assertEqual(result["unmatched_terms"], ["კალოტკა"])
        # What the shop does call the parts of this car.
        self.assertEqual(result["other_part_names"],
                         [{"text": "სამუხრუჭე ხუნდი", "rows": 5, "available": 3}])
        self.assertEqual(result["note"], "No row matches every group; `why` counts what each "
                                         "group matches alone.")
        nothing = INDEX.search(vehicle_terms=["W21"])
        self.assertEqual((nothing["total"], nothing["why"]), (0, {"vehicle_terms_alone": 0}))

    def test_other_part_names_come_from_the_narrowest_car_that_has_rows(self):
        # No make given: the names of the rows for the vehicle term.
        result = INDEX.search(vehicle_terms=["CR-V"], part_terms=["ზეთის ფილტრი"])
        self.assertEqual(result["other_part_names"],
                         [{"text": "წყლის ტუმბო", "rows": 5, "available": 4}])
        # The vehicle term matches nothing, so the make's names are listed instead.
        result = INDEX.search(make_terms=["HONDA"], vehicle_terms=["JAZZ"],
                              part_terms=["ზეთის ფილტრი"])
        self.assertEqual(result["other_part_names"], [
            {"text": "წყლის ტუმბო", "rows": 5, "available": 4},
            {"text": "მორგვი უკ", "rows": 2, "available": 2}])
        # The part exists for the make: the vehicle texts explain the miss, not the names.
        result = INDEX.search(make_terms=["HONDA"], vehicle_terms=["JAZZ"],
                              part_terms=["წყლის ტუმბო"])
        self.assertEqual((result["other_part_names"], result["matched_vehicle"]), ([], 0))

    def test_without_part_terms_a_cut_result_counts_what_the_parts_are_called(self):
        result = INDEX.search(make_terms=["MB"], limit=3)
        self.assertEqual((ids(result), result["total"]), (["15897", "28278", "48973"], 12))
        self.assertEqual(result["other_part_names"], [
            {"text": "სამუხრუჭე ხუნდი", "rows": 8, "available": 4},
            {"text": "სტაბ მილისა", "rows": 2, "available": 1},
            {"text": "სამუხრ ხუნდი", "rows": 1, "available": 0},
            {"text": "ფილტრი ჰაერის", "rows": 1, "available": 0}])
        self.assertEqual(result["note"], "Showing 3 of 12 rows; other_part_names counts what "
                                         "they are, add part_terms to narrow the result.")
        result = INDEX.search(make_terms=["MB"], vehicle_terms=["W212"], limit=3)
        self.assertEqual(result["other_part_names"],
                         [{"text": "სამუხრუჭე ხუნდი", "rows": 6, "available": 4}])
        self.assertEqual(result["note"], "Showing 3 of 6 rows that match a vehicle term; "
                         "other_part_names counts what they are, add part_terms to narrow the "
                         "result.")
        # Everything is shown: the rows themselves say what the parts are.
        self.assertEqual(INDEX.search(make_terms=["MB"])["other_part_names"], [])
        self.assertEqual(INDEX.search(make_terms=["MB"], vehicle_terms=["W212"],
                                      limit=6)["other_part_names"], [])

    def test_similar_spellings_are_reported_and_never_merged(self):
        result = INDEX.search(make_terms=["HONDA"], vehicle_terms=["ACCORD"],
                              part_terms=["მორგვი"])
        self.assertEqual(result["similar_spellings"],
                         [{"term": "ACCORD", "catalogue_token": "ACCROD", "rows": 1}])
        self.assertEqual((ids(result), result["matched_vehicle"]), (["50030", "50031"], 1))
        self.assertEqual(found(vehicle_terms=["ACCORD"]), {"50030", "13581"})
        self.assertEqual(found(vehicle_terms=["ACCROD"]), {"50031"})
        # Insertions, deletions and substitutions too; `term` is the term as it was given.
        for term, token in (("CLASE", "CLASS"), ("ELANTRAS", "ELANTRA"), ("KAMRY", "CAMRY"),
                            ("honda acord", "ACCORD")):
            with self.subTest(term=term):
                self.assertIn({"term": term, "catalogue_token": token,
                               "rows": len(found(vehicle_terms=[token]))},
                              INDEX.search(vehicle_terms=[term])["similar_spellings"])
        # Short words, numbers and the pieces of a word typed as one are left alone.
        for term in ("CAMR", "RAV4", "W2121", "E-CLASS", "CAMR*", "ხუნდა"):
            with self.subTest(term=term):
                self.assertEqual(INDEX.search(vehicle_terms=[term])["similar_spellings"], [])

    def test_rows_under_a_near_spelling_are_returned_apart(self):
        disc = "სამუხრუჭე დისკი უკ"
        rows = [row(str(100 + number), PAD_FRONT, "TOYOTA/LEXUS COROLLA 2008>", "SATO",
                    f"PN{number:04d}", store="1") for number in range(35)]
        rows += [row("1", disc, "TOYOA/LEXUS LC78", "SATO", "RN1976V", store="1"),
                 row("2", PAD_FRONT, "TOYOTA/LEXUS HIGLANDER 2008>", "SATO", "PN1530",
                     tuz11="2"),
                 row("3", PAD_FRONT, "TOYOTA/LEXUS HIGLANDER 2014>", "SB", "BP21530"),
                 row("4", PAD_FRONT, "HONDA ACCROD 2013>", "SATO", "PN8000", store="1"),
                 row("5", disc, "TOYOA/LEXUS LC79 2020>", "SATO", "RN1977V", store="1")]
        index = CatalogIndex.from_rows(rows)

        def near(**terms) -> list[str]:
            return [item[0] for item in index.search(**terms)["similar_spelling_rows"]]

        # A make the catalogue misspelt: its rows that match the other groups.
        result = index.search(make_terms=["TOYOTA"], vehicle_terms=["LC78", "78"],
                              part_terms=["დისკ"])
        self.assertEqual((result["total"], ids(result), near(
            make_terms=["TOYOTA"], vehicle_terms=["LC78", "78"], part_terms=["დისკ"])),
            (0, [], ["1"]))
        self.assertEqual(near(make_terms=["TOYOTA"], part_terms=["დისკ"]), ["1", "5"])
        # A model the catalogue misspelt, under the right make: 37 rows match make and part,
        # the result holds 30 and none of them names the model as typed.
        result = index.search(make_terms=["TOYOTA"], vehicle_terms=["HIGHLANDER"],
                              part_terms=["ხუნდ"], limit=30)
        self.assertEqual((result["total"], result["matched_vehicle"], ids(result)),
                         (37, 0, []))
        self.assertEqual([item[0] for item in result["similar_spelling_rows"]], ["2", "3"])
        self.assertEqual(near(make_terms=["TOYOTA"], vehicle_terms=["HIGHLANDER"],
                              part_terms=["ხუნდ"], limit=30, in_stock_only=True), ["2"])
        self.assertEqual(near(make_terms=["TOYOTA"], vehicle_terms=["HIGHLANDER"],
                              part_terms=["ხუნდ"], exclude_terms=["2014"], limit=30), ["2"])
        # Vehicle terms alone are the filter, and the near spelling is reported with them.
        self.assertEqual(near(vehicle_terms=["HIGHLANDER"]), ["2", "3"])
        # Another make's misspelt model stays out, and so does everything unasked for.
        self.assertEqual(near(make_terms=["TOYOTA"], vehicle_terms=["ACCORD"],
                              part_terms=["ხუნდ"], limit=30), [])
        self.assertEqual(near(make_terms=["HONDA"], vehicle_terms=["ACCORD"],
                              part_terms=["დისკ"]), [])
        # A row the result already holds is not listed twice, and a code one character
        # away is another code, not a spelling.
        result = index.search(make_terms=["HONDA"], vehicle_terms=["ACCORD"],
                              part_terms=["ხუნდ"])
        self.assertEqual((ids(result), result["similar_spelling_rows"]), (["4"], []))
        self.assertEqual(near(vehicle_terms=["LC77"]), [])

    def test_the_guide_counts_the_letter_that_the_numbers_of_a_side_mark_end_in(self):
        """Makers end the numbers of a pair's two sides in different letters: counted, the
        guide lets a reader check which mark is which side."""
        link = "სტაბ კრონშტეინი"
        rows = [row(str(number), f"{link} წნ/მჯ", "TOYOTA CAMRY", "JIKIU", f"LS21{number:03d}R",
                    store="1") for number in range(12)]
        rows += [row(str(100 + number), f"{link} წნ/მც", "TOYOTA CAMRY", "JIKIU",
                     f"LS21{number:03d}L", store="1") for number in range(12)]
        # Numbers that end in a digit, and a brand's name written after the number.
        rows += [row(str(200 + number), PAD_FRONT, "TOYOTA CAMRY", "SATO", f"PN{number:04d}",
                     store="1") for number in range(300)]
        rows += [row(str(600 + number), "მილისა ბერკეტის ქვ", "MB W204", "FEBI",
                     f"26{number:03d} FEBI") for number in range(12)]
        guide = CatalogIndex.from_rows(rows).guide()
        short = guide[guide.index("SHORT WORDS IN NAMES"):guide.index("BRANDS, the 60")]
        self.assertIn(f"მჯ 12 ({link} წნ/მჯ; numbers end in R)", short)
        self.assertIn(f"მც 12 ({link} წნ/მც; numbers end in L)", short)
        self.assertIn("წნ 320 (სამუხრუჭე ხუნდი წნ)", short)  # No letter stands out.
        self.assertIn("ქვ 12 (მილისა ბერკეტის ქვ)", short)  # FEBI is the brand, not the number.
        self.assertEqual((assistant_catalog._ending("LS21340R"), assistant_catalog._ending(
            "26096 FEBI", "FEBI"), assistant_catalog._ending("PN1521"),
            assistant_catalog._ending("FEBI"), assistant_catalog._ending("H-R")),
            ("R", "", "", "", ""))

    def test_the_guide_lists_the_brands_series_that_are_filed_under_a_part_name(self):
        """Makers give each kind of part its own number series: counted per name, they say
        what a name of the shop's own is."""
        link, bush = "სტაბ კრონშტეინი წნ", "სტაბ მილისა წნ"
        rows = [row(str(number), link, "TOYOTA CAMRY", "JIKIU", f"LS21{number:03d}")
                for number in range(8)]
        rows += [row(str(20 + number), link, "TOYOTA CAMRY", "Ctr", f"CL0{number:03d}(CLT{number})")
                 for number in range(6)]
        rows += [row(str(40 + number), bush, "TOYOTA CAMRY", "JIKIU", f"BL21{number:03d}")
                 for number in range(7)]
        # A series spread over many names says nothing about one of them, a series of a
        # few rows is no series, and a number that begins with digits has none.
        rows += [row(str(60 + number), name, "TOYOTA CAMRY", "TEKNOROT", f"TY-{number:03d}")
                 for number, name in enumerate([link] * 5 + [bush] * 5 + [PAD_FRONT] * 5)]
        rows += [row(str(80 + number), bush, "TOYOTA CAMRY", "FEBEST", f"TSB-{number:03d}")
                 for number in range(4)]
        rows += [row(str(90 + number), bush, "TOYOTA CAMRY", "TOYOTA", f"48815-{number:05d}")
                 for number in range(9)]
        guide = CatalogIndex.from_rows(rows).guide()
        names = guide[guide.index("PART NAMES BY OPENING WORDS"):
                      guide.index("SHORT WORDS IN NAMES")]
        self.assertIn(f"სტაბ კრონშტეინი 19/0 e.g. {link}; JIKIU LS, CTR CL\n", names)
        self.assertIn(f"სტაბ მილისა 25/0 e.g. {bush}; JIKIU BL\n", names)
        self.assertIn(f"სამუხრუჭე ხუნდი 5/0 e.g. {PAD_FRONT}\n", names)
        self.assertNotIn("TEKNOROT", names)
        series = assistant_catalog._series
        self.assertEqual([series(Row("1", "", "", brand, code, "", (), None, None, None, ""))
                          for brand, code in (("Jikiu", "LS-21340L"), ("CTR", "CL0311(CLKH65)"),
                                              ("TOYOTA", "48820-06050"), ("", "LS21340"),
                                              ("SATO", "PN"), ("js  asakashi", "A1539"))],
                         ["JIKIU LS", "CTR CL", "", "", "", ""])

    def test_unmatched_terms_are_the_ones_no_row_has(self):
        result = INDEX.search(make_terms=["MB", "MERCEDES"], vehicle_terms=["W212", "C207"],
                              part_terms=["ხუნდ", "კალოტკა"], exclude_terms=["ბარაბნის"])
        self.assertEqual(result["unmatched_terms"], ["MERCEDES", "C207", "კალოტკა", "ბარაბნის"])
        self.assertEqual(INDEX.search(make_terms=["MB"])["unmatched_terms"], [])

    def test_the_result_is_plain_json_with_fixed_keys(self):
        result = INDEX.search(make_terms=["TOYOTA"], vehicle_terms=["CAMRY"],
                              part_terms=["ხუნდ"])
        self.assertEqual(list(result), [
            "total", "available", "matched_vehicle", "columns", "rows", "other_vehicle_texts",
            "other_part_names", "similar_spellings", "similar_spelling_rows",
            "unmatched_terms", "why", "note"])
        self.assertEqual(result["columns"], ["id", "name", "vehicle", "brand", "code",
                                             "cross_codes", "price", "store", "warehouse"])
        self.assertEqual(result["columns"], COLUMNS)
        self.assertEqual(result["rows"][0], [
            "6675", PAD_FRONT, "TOYOTA/LEXUS CAMRY 2006->", "SATO", "PN1521",
            "D1222-8331/GDB3429/KD2765/PN1521", 85, 6, 5])
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)
        result["columns"].append("changed")  # a caller's copy, not the module's list
        self.assertEqual(len(COLUMNS), 9)

    def test_html_entities_are_unescaped_everywhere(self):
        self.assertEqual(INDEX.row("14458").vehicle, "TOYOTA/LEXUS CAMRY 2017>")
        self.assertEqual(INDEX.search(vehicle_terms=["2017"])["rows"][0][2],
                         "TOYOTA/LEXUS CAMRY 2017>")
        self.assertEqual(INDEX.row("50041").cross_codes, ("LS&K100",))
        self.assertEqual(matched(INDEX.by_codes(["LSK100"])["LSK100"]),
                         [("50041", "LS&K100", "cross")])
        self.assertNotIn("&gt;", INDEX.guide())

    def test_rows_keep_the_source_values(self):
        self.assertEqual(INDEX.row("28278"), Row(
            id="28278", name=PAD_FRONT, vehicle="MB W204,W211,W212", brand="BREMBO",
            code="P50069", shop_code="2040028278",
            cross_codes=("GDB1737", "PN0395", "D1342-8453", "PN0395W", "13.0460-2734.2"),
            price=180.0, store=1.0, warehouse=1.0, state=stock.IN_STORE))
        unknown = INDEX.row("40003")
        self.assertEqual((unknown.store, unknown.warehouse, unknown.state),
                         (None, None, stock.UNKNOWN))
        self.assertEqual(INDEX.row("50040").price, 45.5)
        self.assertEqual(INDEX.search(vehicle_terms=["L200"], part_terms=["ამორტ"])["rows"][0][6],
                         45.5)
        self.assertEqual(INDEX.row(28278).id, "28278")
        for missing in ("99999", "", "2827", None):
            self.assertIsNone(INDEX.row(missing))
        self.assertEqual(len(INDEX), len(ROWS))

    def test_stock_thresholds_come_from_the_rules(self):
        index = CatalogIndex.from_rows(ROWS, Rules(min_store=1.0, min_tuz11=1.0))
        result = index.search(make_terms=["MB"], part_terms=["ხუნდ"])
        self.assertEqual((ids(result)[:2], result["available"]), (["15897", "40003"], 1))
        self.assertEqual(index.row("28278").state, stock.UNAVAILABLE)

    def test_long_cross_lists_are_cut_to_eight(self):
        numbers = [f"NUM{number:04d}" for number in range(11)]
        index = CatalogIndex.from_rows([row("1", "ფილტრი", "UNIVERSAL", "X", "OWN-1",
                                            "/".join(numbers))])
        self.assertEqual(index.search(part_terms=["ფილტრი"])["rows"][0][5],
                         "/".join(numbers[:8]) + " (+3 more)")
        self.assertEqual(index.row("1").cross_codes, tuple(numbers))
        self.assertEqual(matched(index.by_codes(["NUM0010"])["NUM0010"]),
                         [("1", "NUM0010", "cross")])


class SearchErrorTests(unittest.TestCase):
    def refused(self, **terms) -> SearchError:
        with self.assertRaises(SearchError) as caught:
            INDEX.search(**terms)
        return caught.exception

    def test_one_letter_is_refused_with_the_reason_and_the_fix(self):
        error = self.refused(vehicle_terms=["W212", "E"], part_terms=["ხუნდ"])
        self.assertEqual((error.message, error.fix), ONE_LETTER)
        self.assertEqual(str(error), " ".join(ONE_LETTER))
        self.assertIsInstance(error, ValueError)
        error = self.refused(make_terms=["წ"])
        self.assertEqual(error.message, "`make_terms` contains 'წ' (1 letter), which matches "
                                        "almost every row.")
        self.assertIn("guide", error.fix)
        for group in ("part_terms", "exclude_terms"):
            error = self.refused(make_terms=["MB"], **{group: [" e "]})
            self.assertEqual(error.message, f"`{group}` contains 'e' (1 letter), which "
                                            "matches almost every row.")
            self.assertIn("longer word", error.fix)

    def test_a_call_without_terms_is_refused(self):
        for terms in ({}, {"exclude_terms": ["ხელის"]}, {"make_terms": [], "part_terms": ()},
                      {"vehicle_terms": ["", "   "]}, {"make_terms": None}):
            with self.subTest(terms=terms):
                error = self.refused(**terms)
                self.assertEqual(error.message, "No search terms were given.")
                self.assertEqual(error.fix, "Put at least one word into make_terms, "
                                            "vehicle_terms or part_terms.")

    def test_terms_that_cannot_be_searched_are_refused(self):
        cases = (
            ({"part_terms": ["->"]}, "`part_terms` contains '->', which has no letters or "
                                     "digits."),
            ({"vehicle_terms": ["5*"]}, "`vehicle_terms` contains '5*', where `*` follows 1 "
                                        "character and matches almost every row."),
            ({"make_terms": [5]}, "`make_terms` must be a list of strings."),
            ({"make_terms": ["MB", None]}, "`make_terms` must be a list of strings."),
            ({"vehicle_terms": [f"W{number}" for number in range(41)]},
             "`vehicle_terms` has 41 terms; at most 40 are read."),
            ({"part_terms": ["ა" * 81]}, "`part_terms` contains a term of 81 characters "
                                         f"({'ა' * 30!r}...); at most 80 are read."),
            ({"part_terms": ["ერთი ორი სამი ოთხი ხუთი ექვსი შვიდი რვა ცხრა"]},
             "`part_terms` contains 'ერთი ორი სამი ოთხი ხუთი ექვსი შვიდი რვა ცხრა' (9 words); "
             "every word of a term must be in the row, and at most 8 are read."),
        )
        for terms, message in cases:
            with self.subTest(terms=terms):
                error = self.refused(**terms)
                self.assertEqual(error.message, message)
                self.assertTrue(error.fix.endswith("."))

    def test_what_is_not_refused(self):
        # A lone digit is a model for some makes; one letter is fine beside another word.
        self.assertEqual(INDEX.search(make_terms=["MAZDA"], vehicle_terms=["3"])["total"], 1)
        self.assertEqual(INDEX.search(vehicle_terms=["E CLASS"])["total"], 2)
        self.assertEqual(INDEX.search(part_terms=["წნ"])["total"], 20)
        self.assertEqual(INDEX.search(vehicle_terms=["W2*"])["total"], 10)
        # Repeated and empty terms are dropped quietly.
        result = INDEX.search(make_terms=["MB", "mb", " MB ", ""], part_terms=["ხუნდ"])
        self.assertEqual((result["total"], result["unmatched_terms"]), (9, []))


class NumberTests(unittest.TestCase):
    def test_numbers_are_found_in_slash_separated_cross_codes(self):
        result = INDEX.by_codes(["GDB1737", "gdb-1737", "D13428453", "XYZ9999"])
        self.assertEqual(list(result), ["GDB1737", "gdb-1737", "D13428453", "XYZ9999"])
        hits = [("15897", "GDB1737", "cross"), ("28278", "GDB1737", "cross"),
                ("7425", "GDB1737", "cross")]
        self.assertEqual(matched(result["GDB1737"]), hits)
        self.assertEqual(matched(result["gdb-1737"]), hits)
        self.assertEqual([hit[1] for hit in matched(result["D13428453"])], ["D1342-8453"] * 3)
        self.assertEqual(result["XYZ9999"], [])
        self.assertEqual(result["GDB1737"][0]["row"],
                         INDEX.search(vehicle_terms=["W212"])["rows"][0])

    def test_the_rows_own_code_comes_before_cross_codes_then_stock(self):
        result = INDEX.by_codes(["PN1521", "GDB3429", "13.0460-7259.2"])
        self.assertEqual(matched(result["PN1521"]), [
            ("6675", "PN1521", "code"), ("3226", "PN1521", "cross"),
            ("10354", "PN1521", "cross")])
        # The shop's own stock number is found too, before the rows that only cross to it.
        self.assertEqual(matched(result["GDB3429"]), [
            ("50051", "GDB3429", "shop_code"), ("6675", "GDB3429", "cross"),
            ("3226", "GDB3429", "cross"), ("10354", "GDB3429", "cross")])
        self.assertEqual(matched(result["13.0460-7259.2"]),
                         [("30790", "13.0460-7259.2", "code")])
        self.assertEqual(matched(INDEX.by_codes(["2040028278"])["2040028278"]),
                         [("28278", "2040028278", "shop_code")])
        self.assertEqual(matched(INDEX.by_codes(["PN1521"], limit_per_number=1)["PN1521"]),
                         [("6675", "PN1521", "code")])
        self.assertEqual(len(INDEX.by_codes(["PN1521"], limit_per_number=0)["PN1521"]), 1)

    def test_a_number_with_a_slash_suffix_is_one_number(self):
        result = INDEX.by_codes(["AP104/2", "ap 104/3", "AP104", "AP139/4", "AP 051/2", "AP139"])
        self.assertEqual(matched(result["AP104/2"]), [("50025", "AP104/2", "code")])
        self.assertEqual(matched(result["ap 104/3"]), [("50026", "AP104/3", "code")])
        # The stem finds both, and `matched` shows that each row holds a longer number.
        self.assertEqual(matched(result["AP104"]), [("50026", "AP104/3", "code"),
                                                    ("50025", "AP104/2", "code")])
        self.assertEqual(matched(result["AP139/4"]), [("50024", "AP139/4", "code")])
        self.assertEqual(matched(result["AP 051/2"]), [("50027", "AP 051/2", "code")])
        self.assertEqual(matched(result["AP139"]), [("50024", "AP139/4", "code")])
        # Two full numbers in one cell: each is a number, and so is the cell as written.
        result = INDEX.by_codes(["343313", "343272", "343313/343272"])
        self.assertEqual(matched(result["343313"]), [("50040", "343313", "code")])
        self.assertEqual(matched(result["343272"]), [("50040", "343272", "code")])
        self.assertEqual(matched(result["343313/343272"]),
                         [("50040", "343313/343272", "code")])

    def test_numbers_beside_brackets_brand_names_and_suffixes_are_found(self):
        result = INDEX.by_codes(["CL0275", "CLKH30", "CL0275(CLKH30)", "26096", "26096 FEBI",
                                 "44994", "44994 01", "C30005", "C 30 005"])
        self.assertEqual(matched(result["CL0275"]), [("4658", "CL0275", "code")])
        self.assertEqual(matched(result["CLKH30"]), [
            ("4658", "CLKH30", "code"), ("4386", "CLKH30", "cross"),
            ("4659", "CLKH30", "cross")])
        self.assertEqual(matched(result["CL0275(CLKH30)"]), [("4658", "CL0275(CLKH30)", "code")])
        self.assertEqual(matched(result["26096"]), [("48972", "26096", "code"),
                                                    ("48973", "26096", "cross")])
        self.assertEqual(matched(result["26096 FEBI"]), [("48972", "26096 FEBI", "code")])
        self.assertEqual(matched(result["44994"]), [("50041", "44994", "code")])
        self.assertEqual(matched(result["44994 01"]), [("50041", "44994 01", "code")])
        self.assertEqual(matched(result["C30005"]), [("50022", "C 30 005", "code")])
        self.assertEqual(result["C 30 005"], result["C30005"])

    def test_short_numbers_and_plain_words_are_ignored(self):
        result = INDEX.by_codes(["H50", "H52", "ZZ-1", "", "--", "FEBI", "BREMBO", "2743", 2743])
        self.assertEqual([result[number]
                          for number in ("H50", "H52", "ZZ-1", "", "--", "FEBI", "BREMBO")],
                         [[]] * 7)
        # A cross cell that holds a description gives the row no number to share.
        index = CatalogIndex.from_rows([
            row("1", "ზეთი", "UNIVERSAL", "A", "OIL-1001", "API SP, ACEA/VALEO"),
            row("2", "ზეთი", "UNIVERSAL", "B", "OIL-1002", "API SP, ACEA/VALEO")])
        self.assertEqual(index.by_codes(["VALEO", "APISP"]), {"VALEO": [], "APISP": []})
        self.assertEqual(index.equivalents("1"), [])
        self.assertEqual(matched(result["2743"]), [("50060", "2743", "code")])
        self.assertEqual(matched(INDEX.by_codes("2743")["2743"]), [("50060", "2743", "code")])
        self.assertEqual(INDEX.by_codes([]), {})

    def test_equivalents_share_numbers_most_shared_first(self):
        same = INDEX.equivalents("28278")
        self.assertEqual([(entry["row"][0], entry["shared_numbers"], entry["same_brand"])
                          for entry in same], [
            ("7425", ["GDB1737", "PN0395", "D1342-8453", "PN0395W", "13.0460-2734.2"], False),
            ("15897", ["GDB1737", "PN0395", "D1342-8453", "PN0395W"], False)])
        self.assertEqual(same[0]["row"], INDEX.by_codes(["37586"])["37586"][0]["row"])
        # Numbers written in brackets count, in the other row's own spelling.
        self.assertEqual([(entry["row"][0], entry["shared_numbers"])
                          for entry in INDEX.equivalents("4658")], [
            ("4386", ["CLKH30", "CLKH23", "LS11090", "LS25001", "LS13001"]),
            ("4659", ["CLKH30", "CLKH23", "LS11090", "LS25001"])])
        self.assertEqual([(entry["row"][0], entry["shared_numbers"])
                          for entry in INDEX.equivalents("48973")], [("48972", ["26096"])])

    def test_equivalents_flag_the_same_brand_and_never_use_the_shop_code(self):
        same = INDEX.equivalents("6675")
        # 50051 carries GDB3429 only as the shop's stock number: not an equivalent.
        self.assertEqual([(entry["row"][0], len(entry["shared_numbers"]), entry["same_brand"])
                          for entry in same],
                         [("3226", 3, False), ("10354", 2, False), ("50050", 1, True)])
        self.assertEqual(INDEX.equivalents("50051"), [])
        # `AP104/2` and `AP104/3` share a stem, not a number.
        self.assertEqual(INDEX.equivalents("50025"), [])
        self.assertEqual(INDEX.equivalents("50026"), [])
        self.assertEqual(INDEX.equivalents("no-such-row"), [])
        self.assertEqual(len(INDEX.equivalents("6675", limit=2)), 2)
        self.assertEqual(len(INDEX.equivalents("6675", limit=0)), 1)

    def test_equivalents_with_equal_shared_numbers_follow_stock_order(self):
        index = CatalogIndex.from_rows([
            row("1", "ფილტრი", "UNIVERSAL", "A", "SRC-0001", "OE-1111"),
            row("2", "ფილტრი", "UNIVERSAL", "B", "OTH-0002", "OE-1111"),
            row("3", "ფილტრი", "UNIVERSAL", "C", "OTH-0003", "OE-1111", tuz11="4"),
            row("4", "ფილტრი", "UNIVERSAL", "D", "OTH-0004", "OE-1111", store="1"),
            row("5", "ფილტრი", "UNIVERSAL", "E", "OTH-0005", "OE-1111/SRC-0001")])
        self.assertEqual([entry["row"][0] for entry in index.equivalents("1")],
                         ["5", "4", "3", "2"])

    def test_a_number_on_more_than_200_rows_is_not_distinctive(self):
        def catalogue(common: int) -> CatalogIndex:
            rows = [row("1", "ზეთი", "UNIVERSAL", "A", "OIL-0001", "COMMON-01/RARE-0001"),
                    row("2", "ზეთი", "UNIVERSAL", "B", "OIL-0002", "RARE-0001")]
            rows += [row(str(100 + number), "ზეთი", "UNIVERSAL", "C", f"OIL-{number:04d}X",
                         "COMMON-01") for number in range(common - 1)]
            return CatalogIndex.from_rows(rows)

        exactly = catalogue(200)  # 200 rows carry the common number: still counted
        self.assertEqual(len(exactly.by_codes(["COMMON-01"], limit_per_number=50)["COMMON-01"]),
                         50)
        same = exactly.equivalents("1", limit=500)
        self.assertEqual(len(same), 100)  # the limit is clamped
        self.assertEqual((same[0]["row"][0], same[0]["shared_numbers"]), ("2", ["RARE-0001"]))
        self.assertEqual(same[1]["shared_numbers"], ["COMMON-01"])
        beyond = catalogue(201)
        self.assertEqual([(entry["row"][0], entry["shared_numbers"])
                          for entry in beyond.equivalents("1")], [("2", ["RARE-0001"])])
        self.assertEqual(beyond.equivalents("100"), [])


def guide_rows() -> list[dict]:
    """Enough rows for every section of the guide to have something to count."""
    rows: list[dict] = []

    def add(count: int, name: str, vehicle: str, brand: str, stocked: int = 0) -> None:
        for number in range(count):
            sid = str(len(rows) + 1)
            rows.append(row(sid, name, vehicle, brand, f"N-{sid}",
                            store="2" if number < stocked else "0"))

    add(130, PAD_FRONT, "TOYOTA/LEXUS CAMRY 2006->", "SATO", stocked=37)
    add(30, PAD_REAR, "TOYOTA/LEXUS COROLLA 2006->", "SATO")
    add(25, PAD_FRONT, "TOYOTA/LEXUS HILUX 2005>", "ABS", stocked=5)
    add(14, PAD_FRONT, "TOYOTA/LEXUS PRADO 120, 150", "ABS")
    add(8, PAD_FRONT, "TOYOTA/LEXUS PRADO", "ABS")
    add(15, AIR, "TOYOTA/LEXUS RAV4 III", "FILTRON")
    add(12, PAD_FRONT, "TOYOTA/LEXUS CARMY 17-", "SATO")
    add(20, AIR, "HONDA FIT >2008", "FILTRON", stocked=4)
    add(11, PUMP, "HONDA CR-V 2007-2011", "GMB")
    add(1234, "ღვედი", "UNIVERSAL", "DAYCO", stocked=151)
    add(14, "სტაბ. კრონშტეინი წნ/მც", "", "CTR")
    return rows


class GuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = guide_rows()
        cls.index = CatalogIndex.from_rows(cls.rows)
        cls.guide = cls.index.guide()

    def test_the_guide_counts_the_catalogue(self):
        lines = self.guide.splitlines()
        self.assertTrue(lines[0].startswith("CATALOGUE GUIDE"))
        # 1513 rows, 197 of them in the shop: two significant digits.
        self.assertEqual(lines[1], "ROWS 1500: 200 in the shop, 0 only in the warehouse, "
                                   "1300 not in stock.")
        self.assertEqual(lines[2], "1500 rows have a vehicle text. 0 have none but write Latin "
                                   "words or numbers in the name. 14 have neither.")
        for heading in ("VEHICLE TEXTS BY FIRST WORD", "YEAR NOTATIONS IN VEHICLE TEXTS",
                        "PART NAMES BY OPENING WORDS", "SHORT WORDS IN NAMES", "BRANDS",
                        "NUMBERS"):
            self.assertTrue(any(line.startswith(heading) for line in lines), heading)

    def test_first_words_list_what_follows_them_and_near_spellings(self):
        self.assertIn("UNIVERSAL 1200/150: || UNIVERSAL", self.guide)
        # The four most frequent later words, then the rarer word one swap away from CAMRY.
        self.assertIn("TOYOTA/LEXUS 230/42: CAMRY 130, COROLLA 30, HILUX 25, PRADO 22 "
                      "~ CARMY 12 (CAMRY 130) || TOYOTA/LEXUS CAMRY 2006-> | "
                      "TOYOTA/LEXUS COROLLA 2006->", self.guide)
        # Those two first words cover nine rows in ten, so the list stops there.
        self.assertNotIn("HONDA ", self.guide.split("YEAR NOTATIONS")[0])
        wider = CatalogIndex.from_rows([source for source in self.rows
                                        if source["product_model"] != "UNIVERSAL"])
        self.assertIn("HONDA 31/4: FIT 20, CR-V 11 || HONDA FIT >2008 | HONDA CR-V 2007-2011",
                      wider.guide())

    def test_year_notations_are_masked_and_counted(self):
        section = self.guide.split("YEAR NOTATIONS")[1].split("PART NAMES")[0]
        self.assertIn("9999-> 160: TOYOTA/LEXUS CAMRY 2006-> ; TOYOTA/LEXUS COROLLA 2006->",
                      section)
        for line in ("9999> 25: TOYOTA/LEXUS HILUX 2005>", ">9999 20: HONDA FIT >2008",
                     "99- 12: TOYOTA/LEXUS CARMY 17-", "9999-9999 11: HONDA CR-V 2007-2011"):
            self.assertIn(line, section)
        # `120, 150` and `RAV4 III` are not years.
        self.assertNotIn("999", section.replace("9999", ""))

    def test_part_names_short_words_brands_and_numbers(self):
        self.assertIn("ღვედი 1200/150 e.g. ღვედი", self.guide)
        self.assertIn(f"სამუხრუჭე ხუნდი 220/42 e.g. {PAD_FRONT}", self.guide)
        self.assertIn(f"ფილტრი ჰაერის 35/4 e.g. {AIR}", self.guide)
        self.assertIn("სტაბ კრონშტეინი 14/0 e.g. სტაბ. კრონშტეინი წნ/მც", self.guide)
        self.assertIn(f"წნ 200 ({PAD_FRONT}); უკ 30 ({PAD_REAR}); მც 14 (", self.guide)
        self.assertIn("DAYCO 1200, SATO 170, ABS 47, FILTRON 35, CTR 14, GMB 11", self.guide)
        self.assertIn("1500 rows carry the maker's own number. 0 rows carry cross numbers",
                      self.guide)

    def test_the_guide_is_the_same_for_the_same_rows_in_any_order(self):
        again = CatalogIndex.from_rows(list(reversed(self.rows)))
        self.assertEqual(again.guide(), self.guide)
        self.assertEqual(again.guide_version, self.index.guide_version)
        self.assertEqual(self.index.guide(), self.guide)
        self.assertEqual(self.index.guide_version,
                         hashlib.sha256(self.guide.encode("utf-8")).hexdigest()[:12])
        self.assertRegex(self.index.guide_version, r"^[0-9a-f]{12}$")

    def test_a_small_change_of_quantity_or_stock_leaves_the_guide_unchanged(self):
        rows = [dict(source) for source in self.rows]
        rows[0]["stock_store"] = "1"  # two in the shop became one
        rows[1]["stock_tuz11"] = "7"  # more arrived at the warehouse
        rows[2]["price_raw"] = "99"
        self.assertEqual(CatalogIndex.from_rows(rows).guide(), self.guide)
        # One of 151 stocked belts sold out and one belt was added: no rounded count moves.
        self.assertEqual((rows[300]["product_name"], rows[300]["stock_store"]), ("ღვედი", "2"))
        rows[300]["stock_store"] = "0"
        changed = CatalogIndex.from_rows([*rows, row("9001", "ღვედი", "UNIVERSAL", "DAYCO",
                                                    "N-9001")])
        self.assertEqual(changed.guide(), self.guide)
        self.assertEqual(changed.guide_version, self.index.guide_version)

    def test_new_vocabulary_changes_the_guide(self):
        rows = [*self.rows, *(row(str(9100 + number), PAD_FRONT, "TOYOTA/LEXUS VENZA 2010>",
                                  "SATO") for number in range(40))]
        changed = CatalogIndex.from_rows(rows)
        self.assertNotEqual(changed.guide_version, self.index.guide_version)
        self.assertIn("VENZA 40", changed.guide())

    def test_an_empty_catalogue_still_has_a_guide(self):
        empty = CatalogIndex.from_rows([])
        self.assertTrue(empty.guide().startswith("CATALOGUE GUIDE"))
        self.assertIn("ROWS 0:", empty.guide())
        self.assertEqual(len(empty), 0)
        self.assertEqual(empty.search(part_terms=["ხუნდ"])["total"], 0)
        self.assertEqual(empty.by_codes(["PN1521"]), {"PN1521": []})


class SharedIndexTests(unittest.TestCase):
    def test_index_for_builds_once_per_snapshot_from_the_stores_catalogue(self):
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS))
        first = index_for(store)
        self.assertIs(index_for(store), first)
        # The raw number cells reach the index through the Catalog's items.
        self.assertEqual(first.by_codes(["GDB1737", "26096", "AP104", "LSK100"]),
                         INDEX.by_codes(["GDB1737", "26096", "AP104", "LSK100"]))
        self.assertEqual(first.equivalents("28278"), INDEX.equivalents("28278"))
        self.assertEqual(first.search(make_terms=["MB"], vehicle_terms=["W212"],
                                      part_terms=["ხუნდ"]),
                         INDEX.search(make_terms=["MB"], vehicle_terms=["W212"],
                                      part_terms=["ხუნდ"]))
        self.assertEqual(first.row("14458"), INDEX.row("14458"))
        self.assertEqual((first.guide(), first.guide_version),
                         (INDEX.guide(), INDEX.guide_version))
        store.replace(Catalog.from_rows(ROWS[:9]))
        second = index_for(store)
        self.assertIsNot(second, first)
        self.assertEqual(len(second), 9)
        self.assertIs(index_for(store), second)
        self.assertEqual(second.search(make_terms=["TOYOTA"])["total"], 0)

    def test_each_store_has_its_own_index_and_the_rules_of_its_catalogue(self):
        strict, plain = CatalogStore(""), CatalogStore("")
        strict.replace(Catalog.from_rows(ROWS, Rules(min_store=1.0, min_tuz11=1.0)))
        plain.replace(Catalog.from_rows(ROWS))
        self.assertIsNot(index_for(strict), index_for(plain))
        self.assertEqual(index_for(strict).row("28278").state, stock.UNAVAILABLE)
        self.assertEqual(index_for(plain).row("28278").state, stock.IN_STORE)

    def test_the_index_of_a_replaced_catalogue_is_released(self):
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS[:4]))
        released = weakref.ref(index_for(store))
        self.assertIsNotNone(released())
        store.replace(Catalog.from_rows(ROWS[:2]))
        gc.collect()
        self.assertIsNone(released())
        self.assertEqual(len(index_for(store)), 2)

    def test_threads_asking_at_once_share_one_build(self):
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS))
        built: list[CatalogIndex] = []
        threads = [threading.Thread(target=lambda: built.append(index_for(store)))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(built), 8)
        self.assertEqual(len({id(index) for index in built}), 1)

    def test_a_store_that_has_not_loaded_yet_gives_an_empty_index(self):
        index = index_for(CatalogStore(""))
        self.assertEqual(len(index), 0)
        self.assertEqual(index.search(make_terms=["MB"])["total"], 0)

    def test_concurrent_readers_get_the_same_answers(self):
        query = {"make_terms": ["MB", "MERCEDES"], "vehicle_terms": ["W212", "E-CLASS"],
                 "part_terms": ["ხუნდ"], "limit": 5}
        expected = (INDEX.search(**query), INDEX.by_codes(["PN1521"]), INDEX.equivalents("6675"))
        failures: list[object] = []

        def read() -> None:
            try:
                for _ in range(40):
                    got = (INDEX.search(**query), INDEX.by_codes(["PN1521"]),
                           INDEX.equivalents("6675"))
                    if got != expected:
                        failures.append(got)
            except Exception as error:  # reported by the assertion below
                failures.append(error)

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(failures, [])


class NoKnowledgeTests(unittest.TestCase):
    def test_the_module_uses_none_of_the_hand_written_tables(self):
        source = inspect.getsource(assistant_catalog)
        for name in ("TOKEN_MAKES", "MAKES", "MODEL_ALIASES", "VAG_", "vehicle_keys",
                     "model_key", "automotive.categories", "automotive.name_match",
                     "PATTERNS", "year_fit", "query_patterns", "category_for_query"):
            self.assertNotIn(name, source)
            self.assertFalse(hasattr(assistant_catalog, name), name)
        # Only the price reader comes from the module that holds the make tables.
        self.assertEqual(re.findall(r"from automotive\.site_catalog import (.+)", source),
                         ["parse_price"])


if __name__ == "__main__":
    unittest.main()


class ShortenedWordTests(unittest.TestCase):
    """The shop shortens Georgian words with a dot; a full word still finds them."""

    INDEX = CatalogIndex.from_rows([
        row("90001", "დიზელის გამათ.სპირალი", "TOYOTA HILUX 3.0", "HKT", "PT156", store="2"),
        row("90002", "ამორტ. ბალიში წნ", "MAZDA CX-5", "FEBI", "183569", store="1"),
        row("90003", "გამ ფილტრი", "TOYOTA HILUX", "X", "1", store="1"),
    ])

    def ids(self, *parts: str) -> set[str]:
        found = self.INDEX.search(part_terms=list(parts))
        columns = found["columns"]
        return {dict(zip(columns, item))["id"] for item in found["rows"]}

    def test_a_full_word_finds_the_shortened_one(self) -> None:
        self.assertEqual(self.ids("გამათბობელი"), {"90001"})
        self.assertEqual(self.ids("ამორტიზატორის ბალიში"), {"90002"})

    def test_a_short_row_word_is_not_a_stem_for_longer_words(self) -> None:
        # Three letters are too few to stand for a longer word.
        self.assertNotIn("90003", self.ids("გამათბობელი"))
