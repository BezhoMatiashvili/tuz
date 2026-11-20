"""The website chat as staff see it: the recorder behind the visitors' ConversationStore, the
fixed message a muted visitor reads, and what the facade charges for it. Scripted model and
fakes, no network."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import psycopg

from automotive.conversations import ConversationStore
from automotive.live_alerts import AlertDraft, TurnAlerts
from automotive.live_store import RaisedAlert, web_key
from automotive.live_web import (
    ALARM_WINDOW,
    MAX_OPEN_NOISY,
    MAX_OPEN_VALUABLE,
    MUTED,
    NOISY_PER_ADDRESS,
    QUEUE_SIZE,
    VALUABLE_PER_ADDRESS,
    LiveRecorder,
)
from automotive.site_chat import PublicChat, public_turn
from automotive.site_guard import DailyBudget
from tests.test_assistant import Script, Search, done, service

SECRET = "private-marker-5d2e"  # gitleaks:allow
QUIET = ("close_failed", "close_stale_failed", "open_web_alert_counts", "web_muted_keys")
NO_WAIT = (0.0, 0.0, 0.0)  # RETRY_DELAYS for tests


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeStore:
    """The part of LiveStore the recorder uses."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.available = True
        self.fail: Exception | None = None
        self.fail_on: set[str] = set()
        self.counts = {"noisy": 0, "valuable": 0}
        self.muted: set[str] = set()
        self.open: set[tuple] = set()  # (chat key, kind) of alarms that are already open
        self.attached: list[RaisedAlert] = []

    def _record(self, name: str, *call) -> None:
        self.calls.append((name, *call))
        if self.fail or name in self.fail_on:
            raise self.fail or RuntimeError("down")

    def usable(self) -> bool:
        return self.available

    def web_customer(self, key, body, language=""):
        self._record("web_customer", key, body, language)

    def web_bot(self, key, body, *, offered=(), outcome="", car_label="", language=""):
        self._record("web_bot", key, body, list(offered), outcome, car_label, language)

    def raise_alert(self, channel, key, draft, *, allow_create=True):
        self._record("raise_alert", channel, key, draft, allow_create)
        exists = (key, draft.kind) in self.open
        if not exists and not allow_create:
            return None
        self.open.add((key, draft.kind))
        return RaisedAlert(1, not exists, True, bool(draft.phone), 1)

    def attach_phone(self, channel, key, phone, words, car_label="", *, allow_create=True):
        self._record("attach_phone", channel, key, phone, allow_create)
        return list(self.attached)

    def close_failed(self, channel, key):
        self._record("close_failed", channel, key)
        return 0

    def open_web_alert_counts(self):
        self._record("open_web_alert_counts")
        return dict(self.counts)

    def web_muted_keys(self):
        self._record("web_muted_keys")
        return set(self.muted)

    def web_prune(self, days):
        self._record("web_prune", days)

    def close_stale_failed(self, hours):
        self._record("close_stale_failed", hours)
        return 0


def turn_payload(**extra) -> dict:
    return {"status": "COMPLETE", "engine_status": "OK", "answer": "I could not find it.",
            "cards": [], "outcome": "chat", "car_label": "", **extra}


def card(name="SATO PN1521", price=85.0, status="IN_STORE") -> dict:
    return {"product": {"product_name": name, "brand": "SATO", "price": price,
                        "stock": {"store": 4}, "source_product_id": "p1"},
            "availability": "In the shop", "availability_status": status}


def kinds(store: FakeStore) -> list[str]:
    return [call[0] for call in store.calls if call[0] not in QUIET]


def staff(intent="visit", note="wants to come tomorrow"):
    return {"intent": intent, "note": note, "phone": "", "missing": []}


class RecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.clock = Clock()
        self.recorder = LiveRecorder(self.store, start=False, clock=self.clock,
                                     shop_phone=lambda: "596 33 33 74")

    def alarms(self) -> list[tuple]:
        return [call for call in self.store.calls if call[0] == "raise_alert"]

    def test_a_visitor_is_named_by_a_hash_and_the_chat_is_written_in_order(self):
        self.recorder.customer("conv-1", "do you have pads", "en")
        self.recorder.turn("conv-1", "do you have pads",
                           turn_payload(outcome="offered", cards=[card()], car_label="Honda Fit"),
                           "en")
        self.assertEqual(self.recorder.drain(), 2)
        key = web_key("conv-1")
        self.assertEqual(self.store.calls[:2], [
            ("web_customer", key, "do you have pads", "en"),
            ("web_bot", key, "I could not find it.",
             [{"name": "SATO PN1521", "brand": "SATO", "price": 85.0,
               "availability": "In the shop", "status": "IN_STORE"}],
             "offered", "Honda Fit", "en")])
        self.assertNotIn("conv-1", str(self.store.calls))

    def test_what_the_visitor_saw_offered_carries_no_stock_numbers_and_is_bounded(self):
        cards = [card(f"Part {number}") for number in range(12)] + ["junk", None]
        self.recorder.turn("c", "x", turn_payload(cards=cards, outcome="offered"), "ka")
        self.recorder.drain()
        offered = self.store.calls[0][3]
        self.assertEqual(len(offered), 8)
        self.assertEqual(set(offered[0]), {"name", "brand", "price", "availability", "status"})
        self.assertNotIn("stock", str(offered))
        self.assertNotIn("p1", str(offered))

    def test_a_part_the_bot_could_not_find_raises_an_alarm_for_the_visitor(self):
        self.recorder.turn("c", "front pads", turn_payload(outcome="nothing_found"), "en")
        self.recorder.drain()
        self.assertEqual(kinds(self.store), ["web_bot", "raise_alert"])
        _, channel, key, draft, allow = self.alarms()[0]
        self.assertEqual((channel, key, allow), ("web", web_key("c"), True))
        self.assertEqual((draft.kind, draft.reason, draft.summary),
                         ("part_not_found", "nothing_found", "front pads"))

    def test_a_visit_and_the_phone_go_through_as_one_visitors_alarm(self):
        payload = turn_payload(outcome="chat",
                               staff={**staff(), "phone": "599123456"})
        self.recorder.turn("c", "tomorrow 599 12 34 56", payload, "en")
        self.recorder.drain()
        self.assertEqual([call[3].kind for call in self.alarms()], ["visit_request"])
        attach = [call for call in self.store.calls if call[0] == "attach_phone"]
        self.assertEqual([call[3] for call in attach], ["599123456"])

    def test_a_turn_that_failed_alarms_because_the_website_has_no_stuck_flag(self):
        failed = {"status": "FAILED", "answer": "The service is temporarily unavailable.",
                  "warnings": ["CHAT_UNAVAILABLE"], "cards": []}
        self.recorder.turn("c", "pads", failed, "en")
        self.recorder.drain()
        self.assertEqual([(c[3].kind, c[3].reason) for c in self.alarms()],
                         [("needs_staff", "failed")])
        self.assertNotIn("close_failed", [call[0] for call in self.store.calls])

    def test_a_turn_that_answered_closes_the_same_visitors_failed_alarm(self):
        self.recorder.turn("c", "hello", turn_payload(), "en")
        self.recorder.drain()
        self.assertIn(("close_failed", "web", web_key("c")), self.store.calls)

    def test_an_ordinary_answer_records_the_message_and_raises_nothing(self):
        self.recorder.turn("c", "hello", turn_payload(), "en")
        self.recorder.drain()
        self.assertEqual(kinds(self.store), ["web_bot"])

    def test_an_empty_answer_with_nothing_offered_is_not_a_message(self):
        self.recorder.turn("c", "x", turn_payload(answer="", outcome="nothing_found"), "en")
        self.recorder.drain()
        self.assertEqual(kinds(self.store), ["raise_alert"])

    def test_a_canned_answer_is_kept_as_what_the_visitor_read_and_raises_nothing(self):
        self.recorder.canned("c", "The assistant is switched off.", "en")
        self.recorder.drain()
        self.assertEqual(kinds(self.store), ["web_bot"])
        self.assertEqual(self.store.calls[0][2:5], ("The assistant is switched off.", [], ""))

    # -- who may create an alarm -------------------------------------------------------------------

    def test_one_address_may_cause_only_a_few_new_part_alarms_an_hour(self):
        self.recorder.seen("c0", "10.0.0.1")
        for number in range(NOISY_PER_ADDRESS + 2):
            conversation = f"c{number}"
            self.recorder.seen(conversation, "10.0.0.1")
            self.recorder.turn(conversation, "part", turn_payload(outcome="nothing_found"), "en")
        self.recorder.seen("other", "10.0.0.2")
        self.recorder.turn("other", "part", turn_payload(outcome="nothing_found"), "en")
        self.recorder.drain()
        flags = [call[4] for call in self.alarms()]
        self.assertEqual(flags, [True] * NOISY_PER_ADDRESS + [False, False, True])
        self.assertEqual(kinds(self.store).count("web_bot"), NOISY_PER_ADDRESS + 3)  # all kept
        self.store.calls.clear()
        self.clock.now += ALARM_WINDOW + 1  # the hour has passed
        self.recorder.turn("c0", "again", turn_payload(outcome="nothing_found"), "en")
        self.recorder.drain()
        self.assertTrue(self.alarms()[0][4])

    def test_a_repeat_of_an_open_alarm_costs_the_address_nothing(self):
        self.recorder.seen("c", "10.0.0.1")
        for _ in range(NOISY_PER_ADDRESS * 3):
            self.recorder.turn("c", "same part", turn_payload(outcome="nothing_found"), "en")
        self.recorder.drain()
        self.assertEqual({call[4] for call in self.alarms()}, {True})  # never refused

    def test_alarms_that_matter_most_are_not_crowded_out_by_the_noisy_ones(self):
        self.recorder.seen("c", "10.0.0.1")
        for number in range(NOISY_PER_ADDRESS + 1):
            self.recorder.seen(f"n{number}", "10.0.0.1")
            self.recorder.turn(f"n{number}", "p", turn_payload(outcome="nothing_found"), "en")
        self.recorder.turn("c", "I will come", turn_payload(outcome="chat", staff=staff()), "en")
        self.recorder.drain()
        visit = [call for call in self.alarms() if call[3].kind == "visit_request"]
        self.assertEqual([call[4] for call in visit], [True])
        self.assertEqual(VALUABLE_PER_ADDRESS > NOISY_PER_ADDRESS, True)

    def test_the_board_has_separate_room_for_each_group(self):
        self.store.counts = {"noisy": MAX_OPEN_NOISY, "valuable": 0}
        self.recorder.turn("a", "pads", turn_payload(outcome="nothing_found"), "en")
        self.recorder.turn("b", "visit", turn_payload(outcome="chat", staff=staff()), "en")
        self.recorder.drain()
        self.assertEqual([(c[3].kind, c[4]) for c in self.alarms()],
                         [("part_not_found", False), ("visit_request", True)])
        self.store.calls.clear()
        self.store.counts = {"noisy": 0, "valuable": MAX_OPEN_VALUABLE}
        self.recorder.turn("d", "visit", turn_payload(outcome="chat", staff=staff()), "en")
        self.recorder.drain()
        self.assertEqual([call[4] for call in self.alarms()], [False])

    def test_a_phone_number_for_an_open_alarm_is_never_refused_by_the_board(self):
        self.store.counts = {"noisy": MAX_OPEN_NOISY, "valuable": MAX_OPEN_VALUABLE}
        payload = turn_payload(outcome="chat", staff={**staff("none", ""), "phone": "599123456"})
        self.recorder.turn("c", "my number 599123456", payload, "en")
        self.recorder.drain()
        attach = [call for call in self.store.calls if call[0] == "attach_phone"]
        self.assertEqual(len(attach), 1)  # asked; it refuses only to CREATE an alarm of its own
        self.assertFalse(attach[0][4])

    # -- failures ----------------------------------------------------------------------------------

    def test_a_full_queue_drops_the_record_and_never_waits(self):
        for number in range(QUEUE_SIZE + 3):
            self.recorder.customer("c", f"m{number}", "en")
        self.assertEqual(self.recorder.dropped, 3)
        self.assertEqual(self.recorder.drain(), QUEUE_SIZE)

    def test_nothing_is_written_while_the_tables_are_missing(self):
        self.store.available = False
        self.recorder.customer("c", "hello", "en")
        self.recorder.turn("c", "hello", turn_payload(outcome="nothing_found"), "en")
        with self.assertLogs("automotive.live_web", "WARNING"):
            self.recorder.drain()
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.recorder.dropped, 2)

    def test_a_database_error_is_counted_and_logged_without_text(self):
        self.store.fail = RuntimeError(SECRET)
        self.recorder.customer("c", "my number is " + SECRET, "en")
        with self.assertLogs("automotive.live_web", "WARNING") as logs:
            self.recorder.drain()
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(self.recorder.dropped, 1)

    def test_a_failed_message_write_does_not_cost_the_turn_its_alarm(self):
        self.store.fail_on = {"web_bot"}
        self.recorder.turn("c", "pads", turn_payload(outcome="nothing_found"), "en")
        with self.assertLogs("automotive.live_web", "WARNING"):
            self.recorder.drain()
        self.assertEqual(len(self.alarms()), 1)

    def test_one_draft_failing_does_not_stop_the_phone_or_the_next(self):
        self.store.fail_on = {"raise_alert"}
        payload = turn_payload(outcome="nothing_found", staff={**staff(), "phone": "599123456"})
        self.recorder.turn("c", "x 599123456", payload, "en")
        with self.assertLogs("automotive.live_web", "WARNING"):
            self.recorder.drain()
        self.assertEqual(kinds(self.store).count("web_bot"), 1)
        self.assertIn("attach_phone", [call[0] for call in self.store.calls])

    def test_the_same_problem_is_logged_once_a_minute(self):
        self.store.fail = RuntimeError("down")
        for _ in range(5):
            self.recorder.customer("c", "x", "en")
        with self.assertLogs("automotive.live_web", "WARNING") as logs:
            self.recorder.drain()
        self.assertEqual(len(logs.output), 1)

    def test_old_chats_are_pruned_after_the_retention(self):
        recorder = LiveRecorder(self.store, retention_days=45, start=False, clock=self.clock)
        recorder._prune()
        # The prune also closes the failed alarms of visitors who never came back (a day).
        self.assertEqual(self.store.calls, [("web_prune", 45), ("close_stale_failed", 24)])

    def test_a_lost_connection_is_tried_again_and_the_message_is_kept(self):
        attempts = []

        def flaky(key, body, language=""):
            attempts.append(key)
            if len(attempts) < 3:
                raise psycopg.OperationalError("connection lost")
            self.store.calls.append(("web_customer", key, body, language))

        self.store.web_customer = flaky
        self.recorder.customer("c", "hello", "en")
        with mock.patch("automotive.live_web.RETRY_DELAYS", NO_WAIT):
            self.recorder.drain()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.recorder.dropped, 0)
        self.assertEqual(kinds(self.store), ["web_customer"])

    def test_any_other_error_is_not_tried_again(self):
        self.store.fail_on = {"web_customer"}
        self.recorder.customer("c", "hello", "en")
        with mock.patch("automotive.live_web.RETRY_DELAYS", NO_WAIT), \
                self.assertLogs("automotive.live_web", "WARNING"):
            self.recorder.drain()
        self.assertEqual(kinds(self.store), ["web_customer"])  # one attempt
        self.assertEqual(self.recorder.dropped, 1)

    def test_an_unreachable_database_is_waited_for_but_missing_tables_are_not(self):
        checks = []

        def usable():
            checks.append(1)
            return len(checks) > 2

        self.store.usable = usable
        self.store.reachable = False  # what LiveStore says while it cannot connect
        self.recorder.customer("c", "hello", "en")
        with mock.patch("automotive.live_web.RETRY_DELAYS", NO_WAIT):
            self.recorder.drain()
        self.assertEqual((len(checks), kinds(self.store)), (3, ["web_customer"]))
        # Reachable but without migration 0015: dropped at once, as before.
        self.store.usable = lambda: False
        self.store.reachable = True
        self.recorder.customer("c", "again", "en")
        with mock.patch("automotive.live_web.RETRY_DELAYS", NO_WAIT), \
                self.assertLogs("automotive.live_web", "WARNING"):
            self.recorder.drain()
        self.assertEqual(self.recorder.dropped, 1)

    def test_the_writer_thread_writes_what_is_queued_before_it_stops(self):
        recorder = LiveRecorder(self.store, clock=time.monotonic)
        recorder.customer("c", "hello", "en")
        recorder.turn("c", "hello", turn_payload(), "en")
        recorder.close()
        self.assertEqual(kinds(self.store), ["web_prune", "web_customer", "web_bot"])
        self.assertIn("web_muted_keys", [call[0] for call in self.store.calls])  # refreshed


class MuteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.phone = "596 33 33 74"
        self.recorder = LiveRecorder(self.store, start=False, clock=Clock(),
                                     shop_phone=lambda: self.phone)
        self.key = web_key("c")

    def test_a_visitor_staff_have_not_muted_gets_the_bot(self):
        self.recorder.refresh()
        self.assertIsNone(self.recorder.muted_reply("c", "en"))

    def test_a_muted_visitor_reads_a_fixed_message_with_the_shop_phone(self):
        self.store.muted = {self.key}
        self.recorder.refresh()
        english = self.recorder.muted_reply("c", "en")
        georgian = self.recorder.muted_reply("c", "ka")
        sentence, call = MUTED["en"]
        self.assertEqual(english, f"{sentence} {call.replace('{phone}', self.phone)}")
        self.assertIn(self.phone, georgian)
        self.assertTrue(georgian.startswith(MUTED["ka"][0]))
        self.assertEqual(self.recorder.muted_reply("c", "auto"), georgian)  # unknown: Georgian
        self.phone = ""
        self.assertEqual(self.recorder.muted_reply("c", "en"), sentence)  # no phone, no call line

    def test_the_visitors_request_never_touches_the_database(self):
        self.store.muted = {self.key}
        self.recorder.refresh()
        self.store.calls.clear()
        for _ in range(20):
            self.recorder.muted_reply("c", "en")
            self.recorder.muted_reply("someone-else", "en")
        self.assertEqual(self.store.calls, [])

    def test_a_switch_staff_flip_is_seen_at_the_next_refresh(self):
        self.store.muted = {self.key}
        self.recorder.refresh()
        self.assertIsNotNone(self.recorder.muted_reply("c", "en"))
        self.store.muted = set()
        self.assertIsNotNone(self.recorder.muted_reply("c", "en"))  # not read yet
        self.recorder.refresh()
        self.assertIsNone(self.recorder.muted_reply("c", "en"))

    def test_a_database_that_cannot_be_read_keeps_the_last_set_and_leaks_nothing(self):
        self.store.muted = {self.key}
        self.recorder.refresh()
        self.store.fail = RuntimeError(SECRET)
        with self.assertLogs("automotive.live_web", "WARNING") as logs:
            self.recorder.refresh()
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIsNotNone(self.recorder.muted_reply("c", "en"))  # the last known stays

    def test_before_the_migration_nobody_is_muted_and_nothing_is_asked(self):
        self.store.available = False
        self.store.muted = {self.key}
        self.recorder.refresh()
        self.assertIsNone(self.recorder.muted_reply("c", "en"))
        self.assertEqual(self.store.calls, [])

    def test_a_shop_phone_that_cannot_be_read_is_left_out(self):
        def broken() -> str:
            raise RuntimeError("content down")

        recorder = LiveRecorder(self.store, start=False, clock=Clock(), shop_phone=broken)
        self.store.muted = {self.key}
        recorder.refresh()
        self.assertEqual(recorder.muted_reply("c", "en"), MUTED["en"][0])


