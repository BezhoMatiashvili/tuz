"""The assistant evaluation runner: its checks, scenario file, VIN handling, ledger and
summary, with fake payloads and a fake store. No network, no model, no database."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import anthropic

from automotive import assistant_eval as runner
from automotive.assistant_catalog import CatalogIndex

VIN = "1HGCM82633A004352"
TOPICS = {"make": ["მარკ", r"\bmake\b"], "fuel": ["საწვავ", "ბენზინ", "დიზელ", r"\bfuel\b"]}


def card(brand, number, name="სამუხრუჭე ხუნდი წნ", vehicle="TOYOTA/LEXUS CAMRY 2006->",
         store=1.0, warehouse=0.0, **extra):
    return {"product": {"brand": brand, "original_code": number, "product_name": name,
                        "product_model": vehicle, "stock": {"store": store, "tuz11": warehouse}},
            "availability_status": "IN_STORE", "confidence": "listed", "group": "g1",
            "reason": "", "evidence": "", **extra}


def reply(answer="ხუნდები ქვემოთაა.", *, cards=(), question=None, options=(), suggestions=(),
          outcome="offered", status="COMPLETE", **extra):
    """A finished turn as ``ConversationStore.get`` returns it."""
    payload = {
        "status": status, "engine_status": "OK" if status == "COMPLETE" else "TIMEOUT",
        "answer": answer, "cards": list(cards), "outcome": outcome, "car_label": "",
        "question": {"text": question, "options": [{"label": label, "value": label}
                                                   for label in options]} if question else None,
        "suggestions": [{"label": label, "value": label} for label in suggestions],
        "groups": [], "warnings": [],
        "usage": {"model_requests": 2, "tool_executions": 1, "input_tokens": 1000,
                  "output_tokens": 500, "cache_read_input_tokens": 28000,
                  "cache_creation_input_tokens": 0},
        "diagnostics": {"tools": [], "requests": [], "corrections": []}}
    payload.update(extra)
    return payload


OFFER = reply(cards=[card("SATO", "PN1521"), card("CTR", "CL0311(CLKH65)")],
              suggestions=["უკანა ხუნდები"], car_label="Toyota Camry 2015")


class MoneyTests(unittest.TestCase):
    def test_a_turn_is_priced_by_the_models_rates(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
        self.assertAlmostEqual(runner.cost("claude-haiku-5-5", usage), 0.10 + 0.50 + 0.01 + 0.125)
        self.assertAlmostEqual(runner.cost("claude-sonnet-5-5", usage), 2 + 10 + 0.20 + 2.50)
        # A model nobody priced is assumed to be the dearest one.
        self.assertAlmostEqual(runner.cost("claude-next", usage), 4 + 20 + 0.20 + 5)
        self.assertEqual(runner.cost("claude-haiku-5-5", None), 0.0)
        # A cache write kept for an hour costs twice the input price; the rest of what was
        # written, the usual write price.
        hour = {"cache_creation_input_tokens": 1_000_000,
                "cache_creation_1h_input_tokens": 800_000}
        self.assertAlmostEqual(runner.cost("claude-sonnet-5-5", hour), 0.8 * 4 + 0.2 * 2.50)
        self.assertAlmostEqual(runner.cost("claude-haiku-5-5", hour), 0.8 * 0.20 + 0.2 * 0.125)
        self.assertAlmostEqual(runner.cost("claude-opus-5-5", hour), 0.8 * 8 + 0.2 * 5)
        self.assertAlmostEqual(runner.cost("claude-sonnet-5-5", {
            "cache_creation_input_tokens": 100, "cache_creation_1h_input_tokens": 900}),
            100 * 4 / 1e6)  # Never more than was written at all.
        kept_an_hour = SimpleNamespace(assistant_cache_ttl="1h")
        prefix = runner.WARM_REQUEST["cache_creation_input_tokens"]
        self.assertAlmostEqual(runner.cost("claude-sonnet-5-5", runner.warm_request(
            kept_an_hour)), prefix * 4 / 1e6)
        self.assertAlmostEqual(runner.cost("claude-sonnet-5-5", runner.warm_request(
            SimpleNamespace(assistant_cache_ttl="5m"))), prefix * 2.50 / 1e6)
        self.assertIn("cache_creation_1h_input_tokens", runner.USAGE_KEYS)

    def test_opening_the_application_costs_what_the_provider_counted(self):
        def service(usage):
            return SimpleNamespace(settings=SimpleNamespace(assistant_cache_ttl="1h"),
                                   assistant_state={"claude-sonnet-5-5": {"warm_usage": usage}})

        prefix = runner.WARM_REQUEST["cache_creation_input_tokens"]
        assumed = prefix * 4 / 1e6
        # A prompt the provider still held is read, not written again.
        read = {"input_tokens": 3, "cache_read_input_tokens": 35_000}
        self.assertAlmostEqual(runner.opening_cost(service(read), "claude-sonnet-5-5"),
                               (3 * 2 + 35_000 * 0.20) / 1e6)
        written = {"cache_creation_input_tokens": 35_000,
                   "cache_creation_1h_input_tokens": 35_000}
        self.assertAlmostEqual(runner.opening_cost(service(written), "claude-sonnet-5-5"),
                               35_000 * 4 / 1e6)
        # No count, or a count of nothing: the fixed prompt is assumed written.
        for usage in (None, {"input_tokens": 0, "cache_read_input_tokens": 0}):
            self.assertAlmostEqual(runner.opening_cost(service(usage), "claude-sonnet-5-5"),
                                   assumed)
        self.assertAlmostEqual(runner.opening_cost(service(read), "claude-haiku-5-5"),
                               prefix * 2 * 0.10 / 1e6)  # Another model was never opened.

    def test_the_ledger_adds_up_and_survives_a_bad_line(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = runner.Ledger(Path(folder) / "deep" / "ledger.jsonl")
            self.assertEqual(ledger.total(), 0.0)
            ledger.append("claude-haiku-5-5", "first", 1.25)
            with ledger.path.open("a", encoding="utf-8") as handle:
                handle.write("not json\n{\"usd\": \"x\"}\n\n")
            ledger.append("claude-sonnet-5-5", "second", 0.5)
            self.assertAlmostEqual(ledger.total(), 1.75)
            first = json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual((first["model"], first["what"], first["usd"]),
                             ("claude-haiku-5-5", "first", 1.25))


class ScenarioFileTests(unittest.TestCase):
    def load(self, scenarios, topics=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "conversations.json"
            path.write_text(json.dumps({"topics": topics or TOPICS, "scenarios": scenarios}),
                            encoding="utf-8")
            return runner.load_scenarios(path)

    def test_the_shipped_file_loads_and_covers_the_plans_cases(self):
        data = runner.load_scenarios()
        ids = [scenario["id"] for scenario in data["scenarios"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(sum(1 for name in ids if name.startswith("log-")), 33)
        self.assertGreaterEqual(sum(1 for name in ids if name.startswith("gold-")), 36)
        # No scenario carries a real VIN: only placeholders.
        text = runner.DEFAULT_SCENARIOS.read_text(encoding="utf-8")
        self.assertIsNone(runner.VIN.search(text))
        self.assertIn("{VIN:any}", text)
        smoke = runner.select(data["scenarios"], "smoke")
        self.assertTrue(5 <= len(smoke) < len(ids))
        self.assertEqual([scenario["id"] for scenario in runner.select(
            data["scenarios"], "log-19-typed-part-number, quick")],
            ["log-19-typed-part-number", "quick-greeting-ka", "quick-greeting-en"])
        self.assertEqual(len(runner.select(data["scenarios"], None)), len(ids))

    def test_a_file_that_cannot_be_run_says_where(self):
        turn = {"say": "Camry 2015 ხუნდები"}
        cases = [
            ([{"id": "a", "turns": [turn]}, {"id": "a", "turns": [turn]}], "used twice"),
            ([{"id": "a", "turns": []}], "at least one turn"),
            ([{"id": "a", "turns": [{"say": " "}]}], "a turn 1: `say`"),
            ([{"id": "a", "turns": [{**turn, "expects": {}}]}], "unknown keys ['expects']"),
            ([{"id": "a", "turns": [{**turn, "kind": "slow"}]}], "kind must be one of"),
            ([{"id": "a", "turns": [{**turn, "expect": {"offer_any": []}}]}],
             "unknown expectations ['offer_any']"),
            ([{"id": "a", "turns": [{**turn, "expect": {"no_question_about": ["colour"]}}]}],
             "topic 'colour' is not defined"),
            ([{"id": "a", "turns": [{**turn, "expect": {"answer_lacks": ["("]}}]}],
             "is not a pattern"),
            ([{"id": "a", "turns": [{**turn, "expect": {"either": [{"either": []}]}}]}],
             "cannot be nested"),
        ]
        for scenarios, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(runner.ScenarioError) as caught:
                self.load(scenarios)
            self.assertIn(expected, str(caught.exception))
        with self.assertRaises(runner.ScenarioError):
            self.load([{"id": "a", "turns": [turn]}], topics={"make": ["("]})

    def test_vin_placeholders_are_filled_from_the_private_file_or_the_scenario_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vins.csv"
            path.write_text(f"name,vin\nAny, {VIN.lower()} \nshort,123\n", encoding="utf-8")
            vins = runner.read_vins(path)
            self.assertEqual(vins, {"any": VIN})
            self.assertEqual(runner.read_vins(Path(folder) / "missing.csv"), {})
        self.assertEqual(runner.fill("ფილტრი {VIN:any}", vins), f"ფილტრი {VIN}")
        self.assertIsNone(runner.fill("{VIN:jeep} ზეთი", vins))
        self.assertEqual(runner.fill("no placeholder", {}), "no placeholder")
        self.assertEqual(runner.needed_vins({"turns": [{"say": "{VIN:any} {VIN:any}"},
                                                       {"say": "{VIN:jeep}"}]}),
                         ["any", "jeep"])


class StandInTests(unittest.TestCase):
    """A VIN scenario runs without a customer's VIN: a stand-in decodes to a written car."""

    CARS = {"Jeep": {"make": "JEEP", "model": "Cherokee", "model_year": 2019,
                     "displacement_l": "2.4", "fuel": "Gasoline", "trim": ""}}
    SCENARIOS = [{"id": "a", "turns": [{"say": "{VIN:jeep} ზეთი"}, {"say": "{VIN:any}"}]},
                 {"id": "b", "turns": [{"say": "{VIN:unknown} ხუნდები"}]}]

    def test_a_stand_in_vin_is_a_vin_of_no_car_and_is_settled_by_its_name(self):
        from automotive.vin import validate_vin

        vin = runner.stand_in_vin("jeep")
        self.assertEqual((validate_vin(vin), len(vin), vin[0]), (vin, 17, "0"))
        self.assertIsNotNone(runner.VIN.fullmatch(vin))
        self.assertEqual(runner.stand_in_vin("JEEP"), vin)
        self.assertNotEqual(runner.stand_in_vin("any"), vin)

    def test_only_a_placeholder_without_a_vin_gets_its_stand_in(self):
        vins = {"any": VIN}
        cars = runner.stand_ins({**self.CARS, "any": {"make": "TOYOTA", "model": "Camry",
                                                       "model_year": 2015}},
                                self.SCENARIOS, vins)
        stand_in = runner.stand_in_vin("jeep")
        self.assertEqual(vins, {"any": VIN, "jeep": stand_in})  # The real VIN stays.
        self.assertEqual(cars, {stand_in: self.CARS["Jeep"]})
        self.assertEqual(runner.stand_ins({}, self.SCENARIOS, {}), {})
        # The shipped file writes a car for every placeholder its scenarios use.
        data = runner.load_scenarios()
        needed = {name for scenario in data["scenarios"]
                  for name in runner.needed_vins(scenario)}
        self.assertEqual(needed, {"any", "e_class_w212", "jeep_cherokee_2019"})
        self.assertEqual(set(data["cars"]), needed)
        self.assertEqual(len(runner.stand_ins(data["cars"], data["scenarios"], {})), 3)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "conversations.json"
            path.write_text(json.dumps({"scenarios": [{"id": "a", "turns": [{"say": "x"}]}],
                                        "stand_in_cars": {"any": {"make": "TOYOTA"}}}),
                            encoding="utf-8")
            with self.assertRaises(runner.ScenarioError) as caught:
                runner.load_scenarios(path)
        self.assertIn("make, model and model_year", str(caught.exception))

    def test_a_stand_in_decodes_here_and_goes_to_the_sources_as_a_typed_car(self):
        stand_in = runner.stand_in_vin("jeep")
        cars = {stand_in: self.CARS["Jeep"]}
        real = SimpleNamespace(calls=[], hmac_key=b"k")
        real.decode = lambda vin, model_year=None, *, deadline=None: (
            real.calls.append(vin) or {"status": "PROVISIONAL", "vehicle": {"make": "HONDA"}})
        decoder = runner.StandInDecoder(real, cars)
        decoded = decoder.decode(stand_in.lower(), deadline=5.0)
        self.assertEqual((decoded["status"], decoded["vehicle"], real.calls),
                         ("PROVISIONAL", self.CARS["Jeep"], []))
        decoded["vehicle"]["make"] = "changed"  # A caller's copy, not the run's car.
        self.assertEqual(decoder.decode(stand_in)["vehicle"]["make"], "JEEP")
        self.assertEqual(decoder.decode(VIN)["vehicle"], {"make": "HONDA"})
        self.assertEqual((real.calls, decoder.hmac_key), ([VIN], b"k"))

        inner = SimpleNamespace(started=[], warmed=[], motorium_enabled=True)
        inner.start = lambda *args, **kwargs: inner.started.append((args, kwargs)) or {"id": 1}
        inner.prewarm = inner.warmed.append
        search = runner.StandInSearch(inner, cars)
        self.assertEqual(search.start(stand_in, "engine oil", None, {"fuel": "petrol"}),
                         {"id": 1})
        # No VIN leaves the process: make, model and year stand for the car.
        (vin, query, year, car), extra = inner.started[0]
        self.assertEqual((vin, query, year, extra), (None, "engine oil", 2019, {}))
        self.assertEqual(car, {"make": "JEEP", "model": "Cherokee", "model_year": 2019,
                               "displacement_l": "2.4", "fuel": "petrol"})
        search.start(VIN, "oil filter", 2015, None)
        self.assertEqual(inner.started[1], ((VIN, "oil filter", 2015, None), {}))
        search.start(None, "pads", 2015, {"make": "TOYOTA"})
        self.assertEqual(inner.started[2][0][0], None)
        search.prewarm(stand_in)
        search.prewarm(VIN)
        self.assertEqual((inner.warmed, search.motorium_enabled), ([VIN], True))

    def test_a_scenario_with_a_stand_in_runs_and_its_vin_is_never_recorded(self):
        vins: dict = {}
        cars = runner.stand_ins({"any": {"make": "TOYOTA", "model": "Camry",
                                         "model_year": 2015}}, [SCENARIOS[1]], vins)
        stand_in, = cars
        store = FakeStore({f"ფილტრი {stand_in}": reply("ფილტრი ქვემოთაა.",
                                                       cards=[card("MANN", "C 2201")])})
        record = runner.run_scenario(store, SCENARIOS[1], vins=vins, model="claude-haiku-5-5",
                                     clock=Clock(), sleep=lambda _: None)
        self.assertEqual((record["status"], store.sent[0][0]), ("passed", f"ფილტრი {stand_in}"))
        self.assertNotIn(stand_in, json.dumps(record, ensure_ascii=False))
        self.assertEqual(record["turns"][0]["say"], "ფილტრი {VIN:any}")


