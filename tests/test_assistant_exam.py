"""The exam's mechanics without any model: truth from data, grading, intervals, building
and the conversation loop, with fake writers and a fake chat store."""

from __future__ import annotations

import random
import unittest

from automotive import assistant_exam as exam
from automotive.assistant_catalog import CatalogIndex
from automotive.codes import normalize_code
from tests.test_assistant_catalog import row

ROWS = [
    row("1", "სამუხრუჭე ხუნდი წნ", "MB W212", "SATO", "PN0395W", "GDB1737/PN0395", store="2"),
    # The same pad (two shared numbers), filed under another car: the E63 twin.
    row("2", "სამუხრუჭე ხუნდი წნ", "MB E63 AMG", "BREMBO", "P50069", "GDB1737/PN0395",
        store="1"),
    # Out of stock under the customer's car, its in-stock twin under another car.
    row("3", "სამუხრუჭე ხუნდი წნ", "TOYOTA CAMRY 2006->", "SATO", "PN1521", "04465-33450/D1222"),
    row("4", "სამუხრუჭე ხუნდი წნ", "LEXUS ES", "SB", "BP21521", "04465-33450/D1222",
        store="3"),
    row("5", "ჰაერის ფილტრი", "?????? ????", "ASHIKA", "20-00-0001", store="1"),
    row("6", "ზეთის ფილტრი", "HONDA CIVIC 2012>", "JS", "OE0056", "15400-RTA-003", store="4"),
]
INDEX = CatalogIndex.from_rows(ROWS)


def card(identifier: str, confidence: str = "listed", in_stock: bool = True) -> dict:
    return {"id": identifier, "confidence": confidence, "in_stock": in_stock,
            "brand": "X", "number": "N", "name": "", "vehicle": "", "reason": ""}


class TruthTests(unittest.TestCase):
    def test_twins_are_rows_sharing_two_numbers(self) -> None:
        self.assertEqual(exam.twins(INDEX, INDEX.row("1")), {"1", "2"})
        self.assertEqual(exam.twins(INDEX, INDEX.row("3")), {"3", "4"})

    def test_carriers_find_every_row_with_the_number(self) -> None:
        self.assertEqual(exam.carriers(INDEX, ["gdb 1737"]), {"1", "2"})
        self.assertEqual(exam.carriers(INDEX, ["NOPE12345"]), set())

    def test_unreadable_vehicle_texts_are_not_drawn(self) -> None:
        pools = exam.Pools(INDEX)
        self.assertNotIn("5", {item.id for item in pools.stocked_rows()})

    def test_twin_pairs_pair_an_out_of_stock_row_with_its_stocked_twin(self) -> None:
        pairs = exam.Pools(INDEX).twin_pairs(random.Random(1), 5)
        self.assertIn(("3", "4"), {(a.id, b.id) for a, b in pairs})

    def test_number_forms_keep_every_character(self) -> None:
        for seed in range(40):
            typed = exam.number_form("15400-RTA-003", "JS", random.Random(seed))
            self.assertIn(normalize_code("15400-RTA-003"), normalize_code(typed))