class SpyLive:
    """What ConversationStore and PublicChat call, recorded."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.muted: str | None = None
        self.fail: set[str] = set()

    def _do(self, name: str, *values) -> None:
        self.events.append((name, *values))
        if name in self.fail:
            raise RuntimeError("recorder is broken")

    def seen(self, identifier, address):
        self._do("seen", identifier, address)

    def customer(self, identifier, message, language):
        self._do("customer", identifier, message, language)

    def canned(self, identifier, answer, language):
        self._do("canned", identifier, answer, language)

    def turn(self, identifier, message, payload, language):
        self._do("turn", identifier, message, payload, language)

    def muted_reply(self, identifier, language):
        self._do("muted", identifier, language)
        return self.muted

    def close(self):
        self.events.append(("close",))


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


NOT_FOUND = dict(reply="ასეთი ნაწილი ვერ ვიპოვე.", outcome="nothing_found",
                 car_label="Toyota Camry 2015")


class StoreCase(unittest.TestCase):
    def store(self, script: Script, live) -> ConversationStore:
        self.chat = service(script)
        store = ConversationStore(self.chat, Search(), ttl_seconds=600, turn_seconds=20,
                                  maximum=10, live=live)
        store.audience = "public"
        self.addCleanup(store.close)
        return store

    def finished(self, store: ConversationStore, identifier: str, started: dict) -> dict:
        self.assertTrue(wait_for(lambda: store.get(identifier, started["turn_id"])["status"]
                                 != "RUNNING"))
        return store.get(identifier, started["turn_id"])


class StoreHookTests(StoreCase):
    def test_the_visitors_message_and_the_bots_turn_reach_the_recorder(self):
        spy = SpyLive()
        store = self.store(Script(done(**NOT_FOUND)), spy)
        identifier = store.create()["conversation_id"]
        store.submit(identifier, "Camry 2015 წინა ხუნდები", language="ka")
        self.assertEqual(spy.events[:2], [("muted", identifier, "ka"),
                                          ("customer", identifier, "Camry 2015 წინა ხუნდები",
                                           "ka")])
        self.assertTrue(wait_for(lambda: any(event[0] == "turn" for event in spy.events)))
        _, who, message, payload, language = [e for e in spy.events if e[0] == "turn"][0]
        self.assertEqual((who, message, language), (identifier, "Camry 2015 წინა ხუნდები", "ka"))
        self.assertEqual((payload["outcome"], payload["engine_status"]), ("nothing_found", "OK"))
        self.assertIn("ვერ ვიპოვე", payload["answer"])

    def test_through_the_recorder_a_part_the_bot_cannot_find_is_one_alarm(self):
        fake = FakeStore()
        recorder = LiveRecorder(fake, start=False)
        store = self.store(Script(done(**NOT_FOUND)), recorder)
        identifier = store.create()["conversation_id"]
        recorder.seen(identifier, "10.0.0.7")
        store.submit(identifier, "Camry 2015 წინა ხუნდები", language="ka")
        self.assertTrue(wait_for(lambda: recorder._queue.qsize() >= 2))
        recorder.drain()
        self.assertEqual(kinds(fake), ["web_customer", "web_bot", "raise_alert"])
        draft = [call for call in fake.calls if call[0] == "raise_alert"][0][3]
        self.assertEqual((draft.kind, draft.reason, draft.summary, draft.car_label),
                         ("part_not_found", "nothing_found", "Camry 2015 წინა ხუნდები",
                          "Toyota Camry 2015"))
        self.assertNotIn(identifier, str(fake.calls))  # the id is a bearer secret

    def test_a_muted_visitor_reads_a_fixed_message_and_no_model_is_called(self):
        script = Script()  # a model call would find no step and fail
        spy = SpyLive()
        spy.muted = "ასისტენტი გამორთულია"
        store = self.store(script, spy)
        identifier = store.create()["conversation_id"]
        started = store.submit(identifier, "გამარჯობა", language="ka")
        self.assertEqual((started["canned"], started["status"]), (True, "RUNNING"))
        payload = store.get(identifier, started["turn_id"])
        self.assertEqual((payload["status"], payload["answer"], payload["cards"]),
                         ("COMPLETE", "ასისტენტი გამორთულია", []))
        self.assertEqual(script.requests, [])
        self.assertEqual([event[0] for event in spy.events], ["muted", "customer", "canned"])
        self.assertEqual(list(store._conversations[identifier]["history"])[-1],
                         {"question": "გამარჯობა", "answer": "", "shown": []})
        view = public_turn(payload, self.chat.catalog_store.current(), said="გამარჯობა")
        self.assertEqual((view["status"], view["answer"], view["cards"]),
                         ("COMPLETE", "ასისტენტი გამორთულია", []))

    def test_a_muted_visitor_is_spoken_to_in_the_language_their_conversation_uses(self):
        spy = SpyLive()
        store = self.store(Script(done(language="en", reply="Hello! Tell me which part you need.")),
                           spy)
        identifier = store.create()["conversation_id"]
        self.finished(store, identifier, store.submit(identifier, "hello", language="auto"))
        spy.muted = "paused"
        store.submit(identifier, "hello again", language="auto")  # the widget always says auto
        self.assertEqual([e for e in spy.events if e[0] == "muted"][-1],
                         ("muted", identifier, "en"))

    def test_a_muted_message_overtakes_a_turn_that_is_still_running(self):
        gate = threading.Event()

        def slow(request):
            gate.wait(5)
            return done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")

        spy = SpyLive()
        store = self.store(Script(slow), spy)
        identifier = store.create()["conversation_id"]
        store.submit(identifier, "ხუნდები", language="ka")
        spy.muted = "paused"
        store.submit(identifier, "გამარჯობა", language="ka")
        history = list(store._conversations[identifier]["history"])
        self.assertEqual([(h["question"], h["answer"]) for h in history],
                         [("ხუნდები", ""), ("გამარჯობა", "")])  # both stay for the assistant
        gate.set()
        time.sleep(0.3)
        self.assertNotIn("turn", [event[0] for event in spy.events])  # the overtaken turn is mute

    def test_a_visitor_is_answered_again_once_staff_switch_the_bot_on(self):
        spy = SpyLive()
        spy.muted = "paused"
        store = self.store(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")), spy)
        identifier = store.create()["conversation_id"]
        store.submit(identifier, "გამარჯობა", language="ka")
        spy.muted = None
        started = store.submit(identifier, "ხუნდები", language="ka")
        self.assertNotIn("canned", started)
        payload = self.finished(store, identifier, started)
        self.assertEqual(payload["status"], "COMPLETE")
        self.assertIn("გამარჯობა", payload["answer"])

    def test_a_switch_that_cannot_be_read_never_stops_the_bot(self):
        spy = SpyLive()
        spy.fail = {"muted"}
        store = self.store(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")), spy)
        identifier = store.create()["conversation_id"]
        started = store.submit(identifier, "გამარჯობა", language="ka")
        self.assertNotIn("canned", started)
        self.assertEqual(self.finished(store, identifier, started)["status"], "COMPLETE")

    def test_a_recorder_that_breaks_never_breaks_the_conversation(self):
        spy = SpyLive()
        spy.fail = {"customer", "turn", "seen"}
        store = self.store(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")), spy)
        identifier = store.create()["conversation_id"]
        started = store.submit(identifier, "გამარჯობა", language="ka")
        self.assertEqual(self.finished(store, identifier, started)["status"], "COMPLETE")
        self.assertTrue(wait_for(lambda: any(event[0] == "turn" for event in spy.events)))

    def test_a_store_without_a_recorder_is_what_it_always_was(self):
        store = self.store(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")), None)
        identifier = store.create()["conversation_id"]
        started = store.submit(identifier, "გამარჯობა", language="ka")
        self.assertNotIn("canned", started)
        self.assertEqual(self.finished(store, identifier, started)["status"], "COMPLETE")


class FacadeTests(StoreCase):
    def test_a_muted_turn_gives_the_model_budget_back_but_the_address_keeps_the_message(self):
        spy = SpyLive()
        spy.muted = "paused"
        store = self.store(Script(), spy)
        daily = DailyBudget(5)
        chat = PublicChat(store, self.chat.catalog_store, daily)
        identifier = chat.create("1.2.3.4")["conversation_id"]
        started = chat.submit("1.2.3.4", identifier, "hi", "ka")
        self.assertEqual(set(started), {"turn_id", "status", "language"})  # the widget's shape
        self.assertEqual(daily._used, 0)  # no model ran
        self.assertEqual(sum(chat._sent.values()), 1)  # a muted visitor still has a daily count
        self.assertIn(("seen", identifier, "1.2.3.4"), spy.events)
        view = chat.turn(identifier, started["turn_id"], 0, 5)
        self.assertEqual((view["status"], view["answer"]), ("COMPLETE", "paused"))

    def test_a_turn_that_ran_is_charged_as_before(self):
        spy = SpyLive()
        store = self.store(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")), spy)
        daily = DailyBudget(5)
        chat = PublicChat(store, self.chat.catalog_store, daily)
        identifier = chat.create("1.2.3.4")["conversation_id"]
        started = chat.submit("1.2.3.4", identifier, "hi", "ka")
        self.finished(store, identifier, started)
        self.assertEqual((daily._used, sum(chat._sent.values())), (1, 1))

    def test_closing_the_chat_closes_the_recorder(self):
        spy = SpyLive()
        chat = PublicChat(self.store(Script(), spy), self.chat.catalog_store, DailyBudget(5))
        chat.close()
        self.assertIn(("close",), spy.events)


class AlarmShapeTests(unittest.TestCase):
    def test_the_recorder_hands_the_store_drafts_in_the_shape_it_expects(self):
        fake = FakeStore()
        recorder = LiveRecorder(fake, start=False)
        recorder.turn("c", "x", turn_payload(outcome="nothing_found"), "en")
        recorder.drain()
        draft = [call for call in fake.calls if call[0] == "raise_alert"][0][3]
        self.assertIsInstance(draft, AlertDraft)
        self.assertIsInstance(TurnAlerts(), TurnAlerts)


if __name__ == "__main__":
    unittest.main()
