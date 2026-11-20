"""Daily chat review: which logged turns are replayed, what counts as a miss, money, privacy.

The logs are temporary files written through ``ChatLogger`` and the store is a double with
scripted turns, so nothing here reaches a model, a network or the real logs.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import stat
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from automotive import chat_review
from automotive.chat_log import ChatLogger
from automotive.config import Settings
from automotive.conversations import ConversationStore

NOW = 1_800_000_000.0
DAY = datetime.fromtimestamp(NOW).strftime("%Y-%m-%d")
VIN = "1HGCM82633A004352"
SECRET = "fixture-private-key-0123456789"  # gitleaks:allow (a test marker, not a key)
# What the assistant engine records about a turn that showed nothing and asked nothing.
SILENT = {"engine": "assistant", "asked": None, "outcome": "nothing_found"}
ASKED = {"engine": "assistant", "asked": "Which engine?", "outcome": "asked"}
# The older engines record none of that.
OLDER = {"rejections": {}, "hidden_unavailable": 0}
LOGGED_CARD = {"product": {"product_code": "AP104/2", "source_product_id": "7"},
               "match_source": "assistant", "fitment_status": "UNKNOWN"}
MILLION = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0}


def card(availability: str = "IN_STORE", number: str = "PN1521", **more) -> dict:
    return {"product": {"source_product_id": number, "product_code": "SHOP-0042",
                        "product_name": "ხუნდი წინა", "product_model": "CAMRY 2006->",
                        "original_code": number, "brand": "Sato"},
            "availability_status": availability, "availability": "მაღაზიაშია: 6 ც.",
            "confidence": "listed", "reason": "The row names this model and year.", **more}


def reply(*cards: dict, status: str = "COMPLETE", outcome: str = "offered", **more) -> dict:
    return {"status": status, "engine_status": "OK", "answer": "ვნახე, რაც გვაქვს.",
            "cards": list(cards), "question": None, "outcome": outcome,
            "car_label": "Toyota Camry 2015",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "diagnostics": {"engine": "assistant", "model": "claude-haiku-5-5"}, **more}


NOTHING = reply(outcome="nothing_found")


class FakeStore:
    """Scripted turns behind the part of the store the review uses."""

    def __init__(self, script: dict | None = None, default: dict | None = None,
                 events: list | None = None, name: str = "store") -> None:
        self.script, self.default = script or {}, default or NOTHING
        self.events = events if events is not None else []
        self.name = name
        self.sent: list[tuple[str, str, dict]] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._turns: dict[str, dict] = {}
        self._polls: dict[str, int] = {}

    def create(self) -> dict:
        self.created.append(f"conversation-capability-{self.name}-{len(self.created) + 1}")
        return {"conversation_id": self.created[-1], "expires_in": 600}

    def submit(self, identifier: str, message: str, **keywords) -> dict:
        self.sent.append((identifier, message, keywords))
        self.events.append((self.name, message))
        answer = self.script.get(message, self.default)
        if isinstance(answer, BaseException):
            raise answer
        turn_id = f"turn-{len(self.sent)}"
        self._turns[turn_id] = answer
        return {"conversation_id": identifier, "turn_id": turn_id, "status": "RUNNING",
                "language": "ka"}

    def get(self, identifier: str, turn_id: str, offset: int = 0, limit: int = 20) -> dict:
        self._polls[turn_id] = self._polls.get(turn_id, 0) + 1
        if self._polls[turn_id] == 1:  # A real turn is still running when first asked about.
            return {"status": "RUNNING", "cards": []}
        return json.loads(json.dumps(self._turns[turn_id]))

    def delete(self, identifier: str) -> None:
        self.deleted.append(identifier)

    def messages(self) -> list[str]:
        return [message for _, message, _ in self.sent]


class ReviewCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.out = self.root / "reviews"
        self.paths = {"staff": self.root / "logs" / "chat.sqlite3",
                      "site": self.root / "logs" / "site.sqlite3"}
        self.loggers = {log: ChatLogger(path, hmac_key="k" * 32)
                        for log, path in self.paths.items()}
        for logger in self.loggers.values():
            self.addCleanup(logger.close)
        self.settings = Settings(chat_log_path=str(self.paths["staff"]),
                                 site_chat_log_path=str(self.paths["site"]),
                                 anthropic_api_key=SECRET, knowledge_enabled=True,
                                 catalog_api_key="paid-catalogue-key")
        self.built: list[tuple] = []
        self.closed: list[bool] = []

    def log(self, log: str, conversation: str, question: str, *, hours: float = 1.0,
            status: str = "COMPLETE", answer: str = "ვერ მოიძებნა.", cards: int = 0,
            engine_status: str = "OK", diagnostics: dict | None = None,
            vin: str | None = None, vehicle: dict | None = None) -> None:
        """One logged turn, written the way the application writes it."""
        payload = {"answer": answer, "engine_status": engine_status,
                   "vehicle": vehicle or {}, "cards": [LOGGED_CARD] * cards,
                   "diagnostics": dict(SILENT if diagnostics is None else diagnostics)}
        self.loggers[log].turn(conversation, question=question, status=status, language="ka",
                               started_at=NOW - hours * 3600, duration_ms=5, payload=payload,
                               vin=vin)
        self.loggers[log].flush()

    def review(self, *argv: str, stores: dict | None = None,
               settings: Settings | None = None, build=None) -> tuple[int, str, str]:
        args = chat_review.build_parser().parse_args(["--out", str(self.out), *argv])

        def default_build(review_settings, wanted):
            self.built.append((review_settings, set(wanted)))
            return dict(stores or {}), lambda: self.closed.append(True)

        out, err = io.StringIO(), io.StringIO()
        code = chat_review.run(args, settings or self.settings, build=build or default_build,
                               clock=lambda: NOW, out=out, err=err, sleep=lambda seconds: None)
        return code, out.getvalue(), err.getvalue()

    def report(self) -> dict:
        return json.loads((self.out / f"chat-review-{DAY}.json").read_text(encoding="utf-8"))

    def markdown(self) -> str:
        return (self.out / f"chat-review-{DAY}.md").read_text(encoding="utf-8")


class SelectionTests(ReviewCase):
    def test_a_turn_is_taken_when_it_ended_with_no_parts_and_no_question(self) -> None:
        self.log("staff", "a", "front pads camry")
        self.log("staff", "b", "before the cut-off", hours=30)
        self.log("staff", "c", "overtaken", status="CANCELLED")
        self.log("staff", "d", "got a part", cards=1)
        self.log("staff", "e", "asked, by the engine's record", answer="Tell me the engine.",
                 engine_status="CLARIFICATION", diagnostics=ASKED)
        self.log("staff", "f", "older engine, status says it asked", answer="Front or rear",
                 engine_status="CLARIFICATION", diagnostics=OLDER)
        self.log("staff", "g", "older engine, answer ends with a question mark",
                 answer="Which make is it?", diagnostics=OLDER)
        self.log("staff", "h", "older engine, plain answer",
                 answer="Could not confirm an offer.", diagnostics=OLDER)
        self.log("staff", "i", "the record beats the text",
                 answer="Nothing found. Anything else?")
        self.log("staff", "j", "failed turn", status="FAILED", engine_status="TIMEOUT",
                 answer="Sorry, try again.",
                 diagnostics={"engine": "assistant", "asked": None, "outcome": "handoff"})
        self.log("staff", "k", "   ")

        read = chat_review.read_log("staff", str(self.paths["staff"]), NOW - 24 * 3600)

        self.assertEqual(read.error, "")
        self.assertEqual(len(read.turns), 10)  # everything but the turn before the cut-off
        taken = {turn.question: turn.asked_by for turn in read.turns if turn.nothing_shown}
        self.assertEqual(taken, {
            "front pads camry": chat_review.RECORD,
            "older engine, plain answer": chat_review.APPROXIMATION,
            "the record beats the text": chat_review.RECORD,
            "failed turn": chat_review.RECORD})

    def test_the_report_says_how_no_question_was_decided(self) -> None:
        self.log("staff", "a", "by the record")
        self.log("staff", "g", "left out", answer="Which make is it?", diagnostics=OLDER)
        self.log("staff", "h", "approximated", answer="Could not confirm.", diagnostics=OLDER)

        code, _, _ = self.review(stores={"staff": FakeStore()})

        self.assertEqual(code, 0)
        self.assertEqual(self.report()["question_rule"],
                         {"record": 1, "approximation": 1, "left_out_by_approximation": 1})
        self.assertIn("For 1 turn of older engines it does not", self.markdown())

    def test_log_is_opened_read_only(self) -> None:
        self.log("staff", "a", "front pads camry")
        with mock.patch.object(chat_review.sqlite3, "connect", wraps=sqlite3.connect) as opened:
            chat_review.read_log("staff", str(self.paths["staff"]), NOW - 3600 * 24)
        (target,), keywords = opened.call_args
        self.assertTrue(target.startswith("file:///"))
        self.assertTrue(target.endswith("?mode=ro"))
        self.assertIs(keywords["uri"], True)

    def test_missing_logs_are_named_and_end_the_run(self) -> None:
        settings = Settings(chat_log_path=str(self.root / "none.sqlite3"),
                            site_chat_log_path="")
        code, out, err = self.review(settings=settings)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("staff log: there is no log file", err)
        self.assertIn("site log: logging is switched off", err)
        self.assertFalse(self.out.exists())

    def test_one_readable_log_is_enough(self) -> None:
        self.log("site", "a", "front pads camry")
        settings = Settings(chat_log_path="", site_chat_log_path=str(self.paths["site"]))
        code, out, _ = self.review(settings=settings, stores={"site": FakeStore()})
        self.assertEqual(code, 0)
        self.assertEqual(self.report()["logs"]["site"], "read")
        self.assertIn("The staff log was not read", self.markdown())
        self.assertIn("replayed 1:", out)


class ReplayTests(ReviewCase):
    def test_each_conversation_is_replayed_in_order_in_a_fresh_one(self) -> None:
        events: list = []
        staff, site = FakeStore(events=events, name="staff"), FakeStore(events=events,
                                                                        name="site")
        self.log("staff", "one", "გამარჯობა", hours=26, answer="გამარჯობა!")
        self.log("staff", "one", "Toyota Camry 2015", hours=3, answer="რა ნაწილი?",
                 engine_status="CLARIFICATION", diagnostics=ASKED)
        self.log("staff", "one", "წინა ხუნდები", hours=2.9)
        self.log("staff", "one", "მადლობა", hours=2.8, cards=1)
        self.log("staff", "three", "ზეთის ფილტრი", hours=2)
        self.log("site", "two", "W212 ზეთის ფილტრი", hours=1)

        code, out, _ = self.review(stores={"staff": staff, "site": site})

        self.assertEqual(code, 0)
        # The newest conversation first; inside one, the messages as they were sent, from
        # the first (even one older than the window) to the last turn to judge.
        self.assertEqual(events, [("site", "W212 ზეთის ფილტრი"), ("staff", "ზეთის ფილტრი"),
                                  ("staff", "გამარჯობა"), ("staff", "Toyota Camry 2015"),
                                  ("staff", "წინა ხუნდები")])
        self.assertEqual([identifier for identifier, _, _ in staff.sent],
                         [staff.created[0]] + [staff.created[1]] * 3)
        self.assertEqual(len(set(staff.created + site.created)), 3)
        self.assertEqual(staff.deleted + site.deleted, staff.created + site.created)
        self.assertTrue(all(keywords == {"language": "auto"}
                            for _, _, keywords in staff.sent + site.sent))
        report = self.report()
        self.assertEqual(report["counts"]["all"]["replayed"], 3)
        self.assertEqual(report["replayed_messages"], 5)
        self.assertEqual(self.built[0][1], {"staff", "site"})
        self.assertEqual(self.closed, [True])
        self.assertEqual(out.count("\n"), 3)

    def test_a_miss_is_a_replay_with_a_part_in_the_store_or_the_warehouse(self) -> None:
        for hours, text in ((4, "in the store"), (3, "at the warehouse"), (2, "sold out"),
                            (1, "nothing at all")):
            self.log("staff", text, text, hours=hours, answer="could not confirm an offer")
        store = FakeStore({
            "in the store": reply(card("UNAVAILABLE", "X1"), card("WITHIN_1_2_HOURS", "W2"),
                                  card("IN_STORE", "PN1521"),
                                  *(card("IN_STORE", f"S{number}") for number in range(4))),
            "at the warehouse": reply(card("WITHIN_1_2_HOURS", "W9")),
            "sold out": reply(card("UNAVAILABLE"), card("UNKNOWN"), outcome="sold_out"),
        })

        code, out, _ = self.review(stores={"staff": store})

        self.assertEqual(code, 0)  # A miss is a finding, not a failure of the command.
        report = self.report()
        self.assertEqual({key: report["counts"]["staff"][key] for key in (
            "in_window", "nothing_shown", "replayed", "miss", "needs_vin", "nothing_missed",
            "replay_failed", "unchecked")}, {
            "in_window": 4, "nothing_shown": 4, "replayed": 4, "miss": 2, "needs_vin": 0,
            "nothing_missed": 2, "replay_failed": 0, "unchecked": 0})
        first, second = report["misses"]
        self.assertEqual(first["customer"], "in the store")
        self.assertEqual(first["session"], ChatLogger.session_key("in the store"))
        self.assertEqual(first["time"], chat_review._iso(NOW - 4 * 3600))
        self.assertEqual(first["logged_answer"], "could not confirm an offer")
        self.assertEqual(first["replay_reply"], "ვნახე, რაც გვაქვს.")
        self.assertEqual(first["car_label"], "Toyota Camry 2015")
        self.assertEqual((first["cards_in_stock"], first["cards_total"]), (6, 7))
        # Five parts at most, those in the store before those at the warehouse.
        self.assertEqual([item["number"] for item in first["cards"]],
                         ["PN1521", "S0", "S1", "S2", "S3"])
        self.assertEqual(first["cards"][0], {
            "name": "ხუნდი წინა", "brand": "Sato", "number": "PN1521",
            "vehicle": "CAMRY 2006->", "availability": "IN_STORE", "confidence": "listed",
            "reason": "The row names this model and year."})
        self.assertEqual(second["cards"][0]["availability"], "WITHIN_1_2_HOURS")
        self.assertEqual({item["verdict"] for item in report["other_turns"]},
                         {"nothing_missed"})
        self.assertIn("replayed 4: misses 2, needs VIN 0, nothing missed 2;", out)

    def test_the_markdown_has_a_summary_table_and_a_section_for_each_miss(self) -> None:
        self.log("staff", "s", "pads | for <b>camry</b>", answer="could not confirm")
        store = FakeStore({"pads | for <b>camry</b>": reply(
            card(reason="fits | the `2006->` range"))})

        self.review(stores={"staff": store})

        text = self.markdown()
        self.assertIn(f"# Chat review {DAY}", text)
        self.assertIn("| | staff | site | all |", text)
        self.assertIn("| **Misses** | 1 | 0 | 1 |", text)
        self.assertIn("## Miss 1: staff log", text)
        self.assertNotIn("Earlier in the conversation", text)
        self.assertIn("> pads | for &lt;b>camry&lt;/b>", text)
        self.assertIn("> could not confirm", text)
        self.assertIn("> ვნახე, რაც გვაქვს.", text)
        self.assertIn("| ხუნდი წინა | Sato | PN1521 | CAMRY 2006-> | in the store | listed | "
                      "fits \\| the '2006->' range |", text)
        self.assertNotIn("## Not judged", text)

    def test_a_miss_is_reported_with_what_the_customer_said_before(self) -> None:
        self.log("staff", "one", "Toyota Camry 2015", hours=3, answer="რა ნაწილი?",
                 engine_status="CLARIFICATION", diagnostics=ASKED)
        self.log("staff", "one", "წინა", hours=2.9, cards=1)
        self.log("staff", "one", "ორივე", hours=2.8)
        store = FakeStore({"ორივე": reply(card())})

        self.review(stores={"staff": store})

        (miss,) = self.report()["misses"]
        self.assertEqual(miss["earlier_messages"], ["Toyota Camry 2015", "წინა"])
        self.assertEqual(miss["customer"], "ორივე")
        self.assertIn("**Earlier in the conversation**\n\n> Toyota Camry 2015\n> წინა\n\n"
                      "**Customer**\n\n> ორივე", self.markdown())

    def test_a_turn_with_a_vin_is_not_a_miss_and_not_checked(self) -> None:
        self.log("site", "typed", "ჰაერის ფილტრი [VIN]", hours=5)
        self.log("site", "typed", "უკანა ხუნდებიც", hours=4.9)
        self.log("site", "form", "წინა ხუნდები", hours=4, vin=VIN)
        self.log("site", "oil", "[VIN] 5W-30 ზეთი", hours=3)
        self.log("site", "plain", "ხუნდები Camry", hours=2)
        asks = reply(outcome="asked", engine_status="CLARIFICATION",
                     question={"text": "რა მანქანაა?", "options": []})
        store = FakeStore({"ჰაერის ფილტრი [VIN]": asks, "[VIN] 5W-30 ზეთი": reply(card())})

        self.review(stores={"site": store})

        report = self.report()
        verdicts = {(item["turn"], item["verdict"], item["had_vin"])
                    for item in report["other_turns"] + report["misses"]}
        self.assertEqual(verdicts, {
            (1, "needs_vin", True),  # the VIN was typed: the replay only sees [VIN]
            (2, "needs_vin", True),  # the conversation still had that VIN
            (3, "needs_vin", True),  # the VIN came beside the message: only its hash is logged
            (4, "miss", True),       # stock found without the car is still a miss
            (5, "nothing_missed", False)})
        self.assertEqual(report["counts"]["site"]["needs_vin"], 3)
        self.assertEqual(report["counts"]["site"]["replayed"], 5)
        # The masked message is sent as it was logged, never with a VIN in its place.
        self.assertIn("ჰაერის ფილტრი [VIN]", store.messages())
        self.assertIn("[VIN] 5W-30 ზეთი", store.messages())
        self.assertNotIn(VIN, json.dumps(store.sent, ensure_ascii=False))
        text = self.markdown()
        self.assertIn("## Not judged", text)
        self.assertIn("| needs vin | asked |", text)

    def test_the_staff_replay_gets_the_car_of_the_log_and_the_public_one_does_not(self) -> None:
        car = {"make": "JEEP", "model": "Cherokee", "model_year": 2019, "trim": "",
               "odd": {"nested": 1}, "note": f"decoded from {VIN}"}
        self.log("staff", "s", "ზეთის ფილტრი", vehicle=car, vin=VIN)
        self.log("site", "p", "ზეთის ფილტრი", vehicle=car)
        staff = FakeStore({"ზეთის ფილტრი": reply(card())})
        site = FakeStore()

        self.review(stores={"staff": staff, "site": site})

        # Plain values only, and a VIN in one of them is masked like any other text.
        self.assertEqual(staff.sent[0][2], {"language": "auto", "vehicle": {
            "make": "JEEP", "model": "Cherokee", "model_year": 2019,
            "note": "decoded from [VIN]"}})
        self.assertEqual(site.sent[0][2], {"language": "auto"})
        (miss,) = self.report()["misses"]
        self.assertEqual(miss["car_from_log"], "JEEP Cherokee 2019")
        self.assertIn("The turn had a VIN. The replay was given the car the log recorded: "
                      "JEEP Cherokee 2019.", self.markdown())

    def test_the_real_store_takes_the_replay(self) -> None:
        class Engine:
            settings = Settings(chat_engine="assistant")

            def __init__(self) -> None:
                self.seen: list[tuple] = []

            def respond_conversation(self, message, **keywords):
                self.seen.append((message, keywords.get("vehicle"), keywords.get("vin")))
                return {"status": "OK", "answer": "გვაქვს.", "language": "ka", "vehicle": {},
                        "cards": [card()], "supplier_only": [], "results": [], "warnings": [],
                        "usage": dict(MILLION), "question": None, "outcome": "offered",
                        "car_label": "Jeep Cherokee 2019", "pending": None,
                        "diagnostics": {"engine": "assistant", "model": "claude-haiku-5-5"}}

        engine = Engine()
        store = ConversationStore(engine, None)
        self.addCleanup(store.close)
        self.log("staff", "s", "[VIN] ზეთის ფილტრი", vin=VIN,
                 vehicle={"make": "JEEP", "model": "Cherokee", "model_year": 2019})
        args = chat_review.build_parser().parse_args(["--out", str(self.out)])

        code = chat_review.run(
            args, self.settings, build=lambda settings, wanted: ({"staff": store}, list),
            clock=lambda: NOW, out=io.StringIO(), err=io.StringIO(),
            sleep=lambda seconds: time.sleep(0.005))

        self.assertEqual(code, 0)
        self.assertEqual(engine.seen, [("[VIN] ზეთის ფილტრი", {
            "make": "JEEP", "model": "Cherokee", "model_year": 2019}, None)])
        report = self.report()
        self.assertEqual(report["counts"]["staff"]["miss"], 1)
        self.assertEqual(report["misses"][0]["car_label"], "Jeep Cherokee 2019")
        self.assertEqual(report["cost_usd"], 0.1)
        self.assertEqual(store._conversations, {})  # the replay's conversation is deleted

    def test_a_conversation_that_cannot_be_replayed_does_not_end_the_run(self) -> None:
        self.log("staff", "broken", "breaks the store", hours=2)
        self.log("staff", "fine", "works", hours=3)
        store = FakeStore({"breaks the store": RuntimeError(f"boom {VIN}"),
                           "works": reply(card())})

        code, _, err = self.review(stores={"staff": store})

        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual(report["counts"]["staff"]["replay_failed"], 1)
        self.assertEqual(report["counts"]["staff"]["miss"], 1)
        (failed,) = report["other_turns"]
        self.assertEqual((failed["verdict"], failed["detail"]),
                         ("replay_failed", "RuntimeError"))
        self.assertIn("could not be replayed (RuntimeError)", err)
        self.assertEqual(store.deleted, store.created)

    def test_replays_that_keep_failing_stop_the_run(self) -> None:
        for number in range(5):
            self.log("staff", f"c{number}", f"message {number}", hours=number + 1)
        down = reply(status="FAILED", outcome="handoff", engine_status="PROVIDER_UNAVAILABLE")
        store = FakeStore(default=down)

        code, out, _ = self.review(stores={"staff": store})

        self.assertEqual(code, 1)
        self.assertEqual(len(store.sent), chat_review.MAX_FAILURES)
        report = self.report()
        self.assertEqual(report["stopped"], "engine")
        self.assertEqual(report["counts"]["staff"]["replay_failed"], 3)
        self.assertEqual(report["counts"]["staff"]["replayed"], 0)
        self.assertEqual([item["reason"] for item in report["unchecked"]], ["engine"] * 2)
        self.assertEqual(report["other_turns"][0]["detail"], "PROVIDER_UNAVAILABLE")
        self.assertIn("replay failed 3, left unchecked 2 (the engine was not answering)", out)

    def test_a_turn_that_never_ends_is_a_failed_replay(self) -> None:
        turn = chat_review.LoggedTurn(
            log="staff", row_id=1, session="s", started_at=NOW, question="pads", answer="",
            status="COMPLETE", cards=0, vin=False, car={}, asked=False,
            asked_by=chat_review.RECORD)
        clock = iter(range(0, 1000, 50))
        store = SimpleNamespace(
            submit=lambda identifier, message, **keywords: {"turn_id": "t"},
            get=lambda identifier, turn_id, offset, limit: {"status": "RUNNING"})

        payload, seconds = chat_review.ask(store, "c", turn, car={}, timeout=120,
                                           sleep=lambda seconds: None,
                                           monotonic=lambda: next(clock))

        self.assertEqual(seconds, 200)
        record = chat_review.judge(turn, payload)
        self.assertEqual((record["verdict"], record["detail"]), ("replay_failed", "TIMED_OUT"))

    def test_a_stopped_run_still_writes_what_it_judged(self) -> None:
        self.log("staff", "first", "first", hours=1)
        self.log("staff", "second", "second", hours=2)
        store = FakeStore({"first": reply(card()), "second": KeyboardInterrupt()})

        code, out, _ = self.review(stores={"staff": store})

        self.assertEqual(code, 130)
        report = self.report()
        self.assertEqual(report["stopped"], "interrupted")
        self.assertEqual(report["counts"]["staff"]["miss"], 1)
        self.assertEqual([item["reason"] for item in report["unchecked"]], ["interrupted"])
        self.assertEqual(self.closed, [True])
        self.assertEqual(store.deleted, store.created)
        self.assertIn("left unchecked 1 (the run was stopped)", out)

    def test_a_log_whose_chat_is_not_installed_is_left_unchecked(self) -> None:
        self.log("site", "p", "public request")
        self.log("staff", "s", "staff request")

        code, _, _ = self.review(stores={"staff": FakeStore()})

        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual(report["counts"]["site"]["unchecked"], 1)
        self.assertEqual(report["unchecked"][0]["reason"], "no_store")
        self.assertEqual(report["counts"]["staff"]["replayed"], 1)

    def test_nothing_to_replay_builds_nothing_and_still_reports(self) -> None:
        self.log("staff", "a", "got a part", cards=1)

        def build(settings, wanted):
            raise AssertionError("the application must not be built")

        code, out, _ = self.review(build=build)

        self.assertEqual(code, 0)
        self.assertEqual(self.report()["counts"]["all"], {
            "in_window": 1, "nothing_shown": 0, "replayed": 0, "miss": 0, "needs_vin": 0,
            "nothing_missed": 0, "replay_failed": 0, "unchecked": 0})
        self.assertIn("0 of 1 logged turn in the last 24 hours", out)

    def test_an_application_that_cannot_be_built_is_reported(self) -> None:
        self.log("staff", "a", "front pads camry")

        def build(settings, wanted):
            raise chat_review.ReviewError("The catalogue could not be loaded.")

        code, out, err = self.review(build=build)

        self.assertEqual(code, 1)
        self.assertIn("The catalogue could not be loaded.", err)
        self.assertEqual(self.report()["unchecked"][0]["reason"], "engine")
        self.assertEqual(self.report()["error"], "The catalogue could not be loaded.")
        self.assertIn("- Nothing was replayed: The catalogue could not be loaded.",
                      self.markdown())
        self.assertIn("left unchecked 1 (the engine was not answering)", out)


class LimitAndBudgetTests(ReviewCase):
    def test_the_limit_takes_the_newest_conversations(self) -> None:
        for hours in (1, 2, 3):
            self.log("staff", f"c{hours}", f"{hours} hours ago", hours=hours)
        store = FakeStore()

        _, out, _ = self.review("--limit", "2", stores={"staff": store})

        self.assertEqual(store.messages(), ["1 hours ago", "2 hours ago"])
        report = self.report()
        self.assertEqual([(item["turn"], item["reason"]) for item in report["unchecked"]],
                         [(3, "limit")])
        self.assertIn("left unchecked 1;", out)
        self.assertIn("over the limit of turns for one run", self.markdown())

    def test_a_conversation_cut_by_the_limit_is_replayed_no_further_than_judged(self) -> None:
        self.log("staff", "long", "first request", hours=3)
        self.log("staff", "long", "second request", hours=2)
        store = FakeStore()

        self.review("--limit", "1", stores={"staff": store})

        self.assertEqual(store.messages(), ["first request"])
        self.assertEqual(self.report()["counts"]["staff"]["unchecked"], 1)

    def test_the_budget_stops_the_run_and_counts_what_is_left(self) -> None:
        for hours in (1, 2, 3):
            self.log("staff", f"c{hours}", f"{hours} hours ago", hours=hours)
        store = FakeStore(default=reply(outcome="nothing_found", usage=dict(MILLION)))

        code, out, _ = self.review("--budget-usd", "0.15", stores={"staff": store})

        self.assertEqual(code, 0)
        self.assertEqual(len(store.sent), 2)  # $0.10 each on Haiku: the third is not sent
        report = self.report()
        self.assertEqual((report["cost_usd"], report["stopped"]), (0.2, "budget"))
        self.assertEqual(report["counts"]["staff"]["replayed"], 2)
        self.assertEqual([item["reason"] for item in report["unchecked"]], ["budget"])
        self.assertIn("left unchecked 1 (the budget was reached); cost $0.2000 of $0.15", out)
        self.assertIn("Left unchecked: 1 turn (the budget was reached)", self.markdown())

    def test_cost_follows_the_price_table_of_the_model_family(self) -> None:
        each = {key: 1_000_000 for key in chat_review.USAGE}
        self.assertAlmostEqual(chat_review.cost("claude-haiku-5-5", each), 0.735)
        self.assertAlmostEqual(chat_review.cost("claude-sonnet-5-5", each), 14.7)
        # A model the table does not name is counted at the dearest rates.
        self.assertAlmostEqual(chat_review.cost("some-other-model", each), 14.7)
        self.assertEqual(chat_review.cost("claude-haiku-5-5", None), 0.0)
        self.assertEqual(chat_review.cost("claude-haiku-5-5",
                                          {"input_tokens": "many", "output_tokens": -5,
                                           "cache_read_input_tokens": True}), 0.0)

    def test_the_model_option_sets_the_model_and_its_prices(self) -> None:
        self.log("staff", "a", "front pads camry")
        store = FakeStore(default=reply(outcome="nothing_found", usage=dict(MILLION),
                                        diagnostics={}))

        _, out, _ = self.review("--model", "claude-sonnet-5-5", stores={"staff": store})

        self.assertEqual(self.built[0][0].assistant_model, "claude-sonnet-5-5")
        self.assertEqual(self.report()["cost_usd"], 2.0)
        self.assertIn("on claude-sonnet-5-5", out)

    def test_a_dry_run_lists_and_estimates_and_touches_nothing(self) -> None:
        self.log("staff", "one", "Toyota Camry 2015", hours=3, answer="რა ნაწილი?",
                 engine_status="CLARIFICATION", diagnostics=ASKED)
        self.log("staff", "one", "წინა ხუნდები", hours=2.9)
        self.log("site", "two", "W212 ზეთის ფილტრი", hours=1)

        def build(settings, wanted):
            raise AssertionError("a dry run must not build the application")

        code, out, err = self.review("--dry-run", build=build)

        self.assertEqual((code, err), (0, ""))
        self.assertFalse(self.out.exists())
        lines = out.splitlines()
        self.assertEqual(lines[0], f"site  {chat_review._stamp(NOW - 3600)}  session "
                                   f"{ChatLogger.session_key('two')}  1 message, 1 to judge")
        self.assertIn("     1* W212 ზეთის ფილტრი", lines)
        self.assertIn("     1  Toyota Camry 2015", lines)
        self.assertIn("     2* წინა ხუნდები", lines)
        self.assertTrue(lines[-3].startswith(
            "dry run: 2 of 2 logged turns with no parts and no question would be judged "
            "(limit 40), in 2 conversations and 3 replayed messages (* marks"))
        each = chat_review.cost("claude-haiku-5-5", chat_review.ASSUMED_MESSAGE)
        self.assertEqual(
            lines[-2],
            f"estimated cost: about ${3 * each:.2f} on claude-haiku-5-5 (3 messages at an "
            f"assumed ${each:.4f} each, haiku rates); the budget of $1.00 covers about "
            f"{int(1 // each)} messages")
        self.assertEqual(lines[-1], "nothing was sent to the model and no report was written")


class PrivacyTests(ReviewCase):
    def test_reports_are_private_files(self) -> None:
        self.log("staff", "a", "front pads camry")
        previous = os.umask(0)
        try:
            self.review(stores={"staff": FakeStore({"front pads camry": reply(card())})})
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(sorted(item.name for item in self.out.iterdir()),
                         [f"chat-review-{DAY}.json", f"chat-review-{DAY}.md"])
        for item in self.out.iterdir():
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)

    def test_a_second_run_replaces_the_report_and_keeps_it_private(self) -> None:
        self.log("staff", "a", "front pads camry")
        self.review(stores={"staff": FakeStore()})
        os.chmod(self.out / f"chat-review-{DAY}.md", 0o644)
        self.review(stores={"staff": FakeStore({"front pads camry": reply(card())})})
        self.assertEqual(self.report()["counts"]["staff"]["miss"], 1)
        self.assertEqual(stat.S_IMODE((self.out / f"chat-review-{DAY}.md").stat().st_mode),
                         0o600)

    def test_no_vin_secret_or_conversation_id_reaches_a_report_or_the_screen(self) -> None:
        self.log("staff", "typed", "pads [VIN]", hours=2, vin=VIN)
        # An old log file may hold what today's redaction would not let through.
        self.log("staff", "raw", f"filter {VIN}", hours=1, answer=f"No offer for {VIN}.")
        leaky = reply(card(reason=f"fits {VIN}"), answer=f"For {VIN}: key {SECRET}.",
                      car_label=f"Camry {VIN}")
        store = FakeStore(default=leaky)

        code, out, err = self.review(stores={"staff": store})

        self.assertEqual(code, 0)
        self.assertEqual(store.messages(), ["filter [VIN]", "pads [VIN]"])
        written = self.markdown() + json.dumps(self.report(), ensure_ascii=False) + out + err
        for private in (VIN, SECRET, "conversation-capability", "paid-catalogue-key"):
            self.assertNotIn(private, written)
        first = self.report()["misses"][0]
        self.assertEqual(first["customer"], "pads [VIN]")
        self.assertEqual(first["replay_reply"], "For [VIN]: key [REDACTED].")
        self.assertEqual(first["car_label"], "Camry [VIN]")
        self.assertEqual(first["cards"][0]["reason"], "fits [VIN]")
        self.assertEqual(self.report()["misses"][1]["logged_answer"], "No offer for [VIN].")

    def test_a_dry_run_shows_no_vin_either(self) -> None:
        self.log("staff", "raw", f"filter {VIN} {SECRET}")
        _, out, _ = self.review("--dry-run")
        self.assertIn("     1* filter [VIN] [REDACTED]", out.splitlines())
        self.assertNotIn(VIN, out)

    def test_secrets_are_the_settings_hidden_from_repr(self) -> None:
        settings = Settings(anthropic_api_key="a-key", operator_token="a-token",
                            database_url="postgresql://u:p@h/d", anthropic_model="public")
        self.assertEqual(set(chat_review.secrets_of(settings)),
                         {"a-key", "a-token", "postgresql://u:p@h/d"})
        self.assertEqual(chat_review.secrets_of(object()), ())
        # Values are redacted; the report's own keys are never rewritten.
        self.assertEqual(chat_review.scrub({"cost_usd": 1.5, "notes": ["the cost", VIN]},
                                           ("cost",)),
                         {"cost_usd": 1.5, "notes": ["the [REDACTED]", "[VIN]"]})


class ApplicationTests(ReviewCase):
    def test_replays_use_a_larger_budget_and_leave_no_trace(self) -> None:
        review = chat_review.review_settings(self.settings, "claude-sonnet-5-5")
        self.assertEqual(
            (review.chat_engine, review.chat_log_path, review.site_chat_log_path,
             review.assistant_max_requests, review.assistant_turn_seconds,
             review.assistant_model, review.knowledge_enabled, review.catalog_api_key),
            ("assistant", "", "", 8, 90.0, "claude-sonnet-5-5", False, ""))
        # The store's own deadline must not end a turn before the engine's does.
        self.assertGreater(review.chat_turn_seconds, review.assistant_turn_seconds)
        self.assertEqual(review.anthropic_api_key, SECRET)
        self.assertEqual(chat_review.review_settings(self.settings).assistant_model, "")
        self.assertEqual(self.settings.chat_log_path, str(self.paths["staff"]))

    def fake_app(self, *, loaded: bool = True, public: bool = True) -> tuple:
        closed: list[str] = []

        class Catalog:
            def __init__(self) -> None:
                self.loaded, self.refreshed = False, 0

            def refresh(self) -> bool:
                self.refreshed += 1
                self.loaded = loaded
                return loaded

        def part(name: str, **more) -> SimpleNamespace:
            return SimpleNamespace(close=lambda: closed.append(name), **more)

        staff = part("staff", turn_seconds=75.0, search_service=part("search"),
                     chat_service=part("chat"))
        visitor = SimpleNamespace(turn_seconds=60.0)
        catalog = Catalog()
        site = SimpleNamespace(catalog_store=catalog,
                               chat=part("public", store=visitor) if public else None)
        app = SimpleNamespace(state=SimpleNamespace(conversations=staff, site=site))
        return app, staff, visitor, catalog, closed

    def test_the_stores_are_the_ones_create_app_builds(self) -> None:
        app, staff, visitor, catalog, closed = self.fake_app()
        review = chat_review.review_settings(self.settings)
        with mock.patch("automotive.api.create_app", return_value=app) as create_app:
            stores, close = chat_review.build_stores(review, {"staff", "site"})
        create_app.assert_called_once_with(settings=review)
        self.assertEqual(stores, {"staff": staff, "site": visitor})
        self.assertEqual(catalog.refreshed, 1)
        self.assertEqual((staff.turn_seconds, visitor.turn_seconds), (105.0, 105.0))
        self.assertEqual(closed, [])
        close()
        self.assertEqual(closed, ["public", "staff", "search", "chat"])

    def test_only_the_wanted_and_installed_stores_are_returned(self) -> None:
        app, staff, _, _, _ = self.fake_app(public=False)
        with mock.patch("automotive.api.create_app", return_value=app):
            stores, _ = chat_review.build_stores(self.settings, {"staff", "site"})
            self.assertEqual(stores, {"staff": staff})
            stores, _ = chat_review.build_stores(self.settings, {"site"})
            self.assertEqual(stores, {})

    def test_no_key_or_no_catalogue_is_an_error_that_names_nothing_private(self) -> None:
        with self.assertRaisesRegex(chat_review.ReviewError, "ANTHROPIC_API_KEY is not set"):
            chat_review.build_stores(Settings(), {"staff"})
        app, _, _, _, closed = self.fake_app(loaded=False)
        with mock.patch("automotive.api.create_app", return_value=app), \
                self.assertRaisesRegex(chat_review.ReviewError, "could not be loaded"):
            chat_review.build_stores(self.settings, {"staff"})
        self.assertEqual(closed, ["public", "staff", "search", "chat"])
        app, _, _, catalog, _ = self.fake_app()
        catalog.refresh = mock.Mock(side_effect=RuntimeError("postgresql://user:pw@host/db"))
        with mock.patch("automotive.api.create_app", return_value=app), \
                self.assertRaises(chat_review.ReviewError) as raised:
            chat_review.build_stores(self.settings, {"staff"})
        self.assertEqual(str(raised.exception),
                         "The catalogue could not be loaded (RuntimeError).")
        self.assertIsNone(raised.exception.__cause__)
        broken = ValueError(f"VIN cache HMAC key must be at least 32 bytes, not {SECRET}.")
        with mock.patch("automotive.api.create_app", side_effect=broken), \
                self.assertRaises(chat_review.ReviewError) as raised:
            chat_review.build_stores(self.settings, {"staff"})
        self.assertEqual(str(raised.exception), "The application could not be built (ValueError: "
                         "VIN cache HMAC key must be at least 32 bytes, not [REDACTED].).")

    def test_the_command_reads_the_settings_of_the_environment(self) -> None:
        self.log("staff", "a", "front pads camry")
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        out = io.StringIO()
        with mock.patch.object(Settings, "from_env", return_value=self.settings), \
                mock.patch("sys.stdout", out):
            code = chat_review.main(["--dry-run", "--log", "staff", "--since-hours", "1e9"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: 1 of 1 logged turn with no parts", out.getvalue())
        with self.assertRaises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    def test_options_must_be_above_zero(self) -> None:
        parser = chat_review.build_parser()
        for option in ("--since-hours", "--limit", "--budget-usd"):
            for value in ("0", "-1", "nan", "many"):
                with self.subTest(option=option, value=value), \
                        mock.patch("sys.stderr", io.StringIO()), \
                        self.assertRaises(SystemExit):
                    parser.parse_args([option, value])
        self.assertEqual(parser.parse_args([]).out, "data/chat_logs/reviews")


if __name__ == "__main__":
    unittest.main()