class ReadingTests(unittest.TestCase):
    def test_a_row_is_named_by_brand_and_number(self):
        self.assertTrue(runner.row_matches("SATO", "PN1521", "SATO PN1521"))
        self.assertTrue(runner.row_matches("SATO", "PN1521", "pn-1521"))
        self.assertFalse(runner.row_matches("ABS", "PN1521", "SATO PN1521"))
        self.assertFalse(runner.row_matches("SATO", "PN15210", "SATO PN1521"))
        # A bracketed or slashed number is also each of its pieces; brands may be cut short.
        self.assertTrue(runner.row_matches("CTR", "CL0311(CLKH65)", "CTR CL0311"))
        self.assertTrue(runner.row_matches("SATO", "D1346/PN0389", "SATO D1346/PN0389"))
        self.assertTrue(runner.row_matches("SATO", "PN0228/PN0093", "SATO PN0093"))
        self.assertTrue(runner.row_matches("ATE", "13.0460-7259.2", "ATE 13.0460-7259.2"))
        self.assertTrue(runner.row_matches("JS-ASAKASHI", "A1539", "ASAKASHI A1539"))
        self.assertTrue(runner.row_matches("SB NAGAMOCHI", "BP31019W", "SB BP31019W"))
        self.assertTrue(runner.row_matches("MOBIL", "5W30", "5W-30"))
        self.assertFalse(runner.row_matches("", "PN1521", "SATO PN1521"))
        self.assertEqual(runner.parse_row("JS ASAKASHI A-1539"), ("JSASAKASHI", "A1539"))

    def test_the_language_is_the_script_of_most_letters(self):
        self.assertEqual(runner.detect_language("Camry 2015-ის წინა ხუნდები ქვემოთაა."), "ka")
        self.assertEqual(runner.detect_language("Задние колодки на Prado 150 ниже."), "ru")
        self.assertEqual(runner.detect_language("Front pads for the Camry are below."), "en")
        self.assertEqual(runner.detect_language("2015 - 150"), "")

    def test_a_reply_asks_through_its_question_its_outcome_or_a_question_mark(self):
        self.assertFalse(runner.asks(OFFER))
        self.assertTrue(runner.asks(reply(question="წინა თუ უკანა?", options=["წინა", "უკანა"])))
        self.assertTrue(runner.asks(reply("რომელი მანქანაა? მომწერეთ მარკა.")))
        self.assertTrue(runner.asks(reply("მომწერეთ მარკა.", outcome="asked")))
        asked = reply("Camry 2015. რომელი ძრავია? ვნახავ.", question="2.5 თუ 3.5?",
                      options=["2.5", "3.5"])
        self.assertEqual(runner.asked_text(asked), "2.5 თუ 3.5? რომელი ძრავია?")
        tools = {"diagnostics": {"tools": [{"name": "search_catalog", "args": {
            "make_terms": ["MMC", "MITSUBISHI"], "vehicle_terms": ["L200", "PAJERO III"],
            "part_terms": ["წევის დაბოლო"], "limit": 20}}]}}
        self.assertTrue({"mmc", "l200", "pajero", "iii", "წევის"} <= runner.searched_terms(tools))


