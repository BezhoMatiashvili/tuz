"""Alarms from the Messenger bot's turns (dashboard > Live chats): what the engine adapter hands the
worker, and what the worker records. A scripted model and fakes: no network, no provider, $0."""

from __future__ import annotations

import dataclasses
import unittest

import anthropic
import httpx

from automotive.live_alerts import AlertDraft, TurnAlerts
from automotive.messenger_engine import MessengerConversations, MessengerEngine
from automotive.meta_graph import GraphError
from tests.test_assistant import KNOWN, REQUEST, Script, Search, done, product, service
from tests.test_messenger_worker import PSID, WorkerCase, failure, ok

PADS = AlertDraft("part_not_found", "nothing_found", "front brake pads", "Honda Fit 2014")
VISIT = AlertDraft("visit_request", "", "coming tomorrow", "", "599123456")


def with_alerts(*drafts: AlertDraft, phone: str = ""):
    """An ok turn whose engine result carries these alarms."""
    turn = TurnAlerts(tuple(drafts), phone, "ხუნდები გაქვთ?", "")

    def build(call: dict):
        return dataclasses.replace(ok()(call), alerts=turn)
    return build


class FakeLive:
    """The part of LiveStore the worker uses."""

    def __init__(self) -> None:
        self.turns: list[tuple] = []
        self.available = True
        self.broken = False

    def usable(self) -> bool:
        return self.available

    def record_turn(self, channel: str, chat_key: str, turn: TurnAlerts) -> list:
        if self.broken:
            raise RuntimeError("database is down: 599 12 34 56")
        self.turns.append((channel, chat_key, turn))
        return []


class LiveWorkerCase(WorkerCase):
    def make_worker(self, **options):
        self.live = FakeLive()
        return super().make_worker(live=self.live, **options)


class WorkerAlarmTests(LiveWorkerCase):
    def test_the_alarms_of_a_turn_are_recorded_for_the_chat_that_caused_them(self):
        self.engine.results = [with_alerts(PADS, VISIT, phone="599123456")]
        self.turn()
        self.assertEqual(len(self.live.turns), 1)
        channel, key, turn = self.live.turns[0]
        self.assertEqual((channel, key, turn.drafts, turn.phone),
                         ("messenger", PSID, (PADS, VISIT), "599123456"))
        self.assertEqual(len(self.graph.sends()), 1)  # and the customer got their reply

    def test_they_are_recorded_before_sending_so_a_failed_send_still_alarms(self):
        self.graph.errors["send_message"] = [
            GraphError(400, 190, 460, "OAuthException", "Error validating access token")]
        self.engine.results = [with_alerts(VISIT)]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "failed"})
        self.assertEqual([item[1] for item in self.live.turns], [PSID])

    def test_a_turn_without_an_alarm_records_nothing(self):
        self.turn()
        self.assertEqual(self.live.turns, [])

    def test_a_failed_engine_turn_records_nothing(self):
        self.engine.results = [failure("PROVIDER_UNAVAILABLE",
                                       engine_status="PROVIDER_UNAVAILABLE")]
        self.turn()
        self.assertEqual(self.live.turns, [])

    def test_a_chat_switched_off_never_reaches_the_engine_so_nothing_is_recorded(self):
        self.say("ხუნდები")
        self.pump()
        self.store.threads[PSID]["bot_enabled"] = False
        self.assertEqual(self.later(5), {PSID: "skipped_chat_off"})
        self.assertEqual((self.engine.calls, self.live.turns), ([], []))

    def test_a_database_error_never_costs_the_customer_their_reply_or_leaks_into_the_log(self):
        self.live.broken = True
        self.engine.results = [with_alerts(AlertDraft("needs_staff", "handoff", "call 599"))]
        with self.assertLogs("automotive.messenger_worker", "WARNING") as logs:
            self.turn()
        self.assertEqual(len(self.graph.sends()), 1)
        text = "\n".join(logs.output)
        self.assertIn("live_alert_failed", text)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("599", text)
        self.assertNotIn("database is down", text)

    def test_nothing_is_recorded_while_the_tables_are_missing(self):
        self.live.available = False
        self.engine.results = [with_alerts(VISIT)]
        self.turn()
        self.assertEqual(self.live.turns, [])
        self.assertEqual(len(self.graph.sends()), 1)


