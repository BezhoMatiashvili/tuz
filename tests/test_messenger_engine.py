"""The Messenger engine adapter: the saved state's shape and round trip, the size cap, the shop's
replies in the engine's view, turns of the real assistant engine with a scripted model (no
network, no provider, $0), and how a turn ends. The database part needs
WAREHOUSE_TEST_ADMIN_DATABASE_URL and skips without it."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from automotive.assistant_sources import shop_info
from automotive.config import Settings
from automotive.conversations import ConversationStore
from automotive.messenger_engine import (
    CREATED_STATE_KEYS,
    ENGINE_STATE_KEYS,
    RUNTIME_KEYS,
    STATE_BYTES,
    STATE_KEYS,
    EngineResult,
    MessengerConversations,
    MessengerEngine,
    ShopFacts,
    build_engine,
    clean_state,
    compact,
    record_shop_reply,
    state_size,
)
from automotive.meta_config import MetaSettings
from automotive.meta_db import MetaDatabase
from automotive.site_content import SiteContent
from automotive.site_repository import SiteRepository
from tests.test_assistant import (
    KNOWN,
    REAR,
    REQUEST,
    VIN,
    Script,
    Search,
    context_of,
    done,
    handles,
    option,
    product,
    says,
    search,
    service,
    use,
)
from tests.test_site_chat import CARD_KEYS, FORBIDDEN_KEYS, TURN_KEYS, Catalogs, keys_in

OTHER_VIN = "4T1BF1FK5FU000001"  # synthetic, as in test_site_chat
ASSISTANT = Settings(chat_engine="assistant")


def full_state() -> dict:
    """A state with every key, Georgian text and a VIN, as a chat's row could hold it."""
    return {
        "vehicle": {"make": "TOYOTA", "model": "Camry", "model_year": 2015,
                    "displacement_l": 2.5},
        "vin": VIN, "category": "", "language": "ka",
        "history": [{"question": f"კითხვა {number}", "answer": f"პასუხი {number}",
                     "note": "", "shown": ["SATO PN1521"]} for number in range(12)],
        "pending": {"act": "clarify", "question": "წინა თუ უკანა?",
                    "options": [{"label": "წინა", "meaning": "r1"},
                                {"label": "უკანა", "meaning": "r3"}],
                    "choices": ["წინა", "უკანა"]},
        "shown": [{"id": "p1", "brand": "SATO", "code": "30023433", "basis": None,
                   "fitment_status": None, "matched_via": []}],
        "vehicle_revision": 3, "customer_stated": {}, "resolution_candidates": [],
        "selected_configuration": {}, "unresolved_fields": [], "clarification_choices": [
            {"label": "წინა", "value": "წინა"}],
        "memory": {"language": "ka", "notes": "Camry 2015, ეძებს ხუნდებს",
                   "car_label": "Toyota Camry 2015", "handles": {"r1": "p1", "r3": "p3"},
                   "transcript": [{"user": "Camry 2015 ხუნდები", "assistant": "წინა თუ უკანა?"}]},
        "vin_typed": True,
        "identity": {"make": "TOYOTA", "model": "Camry", "model_year": 2015},
    }


class Blocking:
    """A chat service that holds its turn until released."""

    settings = ASSISTANT

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def respond_conversation(self, message: str, **kwargs) -> dict:
        self.started.set()
        self.release.wait(5)
        return {"status": "OK", "answer": "late", "language": "ka", "memory": {}}


class Broken:
    settings = ASSISTANT

    def respond_conversation(self, message: str, **kwargs) -> dict:
        raise RuntimeError("private engine detail")


def bare_engine(chat_service, **options) -> MessengerEngine:
    store = MessengerConversations(chat_service, None, **options)
    return MessengerEngine(store, Catalogs(), poll_seconds=0.005)