class CheckTests(unittest.TestCase):
    def check(self, expect, payload=OFFER, **kwargs):
        kwargs.setdefault("topics", TOPICS)
        return runner.check_turn(expect, payload, **kwargs)

    def test_rows_that_should_be_offered_are_required_only_while_in_stock(self):
        passed = self.check({"offers_any": ["SATO PN1521", "ABS 37677"]})
        self.assertEqual((passed["failures"], passed["found"], passed["expected"]), ([], 1, 2))
        missed = self.check({"offers_any": ["ABS 37677", "SB BP21521"]})
        self.assertEqual(missed["failures"], ["none offered of: ABS 37677, SB BP21521"])
        self.assertEqual((missed["found"], missed["expected"]), (0, 2))
        sold_out = self.check({"offers_any": ["ABS 37677"]}, stock=lambda spec: False)
        self.assertEqual((sold_out["failures"], sold_out["expected"]), ([], 0))
        self.assertIn("none in stock today", sold_out["notes"][0])
        # Several sets: each needs one of its rows.
        both = self.check({"offers_any": [["SATO PN1521"], ["ATE 13.0460-7259.2"]]})
        self.assertEqual(both["failures"], ["none offered of: ATE 13.0460-7259.2"])

        stock = {"SATO PN1521": True, "CTR CL0311": True, "ABS 37677": True, "SB BP21521": False,
                 "GONE 1": None}.get
        every = self.check({"offers_all": ["SATO PN1521", "CTR CL0311", "ABS 37677",
                                           "SB BP21521", "GONE 1"]}, stock=stock)
        self.assertEqual(every["failures"], ["in stock but not offered: ABS 37677"])
        self.assertEqual((every["found"], every["expected"]), (2, 3))
        soft = self.check({"soft_offers": ["ABS 37677"], "soft_none": ["SATO PN1521"]})
        self.assertEqual((soft["failures"], soft["soft_wrong"]), ([], ["SATO PN1521"]))
        self.assertEqual(len(soft["notes"]), 2)

    def test_rows_that_must_not_be_offered_are_wrong_offers(self):
        wrong = self.check({"offers_none": ["SATO PN1521", "SATO PN1857"]})
        self.assertEqual((wrong["wrong"], wrong["failures"]),
                         (["SATO PN1521"], ["must-not row offered: SATO PN1521"]))
        only = self.check({"offers_only": ["CTR CL0311"]})
        self.assertEqual(only["wrong"], ["SATO PN1521"])
        self.assertEqual(self.check({"offers_only": ["NGK 4912"]}, reply(
            "სანთელი ვერ მოიძებნა.", suggestions=["VIN"], outcome="nothing_found"))["wrong"], [])

    def test_every_question_needs_two_to_eight_answer_buttons(self):
        asked = reply("Camry 2015.", question="წინა თუ უკანა?", options=["წინა", "უკანა"],
                      outcome="asked")
        self.assertEqual(self.check({"asks": True, "options_min": 2}, asked)["failures"], [])
        self.assertFalse(self.check({}, asked)["question_without_options"])
        in_text = self.check({}, reply("რომელი მანქანისთვის გჭირდებათ? მომწერეთ მარკა.",
                                        suggestions=["წინა ხუნდები"], outcome="chat"))
        self.assertTrue(in_text["question_without_options"])
        self.assertIn("asks with 0 answer buttons", in_text["failures"][0])
        nine = self.check({}, reply(question="რომელი?", options=[str(n) for n in range(9)]))
        self.assertTrue(nine["question_without_options"])
        self.assertEqual(self.check({"asks": True})["failures"],
                         ["expected a question, the reply asks nothing"])
        self.assertIn("expected no question", self.check({"asks": False}, asked)["failures"][0])
        self.assertIn("2 answer buttons, expected at least 3",
                      self.check({"options_min": 3}, asked)["failures"][0])

    def test_a_dead_end_a_failed_turn_and_a_vin_always_fail(self):
        dead = self.check({}, reply("ვერ ვიპოვე.", outcome="nothing_found"))
        self.assertTrue(dead["dead_end"])
        self.assertIn("dead end", dead["failures"][0])
        self.assertFalse(self.check({}, reply("ვერ ვიპოვე.", suggestions=["VIN"]))["dead_end"])
        failed = self.check({}, reply("ბოდიში.", status="FAILED", suggestions=["ხელახლა ცდა"]))
        self.assertEqual((failed["failed"], failed["timeout"]), (True, True))
        self.assertIn("turn did not complete: FAILED/TIMEOUT", failed["failures"][0])
        self.assertIn("empty reply", self.check({}, reply("", suggestions=["x"]))["failures"])
        self.assertIn("a VIN is in the reply",
                      self.check({}, reply(f"VIN {VIN}", suggestions=["x"]))["failures"])
        # Only what the customer reads is looked at: an id of 17 characters is no VIN.
        internal = reply("ხუნდები ქვემოთაა.", suggestions=["x"], conversation_id="A" * 17,
                         diagnostics={"tools": [{"args": {"numbers": [VIN]}}]})
        self.assertEqual(self.check({}, internal)["failures"], [])

    def test_a_question_about_a_known_fact_is_redundant(self):
        asked = reply("Camry 2015.", question="რომელი მარკის მანქანაა?",
                      options=["Toyota", "Lexus"], outcome="asked")
        result = self.check({"no_question_about": ["make", "fuel"]}, asked)
        self.assertEqual(result["redundant"], ["make"])
        self.assertIn("asks about make", result["failures"][0])
        # The word may stand in an option or a statement: only questions count.
        fine = reply("ბენზინის ძრავზე ფილტრი ქვემოთაა.", question="2.0 თუ 2.4?",
                     options=["2.0 ბენზინი", "2.4 ბენზინი"], outcome="asked")
        self.assertEqual(self.check({"no_question_about": ["fuel"]}, fine)["redundant"], [])
        again = self.check({"not_same_question": True}, asked, previous=asked)
        self.assertEqual(again["redundant"], ["same question"])
        self.assertEqual(self.check({"not_same_question": True}, asked, previous=OFFER)[
            "failures"], [])

    def test_the_remaining_expectations(self):
        cases = [
            ({"car_label_has": ["camry", "2015"]}, None),
            ({"car_label_has": ["Corolla"]}, "lacks 'Corolla'"),
            ({"car_label_empty": True}, "names a car the customer did not"),
            ({"car_label_empty": False}, None),
            ({"max_requests": 2}, None), ({"max_requests": 1}, "2 model requests"),
            ({"max_seconds": 6}, "7.5 s, at most 6 s"), ({"max_seconds": 8}, None),
            ({"reply_language": "ka"}, None), ({"reply_language": "en"}, "reply is in ka"),
            ({"outcome_in": ["offered"]}, None), ({"outcome_in": ["asked"]}, "outcome 'offered'"),
            ({"answer_has": ["ქვემოთ"]}, None), ({"answer_has": ["ზემოთ"]}, "reply lacks"),
            ({"answer_lacks": [r"\d+\s*ლარ"]}, None),
            ({"answer_lacks": ["ხუნდ"]}, "reply contains"),
            ({"cards_min": 2, "cards_max": 2}, None), ({"cards_min": 3}, "at least 3"),
            ({"cards_max": 1}, "at most 1"), ({"groups_min": 1}, "0 groups"),
            ({"searched": ["mmc"]}, "no catalogue search used the term 'mmc'"),
        ]
        for expect, expected in cases:
            with self.subTest(expect=expect):
                failures = self.check(expect, seconds=7.5)["failures"]
                if expected is None:
                    self.assertEqual(failures, [])
                else:
                    self.assertEqual(len(failures), 1)
                    self.assertIn(expected, failures[0])

    def test_either_takes_the_alternative_that_holds(self):
        expect = {"car_label_has": ["Camry"],
                  "either": [{"asks": True, "options_min": 2},
                             {"offers_any": ["SATO PN1521"]}]}
        held = self.check(expect)
        self.assertEqual((held["failures"], held["found"], held["expected"]), ([], 1, 1))
        neither = self.check({"either": [{"asks": True}, {"offers_any": ["ABS 37677"]}]})
        self.assertEqual(len(neither["failures"]), 1)
        self.assertIn("no alternative held", neither["failures"][0])
        self.assertIn("expected a question", neither["failures"][0])


class FakeStore:
    """A conversation store that answers each message from a table."""

    def __init__(self, answers):
        self.answers, self.sent, self.deleted = answers, [], []
        self.chat_service = SimpleNamespace(settings=SimpleNamespace())

    def create(self):
        return {"conversation_id": f"c{len(self.sent)}"}

    def submit(self, identifier, message, **kwargs):
        self.sent.append((message, kwargs))
        return {"turn_id": message}

    def get(self, identifier, turn_id, offset=0, limit=20):
        return self.answers[turn_id]

    def delete(self, identifier):
        self.deleted.append(identifier)


