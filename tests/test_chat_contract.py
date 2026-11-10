"""The chat contract: one reading of a turn for the staff route and the public widget.

The golden turns are in tests/fixtures/chat_ui/turns.json. Each has the engine's payload
as ``ConversationStore.get`` returns it, what the staff route adds to it (``staff``) and
what a visitor's browser is sent (``public``). The front-end tests draw their replies
from the same file, so a change here shows up on both screens.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from automotive import chat_contract
from automotive.site_catalog import Catalog
from automotive.site_chat import public_turn
from tests.test_site_catalog import ROWS

FIXTURE = Path(__file__).parent / "fixtures" / "chat_ui" / "turns.json"
TURNS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SCENARIOS = ["question", "offer", "oil_change", "nothing_found", "legacy_clarify",
             "legacy_offer", "running"]
FIT_LEVELS = {"confirmed", "listed", "check"}  # the levels that speak of a part's fit to the car
CAMRY = {"make": "TOYOTA", "model": "Camry", "model_year": 2015}
FRONT_REAR = [{"label": "წინა", "value": "წინა"}, {"label": "უკანა", "value": "უკანა"}]


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    return []


class GoldenTurnTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)

    def test_the_fixture_holds_the_scenarios_both_screens_are_tested_with(self):
        self.assertEqual([turn["id"] for turn in TURNS], SCENARIOS)
        for turn in TURNS:
            self.assertEqual(set(turn), {"id", "about", "engine", "staff", "public"}, turn["id"])

    def test_each_engine_turn_gives_the_staff_view(self):
        for turn in TURNS:
            engine = copy.deepcopy(turn["engine"])
            self.assertEqual(chat_contract.view(engine), turn["staff"], turn["id"])
            self.assertEqual(engine, turn["engine"], "the payload is read, never changed")
            # The view is nothing more than the functions it is made of.
            self.assertEqual(chat_contract.question(engine), turn["staff"]["question"])
            self.assertEqual(chat_contract.suggestions(engine), turn["staff"]["suggestions"])
            self.assertEqual(chat_contract.groups(engine), turn["staff"]["groups"])
            self.assertEqual(len(turn["staff"]["cards"]), len(engine["cards"]), turn["id"])

    def test_each_engine_turn_gives_the_public_view(self):
        for turn in TURNS:
            engine = copy.deepcopy(turn["engine"])
            self.assertEqual(public_turn(engine, self.catalog), turn["public"], turn["id"])
            self.assertEqual(engine, turn["engine"], "the payload is read, never changed")

    def test_both_screens_are_told_the_same_thing(self):
        for turn in TURNS:
            staff, public = turn["staff"], turn["public"]
            self.assertEqual(public["question"], staff["question"], turn["id"])
            self.assertEqual(public["suggestions"], staff["suggestions"][:4], turn["id"])
            self.assertEqual([(entry["label"], entry["count"]) for entry in public["groups"]],
                             [(entry["label"], entry["count"]) for entry in staff["groups"]])
            self.assertEqual([card["confidence"] for card in public["cards"]],
                             [card["confidence"] for card in staff["cards"]], turn["id"])
            # A visitor is given the same grouping under ids of the facade's own making.
            renamed = {entry["id"]: f"g{place}"
                       for place, entry in enumerate(staff["groups"], start=1)}
            self.assertEqual([card["group"] for card in public["cards"]],
                             [renamed.get(card["group"], "") for card in staff["cards"]])

    def test_the_scenarios_say_what_they_are_named_for(self):
        views = {turn["id"]: turn["public"] for turn in TURNS}
        self.assertEqual(len(views["question"]["question"]["options"]), 3)
        self.assertEqual(views["question"]["cards"], [])
        self.assertIsNone(views["offer"]["question"])
        self.assertEqual((len(views["offer"]["groups"]), len(views["offer"]["suggestions"])),
                         (2, 2))
        self.assertEqual({card["confidence"] for card in views["offer"]["cards"]}, FIT_LEVELS)
        # The oil change: every part of a group rests on one ground, which both screens
        # then say once for the group. The oils are chosen by specification, with a reason.
        oil_change = views["oil_change"]
        self.assertEqual([entry["label"] for entry in oil_change["groups"]],
                         ["ზეთის ფილტრი", "ძრავის ზეთი"])
        grounds = {}
        for shown in oil_change["cards"]:
            grounds.setdefault(shown["group"], set()).add((shown["confidence"], shown["reason"]))
        self.assertEqual({group: len(found) for group, found in grounds.items()},
                         {"g1": 1, "g2": 1})
        self.assertEqual(grounds["g1"], {("listed", "")})
        (level, reason), = grounds["g2"]
        self.assertEqual((level, bool(reason)), ("spec", True))
        self.assertEqual({card["confidence"] for turn in TURNS for card in turn["public"]["cards"]
                          if card["confidence"]}, set(chat_contract.LEVELS))
        self.assertEqual((views["nothing_found"]["cards"], views["nothing_found"]["question"]),
                         ([], None))
        self.assertEqual(views["legacy_clarify"]["question"], {"options": FRONT_REAR})
        self.assertEqual([option["label"] for option
                          in views["legacy_offer"]["question"]["options"]], ["დიახ", "არა"])
        self.assertEqual((views["running"]["status"], views["running"]["question"],
                          views["running"]["suggestions"]), ("RUNNING", None, []))
        # The car line never shows more than make, model and year.
        for view in views.values():
            self.assertEqual(view["car"], "Toyota Camry 2015")
        # What only staff may see is in the fixture's payloads and in no public view:
        # stock numbers, the vehicle record, the engine's notes, ids and bookkeeping.
        for turn in TURNS:
            text = json.dumps(turn["public"], ensure_ascii=False)
            for private in ("ZQ-1234", "04465", "2AR-FE", "XLE", "private", "oil_filter",
                            "engine_oil", "front", "evidence", "pending", "input_tokens",
                            "sid-0", "ZQ-7702", "2060006281", "meaning", "memory",
                            "diagnostics", "outcome"):
                self.assertNotIn(private, text, turn["id"])
            # The staff view adds the contract's four keys and never the engine's notes.
            self.assertNotIn("meaning", json.dumps(turn["staff"], ensure_ascii=False))
        payloads = json.dumps([turn["engine"] for turn in TURNS], ensure_ascii=False)
        for carried in ("private meaning", "private notes", "2AR-FE", "ZQ-1234"):
            self.assertIn(carried, payloads, "the fixture no longer carries what it guards")


class OptionTests(unittest.TestCase):
    def test_options_are_label_and_value_pairs_within_their_limits(self):
        found = chat_contract.options([
            "Petrol", {"label": "Diesel", "value": "diesel 2.0"}, {"value": "Hybrid"},
            {"label": "LPG"}, {"label": " two\n lines ", "value": 7}, "", "   ", None, 7,
            {"label": 5, "value": "x"}, {}, ["x"], {"label": "x" * 300, "value": "y" * 900}])
        self.assertEqual(found[:5], [
            {"label": "Petrol", "value": "Petrol"}, {"label": "Diesel", "value": "diesel 2.0"},
            {"label": "Hybrid", "value": "Hybrid"}, {"label": "LPG", "value": "LPG"},
            {"label": "two lines", "value": "two lines"}])
        self.assertEqual((len(found), len(found[5]["label"]), len(found[5]["value"])),
                         (6, 80, 200))
        self.assertEqual(len(chat_contract.options(["a"] * 30)), 8)
        self.assertEqual(len(chat_contract.options(["a"] * 30, limit=3)), 3)
        self.assertEqual(chat_contract.options(["a"], limit=0), [])
        for junk in (None, "text", 7, {"label": "x"}, {"a", "b"}):
            self.assertEqual(chat_contract.options(junk), [], junk)

    def test_engine_fields_win_over_what_an_older_engine_would_have_meant(self):
        # Every sign of an older engine's question is present, and the engine says otherwise.
        signs = {"status": "COMPLETE", "act": "clarify_axle", "engine_status": "CLARIFICATION",
                 "clarification_choices": ["2.5", "3.5"], "quick_replies": ["Front", "Rear"],
                 "pending": {"act": "clarify_axle", "offer": {"category": "rear brake pads"}}}
        stated = {**signs, "question": None, "suggestions": ["უკანა ხუნდებიც"]}
        self.assertIsNone(chat_contract.question(stated))
        self.assertEqual(chat_contract.suggestions(stated),
                         [{"label": "უკანა ხუნდებიც", "value": "უკანა ხუნდებიც"}])
        asking = {**signs, "question": {"text": "?", "options": FRONT_REAR}}
        self.assertEqual(chat_contract.question(asking), {"options": FRONT_REAR})
        self.assertEqual(chat_contract.suggestions(asking), [])
        # Either key is enough to say that the engine speaks for itself.
        self.assertIsNone(chat_contract.question({**signs, "suggestions": []}))

    def test_a_question_needs_two_answers_and_keeps_the_engines_numbering(self):
        def split(question, suggestions=()):
            payload = {"status": "COMPLETE", "question": question,
                       "suggestions": list(suggestions)}
            return (chat_contract.question(payload),
                    [option["label"] for option in chat_contract.suggestions(payload)])

        self.assertEqual(split({"options": ["a", "b"]}), ({"options": [
            {"label": "a", "value": "a"}, {"label": "b", "value": "b"}]}, []))
        # Twelve are cut to eight from the end, so the first eight keep their numbers.
        twelve = [f"o{place}" for place in range(1, 13)]
        self.assertEqual([option["label"] for option
                          in split({"options": twelve})[0]["options"]], twelve[:8])
        # An unusable answer at the end leaves the others where they were...
        self.assertEqual(split({"options": ["a", "b", None]})[0]["options"][1]["label"], "b")
        # ...but one before another would renumber them: they become next steps instead.
        self.assertEqual(split({"options": [None, "a", "b"]}, ["c"]), (None, ["a", "b", "c"]))
        self.assertEqual(split({"options": ["a", "", "b"]}), (None, ["a", "b"]))
        # One answer is no choice; a next step that reads the same is not repeated.
        self.assertEqual(split({"options": ["a"]}, ["A", "c"]), (None, ["a", "c"]))
        for quiet in (None, {}, {"text": "?"}, {"options": []}, {"options": "ab"}, "a?", 3):
            self.assertEqual(split(quiet, ["c"]), (None, ["c"]), quiet)

    def test_a_callers_own_rule_can_drop_or_rewrite_an_option(self):
        def no_digits(option):
            if any(letter.isdigit() for letter in option["label"]):
                return None
            return {"label": option["label"].upper(), "value": option["value"]}

        payload = {"status": "COMPLETE", "question": {"options": ["a", "b", "c3"]},
                   "suggestions": ["d", "e5"]}
        self.assertEqual(chat_contract.question(payload, keep=no_digits), {"options": [
            {"label": "A", "value": "a"}, {"label": "B", "value": "b"}]})
        self.assertEqual(chat_contract.suggestions(payload, keep=no_digits),
                         [{"label": "D", "value": "d"}])
        # Dropping one before another ends the numbering, exactly as an unusable one does.
        early = {"status": "COMPLETE", "question": {"options": ["a1", "b", "c"]}}
        self.assertIsNone(chat_contract.question(early, keep=no_digits))
        self.assertEqual([option["label"] for option
                          in chat_contract.suggestions(early, keep=no_digits)], ["B", "C"])
        # What the rule returns is cleaned like anything else.
        self.assertEqual(chat_contract.suggestions(payload, keep=lambda option: "x" * 300),
                         [{"label": "x" * 80, "value": "x" * 200}])
        self.assertEqual(chat_contract.suggestions(payload, keep=lambda option: 7), [])

    def test_an_older_engines_reply_is_a_question_by_any_of_four_signs(self):
        quick = {"status": "COMPLETE", "quick_replies": ["წინა", "უკანა"]}
        self.assertIsNone(chat_contract.question(quick))
        self.assertEqual(chat_contract.suggestions(quick), FRONT_REAR)
        for sign in ({"act": "clarify_axle"}, {"engine_status": "CLARIFICATION"},
                     {"pending": {"act": "clarify_engine", "options": []}}):
            asked = {**quick, **sign}
            self.assertEqual(chat_contract.question(asked), {"options": FRONT_REAR}, sign)
            self.assertEqual(chat_contract.suggestions(asked), [], sign)
        # Its own list of choices is a sign too, and wins over the quick replies.
        chosen = {**quick, "clarification_choices": ["2.5", {"label": "3.5 V6", "value": "3.5"}]}
        self.assertEqual(chat_contract.question(chosen), {"options": [
            {"label": "2.5", "value": "2.5"}, {"label": "3.5 V6", "value": "3.5"}]})
        self.assertEqual(chat_contract.suggestions(chosen), [])
        # A question it has no buttons for stays a sentence: there is nothing to tap.
        bare = {"status": "COMPLETE", "act": "clarify_vehicle", "quick_replies": []}
        self.assertEqual((chat_contract.question(bare), chat_contract.suggestions(bare)),
                         (None, []))

    def test_the_reply_text_carries_the_question_it_asks(self):
        asked = {"text": "წინა თუ უკანა ხუნდები გჭირდებათ?", "options": FRONT_REAR}
        # An engine writes its question into the answer: nothing is added to that.
        whole = "Toyota Camry 2015. წინა თუ უკანა ხუნდები გჭირდებათ?"
        self.assertEqual(chat_contract.answer({"answer": whole, "question": asked}), whole)
        # In other words, but asked: still nothing is added.
        other = "Toyota Camry 2015. რომელი ღერძისთვის გჭირდებათ?"
        self.assertEqual(chat_contract.answer({"answer": other, "question": asked}), other)
        # Kept apart from a text that asks nothing, it is added: buttons need their question.
        self.assertEqual(chat_contract.answer({"answer": "Toyota Camry 2015. ", "question": asked}),
                         "Toyota Camry 2015. წინა თუ უკანა ხუნდები გჭირდებათ?")
        self.assertEqual(chat_contract.answer({"answer": "", "question": asked}), asked["text"])
        plain = {"text": "front  or\nrear", "options": []}
        self.assertEqual(chat_contract.answer({"answer": "Pads.", "question": plain}),
                         "Pads. front or rear")
        self.assertEqual(chat_contract.answer({"answer": "Pads: Front or rear.",
                                               "question": plain}), "Pads: Front or rear.")
        # No question, no change; and never more than the limit.
        for quiet in (None, {}, {"text": ""}, {"text": 7}, "text?", ["?"]):
            self.assertEqual(chat_contract.answer({"answer": "Pads.", "question": quiet}),
                             "Pads.", quiet)
        self.assertEqual(len(chat_contract.answer({"answer": "a" * 9000, "question": asked})),
                         4000)
        self.assertEqual(chat_contract.answer({"answer": "abcdef"}, limit=3), "abc")
        for junk in (None, "text", 7, [], {"answer": None}, {"answer": 7}, {"answer": ["x"]}):
            self.assertEqual(chat_contract.answer(junk), "", junk)

    def test_a_pending_offer_is_answered_with_yes_or_no(self):
        offer = {"status": "COMPLETE", "act": "results", "quick_replies": ["Air filter"],
                 "pending": {"act": "results", "offer": {"category": "rear brake pads"}}}
        self.assertEqual(chat_contract.question({**offer, "language": "en"}), {"options": [
            {"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}]})
        for language in ("ka", None, "fr"):
            self.assertEqual(chat_contract.question({**offer, "language": language}),
                             {"options": [{"label": "დიახ", "value": "დიახ"},
                                          {"label": "არა", "value": "არა"}]}, language)
        # What it also suggested stays a next step.
        self.assertEqual(chat_contract.suggestions(offer),
                         [{"label": "Air filter", "value": "Air filter"}])

    def test_a_running_turn_has_asked_and_suggested_nothing(self):
        for payload in (
                {"status": "RUNNING", "act": "clarify_axle", "quick_replies": ["a", "b"],
                 "clarification_choices": ["a", "b"], "pending": {"offer": {"category": "x"}}},
                {"status": "RUNNING", "question": {"options": ["a", "b"]},
                 "suggestions": ["c"]}):
            self.assertIsNone(chat_contract.question(payload))
            self.assertEqual(chat_contract.suggestions(payload), [])
        for junk in (None, "text", 7, [], [{"question": {"options": ["a", "b"]}}]):
            self.assertIsNone(chat_contract.question(junk), junk)
            self.assertEqual(chat_contract.suggestions(junk), [], junk)

    def test_next_steps_are_at_most_six_and_fewer_when_asked(self):
        payload = {"status": "COMPLETE", "question": None,
                   "suggestions": [f"step {place}" for place in range(20)]}
        self.assertEqual(len(chat_contract.suggestions(payload)), 6)
        self.assertEqual(len(chat_contract.suggestions(payload, 4)), 4)
        self.assertEqual(chat_contract.suggestions(payload, 0), [])
        self.assertEqual(chat_contract.suggestions(payload, -3), [])


class GroupAndCardTests(unittest.TestCase):
    def test_groups_are_bounded_headings_in_the_engines_order(self):
        found = chat_contract.groups({"groups": [
            {"id": "front", "label": " წინა \n ხუნდები ", "count": 3},
            {"id": "front", "label": "a second one with the same id", "count": 1},
            {"id": 2, "label": "x" * 300, "count": -4}, {"id": True, "label": "b"},
            {"id": "rear", "label": "", "count": 1}, {"label": "no id"}, "text", None,
            {"id": "y" * 90, "label": "long id", "count": 2.5},
            *({"id": f"g{place}", "label": "l", "count": 10 ** 9} for place in range(9))]})
        self.assertEqual(found[:3], [
            {"id": "front", "label": "წინა ხუნდები", "count": 3},
            {"id": "2", "label": "x" * 80, "count": 0},
            {"id": "y" * 40, "label": "long id", "count": 0}])
        self.assertEqual((len(found), found[3]), (6, {"id": "g0", "label": "l", "count": 999}))
        for junk in (None, "text", {}, {"groups": None}, {"groups": "front"},
                     {"groups": {"id": "front", "label": "x"}}):
            self.assertEqual(chat_contract.groups(junk), [], junk)

    def test_a_cards_group_and_reason_are_bounded_lines(self):
        self.assertEqual(chat_contract.group({"group": " front "}), "front")
        self.assertEqual(chat_contract.group({"group": 3}), "3")
        self.assertEqual(len(chat_contract.group({"group": "g" * 90})), 40)
        self.assertEqual(chat_contract.reason({"reason": " ორი\nხაზი \t ერთად "}), "ორი ხაზი ერთად")
        self.assertEqual(len(chat_contract.reason({"reason": "მიზეზი " * 90})), 160)
        for junk in (None, "text", 7, [], {}, {"group": None, "reason": None},
                     {"group": True, "reason": 7}, {"group": ["g"], "reason": {"text": "x"}}):
            self.assertEqual((chat_contract.group(junk), chat_contract.reason(junk)), ("", ""),
                             junk)

    def test_confidence_is_one_of_four_levels_or_nothing(self):
        # Three speak of a part's fit to the car; "spec" is for a product no car is named
        # on (an oil, a fluid) that was chosen because its specification is what the car needs.
        self.assertEqual(chat_contract.LEVELS, ("confirmed", "listed", "check", "spec"))
        # The engine's own level stands, with or without a car.
        for level in chat_contract.LEVELS:
            self.assertEqual(chat_contract.confidence({"confidence": level}), level)
            self.assertEqual(chat_contract.confidence({"confidence": level, "basis": "name"},
                                                      CAMRY), level)
        # A level that is not one of the four is not passed on: a screen then shows no
        # label, never a wrong one.
        for other in ("likely", "exact", "Confirmed", "Spec", "specification", "", None, 1,
                      True, ["listed"]):
            self.assertEqual(chat_contract.confidence({"confidence": other}, CAMRY), "", other)
        for junk in (None, "listed", 7, []):
            self.assertEqual(chat_contract.confidence(junk, CAMRY), "", junk)
        # An engine that states a level speaks for itself: one it should not have sent is
        # not replaced by what the older fields beside it would have meant.
        for basis, status in (("code", "UNKNOWN"), ("name_year", "UNKNOWN"),
                              ("fitment", "VERIFIED"), ("", "VERIFIED")):
            unknown = {"confidence": "exact", "basis": basis, "fitment_status": status}
            self.assertEqual(chat_contract.confidence(unknown, CAMRY), "", (basis, status))
        # Left empty, it is an older engine's card after all.
        self.assertEqual(chat_contract.confidence({"confidence": " ", "basis": "code"}, CAMRY),
                         "confirmed")

    def test_an_older_engines_basis_is_read_as_a_level_once_the_car_is_known(self):
        expected = {"staff_rule": "confirmed", "code": "confirmed", "name_year": "listed",
                    "name": "check", "engine": "check", "verified_oil": "",
                    "oil_grade_brand": "", "oil_grade": "", "typed_code": "",
                    "something new": "", "": "", None: "", 7: ""}
        for basis, level in expected.items():
            self.assertEqual(chat_contract.confidence({"basis": basis}, CAMRY), level, basis)
            # Without a car's model there is nothing for a part to fit.
            for unknown in (None, {}, {"make": "TOYOTA"}, {"model": "  "}, "Camry"):
                self.assertEqual(chat_contract.confidence({"basis": basis}, unknown), "")
        # Reviewed fitment data confirms; a candidate from it is still to be checked.
        verified = {"basis": "fitment", "fitment_status": "VERIFIED"}
        self.assertEqual(chat_contract.confidence(verified, CAMRY), "confirmed")
        self.assertEqual(chat_contract.confidence({"basis": "fitment"}, CAMRY), "check")
        self.assertEqual(chat_contract.confidence({"fitment_status": "VERIFIED"}, CAMRY),
                         "confirmed")
        # An oil's grade is not a fit, even where the engine marked the row verified.
        self.assertEqual(chat_contract.confidence(
            {"basis": "verified_oil", "fitment_status": "VERIFIED"}, CAMRY), "")

    def test_nothing_leaves_longer_than_its_limit_whatever_comes_in(self):
        huge = "ძ" * 5000
        payload = {
            "status": "COMPLETE", "vehicle": CAMRY,
            "question": {"text": huge, "options": [{"label": huge, "value": huge}] * 40},
            "suggestions": [{"label": huge + str(place), "value": huge} for place in range(40)],
            "groups": [{"id": huge + str(place), "label": huge, "count": 10 ** 12}
                       for place in range(40)],
            "cards": [{"group": huge, "reason": huge, "confidence": huge}, None, "card", 7]}
        view = chat_contract.view(payload)
        self.assertLessEqual(max(len(text) for text in strings_in(view)), 200)
        self.assertEqual((len(view["question"]["options"]), len(view["suggestions"]),
                          len(view["groups"]), len(view["cards"])), (8, 1, 1, 4))
        self.assertEqual(view["cards"][0], {"group": "ძ" * 40, "reason": "ძ" * 160,
                                            "confidence": ""})
        self.assertEqual(view["cards"][1:], [{"group": "", "reason": "", "confidence": ""}] * 3)
        json.dumps(view)  # plain data, ready for a response model
        for junk in (None, "text", 7, [], {"cards": "x"}, {"cards": None}):
            self.assertEqual(chat_contract.view(junk), {"question": None, "suggestions": [],
                                                        "groups": [], "cards": []}, junk)


if __name__ == "__main__":
    unittest.main()