class StateTests(unittest.TestCase):
    def store(self) -> MessengerConversations:
        store = MessengerConversations(SimpleNamespace(settings=ASSISTANT), None)
        self.addCleanup(store.close)
        return store

    def test_the_state_is_exactly_what_create_makes_minus_the_runtime_fields(self):
        # A field added to ConversationStore.create() must fail here, so the saved state
        # cannot silently leave it out.
        for store in (self.store(), ConversationStore(SimpleNamespace(settings=ASSISTANT), None)):
            self.addCleanup(store.close)
            identifier = store.create()["conversation_id"]
            self.assertEqual(set(store._conversations[identifier]),
                             set(CREATED_STATE_KEYS) | set(RUNTIME_KEYS))
        self.assertEqual(STATE_KEYS, CREATED_STATE_KEYS + ("memory", "vin_typed", "identity"))
        self.assertEqual(ENGINE_STATE_KEYS, ("memory", "vin_typed", "identity"))

    def test_a_state_goes_round_through_json_and_a_conversation_unchanged(self):
        store = self.store()
        state = full_state()
        identifier = store.restore(json.loads(json.dumps(state, ensure_ascii=False)))
        conversation = store._conversations[identifier]
        self.assertIsInstance(conversation["history"], deque)
        self.assertEqual(conversation["history"].maxlen, 12)
        self.assertEqual((conversation["turns"], conversation["vin"]), ({}, VIN))
        exported = store.export_state(identifier)
        self.assertEqual(exported, state)
        self.assertEqual(json.loads(json.dumps(exported, ensure_ascii=False)), exported)
        exported["vehicle"]["make"] = "changed"  # a deep copy: the conversation is untouched
        exported["history"].clear()
        self.assertEqual(conversation["vehicle"]["make"], "TOYOTA")
        self.assertEqual(len(conversation["history"]), 12)
        store.delete(identifier)
        with self.assertRaises(Exception):
            store.export_state(identifier)

    def test_any_row_is_restored_without_raising(self):
        store = self.store()
        rows = [None, [], "text", 7, {"history": "abc", "vin": "NOT-A-VIN", "language": "ru",
                                     "vehicle_revision": True, "memory": ["not", "a", "dict"],
                                     "vin_typed": "yes", "pending": [], "something_new": 1,
                                     "vehicle": None, "category": 5}]
        for row in rows:
            with self.subTest(row=row):
                exported = store.export_state(store.restore(row))
                self.assertEqual(exported, clean_state(None))
                self.assertEqual(set(exported), set(CREATED_STATE_KEYS))
        many = {"history": [{"question": str(number)} for number in range(30)] + ["junk"]}
        exported = store.export_state(store.restore(many))
        self.assertEqual([entry["question"] for entry in exported["history"]],
                         [str(number) for number in range(18, 30)])
        odd = store.export_state(store.restore({"vehicle": {"displacement_l": float("nan"),
                                                            "note": "a\x00b\ud800c", 1: (2, 3)}}))
        self.assertEqual(odd["vehicle"], {"displacement_l": None, "note": "ab�c",
                                          "1": [2, 3]})

    def test_a_state_too_large_for_its_column_is_cut_where_the_assistant_loses_least(self):
        state = full_state()
        state["history"] = [{"question": "ხ" * 4000, "answer": "პ" * 4000, "note": "ნ" * 400,
                             "shown": []} for _ in range(11)]
        state["history"].append({"question": "ბოლო შეკითხვა " + "ა" * 3000, "answer": "",
                                 "shown": []})
        memory = state["memory"]
        memory["handles"] = {f"r{number}": f"product-{number:08d}" for number in range(3000)}
        memory["notes"] = "rear pads are r2999 and r5"
        memory["transcript"] = [{"user": "მ" * 1500, "assistant": "ბ" * 1500}
                                for _ in range(8)]
        self.assertGreater(state_size(state), 262_144)
        before = json.dumps(state, ensure_ascii=False)
        small = compact(state)
        self.assertLessEqual(state_size(small), STATE_BYTES)
        self.assertEqual(json.dumps(state, ensure_ascii=False), before)  # the input is unchanged
        # The newest message is still unanswered and readable, the car and the open question
        # are kept, and so are the handles the notes name.
        self.assertTrue(small["history"][-1]["question"].startswith("ბოლო შეკითხვა"))
        self.assertEqual(small["history"][-1]["answer"], "")
        self.assertEqual((small["vin"], small["pending"]), (state["vin"], state["pending"]))
        self.assertTrue({"r2999", "r5"} <= set(small["memory"]["handles"]))
        self.assertEqual(small["memory"]["transcript"][-1], memory["transcript"][-1])
        self.assertIs(compact(full_state()) == full_state(), True)

    def test_the_shops_reply_goes_where_the_assistant_reads_earlier_turns(self):
        state = full_state()
        state["history"].append({"question": "უკანაც გაქვთ?", "answer": "", "shown": []})
        recorded = record_shop_reply(state, "დიახ, უკანაც გვაქვს, 120 ₾.",
                                     customer_text=f"ახალი მანქანა: {OTHER_VIN}")
        self.assertEqual(recorded["memory"]["transcript"][-1], {
            "user": "უკანაც გაქვთ?\nახალი მანქანა: [VIN]",
            "assistant": "დიახ, უკანაც გვაქვს, 120 ₾."})
        self.assertEqual(recorded["history"][-1]["answer"], "დიახ, უკანაც გვაქვს, 120 ₾.")
        # A VIN the customer gave the shop is the chat's car from now on, as in a turn.
        self.assertEqual((recorded["vin"], recorded["vin_typed"], recorded["vehicle"],
                          recorded["vehicle_revision"]), (OTHER_VIN, True, {}, 4))
        text = json.dumps({key: value for key, value in recorded.items() if key != "vin"},
                          ensure_ascii=False)
        self.assertNotIn(OTHER_VIN, text)
        self.assertNotIn(VIN, text)
        # The shop writing first: added to the last reply, nothing invented for the customer.
        first = record_shop_reply(full_state(), "კიდევ რამე ხომ არ გჭირდებათ?")
        self.assertEqual(first["memory"]["transcript"][-1]["assistant"],
                         "წინა თუ უკანა?\nკიდევ რამე ხომ არ გჭირდებათ?")
        new_chat = record_shop_reply(None, "გამარჯობა!")
        self.assertNotIn("memory", new_chat)
        self.assertEqual(new_chat["history"][-1], {"question": "", "answer": "გამარჯობა!",
                                                   "note": "", "shown": []})
        self.assertEqual(record_shop_reply(state, "   "), clean_state(state))