class GradingTests(unittest.TestCase):
    def turn(self, *cards: dict, status: str = "COMPLETE") -> dict:
        return {"cards": list(cards), "status": status}

    def test_a_truth_row_in_stock_settles_the_outcome(self) -> None:
        case = {"kind": "text", "truth": {"rows": ["1", "2"], "nothing": False}}
        result = exam.grade(case, [self.turn(card("9", "check")), self.turn(card("2"))])
        self.assertTrue(result["outcome_ok"])
        self.assertEqual(result["found_at_turn"], 2)
        self.assertEqual([row["id"] for row in result["undecided"]], ["9"])

    def test_an_out_of_stock_truth_row_is_not_enough(self) -> None:
        case = {"kind": "text", "truth": {"rows": ["3"], "nothing": False}}
        self.assertFalse(exam.grade(case, [self.turn(card("3", in_stock=False))])["outcome_ok"])

    def test_a_negative_case_fails_only_on_a_row_shown_as_fitting(self) -> None:
        case = {"kind": "negative", "truth": {"rows": [], "nothing": True}}
        self.assertTrue(exam.grade(case, [self.turn()])["outcome_ok"])
        self.assertTrue(exam.grade(case, [self.turn(card("1", "check"))])["outcome_ok"])
        self.assertFalse(exam.grade(case, [self.turn(card("1", "listed"))])["outcome_ok"])

    def test_shop_facts_are_checked_against_the_stored_value(self) -> None:
        case = {"fact": {"topic": "phone", "value": "+995 555 12 34 56"}}
        self.assertTrue(exam.check_fact(case, [{"answer": "დაგვირეკეთ: 555 123 456"}]))
        self.assertFalse(exam.check_fact(case, [{"answer": "Call the shop."}]))

    def test_hours_and_address_count_in_any_order_and_either_language(self) -> None:
        hours = {"fact": {"topic": "hours", "value": [
            {"day": "Monday – Saturday", "time": "09:30 – 19:00"}]}}
        self.assertTrue(exam.check_fact(hours, [{"answer": "Open until 19:00, from 9.30."}]))
        self.assertFalse(exam.check_fact(hours, [{"answer": "Open until 18:00, from 09:30."}]))
        address = {"fact": {"topic": "address", "value": "17 Kavtaradze St, Tbilisi",
                            "value_ka": "ქ. თბილისი, ქავთარაძის ქ. 17"}}
        georgian = [{"answer": "მაღაზია თბილისში, ქავთარაძის ქ. N 17-ში მდებარეობს."}]
        self.assertTrue(exam.check_fact(address, georgian))
        self.assertTrue(exam.check_fact(address, [{"answer": "We are at Kavtaradze St 17."}]))
        self.assertFalse(exam.check_fact(address, [{"answer": "We are on Rustaveli Ave."}]))
        english_only = {"fact": {"topic": "address", "value": "17 Kavtaradze St, Tbilisi"}}
        # Any script and grammatical ending: Georgian, Russian or English replies.
        self.assertTrue(exam.check_fact(english_only, georgian))
        russian = [{"answer": "Мы находимся по адресу: Тбилиси, ул. Кавтарадзе, 17."}]
        self.assertTrue(exam.check_fact(english_only, russian))
        self.assertFalse(exam.check_fact(english_only, [{"answer": "ул. Руставели, 5"}]))

    def test_the_decoder_must_return_the_written_car(self) -> None:
        request = {"car": "Toyota Camry XV50", "year": 2015}
        self.assertTrue(exam._decodes_to({"make": "TOYOTA", "model": "Camry",
                                          "model_year": 2015}, request))
        self.assertFalse(exam._decodes_to({"make": "TOYOTA", "model": "Corolla",
                                           "model_year": 2015}, request))
        self.assertFalse(exam._decodes_to({"make": "TOYOTA", "model": "Camry",
                                           "model_year": 2012}, request))


