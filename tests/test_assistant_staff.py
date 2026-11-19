"""finish.staff: what the shop's staff are told about a turn (dashboard > Live chats).

The model decides what the customer means; the engine checks the shape, keeps a phone number only
when it stands in the customer's own words, and never calls the staff for the staff chat. A scripted
model, so no network and no provider.
"""

from __future__ import annotations

import json
import unittest

from automotive.assistant import NO_STAFF
from automotive.assistant_tools import STAFF_INTENTS, tool_definitions
from automotive.live_alerts import turn_alerts
from automotive.live_web import LiveRecorder
from automotive.site_chat import public_turn
from tests.test_assistant import KNOWN, Script, context_of, done, product, run, service
from tests.test_live_web import FakeStore, SpyLive, StoreCase, wait_for

COMING = "ხვალ მოვალ, ჩემი ნომერია 599 12 34 56"
REPLY = "გამარჯობა! გამოგვიგზავნეთ ტელეფონის ნომერი და ჩვენი გუნდი დაგიკავშირდებათ."


def staff(intent="none", note="", phone="", missing=()):
    return {"intent": intent, "note": note, "phone": phone, "missing": list(missing)}


def visit(**extra):
    return done(reply=REPLY, outcome="chat", staff=staff(
        "visit", "wants to come tomorrow for front pads", **extra))


def public(script: Script, message: str, memory=None, **kwargs) -> dict:
    state = {"audience": "public", **({"memory": memory} if memory else {})}
    return run(service(script), message, dialogue=state, **kwargs)


def as_payload(result: dict) -> dict:
    """What ConversationStore keeps: the engine's own status stands as ``engine_status``."""
    return {**result, "engine_status": result["status"]}


class SchemaTests(unittest.TestCase):
    def test_the_finish_tool_asks_for_staff_with_four_required_fields(self):
        schema = tool_definitions()[-1]["input_schema"]
        self.assertEqual(schema["required"][-1], "staff")
        block = schema["properties"]["staff"]
        self.assertEqual(list(block["properties"]), ["intent", "note", "phone", "missing"])
        self.assertEqual(block["required"], ["intent", "note", "phone", "missing"])
        self.assertIs(block["additionalProperties"], False)
        self.assertEqual(block["properties"]["intent"]["enum"], list(STAFF_INTENTS))
        self.assertEqual(block["properties"]["missing"]["items"], {"type": "string"})
        for name, field in block["properties"].items():
            with self.subTest(field=name):
                self.assertTrue(field["description"])
        self.assertTrue(block["description"])