class EngineTurnTests(unittest.TestCase):
    """Turns of the real assistant engine (ChatService) with the scripted model."""

    def engine(self, script: Script, **options) -> MessengerEngine:
        chat = service(script)
        engine = MessengerEngine(MessengerConversations(chat, Search(), **options),
                                 chat.catalog_store, poll_seconds=0.005)
        self.addCleanup(engine.close)
        return engine

    def test_a_question_and_its_tapped_answer_survive_a_restart(self):
        asked = {"text": "წინა თუ უკანა?", "options": [option("წინა", "front pads: r1, r2"),
                                                       option("უკანა", "rear pads: r3")]}

        def first(request):
            found = handles(request)
            return done(reply="Camry 2015-ისთვის ორივე გვაქვს.", question=asked, suggestions=[],
                        outcome="asked", car_label="Toyota Camry 2015",
                        notes=f"Camry 2015 pads; rear is {found['04466-33180']}")

        one = self.engine(Script(says(use("search_catalog",
                                          search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                                 first)).respond(None, "Camry 2015 ხუნდები", language="auto")
        self.assertEqual((one.status, one.engine_status, one.error_code, one.language),
                         ("ok", "CLARIFICATION", "", "ka"))
        self.assertEqual([entry["label"] for entry in one.public["question"]["options"]],
                         ["წინა", "უკანა"])
        saved = json.loads(json.dumps(one.state, ensure_ascii=False))  # the database row
        self.assertTrue(set(CREATED_STATE_KEYS) | {"memory"} <= set(saved) <= set(STATE_KEYS))
        self.assertEqual(saved["pending"]["options"][1]["meaning"], "rear pads: r3")
        self.assertEqual(saved["history"][-1]["question"], "Camry 2015 ხუნდები")

        def second(request):
            sent = context_of(request)
            self.assertEqual(sent["tapped_option"],
                             {"index": 2, "label": "უკანა", "meaning": "rear pads: r3"})
            self.assertEqual(sent["your_question"]["text"], "წინა თუ უკანა?")
            self.assertEqual(request["messages"][0]["content"], "Camry 2015 ხუნდები")
            self.assertIn("წინა თუ უკანა?", request["messages"][1]["content"])
            rear = [row[0] for row in sent["rows"]["rows"] if row[1] == REAR]
            return done(reply="უკანა ხუნდები ქვემოთაა.", outcome="offered",
                        car_label=sent["car_label"], products=[product(rear[0])])

        # Another process: a new store, chat service and engine, the state read back.
        two = self.engine(Script(second)).respond(saved, "უკანა", choice=2)
        self.assertEqual((two.status, two.engine_status), ("ok", "OK"), two.public.get("answer"))
        self.assertEqual([card["name"] for card in two.public["cards"]], [REAR])
        self.assertIsNone(two.state["pending"])
        self.assertEqual(len(two.state["memory"]["transcript"]), 2)

    def test_the_reply_carries_the_sap_code_on_the_card(self):
        script = Script(done(reply="გვაქვს SATO ხუნდი (30023433).", outcome="offered",
                             car_label="Toyota Camry 2015", products=[product("r1")]))
        result = self.engine(script).respond({"memory": KNOWN}, "Camry 2015 წინა ხუნდები")
        self.assertEqual(result.status, "ok", result.public)
        self.assertEqual(set(result.public), TURN_KEYS)
        self.assertFalse(keys_in(result.public) & FORBIDDEN_KEYS)
        self.assertEqual(set(result.public["cards"][0]), CARD_KEYS)
        self.assertEqual(result.public["cards"][0]["price"], 85.0)
        self.assertEqual(result.public["cards"][0]["sap_code"], "30023433")
        self.assertEqual(result.public["answer"], "გვაქვს SATO ხუნდი.")
        self.assertGreater(result.usage["model_requests"], 0)
        # A number the customer typed in this chat, even in an earlier message, stays.
        typed = Script(done(reply="30023433 მარაგშია.", outcome="offered",
                            car_label="Toyota Camry 2015", products=[product("r1")]))
        state = {"memory": KNOWN, "history": [{"question": "გაქვთ 30023433?",
                                               "answer": "რომელი მანქანისთვის?", "shown": []}]}
        kept = self.engine(typed).respond(state, "Camry 2015")
        self.assertIn("30023433", kept.public["answer"])

    def test_a_refusing_ai_account_is_reported_so_the_bot_can_pause(self):
        cases = [
            (anthropic.AuthenticationError(
                "private", response=httpx.Response(401, request=REQUEST), body=None),
             "AUTHENTICATION_ERROR"),
            (anthropic.PermissionDeniedError(
                "private", response=httpx.Response(403, request=REQUEST), body=None),
             "ACCESS_DENIED"),
            (anthropic.BadRequestError(
                "private: Your credit balance is too low to access the Anthropic API.",
                response=httpx.Response(400, request=REQUEST), body=None), "PROVIDER_BILLING"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                with self.assertLogs("automotive.assistant", level="ERROR"):
                    result = self.engine(Script(error)).respond(None, "ხუნდები")
                self.assertEqual((result.status, result.error_code, result.engine_status),
                                 ("failed", code, code))
                self.assertNotIn("private", json.dumps([result.public, result.state]))
                # The engine kept its own record of the failed turn: the words are not lost.
                self.assertEqual(result.state["memory"]["transcript"][-1]["user"], "ხუნდები")
                self.assertEqual(result.unsent_state["history"][-1]["answer"], "")
        other = self.engine(Script(anthropic.APIStatusError(
            "private", response=httpx.Response(418, request=REQUEST), body=None)))
        result = other.respond(None, "ხუნდები")
        self.assertEqual((result.status, result.error_code), ("failed", "PROVIDER_UNAVAILABLE"))

    def test_two_vins_are_refused_and_the_message_kept_without_them(self):
        engine = self.engine(Script())
        result = engine.respond(full_state(), f"{VIN} ან {OTHER_VIN}, რომელს აქვს ხუნდი?")
        self.assertEqual((result.status, result.error_code, result.language),
                         ("failed", "CONFLICTING_VINS", "ka"))
        self.assertEqual(result.state, result.unsent_state)
        self.assertEqual(result.state["history"][-1]["question"],
                         "[VIN] ან [VIN], რომელს აქვს ხუნდი?")
        self.assertEqual(result.state["vin"], VIN)  # the car on record stays
        self.assertNotIn(OTHER_VIN, json.dumps(result.state))
        self.assertEqual(engine.store._conversations, {})

    def test_an_ok_turn_also_says_what_to_keep_when_its_reply_is_not_sent(self):
        engine = self.engine(Script(done(reply="გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")))
        result = engine.respond(None, "გამარჯობა")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.state["memory"]["transcript"][-1]["user"], "გამარჯობა")
        self.assertEqual(result.unsent_state["history"],
                         [{"question": "გამარჯობა", "answer": "", "shown": []}])
        self.assertNotIn("memory", result.unsent_state)

        def joined(request):  # the message nobody saw an answer to is read with the next
            self.assertEqual(context_of(request)["unanswered_messages"], ["გამარჯობა"])
            return done(reply="მომწერეთ მანქანის მოდელი და წელი.")

        later = self.engine(Script(joined)).respond(result.unsent_state, "ხუნდები მინდა")
        self.assertEqual(later.status, "ok")

    def test_the_shops_reply_is_in_the_next_turns_view(self):
        state = record_shop_reply(None, "დიახ, გვაქვს. რომელი მანქანისთვის?",
                                  customer_text="ხუნდები გაქვთ?")

        def next_turn(request):
            self.assertEqual(request["messages"][0]["content"], "ხუნდები გაქვთ?")
            self.assertEqual(request["messages"][1]["content"],
                             "დიახ, გვაქვს. რომელი მანქანისთვის?")
            self.assertNotIn("unanswered_messages", context_of(request))
            return done(reply="Camry 2015-ისთვის ვნახავ.")

        self.assertEqual(self.engine(Script(next_turn)).respond(state, "Camry 2015").status,
                         "ok")

    def test_a_turn_past_the_wait_is_stopped_and_its_message_kept(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(request):
            release.wait(5)
            return done()

        engine = self.engine(Script(slow))
        started = time.monotonic()
        result = engine.respond({"language": "en"}, "brake pads", timeout=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((result.status, result.error_code, result.language, result.public),
                         ("timeout", "TIMEOUT", "en", {}))
        self.assertEqual(result.state, result.unsent_state)
        self.assertEqual(result.state["history"][-1],
                         {"question": "brake pads", "answer": "", "shown": []})
        self.assertEqual(engine.store._conversations, {})


class TurnEndingTests(unittest.TestCase):
    """How a turn ends, with chat services that block, fail or run another engine."""

    def test_the_stores_own_deadline_reads_as_a_timeout(self):
        chat = Blocking()
        self.addCleanup(chat.release.set)
        engine = bare_engine(chat, turn_seconds=0.2)
        self.addCleanup(engine.close)
        result = engine.respond(None, "ხუნდები", timeout=5)
        self.assertEqual((result.status, result.error_code), ("timeout", "TURN_DEADLINE"))
        self.assertEqual(result.state["history"][-1]["question"], "ხუნდები")

    def test_cancel_stops_the_turn_at_once(self):
        chat = Blocking()
        self.addCleanup(chat.release.set)
        engine = bare_engine(chat)
        self.addCleanup(engine.close)
        cancel = threading.Event()
        threading.Thread(target=lambda: chat.started.wait(5) and cancel.set()).start()
        result = engine.respond(full_state(), "უკანაც", cancel=cancel, timeout=5)
        self.assertEqual((result.status, result.error_code), ("cancelled", ""))
        self.assertEqual(result.state["history"][-1]["question"], "უკანაც")
        self.assertEqual(result.state["memory"], full_state()["memory"])
        self.assertEqual(engine.store._conversations, {})
        chat.release.set()  # the stopped turn finishes later and changes nothing
        time.sleep(0.05)
        self.assertEqual(engine.store._conversations, {})

    def test_an_engine_fault_is_a_failure_that_keeps_the_message(self):
        engine = bare_engine(Broken())
        self.addCleanup(engine.close)
        result = engine.respond(None, "ხუნდები")
        self.assertEqual((result.status, result.error_code), ("failed", "CHAT_UNAVAILABLE"))
        self.assertEqual(result.state["history"][-1]["question"], "ხუნდები")
        self.assertNotIn("private", json.dumps(result.public))

    def test_only_the_assistant_engine_answers_messenger(self):
        engine = bare_engine(SimpleNamespace(settings=Settings(chat_engine="scripted")))
        self.addCleanup(engine.close)
        result = engine.respond(None, "ხუნდები")
        self.assertEqual((result.status, result.error_code), ("failed", "ASSISTANT_REQUIRED"))

    def test_an_empty_message_or_a_closed_engine_starts_nothing(self):
        engine = bare_engine(Broken())
        for text in ("", "   ", None, 42):
            result = engine.respond(full_state(), text)
            self.assertEqual((result.status, result.error_code), ("failed", "EMPTY_MESSAGE"))
            self.assertEqual(result.state, full_state())
        self.assertFalse(engine.closing)
        engine.close()
        self.assertTrue(engine.closing)  # meta_comments sends no fallback for a closed engine
        closed = engine.respond(None, "ხუნდები")
        self.assertEqual((closed.status, closed.error_code), ("failed", "CHAT_CLOSED"))
        self.assertIsInstance(closed, EngineResult)

    def test_a_turn_never_starts_the_warm_up_the_worker_starts_it_ahead(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def prepare():
            calls.append("prepare")
            started.set()
            release.wait(5)

        chat = SimpleNamespace(settings=ASSISTANT, prepare=prepare,
                               respond_conversation=lambda message, **kwargs: {
                                   "status": "OK", "answer": "კი, გვაქვს.", "language": "ka",
                                   "memory": {}})
        engine = bare_engine(chat)
        self.addCleanup(engine.close)
        self.assertFalse(hasattr(engine.store.chat_service, "prepare"))
        self.assertEqual(engine.store.chat_service.settings, ASSISTANT)
        self.assertEqual(engine.respond(None, "ხუნდები").status, "ok")
        time.sleep(0.05)
        self.assertEqual(calls, [])  # opening the turn's conversation started nothing
        self.assertTrue(engine.prepare())
        self.assertTrue(started.wait(2))
        self.assertFalse(engine.prepare())  # one at a time
        release.set()
        until = time.monotonic() + 2
        while not engine.prepare() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertEqual(calls, ["prepare", "prepare"])
        self.assertFalse(bare_engine(Broken()).prepare())  # nothing to prepare

    def test_the_shop_phone_comes_from_the_brand_settings(self):
        content = SimpleNamespace(snapshot=lambda: {"sections": {"brand": {
            "phone": " +995 555  12 34 56 "}}})
        engine = MessengerEngine(MessengerConversations(Broken(), None), Catalogs(),
                                 content=content)
        self.addCleanup(engine.close)
        self.assertEqual(engine.shop_phone(), "+995 555 12 34 56")
        engine.content = SimpleNamespace(snapshot=lambda: 1 / 0)
        self.assertEqual(engine.shop_phone(), "")
        self.assertTrue(engine.ready)


class BuildTests(unittest.TestCase):
    """build_engine wires the public widget's configuration; nothing here sends a request."""

    def build(self, **changes):
        folder = tempfile.mkdtemp(prefix="messenger-engine-")
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        # The chat log an earlier version kept here, with its SQLite side files.
        for name in ("chat_log.sqlite3", "chat_log.sqlite3-wal", "chat_log.sqlite3-shm"):
            (Path(folder) / name).write_bytes(b"customer words")
        settings = Settings(**{
            "chat_engine": "scripted", "vin_hmac_key": "h" * 32,
            "chat_log_path": "data/chat_logs/chat_log.sqlite3",
            "site_chat_log_path": "data/chat_logs/site_chat_log.sqlite3",
            "automotive_cache_path": "data/automotive-cache.sqlite3",
            "catalog_api_key": "private-catalogue-key",  # gitleaks:allow
            "motorium_enabled": True,
            "knowledge_enabled": True, **changes})
        with self.assertLogs("automotive.messenger_engine", level="INFO") as logged:
            engine = build_engine(MetaSettings(data_dir=folder), settings)
        self.addCleanup(engine.close)
        return engine, Path(folder), "\n".join(logged.output)

    def test_the_public_configuration_with_its_own_files(self):
        engine, folder, logged = self.build(assistant_research="staff")
        chat = engine.store.chat_service
        used = chat.settings
        self.assertEqual((engine.store.engine, engine.store.audience), ("assistant", "public"))
        self.assertEqual((engine.store.ttl_seconds, engine.store.turn_seconds,
                          engine.store.maximum), (48 * 3600.0, 60.0, 300))
        self.assertEqual((used.automotive_cache_path, used.assistant_research_cache_path),
                         (str(folder / "automotive-cache.sqlite3"),
                          str(folder / "research_cache.sqlite3")))
        self.assertEqual((used.chat_log_path, used.site_chat_log_path, used.catalog_api_key,
                          used.motorium_enabled, used.knowledge_enabled,
                          used.assistant_research),
                         ("", "", "", False, False, "off"))
        # No chat log: a customer's words would outlive the dashboard's "Delete data" in a
        # file keyed by nothing that names them. The one an earlier version kept is removed.
        self.assertIsNone(engine.store.chat_log)
        self.assertFalse(list(folder.glob("chat_log*")))
        self.assertIn("chat_log=off", logged)
        search_service = engine.store.search_service
        self.assertFalse(search_service.motorium_enabled)
        self.assertIsNone(search_service._catalog)  # no paid catalogue
        self.assertIs(chat.catalog_store, engine.catalog_store)
        self.assertNotIn("research", chat.assistant_extras)
        self.assertIs(chat.assistant_extras["site_content"], engine.content)
        self.assertIsInstance(engine.content.repository, ShopFacts)
        self.assertFalse(engine.ready)  # no catalogue database in this test
        for part in ("audience=public", "engine=assistant", f"cache={folder}", "research=off",
                     "research_audiences=staff", "shop_facts=defaults", "model_key=missing"):
            self.assertIn(part, logged)
        self.assertNotIn("private", logged)
        self.assertNotIn("postgres", logged)

    def test_a_chat_log_the_application_itself_writes_is_never_removed(self):
        folder = Path(tempfile.mkdtemp(prefix="messenger-engine-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        own = folder / "chat_log.sqlite3"
        own.write_bytes(b"staff log")
        settings = Settings(chat_engine="scripted", vin_hmac_key="h" * 32,
                            chat_log_path=str(own), site_chat_log_path="")
        with self.assertLogs("automotive.messenger_engine", level="INFO"):
            engine = build_engine(MetaSettings(data_dir=str(folder)), settings)
        self.addCleanup(engine.close)
        self.assertEqual(own.read_bytes(), b"staff log")

    def test_research_only_when_the_public_widget_has_it(self):
        unused_key = "sk-ant-unused-test-key"  # gitleaks:allow
        engine, folder, logged = self.build(assistant_research="all", anthropic_api_key=unused_key)
        extras = engine.store.chat_service.assistant_extras
        self.assertEqual(extras["research_audiences"], ("staff", "public"))
        self.assertIn("research=on", logged)
        self.assertIn(f"research_cache={folder / 'research_cache.sqlite3'}", logged)
        self.assertNotIn("sk-ant", logged)


ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class ShopFactsDatabaseTests(unittest.TestCase):
    """The worker's login (a member of garage_messenger) reads the brand and the services
    ``shop_info`` quotes, and the site repository's reader would not work with it."""

    @classmethod
    def setUpClass(cls) -> None:
        from warehouse_sync.migrations import apply_migrations_on_connection

        cls.suffix = uuid.uuid4().hex[:12]
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.database = f"messenger_test_{cls.suffix}"
        cls.role = f"messenger_worker_{cls.suffix}"
        cls.addClassCleanup(cls._drop)
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.database
        cls.url = make_conninfo(**info)
        roles = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
        with psycopg.connect(cls.url) as connection:
            connection.execute(roles)
            apply_migrations_on_connection(connection)
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(cls.role)))
        cls.admin.execute(sql.SQL("GRANT garage_messenger TO {}").format(
            sql.Identifier(cls.role)))
        cls.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(cls.role)))
        brand = {"phone": "+995 555 12 34 56", "whatsapp": "", "email": "",
                 "address": {"ka": "ქავთარაძის ქ. N 17", "en": "17 Kavtaradze St"},
                 "map_url": "", "facebook": "", "instagram": "", "tiktok": "",
                 "currency": "₾", "hours": [{"days": {"ka": "ორშ-შაბ", "en": "Mon-Sat"},
                                             "time": "9:30-19:00"}]}
        with psycopg.connect(cls.url) as connection:
            connection.execute("INSERT INTO garage.site_sections (section_key, document) "
                               "VALUES ('brand', %s)", (Jsonb(brand),))
            connection.execute("INSERT INTO garage.services (title_ka, title_en, price_from) "
                               "VALUES ('ზეთის შეცვლა', 'Oil change', 40)")

    @classmethod
    def _drop(cls) -> None:
        cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(cls.database)))
        cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(cls.role)))
        cls.admin.close()

    def as_worker(self) -> MetaDatabase:
        role, url = self.role, self.url

        class Worker(MetaDatabase):
            def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
                connection = super().connect(autocommit=autocommit)
                connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                return connection

        return Worker(url)

    def test_the_worker_reads_the_brand_and_services_for_shop_info(self):
        content = SiteContent(ShopFacts(self.as_worker()))
        snapshot = content.snapshot()
        self.assertEqual(snapshot["source"], "database")
        self.assertEqual(snapshot["sections"]["brand"]["phone"], "+995 555 12 34 56")
        facts = shop_info(content, "ka")
        self.assertEqual(facts["phone"], "+995 555 12 34 56")
        self.assertEqual(facts["services"], [{"title": "ზეთის შეცვლა", "price_from": 40.0}])
        self.assertNotIn("phone", facts["missing"])
        engine = MessengerEngine(MessengerConversations(Broken(), None), Catalogs(),
                                 content=content)
        self.addCleanup(engine.close)
        self.assertEqual(engine.shop_phone(), "+995 555 12 34 56")

    def test_the_site_repositorys_reader_is_refused_this_login(self):
        repository = SiteRepository(self.url)
        connect, role = repository._connect, self.role

        def as_role() -> psycopg.Connection:
            connection = connect()
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            return connection

        repository._connect = as_role
        with self.assertRaises(errors.InsufficientPrivilege):
            repository.load_content()  # it reads garage.media too


if __name__ == "__main__":
    unittest.main()