class ReportTests(unittest.TestCase):
    def test_wilson_interval(self) -> None:
        low, high = exam.wilson(98, 100)
        self.assertAlmostEqual(low, 0.9300, places=3)
        self.assertAlmostEqual(high, 0.9945, places=3)
        self.assertEqual(exam.wilson(0, 0), (0.0, 0.0))

    def test_make_only_requests_stay_out_of_the_headline(self) -> None:
        results = [
            {"id": "a", "kind": "text", "language": "ka", "names": "model", "outcome_ok": True,
             "shown": 3, "wrong_rows": [{"id": "9"}], "unsure_rows": []},
            {"id": "b", "kind": "text", "language": "en", "names": "make", "outcome_ok": False,
             "shown": 0, "wrong_rows": [], "unsure_rows": []},
            {"id": "c", "kind": "number", "language": "ru", "names": "", "outcome_ok": False,
             "shown": 1, "wrong_rows": [], "unsure_rows": [{"id": "8"}]},
        ]
        summary = exam.summarize(results)
        self.assertEqual((summary["all"]["passed"], summary["all"]["cases"]), (1, 2))
        self.assertEqual(summary["all"]["wrong_rows"], 1)
        self.assertEqual(summary["all"]["wrong_rate"], 0.25)
        self.assertEqual(summary["make_only"]["cases"], 1)

    def test_a_vin_case_needs_a_row_whose_text_ties_it_to_years(self) -> None:
        readings = {"MB W212": "Mercedes-Benz E-Class W212 (2009-2016)",
                    "TOYOTA CAMRY": "Toyota Camry, generation not stated"}
        self.assertTrue(exam.settles_a_year(readings, " MB W212 "))
        self.assertFalse(exam.settles_a_year(readings, "TOYOTA CAMRY"))
        self.assertFalse(exam.settles_a_year(readings, "UNREAD TEXT"))
        self.assertTrue(exam.settles_a_year(None, "TOYOTA CAMRY"))  # no readings: as before

        class Builder(exam.Builder):
            def _text(self, item: dict, rng: random.Random) -> dict:
                return {"kind": "text"}

        builder = Builder(exam.Pools(INDEX), models=None, decode=lambda vin: {},
                          vehicle_readings={})
        item = {"row": INDEX.row("3"), "language": "en", "typos": False}
        self.assertEqual(builder._vin(item, random.Random(1)), {"kind": "text"})

    def test_crawled_truth_holds_every_number_listed_for_the_car(self) -> None:
        class Crawled:
            def listed(self, label: str, part: str) -> list[str]:
                return ["PN1521", "GDB1737"] if (label, part) == ("CAR", "Pads") else []

        vehicle = {"label": "CAR", "part": "Pads"}
        self.assertEqual(exam.listed_numbers(Crawled(), vehicle, "PN1521"),
                         ["GDB1737", "PN1521"])
        self.assertEqual(exam.listed_numbers(None, vehicle, "PN1521"), ["PN1521"])
        self.assertEqual(exam.crawled_truth(INDEX, ["PN1521", "GDB1737"]),
                         {"1", "2", "3", "4"})

    def test_regrade_widens_a_crawled_truth_and_keeps_the_stored_verdicts(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        class Crawled:
            def listed(self, label: str, part: str) -> list[str]:
                return ["PN1521", "GDB1737"]

        case = {"id": "alt-1", "kind": "alternative", "variant": "crawled",
                "language": "en", "truth": {"rows": ["3", "4"], "nothing": False},
                "crawled": {"number": "PN1521", "vehicle": {"label": "CAR", "part": "Pads"}}}
        turns = [{"cards": [card("2"), card("6", "check")], "status": "COMPLETE",
                  "estimated_seconds": 6.8, "tools": ["car_numbers"], "requests": 2}]
        judged = [{**card("2"), "verdict": "wrong", "note": "only the first number"},
                  {**card("6", "check"), "verdict": "wrong", "note": "an oil filter"}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "traces").mkdir()
            (root / "traces" / "alt-1.json").write_text(json.dumps(
                {"case": case, "turns": turns, "judged": judged}), encoding="utf-8")
            (root / "results.jsonl").write_text(json.dumps(
                {"id": "alt-1", "outcome_ok": False, "seconds": 30.0}) + "\n",
                encoding="utf-8")
            self.assertEqual(exam.regrade(root, INDEX, Crawled()), 1)
            record = exam.load_results(root)[0]
            self.assertTrue(record["outcome_ok"])
            self.assertEqual([row["id"] for row in record["wrong_rows"]], ["6"])
            self.assertEqual(record["seconds"], 30.0)
            # A second regrade finds nothing left to widen.
            self.assertEqual(exam.regrade(root, INDEX, Crawled()), 0)

    def test_a_grader_that_fails_is_logged_and_does_not_stop_the_exam(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        case = {"id": "t-1", "kind": "text", "language": "en",
                "truth": {"rows": ["1"], "nothing": False}}
        turns = [{"cards": [card("9", "check")], "status": "COMPLETE",
                  "estimated_seconds": 6.8, "tools": [], "requests": 2}]
        with tempfile.TemporaryDirectory() as folder:
            run = exam.Exam(Path(folder), store=None, models=None)
            with patch.object(exam, "converse", return_value=turns), \
                    patch.object(exam, "judge", side_effect=TimeoutError("no answer")):
                run.one(case)
            errors = (Path(folder) / "errors.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(errors[0])["error"], "grading_TimeoutError")
            self.assertEqual(run.done_ids(), set())  # run again on resume

    def test_a_stocked_row_the_judge_says_fits_counts_as_right(self) -> None:
        results = [
            {"id": "a", "kind": "text", "language": "en", "names": "model",
             "outcome_ok": False, "outcome_or_judged_fit": True, "shown": 2,
             "wrong_rows": [], "unsure_rows": []},
            {"id": "b", "kind": "text", "language": "en", "names": "model",
             "outcome_ok": True, "shown": 1, "wrong_rows": [], "unsure_rows": []},
            {"id": "c", "kind": "text", "language": "en", "names": "model",
             "outcome_ok": False, "outcome_or_judged_fit": False, "shown": 1,
             "wrong_rows": [], "unsure_rows": []},
        ]
        whole = exam.summarize(results)["all"]
        self.assertEqual((whole["passed"], whole["passed_from_data"], whole["cases"]),
                         (2, 1, 3))

    def test_comparison_counts_flips_both_ways(self) -> None:
        before = [{"id": "a", "outcome_ok": False}, {"id": "b", "outcome_ok": True},
                  {"id": "c", "outcome_ok": True}]
        after = [{"id": "a", "outcome_ok": True}, {"id": "b", "outcome_ok": False},
                 {"id": "c", "outcome_ok": True}, {"id": "d", "outcome_ok": True}]
        self.assertEqual(exam.compare(before, after),
                         {"cases": 3, "fixed": ["a"], "broken": ["b"], "net": 0})


class FakeAsker:
    def ask(self, model: str, system: str, case: dict, schema: dict, **_: object) -> dict:
        if "number" in case:
            return {"message": f"gaqvt {case['number']}?", "car": "", "part": ""}
        if system == exam.VIN_SYSTEM:
            return {"vins": []}
        return {"message": "need this part for my car", "car": "Honda Civic", "part": "x"}


class FakeModels:
    judge_model = "judge"

    def __init__(self) -> None:
        self._asker = FakeAsker()

    def write(self, row: object, language: str, typos: bool, reading: str = "") -> dict:
        return {"askable": True, "message": f"{row.vehicle} part please", "car": row.vehicle,
                "year": 0, "part": row.name, "names": "model", "language": language}

    def answer(self, *_: object) -> dict:
        return {"action": "done", "option": 0, "text": ""}


class BuildTests(unittest.TestCase):
    def test_the_same_seed_draws_the_same_cases_with_frozen_truth(self) -> None:
        pools = exam.Pools(INDEX)
        shares = {"text": 0.4, "number": 0.4, "alternative": 0.2}
        first = exam.Builder(pools, FakeModels()).build(10, 5, shares, parallel=4)
        second = exam.Builder(pools, FakeModels()).build(10, 5, shares, parallel=1)
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])
        self.assertEqual([c["message"] for c in first], [c["message"] for c in second])
        kinds = {case["kind"] for case in first}
        self.assertTrue({"text", "number"} <= kinds)
        for case in first:
            self.assertTrue(case["truth"]["rows"])
            if case["kind"] == "number":
                self.assertIn(normalize_code(case["typed"]), normalize_code(case["message"]))


class FakeStore:
    """Replies with the scripted cards, one reply per message."""

    def __init__(self, replies: list[list[dict]]) -> None:
        self.replies, self.deleted = list(replies), []

    def create(self) -> dict:
        return {"conversation_id": "c1"}

    def submit(self, conversation: str, message: str, **_: object) -> dict:
        self.current = self.replies.pop(0)
        return {"turn_id": "t"}

    def get(self, *_: object) -> dict:
        cards = [{"product": {"source_product_id": item, "brand": "B", "original_code": "N",
                              "stock": {"store": 1}}, "confidence": "listed"}
                 for item in self.current]
        return {"status": "COMPLETE", "outcome": "offered", "cards": cards, "answer": "ok",
                "diagnostics": {"requests": [{}, {}], "tools": [{"name": "search_catalog",
                                                                 "ms": 20}]}}

    def delete(self, conversation: str) -> None:
        self.deleted.append(conversation)


class ConverseTests(unittest.TestCase):
    def test_the_conversation_stops_when_a_truth_row_shows(self) -> None:
        store = FakeStore([["9"], ["2"], ["1"]])
        case = {"kind": "text", "message": "pads", "truth": {"rows": ["2"], "nothing": False},
                "request": {"car": "MB", "part": "pads", "language": "en"},
                "row": {"id": "1", "name": "pads", "vehicle": "MB W212", "in_stock": True}}

        class Talker(FakeModels):
            def answer(self, *_: object) -> dict:
                return {"action": "say", "option": 0, "text": "W212 2014"}

        turns = exam.converse(store, Talker(), case, sleep=lambda _: None)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["cards"][0]["id"], "2")
        self.assertEqual(turns[0]["estimated_seconds"], round(2 * exam.REQUEST_SECONDS + 0.02,
                                                             2))
        self.assertEqual(store.deleted, ["c1"])

    def test_a_crawled_alternative_customer_owns_the_crawled_car(self) -> None:
        case = {"variant": "crawled", "request": {"car": "Land Rover Range Rover Sport TDV8"},
                "row": {"id": "1", "name": "oil filter", "vehicle": "VAG AUDI A6",
                        "number": "OE0056", "cross_numbers": "HU8003X", "in_stock": True}}
        customer = exam._customer_row(case)
        self.assertEqual(customer["vehicle"], "Land Rover Range Rover Sport TDV8")
        self.assertEqual(customer["cross_codes"], ())


if __name__ == "__main__":
    unittest.main()