class StaffTests(unittest.TestCase):
    def test_a_visit_and_the_phone_the_customer_typed(self):
        result = public(Script(visit(phone="599123456")), COMING, KNOWN)
        self.assertEqual(result["status"], "OK", result.get("answer"))
        self.assertEqual(result["staff"], {"intent": "visit", "phone": "599123456", "missing": [],
                                           "note": "wants to come tomorrow for front pads"})
        self.assertEqual(result["memory"]["phone"], "on_file")
        # The website log keeps diagnostics: the intent, never the number.
        self.assertEqual(result["diagnostics"]["staff_intent"], "visit")
        self.assertNotIn("599123456", json.dumps(result["diagnostics"]))

    def test_a_number_the_customer_never_wrote_is_dropped_silently(self):
        script = Script(visit(phone="599123457"))
        result = public(script, COMING, KNOWN)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["staff"]["phone"], "")
        self.assertEqual(result["staff"]["intent"], "visit")
        self.assertEqual(len(script.requests), 1)  # no retry for a number
        # What the customer typed was not taken, so the next turn is not told a number was
        # asked for or given: the bot may ask again instead of leaving the alarm without one.
        self.assertEqual(result["memory"]["phone"], "")

    def test_the_model_may_add_the_country_code_to_the_number_the_customer_typed(self):
        result = public(Script(visit(phone="+995599123456")), COMING, KNOWN)
        self.assertEqual(result["staff"]["phone"], "+995599123456")

    def test_an_unknown_intent_is_corrected_and_the_second_answer_counts(self):
        script = Script(done(reply=REPLY, outcome="chat", staff=staff("book")),
                        visit(phone="599123456"))
        result = public(script, COMING, KNOWN)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(script.requests), 2)
        self.assertIn("staff.intent must be one of none, visit, service",
                      json.dumps(script.requests[1], ensure_ascii=False))
        self.assertEqual(result["staff"]["intent"], "visit")

    def test_a_staff_value_that_is_not_an_object_is_corrected(self):
        script = Script(done(reply=REPLY, outcome="chat", staff="visit"), visit())
        result = public(script, COMING, KNOWN)
        self.assertEqual(len(script.requests), 2)
        self.assertIn("staff must be an object", json.dumps(script.requests[1],
                                                           ensure_ascii=False))
        self.assertEqual(result["staff"]["intent"], "visit")

    def test_the_staff_chat_never_calls_the_staff(self):
        result = run(service(Script(visit(phone="599123456"))), COMING,
                     dialogue={"memory": KNOWN})  # no audience: the staff page
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["staff"], NO_STAFF)
        self.assertEqual(result["memory"]["phone"], "")

    def test_an_older_finish_without_staff_means_nothing_for_the_staff(self):
        result = public(Script(done()), "გამარჯობა", KNOWN)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["staff"], NO_STAFF)

    def test_a_part_missing_from_an_answer_that_offered_others_is_kept(self):
        script = Script(done(reply="ზეთი გვაქვს, ფილტრი ვერ ვიპოვე.", outcome="offered",
                             car_label="Toyota Camry 2015", products=[product("r1")],
                             staff=staff(missing=["oil filter", "  ", "x" * 200,
                                                  *[f"part {n}" for n in range(9)]])))
        result = public(script, "ზეთი და ზეთის ფილტრი Camry 2015-ზე", KNOWN)
        self.assertEqual(result["status"], "OK", result.get("answer"))
        missing = result["staff"]["missing"]
        self.assertEqual((missing[0], len(missing[1]), len(missing)), ("oil filter", 80, 6))
        alerts = turn_alerts(as_payload(result), "x")
        self.assertEqual([(d.kind, d.reason) for d in alerts.drafts],
                         [("part_not_found", "partial")])

    def test_a_visit_raises_an_alarm_with_the_phone(self):
        result = public(Script(visit(phone="599123456")), COMING, KNOWN)
        alerts = turn_alerts(as_payload(result), COMING)
        self.assertEqual([(d.kind, d.phone) for d in alerts.drafts],
                         [("visit_request", "599123456")])
        self.assertEqual(alerts.phone, "599123456")

    def test_the_next_turn_is_told_whether_a_phone_was_asked_or_given(self):
        first_script = Script(visit())
        first = public(first_script, "მინდა მოვიდე ხვალ", KNOWN)
        self.assertNotIn("phone", context_of(first_script.requests[0]))
        self.assertEqual(first["memory"]["phone"], "asked")
        second_script = Script(visit(phone="599123456"))
        second = public(second_script, "599 12 34 56", first["memory"])
        self.assertEqual(context_of(second_script.requests[0])["phone"], "asked")
        self.assertEqual(second["memory"]["phone"], "on_file")
        third_script = Script(done(reply="კარგი, გავითვალისწინებთ.", outcome="chat"))
        third = public(third_script, "მადლობა", second["memory"])
        self.assertEqual(context_of(third_script.requests[0])["phone"], "on_file")
        self.assertEqual(third["memory"]["phone"], "on_file")  # not forgotten, not asked again

    def test_a_number_typed_in_an_earlier_message_still_counts(self):
        memory = {**KNOWN, "transcript": [{"user": "ჩემი ნომერია 599 12 34 56",
                                           "assistant": "გმადლობთ."}]}
        result = public(Script(visit(phone="599123456")), "ხვალ მოვალ", memory)
        self.assertEqual(result["staff"]["phone"], "599123456")


class WebsiteFlowTests(StoreCase):
    """The same turn through the visitors' store, the recorder and an alarm."""

    def test_a_visit_with_a_phone_reaches_the_recorder_as_an_alarm(self):
        fake = FakeStore()
        recorder = LiveRecorder(fake, start=False)
        store = self.store(Script(visit(phone="599123456")), recorder)
        identifier = store.create()["conversation_id"]
        recorder.seen(identifier, "10.0.0.9")
        store.submit(identifier, COMING, language="ka")
        self.assertTrue(wait_for(lambda: recorder._queue.qsize() >= 2))
        recorder.drain()
        alarms = [call for call in fake.calls if call[0] == "raise_alert"]
        self.assertEqual([(call[3].kind, call[3].phone) for call in alarms],
                         [("visit_request", "599123456")])
        attached = [call for call in fake.calls if call[0] == "attach_phone"]
        self.assertEqual([call[3] for call in attached], ["599123456"])

    def test_the_payload_the_recorder_gets_carries_staff_and_the_widget_never_does(self):
        spy = SpyLive()
        store = self.store(Script(visit(phone="599123456")), spy)
        identifier = store.create()["conversation_id"]
        started = store.submit(identifier, COMING, language="ka")
        self.assertTrue(wait_for(lambda: any(event[0] == "turn" for event in spy.events)))
        payload = [event for event in spy.events if event[0] == "turn"][0][3]
        self.assertEqual(payload["staff"]["phone"], "599123456")
        view = public_turn(store.get(identifier, started["turn_id"]),
                           self.chat.catalog_store.current(), said=COMING)
        self.assertNotIn("staff", view)
        self.assertNotIn("599123456", json.dumps(view, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