class Clock:
    """Each look at the clock is one second later."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


QUESTION = reply("ML350 2013.", question="ბენზინი თუ დიზელი?", options=["ბენზინი", "დიზელი"],
                 outcome="asked")
SCENARIOS = [
    {"id": "tap", "tags": ["gold"], "turns": [
        {"say": "ML350 2013 ჰაერის ფილტრი", "kind": "part",
         "expect": {"asks": True, "options_min": 2}},
        {"say": "დიზელზეა", "tap": ["დიზელ", "diesel"], "kind": "quick",
         "expect": {"offers_any": ["SATO PN1521"], "max_requests": 2}}]},
    {"id": "vin", "tags": ["vin"], "turns": [{"say": "ფილტრი {VIN:any}"}]},
    {"id": "wrong", "turns": [{"say": "Camry 2019 ხუნდები", "kind": "part",
                               "expect": {"offers_none": ["SATO PN1521"]}}]},
]
ANSWERS = {"ML350 2013 ჰაერის ფილტრი": QUESTION, "დიზელი": OFFER, "Camry 2019 ხუნდები": OFFER,
           f"ფილტრი {VIN}": reply("ფილტრი ქვემოთაა.", cards=[card("MANN", "C 2201")])}


class RunTests(unittest.TestCase):
    def run_one(self, scenario, **kwargs):
        store = FakeStore(ANSWERS)
        record = runner.run_scenario(store, scenario, vins=kwargs.pop("vins", {}),
                                     model="claude-haiku-5-5", topics=TOPICS, clock=Clock(),
                                     sleep=lambda _: None, **kwargs)
        return store, record

    def test_a_tapped_answer_is_sent_as_the_buttons_label_and_number(self):
        store, record = self.run_one(SCENARIOS[0])
        self.assertEqual(store.sent, [
            ("ML350 2013 ჰაერის ფილტრი", {"language": "auto"}),
            ("დიზელი", {"language": "auto", "choice": 2})])
        self.assertEqual((record["status"], store.deleted), ("passed", ["c0"]))
        first, second = record["turns"]
        self.assertEqual((first["asks"], first["options"], first["tapped"]),
                         (True, ["ბენზინი", "დიზელი"], None))
        self.assertEqual((second["say"], second["tapped"], second["kind"]),
                         ("დიზელი", 2, "quick"))
        self.assertEqual((second["found"], second["expected"], second["failures"]), (1, 1, []))
        self.assertEqual(second["cards"][0]["number"], "PN1521")
        self.assertAlmostEqual(second["cost_usd"], runner.cost(
            "claude-haiku-5-5", OFFER["usage"]), places=5)
        self.assertEqual(second["seconds"], 1.0)
        # No button matches: the turn's own words are typed.
        typed = {**SCENARIOS[0], "turns": [SCENARIOS[0]["turns"][0],
                                            {"say": "Camry 2019 ხუნდები", "tap": ["LPG"]}]}
        store, _ = self.run_one(typed)
        self.assertEqual(store.sent[1], ("Camry 2019 ხუნდები", {"language": "auto"}))

    def test_a_scenario_without_its_vin_is_skipped_and_a_vin_is_never_recorded(self):
        store, record = self.run_one(SCENARIOS[1])
        self.assertEqual((record["status"], record["reason"], store.sent),
                         ("skipped", "no VIN for any", []))
        store, record = self.run_one(SCENARIOS[1], vins={"any": VIN})
        self.assertEqual(store.sent[0][0], f"ფილტრი {VIN}")
        self.assertEqual(record["status"], "passed")
        self.assertNotIn(VIN, json.dumps(record, ensure_ascii=False))
        self.assertEqual(record["turns"][0]["say"], "ფილტრი {VIN:any}")

    def test_a_run_prints_one_line_per_turn_and_stops_when_the_money_is_gone(self):
        out = io.StringIO()
        results, spent = runner.evaluate(
            FakeStore(ANSWERS), SCENARIOS, vins={}, model="claude-haiku-5-5", out=out,
            topics=TOPICS, clock=Clock(), sleep=lambda _: None)
        self.assertEqual([record["status"] for record in results],
                         ["passed", "skipped", "failed"])
        self.assertAlmostEqual(spent, sum(record["cost_usd"] for record in results))
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("ok   tap t1 1.0s req=2 out=500 $"))
        self.assertIn("asks [ბენზინი | დიზელი]", lines[0])
        self.assertIn("2 cards [SATO PN1521, CTR CL0311(CLKH65)]", lines[1])
        self.assertIn("skipped vin (no VIN for any)", lines[2])
        self.assertTrue(lines[3].startswith("FAIL wrong t1"))
        self.assertEqual(lines[4].strip(), "- must-not row offered: SATO PN1521")

        summary = runner.summarize(results)
        self.assertEqual(summary["scenarios"], {"total": 3, "passed": 1, "failed": 1,
                                                "skipped": 1, "not_run": 0})
        self.assertEqual((summary["turns"], summary["parts_found"], summary["parts_expected"],
                          summary["wrong_rows_offered"], summary["questions"],
                          summary["questions_without_options"], summary["failed_turns"]),
                         (3, 1, 1, 1, 1, 0, 0))
        self.assertEqual(summary["seconds"]["part"], {"count": 2, "median": 1.0, "p90": 1.0,
                                                      "max": 1.0})
        self.assertEqual(summary["targets"]["part_median_le_6s"], True)
        self.assertEqual(summary["tokens"]["output_tokens"], 1500)
        self.assertEqual((summary["failed"], summary["skipped"]), (["wrong"], ["vin"]))
        printed = io.StringIO()
        runner.print_summary(summary, printed, "claude-haiku-5-5")
        self.assertIn("scenarios passed           1 of 2 (1 skipped, 0 not run)",
                      printed.getvalue())
        self.assertIn("failed: wrong", printed.getvalue())

        # With the budget already used nothing is sent.
        store = FakeStore(ANSWERS)
        results, spent = runner.evaluate(store, SCENARIOS, vins={}, model="claude-haiku-5-5",
                                         out=io.StringIO(), budget_left=0.0)
        self.assertEqual(({record["status"] for record in results}, spent, store.sent),
                         ({"not_run"}, 0.0, []))

    def test_a_run_ends_when_the_providers_account_refuses_a_turn(self):
        # No credit or no working key: such a turn is billed nothing, and the rest of the
        # run is not started, because it could only fail the same way.
        usage = dict.fromkeys(("model_requests", "tool_executions", "input_tokens",
                               "output_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens"), 0)
        refusal = reply("ბოდიში, ასისტენტი ახლა მიუწვდომელია.", status="FAILED",
                        outcome="handoff", engine_status="PROVIDER_BILLING", usage=usage)
        scenarios = [{"id": name, "tags": [], "turns": [{"say": name, "kind": "part"}]}
                     for name in ("first", "second", "third")]
        store = FakeStore(dict.fromkeys(("first", "second", "third"), refusal))
        printed = io.StringIO()
        results, spent = runner.evaluate(store, scenarios, vins={}, model="claude-sonnet-5-5",
                                         out=printed, clock=Clock(), sleep=lambda _: None)
        self.assertEqual([record["status"] for record in results],
                         ["failed", "not_run", "not_run"])
        self.assertEqual((spent, [sent[0] for sent in store.sent]), (0.0, ["first"]))
        self.assertIn("PROVIDER_BILLING", results[1]["reason"])
        self.assertIn("add credit", printed.getvalue())
        # A fault of one turn (a timeout) is charged as a lost request and ends nothing.
        late = reply("", status="FAILED", outcome="handoff", usage=usage)
        store = FakeStore(dict.fromkeys(("first", "second", "third"), late))
        results, spent = runner.evaluate(store, scenarios, vins={}, model="claude-sonnet-5-5",
                                         out=io.StringIO(), clock=Clock(), sleep=lambda _: None)
        self.assertEqual([record["status"] for record in results], ["failed"] * 3)
        self.assertGreater(spent, 0.0)

    def test_scenarios_run_side_by_side_keep_their_order(self):
        results, _ = runner.evaluate(
            FakeStore(ANSWERS), SCENARIOS, vins={}, model="claude-haiku-5-5",
            out=io.StringIO(), topics=TOPICS, parallel=3, sleep=lambda _: None)
        self.assertEqual([record["id"] for record in results], ["tap", "vin", "wrong"])

    def test_the_spread_reports_median_p90_and_max(self):
        spread = runner._spread([float(value) for value in range(1, 11)])
        self.assertEqual(spread, {"count": 10, "median": 5.5, "p90": 9.0, "max": 10.0})
        self.assertEqual(runner._spread([4.0])["p90"], 4.0)
        self.assertIsNone(runner._spread([])["median"])
        empty = runner.summarize([])
        self.assertEqual((empty["turns"], empty["cost_per_turn_usd"],
                          empty["targets"]["part_median_le_6s"]), (0, None, None))

    def test_a_lost_request_is_charged_and_a_broken_scenario_does_not_end_the_run(self):
        late = FakeStore({"x": reply("ბოდიში.", status="FAILED", suggestions=["ხელახლა ცდა"])})
        record = runner.run_scenario(late, {"id": "late", "turns": [{"say": "x"}]}, vins={},
                                     model="claude-sonnet-5-5", sleep=lambda _: None)
        self.assertEqual(record["status"], "failed")
        self.assertAlmostEqual(record["cost_usd"], round(runner.cost(
            "claude-sonnet-5-5", late.answers["x"]["usage"]) + runner.cost(
            "claude-sonnet-5-5", runner.LOST_REQUEST), 5))
        results, _ = runner.evaluate(FakeStore({}), [{"id": "broken", "turns": [{"say": "y"}]}],
                                     vins={}, model="claude-haiku-5-5", out=io.StringIO())
        self.assertEqual((results[0]["status"], results[0]["reason"]),
                         ("failed", "runner error: KeyError"))


class StockTests(unittest.TestCase):
    def test_stock_is_read_from_the_catalogue_by_brand_number(self):
        def row(sid, brand, code, store, warehouse):
            return {"source_product_id": sid, "product_code": sid, "product_name": "ხუნდი წნ",
                    "product_model": "TOYOTA CAMRY 2006->", "original_code": code,
                    "duplicate_code": "D1222/PN1521", "brand": brand, "price_raw": "85",
                    "stock_store": store, "stock_tuz11": warehouse, "last_observed_at": None}

        stock = runner.stock_reader(CatalogIndex.from_rows([
            row("1", "SATO", "PN1521", "6", "0"), row("2", "ABS", "37677", "0", "10"),
            row("3", "SB", "BP21521", "0", "0"), row("4", "CTR", "CL0311(CLKH65)", "2", "0")]))
        self.assertIs(stock("SATO PN1521"), True)
        self.assertIs(stock("ABS 37677"), True)
        self.assertIs(stock("SB BP21521"), False)
        self.assertIs(stock("CTR CL0311"), True)
        self.assertIs(stock("BREMBO PN1521"), None)  # Another brand's row is another row.
        self.assertIs(stock("D1222"), None)  # A cross number is not the row's own.
        self.assertIs(stock("NGK 4912"), None)


class CommandTests(unittest.TestCase):
    def test_no_run_starts_once_the_ledger_has_reached_the_budget(self):
        class Parser:
            def exit(self, status, message):
                raise SystemExit(f"{status}: {message}")

        with tempfile.TemporaryDirectory() as folder:
            ledger = runner.Ledger(Path(folder) / "ledger.jsonl")
            ledger.append("claude-sonnet-5-5", "earlier runs", 40.0)
            args = SimpleNamespace(ledger=str(ledger.path), budget=None, scenarios=None,
                                   only=None, vins=None, audience=None, model=None,
                                   parallel=1, report_out=None)
            with self.assertRaises(SystemExit) as caught:
                runner.run(Parser(), args, io.StringIO())
            self.assertIn("Nothing was run: $40.00 of the $40.00 budget", str(caught.exception))
            lines = ledger.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)  # Refusing costs nothing and records nothing.
            args.budget, args.scenarios = 50.0, str(Path(folder) / "missing.json")
            with self.assertRaises(SystemExit) as caught:
                runner.run(Parser(), args, io.StringIO())
            self.assertIn("Scenarios could not be read", str(caught.exception))

    def test_the_catalogue_check_refuses_a_spent_budget_and_an_unreadable_earlier_report(self):
        class Parser:
            def exit(self, status, message):
                raise SystemExit(f"{status}: {message}")

        with tempfile.TemporaryDirectory() as folder:
            ledger = runner.Ledger(Path(folder) / "ledger.jsonl")
            ledger.append("claude-sonnet-5-5", "earlier runs", 48.0)
            args = SimpleNamespace(evaluate_assistant="catalogue", ledger=str(ledger.path),
                                   budget=48.0, only=None, audience=None, model=None,
                                   parallel=1, report_out=None)
            with self.assertRaises(SystemExit) as caught:
                runner.run(Parser(), args, io.StringIO())
            self.assertIn("Nothing was run: $48.00 of the $48.00 budget", str(caught.exception))
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 1)
            args.budget = 60.0
            args.requests_from = str(Path(folder) / "ledger.jsonl")  # Not a report.
            with self.assertRaises(SystemExit) as caught:
                runner.run(Parser(), args, io.StringIO())
            self.assertIn("The earlier report could not be read", str(caught.exception))


# -- the catalogue-wide check ----------------------------------------------------------------

PAD, LINK, FILTER = "სამუხრუჭე ხუნდი წნ", "სტაბ კრონშტეინი წნ", "ჰაერის ფილტრი"


def stock_row(sid, vehicle, name, brand="SATO", code=None, cross="", store="1", warehouse="0"):
    """One row as site_catalog.SNAPSHOT_SQL returns it."""
    return {"source_product_id": str(sid), "product_code": f"30{sid}", "product_name": name,
            "product_model": vehicle, "original_code": code or f"PN{sid}",
            "duplicate_code": cross, "brand": brand, "price_raw": "85", "stock_store": store,
            "stock_tuz11": warehouse, "last_observed_at": None}


def small_catalogue():
    """200 rows in stock in the shapes a sample has to cover, and 40 that are not."""
    rows, sid = [], iter(range(1000, 9999))
    for vehicle, name, count in (
            ("TOYOTA/LEXUS CAMRY 2006->", PAD, 70), ("TOYOTA/LEXUS COROLLA 2008>", LINK, 30),
            ("MB W204,W211,W212", PAD, 20), ("MB W204,W211,W212", LINK, 20),
            ("HONDA CR-V III 2.4", FILTER, 24), ("HONDA CR-V III 2.4", "წყლის ტუმბო", 10),
            ("UNIVERSAL", "ძრავის ზეთი 5W30 1L", 12), ("", "ჰაერის ფილტრი", 8)):
        rows += [stock_row(next(sid), vehicle, name) for _ in range(count)]
    # Spellings too rare for a pick of their own, and a part named once.
    rows += [stock_row(next(sid), "TOYOA/LEXUS CAMRY", PAD),
             stock_row(next(sid), "HYNDAI ELANTRA 06->", LINK),
             stock_row(next(sid), "SUZUKU SWIFT", "თერმოსტატი"),
             stock_row(next(sid), "OPEL ASTRA H", "ამორტიზატორი უკ"),
             stock_row(next(sid), "FIAT DOBLO", "საჭის შლეიფი"),
             stock_row(next(sid), "JEEP CHEROKEE", "ანთების კოჭა")]
    rows += [stock_row(next(sid), "BMW F10", "ზეთის ფილტრი", store="0") for _ in range(40)]
    return CatalogIndex.from_rows(rows)


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = small_catalogue()
        cls.sample = runner.draw(cls.index.rows(), 40, 7)

    def test_the_sample_is_settled_by_the_seed_and_not_by_the_order_of_the_rows(self):
        ids = [item["row"].id for item in self.sample]
        self.assertEqual(len(ids), 40)
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual([item["row"].id for item in runner.draw(
            reversed(self.index.rows()), 40, 7)], ids)
        self.assertNotEqual([item["row"].id for item in runner.draw(
            self.index.rows(), 40, 8)], ids)
        self.assertEqual(runner.draw(self.index.rows(), 0, 7), [])
        # More than there is in stock: every stocked row, once.
        self.assertEqual(len(runner.draw(self.index.rows(), 5000, 7)), 200)

    def test_only_rows_in_stock_are_sampled(self):
        self.assertTrue(all(runner.in_stock(item["row"]) for item in self.sample))
        self.assertNotIn("bmw", {item["make"] for item in self.sample})
        warehouse = CatalogIndex.from_rows([stock_row(1, "BMW F10", PAD, store="0",
                                                      warehouse="3")])
        self.assertEqual(len(runner.draw(warehouse.rows(), 5, 1)), 1)

    def test_every_frequent_make_spelling_and_part_name_is_in_the_sample(self):
        makes = runner.Counter(item["make"] for item in self.sample)
        parts = runner.Counter(item["part"] for item in self.sample)
        # Strata are what the rows say, counted: the first word and the opening of the name.
        self.assertTrue({"toyota/lexus", "mb", "honda", "universal", ""} <= set(makes))
        self.assertTrue({"სამუხრუჭე ხუნდი", "სტაბ კრონშტეინი", "ჰაერის ფილტრი", "წყლის ტუმბო",
                         "ძრავის ზეთი"} <= set(parts))
        # About as often as in the stock: half of it is TOYOTA/LEXUS, a fifth is MB.
        self.assertEqual((makes["toyota/lexus"], makes["mb"], makes["honda"]), (20, 8, 7))
        self.assertEqual(parts["სამუხრუჭე ხუნდი"], 18)
        # A small share has no vehicle text, and the rare spellings share a quota.
        self.assertEqual(makes[""], 2)
        rare = set(makes) - {"toyota/lexus", "mb", "honda", "universal", ""}
        self.assertEqual(sum(makes[name] for name in rare), 1)
        self.assertTrue(rare <= {"toyoa/lexus", "hyndai", "suzuku", "opel", "fiat", "jeep"})

    def test_quotas_follow_the_counts_and_keep_a_place_for_the_frequent(self):
        counts = runner.Counter({"a": 600, "b": 300, "c": 60, "d": 3, "e": 2, "": 35})
        quota = runner._quotas(counts, 20, {"": 2})
        self.assertEqual(sum(quota.values()), 20)
        # 12, 6, 1.2 and 0.7 of twenty: the floor of two for "" is taken from the largest.
        self.assertEqual((quota["a"], quota["b"], quota["c"], quota[""]), (11, 6, 1, 2))
        self.assertNotIn("d", quota)
        self.assertEqual(quota[runner.RARE], 0)
        self.assertEqual(runner._quotas(runner.Counter(), 10), {})
        self.assertEqual(runner._quotas(counts, 20, {"missing": 3}).get("missing"), None)

    def test_the_seed_spreads_languages_and_typos_over_the_rows(self):
        languages = runner.Counter(item["language"] for item in self.sample)
        self.assertEqual(dict(languages), {"ka": 16, "ka-latin": 10, "ru": 8, "en": 6})
        self.assertEqual(sum(item["typos"] for item in self.sample), 12)
        self.assertEqual(len(runner._styles(7)), 7)
        again = runner.draw(self.index.rows(), 40, 7)
        self.assertEqual([(item["language"], item["typos"]) for item in again],
                         [(item["language"], item["typos"]) for item in self.sample])


def shown(sid, brand, number, cross="", name=PAD, vehicle="TOYOTA/LEXUS CAMRY 2006->", **extra):
    """A card as the engine sends it to the store."""
    return {"product": {"source_product_id": str(sid), "brand": brand, "original_code": number,
                        "duplicate_code": cross, "product_name": name, "product_model": vehicle,
                        "stock": {"store": 1.0, "tuz11": 0.0}},
            "availability_status": "IN_STORE", "confidence": "listed", "group": "g1",
            "reason": "", "evidence": "CAMRY 2006->", **extra}


WANTED = SimpleNamespace(id="77", name=PAD, vehicle="TOYOTA/LEXUS CAMRY 2006->", brand="SATO",
                         code="PN1521", cross_codes=("D1222-8331", "GDB3429", "KD2765"),
                         store=6.0, warehouse=0.0)


class ShownTests(unittest.TestCase):
    def test_the_row_itself_or_a_row_sharing_two_numbers_is_a_find(self):
        self.assertEqual(runner.shown_as(WANTED, [shown(1, "ABS", "37001"),
                                                  shown(77, "SATO", "PN1521")])["how"],
                         "the row itself")
        other_brand = runner.shown_as(WANTED, [shown(5, "ABS", "37677", "GDB3429/KD2765/D9")])
        self.assertEqual((other_brand["how"], other_brand["numbers"], other_brand["brand"]),
                         ("shared numbers", ["GDB3429", "KD2765"], "ABS"))
        # Its own number among another row's cross numbers counts like any other.
        self.assertIsNotNone(runner.shown_as(WANTED, [shown(6, "SB", "BP21521",
                                                            "pn-1521 / gdb.3429")]))
        self.assertIsNone(runner.shown_as(WANTED, [shown(7, "ABS", "37677", "GDB3429")]))
        self.assertIsNone(runner.shown_as(WANTED, []))
        self.assertIsNone(runner.shown_as(WANTED, [{"product": None}, "junk"]))

    def test_a_word_without_a_digit_is_not_a_number(self):
        oil = SimpleNamespace(id="9", code="5W30", cross_codes=("600044002", "HELIX ULTRA 5W30 1L"))
        self.assertEqual(runner.numbers_of(oil.code, oil.cross_codes), {"5W30", "600044002"})
        # Another oil of the same grade and brand line shares one number, not two.
        self.assertIsNone(runner.shown_as(oil, [shown(3, "SHELL", "5W30", "HELIX HX8 5W30 4L")]))
        self.assertEqual(runner.numbers_of("PN1521", "D1222/GDB3429"),
                         {"PN1521", "D1222", "GDB3429"})

    def test_a_written_message_may_not_leak_the_row_or_use_other_letters(self):
        problems = runner.request_problems
        self.assertEqual(problems(WANTED, "ka", "კამრი 2010 წინა ხუნდები მინდა"), [])
        self.assertEqual(problems(WANTED, "ka-latin", "camry 2010 wina xundebi minda"), [])
        self.assertIn("part number GDB3429", problems(WANTED, "en", "pads gdb-3429 camry")[0])
        self.assertEqual(problems(WANTED, "ru", "колодки Sato на камри"),
                         ["it names the brand of the part"])
        self.assertEqual(problems(WANTED, "ka", "camry 2010 front pads"),
                         ["it is not written as language ka asks"])
        self.assertEqual(problems(WANTED, "en", "კამრი 2010"),
                         ["it is not written as language en asks"])
        # What the row's own name or vehicle text states is what the product is called.
        belt = SimpleNamespace(id="3", name="ღვედი 6PK1054", vehicle="UNIVERSAL", brand="BOSCH",
                               code="6PK1054", cross_codes=("1987948452",))
        self.assertEqual(problems(belt, "en", "6pk1054 belt please"), [])
        maker = SimpleNamespace(id="4", name=PAD, vehicle="TOYOTA CAMRY", brand="TOYOTA",
                                code="04465-33450", cross_codes=())
        self.assertEqual(problems(maker, "en", "toyota camry front pads"), [])

    def test_the_customers_next_move_is_a_tap_a_text_or_nothing(self):
        asked = reply(question="წინა თუ უკანა?", options=["წინა", "უკანა"], outcome="asked")
        move = runner.next_move
        self.assertEqual(move({"action": "tap", "option": 2, "text": ""}, asked), ("უკანა", 2))
        self.assertEqual(move({"action": "tap", "option": 5, "text": "უკანა მინდა"}, asked),
                         ("უკანა მინდა", None))
        self.assertEqual(move({"action": "say", "option": 0, "text": " არ  ვიცი "}, asked),
                         ("არ ვიცი", None))
        self.assertIsNone(move({"action": "tap", "option": 1, "text": ""}, OFFER))
        self.assertIsNone(move({"action": "done", "option": 0, "text": "მადლობა"}, asked))
        self.assertIsNone(move({"action": "say", "option": 0, "text": ""}, asked))
        self.assertIsNone(move(None, asked))
        self.assertEqual(move({"action": "say", "option": True, "text": f"VIN {VIN}"}, asked),
                         ("VIN [VIN]", None))


class Customer:
    """The writer and the judge of the check, scripted."""

    def __init__(self, requests=None, replies=(), verdict=None, price=0.0):
        self.requests, self.replies = requests or {}, list(replies)
        self.verdict, self.price = verdict, price
        self.written, self.answered, self.cases = [], [], []

    def total(self):
        return self.price * (len(self.written) + len(self.answered) + len(self.cases))

    def spent(self):
        return {runner.WRITER_MODEL: self.total()} if self.price else {}

    def write(self, row, language, typos, reading=""):
        self.written.append((row.id, language, typos))
        if row.id in self.requests:
            return self.requests[row.id]
        return {"askable": True, "why_not": "", "message": f"ask {row.id}",
                "car": "Toyota Camry XV40", "year": 2010, "part": "front brake pads",
                "names": "model" if row.vehicle else "product", "language": language,
                "typos": typos, "problems": [], "reading": reading}

    def answer(self, row, request, turns, payload):
        self.answered.append((row.id, len(turns), payload.get("outcome")))
        return self.replies.pop(0) if self.replies else None

    def judge(self, case):
        self.cases.append(case)
        return self.verdict(case) if self.verdict else None


def item(row, language="ka", typos=False, **extra):
    return {"row": row, "make": runner.first_word(row.vehicle),
            "part": runner.opening_of(row.name), "language": language, "typos": typos, **extra}


ASKS_AXLE = reply("Camry 2010.", question="წინა თუ უკანა?", options=["წინა", "უკანა"],
                  outcome="asked", car_label="Toyota Camry 2010", diagnostics={
                      "tools": [{"name": "search_catalog", "rows": 2, "args": {
                          "make_terms": ["TOYOTA"], "vehicle_terms": ["CAMRY"],
                          "part_terms": ["ხუნდ"]}}], "notes": "Camry XV40 2010; pads"})
SHOWS_IT = reply(cards=[shown(77, "SATO", "PN1521")], car_label="Toyota Camry 2010")
SHOWS_ANOTHER = reply(cards=[shown(5, "ABS", "37001", "D999")], suggestions=["უკანა ხუნდები"],
                      car_label="Toyota Camry 2010")
NOTHING = reply("ვერ ვიპოვე.", suggestions=["VIN"], outcome="nothing_found")


class RowTests(unittest.TestCase):
    def run_row(self, answers, customer=None, **extra):
        store, customer = FakeStore(answers), customer or Customer()
        record = runner.run_row(store, customer, item(WANTED, **extra),
                                model="claude-sonnet-5-5", clock=Clock(), sleep=lambda _: None)
        return store, customer, record

    def test_a_row_shown_at_once_is_found_after_one_turn(self):
        store, customer, record = self.run_row({"ask 77": SHOWS_IT}, language="ru", typos=True)
        self.assertEqual((record["status"], record["found_at"], record["found_as"]["how"]),
                         ("found", 1, "the row itself"))
        self.assertEqual(customer.written, [("77", "ru", True)])
        self.assertEqual((customer.answered, store.sent, store.deleted),
                         ([], [("ask 77", {"language": "auto"})], ["c0"]))
        self.assertEqual((record["language"], record["typos"], record["make"], record["part"]),
                         ("ru", True, "toyota/lexus", "სამუხრუჭე ხუნდი"))
        turn = record["turns"][0]
        self.assertEqual((turn["say"], turn["seconds"], turn["cards"][0]["id"]),
                         ("ask 77", 1.0, "77"))
        self.assertAlmostEqual(record["cost_usd"], round(runner.cost(
            "claude-sonnet-5-5", SHOWS_IT["usage"]), 5))

    def test_the_customer_answers_a_question_by_tapping_and_is_found_on_the_second_turn(self):
        customer = Customer(replies=[{"action": "tap", "option": 1, "text": ""}])
        store, _, record = self.run_row({"ask 77": ASKS_AXLE, "წინა": SHOWS_IT}, customer)
        self.assertEqual(store.sent[1], ("წინა", {"language": "auto", "choice": 1}))
        self.assertEqual((record["status"], record["found_at"]), ("found", 2))
        self.assertEqual(customer.answered, [("77", 1, "asked")])
        self.assertEqual((record["turns"][0]["asks"], record["turns"][0]["options"],
                          record["turns"][1]["tapped"]), (True, ["წინა", "უკანა"], 1))

    def test_three_turns_without_the_row_are_a_miss(self):
        customer = Customer(replies=[{"action": "say", "option": 0, "text": "წინა მინდა"},
                                     {"action": "say", "option": 0, "text": "კიდევ"},
                                     {"action": "say", "option": 0, "text": "მეოთხე"}])
        store, _, record = self.run_row(
            {"ask 77": ASKS_AXLE, "წინა მინდა": SHOWS_ANOTHER, "კიდევ": NOTHING}, customer)
        self.assertEqual((record["status"], record["found_at"], record["ended"]),
                         ("missed", None, "3 turns used"))
        self.assertEqual(len(store.sent), 3)
        self.assertEqual(len(customer.answered), 2)  # Nobody is asked after the last turn.
        self.assertEqual(record["turns"][1]["cards"][0]["number"], "37001")

    def test_a_customer_with_nothing_to_add_ends_the_conversation(self):
        _, _, record = self.run_row({"ask 77": SHOWS_ANOTHER},
                                    Customer(replies=[{"action": "done", "option": 0,
                                                       "text": ""}]))
        self.assertEqual((record["status"], record["ended"], len(record["turns"])),
                         ("missed", "the customer had nothing to add", 1))

    def test_a_failed_turn_ends_it_and_is_charged_as_lost(self):
        late = reply("ბოდიში.", status="FAILED", suggestions=["ხელახლა ცდა"])
        _, customer, record = self.run_row({"ask 77": late})
        self.assertEqual((record["status"], record["turns"][0]["failed"]), ("missed", True))
        self.assertIn("turn 1 did not complete: FAILED/TIMEOUT", record["ended"])
        self.assertEqual(customer.answered, [])
        self.assertAlmostEqual(record["cost_usd"], round(runner.cost(
            "claude-sonnet-5-5", late["usage"]) + runner.cost(
            "claude-sonnet-5-5", runner.LOST_REQUEST), 5))

    def test_a_row_nobody_can_ask_for_is_set_aside_and_an_earlier_request_is_used_again(self):
        silent = {"askable": False, "why_not": "only a number tells what it is", "message": ""}
        store, _, record = self.run_row({}, Customer(requests={"77": silent}))
        self.assertEqual((record["status"], record["ended"], store.sent),
                         ("not_askable", "only a number tells what it is", []))
        store, _, record = self.run_row({}, Customer(requests={"77": None}))
        self.assertEqual((record["status"], store.sent), ("not_written", []))
        kept = {"askable": True, "message": "как тогда", "car": "Camry", "year": 0,
                "part": "pads"}
        store, customer, record = self.run_row({"как тогда": SHOWS_IT}, request=kept)
        self.assertEqual((customer.written, record["status"], record["request"]),
                         ([], "found", kept))
        # What a specialist read in the row's name goes to the writer with the row.
        _, _, record = self.run_row({"ask 77": SHOWS_IT}, reading="front brake pad set")
        self.assertEqual(record["request"]["reading"], "front brake pad set")


class MissTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [stock_row(77, "TOYOTA/LEXUS CAMRY 2006->", PAD, code="PN1521",
                          cross="D1222/GDB3429"),
                stock_row(78, "TOYOTA/LEXUS CAMRY 2006->", "სამუხრუჭე ხუნდი უკ", store="0")]
        rows += [stock_row(100 + number, "TOYOTA/LEXUS COROLLA 2008>", PAD)
                 for number in range(35)]
        rows.append(stock_row(900, "TOYOTA/LEXUS CAMRY 2017>", PAD))
        cls.index = CatalogIndex.from_rows(rows)
        cls.row = cls.index.row("77")

    def call(self, make=(), vehicle=(), part=(), rows=0):
        return {"name": "search_catalog", "rows": rows, "ms": 1, "args": {
            "make_terms": list(make), "vehicle_terms": list(vehicle),
            "part_terms": list(part)}}

    def trace(self, call, row=None):
        row = row or self.row
        return runner.search_trace(self.index, CatalogIndex([row]), row, call)["row"]

    def test_a_search_run_again_says_where_the_row_was(self):
        self.assertEqual(self.trace(self.call(["TOYOTA"], ["CAMRY"], ["ხუნდ"])),
                         "returned at place 1")
        self.assertEqual(self.trace(self.call(["TOYOTA"], ["CAMRY"], ["კალოტკ"])),
                         "not matched by part_terms")
        self.assertEqual(self.trace(self.call(["HONDA"], ["CAMRY"], ["კალოტკ"])),
                         "not matched by make_terms, part_terms")
        # 38 rows match make and part, the result holds 30, and only COROLLA rows are in it.
        self.assertTrue(self.trace(self.call(["TOYOTA"], ["COROLLA"], ["ხუნდ"])).startswith(
            "matches the make and part terms but no vehicle term"))
        # Without a vehicle term the result is the first 30 of them, and the last is cut.
        last = self.index.row("900")
        self.assertEqual(self.trace(self.call(["TOYOTA"], [], ["ხუნდ"])), "returned at place 1")
        self.assertTrue(self.trace(self.call(["TOYOTA"], [], ["ხუნდ"]), last).startswith(
            "matched, but past the 30 rows"))
        self.assertEqual(self.trace(self.call(["TOYOTA"], ["CAMRY"], ["ხუნდ"]), last),
                         "returned at place 2")
        # A search the tool refused returned nothing at all.
        self.assertTrue(self.trace(self.call(["TOYOTA"], ["E"], ["ხუნდ"])).startswith(
            "the search was refused: `vehicle_terms` contains 'E' (1 letter)"))
        lookup = {"name": "lookup_codes", "rows": 1, "args": {"numbers": ["GDB3429"],
                                                              "vehicle_hint": ""}}
        self.assertEqual(self.trace(lookup), "returned")
        self.assertEqual(self.trace({"name": "lookup_codes", "args": {"numbers": ["X9999"]}}),
                         "not returned")
        other = runner.search_trace(self.index, None, self.row, {
            "name": "oil_guide", "rows": 0, "args": {"make": "Toyota", "year": 0, "trim": ""}})
        self.assertEqual((other.get("row"), other["args"]), (None, {"make": "Toyota"}))
        returned = runner.searched_rows(self.index, [self.call(["TOYOTA"], ["CAMRY"], ["ხუნდ"]),
                                                     lookup, {"name": "finish"}, "junk"])
        # The three CAMRY rows, those in stock first; the looked-up row is not listed twice.
        self.assertEqual([row.id for row in returned], ["77", "900", "78"])

    def record(self, *turns):
        return {"id": "77", "status": "missed", "found_at": None, "ended": "3 turns used",
                "turns": [{"tools": tools, "cards": cards, "asks": asking, "failed": failed,
                           "outcome": outcome} for tools, cards, asking, failed, outcome in turns]}

    def test_a_miss_is_explained_by_its_most_telling_search(self):
        seen = self.record(([self.call(["TOYOTA"], ["CAMRY"], ["კალოტკ"]),
                             self.call(["TOYOTA"], ["CAMRY"], ["ხუნდ"])], [], True, False,
                            "asked"),
                           ([], [{"id": "5"}], False, False, "offered"))
        runner.explain(self.index, self.row, seen)
        self.assertEqual((seen["cause"], seen["ending"]),
                         ("the row was returned to the model and not offered",
                          "other rows were offered"))
        self.assertEqual([search["row"] for search in seen["turns"][0]["searches"]],
                         ["not matched by part_terms", "returned at place 1"])
        wording = self.record(([self.call(["TOYOTA"], ["CAMRY"], ["კალოტკ"])], [], False, False,
                               "nothing_found"))
        runner.explain(self.index, self.row, wording)
        self.assertEqual((wording["cause"], wording["ending"]),
                         ("no part term of a search matched the row's name",
                          "it said nothing found"))
        silent = self.record(([], [], True, False, "asked"))
        runner.explain(self.index, self.row, silent)
        self.assertEqual((silent["cause"], silent["ending"]),
                         (runner.NO_SEARCH, "it was still asking"))
        broken = self.record(([], [], False, True, "handoff"))
        runner.explain(self.index, self.row, broken)
        self.assertEqual(broken["ending"], "a turn failed")


class CatalogueRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = CatalogIndex.from_rows([
            stock_row(77, "TOYOTA/LEXUS CAMRY 2006->", PAD, code="PN1521", cross="D1222/GDB3429"),
            stock_row(80, "MB W204,W211,W212", LINK, brand="JIKIU", code="LS31001"),
            stock_row(81, "MB W204,W211,W212", PAD, code="PN0395W"),
            stock_row(82, "", "ჰაერის ფილტრი", brand="", code="TKS-B155")])

    def sample(self):
        return [item(self.index.row("77")), item(self.index.row("80"), "ru"),
                item(self.index.row("81"), "ru", True), item(self.index.row("82"), "en")]

    def run_all(self, **kwargs):
        answers = {"ask 77": ASKS_AXLE, "წინა": reply(cards=[shown(77, "SATO", "PN1521")]),
                   "ask 80": SHOWS_ANOTHER, "ask 81": reply(cards=[shown(81, "SATO", "PN0395W")])}
        customer = Customer(
            requests={"82": {"askable": False, "why_not": "no car, no size", "message": ""}},
            replies=[{"action": "tap", "option": 1, "text": ""}], **kwargs.pop("customer", {}))
        out = io.StringIO()
        records, spent = runner.check_catalogue(
            FakeStore(answers), customer, self.sample(), model="claude-sonnet-5-5", out=out,
            index=self.index, clock=Clock(), sleep=lambda _: None, **kwargs)
        return records, spent, out.getvalue(), customer

    def test_rows_are_run_and_counted_after_one_two_and_three_turns(self):
        records, spent, printed, _ = self.run_all()
        self.assertEqual([(record["id"], record["status"], record["found_at"])
                          for record in records],
                         [("77", "found", 2), ("80", "missed", None), ("81", "found", 1),
                          ("82", "not_askable", None)])
        self.assertAlmostEqual(spent, sum(record["cost_usd"] for record in records))
        lines = printed.splitlines()
        self.assertTrue(lines[0].startswith("found@2 77 [ka] TOYOTA/LEXUS CAMRY 2006-> | "))
        self.assertIn(":: ask 77 (2 turns, 2.0s, $", lines[0])
        self.assertTrue(lines[1].startswith("MISS    80 [ru] MB W204,W211,W212"))
        self.assertEqual(lines[2].strip(), "other rows were offered; " + runner.NO_SEARCH)
        self.assertTrue(lines[4].startswith("not_askable 82 [en] - | ჰაერის ფილტრი"))

        summary = runner.summarize_catalogue(records)
        self.assertEqual(summary["rows"], {"sampled": 4, "asked": 3, "found": 2, "missed": 1,
                                           "not_askable": 1, "not_written": 0, "not_run": 0,
                                           "error": 0})
        self.assertEqual(summary["findability"], {"rows": 3, "found": [1, 2, 2],
                                                  "rate": [0.333, 0.667, 0.667]})
        self.assertEqual(summary["by_language"], [
            {"name": "ru", "rows": 2, "found": [1, 1, 1], "rate": [0.5, 0.5, 0.5]},
            {"name": "ka", "rows": 1, "found": [0, 1, 1], "rate": [0.0, 1.0, 1.0]}])
        self.assertEqual([(group["name"], group["found"]) for group in summary["by_make"]],
                         [("mb", [1, 1, 1]), ("toyota/lexus", [0, 1, 1])])
        self.assertEqual([(group["name"], group["rows"]) for group in summary["by_request"]],
                         [("model", 3)])
        self.assertEqual({group["name"]: group["rate"][-1] for group in summary["by_part"]},
                         {"სამუხრუჭე ხუნდი": 1.0, "სტაბ კრონშტეინი": 0.0})
        self.assertEqual(summary["miss_causes"], {runner.NO_SEARCH: 1})
        self.assertEqual(summary["miss_endings"], {"other rows were offered": 1})
        self.assertEqual((summary["turns"], summary["questions"], summary["failed_turns"],
                          summary["questions_without_options"]), (4, 1, 0, 0))
        self.assertEqual(summary["seconds"]["median"], 1.0)
        self.assertAlmostEqual(summary["cost_per_turn_usd"], round(spent / 4, 5))
        self.assertIsNone(summary["offers"])

        text = io.StringIO()
        runner.print_catalogue(summary, records, text, "claude-sonnet-5-5")
        report = text.getvalue()
        self.assertIn("4 rows sampled, 3 asked (1 cannot be asked for in words", report)
        self.assertIn("1/3 (33%)  2/3 (67%)  2/3 (67%)", report)
        self.assertIn("MISS 80 [ru] MB W204,W211,W212 | სტაბ კრონშტეინი წნ | JIKIU LS31001",
                      report)
        self.assertIn("t1 customer: ask 80", report)
        self.assertIn("1 cards: ABS 37001 (TOYOTA/LEXUS CAMRY 2006->; listed)", report)

    def test_the_weakest_groups_need_enough_rows_to_be_named(self):
        groups = [{"name": "a", "rows": 10, "found": [5, 6, 6], "rate": [0.5, 0.6, 0.6]},
                  {"name": "b", "rows": 2, "found": [0, 0, 0], "rate": [0.0, 0.0, 0.0]},
                  {"name": "c", "rows": 4, "found": [1, 1, 2], "rate": [0.25, 0.25, 0.5]},
                  {"name": "d", "rows": 3, "found": [3, 3, 3], "rate": [1.0, 1.0, 1.0]},
                  {"name": "e", "rows": 5, "found": [0, 2, 3], "rate": [0.0, 0.4, 0.6]}]
        self.assertEqual([group["name"] for group in runner.weakest(groups)], ["c", "e", "a"])
        self.assertEqual(runner.found_by_turn([])["rate"], [None, None, None])

    def test_no_row_is_started_once_the_money_is_gone_and_a_broken_row_does_not_end_the_run(self):
        records, spent, _, customer = self.run_all(budget_left=0.0)
        self.assertEqual(({record["status"] for record in records}, spent, customer.written),
                         ({"not_run"}, 0.0, []))
        # What the writer costs counts against the same budget.
        records, _, _, _ = self.run_all(budget_left=0.5, customer={"price": 0.3})
        self.assertEqual([record["status"] for record in records],
                         ["found", "not_run", "not_run", "not_run"])
        out = io.StringIO()
        records, _ = runner.check_catalogue(
            FakeStore({}), Customer(), self.sample()[:1], model="claude-haiku-5-5", out=out)
        self.assertEqual((records[0]["status"], records[0]["ended"]),
                         ("error", "runner error: KeyError"))
        side_by_side, _ = runner.check_catalogue(
            FakeStore({"ask 77": SHOWS_IT, "ask 81": SHOWS_ANOTHER}), Customer(),
            [self.sample()[0], self.sample()[2]], model="claude-haiku-5-5", out=io.StringIO(),
            parallel=2, sleep=lambda _: None)
        self.assertEqual([record["id"] for record in side_by_side], ["77", "81"])

    def test_an_earlier_reports_rows_come_back_with_their_requests(self):
        records, _, _, _ = self.run_all()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            gone = {**records[1], "id": "9999"}
            path.write_text(json.dumps({"kind": "catalogue", "rows": [*records, gone]},
                                       ensure_ascii=False), encoding="utf-8")
            earlier = runner.earlier_rows(path)
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                runner.earlier_rows(path)
        missed = runner.earlier_sample(self.index, earlier, "misses")
        self.assertEqual([(entry["row"].id, entry["language"], entry["request"]["message"])
                          for entry in missed], [("80", "ru", "ask 80")])
        self.assertEqual([entry["row"].id for entry in runner.earlier_sample(
            self.index, earlier, " 81 , 77 ,404")], ["81", "77"])
        # All of them, without the row that has left the catalogue.
        self.assertEqual([entry["row"].id for entry in runner.earlier_sample(
            self.index, earlier, None)], ["77", "80", "81", "82"])


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [stock_row(77, "TOYOTA/LEXUS CAMRY 2006->", PAD, code="PN1521",
                          cross="D1222/GDB3429"),
                stock_row(79, "TOYOTA/LEXUS CAMRY 2017>", PAD, code="PN1857", store="0"),
                stock_row(83, "TOYOTA/LEXUS CAMRY 2006->", PAD, brand="ABS", code="37677",
                          cross="GDB3429/D1222"),
                stock_row(84, "TOYOTA/LEXUS CAMRY 01->06", PAD, code="PN1479")]
        rows += [stock_row(200 + number, "TOYOTA/LEXUS CAMRY 2006->", PAD, store="0")
                 for number in range(50)]
        cls.index = CatalogIndex.from_rows(rows)

    def record(self):
        search = {"name": "search_catalog", "rows": 30, "args": {
            "make_terms": ["TOYOTA"], "vehicle_terms": ["CAMRY"], "part_terms": ["ხუნდ"]}}
        offer = reply("Camry 2010-ის წინა ხუნდები.", car_label="Toyota Camry 2010", cards=[
            shown(84, "SATO", "PN1479", vehicle="TOYOTA/LEXUS CAMRY 01->06", confidence="check",
                  reason="წელი გადაამოწმეთ", evidence="CAMRY"),
            shown(83, "ABS", "37677", "GDB3429/D1222")],
            groups=[{"id": "g1", "label": "წინა ხუნდები", "count": 2}],
            diagnostics={"tools": [], "notes": "Camry XV40 2010; front pads"})
        asked = {**ASKS_AXLE, "diagnostics": {"tools": [search], "notes": ""}}
        customer = Customer(replies=[{"action": "tap", "option": 1, "text": ""}])
        return runner.run_row(FakeStore({"ask 77": asked, "წინა": offer}), customer,
                              item(self.index.row("77")), model="claude-sonnet-5-5",
                              clock=Clock(), sleep=lambda _: None)

    def test_the_judge_reads_the_offer_the_engines_judgement_and_other_rows(self):
        record = self.record()
        self.assertEqual((record["status"], record["found_at"]), ("found", 2))
        case, offered, others = runner.judge_case(self.index, record, 2)
        self.assertEqual(case["conversation"], [
            {"customer": "ask 77", "shop": "Camry 2010."},
            {"customer": "წინა", "shop": "Camry 2010-ის წინა ხუნდები."}])
        self.assertEqual((case["car_as_the_assistant_understood_it"], case["assistant_notes"]),
                         ("Toyota Camry 2010", "Camry XV40 2010; front pads"))
        self.assertEqual(case["offered"][0], {
            "id": "o1", "name": PAD, "vehicle": "TOYOTA/LEXUS CAMRY 01->06", "brand": "SATO",
            "number": "PN1479", "cross_numbers": "", "heading": "წინა ხუნდები",
            "confidence": "check", "evidence": "CAMRY", "reason": "წელი გადაამოწმეთ"})
        # The other rows are what the conversation's searches returned and the reply left
        # out: in stock first, at most forty, and never a hint which row was wanted.
        self.assertEqual(len(others), 28)
        self.assertEqual((others[0].id, case["other_rows"][0]["id"],
                          case["other_rows"][0]["number"], case["other_rows"][0]["in_stock"]),
                         ("77", "c1", "PN1521", True))
        self.assertFalse(case["other_rows"][1]["in_stock"])
        self.assertNotIn("84", [row.id for row in others])
        self.assertNotIn("wanted", json.dumps(case))
        self.assertEqual(len(offered), 2)

    def test_marks_become_a_wrong_offer_rate_and_a_sheet_without_a_vin(self):
        def verdict(case):
            return {"offers": [{"id": "o1", "verdict": "wrong", "issue": "generation_or_year",
                                "note": "Ends in 2006; the car is a 2010."},
                               {"id": "o2", "verdict": "fits", "issue": "none",
                                "note": "Same numbers as the 2006-> pad."},
                               {"id": "o9", "verdict": "fits", "issue": "none", "note": "?"}],
                    "missed": [{"id": "c1", "note": "In stock and listed for 2006->."},
                               {"id": "c99", "note": "no such row"}, {"id": "o1", "note": "x"}]}

        record = self.record()
        customer = Customer(verdict=verdict)
        out = io.StringIO()
        judged = runner.review(customer, self.index, [record], size=5, seed=1, out=out)
        self.assertEqual(len(judged), 1)  # Only the turn that showed cards is read.
        entry = judged[0]
        self.assertEqual((entry["row_id"], entry["turn"], entry["request"], entry["car"]),
                         ("77", 2, "ask 77 / წინა", "Toyota Camry 2010"))
        self.assertEqual([(offer["number"], offer["verdict"], offer["issue"], offer["wanted"])
                          for offer in entry["offers"]],
                         [("PN1479", "wrong", "generation_or_year", False),
                          ("37677", "fits", "none", True)])
        self.assertEqual([(row["id"], row["number"]) for row in entry["missed"]],
                         [("77", "PN1521")])
        self.assertIn("judged 77 t2: 1 fits, 1 wrong, 1 better rows missed", out.getvalue())

        summary = runner.offer_summary(judged)
        self.assertEqual((summary["turns"], summary["rows"], summary["fits"],
                          summary["plausible"], summary["wrong"], summary["wrong_rate"]),
                         (1, 2, 1, 0, 1, 0.5))
        self.assertEqual(summary["by_confidence"], {
            "check": {"fits": 0, "plausible": 0, "wrong": 1},
            "listed": {"fits": 1, "plausible": 0, "wrong": 0}})
        self.assertEqual((summary["wrong_by_issue"], summary["turns_with_a_wrong_row"],
                          summary["turns_with_better_rows_missed"],
                          summary["turns_with_stocked_rows_missed"],
                          summary["wanted_rows_marked_wrong"]),
                         ({"generation_or_year": 1}, 1, 1, 1, 0))
        # The wrong row was another row than the wanted one, offered as check.
        self.assertEqual(summary["wrong_apart"], {
            "the_wanted_row": 0, "offered_as_check": 1, "offered_as_spec": 0,
            "offered_as_fitting": 0, "rows_offered_as_fitting": 1,
            "wrong_as_fitting_rate": 0.0})
        whole = runner.summarize_catalogue([record], judged)
        printed = io.StringIO()
        runner.print_catalogue(whole, [record], printed)
        self.assertIn("2 offered rows in 1 turns: 1 fit, 0 plausible, 1 wrong (50.0%)",
                      printed.getvalue())
        self.assertIn("1 turns left out a row in stock that fits as well", printed.getvalue())
        self.assertIn("the wrong rows apart: 0 are the row the request was written from (the "
                      "judge against the catalogue), 1 other rows were offered as check, 0 as "
                      "spec, 0 as listed or confirmed, of 1 rows offered so (0.0%)",
                      printed.getvalue())

        entry["request"] += f" {VIN}"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "judged.csv"
            self.assertEqual(runner.write_csv(path, judged), 2)
            text = path.read_text(encoding="utf-8")
            with path.open(newline="", encoding="utf-8") as handle:
                lines = list(csv.reader(handle))
        self.assertEqual(lines[0][:8], ["request", "car", "brand", "number",
                                        "catalogue vehicle text", "confidence", "verdict",
                                        "judge note"])
        self.assertEqual(lines[1], ["ask 77 / წინა [VIN]", "Toyota Camry 2010", "SATO", "PN1479",
                                    "TOYOTA/LEXUS CAMRY 01->06", "check", "wrong",
                                    "Ends in 2006; the car is a 2010.", PAD, ""])
        # The sheet says which offered row is the row the request was written from.
        self.assertEqual((lines[0][9], lines[2][3], lines[2][9]),
                         ("the row the request was written from", "37677", "yes"))
        self.assertNotIn(VIN, text)

    def test_the_wrong_rows_are_told_apart_by_what_the_mark_measures(self):
        def offer(verdict, confidence, wanted=False):
            return {"verdict": verdict, "confidence": confidence, "wanted": wanted}

        apart = runner.wrong_apart([
            offer("wrong", "listed", wanted=True),  # the judge against the catalogue
            offer("wrong", "check", wanted=True),
            offer("wrong", "check"), offer("wrong", "check"), offer("wrong", "spec"),
            offer("wrong", "listed"), offer("wrong", "confirmed"),
            offer("fits", "listed"), offer("plausible", "confirmed"), offer("fits", "check")])
        self.assertEqual(apart, {
            "the_wanted_row": 2, "offered_as_check": 2, "offered_as_spec": 1,
            "offered_as_fitting": 2, "rows_offered_as_fitting": 5,
            "wrong_as_fitting_rate": 0.4})
        self.assertEqual(runner.wrong_apart([offer("wrong", "check")])[
            "wrong_as_fitting_rate"], None)

    def test_a_judge_that_says_nothing_leaves_the_rows_unjudged_and_the_budget_ends_it(self):
        record = self.record()
        judged = runner.review(Customer(), self.index, [record], size=5, seed=1,
                               out=io.StringIO())
        self.assertEqual([offer["verdict"] for offer in judged[0]["offers"]],
                         ["unjudged", "unjudged"])
        summary = runner.offer_summary(judged)
        self.assertEqual((summary["rows"], summary["unjudged_rows"], summary["wrong_rate"]),
                         (0, 2, None))
        self.assertEqual(runner.review(Customer(price=1.0), self.index, [record], size=5,
                                       seed=1, out=io.StringIO(), budget_left=0.0), [])
        # The sample of turns is settled by the seed.
        records = [{"id": str(number), "turns": [{"cards": [1]}, {"cards": []}, {"cards": [2]}]}
                   for number in range(20)]
        first = [(entry["id"], turn) for entry, turn in runner.card_turns(records, 6, 3)]
        self.assertEqual(len(first), 6)
        self.assertEqual(first, [(entry["id"], turn)
                                 for entry, turn in runner.card_turns(records[::-1], 6, 3)])
        self.assertNotEqual(first, [(entry["id"], turn)
                                    for entry, turn in runner.card_turns(records, 6, 4)])
        self.assertTrue(all(turn in (1, 3) for _, turn in first))
        self.assertEqual(len(runner.card_turns(records, 500, 3)), 40)


class Messages:
    """An Anthropic client that answers from a list and keeps what it was asked."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text, stop = answer if isinstance(answer, tuple) else (answer, "end_turn")
        return SimpleNamespace(
            stop_reason=stop, content=[SimpleNamespace(type="thinking", thinking=""),
                                       SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))


