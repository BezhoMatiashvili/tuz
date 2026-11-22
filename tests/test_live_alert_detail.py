"""Why the bot found nothing, for the staff (dashboard > Live chats).

What the engine records of each search (``live_alerts.search_facts``), what an alarm keeps of the
turn that raised it (``turn_detail``), and what the Messenger adapter keeps of a turn that failed
(``messenger_engine._cause``). A scripted model and the catalogue fixture of
tests/test_assistant.py: no network, no provider, $0.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from unittest.mock import patch

from automotive.live_alerts import (
    DETAIL_BYTES,
    MAX_SEARCHES,
    AlertDraft,
    search_facts,
    turn_alerts,
    turn_detail,
)
from automotive.live_web import LiveRecorder
from automotive.messenger_engine import _cause
from automotive.site_chat import public_turn
from tests.test_assistant import Script, done, run, says, search, service, use
from tests.test_assistant_staff import as_payload, public, staff
from tests.test_live_web import FakeStore, StoreCase, wait_for
from tests.test_messenger_worker import PSID, WorkerCase, failure
from tests.test_site_live import LiveCase, alert, chat

PADS = "ხუნდი წნ"


def catalogue_args(make=("TOYOTA",), vehicle=("CAMRY",), part=("სანთელი",), stock=False):
    return {"make_terms": list(make), "vehicle_terms": list(vehicle), "part_terms": list(part),
            "exclude_terms": [], "in_stock_only": stock, "limit": 20}


def size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


class SearchFactsTests(unittest.TestCase):
    def test_a_catalogue_search_keeps_its_words_and_the_counts_the_catalogue_gave(self):
        answer = {"columns": ["id"], "rows": [["r1"], ["r2"]], "total": 31, "available": 12,
                  "matched_vehicle": 0, "why": {}, "note": "not kept",
                  "other_vehicle_texts": [["COROLLA 2007-2013", 8, 3], ["RAV4", 5, 1],
                                          ["YARIS", 3, 0], ["AURIS", 2, 2]],
                  "similar_spellings": [{"term": "CAMRI", "catalogue_token": "CAMRY",
                                         "rows": 40}],
                  "unmatched_terms": ["свеча"], "rows_under_similar_spellings": [["r3"]],
                  "in_stock_under_other_cars": [["r4"], ["r5"]]}
        self.assertEqual(search_facts("search_catalog", catalogue_args(), answer), {
            "part": ["სანთელი"], "car": ["CAMRY"], "make": ["TOYOTA"], "total": 31,
            "available": 12, "rows": 2, "car_rows": 0, "unmatched": ["свеча"],
            "cars": [["COROLLA 2007-2013", 8, 3], ["RAV4", 5, 1], ["YARIS", 3, 0]],
            "spellings": [["CAMRI", "CAMRY", 40]], "near": 1, "elsewhere": 2})

    def test_when_every_row_was_shown_and_none_names_the_car_their_own_cars_are_counted(self):
        columns = ["id", "name", "vehicle", "brand", "code", "cross_codes", "price", "store",
                   "warehouse", "same_part"]
        rows = [["r1", "სანთელი", "UNIVERSAL", "NGK", "1", "", 9, 0, 0, "a"],
                ["r2", "სანთელი", "TOYOTA/LEXUS PRIUS", "DENSO", "2", "", 9, 1, 0, "a"],
                ["r3", "სანთელი", "UNIVERSAL", "NGK", "3", "", 9, 0, 2, "b"],
                ["r4", "სანთელი", "", "BOSCH", "4", "", 9, 0, 0, "c"],
                ["r5", "სანთელი", "TOYOTA/LEXUS PRIUS", "DENSO", "5", "", 9, 0, 0, "a"],
                ["r6", "სანთელი", "AURIS", "DENSO", "6", "", 9, 0, 0, "a"],
                ["r7", "სანთელი", "UNIVERSAL", "NGK", "7", "", 9, None, "?", "a"]]
        answer = {"columns": columns, "rows": rows, "total": 7, "available": 2,
                  "matched_vehicle": 0, "why": {}}
        facts = search_facts("search_catalog", catalogue_args(), answer)
        self.assertEqual(facts["cars"], [["UNIVERSAL", 3, 1], ["TOYOTA/LEXUS PRIUS", 2, 1],
                                         ["AURIS", 1, 0]])
        # Rows that name the car need no such count; neither does a search without car words.
        found = search_facts("search_catalog", catalogue_args(), {**answer, "matched_vehicle": 2})
        self.assertNotIn("cars", found)
        self.assertNotIn("cars", search_facts("search_catalog", catalogue_args(vehicle=()),
                                              answer))

    def test_a_search_with_no_row_keeps_what_each_group_matched_alone(self):
        answer = {"rows": [], "total": 0, "available": 0, "matched_vehicle": 0,
                  "why": {"make_terms_alone": 4012, "vehicle_terms_alone": 210,
                          "part_terms_alone": 0, "removed_by_exclude_terms": 3}}
        facts = search_facts("search_catalog", catalogue_args(), answer)
        self.assertEqual(facts["alone"], {"part": 0, "car": 210, "make": 4012})
        self.assertEqual((facts["total"], facts["excluded"]), (0, 3))
        # Without car words there is nothing to count rows naming the car against.
        without = search_facts("search_catalog", catalogue_args(vehicle=(), stock=True),
                               {"rows": [], "total": 4, "available": 0, "matched_vehicle": 0})
        self.assertNotIn("car_rows", without)
        self.assertIs(without["stock_only"], True)

    def test_part_numbers_found_not_found_too_short_and_through_revisions(self):
        answer = {"numbers": [
            {"number": "90919-01253", "rows": [], "note": "No catalogue row carries this number."},
            {"number": "AB", "note": "Too short to look up."},
            {"number": "0242236565", "rows": [["r1"], ["r2"]], "equivalents": [{"number": "x"}]},
            {"number": "1EA698151D", "rows": [],
             "other_revisions": {"carried_as": ["1EA698151B"], "rows": [["r9"]]}}]}
        self.assertEqual(search_facts("lookup_codes", {"numbers": []}, answer), {"numbers": [
            {"number": "90919-01253", "rows": 0}, {"number": "AB", "short": True},
            {"number": "0242236565", "rows": 2, "cross": 1},
            {"number": "1EA698151D", "rows": 0, "revision": 1}]})
        # A lookup that failed: only what was asked for.
        self.assertEqual(search_facts("lookup_codes", {"numbers": ["90919-01253"]}, None),
                         {"numbers": [{"number": "90919-01253"}]})

    def test_the_outside_sources_keep_what_they_answered(self):
        self.assertEqual(search_facts("supplier_parts", {}, {
            "status": "complete", "supplier_numbers": 4, "rows": [],
            "numbers_not_in_catalogue": ["a", "b", "c", "d"], "sources": ["x"]}),
            {"rows": 0, "status": "complete", "numbers": 4, "not_in_catalogue": 4})
        self.assertEqual(search_facts("oil_guide", {}, {"status": "no_answer", "sources": []}),
                         {"status": "no_answer"})
        self.assertEqual(search_facts("car_numbers", {}, {"vehicles": [{}, {}], "parts": [],
                                                          "rows": []}),
                         {"rows": 0, "vehicles": 2, "parts": 0})
        self.assertEqual(search_facts("research", {}, {"status": "answered", "rows": [["r1"]]}),
                         {"rows": 1, "status": "answered"})
        self.assertEqual(search_facts("equivalents", {"id": "r1"}, {"of": "r1", "rows": []}),
                         {"rows": 0})
        self.assertEqual(search_facts("shop_info", {}, {"georgian": {}}), {})
        self.assertEqual(search_facts("supplier_parts", {}, None), {})

    def test_answers_of_another_shape_are_read_as_nothing_never_raised_on(self):
        for name in ("search_catalog", "lookup_codes", "supplier_parts", "oil_guide", "nope"):
            with self.subTest(tool=name):
                search_facts(name, "x", "y")
                search_facts(name, {"part_terms": "pads", "numbers": [1, None]}, {
                    "total": "31", "why": [], "other_vehicle_texts": [["x"], None, [1, 2, 3]],
                    "similar_spellings": ["x"], "numbers": [None, {"number": 5}]})
        facts = search_facts("search_catalog", {"part_terms": "pads"}, {"total": "31"})
        self.assertEqual(facts["total"], 0)


class TurnDetailTests(unittest.TestCase):
    def test_the_searches_of_the_turn_and_the_staff_note(self):
        detail = turn_detail({"engine_status": "OK", "outcome": "nothing_found",
                              "staff": {"note": "front pads"}, "diagnostics": {"tools": [
                                  {"name": "shop_info", "label": "shop", "found": {}},
                                  {"name": "search_catalog", "label": "x", "rows": 0,
                                   "args": {"part_terms": ["pads"]},
                                   "found": {"part": ["pads"], "total": 0}},
                                  {"name": "oil_guide", "label": "oil · Toyota",
                                   "error": "The tool did not answer in time.", "found": {}}]}})
        self.assertEqual(detail, {
            "v": 1, "outcome": "nothing_found", "note": "front pads", "searched": 2,
            "searches": [{"tool": "search_catalog", "part": ["pads"], "total": 0},
                         {"tool": "oil_guide", "label": "oil · Toyota",
                          "error": "The tool did not answer in time."}]})

    def test_the_parts_the_reply_could_not_offer_are_named(self):
        detail = turn_detail({"engine_status": "OK", "outcome": "offered", "diagnostics": {},
                              "staff": {"missing": ["ზეთის ფილტრი", "", 5]}})
        self.assertEqual(detail["missing"], ["ზეთის ფილტრი"])
        self.assertNotIn("missing", turn_detail({"engine_status": "OK",
                                                 "staff": {"missing": []}}))

    def test_every_search_is_counted_and_the_list_is_capped(self):
        tools = [{"name": "search_catalog", "found": {"part": [f"p{n}"], "total": n}}
                 for n in range(MAX_SEARCHES + 3)]
        detail = turn_detail({"engine_status": "OK", "diagnostics": {"tools": tools}})
        self.assertEqual(detail["searched"], MAX_SEARCHES + 3)
        self.assertEqual(len(detail["searches"]), MAX_SEARCHES)

    def test_out_of_stock_rows_shop_rules_limits_and_what_the_model_chose(self):
        detail = turn_detail({
            "engine_status": "OK", "outcome": "nothing_found",
            "warnings": ["STOCK_MAY_BE_STALE", "ANSWER_CHECKS_FAILED"],
            "cards": [{"availability_status": "UNAVAILABLE", "product": {
                "brand": "DENSO", "original_code": "SK20R11", "product_name": "სანთელი",
                "product_code": "30099999", "stock": {"store": 0, "tuz11": 0}}},
                {"availability_status": "IN_STORE", "product": {"brand": "NGK"}}],
            "diagnostics": {"tools": [], "limits": ["time", "bogus"], "refused": 2,
                            "fallback": "CHECKS_FAILED", "chosen_outcome": "offered",
                            "removed": [{"product": "p9", "reason": "Not for hybrids"},
                                        {"product": "p8", "reason": "Not for hybrids"}]}})
        # The shop's own stock number is not kept: the brand's number and the name say it.
        self.assertEqual(detail["unavailable"], [{"brand": "DENSO", "code": "SK20R11",
                                                  "name": "სანთელი", "store": 0, "warehouse": 0}])
        self.assertEqual(detail["barred"], ["Not for hybrids"])
        self.assertEqual(detail["limits"], ["time", "searches", "checks", "stale_stock"])
        self.assertEqual(detail["chosen"], "offered")
        self.assertNotIn("status", detail)

    def test_a_failed_turn_keeps_its_status_and_where_it_failed(self):
        self.assertEqual(turn_detail({"engine_status": "DATA_UNAVAILABLE", "status": "FAILED",
                                      "diagnostics": {"error": "ValueError", "stage": "model",
                                                      "tools": []}}),
                         {"v": 1, "searched": 0, "status": "DATA_UNAVAILABLE",
                          "error": "ValueError", "stage": "model"})
        # A turn the store's own deadline ended has no engine status: the warning names it.
        store = turn_detail({"status": "PARTIAL", "warnings": ["TURN_DEADLINE"]})
        self.assertEqual((store["status"], store["limits"]), ("TURN_DEADLINE", ["time"]))
        self.assertEqual(turn_detail({})["status"], "FAILED")

    def test_it_stays_small_and_on_one_line_whatever_the_turn_holds(self):
        wide = {"part": ["x" * 500] * 20, "car": ["‮camry\nthe\x00end"],
                "cars": [["C" * 300, 1, 1]] * 50, "spellings": [["a", "b" * 300, 1]] * 50,
                "numbers": [{"number": "9" * 300, "rows": 1}] * 50, "total": 10**12}
        detail = turn_detail({"engine_status": "OK", "outcome": "nothing_found",
                              "staff": {"note": "n" * 900},
                              "cards": [{"availability_status": "UNAVAILABLE",
                                         "product": {"product_name": "p" * 500}}] * 20,
                              "diagnostics": {"tools": [{"name": "search_catalog",
                                                         "found": wide}] * 30,
                                              "removed": [{"reason": str(n) * 300}
                                                          for n in range(20)]}})
        self.assertLessEqual(size(detail), DETAIL_BYTES)
        self.assertEqual(detail["searched"], 30)
        text = json.dumps(detail, ensure_ascii=False)
        for hidden in ("‮", "\\n", "\\u0000"):
            self.assertNotIn(hidden, text)
        # How the rows name their cars goes first; the searches themselves stay.
        self.assertTrue(detail["searches"])
        self.assertNotIn("cars", detail["searches"][0])
        self.assertEqual(detail["searches"][0]["car"], ["camry the end"])

    def test_it_reaches_the_alarms_that_say_something_was_not_found(self):
        payload = {"engine_status": "OK", "outcome": "nothing_found", "car_label": "Prius",
                   "staff": {"intent": "visit", "note": "pads", "missing": []},
                   "diagnostics": {"tools": [{"name": "search_catalog",
                                              "found": {"part": ["pads"], "total": 0}}]}}
        drafts = {draft.kind: draft for draft in turn_alerts(payload, "pads?").drafts}
        self.assertEqual(drafts["part_not_found"].detail["searches"][0]["part"], ["pads"])
        self.assertEqual(drafts["visit_request"].detail, {})  # a visit needs no reason
        handoff = turn_alerts({**payload, "outcome": "handoff"}, "x").drafts
        self.assertEqual([draft.detail["outcome"] for draft in handoff
                          if draft.kind == "needs_staff"], ["handoff"])
        failed = turn_alerts({"engine_status": "TIMEOUT"}, "x", alarm_failures=True).drafts
        self.assertEqual(failed[0].detail["status"], "TIMEOUT")
        # Two drafts are the same alarm whatever their reasons say.
        self.assertEqual(AlertDraft("part_not_found", "nothing_found", "pads"),
                         AlertDraft("part_not_found", "nothing_found", "pads", detail={"v": 1}))


class EngineRecordTests(unittest.TestCase):
    """The engine records, for every search, what the staff are told it found."""

    def test_a_search_for_another_car_is_recorded_with_its_counts_and_reaches_the_alarm(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["PRIUS"], [PADS]))),
                        done(reply="Prius-ის წინა ხუნდები ვერ ვიპოვე.", outcome="nothing_found",
                             car_label="Toyota Prius 2012",
                             staff=staff(note="Prius 2012 წინა ხუნდები", missing=["წინა ხუნდები"])))
        result = public(script, "Prius 2012 წინა ხუნდები")
        record = result["diagnostics"]["tools"][0]
        self.assertIn(PADS, record["label"])
        self.assertEqual(record["found"], {
            "part": [PADS], "car": ["PRIUS"], "make": ["TOYOTA"], "total": 2, "available": 2,
            "rows": 2, "car_rows": 0, "unmatched": ["PRIUS"],
            # the rows the search returned are for another car, in the catalogue's own words
            "cars": [["TOYOTA/LEXUS CAMRY 2006->", 2, 2]]})
        (draft,) = turn_alerts(as_payload(result), "Prius 2012 წინა ხუნდები").drafts
        self.assertEqual((draft.kind, draft.reason), ("part_not_found", "nothing_found"))
        self.assertEqual(draft.detail["searches"][0]["car_rows"], 0)
        self.assertEqual(draft.detail["note"], "Prius 2012 წინა ხუნდები")
        # None of it reaches the customer: the public turn is built key by key.
        view = public_turn(as_payload(result), service(Script()).catalog_store.current())
        text = json.dumps(view, ensure_ascii=False)
        for private in ("diagnostics", "found", "car_rows", "PRIUS\"]", "staff"):
            self.assertNotIn(private, text)

    def test_searches_the_budget_cut_off_and_the_limits_that_cut_the_turn_short(self):
        script = Script(says(*(use("search_catalog", search(["TOYOTA"], ["CAMRY"], [term]))
                               for term in ("ხუნდი წნ", "ხუნდი უკ", "ფილტრი"))), done())
        result = run(service(script, assistant_max_tool_calls=2), "Camry")
        diagnostics = result["diagnostics"]
        self.assertEqual((diagnostics["refused"], diagnostics["limits"]), (1, ["searches"]))
        again = says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])))
        last = run(service(Script(again, done()), assistant_max_requests=2), "Camry")
        self.assertEqual(last["diagnostics"]["limits"], ["requests"])

    def test_rows_the_model_chose_that_could_not_be_shown_are_recorded_as_its_choice(self):
        result = run(service(Script(done(outcome="offered", reply="აი ნაწილები."))), "Camry")
        self.assertEqual((result["outcome"], result["diagnostics"]["chosen_outcome"]),
                         ("nothing_found", "offered"))

    def test_an_unexpected_error_records_its_class_and_the_step_it_came_from(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], [PADS]))),
                        ValueError("Expecting value: line 1 column 2 (char 1)"))
        with self.assertLogs("automotive.assistant", "WARNING") as logged:
            result = public(script, "Camry ხუნდები")
        self.assertEqual(result["status"], "DATA_UNAVAILABLE")
        self.assertEqual((result["diagnostics"]["error"], result["diagnostics"]["stage"]),
                         ("ValueError", "model"))
        line = next(entry for entry in logged.output if "assistant_turn_failed" in entry)
        self.assertIn("ValueError stage=model at=test_assistant.py:create:", line)
        self.assertNotIn("Expecting value", line)  # the error's message never reaches the log
        (draft,) = turn_alerts(as_payload(result), "Camry ხუნდები", alarm_failures=True).drafts
        self.assertEqual((draft.kind, draft.detail["status"], draft.detail["stage"]),
                         ("needs_staff", "DATA_UNAVAILABLE", "model"))
        self.assertEqual(draft.detail["searched"], 1)
        self.assertEqual(_cause(as_payload(result)), "ValueError@model")

    def test_a_fault_while_recording_what_a_search_found_never_fails_the_turn(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], [PADS]))),
                        done())
        with patch("automotive.assistant.search_facts", side_effect=RuntimeError("boom")), \
                self.assertLogs("automotive.assistant", "WARNING") as logged:
            result = run(service(script), "Camry ხუნდები")
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("found", result["diagnostics"]["tools"][0])
        self.assertIn("assistant_search_facts_failed RuntimeError", "\n".join(logged.output))

    def test_the_messenger_cause_is_code_names_only(self):
        self.assertEqual(_cause({"diagnostics": {"error": "Key Error!", "stage": "Tools 1"}}),
                         "KeyError@ools")
        self.assertEqual(_cause({"diagnostics": {"error": "TimeoutError"}}), "TimeoutError")
        for empty in (None, {}, {"diagnostics": "x"}, {"diagnostics": {"stage": "model"}}):
            with self.subTest(payload=empty):
                self.assertEqual(_cause(empty), "")


class WebsiteChainTests(StoreCase):
    """A turn through the visitors' store and the recorder: the reason survives the store and
    reaches the alarm the recorder raises."""

    def test_the_reason_reaches_the_alarm_the_recorder_raises(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["PRIUS"], [PADS]))),
                        done(reply="Prius-ის წინა ხუნდები ვერ ვიპოვე.", outcome="nothing_found",
                             staff=staff(note="Prius 2012 წინა ხუნდები",
                                         missing=["წინა ხუნდები"])))
        fake = FakeStore()
        recorder = LiveRecorder(fake, start=False)
        store = self.store(script, recorder)
        identifier = store.create()["conversation_id"]
        recorder.seen(identifier, "10.0.0.9")
        store.submit(identifier, "Prius 2012 წინა ხუნდები", language="ka")
        self.assertTrue(wait_for(lambda: recorder._queue.qsize() >= 2))
        recorder.drain()
        (alarm,) = [call[3] for call in fake.calls if call[0] == "raise_alert"]
        self.assertEqual((alarm.kind, alarm.reason), ("part_not_found", "nothing_found"))
        self.assertEqual((alarm.detail["searched"], alarm.detail["missing"]),
                         (1, ["წინა ხუნდები"]))
        self.assertEqual(alarm.detail["searches"][0]["cars"],
                         [["TOYOTA/LEXUS CAMRY 2006->", 2, 2]])


class WorkerCauseTests(WorkerCase):
    """The Messenger worker keeps why a turn failed on its reply, for the stuck chat's card."""

    def test_a_failed_turn_keeps_the_engines_cause_on_its_reply(self):
        def crashed(call):
            result = failure("DATA_UNAVAILABLE", engine_status="DATA_UNAVAILABLE")(call)
            return dataclasses.replace(result, cause="ValueError@model")

        self.engine.results = [crashed, failure("TIMEOUT", status="timeout")]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "fallback_sent"})
        self.assertEqual((self.reply(mid)["engine_status"], self.reply(mid)["error_text"]),
                         ("DATA_UNAVAILABLE", "ValueError@model"))
        later, _ = self.turn("ზეთი")  # a failure with no cause leaves the text empty
        self.assertEqual(self.reply(later).get("error_text", ""), "")


class RouteTests(LiveCase):
    """The Live routes hand both roles the reasons as the store keeps them."""

    async def test_an_employee_reads_why_with_the_alarm_and_the_cause_of_a_stuck_chat(self):
        why = {"v": 1, "outcome": "nothing_found", "searched": 1,
               "searches": [{"tool": "search_catalog", "part": ["pads"], "total": 0}]}
        self.store.alerts = [alert(1, detail=why)]
        stuck = chat(needs_attention=True, attention_reason="engine_failed",
                     failed_engine="DATA_UNAVAILABLE", failed_error="ValueError@model")
        self.store.chats = lambda filter="all", limit=40: [stuck]
        alarms = await self.live("GET", "/alerts", who=self.employee)
        self.assertEqual(alarms.status_code, 200)
        self.assertEqual(alarms.json()["alerts"][0]["detail"], why)
        chats = await self.live("GET", "/chats?filter=stuck", who=self.employee)
        self.assertEqual({key: chats.json()["chats"][0][key]
                          for key in ("failed_engine", "failed_error")},
                         {"failed_engine": "DATA_UNAVAILABLE", "failed_error": "ValueError@model"})


if __name__ == "__main__":
    unittest.main()