class WorkerWithoutLiveTests(WorkerCase):
    def test_a_store_without_a_database_records_nothing_and_the_bot_still_answers(self):
        self.assertIsNone(self.worker.live)  # the test double has no database
        self.engine.results = [with_alerts(PADS)]
        self.turn()
        self.assertEqual(len(self.graph.sends()), 1)


class EngineAlertTests(unittest.TestCase):
    """The real assistant engine with a scripted model, through the Messenger adapter."""

    def engine(self, script: Script) -> MessengerEngine:
        chat = service(script)
        engine = MessengerEngine(MessengerConversations(chat, Search()), chat.catalog_store,
                                 poll_seconds=0.005)
        self.addCleanup(engine.close)
        return engine

    def test_a_part_the_bot_could_not_find_is_handed_to_the_worker(self):
        script = Script(done(reply="ასეთი ნაწილი ვერ ვიპოვე.", outcome="nothing_found",
                             car_label="Toyota Camry 2015"))
        result = self.engine(script).respond({"memory": KNOWN}, "Camry 2015 წინა ხუნდები")
        self.assertEqual(result.status, "ok", result.public)
        self.assertEqual(
            [(d.kind, d.reason, d.summary, d.car_label) for d in result.alerts.drafts],
            [("part_not_found", "nothing_found", "Camry 2015 წინა ხუნდები",
              "Toyota Camry 2015")])

    def test_a_visit_and_the_phone_the_customer_typed_reach_the_worker(self):
        script = Script(done(
            reply="გამარჯობა! გამოგვიგზავნეთ ტელეფონის ნომერი და ჩვენი გუნდი დაგიკავშირდებათ.",
            outcome="chat", staff={"intent": "visit", "note": "wants to come tomorrow",
                                   "phone": "599123456", "missing": []}))
        result = self.engine(script).respond({"memory": KNOWN},
                                             "ხვალ მოვალ, ჩემი ნომერია 599 12 34 56")
        self.assertEqual(result.status, "ok", result.public)
        self.assertEqual([(d.kind, d.summary, d.phone) for d in result.alerts.drafts],
                         [("visit_request", "wants to come tomorrow", "599123456")])
        self.assertEqual(result.alerts.phone, "599123456")
        self.assertEqual(result.state["memory"]["phone"], "on_file")  # saved with the chat

    def test_the_customers_own_words_are_redacted_before_they_are_kept(self):
        script = Script(done(reply="ვერ ვიპოვე.", outcome="nothing_found"))
        result = self.engine(script).respond({"memory": KNOWN},
                                             "ხუნდები მანქანას 4T1BF1FK5FU000001-ზე")
        self.assertEqual(result.status, "ok", result.public)
        summary = result.alerts.drafts[0].summary
        self.assertIn("[VIN]", summary)
        self.assertNotIn("4T1BF1FK5FU000001", summary)

    def test_an_answer_with_cards_hands_over_nothing(self):
        script = Script(done(reply="გვაქვს.", outcome="offered", car_label="Toyota Camry 2015",
                             products=[product("r1")]))
        result = self.engine(script).respond({"memory": KNOWN}, "Camry 2015 წინა ხუნდები")
        self.assertEqual(result.status, "ok", result.public)
        self.assertFalse(result.alerts)

    def test_a_failed_turn_hands_over_nothing(self):
        failing = Script(anthropic.APIStatusError(
            "private", response=httpx.Response(418, request=REQUEST), body=None))
        result = self.engine(failing).respond(None, "ხუნდები")
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.alerts)


if __name__ == "__main__":
    unittest.main()