def bad_request(message):
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(message, response=httpx.Response(400, request=request),
                                            body=None)


class ModelsTests(unittest.TestCase):
    WRITTEN = json.dumps({"askable": True, "why_not": "", "message": " კამრი 2010,  წინა ხუნდები ",
                          "car": "Toyota Camry XV40", "year": 2010, "part": "front brake pads",
                          "names": "model"}, ensure_ascii=False)

    def test_part_names_are_read_by_the_stronger_model_from_the_rows_that_carry_them(self):
        index = CatalogIndex.from_rows([
            stock_row(1, "TOYOTA/LEXUS CAMRY 2006->", LINK, brand="JIKIU", code="LS21340",
                      cross="48820-06050"),
            stock_row(2, "MB W204", LINK), stock_row(3, "HONDA FIT", LINK),
            stock_row(4, "BMW F10", LINK), stock_row(5, "MB W212", PAD)])
        sample = [item(index.row("3")), item(index.row("5"))]
        examples = runner.name_examples(sample, index.rows())
        # The sampled row first, then others of the catalogue: three rows show a name.
        self.assertEqual({name: [row.id for row in rows] for name, rows in examples.items()},
                         {LINK: ["3", "1", "2"], PAD: ["5"]})
        self.assertEqual(runner.name_examples([], index.rows()), {})
        client = Messages(json.dumps({"readings": [
            {"id": "n1", "part": "front brake pad set"},
            {"id": "n2", "part": " front stabiliser  link "},
            {"id": "n7", "part": "no such name"}, {"id": "n1x", "part": "junk"}]}))
        models = runner.Models(client)
        self.assertEqual(models.read_names(examples),
                         {PAD: "front brake pad set", LINK: "front stabiliser link"})
        sent = client.requests[0]
        self.assertEqual((sent["model"], sent["system"]),
                         ("claude-opus-5-5", runner.READER_SYSTEM))
        case = json.loads(sent["messages"][0]["content"])
        self.assertEqual([entry["name"] for entry in case["names"]], [PAD, LINK])
        self.assertEqual(case["names"][1]["example_rows"][1], {
            "vehicle": "TOYOTA/LEXUS CAMRY 2006->", "brand": "JIKIU", "number": "LS21340",
            "cross_numbers": "48820-06050"})
        self.assertEqual(runner.Models(Messages("no json")).read_names(examples), {})
        self.assertEqual(runner.Models(Messages()).read_names({}), {})

    def test_the_writer_is_asked_for_one_json_object_and_never_sees_the_stock(self):
        client = Messages(self.WRITTEN, self.WRITTEN)
        models = runner.Models(client)
        request = models.write(WANTED, "ka", True)
        self.assertEqual(request, {
            "askable": True, "why_not": "", "message": "კამრი 2010, წინა ხუნდები",
            "car": "Toyota Camry XV40", "year": 2010, "part": "front brake pads",
            "names": "model", "language": "ka", "typos": True, "problems": [], "reading": ""})
        read = models.write(WANTED, "ka", True, "front brake pad set")
        self.assertEqual(read["reading"], "front brake pad set")
        self.assertEqual(json.loads(client.requests[1]["messages"][0]["content"])[
            "the_part_is"], "front brake pad set")
        models = runner.Models(client)
        client.answers.append(self.WRITTEN)
        request = models.write(WANTED, "ka", True)
        client.requests[:] = client.requests[-1:]
        sent = client.requests[0]
        self.assertEqual((sent["model"], sent["system"]),
                         ("claude-haiku-5-5", runner.WRITER_SYSTEM))
        self.assertEqual(sent["extra_body"]["output_config"], {
            "effort": "low", "format": {"type": "json_schema",
                                        "schema": runner.REQUEST_SCHEMA}})
        self.assertNotIn("temperature", sent)
        case = json.loads(sent["messages"][0]["content"])
        self.assertEqual((case["language"], case["typos"], case["row"]),
                         ("ka", True, {"name": PAD, "vehicle": "TOYOTA/LEXUS CAMRY 2006->",
                                       "brand": "SATO", "number": "PN1521",
                                       "cross_numbers": "D1222-8331/GDB3429/KD2765"}))
        self.assertAlmostEqual(models.total(), runner.cost(
            "claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 200}))
        self.assertEqual(list(models.spent()), ["claude-haiku-5-5"])

    def test_a_message_that_leaks_the_row_is_written_once_more(self):
        leaking = json.dumps({"askable": True, "why_not": "", "message": "pads GDB3429 camry",
                              "car": "", "year": 0, "part": "pads"})
        client = Messages(leaking, self.WRITTEN)
        self.assertEqual(runner.Models(client).write(WANTED, "ka", False)["problems"], [])
        again = json.loads(client.requests[1]["messages"][0]["content"])
        self.assertEqual(again["your_last_message"], "pads GDB3429 camry")
        self.assertEqual(len(again["what_is_wrong_with_it"]), 2)
        # What is still wrong after the second try is kept with the request.
        kept = runner.Models(Messages(leaking, leaking)).write(WANTED, "en", False)
        self.assertEqual(kept["problems"], ["it contains the part number GDB3429"])
        unaskable = json.dumps({"askable": False, "why_not": "no car", "message": "",
                                "car": "", "year": 0, "part": ""})
        self.assertEqual(runner.Models(Messages(unaskable)).write(WANTED, "en", False)[
            "askable"], False)

    def test_a_model_that_refuses_the_format_is_asked_in_plain_words_and_failures_are_none(self):
        client = Messages(bad_request("output_config.format is not supported"),
                          "Here it is:\n" + self.WRITTEN + "\nDone.", self.WRITTEN)
        models = runner.Models(client)
        self.assertEqual(models.write(WANTED, "ka", False)["year"], 2010)
        self.assertNotIn("format", client.requests[1]["extra_body"]["output_config"])
        self.assertIn("Reply with one JSON object", client.requests[1]["system"])
        models.write(WANTED, "ka", False)
        self.assertNotIn("format", client.requests[2]["extra_body"]["output_config"])
        import httpx

        down = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        for answers in ([down], [("", "refusal")], ["no json here"], ["[1, 2]"],
                        [bad_request("a"), bad_request("b")]):
            with self.subTest(answers=answers):
                self.assertIsNone(runner.Models(Messages(*answers)).write(WANTED, "ka", False))

    def test_the_customer_answers_from_the_row_and_the_judge_is_the_stronger_model(self):
        client = Messages(json.dumps({"action": "tap", "option": 1, "text": ""}),
                          json.dumps({"offers": [], "missed": []}))
        models = runner.Models(client)
        request = {"car": "Toyota Camry XV40", "year": 2010, "part": "front pads",
                   "language": "ka", "message": "ask 77"}
        turns = [{"say": "ask 77", "answer": "Camry 2010.\nწინა თუ უკანა?"}]
        self.assertEqual(models.answer(WANTED, request, turns, {**ASKS_AXLE, "cards": [
            shown(5, "ABS", "37001")], "groups": [{"id": "g1", "label": "ხუნდები"}]}),
            {"action": "tap", "option": 1, "text": ""})
        case = json.loads(client.requests[0]["messages"][0]["content"])
        self.assertEqual((case["assistant_question"], case["answer_buttons"],
                          case["you_asked_for"]["year"], case["conversation"]),
                         ("წინა თუ უკანა?", ["1. წინა", "2. უკანა"], 2010,
                          [{"you": "ask 77", "assistant": "Camry 2010.\nწინა თუ უკანა?"}]))
        # The customer sees a card as a visitor does: no number, no brand.
        self.assertEqual(case["parts_shown"], [{
            "heading": "ხუნდები", "name": PAD, "fits": "TOYOTA/LEXUS CAMRY 2006->",
            "label": "listed", "note": ""}])
        self.assertEqual(client.requests[0]["system"], runner.ANSWER_SYSTEM)
        self.assertEqual(models.judge({"offered": []}), {"offers": [], "missed": []})
        judge = client.requests[1]
        self.assertEqual((judge["model"], judge["extra_body"]["output_config"]["effort"]),
                         ("claude-opus-5-5", "medium"))
        self.assertNotIn("tools", judge)
        self.assertEqual(set(models.spent()), {"claude-haiku-5-5", "claude-opus-5-5"})


if __name__ == "__main__":
    unittest.main()
