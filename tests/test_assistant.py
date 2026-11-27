"""The assistant engine with a scripted model: the turn loop, the tools, the checks on
``finish``, privacy, limits, the store branch and the API models. No network, no model."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import re
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx

from automotive import assistant_prompt, stock
from automotive.api import ConversationTurn, clamp_turn
from automotive.assistant_tools import (
    FILTER_GROUPS,
    OIL_GROUPS,
    OIL_QUERY,
    SUPPLIER_GROUPS,
    supplier_query,
    tool_definitions,
    typed_tokens,
)
from automotive.catalog_api import categories_for
from automotive.categories import category_for_query
from automotive.chat import ChatService, engine_name
from automotive.config import Settings
from automotive.conversations import ConversationStore
from automotive.rules import NeverOffer, Rules
from automotive.site_catalog import Catalog, CatalogStore
from automotive.vin_search import VINSearchCoordinator

VIN = "1HGCM82633A004352"
SECRET = "sk-ant-fixture-secret-0000000000000000"
NOW = datetime.now(UTC)
REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
FRONT, REAR = "სამუხრუჭე ხუნდი წნ", "სამუხრუჭე ხუნდი უკ"
CAMRY = "TOYOTA/LEXUS CAMRY 2006->"


def row(sid, shop, name, vehicle, brand, code, cross="", price="85", store="1", tuz11="0"):
    """One row as site_catalog.SNAPSHOT_SQL returns it."""
    return {"source_product_id": sid, "product_code": shop, "product_name": name,
            "product_model": vehicle, "original_code": code, "duplicate_code": cross,
            "brand": brand, "last_observed_at": NOW, "price_raw": price,
            "stock_store": store, "stock_tuz11": tuz11}


ROWS = [
    row("p1", "30023433", FRONT, CAMRY, "SATO", "PN1521", "D1222-8331/GDB3429/KD2765",
        "85", "6", "5"),
    row("p2", "2040026342", FRONT, CAMRY, "ABS", "37677", "D1222/GDB3429/KD2765/PN1521",
        "85", "0", "10"),
    row("p3", "30015804", REAR, CAMRY, "TOYOTA", "04466-33180", "D1212/GDB3426", "120", "0", "0"),
    row("p4", "30024596", "ზეთის ფილტრი", "MB C CLS E GLK M276", "MANN", "HU7025z",
        "HU7025Z/OX814D/OE6775", "50", "2", "0"),
    row("p5", "30030000", "ძრავის ზეთი 5W-30 4L", "", "SHELL", "550046", "", "120", "3", "0"),
    row("p6", "30031111", "ჰაერის ფილტრი", CAMRY, "SAKURA", "A-1858", "17801-0H050",
        "30", "1", "0"),
]
KNOWN = {"handles": {"r1": "p1", "r2": "p2", "r3": "p3", "r4": "p4"}}


class Repository:
    """The database behind the cards: the same rows, read one at a time."""

    def __init__(self, rows=ROWS):
        self.rows = {item["source_product_id"]: dict(item) for item in rows}
        self.read: list[str] = []

    def get_product(self, product_id):
        self.read.append(product_id)
        item = self.rows.get(product_id)
        if item is None:
            return None
        return {**{key: item[key] for key in (
            "source_product_id", "product_code", "product_name", "product_model",
            "original_code", "duplicate_code", "brand", "last_observed_at")},
            "price": None, "currency": None, "status": "PROVISIONAL",
            "source_fingerprint": "f" * 16,
            "stock": {"store": float(item["stock_store"]), "tuz11": float(item["stock_tuz11"])}}


class Vin:
    def __init__(self, vehicle=None, error=None):
        self.calls: list[str] = []
        self.error = error
        self.vehicle = vehicle or {"make": "TOYOTA", "model": "Camry", "model_year": 2015,
                                   "displacement_l": "2.5", "fuel": "Gasoline",
                                   "engine_code": None}

    def decode(self, vin, model_year=None, *, deadline=None):
        self.calls.append(vin)
        if self.error:
            raise self.error
        return {"status": "PROVISIONAL", "vehicle": dict(self.vehicle)}


class Search:
    """The source federation: one finished snapshot for every job."""

    def __init__(self, **snapshot):
        self.started: list[tuple] = []
        self.snapshot = {"status": "COMPLETE", "vehicle": {}, "sources": [], "items": [],
                         "fluids": [], "next_offset": None, **snapshot}

    def prewarm(self, vin):
        return None

    def start(self, vin, part_query="", model_year=None, configuration=None, **kwargs):
        self.started.append((vin, part_query, model_year, configuration))
        return {"search_id": "job", **copy.deepcopy(self.snapshot)}

    def get(self, identifier, offset=0, limit=50):
        return {"search_id": identifier, **copy.deepcopy(self.snapshot)}


class Script:
    """The model: replies in order. A callable step builds its reply from the request."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


COUNTER = iter(range(1, 100_000))


def use(name, args):
    return {"type": "tool_use", "id": f"tu{next(COUNTER)}", "name": name, "input": args}


def says(*blocks, stop="tool_use", usage=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


def search(make=(), vehicle=(), part=(), exclude=(), limit=20):
    return {"make_terms": list(make), "vehicle_terms": list(vehicle), "part_terms": list(part),
            "exclude_terms": list(exclude), "in_stock_only": False, "limit": limit}


def car(**values):
    return {"use_vin": False, "make": "", "model": "", "year": 0, "trim": "",
            "engine_code": "", "engine_litres": 0, "fuel": "unknown", "body": "", **values}


def option(label, meaning="what it establishes"):
    return {"label": label, "meaning": meaning}


def product(*handles, confidence="listed", evidence="CAMRY 2006->", **extra):
    """One entry of finish.products: the rows that share one judgement."""
    return {"ids": list(handles), "confidence": confidence, "evidence": evidence,
            "reason": "2006 წლიდან, მოიცავს 2015-ს.", "group": "წინა ხუნდები", **extra}


def sse(blocks, usage, stop="tool_use"):
    """A Messages API reply as the event stream the SDK reads."""
    events = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {**usage, "output_tokens": 1}}}]
    for index, block in enumerate(blocks):
        opening = {**block}
        if block["type"] == "tool_use":
            opening["input"] = {}
            delta = {"type": "input_json_delta",
                     "partial_json": json.dumps(block["input"], ensure_ascii=False)}
        elif block["type"] == "thinking":
            opening["signature"] = ""
            delta = {"type": "signature_delta", "signature": block["signature"]}
        else:
            opening["text"] = ""
            delta = {"type": "text_delta", "text": block["text"]}
        events += [{"type": "content_block_start", "index": index, "content_block": opening},
                   {"type": "content_block_delta", "index": index, "delta": delta},
                   {"type": "content_block_stop", "index": index}]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]}},
               {"type": "message_stop"}]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                   for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=body.encode("utf-8"))


def finish(**values):
    """A finish call as its schema shapes it. A test may also pass `question` the way a
    stored question is shaped ({text, options with meanings}), which the engine reads too."""
    return {"language": "ka", "car_label": "", "products": [], "question": "", "options": [],
            "suggestions": ["ნაწილის ძებნა"],
            "reply": "გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.", "notes": "",
            "oil_quote": {"oil_id": "", "filter_id": "", "litres": 0},
            "outcome": "chat", **values}


def done(**values):
    return says(use("finish", finish(**values)))


def service(script=None, *, rows=ROWS, repository=None, rules=None, vin=None, **settings):
    options = {"chat_engine": "assistant", "anthropic_api_key": SECRET,
               "vin_hmac_key": "h" * 32, **settings}
    client = SimpleNamespace(messages=SimpleNamespace(create=script.create)) if script else None
    chat = ChatService(repository or Repository(rows), vin or Vin(), Settings(**options),
                       client, rules=rules)
    chat.catalog_store = CatalogStore("", chat.rules)
    chat.catalog_store.replace(Catalog.from_rows(rows, chat.rules))
    return chat


def run(chat, message, **kwargs):
    kwargs.setdefault("search_service", Search())
    return chat.respond_conversation(message, language="ka", **kwargs)


def context_of(request):
    """The JSON context of the turn: the last text block of the opening user message."""
    opening = [message for message in request["messages"] if message["role"] == "user"
               and isinstance(message["content"], list)
               and message["content"][0].get("cache_control")]
    return json.loads(opening[0]["content"][0]["text"])


def tool_results(request):
    """The tool results of the last message, each as (parsed content, is_error)."""
    return [(json.loads(block["content"]), bool(block.get("is_error")))
            for block in request["messages"][-1]["content"] if block["type"] == "tool_result"]


def handles(request):
    """Brand number to handle, for every row the model has been sent so far."""
    found = {}

    def walk(value):
        if isinstance(value, list):
            if (len(value) >= 9 and isinstance(value[0], str)
                    and re.fullmatch(r"r\d+", value[0])):
                found[value[4]] = value[0]
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    for message in request["messages"]:
        content = message["content"]
        for block in content if isinstance(content, list) else []:
            text = block.get("content") if block.get("type") == "tool_result" else block.get("text")
            try:
                walk(json.loads(text))
            except (TypeError, ValueError):
                continue
    return found


class TurnTests(unittest.TestCase):
    def test_parallel_searches_then_finish_give_cards_from_the_catalogue(self):
        def judge(request):
            found = handles(request)
            return done(
                reply="Camry 2015-ის წინა ხუნდები ქვემოთაა.", car_label="Toyota Camry 2015",
                outcome="offered", notes=f"Camry 2015. good rows {found['PN1521']}",
                products=[product(found["37677"], name="INVENTED PAD", price=1, stock=99),
                          product(found["PN1521"], product_name="INVENTED", price_raw="1")])

        script = Script(
            says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი წნ"])),
                 use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი უკ"]))),
            judge)
        repository = Repository()
        repository.rows["p1"]["stock_store"] = "2"  # Sold since the snapshot was taken.
        events = []
        result = run(service(script, repository=repository), "Camry 2015, წინა ხუნდები",
                     progress=events.append)

        self.assertEqual((result["status"], result["act"], result["outcome"]),
                         ("OK", "results", "offered"))
        self.assertEqual((result["usage"]["model_requests"],
                          result["usage"]["tool_executions"]), (2, 2))
        # In the shop first, whatever order the model gave; everything from the records.
        first, second = result["cards"]
        self.assertEqual(first["product"]["source_product_id"], "p1")
        self.assertEqual(first["product"]["product_name"], FRONT)
        self.assertEqual(first["product"]["product_model"], CAMRY)
        self.assertEqual((first["product"]["price"], first["product"]["currency"]),
                         (85.0, "GEL"))
        self.assertEqual(first["product"]["stock"], {"store": 2.0, "tuz11": 5.0})
        self.assertEqual(first["product"]["original_code"], "PN1521")
        self.assertEqual(first["product"]["product_code"], "30023433")
        self.assertEqual((first["availability_status"], second["availability_status"]),
                         ("IN_STORE", "WITHIN_1_2_HOURS"))
        self.assertIn("2", first["availability"])
        self.assertEqual((first["fitment_status"], first["basis"], first["confidence"]),
                         ("UNKNOWN", "listed", "listed"))
        self.assertEqual(first["matched_via"], [first["reason"]])
        self.assertEqual((first["evidence"], first["warnings"]), ("CAMRY 2006->", []))
        self.assertNotIn("INVENTED", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["groups"], [{"id": "g1", "label": "წინა ხუნდები", "count": 2}])
        self.assertEqual({card["group"] for card in result["cards"]}, {"g1"})
        self.assertEqual(result["car_label"], "Toyota Camry 2015")
        self.assertEqual(sorted(repository.read), ["p1", "p2"])

        opening, judging = script.requests
        self.assertEqual((opening["tool_choice"], judging["tool_choice"]),
                         ({"type": "any"}, {"type": "auto"}))
        # Append-only: the second request repeats the first and adds to it.
        self.assertEqual(judging["messages"][:len(opening["messages"])], opening["messages"])
        asked = judging["messages"][-2]["content"]
        answered = judging["messages"][-1]["content"]
        self.assertEqual([block["tool_use_id"] for block in answered[:2]],
                         [block["id"] for block in asked])
        front, rear = tool_results(judging)
        self.assertEqual((front[0]["total"], rear[0]["total"], front[1]), (2, 1, False))
        self.assertEqual(front[0]["columns"][0], "id")
        self.assertEqual([entry["stage"] for entry in result["progress"]],
                         ["search_catalog", "search_catalog"])
        self.assertEqual({entry["status"] for entry in result["progress"]}, {"OK"})
        self.assertIn("ხუნდი წნ", result["progress"][0]["label"])
        self.assertEqual(events[0]["progress"][0]["status"], "RUNNING")
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["engine"], "assistant")
        self.assertEqual([tool["rows"] for tool in diagnostics["tools"]], [2, 1])
        self.assertEqual(diagnostics["tools"][0]["args"]["part_terms"], ["ხუნდი წნ"])
        self.assertEqual(len(diagnostics["requests"]), 2)
        self.assertEqual(result["memory"]["shown"], ["p1", "p2"])

    def test_a_greeting_takes_one_request_and_shows_next_steps(self):
        script = Script(done())
        result = run(service(script), "გამარჯობა")
        self.assertEqual(len(script.requests), 1)
        self.assertEqual((result["status"], result["act"], result["cards"]), ("OK", "help", []))
        self.assertEqual(result["quick_replies"], ["ნაწილის ძებნა"])
        self.assertEqual(result["suggestions"], [{"label": "ნაწილის ძებნა",
                                                  "value": "ნაწილის ძებნა"}])
        self.assertEqual(result["clarification_choices"], result["suggestions"])
        self.assertIsNone(result["question"])
        self.assertIsNone(result["pending"])
        self.assertEqual(result["answer"], "გამარჯობა! მომწერეთ, რა ნაწილი გჭირდებათ.")

    def test_a_question_carries_buttons_and_is_remembered_with_its_meanings(self):
        asked = {"text": "წინა თუ უკანა?", "options": [
            option("წინა", "front pads: show r1 and r2"), option("უკანა", "rear pads: show r3")]}
        result = run(service(Script(done(reply="Camry 2015-ზე ორივე გვაქვს.", question=asked,
                                         suggestions=[], outcome="asked", language="ka"))),
                     "ხუნდები მინდა", dialogue={"memory": KNOWN})
        self.assertEqual((result["status"], result["act"]), ("CLARIFICATION", "clarify"))
        self.assertEqual(result["answer"], "Camry 2015-ზე ორივე გვაქვს.\nწინა თუ უკანა?")
        self.assertEqual(result["quick_replies"], ["წინა", "უკანა"])
        self.assertEqual(result["question"], {"text": "წინა თუ უკანა?", "options": [
            {"label": "წინა", "value": "წინა"}, {"label": "უკანა", "value": "უკანა"}]})
        self.assertEqual(result["pending"]["options"][1]["meaning"], "rear pads: show r3")
        self.assertEqual(result["memory"]["question"], result["pending"])

    def test_a_tapped_choice_and_the_rows_in_notes_reach_the_model(self):
        memory = {**KNOWN, "language": "ka", "notes": "Camry 2015; pads found, r3 is the rear",
                  "car_label": "Toyota Camry 2015", "shown": ["p1"],
                  "question": {"question": "წინა თუ უკანა?", "options": [
                      option("წინა", "front pads: show r1"), option("უკანა", "rear: show r3")]},
                  "transcript": [{"user": "Camry 2015 ხუნდები",
                                  "assistant": "წინა თუ უკანა?"}]}
        script = Script(done())
        run(service(script), "უკანა", dialogue={"memory": memory, "choice": 2,
                                                   "pending": memory["question"]})
        request = script.requests[0]
        self.assertEqual([message["role"] for message in request["messages"]],
                         ["user", "assistant", "user"])
        self.assertEqual(request["messages"][0]["content"], "Camry 2015 ხუნდები")
        sent = context_of(request)
        self.assertEqual(sent["customer_message"], "უკანა")
        self.assertEqual(sent["tapped_option"],
                         {"index": 2, "label": "უკანა", "meaning": "rear: show r3"})
        self.assertEqual(sent["your_question"]["text"], "წინა თუ უკანა?")
        self.assertEqual(len(sent["your_question"]["options"]), 2)
        self.assertEqual((sent["notes"], sent["car_label"]),
                         (memory["notes"], "Toyota Camry 2015"))
        # The card of the last turn and the row the notes and meanings name, re-read.
        self.assertEqual([item[0] for item in sent["rows"]["rows"]], ["r1", "r3"])
        self.assertEqual(sent["rows"]["rows"][1][1], REAR)
        self.assertEqual((sent["audience"], sent["language_so_far"]), ("staff", "ka"))

    def test_typed_numbers_are_looked_up_before_the_model_starts(self):
        script = Script(done())
        run(service(script), "გაქვთ PN1521 ან XYZ99999? 2015 წელი, W212")
        typed = context_of(script.requests[0])["typed_numbers"]
        self.assertEqual([item["typed"] for item in typed["found"]], ["PN1521"])
        self.assertEqual({item[4] for item in typed["found"][0]["rows"]}, {"PN1521", "37677"})
        self.assertEqual(typed["without_rows"], ["XYZ99999"])
        self.assertEqual(typed_tokens("5W-30 W212 2015 [VIN] 31835AA030"),
                         ["5W-30", "31835AA030"])

    def test_other_revisions_and_earlier_numbers_bring_their_rows(self):
        rows = [*ROWS, row("p7", "30040000", FRONT, "VAG WV", "SATO", "PN0001",
                           "CX0001/7QX445566B", "185", "2", "0")]
        script = Script(done())
        run(service(script, rows=rows), "გაქვთ 7QX445566D?")
        typed = context_of(script.requests[0])["typed_numbers"]
        self.assertEqual(typed["without_rows"], ["7QX445566D"])
        self.assertNotIn("found", typed)
        revised = typed["other_revisions"]
        self.assertEqual([(item["typed"], item["carried_as"]) for item in revised],
                         [("7QX445566D", ["7QX445566B"])])
        self.assertEqual([item[4] for item in revised[0]["rows"]], ["PN0001"])

        # Numbers of the staff-handled messages before: rows only, a miss is not repeated,
        # and a number the new message types again is answered in typed_numbers alone.
        memory = {"transcript": [{"user": "PN1521 ან XYZ99999 გაქვთ? [VIN]",
                                  "assistant": "ახლავე გადავამოწმებ"}]}
        script = Script(done())
        run(service(script), "რა ღირს?", dialogue={"memory": memory})
        earlier = context_of(script.requests[0])["earlier_typed_numbers"]
        self.assertEqual([item["typed"] for item in earlier["found"]], ["PN1521"])
        self.assertNotIn("without_rows", earlier)
        script = Script(done())
        run(service(script), "PN1521 რა ღირს?", dialogue={"memory": memory})
        sent = context_of(script.requests[0])
        self.assertNotIn("earlier_typed_numbers", sent)
        self.assertEqual([item["typed"] for item in sent["typed_numbers"]["found"]], ["PN1521"])

    def test_plain_text_is_nudged_once_then_delivered(self):
        script = Script(says({"type": "text", "text": "მოგესალმებით!"}, stop="end_turn"),
                        done(reply="მოგესალმებით!"))
        result = run(service(script), "გამარჯობა")
        self.assertEqual(result["answer"], "მოგესალმებით!")
        self.assertEqual(script.requests[1]["messages"][-1]["content"][0]["text"],
                         assistant_prompt.NUDGE)
        twice = Script(says({"type": "text", "text": "მოგესალმებით! რა გჭირდებათ"},
                            stop="end_turn"),
                       says({"type": "text", "text": "მოგესალმებით! რა გჭირდებათ"},
                            stop="end_turn"))
        result = run(service(twice), "გამარჯობა")
        self.assertEqual((result["status"], result["answer"]),
                         ("OK", "მოგესალმებით! რა გჭირდებათ"))
        self.assertEqual(len(twice.requests), 2)

    def test_finish_beside_read_tools_is_sent_back_and_the_reads_still_answered(self):
        script = Script(
            says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])),
                 use("finish", finish())),
            done())
        result = run(service(script), "Camry ხუნდები")
        read, finished = tool_results(script.requests[1])
        self.assertEqual((read[0]["total"], read[1]), (3, False))
        self.assertIn("together with read tools", finished[0]["error"])
        self.assertTrue(finished[1])
        self.assertEqual(result["status"], "OK")


class FinishCheckTests(unittest.TestCase):
    """Each failed check goes back with what was wrong and what to do; the model corrects."""

    def corrected(self, bad, expected, *, memory=KNOWN, repository=None, good=None):
        script = Script(done(**bad), done(**(good or {})))
        result = run(service(script, repository=repository), "Camry 2015 წინა ხუნდები",
                     dialogue={"memory": memory})
        self.assertEqual(len(script.requests), 2, expected)
        (content, is_error), = tool_results(script.requests[1])
        said = json.dumps(content, ensure_ascii=False)
        self.assertTrue(is_error, expected)
        self.assertFalse(content["ok"])
        self.assertIn(expected, said)
        self.assertTrue(all(error["fix"] for error in content["errors"]))
        self.assertEqual(result["status"], "OK", expected)
        self.assertEqual(len(result["diagnostics"]["corrections"]), 1)
        return content, result

    def test_every_check_explains_itself(self):
        many = [option(f"ვარიანტი {number}") for number in range(9)]
        cases = [
            ({"reply": "ხუნდი ღირს 85 ლარი."}, "Do not state prices"),
            ({"reply": "ფასი $40-დან."}, "amount of money"),
            ({"reply": "ა" * 601}, "the limit is 600"),
            ({"reply": "რომელი წლისაა?"}, "Move the question into `question`"),
            ({"reply": "რომელი მანქანაა? მომწერეთ მარკა და მოდელი."},
             "reply asks a question, but `question` is empty"),
            ({"reply": "მომწერეთ მარკა და მოდელი.", "outcome": "asked"},
             "outcome is asked, but `question` is empty"),
            ({"products": [product()]}, "A products entry names no rows"),
            ({"products": [product("r1", "r99")]},
             "finish names r99, which you were never shown"),
            ({"products": [product("r1", "r3", evidence="ხუნდი წნ")]},
             "or give r3 an entry of its own"),
            ({"question": {"text": "წინა თუ უკანა?", "options": [option("წინა")]}},
             "it needs 2 to 8"),
            ({"question": {"text": "რომელი?", "options": many}}, "it needs 2 to 8"),
            ({"question": {"text": "რომელი?", "options": [option("წინა"), option("წინა")]}},
             "More than one option is labelled"),
            ({"question": {"text": "რომელი?", "options": [option("წ" * 41), option("უკანა")]}},
             "the limit is 40"),
            ({"question": {"text": "", "options": [option("წინა"), option("უკანა")]}},
             "options but no text"),
            ({"options": ["წინა", "უკანა"]}, "options but no text"),
            ({"question": "წინა თუ უკანა?", "options": ["წინა"]}, "it needs 2 to 8"),
            ({"question": "წინა თუ უკანა?", "options": ["წინა", " წინა "]},
             "More than one option is labelled"),
            ({"products": [product("r99")]}, "finish names r99, which you were never shown"),
            ({"products": [product("r1", evidence="CAMRY 2017>")]}, "is not text of that row"),
            ({"products": [product("r1", evidence="")]}, "r1 has no evidence"),
            ({"products": [product("r1", confidence="confirmed")]},
             "Use listed when the row's own text names this car and year, otherwise check"),
            ({"products": [product("r1", confidence="exact")]},
             "Use confirmed, listed, spec, check or depends"),
            ({"products": [product("r1", confidence="check", reason="")]},
             "r1 is marked check without a reason"),
            ({"products": [product("r1", confidence="spec", reason=" ")]},
             "which specification of the row is the one this car needs"),
            ({"reply": "წინა ხუნდი r1 ქვემოთაა."}, "names the row handle r1"),
            ({"products": [product("r1", reason="იგივეა, რაც r2/r3")]},
             "names the row handles r2, r3"),
            ({"products": [product("r1", group="r1 და r2")]}, "Handles are private to you"),
            ({"question": {"text": "r1 თუ r2?", "options": [option("წინა"), option("უკანა")]}},
             "names the row handles r1, r2"),
            ({"question": {"text": "რომელი?", "options": [option("r3"), option("უკანა")]}},
             "names the row handle r3"),
            ({"suggestions": ["r4-ის ნახვა"]}, "names the row handle r4"),
            ({"products": [product("r1", reason="ღირს 85 ₾")]}, "states an amount of money"),
            ({"suggestions": []}, "no products, no question and no suggestions"),
            ({"reply": "", "suggestions": [option("ძებნა")]}, "reply is empty"),
            ({"language": "de"}, "language must be ka, en or ru"),
        ]
        for bad, expected in cases:
            with self.subTest(expected=expected):
                self.corrected(bad, expected)

    def test_a_handle_is_private_and_only_this_conversations_handles_are_handles(self):
        # A token that looks like a handle but names no row of this conversation is a word
        # like any other: an Audi R8, a bearing size, a row never shown.
        script = Script(done(reply="Audi R8 და r99 ზომის საკისარი ქვემოთაა.",
                             products=[product("r1", reason="ერგება r77 დისკს")]))
        result = run(service(script), "ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual((len(script.requests), len(result["cards"])), (1, 1))
        # Notes are the model's own and may name rows.
        script = Script(done(notes="front: r1 r2; rear: r3", products=[product("r1")]))
        self.assertEqual(len(run(service(script), "ხუნდები",
                                 dialogue={"memory": KNOWN})["cards"]), 1)
        self.assertEqual(len(script.requests), 1)
        # An answer that never passes loses every piece that names a handle.
        bad = {"reply": "წინა ხუნდები გვაქვს. r1 და r2 ერთი და იგივეა.", "outcome": "offered",
               "products": [product("r1", reason="იგივეა, რაც r2", group="r1-ის ჯგუფი"),
                            product("r2", reason="შეამოწმეთ წელი", group="ხუნდები")],
               "suggestions": ["r3-ის ნახვა", "უკანა ხუნდები"]}
        script = Script(done(**bad), done(**bad), done(**bad))
        result = run(service(script), "ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual(result["answer"], "წინა ხუნდები გვაქვს.")
        self.assertEqual([(card["reason"], card["group"]) for card in result["cards"]],
                         [("", ""), ("შეამოწმეთ წელი", "g1")])
        self.assertEqual(result["suggestions"], [{"label": "უკანა ხუნდები",
                                                  "value": "უკანა ხუნდები"}])
        seen = json.dumps({key: result[key] for key in (
            "answer", "cards", "groups", "suggestions", "question")}, ensure_ascii=False)
        self.assertIsNone(re.search(r"\br\d\b", seen))
        for told in ("A row handle (r12) is yours alone", "no handles"):
            self.assertIn(told, assistant_prompt.SYSTEM_PROMPT)

    def test_a_product_that_is_not_tied_to_a_car_is_offered_by_its_specification(self):
        memory = {"handles": {**KNOWN["handles"], "r5": "p5"}}
        script = Script(done(
            reply="5W-30 ზეთი ქვემოთაა.", outcome="offered",
            products=[product("r5", confidence="spec", evidence="5W-30",
                              reason="5W-30, როგორც მოითხოვეთ", group="ძრავის ზეთი")]))
        result = run(service(script), "5W-30 ძრავის ზეთი მჭირდება", dialogue={"memory": memory})
        self.assertEqual(len(script.requests), 1)
        card = result["cards"][0]
        self.assertEqual((card["product"]["source_product_id"], card["confidence"],
                          card["basis"], card["reason"]),
                         ("p5", "spec", "spec", "5W-30, როგორც მოითხოვეთ"))
        # The staff API carries the new level like the others.
        model = ConversationTurn.model_validate(clamp_turn({
            **result, "conversation_id": "c", "turn_id": "t", "status": "COMPLETE"}))
        self.assertEqual(model.cards[0].confidence, "spec")
        from automotive.assistant_tools import CONFIDENCES, JUDGEMENTS, REASONED

        self.assertEqual((CONFIDENCES, JUDGEMENTS, REASONED),
                         (("confirmed", "listed", "spec", "check"),
                          ("confirmed", "listed", "spec", "check", "depends"),
                          ("spec", "check")))
        for told in ("spec      - the row is not tied to a car by its text",
                     "Never listed:\n              such a row lists no car",
                     "reason: required for check and spec"):
            self.assertIn(told, assistant_prompt.SYSTEM_PROMPT)
        # A row that cannot keep its level after two corrections is shown at the most
        # cautious one, spec included.
        bad = {"products": [product("r5", confidence="spec", evidence="not in the row",
                                    reason="5W-30")], "outcome": "offered"}
        script = Script(done(**bad), done(**bad), done(**bad))
        result = run(service(script), "5W-30", dialogue={"memory": memory})
        self.assertEqual([(card["confidence"], card["evidence"]) for card in result["cards"]],
                         [("spec", "")])

    def test_a_car_named_like_a_currency_is_not_a_price(self):
        script = Script(done(reply="2015 ლარგუსზე ფილტრი ქვემოთაა, 5W-30 ზეთიც გვაქვს."))
        self.assertEqual(run(service(script), "ლარგუსი")["status"], "OK")
        self.assertEqual(len(script.requests), 1)

    def test_a_finish_cut_off_at_the_length_limit_is_told_so(self):
        cut = says(use("finish", {"reply": "ხუნდები ქვემოთაა.", "language": "ka"}),
                   stop="max_tokens")
        script = Script(cut, done(products=[product(" R1 ")]))
        result = run(service(script), "ხუნდები", dialogue={"memory": KNOWN})
        errors = tool_results(script.requests[1])[0][0]["errors"]
        self.assertEqual(errors[0]["error"], "Your output was cut off at the length limit.")
        self.assertIn("outcome must be one of", errors[1]["error"])
        # A handle is read as the model wrote it, whatever its case and spacing.
        self.assertEqual(len(result["cards"]), 1)

    def test_evidence_may_quote_name_vehicle_brand_or_a_number(self):
        for evidence in ("camry  2006->", "ხუნდი წნ", "SATO", "PN1521", "gdb3429", "30023433",
                         "\"CAMRY 2006->\""):
            with self.subTest(evidence=evidence):
                script = Script(done(products=[product("r1", evidence=evidence)]))
                result = run(service(script), "ხუნდები", dialogue={"memory": KNOWN})
                self.assertEqual(len(result["cards"]), 1)
                self.assertEqual(len(script.requests), 1)

    def test_a_row_that_left_the_catalogue_is_sent_back(self):
        repository = Repository()
        del repository.rows["p1"]
        _, result = self.corrected({"products": [product("r1")]},
                                   "r1 is no longer in the catalogue", repository=repository,
                                   good={"products": [product("r2")]})
        self.assertEqual([card["product"]["source_product_id"] for card in result["cards"]],
                         ["p2"])

    def test_all_failures_of_one_finish_come_back_together(self):
        content, _ = self.corrected(
            {"reply": "ღირს 85 ლარი?", "products": [product("r99"), product("r1", evidence="")]},
            "Do not state prices")
        self.assertEqual(len(content["errors"]), 4)

    def test_after_two_corrections_the_valid_part_of_the_answer_is_used(self):
        bad = {"reply": "წინა ხუნდები გვაქვს. ღირს 85 ლარი.", "outcome": "offered",
               "products": [product("r1", confidence="confirmed"), product("r99"),
                            product("r2", evidence="not in the row")]}
        script = Script(done(**bad), done(**bad), done(**bad))
        result = run(service(script), "Camry 2015 წინა ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual(len(script.requests), 3)
        self.assertEqual((result["status"], result["answer"]), ("OK", "წინა ხუნდები გვაქვს."))
        self.assertEqual([(card["product"]["source_product_id"], card["confidence"],
                           card["evidence"]) for card in result["cards"]],
                         [("p1", "check", "CAMRY 2006->"), ("p2", "listed", "")])
        self.assertIn("ANSWER_CHECKS_FAILED", result["warnings"])
        self.assertEqual(result["diagnostics"]["fallback"], "CHECKS_FAILED")
        self.assertEqual(len(result["diagnostics"]["corrections"]), 3)

    def test_confirmed_needs_a_number_from_a_car_specific_source(self):
        supplied = {"status": "COMPLETE", "vehicle": {"make": "TOYOTA", "model": "Camry",
                                                      "model_year": 2015},
                    "sources": [{"source_id": "motorium", "status": "OK", "count": 1,
                                 "error_code": None}],
                    "items": [{"warehouse_records": [], "supplier_records": [{
                        "mpn": "GDB3429", "manufacturer": "TRW", "name": "Brake pad set",
                        "oem_codes": ["04465-33450"], "status": "PROVISIONAL"}]}]}
        federation = Search(**supplied)

        def judge(request):
            found = handles(request)
            return done(products=[product(found["PN1521"], confidence="confirmed",
                                          evidence="GDB3429")], outcome="offered")

        script = Script(says(use("supplier_parts", {"part_group": "brake_pads", **car(
            make="Toyota", model="Camry", year=2015, engine_litres=2.5, fuel="petrol")})), judge)
        result = run(service(script), "Camry 2015 წინა ხუნდები", search_service=federation)
        self.assertEqual(federation.started, [(None, "brake pads", 2015, {
            "make": "Toyota", "model": "Camry", "model_year": 2015, "displacement_l": 2.5,
            "fuel": "petrol"})])
        (supplier, is_error), = tool_results(script.requests[1])
        self.assertFalse(is_error)
        self.assertEqual(supplier["columns"][-1], "via")
        self.assertEqual({item[4]: item[-1] for item in supplier["rows"]}, {
            "PN1521": "TRW GDB3429 = cross number", "37677": "TRW GDB3429 = cross number"})
        self.assertEqual(supplier["numbers_not_in_catalogue"], ["OE 04465-33450 (Brake pad set)"])
        self.assertEqual(supplier["sources"], [{"source": "motorium", "status": "OK", "count": 1}])
        self.assertEqual(result["cards"][0]["confidence"], "confirmed")
        self.assertIn("GDB3429", result["memory"]["sourced"])
        # The number stays evidence for this car for the rest of the conversation ...
        memory = {**result["memory"], "car_label": "Toyota Camry 2015"}
        same_car = {"products": [product("r1", confidence="confirmed")],
                    "car_label": "Toyota Camry 2015"}
        again = run(service(Script(done(**same_car))), "კიდევ ერთხელ",
                    dialogue={"memory": memory})
        self.assertEqual(again["cards"][0]["confidence"], "confirmed")
        self.assertIn("GDB3429", again["memory"]["sourced"])
        # ... and is no evidence once the model names another car.
        other_car = Script(done(**{**same_car, "car_label": "Lexus ES 350 2010"}),
                           done(products=[product("r1")], car_label="Lexus ES 350 2010"))
        switched = run(service(other_car), "ES 350-ზეც მინდა", dialogue={"memory": memory})
        self.assertIn("marked confirmed", tool_results(other_car.requests[1])[0][0][
            "errors"][0]["error"])
        self.assertEqual(switched["memory"]["sourced"], [])

    def test_staff_rules_bar_a_row_for_this_car(self):
        rules = Rules(never_offer=(NeverOffer("30023433", "TOYOTA", "Camry", "wrong pad"),))
        answer = {"products": [product("r1"), product("r2")], "outcome": "offered"}
        script = Script(done(car_label="Toyota Camry 2015", **answer))
        result = run(service(script, rules=rules), "ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual(len(script.requests), 1)
        self.assertEqual([card["product"]["source_product_id"] for card in result["cards"]],
                         ["p2"])
        self.assertEqual(result["diagnostics"]["removed"], [
            {"product": "p1", "rule": "never_offer", "reason": "wrong pad"}])
        # The decoded car decides when there is one; another make is not barred.
        other = Script(done(car_label="Honda Accord 2015", **answer))
        self.assertEqual(len(run(service(other, rules=rules), "ხუნდები",
                                 dialogue={"memory": KNOWN})["cards"]), 2)
        decoded = Script(done(car_label="", **answer))
        result = run(service(decoded, rules=rules), "ხუნდები " + VIN, dialogue={"memory": KNOWN})
        self.assertEqual(len(result["cards"]), 1)
        # Nothing left to show: the model is told why and answers again.
        alone = Script(done(car_label="Toyota Camry", products=[product("r1")], suggestions=[]),
                       done(reply="ამ მანქანაზე შესაფერისი ხუნდი ვერ მოიძებნა."))
        result = run(service(alone, rules=rules), "ხუნდები", dialogue={"memory": KNOWN})
        self.assertIn("barred for this car by a staff rule (r1: wrong pad)",
                      tool_results(alone.requests[1])[0][0]["errors"][0]["error"])
        self.assertEqual(result["cards"], [])


class ToolTests(unittest.TestCase):
    def one(self, name, args, **kwargs):
        """Run one read tool through a turn and return (its result, is_error)."""
        script = Script(says(use(name, args)), done())
        chat = kwargs.pop("chat", None) or service(script, **kwargs.pop("settings", {}))
        if chat.client is None:
            chat.client = SimpleNamespace(messages=SimpleNamespace(create=script.create))
        run(chat, kwargs.pop("message", "ხუნდები"), **kwargs)
        return tool_results(script.requests[1])[0]

    def test_an_fmsi_number_looked_up_finds_the_pads_that_write_it_with_a_plate(self):
        sibling = row("p8", "30050000", FRONT, "VW GOLF V PASSAT", "SATO", "PN0002",
                      "GDB0002/D9999-8000", "90", "2", "0")
        content, is_error = self.one("lookup_codes", {"numbers": ["D9999"], "vehicle_hint": ""},
                                     settings={"rows": [*ROWS, sibling]})
        self.assertFalse(is_error)
        found = content["numbers"][0]
        self.assertEqual([(item[4], item[-1]) for item in found["rows"]],
                         [("PN0002", "cross number")])

    def test_tools_are_strict_and_the_last_one_is_a_cache_breakpoint(self):
        tools = tool_definitions()
        self.assertEqual([tool["name"] for tool in tools], [
            "search_catalog", "lookup_codes", "equivalents", "supplier_parts", "oil_guide",
            "car_numbers", "research", "shop_info", "finish"])
        self.assertEqual([bool(tool.get("cache_control")) for tool in tools],
                         [False] * 8 + [True])
        forbidden = {"minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
                     "pattern", "anyOf", "oneOf", "allOf", "format"}

        def walk(schema, path):
            self.assertFalse(forbidden & set(schema), path)
            self.assertIsInstance(schema.get("type"), str, path)
            if schema["type"] == "object":
                self.assertIs(schema["additionalProperties"], False, path)
                self.assertEqual(schema["required"], list(schema["properties"]), path)
                for name, child in schema["properties"].items():
                    self.assertTrue(child.get("description"), f"{path}.{name}")
                    walk(child, f"{path}.{name}")
            elif schema["type"] == "array":
                walk(schema["items"], path + "[]")

        for tool in tools:
            self.assertIs(tool["strict"], True)
            self.assertTrue(tool["description"])
            walk(tool["input_schema"], tool["name"])
        self.assertEqual(tool_definitions(), tools)  # Byte-stable, or the cache never hits.

    def test_every_supplier_group_is_one_the_federation_classifies_and_a_source_answers(self):
        for group in SUPPLIER_GROUPS:
            query = supplier_query(group)
            self.assertEqual(category_for_query(query), group)
            self.assertTrue(categories_for(query) or VINSearchCoordinator._scope(query), group)
        self.assertEqual(category_for_query(OIL_QUERY), "engine_oil")
        self.assertTrue(set(FILTER_GROUPS) | set(OIL_GROUPS) <= set(SUPPLIER_GROUPS))
        enum = tool_definitions()[3]["input_schema"]["properties"]["part_group"]["enum"]
        self.assertEqual(enum, list(SUPPLIER_GROUPS))

    def test_search_errors_and_empty_results_say_what_to_do(self):
        content, is_error = self.one("search_catalog", search(vehicle=["E"], part=["ხუნდი"]))
        self.assertTrue(is_error)
        self.assertEqual(set(content), {"ok", "error", "fix"})
        self.assertIn("1 letter", content["error"])
        self.assertIn("chassis code", content["fix"])
        content, is_error = self.one("search_catalog", search())
        self.assertTrue(is_error)
        self.assertIn("all empty", content["error"])
        content, is_error = self.one("search_catalog", search(["TOYOTA"], ["CAMRI"], ["ხუნდი"]))
        self.assertFalse(is_error)
        self.assertEqual(content["similar_spellings"][0]["catalogue_token"], "CAMRY")
        content, is_error = self.one("search_catalog", search(["HONDA"], [], ["ხუნდი"]))
        self.assertEqual((content["total"], content["rows"], is_error), (0, [], False))
        self.assertEqual(content["why"], {"make_terms_alone": 0, "part_terms_alone": 3})
        content, is_error = self.one("nonexistent", {})
        self.assertTrue(is_error)
        self.assertIn("no tool named", content["error"])

    def test_a_search_marks_rows_that_share_two_or_more_numbers(self):
        from automotive.assistant_tools import same_parts

        content, is_error = self.one("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))
        self.assertFalse(is_error)
        self.assertEqual(content["columns"][-1], "same_part")
        letters = {item[4]: item[-1] for item in content["rows"]}
        # The two front pads share D1222, GDB3429, KD2765 and PN1521; the rear pad stands alone.
        self.assertEqual(letters, {"PN1521": "A", "37677": "A", "04466-33180": ""})

        def part(code, cross):
            return SimpleNamespace(code=code, cross_codes=tuple(cross.split("/")))

        rows = [part("H52", "19200-RAA-A01/19200-RBB-003"), part("GWHO40A", "19200-P72-A01/1625"),
                part("35-04-431", "19200-RAA-A01/19200-RBB-003/GWHO50A"),
                part("GWHO63", "19200-RAA-A01"),  # one shared number is no link
                part("35-04-432", "19200-P72-A01/1625/GWHO-40A")]
        self.assertEqual(same_parts(rows), ["A", "B", "A", "", "B"])
        self.assertEqual(same_parts([]), [])
        # A row's own number among another row's numbers is a link by itself, unless it is
        # too short to name one part (a grade, a size).
        rows = [part("OE118J", "SH4031P"), part("OE685/1", "04152-31090/OE118J"),
                part("5W30", "600044002"), part("HX8", "5W30/550040")]
        self.assertEqual(same_parts(rows), ["A", "A", "", ""])

    def test_a_search_adds_rows_in_stock_that_carry_the_numbers_of_its_out_of_stock_rows(self):
        filter_name, other = "ზეთის ფილტრი", "ფილტრი ზეთის"
        rows = [
            row("f1", "301", filter_name, "TOYOTA/LEXUS RAV 4 CAMRY RX350", "JS-ASAKASHI",
                "OE118J", "SH4031P/SH4087P", store="0"),
            row("f2", "302", other, "TOYOTA/LEXUS RX350 NX300H", "JAPANPARTS", "FO-ECO077",
                "OE118J/SH4087P/HU7019z", store="1"),
            row("f3", "303", other, "TOYOTA", "FILTRON", "OE685/1", "04152-31090/OE118J",
                store="0", tuz11="7"),
            # One number in common with a row that is itself no row of the result: no link.
            row("f4", "304", filter_name, "TOYOTA/LEXUS RX450", "HENGST", "E1024HD234",
                "A210372/HU7019z", store="2"),
            row("f5", "305", filter_name, "TOYOTA/LEXUS CAMRY 2001>", "SAKURA", "C1109",
                "OP572", store="3"),
            # The twin of a row that is in stock is not looked for: f5 is there to buy.
            row("f6", "306", other, "TOYOTA MAZDA", "FILTRON", "OP572", "C1109/W683", store="4"),
            # A twin that is out of stock itself helps nobody.
            row("f7", "307", other, "TOYOTA", "IDOCHI", "IDO81007H", "OE118J/SH4087P",
                store="0")]
        rows += [row(f"x{number}", f"4{number:02d}", filter_name, "TOYOTA/LEXUS COROLLA",
                     "ASHIKA", f"10-02-{number:03d}", "", store="1") for number in range(32)]
        content, is_error = self.one(
            "search_catalog", search(["TOYOTA"], ["CAMRY"], [filter_name, other]),
            settings={"rows": rows})
        self.assertFalse(is_error)
        # More rows match than a result holds, so only the CAMRY rows are listed ...
        self.assertEqual([(item[4], item[7], item[-1]) for item in content["rows"]],
                         [("C1109", 3, ""), ("OE118J", 0, "A")])
        # ... and under them the rows in stock that are the out-of-stock one by number.
        self.assertEqual([(item[4], item[2], item[-1])
                          for item in content["in_stock_under_other_cars"]],
                         [("FO-ECO077", "TOYOTA/LEXUS RX350 NX300H", "A"),
                          ("OE685/1", "TOYOTA", "A")])
        self.assertEqual(content["columns"][-1], "same_part")
        # A result whose rows are all in stock, or have no twin, has no such list.
        content, _ = self.one("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))
        self.assertNotIn("in_stock_under_other_cars", content)

    def test_in_stock_only_still_reaches_the_twin_of_an_out_of_stock_row_for_the_car(self):
        name = "ზეთის ფილტრი"
        rows = [row("h1", "501", name, "HONDA CIVIC CRV", "JS-ASAKASHI", "C809J",
                    "15400-RTA-003/15400-PLM-A02", store="0"),
                row("h2", "502", name, "HONDA ACCORD", "ASHIKA", "10-04-410",
                    "C809J/15400-RTA-003", store="2"),
                row("h3", "503", name, "HONDA CIVIC", "ASHIKA", "10-04-401", "", store="1"),
                # Out of stock with no twin in stock: stays hidden.
                row("h4", "504", name, "HONDA CIVIC", "SAKURA", "C1821", "", store="0")]
        # More rows than a result holds, so only the CIVIC rows are listed.
        rows += [row(f"j{number}", f"6{number:02d}", name, "HONDA JAZZ", "ASHIKA",
                     f"10-04-{number + 600}", "", store="1") for number in range(32)]
        args = {**search(["HONDA"], ["CIVIC"], [name]), "in_stock_only": True}
        content, is_error = self.one("search_catalog", args, settings={"rows": rows})
        self.assertFalse(is_error)
        self.assertEqual([item[4] for item in content["rows"]], ["10-04-401"])
        self.assertEqual([(item[4], item[2], item[-1])
                          for item in content["in_stock_under_other_cars"]],
                         [("10-04-410", "HONDA ACCORD", "A")])
        self.assertEqual([(item[4], item[7], item[-1])
                          for item in content["out_of_stock_rows_with_a_twin_in_stock"]],
                         [("C809J", 0, "A")])
        # Without in_stock_only the row is in the result itself, and nothing else is added.
        content, _ = self.one("search_catalog", {**args, "in_stock_only": False},
                              settings={"rows": rows})
        self.assertNotIn("out_of_stock_rows_with_a_twin_in_stock", content)
        self.assertEqual([item[4] for item in content["in_stock_under_other_cars"]],
                         ["10-04-410"])

    def test_stocked_rows_do_not_hide_an_unstocked_bridge_to_an_equivalent(self):
        name = "ზეთის ფილტრი"
        for count in (1, 30, 65):
            for in_stock_only in (True, False):
                for bridge_vehicle in ("HONDA CIVIC", "CIVIC"):
                    with self.subTest(count=count, in_stock_only=in_stock_only,
                                      bridge_vehicle=bridge_vehicle):
                        rows = [row(str(number), str(number), name, "HONDA CIVIC", "JS",
                                    f"DIRECT{number:04d}", store="1")
                                for number in range(1, count + 1)]
                        rows += [
                            row("bridge", "501", name, bridge_vehicle, "JS", "C809J",
                                "15400-RTA-003/15400-PLM-A02", store="0"),
                            row("twin", "502", name, "HONDA ACCORD", "ASHIKA", "ACCORD01",
                                "C809J/15400-RTA-003", store="2"),
                            # One shared cross-number does not make a transitive twin.
                            row("unrelated", "503", name, "HONDA JAZZ", "ASHIKA", "JAZZ001",
                                "15400-RTA-003", store="2"),
                            # Excluded positions must not seed equivalents either.
                            row("rear", "504", name + " REAR", "HONDA CIVIC", "JS",
                                "REAR001", "REAR002/REAR003", store="0"),
                            row("rear-twin", "505", name, "HONDA ACCORD", "JS", "REAR004",
                                "REAR002/REAR003", store="2"),
                        ]
                        args = {**search(["HONDA"], ["CIVIC"], [name], ["REAR"]),
                                "in_stock_only": in_stock_only, "limit": min(count, 30)}
                        content, is_error = self.one("search_catalog", args,
                                                     settings={"rows": rows})
                        self.assertFalse(is_error)
                        self.assertLessEqual(len(content["rows"]), args["limit"])
                        self.assertEqual([item[4] for item in
                                          content.get("in_stock_under_other_cars", [])],
                                         ["ACCORD01"])
                        explanations = [*content.get("rows_without_the_make_word", []),
                                        *content.get("out_of_stock_rows_with_a_twin_in_stock", [])]
                        self.assertIn("C809J", [item[4] for item in explanations])
                        self.assertNotIn("REAR001", [item[4] for item in explanations])
                        # Discovery supplies candidates and their source rows; it does not
                        # make a fitment decision or assign offer confidence.
                        self.assertNotIn("confidence", content["columns"])

    def test_hidden_bridge_discovery_keeps_extra_rows_bounded(self):
        name = "ზეთის ფილტრი"
        rows = [row("direct", "1", name, "HONDA CIVIC", "JS", "DIRECT001", store="1")]
        for number in range(15):
            rows.extend([
                row(f"bridge{number}", f"5{number}", name, "HONDA CIVIC", "JS",
                    f"SOURCE{number:03d}", f"CROSS{number:03d}/OTHER{number:03d}", store="0"),
                row(f"twin{number}", f"6{number}", name, "HONDA ACCORD", "ASHIKA",
                    f"TWIN{number:03d}", f"CROSS{number:03d}/OTHER{number:03d}", store="1"),
            ])
        args = {**search(["HONDA"], ["CIVIC"], [name]), "in_stock_only": True, "limit": 1}
        content, is_error = self.one("search_catalog", args, settings={"rows": rows})
        self.assertFalse(is_error)
        self.assertEqual(len(content["rows"]), 1)
        self.assertEqual(len(content["in_stock_under_other_cars"]), 10)
        self.assertEqual(len(content["out_of_stock_rows_with_a_twin_in_stock"]), 10)

    def test_new_hidden_bridges_do_not_displace_existing_stocked_candidates(self):
        name = "ზეთის ფილტრი"
        rows = [
            row("0", "0", name, "HONDA CIVIC", "JS", "BASE001",
                "CROSS001/CROSS002", store="0"),
            row("900", "900", name, "HONDA ACCORD", "JS", "ORIGINAL001",
                "CROSS001/CROSS002", store="1"),
        ]
        for number in range(1, 12):
            numbers = f"X{number:04d}/Y{number:04d}/Z{number:04d}"
            rows.extend([
                row(str(number), str(number), name, "HONDA CIVIC", "JS",
                    f"SOURCE{number:03d}", numbers, store="0"),
                row(f"80{number}", f"80{number}", name, "HONDA ACCORD", "JS",
                    f"TWIN{number:03d}", numbers, store="1"),
            ])
        args = {**search([], ["CIVIC"], [name]), "limit": 1}
        content, is_error = self.one("search_catalog", args, settings={"rows": rows})
        self.assertFalse(is_error)
        extra = content["in_stock_under_other_cars"]
        self.assertEqual(len(extra), 10)
        self.assertIn("ORIGINAL001", [item[4] for item in extra])

    def test_lookup_codes_reads_rows_and_tolerates_missing_crawled_data(self):
        content, is_error = self.one("lookup_codes", {"numbers": ["gdb-3429", "E1", "ZZ9999"],
                                                     "vehicle_hint": "Camry"})
        self.assertFalse(is_error)
        found, short, missing = content["numbers"]
        self.assertEqual(content["columns"][-1], "matched_in")
        self.assertEqual({item[4]: item[-1] for item in found["rows"]},
                         {"PN1521": "cross number", "37677": "cross number"})
        self.assertIn("Too short", short["note"])
        self.assertEqual(missing["note"], "No catalogue row carries this number.")
        # The fake repository has no database behind it.
        self.assertIn("could not be read", content["note"])

    def test_lookup_codes_brings_the_same_number_in_another_revision(self):
        rows = [*ROWS, row("p7", "30040000", FRONT, "VAG WV", "SATO", "PN0001",
                           "CX0001/7QX445566B", "185", "2", "0")]
        content, is_error = self.one("lookup_codes", {"numbers": ["7QX445566D"],
                                                     "vehicle_hint": ""},
                                     settings={"rows": rows})
        self.assertFalse(is_error)
        (entry,) = content["numbers"]
        self.assertEqual(entry["other_revisions"]["carried_as"], ["7QX445566B"])
        self.assertEqual([(item[4], item[-1]) for item in entry["other_revisions"]["rows"]],
                         [("PN0001", "other revision")])
        self.assertNotIn("note", entry)  # a row was found, in another revision

    def test_lookup_codes_adds_what_crawled_catalogues_say(self):
        class Crawled:
            def lookup(self, code, brand=None, limit=50):
                if "1521" not in code:
                    return {"status": "NOT_FOUND", "parts": [], "vehicles": [],
                            "cross_references": []}
                return {"status": "PROVISIONAL",
                        "parts": [{"brand": "SATO", "number": "PN1521", "name": "Brake pad"}],
                        "vehicles": [
                            {"make": "LEXUS", "model": "ES350", "year_from": 2006,
                             "year_to": 2012, "position": "front"},
                            {"make": "TOYOTA", "model": "Camry", "year_from": 2011,
                             "year_to": 2017, "engine": "2.5", "position": "front"}],
                        "cross_references": [
                            {"brand": "ABS", "number": "37677", "relation": "equivalent",
                             "source": "catalog"},
                            {"brand": "BOSCH", "number": "0986494154", "relation": "equivalent",
                             "source": "catalog"}]}

        def judge(request):
            return done(products=[product(handles(request)["PN1521"], confidence="confirmed",
                                          evidence="PN1521")])

        def corrected(request):
            # The cars a crawled catalogue lists for a number are no source for this car.
            return done(products=[product(handles(request)["PN1521"], confidence="check",
                                          evidence="PN1521", reason="ნომრით შემოწმება")])

        script = Script(says(use("lookup_codes", {"numbers": ["PN1521", "XYZ9999"],
                                                  "vehicle_hint": "Toyota Camry 2015"})), judge,
                        corrected)
        chat = service(script)
        chat.crawled_catalog = Crawled()
        result = run(chat, "PN1521 Camry 2015-ზე მოდის?")
        entry, unknown = tool_results(script.requests[1])[0][0]["numbers"]
        self.assertEqual(entry["catalogue_part"], ["SATO PN1521 Brake pad"])
        self.assertEqual(entry["listed_for"]["shown"][0], "TOYOTA Camry 2011-2017 2.5 front")
        self.assertEqual((entry["listed_for"]["total"], entry["listed_for"]["naming_the_hint"]),
                         (2, 1))
        self.assertEqual(entry["equivalents"][0]["number"], "ABS 37677")
        # A row the same result already carries in full is named by its handle only.
        listed = {item[4]: item[0] for item in entry["rows"]}
        self.assertEqual(entry["equivalents"][0]["rows_listed_above"], [listed["37677"]])
        self.assertNotIn("rows", entry["equivalents"][0])
        self.assertEqual(entry["equivalents_not_in_catalogue"], ["BOSCH 0986494154"])
        self.assertEqual(unknown["note"], "No catalogue row carries this number.")
        self.assertEqual(result["cards"][0]["confidence"], "check")

    def test_equivalents_lists_rows_sharing_numbers(self):
        content, is_error = self.one("equivalents", {"id": "r1"}, dialogue={"memory": KNOWN})
        self.assertFalse(is_error)
        self.assertEqual(content["columns"][-2:], ["shared_numbers", "same_brand"])
        self.assertEqual([(item[0], item[4], item[-1]) for item in content["rows"]],
                         [("r2", "37677", False)])
        self.assertEqual(set(content["rows"][0][-2].split("/")), {"PN1521", "GDB3429", "KD2765"})
        content, is_error = self.one("equivalents", {"id": "r77"})
        self.assertTrue(is_error)
        self.assertIn("r77, which you were never shown", content["error"])

    def test_supplier_parts_without_a_car_explains_what_is_missing(self):
        federation = Search()
        content, is_error = self.one("supplier_parts", {"part_group": "brake_pads", **car(
            model="Camry")}, search_service=federation)
        self.assertTrue(is_error)
        self.assertIn("make, year are missing", content["error"])
        self.assertIn("ask the customer", content["fix"])
        content, is_error = self.one("supplier_parts", {"part_group": "brake_pads", **car(
            use_vin=True)}, search_service=federation)
        self.assertIn("This conversation has no VIN", content["error"])
        content, is_error = self.one("oil_guide", car(), search_service=federation)
        self.assertIn("oil_guide needs the car", content["error"])
        content, is_error = self.one("supplier_parts", {"part_group": "tyres", **car()},
                                     search_service=federation)
        self.assertIn("not a group the outside sources answer", content["error"])
        self.assertEqual(federation.started, [])

    def test_supplier_parts_uses_the_vin_without_showing_it(self):
        federation = Search(vehicle={"make": "TOYOTA", "model": "Camry", "model_year": 2015})
        content, is_error = self.one("supplier_parts", {"part_group": "air_filter", **car(
            use_vin=True, engine_code="2AR-FE")}, search_service=federation,
            message="ჰაერის ფილტრი " + VIN)
        self.assertFalse(is_error)
        self.assertEqual(federation.started, [(VIN, "air filter", None,
                                               {"engine_code": "2AR-FE"})])
        self.assertEqual((content["car"], content["supplier_numbers"]),
                         ("TOYOTA Camry 2015", 0))
        self.assertIn("says nothing about fit", content["note"])

    def test_supplier_parts_asks_no_installation_that_cannot_answer(self):
        federation = Search()
        federation._external = {name: (None, False) for name in ("fuchs", "shell", "pakelo",
                                                                 "jsfilter")}
        federation._catalog = None
        federation.motorium_enabled = False
        content, is_error = self.one("supplier_parts", {"part_group": "brake_pads", **car(
            make="Toyota", model="Camry", year=2015)}, search_service=federation)
        self.assertTrue(is_error)
        self.assertIn("No outside source of this installation answers brake_pads",
                      content["error"])
        self.assertEqual(federation.started, [])

    def test_supplier_parts_returns_what_arrived_when_the_sources_are_slow(self):
        federation = Search(status="RUNNING", sources=[
            {"source_id": "vpic", "status": "SKIPPED", "error_code": "VIN_REQUIRED"},
            {"source_id": "catalog_api", "status": "RUNNING", "error_code": None}])
        with patch("automotive.assistant_tools.SUPPLIER_SECONDS", 0.2):
            content, is_error = self.one("supplier_parts", {"part_group": "brake_pads", **car(
                make="Toyota", model="Camry", year=2015)}, search_service=federation)
        self.assertFalse(is_error)
        self.assertEqual(content["status"], "still_running")
        self.assertIn("the same call next turn", content["note"])
        self.assertEqual(content["sources"], [
            {"source": "vpic", "status": "SKIPPED", "error": "VIN_REQUIRED"},
            {"source": "catalog_api", "status": "RUNNING"}])

    def test_supplier_parts_reads_catalogue_articles_and_skips_the_warehouse_stage(self):
        import threading

        federation = Search(status="RUNNING", sources=[
            {"source_id": "vpic", "status": "SKIPPED", "error_code": "VIN_REQUIRED"},
            {"source_id": "catalog_api", "status": "OK", "count": 1, "error_code": None},
            {"source_id": "warehouse", "status": "RUNNING", "error_code": None}])
        # The federation keeps the catalogue API's articles in the job, not in its snapshot.
        federation._lock = threading.RLock()
        federation._jobs = {"job": {"catalog_items": [{
            "code": "PN 1521", "supplier": "SATO", "product": "Brake Pad Set",
            "oem_codes": ["04465-33450"]}]}}
        started = time.monotonic()
        with patch("automotive.assistant_tools.SETTLE_SECONDS", 0.05):
            content, is_error = self.one("supplier_parts", {"part_group": "brake_pads", **car(
                make="Toyota", model="Camry", year=2015)}, search_service=federation)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((content["status"], is_error), ("complete", False))
        self.assertEqual({item[4]: item[-1] for item in content["rows"]}, {
            "PN1521": "SATO PN 1521 = own number", "37677": "SATO PN 1521 = cross number"})
        self.assertEqual(content["numbers_not_in_catalogue"],
                         ["OE 04465-33450 (Brake Pad Set)"])
        self.assertEqual(content["supplier_numbers"], 2)

    def test_oil_guide_summarises_the_selectors(self):
        def report(provider, grade, litres, code):
            return {"provider": provider, "status": "PROVISIONAL", "engine_code": code,
                    "vehicle_label": "TOYOTA Camry 2015", "candidates": [],
                    "items": [{"kind": "oil", "component": "ENGINE", "capacity": litres,
                               "product_name": f"Helix Ultra {grade}", "viscosity": grade,
                               "product_code": "550046", "approvals": ["API SN"]}]}

        agreed = Search(fluids=[report("Shell", "0W-20", "4.4", "2AR-FE"),
                                report("Pakelo", "0W-20", "4.4", "2AR-FE")],
                        sources=[{"source_id": "shell", "status": "OK", "count": 1}])
        script = Script(
            says(use("oil_guide", car(make="Toyota", model="Camry", year=2015))),
            done(products=[product("r1", evidence="5W-30", group="ძრავის ზეთი")]))
        result = run(service(script), "ზეთი Camry 2015",
                     search_service=agreed, dialogue={"memory": {"handles": {"r1": "p5"}}})
        self.assertEqual((result["status"], len(result["cards"])), ("OK", 1))  # the model's own
        content, is_error = tool_results(script.requests[1])[0]
        self.assertEqual((content["status"], is_error), ("answered", False))
        self.assertIn("engine oils of these grades", content["note"])
        self.assertEqual(content["needs"], {
            "grades": ["0W-20"], "capacity_l": 4.4, "engine_code": "2AR-FE",
            "sources": ["Shell", "Pakelo"], "approvals": ["API SN"],
            "products": ["Helix Ultra 0W-20"]})
        self.assertEqual(agreed.started[0][:2], (None, "engine oil"))
        self.assertEqual(result["requirements"], {
            "grades": ["0W-20"], "capacity_l": 4.4, "engine_code": "2AR-FE",
            "sources": ["Shell", "Pakelo"]})
        self.assertIn("550046", result["memory"]["sourced"])

        split = Search(fluids=[report("Shell", "0W-20", "4.4", "2AR-FE"),
                               report("Pakelo", "5W-30", "6.1", "2GR-FE"),
                               {"provider": "Fuchs", "status": "CONFLICTED", "items": [],
                                "candidates": ["Camry 2.5 (P)", "Camry 3.5 V6 (P)"]}])
        content, is_error = self.one("oil_guide", car(make="Toyota", model="Camry", year=2015),
                                     search_service=split)
        self.assertEqual(content["status"], "sources_disagree")
        self.assertEqual([item["grades"] for item in content["variants"]],
                         [["0W-20"], ["5W-30"]])
        self.assertEqual(content["engine_variants"], ["Camry 2.5 (P)", "Camry 3.5 V6 (P)"])
        self.assertNotIn("needs", content)
        content, _ = self.one("oil_guide", car(make="Toyota", model="Camry", year=2015),
                              search_service=Search())
        self.assertEqual(content["status"], "no_answer")

    def test_oil_guide_verifies_exact_sku_only_after_rechecking_catalog_identity(self):
        vehicle = {"make": "Toyota", "model": "Camry", "model_year": 2015,
                   "engine_code": "2AR-FE"}
        reviewed = {"authority": "manufacturer", "reuse_permission": "owned",
                    "review_status": "approved", "reviewed_by": "test reviewer"}

        class OilRepository(Repository):
            def vehicle_specifications(self, configuration):
                return [{**reviewed, "category": "engine_oil", "applicability": vehicle,
                         "approval": "TEST-OIL", "viscosity": "5W-30"}]

            def oil_approval_products(self):
                return [(self.get_product("p5"), [{**reviewed, "product_id": "p5",
                         "approval": "TEST-OIL", "viscosity": "5W-30",
                         "source_fingerprint": "f" * 16}])]

        script = Script(
            says(use("oil_guide", car(make="Toyota", model="Camry", year=2015,
                                     engine_code="2AR-FE"))),
            done(products=[product("r1", confidence="confirmed", evidence="5W-30")],
                 outcome="offered"),
        )
        federation = Search()
        result = run(service(script, repository=OilRepository()),
                     "Camry 2015 2AR-FE ზეთი", search_service=federation)

        content, is_error = tool_results(script.requests[1])[0]
        self.assertFalse(is_error)
        self.assertEqual(content["status"], "verified")
        self.assertEqual(content["rows"][0][-1], "TEST-OIL")
        self.assertEqual(federation.started, [])
        self.assertEqual(result["cards"][0]["fitment_status"], "VERIFIED")
        self.assertTrue(result["cards"][0]["compatible"])

        guessed = Script(
            says(use("oil_guide", car(make="Toyota", model="Camry", year=2015,
                                     engine_code="2AR-FE"))),
            done(),
        )
        run(service(guessed, repository=OilRepository()), "Camry 2015 ზეთი")
        content, is_error = tool_results(guessed.requests[1])[0]
        self.assertFalse(is_error)
        self.assertEqual((content["status"], content["detail"]), ("needs_detail", "engine"))
        self.assertNotIn("rows", content)

    def test_oil_and_filter_quote_uses_pack_count_and_current_stock(self):
        rows = [dict(item) for item in ROWS]
        rows[4].update(price_raw="20,25", stock_store="9")
        memory = {"handles": {"r4": "p4", "r5": "p5"}, "shown": ["p4", "p5"]}
        quote = {"oil_id": "r5", "filter_id": "r4", "litres": 5}
        for stocked, quantity, expected in (
                ("9", "5 ლიტრი", True), ("7.9", "5 ლიტრი", False),
                ("9", "4 ლიტრი", False)):
            with self.subTest(stocked=stocked, quantity=quantity):
                rows[4]["stock_store"] = stocked
                script = Script(done(
                    products=[product("r5", confidence="spec", evidence="5W-30"),
                              product("r4", evidence="M276")],
                    reply="ეს ზეთი და ფილტრია.", outcome="offered", oil_quote=quote,
                ))
                turn = run(service(script, rows=rows), f"{quantity} ზეთი და ფილტრი ჯამში",
                           dialogue={"memory": memory})
                self.assertEqual(turn["status"], "OK")
                oil = next(card for card in turn["cards"] if card["pack_litres"])
                self.assertEqual(oil["product"]["price"], 81)
                self.assertEqual("212.00 ₾" in turn["answer"], expected)
                packs = context_of(script.requests[0])["rows"]["oil_packs"]
                self.assertEqual(packs["r5"], {"litres": 4, "price": 81,
                                               "stock": {"store": int(float(stocked) // 4),
                                                         "tuz11": 0}})

    def test_followup_preserves_explicit_vehicle_oil_grade_and_quantity(self):
        rows = [dict(item) for item in ROWS]
        rows[4].update(price_raw="20,25", stock_store="9")
        initial = Script(done(products=[product("r5", confidence="spec", evidence="5W-30")],
                              outcome="offered"))
        first = run(service(initial, rows=rows),
                    "Toyota Camry 2015, engine code 2AR-FE, 5W-30 5 ლიტრი",
                    dialogue={"memory": {"handles": {"r5": "p5", "r4": "p4"}}})
        next_turn = Script(
            says(use("oil_guide", car())),
            done(products=[product("r5", confidence="spec", evidence="5W-30"),
                           product("r4", evidence="M276")],
                 reply="ეს არის ზეთი და ფილტრი.", outcome="offered",
                 oil_quote={"oil_id": "r5", "filter_id": "r4", "litres": 5}),
        )
        search_source = Search()
        result = run(service(next_turn, rows=rows), "და ფილტრით ჯამში?",
                     dialogue={"memory": first["memory"]}, search_service=search_source)
        focus = context_of(next_turn.requests[0])["followup"]
        self.assertEqual((focus["oil_grade"], focus["oil_litres"], focus["previous_oil"]),
                         ("5W-30", 5, "r5"))
        self.assertEqual(search_source.started[0][3]["engine_code"], "2AR-FE")
        self.assertIn("212.00 ₾", result["answer"])

        switch = Script(says(use("oil_guide", car())), done())
        switched = Search()
        changed = run(service(switch, rows=rows), "და Honda Accord 2017-ზე?",
                      dialogue={"memory": result["memory"]}, search_service=switched)
        context = context_of(switch.requests[0])
        self.assertNotIn("rows", context)
        self.assertNotIn("oil_grade", context["followup"])
        self.assertEqual(switched.started[0][3]["model"], "Accord")
        self.assertNotIn("engine_code", changed["memory"]["followup"]["vehicle"])

    def test_engine_clarification_uses_selector_choices_and_remembers_the_tap(self):
        source = Search(fluids=[{
            "provider": "Shell", "status": "CONFLICTED",
            "candidates": ["Camry 2.5 (2AR-FE)", "Camry 3.5 (2GR-FE)"],
        }])
        first = Script(says(use("oil_guide", car())),
                       done(reply="ზეთის შერჩევა ძრავზეა დამოკიდებული.",
                            question="რომელი ძრავია?", options=["პირველი", "მეორე"],
                            outcome="asked"))
        asked = run(service(first), "Toyota Camry 2015 ზეთი", search_service=source)
        self.assertEqual(asked["quick_replies"],
                         ["Camry 2.5 (2AR-FE)", "Camry 3.5 (2GR-FE)", "არ ვიცი"])
        second = Script(says(use("oil_guide", car())), done())
        chosen = Search()
        run(service(second), "Camry 3.5 (2GR-FE)", search_service=chosen,
            dialogue={"memory": asked["memory"], "choice": 2, "pending": asked["pending"]})
        self.assertEqual(chosen.started[0][3]["trim"], "Camry 3.5 (2GR-FE)")

    def test_oil_asks_for_dpf_and_retries_verified_match_after_reply(self):
        vehicle = {"make": "TOYOTA", "model": "Camry", "model_year": 2015, "engine_code": "2AR-FE"}
        reviewed = {"authority": "manufacturer", "reuse_permission": "owned",
                    "review_status": "approved", "reviewed_by": "test reviewer"}

        class OilRepository(Repository):
            def vehicle_specifications(self, configuration):
                return [{**reviewed, "category": "engine_oil", "applicability": vehicle,
                         "restrictions": {"dpf": True}, "approval": "TEST-OIL",
                         "viscosity": "5W-30"}]

            def oil_approval_products(self):
                return [(self.get_product("p5"), [{**reviewed, "product_id": "p5",
                         "approval": "TEST-OIL", "viscosity": "5W-30",
                         "source_fingerprint": "f" * 16}])]

        lookup = says(use("oil_guide", car()))
        first = Script(lookup, done(language="en", reply="One detail decides the oil.",
                                    question="Does it have a particle filter (DPF)?",
                                    options=["With DPF", "No DPF"], outcome="asked"))
        asked = run(service(first, repository=OilRepository()),
                    "Which oil for Toyota Camry 2015, engine code 2AR-FE?")
        content, _ = tool_results(first.requests[1])[0]
        self.assertEqual((content["status"], content["detail"]), ("needs_detail", "dpf"))

        second = Script(lookup, done(language="en", reply="This is the oil for your engine.",
                                    products=[product("r1", confidence="confirmed",
                                                      evidence="5W-30")], outcome="offered"))
        answer = run(service(second, repository=OilRepository()), "With DPF",
                     dialogue={"memory": asked["memory"], "choice": 1})
        self.assertEqual(answer["cards"][0]["fitment_status"], "VERIFIED")
        self.assertIsNone(answer["question"])

    def test_conflicting_oil_grade_cannot_be_offered_as_either_grade(self):
        rows = [*ROWS, row("65812", "20417255", "ზეთი BMW TWIN POWER TURBO 5W30 LL01 1L",
                           "UNIVERSAL", "BMW", "0W20", store="12")]
        offered = done(products=[product("r7", confidence="spec", evidence="5W30")],
                       outcome="offered")
        script = Script(offered, done(reply="შეფუთვა უნდა გადავამოწმოთ.", outcome="chat"))
        result = run(service(script, rows=rows), "BMW 5W30 ზეთი",
                     dialogue={"memory": {"handles": {"r7": "65812"}, "shown": ["65812"]}})
        self.assertEqual(context_of(script.requests[0])["rows"]["oil_grade_conflicts"], ["r7"])
        self.assertEqual(result["cards"], [])
        self.assertIn("შეფუთვა უნდა გადავამოწმოთ", result["answer"])
        self.assertEqual(result["diagnostics"]["removed"][0]["rule"], "oil_grade_conflict")

    def test_shared_oil_approvals_never_make_an_equivalent(self):
        rows = [*ROWS, row("p7", "77777777", "ძრავის ზეთი 5W-30 1L", "", "FUCHS",
                           "5W30", "MB22951/ACEAC3")]
        rows[4] = {**rows[4], "original_code": "5W30", "duplicate_code": "MB22951/ACEAC3"}
        script = Script(says(use("equivalents", {"id": "r5"})), done())
        run(service(script, rows=rows), "ალტერნატივა",
            dialogue={"memory": {"handles": {"r5": "p5"}}})
        content, error = tool_results(script.requests[1])[0]
        self.assertFalse(error)
        self.assertEqual(content["rows"], [])

    def test_supplier_cross_reference_finds_stock_for_another_car_not_a_shop_code(self):
        rows = [*ROWS, row("p7", "0446533450", FRONT, CAMRY, "OTHER", "OTHER123")]
        rows[1] = {**rows[1], "product_model": "TOYOTA AVALON 2015",
                   "duplicate_code": "04465-33450"}
        source = Search(fluids=[{"provider": "brembo", "status": "PROVISIONAL", "items": [
            {"kind": "brake", "component": "front brake pads", "oem_codes": ["04465-33450"]}]}])
        source._external = {}
        source._catalog = object()
        script = Script(says(use("supplier_parts", {"part_group": "brake_pads", **car(
            make="Toyota", model="Camry", year=2015)})), done())
        run(service(script, rows=rows), "Camry 2015 ხუნდები", search_service=source)
        content, error = tool_results(script.requests[1])[0]
        self.assertFalse(error)
        # The same OE cross number reaches the stocked row filed under the Avalon.
        self.assertEqual([item[2] for item in content["rows"]], ["TOYOTA AVALON 2015"])


class RequestTests(unittest.TestCase):
    def test_no_sampling_parameters_and_cache_breakpoints_in_every_request(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                        done(reply="რომელი?"), done())
        run(service(script), "Camry ხუნდები")
        self.assertEqual(len(script.requests), 3)
        for request in script.requests:
            self.assertEqual(set(request), {"model", "max_tokens", "system", "tools", "messages",
                                            "tool_choice", "timeout", "extra_body"})
            self.assertEqual((request["model"], request["max_tokens"]),
                             ("claude-haiku-5-5", 2400))
            self.assertEqual(request["extra_body"], {"output_config": {"effort": "low"}})
            # The fixed part is kept for an hour; the turn's own context the default five
            # minutes, and it comes after everything that is kept longer.
            hour = {"type": "ephemeral", "ttl": "1h"}
            self.assertEqual([block.get("cache_control") for block in request["system"]],
                             [hour] * 2)
            self.assertEqual(request["system"][0]["text"], assistant_prompt.SYSTEM_PROMPT)
            self.assertIn("CATALOGUE GUIDE", request["system"][1]["text"])
            self.assertEqual(request["tools"][-1]["cache_control"], hour)
            opening = [message for message in request["messages"]
                       if isinstance(message["content"], list)
                       and message["content"][0].get("cache_control")]
            self.assertEqual(opening[0]["content"][0]["cache_control"], {"type": "ephemeral"})
            marked = json.dumps(request, ensure_ascii=False, default=str).count("cache_control")
            self.assertEqual(marked, 4)
            self.assertLessEqual(request["timeout"], 30)
            self.assertEqual(context_of(request)["customer_message"], "Camry ხუნდები")
        self.assertEqual([request["tool_choice"]["type"] for request in script.requests],
                         ["any", "auto", "auto"])
        self.assertEqual(script.requests[0]["system"], script.requests[2]["system"])
        self.assertEqual(script.requests[0]["tools"], script.requests[2]["tools"])

    def test_the_sdk_sends_the_turn_as_built_and_gets_its_blocks_back_unchanged(self):
        """The real SDK over a transport that answers from memory: no network."""
        bodies, replies = [], [
            [{"type": "thinking", "thinking": "", "signature": "sig-1"},
             {"type": "tool_use", "id": "toolu_1", "name": "search_catalog",
              "input": search(["TOYOTA"], ["CAMRY"], ["ხუნდი წნ"])}],
            [{"type": "text", "text": "ვასრულებ."},
             {"type": "tool_use", "id": "toolu_2", "name": "finish", "input": finish()}]]

        def answer(request):
            bodies.append(json.loads(request.content))
            return sse(replies[len(bodies) - 1], {
                "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 5})

        chat = service()
        chat.client = anthropic.Anthropic(
            api_key=SECRET, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(answer)))
        self.addCleanup(chat.client.close)
        result = run(chat, "Camry 2015 წინა ხუნდები")
        self.assertEqual(result["status"], "OK", result["diagnostics"])
        first, second = bodies
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertEqual((first["tool_choice"], second["tool_choice"]),
                         ({"type": "any"}, {"type": "auto"}))
        self.assertEqual(set(first), {"model", "max_tokens", "system", "tools", "messages",
                                      "tool_choice", "output_config", "stream"})
        self.assertIs(first["stream"], True)
        self.assertTrue(all(tool["strict"] is True for tool in first["tools"]))
        self.assertEqual(first["tools"][-1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        # The assistant's blocks, the thinking block's signature included, as received.
        self.assertEqual(second["messages"][-2], {"role": "assistant", "content": replies[0]})
        answered = second["messages"][-1]["content"][0]
        self.assertEqual((answered["type"], answered["tool_use_id"]),
                         ("tool_result", "toolu_1"))
        self.assertEqual(json.loads(answered["content"])["total"], 2)
        self.assertEqual(result["usage"], {
            "model_requests": 2, "tool_executions": 1, "input_tokens": 200,
            "output_tokens": 40, "cache_read_input_tokens": 14,
            "cache_creation_input_tokens": 10, "cache_creation_1h_input_tokens": 0})
        self.assertEqual(result["diagnostics"]["requests"][0]["input_tokens"], 100)
        # A streamed reply says when each of its blocks began: what a slow request did.
        timing = result["diagnostics"]["requests"][0]
        self.assertEqual([name for name, _ in timing["blocks"]], ["thinking", "search_catalog"])
        self.assertGreaterEqual(timing["blocks"][0][1], 0)
        self.assertEqual(timing["first_ms"], timing["blocks"][0][1])

    def test_credit_exhaustion_mid_turn_keeps_search_results_and_finishes_via_openrouter(self):
        sdk, requests = anthropic.Anthropic, []
        content = [
            use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი წნ"]))]
        completed = [use("finish", finish(products=[product("r1")], outcome="offered"))]

        def answer(request):
            requests.append(request)
            if len(requests) == 1:
                return sse(content, {"input_tokens": 5, "output_tokens": 5})
            if request.url.host == "api.anthropic.com":
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": "Your credit balance is too low"}})
            return sse(completed, {"input_tokens": 6, "output_tokens": 6})

        def factory(**options):
            options["http_client"] = httpx.Client(transport=httpx.MockTransport(answer))
            return sdk(**options)

        chat = service(assistant_model="claude-sonnet-5-5",
                       openrouter_api_key="fixture-openrouter-key")
        self.addCleanup(chat.close)
        with patch("anthropic.Anthropic", side_effect=factory):
            result = run(chat, "Camry 2015 წინა ხუნდები")
        self.assertEqual(result["status"], "OK", result["diagnostics"])
        self.assertEqual(result["outcome"], "offered")
        self.assertEqual(result["cards"][0]["product"]["source_product_id"], "p1")
        self.assertEqual(result["usage"]["model_requests"], 2)
        self.assertEqual(result["usage"]["tool_executions"], 1)
        self.assertEqual([r.url.host for r in requests], [
            "api.anthropic.com", "api.anthropic.com", "openrouter.ai"])
        refused, retried = [json.loads(r.content) for r in requests[1:]]
        self.assertEqual(retried.pop("model"), "anthropic/claude-sonnet-5.5")
        refused.pop("model")
        retried.pop("provider")
        self.assertEqual(refused, retried)
        self.assertEqual(retried["messages"][-2]["content"], content)
        self.assertEqual(retried["messages"][-1]["content"][0]["type"], "tool_result")

    def test_with_the_account_unfunded_every_turn_finishes_on_openrouter_as_on_anthropic(self):
        """As measured on 2026-11-11: the unfunded account answers every request, a forced
        tool choice too, with the credit refusal. Through OpenRouter, Anthropic refuses the
        forced choice in its own words, so the turn asks again unforced as it does on the
        direct API; OpenRouter's own parameter check, when asked for, answers it with a
        404 instead, and the customer would get an error."""
        sdk, requests = anthropic.Anthropic, []
        replies = iter([
            [use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი წნ"]))],
            [use("finish", finish(products=[product("r1")], outcome="offered"))],
            [use("finish", finish())]])

        def answer(request):
            requests.append(request)
            body = json.loads(request.content)
            if request.url.host == "api.anthropic.com":
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": (
                        "Your credit balance is too low to access the Anthropic API. Please go "
                        "to Plans & Billing to upgrade or purchase credits.")}})
            if body["tool_choice"]["type"] == "any":
                if body["provider"].get("require_parameters"):
                    return httpx.Response(404, json={"error": {
                        "type": "not_found_error", "message": "No endpoints found that support "
                        "the provided 'tool_choice' value."}})
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": 'tool_choice: type "tool" and "any" are not supported for this '
                               'model.'}})
            return sse(next(replies), {"input_tokens": 5, "output_tokens": 5})

        def factory(**options):
            options["http_client"] = httpx.Client(transport=httpx.MockTransport(answer))
            return sdk(**options)

        chat = service(assistant_model="claude-sonnet-5-5",
                       openrouter_api_key="fixture-openrouter-key")
        self.addCleanup(chat.close)
        with patch("anthropic.Anthropic", side_effect=factory):
            first = run(chat, "Camry 2015 წინა ხუნდები")
            second = run(chat, "მადლობა")
        self.assertEqual((first["status"], first["outcome"]), ("OK", "offered"),
                         first["diagnostics"])
        self.assertEqual(first["cards"][0]["product"]["source_product_id"], "p1")
        self.assertEqual(second["status"], "OK", second["diagnostics"])
        bodies = [json.loads(r.content) for r in requests]
        # One refused request to the unfunded account, then OpenRouter only; the next turn
        # neither asks the unfunded account nor forces the tool choice again.
        self.assertEqual([(r.url.host, b["tool_choice"]["type"]) for r, b in zip(
            requests, bodies, strict=True)], [
            ("api.anthropic.com", "any"), ("openrouter.ai", "any"),
            ("openrouter.ai", "auto"), ("openrouter.ai", "auto"), ("openrouter.ai", "auto")])
        for body in bodies[1:]:
            self.assertEqual(body["model"], "anthropic/claude-sonnet-5.5")
            self.assertEqual(body["provider"], {"only": ["anthropic"], "allow_fallbacks": False})
            self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertTrue(chat.assistant_state["claude-sonnet-5-5"]["auto_only"])

    def test_when_no_account_can_pay_the_turn_says_so_instead_of_asking_to_try_again(self):
        sdk = anthropic.Anthropic
        credit = {"type": "error", "error": {"type": "invalid_request_error",
                                             "message": "Your credit balance is too low."}}
        cap = {"type": "error", "error": {
            "type": "rate_limit_error", "message": "You have reached your API usage limits.",
            "details": {"error_code": "enforced_spend_limit_reached"}}}
        cases = {
            # The direct account is unfunded and OpenRouter has no credit left either.
            "backup without credit": ("fixture-openrouter-key", lambda r: httpx.Response(
                400, json=credit) if r.url.host == "api.anthropic.com" else httpx.Response(
                402, json={"error": {"code": 402, "message": "Insufficient credits."}})),
            # No backup, and the usage tier's monthly spend cap is reached.
            "spend cap, no backup": ("", lambda _: httpx.Response(429, json=cap)),
        }
        for name, (backup, answer) in cases.items():
            with self.subTest(name):
                def factory(answer=answer, **options):
                    options["http_client"] = httpx.Client(transport=httpx.MockTransport(answer))
                    return sdk(**options)

                chat = service(assistant_model="claude-sonnet-5-5", openrouter_api_key=backup)
                self.addCleanup(chat.close)
                with patch("anthropic.Anthropic", side_effect=factory), \
                        self.assertLogs("automotive.assistant", level="ERROR") as logs:
                    result = run(chat, "Camry 2015 წინა ხუნდები")
                self.assertEqual(result["status"], "PROVIDER_BILLING", result["diagnostics"])
                self.assertEqual(result["suggestions"], [])
                self.assertIn("assistant_provider_account", "\n".join(logs.output))

    def test_the_fixed_prefix_is_kept_as_long_as_the_setting_says(self):
        hour, short = {"type": "ephemeral", "ttl": "1h"}, {"type": "ephemeral"}
        self.assertEqual((assistant_prompt.cache_control("1h"), assistant_prompt.cache_control(),
                          assistant_prompt.cache_control("5m")), (hour, short, short))
        self.assertEqual(tool_definitions("1h")[-1]["cache_control"], hour)
        self.assertEqual(tool_definitions()[-1]["cache_control"], short)
        self.assertEqual(tool_definitions("1h")[:-1], tool_definitions()[:-1])
        script = Script(done())
        run(service(script, assistant_cache_ttl="5m"), "გამარჯობა")
        request = script.requests[0]
        self.assertEqual([block["cache_control"] for block in request["system"]], [short] * 2)
        self.assertEqual(request["tools"][-1]["cache_control"], short)
        self.assertNotIn('"ttl"', json.dumps(request, ensure_ascii=False, default=str))
        # What was written for an hour is counted apart: it is priced differently.
        usage = SimpleNamespace(
            input_tokens=900, output_tokens=120, cache_read_input_tokens=0,
            cache_creation_input_tokens=31_000,
            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=30_000,
                                           ephemeral_5m_input_tokens=1_000))
        script = Script(says(use("finish", finish()), usage=usage))
        result = run(service(script), "გამარჯობა")
        self.assertEqual((result["usage"]["cache_creation_input_tokens"],
                          result["usage"]["cache_creation_1h_input_tokens"]), (31_000, 30_000))
        self.assertEqual(result["diagnostics"]["requests"][0]["cache_creation_1h_input_tokens"],
                         30_000)
        self.assertEqual(run(service(Script(done())), "x")["usage"][
            "cache_creation_1h_input_tokens"], 0)

    def test_a_provider_that_refuses_the_hour_gets_five_minutes_from_then_on(self):
        refused = anthropic.BadRequestError(
            "tools.5.cache_control.ephemeral.ttl: Extra inputs are not permitted",
            response=httpx.Response(400, request=REQUEST), body=None)
        script = Script(refused, done(), done())
        chat = service(script)
        self.assertEqual(run(chat, "გამარჯობა")["status"], "OK")
        first, second = script.requests[:2]
        self.assertIn('"ttl": "1h"', json.dumps([first["tools"], first["system"]]))
        self.assertNotIn('"ttl"', json.dumps([second["tools"], second["system"]],
                                             ensure_ascii=False))
        self.assertEqual([block["cache_control"] for block in second["system"]],
                         [{"type": "ephemeral"}] * 2)
        self.assertEqual(second["system"][0]["text"], first["system"][0]["text"])
        # Remembered for the process: the next turn does not try the hour again.
        run(chat, "კიდევ")
        self.assertNotIn('"ttl"', json.dumps([script.requests[2]["tools"],
                                              script.requests[2]["system"]],
                                             ensure_ascii=False))
        self.assertIs(chat.assistant_state[chat.settings.anthropic_model]["no_ttl"], True)
        # With five minutes configured there is nothing to fall back to: the error stands.
        script = Script(refused)
        self.assertEqual(run(service(script, assistant_cache_ttl="5m"), "x")["status"],
                         "PROVIDER_UNAVAILABLE")

    def test_the_model_and_effort_come_from_settings(self):
        script = Script(done())
        run(service(script, assistant_model="claude-sonnet-5-5", assistant_effort=""), "hi")
        self.assertEqual(script.requests[0]["model"], "claude-sonnet-5-5")
        self.assertNotIn("extra_body", script.requests[0])

    def test_a_model_that_refuses_forced_tool_choice_is_remembered(self):
        refused = anthropic.BadRequestError(
            'tool_choice: type "tool" and "any" are not supported for this model.',
            response=httpx.Response(400, request=REQUEST), body=None)
        script = Script(refused, done(), done())
        chat = service(script)
        result = run(chat, "გამარჯობა")
        self.assertEqual(result["status"], "OK")
        self.assertEqual([request["tool_choice"]["type"] for request in script.requests],
                         ["any", "auto"])
        self.assertEqual(result["usage"]["model_requests"], 1)
        self.assertTrue(chat.assistant_state["claude-haiku-5-5"]["auto_only"])
        run(chat, "კიდევ")
        self.assertEqual(script.requests[2]["tool_choice"], {"type": "auto"})

    def test_effort_is_dropped_for_a_model_that_refuses_it(self):
        refused = anthropic.BadRequestError(
            "output_config: Extra inputs are not permitted",
            response=httpx.Response(400, request=REQUEST), body=None)
        script = Script(refused, done(), done())
        chat = service(script)
        self.assertEqual(run(chat, "გამარჯობა")["status"], "OK")
        self.assertIn("extra_body", script.requests[0])
        self.assertNotIn("extra_body", script.requests[1])
        self.assertEqual(script.requests[1]["tool_choice"], {"type": "any"})
        run(chat, "კიდევ")
        self.assertNotIn("extra_body", script.requests[2])
        strict = anthropic.BadRequestError(
            "tools.0.custom.strict: Extra inputs are not permitted",
            response=httpx.Response(400, request=REQUEST), body=None)
        script = Script(strict, done())
        self.assertEqual(run(service(script), "გამარჯობა")["status"], "OK")
        self.assertTrue(all("strict" in tool for tool in script.requests[0]["tools"]))
        self.assertFalse(any("strict" in tool for tool in script.requests[1]["tools"]))
        self.assertEqual(script.requests[1]["tools"][-1]["cache_control"],
                         {"type": "ephemeral", "ttl": "1h"})
        other = anthropic.BadRequestError("messages: something else is wrong",
                                          response=httpx.Response(400, request=REQUEST),
                                          body=None)
        self.assertEqual(run(service(Script(other)), "x")["status"], "PROVIDER_UNAVAILABLE")

    def test_the_vin_and_secrets_never_reach_the_model(self):
        script = Script(
            says(use("supplier_parts", {"part_group": "air_filter", **car(use_vin=True)}),
                 use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ფილტრი"]))),
            done(reply="ფილტრი ქვემოთაა.", car_label="Toyota Camry 2015"))
        vin = Vin()
        backup_key = "fixture-backup-private-key"
        chat = service(script, vin=vin, operator_token="operator-token-" + "x" * 24,
                       openrouter_api_key=backup_key)
        federation = Search(vehicle={"make": "TOYOTA", "model": "Camry", "model_year": 2015})
        result = run(chat, f"ჰაერის ფილტრი {VIN} {SECRET} {backup_key} operator-token-{'x' * 24}",
                     search_service=federation, vin=VIN)
        self.assertEqual(vin.calls, [VIN])
        self.assertEqual(federation.started[0][0], VIN)
        sent = json.dumps(script.requests, ensure_ascii=False, default=str)
        for private in (VIN, VIN.lower(), SECRET, backup_key, "operator-token-", "h" * 32):
            self.assertNotIn(private, sent)
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False, default=str))
        context = context_of(script.requests[0])
        self.assertIn("[VIN]", context["customer_message"])
        self.assertEqual(context["vin"]["facts"]["model"], "Camry")
        self.assertTrue(context["vin"]["decoded"])
        self.assertEqual(result["vehicle"]["make"], "TOYOTA")
        # The decoded facts are kept for the next turn; the VIN is not.
        self.assertNotIn(VIN, json.dumps(result["memory"]))
        self.assertEqual(result["memory"]["vin_facts"]["make"], "TOYOTA")

    def test_decoded_facts_are_reused_and_a_failed_decode_is_said(self):
        vin = Vin()
        first = run(service(Script(done()), vin=vin), "ფილტრი " + VIN)
        script = Script(done())
        run(service(script, vin=vin), "ზეთიც", vin=VIN, dialogue={"memory": first["memory"]})
        self.assertEqual(vin.calls, [VIN])
        self.assertEqual(context_of(script.requests[0])["vin"]["facts"]["model_year"], 2015)
        script = Script(done())
        result = run(service(script, vin=Vin(error=RuntimeError("down"))), "ფილტრი " + VIN)
        sent = context_of(script.requests[0])["vin"]
        self.assertFalse(sent["decoded"])
        self.assertIn("could not be decoded", sent["note"])
        self.assertIn("VIN_UNAVAILABLE", result["warnings"])

    def test_a_visitor_never_gets_the_shops_own_numbers(self):
        calls = says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])),
                     use("lookup_codes", {"numbers": ["30023433"], "vehicle_hint": ""}),
                     use("equivalents", {"id": "r1"}))
        memory = {**KNOWN, "shown": ["p1"]}
        public, staff = Script(calls, done()), Script(copy.deepcopy(calls), done())
        run(service(public), "PN1521", dialogue={"memory": memory, "audience": "public"})
        run(service(staff), "PN1521", dialogue={"memory": memory})
        for request in public.requests:
            sent = json.dumps(request["messages"], ensure_ascii=False, default=str)
            self.assertNotIn("shop_code", sent)
            for item in ROWS:
                if item["product_code"] != "30023433":  # The visitor typed that one.
                    self.assertNotIn(item["product_code"], sent)
        self.assertEqual(context_of(public.requests[0])["audience"], "public")
        self.assertEqual(len(context_of(public.requests[0])["rows"]["columns"]), 9)
        for content, _ in tool_results(public.requests[1]):
            self.assertNotIn("shop_code", content["columns"])
        searched = tool_results(staff.requests[1])[0][0]
        self.assertEqual(searched["columns"][9], "shop_code")
        self.assertEqual({item[9] for item in searched["rows"]},
                         {"30023433", "2040026342", "30015804"})
        self.assertEqual(context_of(staff.requests[0])["rows"]["rows"][0][-1], "30023433")


class ButtonTests(unittest.TestCase):
    def test_a_question_and_its_buttons_are_plain_texts_and_come_back_next_turn(self):
        asking = Script(done(reply="Camry 2015-ზე ორივე გვაქვს.", question="წინა თუ უკანა?",
                             options=["წინა", "უკანა", "ორივე"], suggestions=[],
                             outcome="asked", notes="Camry 2015; front r1 r2, rear r3"))
        first = run(service(asking), "Camry 2015 ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual((first["status"], first["act"]), ("CLARIFICATION", "clarify"))
        self.assertEqual(first["question"], {"text": "წინა თუ უკანა?", "options": [
            {"label": label, "value": label} for label in ("წინა", "უკანა", "ორივე")]})
        self.assertEqual(first["quick_replies"], ["წინა", "უკანა", "ორივე"])
        self.assertEqual(first["pending"]["choices"], ["წინა", "უკანა", "ორივე"])
        # Next turn the model reads its question, the buttons, the tap and the rows its
        # notes name.
        script = Script(done(suggestions=["უკანა ხუნდები", "დისკები"]))
        second = run(service(script), "წინა", dialogue={
            "memory": first["memory"], "choice": 1, "pending": first["pending"]})
        sent = context_of(script.requests[0])
        self.assertEqual(sent["your_question"], {"text": "წინა თუ უკანა?",
                                                 "options": ["წინა", "უკანა", "ორივე"]})
        self.assertEqual(sent["tapped_option"], {"index": 1, "label": "წინა"})
        self.assertEqual([item[0] for item in sent["rows"]["rows"]], ["r1", "r2", "r3"])
        self.assertEqual(second["suggestions"], [
            {"label": "უკანა ხუნდები", "value": "უკანა ხუნდები"},
            {"label": "დისკები", "value": "დისკები"}])
        third = Script(done())
        run(service(third), "დისკები", dialogue={"memory": second["memory"]})
        self.assertEqual(context_of(third.requests[0])["your_suggestions"],
                         ["უკანა ხუნდები", "დისკები"])
        schema = tool_definitions()[-1]["input_schema"]["properties"]
        self.assertEqual(list(schema), ["language", "car_label", "products", "oil_quote",
                                        "question", "options", "suggestions", "reply", "notes",
                                        "outcome", "staff"])
        self.assertEqual((schema["question"]["type"], schema["options"]["items"],
                          schema["suggestions"]["items"]),
                         ("string", {"type": "string"}, {"type": "string"}))
        search_schema = tool_definitions()[0]["input_schema"]["properties"]
        self.assertEqual(list(search_schema), ["make_terms", "vehicle_terms", "part_terms",
                                               "exclude_terms", "in_stock_only"])


class KeptBackTests(unittest.TestCase):
    """Rows that fit only one answer wait for it: not shown, asked about, handed back."""

    def test_rows_that_depend_on_an_answer_are_kept_back_and_come_back_next_turn(self):
        asking = Script(done(
            reply="Camry 2015-ზე ხუნდები გვაქვს.", question="წინა თუ უკანა?",
            options=["წინა", "უკანა"], suggestions=[], outcome="asked",
            notes="front: r1 r2; rear: r3",
            products=[product("r1", "r2", confidence="depends", reason="front"),
                      product("r3", confidence="depends", reason="rear", evidence="ხუნდი უკ"),
                      product("r6", evidence="ჰაერის ფილტრი", group="ფილტრი")]))
        memory = {"handles": {**KNOWN["handles"], "r6": "p6"}}
        first = run(service(asking), "Camry 2015 ხუნდები და ფილტრი",
                    dialogue={"memory": memory})
        self.assertEqual(len(asking.requests), 1)
        # Only the row that fits whatever the answer is becomes a card.
        self.assertEqual([card["product"]["source_product_id"] for card in first["cards"]],
                         ["p6"])
        self.assertEqual((first["status"], first["question"]["text"]), ("OK", "წინა თუ უკანა?"))
        self.assertEqual(first["memory"]["held"], ["p1", "p2", "p3"])
        script = Script(done(products=[product("r3", evidence="ხუნდი უკ")], outcome="offered",
                             notes=""))
        second = run(service(script), "უკანა", dialogue={
            "memory": first["memory"], "choice": 2, "pending": first["pending"]})
        self.assertEqual([item[0] for item in context_of(script.requests[0])["rows"]["rows"]],
                         ["r6", "r1", "r2", "r3"])
        self.assertEqual([card["product"]["source_product_id"] for card in second["cards"]],
                         ["p3"])
        self.assertEqual(second["memory"]["held"], [])

    def test_after_a_question_the_rows_the_searches_returned_come_back(self):
        """The answer is judged on every candidate, not only on the rows the notes name."""
        asking = Script(
            says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])),
                 use("lookup_codes", {"numbers": ["HU7025Z"], "vehicle_hint": ""})),
            done(reply="Camry 2015-ზე ხუნდები გვაქვს.", question="წინა თუ უკანა?",
                 options=["წინა", "უკანა"], suggestions=[], outcome="asked",
                 notes="Camry 2015; pads; asked the axle"))
        first = run(service(asking), "Camry 2015 ხუნდები")
        self.assertEqual(first["cards"], [])
        self.assertEqual(first["memory"]["seen"], ["p1", "p2", "p3", "p4"])
        answering = Script(done(reply="უკანა ხუნდს ვეძებ."))
        second = run(service(answering), "უკანა", dialogue={
            "memory": first["memory"], "choice": 2, "pending": first["pending"]})
        sent = context_of(answering.requests[0])
        self.assertEqual([item[4] for item in sent["rows"]["rows"]],
                         ["PN1521", "37677", "04466-33180", "HU7025z"])
        # Once nothing is asked, they are let go.
        self.assertEqual(second["memory"]["seen"], [])
        # A second question without a new search keeps them for the next answer.
        again = Script(done(reply="გასაგებია.", question="რა წელია?", options=["2015", "2019"],
                            suggestions=[], outcome="asked"))
        third = run(service(again), "არ ვიცი", dialogue={
            "memory": first["memory"], "pending": first["pending"]})
        self.assertEqual(third["memory"]["seen"], ["p1", "p2", "p3", "p4"])
        for told in ("when you asked a question last turn", "the rows your searches returned"):
            self.assertIn(told, assistant_prompt.SYSTEM_PROMPT)

    def test_the_car_label_is_the_customers_car_and_never_a_rows(self):
        told = " ".join(assistant_prompt.SYSTEM_PROMPT.split())
        for passage in ("the car the customer stated, the car the VIN decoded to, or the car "
                        "the staff form holds, and no other",
                        "A car that appears only in a row's vehicle text or in a look-up "
                        "result is the car a part was made for, never the customer's car",
                        "the label stays empty, or stays what it was",
                        "The customer named no car: the car in the row's vehicle text is not "
                        "theirs, and car_label stays empty."):
            self.assertIn(passage, told)
        finish = tool_definitions()[-1]["input_schema"]["properties"]
        self.assertIn("never a car taken from a row's vehicle text or from a look-up result",
                      finish["car_label"]["description"])

    def test_rows_kept_back_without_a_question_are_sent_back_to_the_model(self):
        script = Script(done(products=[product("r1", confidence="depends",
                                               reason="engine 2.5")], outcome="offered"),
                        done(products=[product("r1")], outcome="offered"))
        result = run(service(script), "Camry 2015 ხუნდები", dialogue={"memory": KNOWN})
        error = tool_results(script.requests[1])[0][0]["errors"][0]
        self.assertEqual(error["error"],
                         "Rows are marked depends (engine 2.5), but the reply asks nothing.")
        self.assertIn("Ask for that fact in `question`", error["fix"])
        self.assertEqual(len(result["cards"]), 1)
        # An answer that never passes shows nothing that was kept back.
        bad = {"products": [product("r1", confidence="depends", reason="engine 2.5"),
                            product("r2")],
               "outcome": "offered", "reply": "ხუნდები ქვემოთაა."}
        script = Script(done(**bad), done(**bad), done(**bad))
        result = run(service(script), "Camry 2015 ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual([card["product"]["source_product_id"] for card in result["cards"]],
                         ["p2"])
        self.assertIn("ANSWER_CHECKS_FAILED", result["warnings"])


class LanguageTests(unittest.TestCase):
    def test_a_message_in_georgian_or_russian_letters_settles_the_reply_language(self):
        from automotive.assistant import written_in

        self.assertEqual(written_in("Kia Sportage 2014 წინა ხუნდები"), "ka")
        self.assertEqual(written_in("нужны задние колодки на Prado 150"), "ru")
        self.assertEqual(written_in("Toyota Camry 2015, front brake pads"), "")
        self.assertEqual(written_in("mersedesis w204 haeris filtri minda"), "")
        self.assertEqual(written_in("W212"), "")
        english = {"reply": "Front pads for the Sportage are below.", "language": "en",
                   "suggestions": ["Rear pads"]}
        script = Script(done(**english), done(reply="Sportage-ის წინა ხუნდები ქვემოთაა."))
        result = run(service(script), "Kia Sportage 2014 წინა ხუნდები")
        self.assertEqual(context_of(script.requests[0])["reply_language"], "ka")
        error = tool_results(script.requests[1])[0][0]["errors"][0]
        self.assertEqual(error["error"],
                         "The customer wrote in Georgian, but the reply is not in Georgian.")
        self.assertEqual(result["answer"], "Sportage-ის წინა ხუნდები ქვემოთაა.")
        # The label alone does not pass either: the text has to be in the language.
        mislabelled = Script(done(**{**english, "language": "ka"}), done())
        run(service(mislabelled), "Kia Sportage 2014 წინა ხუნდები")
        self.assertEqual(len(mislabelled.requests), 2)
        russian = Script(done(reply="Задние колодки ниже.", language="ru",
                              suggestions=["Передние колодки"]))
        result = run(service(russian), "нужны задние колодки на прадо 150")
        self.assertEqual((len(russian.requests), result["memory"]["language"]), (1, "ru"))
        # A car name in Latin letters inside a Georgian reply is still Georgian.
        named = Script(done(reply="Mercedes-Benz E-Class Coupe 2015: ხუნდები ქვემოთაა."))
        self.assertEqual(run(service(named), "E400 კუპე 2015 ხუნდები")["status"], "OK")
        self.assertEqual(len(named.requests), 1)
        # Latin letters settle nothing: the model decides, and nothing is checked.
        latin = Script(done(**english))
        run(service(latin), "Toyota Camry 2015, front brake pads")
        self.assertEqual(len(latin.requests), 1)
        self.assertNotIn("reply_language", context_of(latin.requests[0]))


class WarmTests(unittest.TestCase):
    """Opening a conversation has the provider read the fixed prompt before a message."""

    def chat(self, **settings):
        from automotive import assistant

        calls = []

        def create(**request):
            calls.append(request)
            if isinstance(self.reply, Exception):
                raise self.reply
            return says(stop="max_tokens", usage=self.usage)

        self.reply = self.usage = None
        chat = service(**settings)
        chat._assistant_client = SimpleNamespace(messages=SimpleNamespace(create=create))
        return assistant, chat, calls

    def test_the_fixed_prompt_is_read_once_per_cache_lifetime(self):
        assistant, chat, calls = self.chat(assistant_thinking="off")
        chat.prepare()
        chat.prepare()
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual((request["max_tokens"], request["tool_choice"], request["thinking"]),
                         (0, {"type": "auto"}, {"type": "disabled"}))
        self.assertEqual(request["extra_body"], {"output_config": {"effort": "low"}})
        self.assertEqual(request["tools"], tool_definitions("1h"))
        self.assertEqual(request["system"][0]["text"], assistant_prompt.SYSTEM_PROMPT)
        self.assertIn("CATALOGUE GUIDE", request["system"][1]["text"])
        self.assertEqual(request["messages"], [{"role": "user", "content": "warm"}])
        self.assertNotIn("stream", request)
        state = chat.assistant_state[chat.settings.assistant_model or chat.settings.anthropic_model]
        # The cache is kept an hour: four quiet minutes are not its end...
        state["used_at"] -= assistant.WARM_SECONDS + 1
        chat.prepare()
        self.assertEqual(len(calls), 1)
        # ...but near the end of the hour it is read again.
        state["used_at"] -= assistant.WARM_SECONDS_1H
        chat.prepare()
        self.assertEqual(len(calls), 2)

    def test_what_reading_the_fixed_prompt_used_is_kept_for_whoever_counts_the_cost(self):
        assistant, chat, calls = self.chat()
        model = chat.settings.assistant_model or chat.settings.anthropic_model
        self.assertIsNone(assistant.warm_usage(chat, model))
        self.usage = SimpleNamespace(
            input_tokens=3, output_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=35_000,
            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=35_000))
        chat.prepare()
        self.assertEqual(assistant.warm_usage(chat, model), {
            "input_tokens": 3, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 35_000, "cache_creation_1h_input_tokens": 35_000})
        self.assertIsNone(assistant.warm_usage(chat, "another-model"))
        # A request that failed left no count.
        chat.assistant_state[model]["used_at"] -= assistant.WARM_SECONDS_1H + 1
        self.reply = anthropic.APIConnectionError(request=REQUEST)
        chat.prepare()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(assistant.warm_usage(chat, model))

    def test_a_refusal_teaches_the_warm_up_what_the_turns_learn_and_the_rest_is_logged(self):
        assistant, chat, calls = self.chat()
        replies = [anthropic.BadRequestError(  # OpenRouter's backend, measured 2026-11-20
            "The compiled grammar is too large, which would cause performance issues. "
            "Simplify your tool schemas or reduce the number of strict tools.",
            response=httpx.Response(400, request=REQUEST), body=None)]

        def create(**request):
            calls.append(request)
            if replies:
                raise replies.pop(0)
            return says(stop="max_tokens", usage=None)

        chat._assistant_client = SimpleNamespace(messages=SimpleNamespace(create=create))
        chat.prepare()
        self.assertEqual(len(calls), 2)  # asked again without strict tools, at once
        self.assertTrue(any("strict" in tool for tool in calls[0]["tools"]))
        self.assertFalse(any("strict" in tool for tool in calls[1]["tools"]))
        model = chat.settings.assistant_model or chat.settings.anthropic_model
        self.assertTrue(chat.assistant_state[model]["no_strict"])  # the turns send the same
        # A refusal that teaches nothing ends the warm-up, with the provider's own words.
        chat.assistant_state[model]["used_at"] -= assistant.WARM_SECONDS_1H + 1
        replies.append(anthropic.BadRequestError(
            "prompt is too long", response=httpx.Response(400, request=REQUEST), body=None))
        with self.assertLogs("automotive.assistant", "INFO") as logs:
            chat.prepare()
        self.assertEqual(len(calls), 3)
        self.assertIn("assistant_warm_skipped BadRequestError status=400 said=prompt is too long",
                      "\n".join(logs.output))

    def test_a_turn_counts_as_reading_it_and_a_failure_is_only_logged(self):
        _, chat, calls = self.chat()
        chat.client = SimpleNamespace(messages=SimpleNamespace(create=Script(done()).create))
        run(chat, "გამარჯობა")
        chat.client = None
        chat.prepare()
        self.assertEqual(calls, [])  # The turn just read it.
        _, chat, calls = self.chat()
        self.reply = anthropic.APIConnectionError(request=REQUEST)
        chat.prepare()  # Does not raise.
        self.assertEqual(len(calls), 1)
        # An injected client is never warmed: it is a test double or someone else's.
        injected = service(Script())
        injected.prepare()
        _, unconfigured, calls = self.chat(anthropic_api_key="")
        unconfigured.prepare()
        self.assertEqual(calls, [])


class Stream:
    """A model client that streams: each step is a reply, or an error raised on reading."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **request):
        self.requests.append(copy.deepcopy(request))
        step = self.steps.pop(0)
        events = [SimpleNamespace(type="message_start")]
        if not isinstance(step, Exception):
            events += [SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
                type=block["type"], name=block.get("name"))) for block in step.content]
            events.append(SimpleNamespace(type="message_stop"))

        class Reading:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                yield from events
                if isinstance(step, Exception):
                    raise step

            def get_final_message(self):
                return step

        return Reading()


class SpeedTests(unittest.TestCase):
    """What keeps a turn short: rows that share a judgement, no thinking where the answers
    hold without it, and a reply that is read while it is written."""

    def test_one_entry_carries_every_row_that_shares_its_judgement(self):
        answer = done(reply="წინა ხუნდები ქვემოთაა.", outcome="offered", products=[
            product("r2", "r1", reason=""),
            product("r3", evidence="ხუნდი უკ", group="უკანა ხუნდები", confidence="check",
                    reason="გადაამოწმეთ წელი.")])
        script = Script(answer)
        result = run(service(script), "Camry 2015 ხუნდები", dialogue={"memory": KNOWN})
        self.assertEqual(len(script.requests), 1)
        self.assertEqual([(card["product"]["source_product_id"], card["confidence"],
                           card["evidence"], card["reason"], card["group"])
                          for card in result["cards"]], [
            ("p1", "listed", "CAMRY 2006->", "", "g1"),
            ("p2", "listed", "CAMRY 2006->", "", "g1"),
            ("p3", "check", "ხუნდი უკ", "გადაამოწმეთ წელი.", "g2")])
        self.assertEqual(result["groups"], [
            {"id": "g1", "label": "წინა ხუნდები", "count": 2},
            {"id": "g2", "label": "უკანა ხუნდები", "count": 1}])
        product_schema = tool_definitions()[-1]["input_schema"]["properties"]["products"]["items"]
        self.assertEqual(list(product_schema["properties"]),
                         ["ids", "confidence", "evidence", "reason", "group"])
        self.assertEqual(product_schema["properties"]["confidence"]["enum"],
                         ["confirmed", "listed", "spec", "check", "depends"])
        self.assertEqual(product_schema["properties"]["ids"]["items"], {"type": "string"})

    def test_a_quote_may_differ_from_the_row_in_spacing_case_and_dashes(self):
        rows = [*ROWS, row("p7", "30040000", "წევის დაბოლოება მც", "TOYOTA/LEXUS RAV-4 2005–>",
                           "JIKIU", "ET21221L")]
        memory = {"handles": {"r1": "p1", "r7": "p7"}}
        for handle, evidence in (("r1", "camry 2006 ->"), ("r1", "CAMRY  2006->"),
                                 ("r7", "RAV-4 2005->"), ("r7", "rav-4 2005 –>")):
            with self.subTest(evidence=evidence):
                script = Script(done(products=[product(handle, evidence=evidence)]))
                result = run(service(script, rows=rows), "ხუნდები", dialogue={"memory": memory})
                self.assertEqual((len(result["cards"]), len(script.requests)), (1, 1))

    def test_thinking_off_forces_a_tool_call_on_every_request(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                        done())
        run(service(script, assistant_thinking="off"), "Camry ხუნდები")
        for request in script.requests:
            self.assertEqual(request["thinking"], {"type": "disabled"})
            self.assertEqual(request["tool_choice"], {"type": "any"})
        default = Script(done())
        run(service(default), "გამარჯობა")
        self.assertNotIn("thinking", default.requests[0])

    def test_a_model_that_cannot_stop_thinking_gets_its_lowest_setting(self):
        def refused(message):
            return anthropic.BadRequestError(
                message, response=httpx.Response(400, request=REQUEST), body=None)

        script = Script(
            refused('tool_choice: type "tool" and "any" are not supported for this model.'),
            refused('"thinking.type.disabled" is not supported for this model. Use '
                    '"thinking.type.between_tools" for the lowest thinking setting, or '
                    '"thinking.type.adaptive" and "output_config.effort".'),
            done(), done())
        chat = service(script, assistant_thinking="off", assistant_model="claude-sonnet-5-5")
        result = run(chat, "გამარჯობა")
        self.assertEqual((result["status"], result["usage"]["model_requests"]), ("OK", 1))
        self.assertEqual([(request["tool_choice"]["type"], request["thinking"]["type"])
                          for request in script.requests],
                         [("any", "disabled"), ("auto", "disabled"), ("auto", "between_tools")])
        # The effort setting was not mistaken for the cause, and both facts are remembered.
        self.assertEqual(script.requests[2]["extra_body"], {"output_config": {"effort": "low"}})
        self.assertEqual(result["diagnostics"]["requests"][0]["thinking"], "between_tools")
        run(chat, "კიდევ")
        self.assertEqual((script.requests[3]["tool_choice"], script.requests[3]["thinking"]),
                         ({"type": "auto"}, {"type": "between_tools"}))
        # A model that refuses that too is left to its own default.
        script = Script(refused("thinking: disabled is not supported"),
                        refused("thinking: between_tools is not supported"), done())
        self.assertEqual(run(service(script, assistant_thinking="off"), "x")["status"], "OK")
        self.assertNotIn("thinking", script.requests[2])
        self.assertEqual(script.requests[2]["tool_choice"], {"type": "any"})
        # Through OpenRouter (measured 2026-11-11) the same refusal says "reasoning".
        script = Script(refused("Reasoning is mandatory for this endpoint and cannot be "
                                "disabled."), done())
        chat = service(script, assistant_thinking="off", assistant_model="claude-sonnet-5-5")
        self.assertEqual(run(chat, "x")["status"], "OK")
        self.assertEqual([request["thinking"]["type"] for request in script.requests],
                         ["disabled", "between_tools"])

    def test_a_streamed_reply_is_read_as_it_is_written(self):
        client = Stream(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                        done(reply="ხუნდები ქვემოთაა."))
        chat = service()
        chat.client = client
        result = run(chat, "Camry ხუნდები")
        self.assertEqual((result["status"], result["answer"]), ("OK", "ხუნდები ქვემოთაა."))
        first, second = result["diagnostics"]["requests"]
        self.assertEqual(([name for name, _ in first["blocks"]],
                          [name for name, _ in second["blocks"]]),
                         (["search_catalog"], ["finish"]))
        self.assertIn("first_ms", first)
        # The wait is bounded by the turn, and silence between pieces by a few seconds.
        limits = client.requests[0]["timeout"]
        self.assertLessEqual(limits.read, 10)
        self.assertLessEqual(limits.connect, 5)
        self.assertLessEqual(limits.write, 30)

    def test_a_stalled_stream_is_asked_again_once(self):
        stalled = anthropic.APITimeoutError(REQUEST)
        client = Stream(stalled, done(reply="მოგესალმებით!"))
        chat = service()
        chat.client = client
        with patch("automotive.assistant.time.sleep"):
            self.assertEqual(run(chat, "გამარჯობა")["status"], "OK")
            self.assertEqual(len(client.requests), 2)
            chat.client = Stream(anthropic.APITimeoutError(REQUEST),
                                 anthropic.APITimeoutError(REQUEST), done())
            self.assertEqual(run(chat, "გამარჯობა")["status"], "TIMEOUT")

    def test_a_reply_that_breaks_off_with_an_error_event_is_asked_again_once(self):
        # The provider says "overloaded" inside a stream whose response began with 200.
        broke = anthropic.APIStatusError(
            "Overloaded", response=httpx.Response(200, request=REQUEST),
            body={"type": "error", "error": {"type": "overloaded_error"}})
        client = Stream(broke, done(reply="მოგესალმებით!"))
        chat = service()
        chat.client = client
        with patch("automotive.assistant.time.sleep"):
            result = run(chat, "გამარჯობა")
        self.assertEqual((result["status"], len(client.requests)), ("OK", 2))
        # A refused request is not repeated: it would be refused again.
        refused = anthropic.PermissionDeniedError(
            "no", response=httpx.Response(403, request=REQUEST), body=None)
        chat.client = Stream(refused, done())
        with self.assertLogs("automotive.assistant", level="ERROR"):
            self.assertEqual(run(chat, "გამარჯობა")["status"], "ACCESS_DENIED")
        self.assertEqual(len(chat.client.requests), 1)

    def test_the_turns_deadline_ends_a_reply_that_is_still_being_written(self):
        client = Stream(done())
        chat = service()
        chat.client = client
        with patch("automotive.assistant.AssistantTurn._remaining", side_effect=[30.0] * 4 + [
                0.0] * 20):
            result = run(chat, "გამარჯობა")
        self.assertEqual((result["status"], len(client.requests)), ("TIMEOUT", 1))
        self.assertTrue(result["answer"])

    def test_the_context_says_what_outside_sources_can_answer_and_no_language_at_first(self):
        script = Script(done(), done())
        federation = Search()
        federation._external = {"shell": (object(), True), "jsfilter": (object(), True)}
        federation._catalog = None
        federation.motorium_enabled = False
        first = run(service(script), "Camry 2015 ზეთი", search_service=federation)
        sent = context_of(script.requests[0])
        self.assertEqual(sent["outside_sources"], {
            "supplier_parts": "cannot answer in this turn", "oil_guide": "can answer",
            "car_numbers": "cannot answer in this turn", "research": "cannot answer in this turn",
            "shop_info": "cannot answer in this turn"})
        self.assertNotIn("language_so_far", sent)
        run(service(script), "ჰაერის ფილტრი " + VIN, search_service=federation,
            dialogue={"memory": first["memory"]})
        sent = context_of(script.requests[1])
        self.assertEqual(sent["outside_sources"]["supplier_parts"], list(FILTER_GROUPS))
        self.assertEqual(sent["language_so_far"], "ka")
        # A federation that does not say what it has is asked whatever the model wants.
        script = Script(done())
        run(service(script), "ზეთი")
        self.assertEqual(context_of(script.requests[0])["outside_sources"], {
            "supplier_parts": "any part group", "oil_guide": "can answer",
            "car_numbers": "cannot answer in this turn", "research": "cannot answer in this turn",
            "shop_info": "cannot answer in this turn"})


class LimitTests(unittest.TestCase):
    def test_the_request_limit_ends_a_turn_that_never_finishes(self):
        again = says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])))
        script = Script(again, copy.deepcopy(again), copy.deepcopy(again))
        result = run(service(script, assistant_max_requests=2), "Camry ხუნდები")
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(script.requests[1]["messages"][-1]["content"][-1],
                         {"type": "text", "text": assistant_prompt.LAST_ROUND})
        self.assertEqual((result["status"], result["cards"], result["results"]),
                         ("LIMIT_EXHAUSTED", [], []))
        self.assertTrue(result["answer"])
        self.assertEqual(result["suggestions"], [{"label": "ხელახლა ცდა",
                                                  "value": "Camry ხუნდები"}])
        self.assertEqual(result["diagnostics"]["fallback"], "LIMIT_EXHAUSTED")

    def test_calls_past_the_tool_budget_are_answered_but_not_run(self):
        script = Script(says(*(use("search_catalog", search(["TOYOTA"], ["CAMRY"], [term]))
                               for term in ("ხუნდი წნ", "ხუნდი უკ", "ფილტრი"))), done())
        result = run(service(script, assistant_max_tool_calls=2), "Camry")
        first, second, third = tool_results(script.requests[1])
        self.assertEqual((first[1], second[1], third[1]), (False, False, True))
        self.assertIn("Search budget used (2 of 2)", third[0]["error"])
        self.assertEqual(third[0]["fix"], "Call finish now with what you have.")
        self.assertIn("Search budget used (2 of 2)",
                      script.requests[1]["messages"][-1]["content"][-1]["text"])
        self.assertEqual(result["usage"]["tool_executions"], 2)

    def test_a_turn_out_of_time_stops_asking_the_model(self):
        script = Script(done())
        result = run(service(script), "გამარჯობა", deadline=time.monotonic() + 0.5)
        self.assertEqual((script.requests, result["status"]), ([], "TIMEOUT"))
        self.assertTrue(result["answer"])
        cancelled = run(service(Script(done())), "გამარჯობა", cancelled=lambda: True)
        self.assertEqual(cancelled["status"], "CANCELLED")

    def test_a_dropped_connection_is_tried_once_more_and_a_timeout_is_not(self):
        dropped = anthropic.APIConnectionError(request=REQUEST)
        script = Script(dropped, done())
        with patch("automotive.assistant.time.sleep"):
            self.assertEqual(run(service(script), "გამარჯობა")["status"], "OK")
            twice = Script(anthropic.APIConnectionError(request=REQUEST),
                           anthropic.APIConnectionError(request=REQUEST), done())
            self.assertEqual(run(service(twice), "გამარჯობა")["status"], "PROVIDER_UNAVAILABLE")
        slow = Script(anthropic.APITimeoutError(REQUEST), done())
        self.assertEqual(run(service(slow), "გამარჯობა")["status"], "TIMEOUT")
        self.assertEqual(len(slow.requests), 1)


class FailureTests(unittest.TestCase):
    @staticmethod
    def rate_limit(*, headers=None, body=None):
        response = httpx.Response(429, headers=headers or {}, request=REQUEST)
        return anthropic.RateLimitError("rate limited", response=response, body=body)

    def test_rate_limit_retry_after_parses_seconds_dates_and_default(self):
        from email.utils import formatdate

        from automotive.assistant import AssistantTurn

        self.assertEqual(AssistantTurn._rate_limit_wait(self.rate_limit()), 1.0)
        seconds = self.rate_limit(headers={"Retry-After": "2.5"})
        self.assertEqual(AssistantTurn._rate_limit_wait(seconds), 2.5)
        date = self.rate_limit(headers={"Retry-After": formatdate(time.time() + 5, usegmt=True)})
        self.assertGreaterEqual(AssistantTurn._rate_limit_wait(date), 4.0)
        self.assertLessEqual(AssistantTurn._rate_limit_wait(date), 5.0)
        invalid = self.rate_limit(headers={"Retry-After": "tomorrow"})
        self.assertIsNone(AssistantTurn._rate_limit_wait(invalid))

    def test_a_transient_rate_limit_waits_then_retries_once(self):
        limited = self.rate_limit(headers={"Retry-After": "0.05"})
        script = Script(limited, done())
        real_sleep, waits = time.sleep, []

        def record_sleep(seconds):
            waits.append(seconds)
            real_sleep(seconds)

        with patch("automotive.assistant.time.sleep", side_effect=record_sleep):
            result = run(service(script), "გამარჯობა")
        self.assertEqual((result["status"], len(script.requests)), ("OK", 2))
        self.assertGreaterEqual(sum(waits), 0.045)
        self.assertEqual(result["diagnostics"]["retries"], [{
            "status": 429, "cause": "rate_limit", "wait_seconds": 0.05,
            "exhausted": False}])

    def test_rate_limit_wait_is_never_shortened_to_fit_the_turn(self):
        script = Script(self.rate_limit(headers={"Retry-After": "120"}))
        result = run(service(script), "გამარჯობა")
        self.assertEqual((result["status"], len(script.requests)),
                         ("PROVIDER_RATE_LIMITED", 1))
        self.assertEqual(result["diagnostics"]["retries"], [{
            "status": 429, "cause": "rate_limit", "wait_seconds": 120.0,
            "exhausted": True}])

    def test_spend_cap_rate_limit_is_not_retried(self):
        error = self.rate_limit(body={"error": {"type": "rate_limit_error", "details": {
            "error_code": "enforced_spend_limit_reached"}}})
        script = Script(error)
        result = run(service(script), "გამარჯობა")
        self.assertEqual((result["status"], len(script.requests)), ("PROVIDER_BILLING", 1))
        self.assertEqual(result["diagnostics"]["retries"], [{
            "status": 429, "cause": "rate_limit", "wait_seconds": None,
            "exhausted": True}])

    def test_rate_limit_retry_shares_the_connection_retry_cap(self):
        limited = self.rate_limit(headers={"Retry-After": "0"})
        script = Script(anthropic.APIConnectionError(request=REQUEST), limited, done())
        real_sleep = time.sleep
        with patch("automotive.assistant.time.sleep", side_effect=real_sleep):
            result = run(service(script), "გამარჯობა")
        self.assertEqual((result["status"], len(script.requests)),
                         ("PROVIDER_RATE_LIMITED", 2))
        retries = result["diagnostics"]["retries"]
        self.assertEqual(retries[0]["cause"], "connection_or_server")
        self.assertEqual(retries[1], {"status": 429, "cause": "rate_limit",
                                      "wait_seconds": None, "exhausted": True})

    def test_cancellation_during_rate_limit_handling_stops_without_retry(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 4

        script = Script(self.rate_limit(headers={"Retry-After": "10"}), done())
        result = run(service(script), "გამარჯობა", cancelled=cancelled)
        self.assertEqual((result["status"], len(script.requests)), ("CANCELLED", 1))

    def test_provider_failures_apologise_in_the_conversations_language(self):
        denied = httpx.Response(401, request=REQUEST)
        cases = [
            (anthropic.AuthenticationError("private", response=denied, body=None),
             "AUTHENTICATION_ERROR"),
            (anthropic.PermissionDeniedError(
                "private", response=httpx.Response(403, request=REQUEST), body=None),
             "ACCESS_DENIED"),
            # Retry-After 0: the one retry runs at once and is refused again.
            (anthropic.RateLimitError(
                "private", response=httpx.Response(
                    429, headers={"Retry-After": "0"}, request=REQUEST), body=None),
             "PROVIDER_RATE_LIMITED"),
            (anthropic.APITimeoutError(REQUEST), "TIMEOUT"),
            (anthropic.APIStatusError(
                "private", response=httpx.Response(418, request=REQUEST), body=None),
             "PROVIDER_UNAVAILABLE"),
            # The provider's words for an account without credit, and a request it
            # refuses for another reason.
            (anthropic.BadRequestError(
                "private: Your credit balance is too low to access the Anthropic API.",
                response=httpx.Response(400, request=REQUEST), body=None),
             "PROVIDER_BILLING"),
            (anthropic.BadRequestError(
                "private", response=httpx.Response(400, request=REQUEST), body=None),
             "PROVIDER_UNAVAILABLE"),
        ]
        asked = {"question": "წინა თუ უკანა?", "options": [option("წინა"), option("უკანა")]}
        # Until the account is put right the provider refuses everything: "try again" is
        # not offered, the reply points to the part search and the phone.
        account = {"AUTHENTICATION_ERROR", "ACCESS_DENIED", "PROVIDER_BILLING"}
        for error, status in cases:
            with self.subTest(status=status):
                with self.assertLogs("automotive.assistant", level="ERROR") \
                        if status in account else contextlib.nullcontext() as logged:
                    # A provider that keeps refusing; only a retried error uses the second.
                    result = run(service(Script(error, error)), "нужны колодки", dialogue={
                        "memory": {"language": "ru", "question": asked,
                                   "notes": "Camry 2015"}})
                self.assertEqual((result["status"], result["act"]), (status, "help"))
                self.assertIn("Извините", result["answer"])
                if status in account:
                    self.assertIn("недоступен", result["answer"])
                    self.assertEqual(result["suggestions"], [])
                    self.assertIn(f"status={status}", "\n".join(logged.output))
                    self.assertNotIn("private", "\n".join(logged.output))
                else:
                    self.assertEqual(result["suggestions"], [{"label": "Попробовать снова",
                                                              "value": "нужны колодки"}])
                self.assertEqual(result["language"], "en")  # The API knows ka and en only.
                self.assertEqual(result["memory"]["language"], "ru")
                self.assertIn(status, result["warnings"])
                self.assertEqual((result["cards"], result["results"]), ([], []))
                self.assertNotIn("private", json.dumps(result))
                # The open question and the notes survive a failed turn.
                self.assertEqual(result["pending"]["question"], "წინა თუ უკანა?")
                self.assertEqual(result["memory"]["notes"], "Camry 2015")

    def test_without_a_key_the_reply_says_the_assistant_is_not_configured(self):
        result = run(service(anthropic_api_key=""), "გამარჯობა")
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertIn("API", result["answer"])
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["usage"]["model_requests"], 0)

    def test_a_catalogue_that_is_not_loaded_is_said_plainly(self):
        chat = service(Script(done()))
        chat.catalog_store = CatalogStore("")
        result = run(chat, "hello", dialogue={"memory": {"language": "en"}})
        self.assertEqual(result["status"], "DATA_UNAVAILABLE")
        self.assertIn("still loading", result["answer"])
        chat.catalog_store = None
        self.assertEqual(run(chat, "hello")["status"], "DATA_UNAVAILABLE")

    def test_a_refusal_and_a_fault_inside_the_turn_still_give_a_reply(self):
        refused = run(service(Script(says(stop="refusal"))), "გამარჯობა")
        self.assertEqual(refused["status"], "REFUSED")
        self.assertTrue(refused["answer"])
        with patch("automotive.assistant.AssistantTurn._context", side_effect=KeyError("bug")):
            broken = run(service(Script(done())), "გამარჯობა")
        self.assertEqual((broken["status"], broken["diagnostics"]["error"]),
                         ("DATA_UNAVAILABLE", "KeyError"))
        self.assertTrue(broken["answer"])

    def test_the_model_hears_how_old_the_stock_is_only_once_it_is_old(self):
        script = Script(done())
        run(service(script), "ხუნდები")
        self.assertNotIn("stock_note", context_of(script.requests[0]))  # current: nothing
        script = Script(done())
        chat = service(script)
        chat.catalog_store.current().observed_at = NOW - timedelta(hours=40)
        run(chat, "ხუნდები")
        self.assertEqual(context_of(script.requests[0])["stock_note"],
                         "stock figures last checked 40 hours ago; they may have changed "
                         "since, so availability needs the shop's confirmation")

    def test_a_customer_turn_after_hours_tells_the_model_the_shop_is_closed(self):
        public = {"memory": {"language": "en"}, "audience": "public"}
        with patch("automotive.shop_hours.is_open", return_value=False):
            script = Script(done())
            run(service(script), "brake pads", dialogue=public)
            self.assertIs(context_of(script.requests[0])["shop_closed"], True)
            script = Script(done())  # a shop employee at the counter is never told
            run(service(script), "brake pads")
            self.assertNotIn("shop_closed", context_of(script.requests[0]))
        with patch("automotive.shop_hours.is_open", return_value=True):
            script = Script(done())  # open: the turn carries nothing new
            run(service(script), "brake pads", dialogue=public)
            self.assertNotIn("shop_closed", context_of(script.requests[0]))

    def test_the_prompt_says_what_to_tell_a_customer_after_hours(self):
        self.assertIn("When shop_closed is true", assistant_prompt.SYSTEM_PROMPT)
        self.assertIn("during working hours", assistant_prompt.SYSTEM_PROMPT)

    def test_a_card_without_a_database_ages_with_the_snapshot(self):
        class Down(Repository):
            def get_product(self, product_id):
                raise OSError("no route")

        chat = service(Script(done(products=[product("r1")])), repository=Down())
        chat.catalog_store.current().observed_at = NOW - timedelta(hours=40)
        card = run(chat, "ხუნდები", dialogue={"memory": KNOWN})["cards"][0]
        # A week-old snapshot is not shown as in stock because the database is down.
        self.assertEqual(card["availability_status"], stock.UNKNOWN)

    def test_a_card_without_a_database_shows_the_snapshot(self):
        class Down(Repository):
            def get_product(self, product_id):
                raise OSError("no route")

        result = run(service(Script(done(products=[product("r1")])), repository=Down()),
                     "ხუნდები", dialogue={"memory": KNOWN})
        card = result["cards"][0]
        self.assertEqual(card["product"]["stock"], {"store": 6.0, "tuz11": 5.0})
        self.assertEqual(card["product"]["product_code"], "30023433")
        self.assertEqual(card["product"]["duplicate_code"], "D1222-8331/GDB3429/KD2765")
        self.assertIn("PRODUCT_REFRESH_UNAVAILABLE", result["warnings"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.script = Script()
        self.chat = service(self.script)
        self.search = Search()
        self.store = ConversationStore(self.chat, self.search)
        self.addCleanup(self.store.close)
        self.cid = self.store.create()["conversation_id"]

    def turn(self, message, *steps, **kwargs):
        self.script.steps.extend(steps)
        submitted = self.store.submit(self.cid, message, **kwargs)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            result = self.store.get(self.cid, submitted["turn_id"])
            if result["status"] != "RUNNING":
                return submitted, result
            time.sleep(0.005)
        self.fail("The stored assistant turn did not finish")

    def test_switching_to_typing_another_car_clears_vin_for_later_turns(self):
        _, first = self.turn(f"Pads for {VIN}", done(language="en", reply="I can help."))
        self.assertEqual(first["engine_status"], "OK")
        _, second = self.turn("For Honda Accord 2017", done(language="en", reply="Got it."))
        self.assertEqual(second["engine_status"], "OK")
        self.assertNotIn("vin", context_of(self.script.requests[-1]))
        _, third = self.turn("And a filter?", done(language="en", reply="I can help."))
        self.assertEqual(third["engine_status"], "OK")
        context = context_of(self.script.requests[-1])
        self.assertNotIn("vin", context)
        self.assertEqual(context["followup"]["vehicle"]["model"], "Accord")

    def test_memory_choice_and_the_pending_question_go_round_through_the_store(self):
        self.assertTrue(self.store.ai_led)
        asked = {"text": "Front or rear?", "options": [
            option("Front", "front pads: r1, r2"), option("Rear", "rear pads: r3")]}

        def first(request):
            found = handles(request)
            return done(reply="I found pads for the Camry.", language="en", question=asked,
                        suggestions=[], outcome="asked", car_label="Toyota Camry 2015",
                        notes=f"Camry 2015 pads; rear is {found['04466-33180']}")

        submitted, one = self.turn(
            "Camry 2015 brake pads",
            says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))), first,
            language="auto")
        self.assertEqual(submitted["language"], "ka")  # Nothing guesses it from the letters.
        self.assertEqual((one["status"], one["engine_status"], one["language"]),
                         ("COMPLETE", "CLARIFICATION", "en"))
        self.assertNotIn("memory", one)
        self.assertEqual(one["question"]["options"][1], {"label": "Rear", "value": "Rear"})
        conversation = self.store._conversations[self.cid]
        self.assertEqual(conversation["pending"]["options"][1]["meaning"], "rear pads: r3")
        self.assertEqual(conversation["memory"]["car_label"], "Toyota Camry 2015")
        self.assertEqual(conversation["category"], "")  # No rule read the message.

        def second(request):
            sent = context_of(request)
            self.assertEqual(sent["tapped_option"],
                             {"index": 2, "label": "Rear", "meaning": "rear pads: r3"})
            self.assertEqual(sent["your_question"]["text"], "Front or rear?")
            self.assertEqual(sent["language_so_far"], "en")
            # The rows its notes and option meanings name come back with fresh stock.
            rear = [item[0] for item in sent["rows"]["rows"] if item[1] == REAR]
            self.assertEqual(len(sent["rows"]["rows"]), 3)
            self.assertEqual(request["messages"][0]["content"], "Camry 2015 brake pads")
            self.assertIn("Front or rear?", request["messages"][1]["content"])
            return done(reply="Rear pads are below.", language="en", outcome="offered",
                        car_label=sent["car_label"],
                        products=[product(rear[0], evidence="CAMRY 2006->")])

        submitted, two = self.turn("Rear", second, language="auto", choice=2)
        self.assertEqual(submitted["language"], "en")
        self.assertEqual(two["status"], "COMPLETE", two.get("answer"))
        self.assertEqual(len(self.script.requests), 3)  # A tapped answer took one request.
        self.assertEqual([card["product"]["source_product_id"] for card in two["cards"]],
                         ["p3"])
        self.assertIsNone(self.store._conversations[self.cid]["pending"])

        # What the staff API returns validates, and nothing internal is in it.
        for payload in (one, two):
            model = ConversationTurn.model_validate(clamp_turn(payload))
            dumped = model.model_dump()
            self.assertEqual(dumped["outcome"], payload["outcome"])
            self.assertEqual(dumped["car_label"], "Toyota Camry 2015")
            sent = json.dumps(dumped, ensure_ascii=False, default=str)
            for private in ("memory", "meaning", "rear pads: r3", "diagnostics", "pending"):
                self.assertNotIn(private, sent)
        card = ConversationTurn.model_validate(clamp_turn(two)).cards[0]
        self.assertEqual((card.confidence, card.evidence, card.group, card.basis),
                         ("listed", "CAMRY 2006->", "g1", "listed"))
        self.assertEqual(card.reason, card.matched_via[0])

    def test_a_vin_is_kept_by_the_store_and_a_second_message_supersedes_the_first(self):
        release = []

        def slow(request):
            while not release:
                time.sleep(0.005)
            return done()

        self.script.steps.append(slow)
        first = self.store.submit(self.cid, "Camry 2015 " + VIN)
        while not self.script.requests:
            time.sleep(0.005)
        self.assertEqual(self.store._conversations[self.cid]["vin"], VIN)

        def answer(request):
            self.assertEqual(context_of(request)["unanswered_messages"], ["Camry 2015 [VIN]"])
            return done(reply="ხუნდები ქვემოთაა.")

        submitted, result = self.turn("წინა ხუნდები", answer)
        release.append(True)
        self.assertEqual(result["answer"], "ხუნდები ქვემოთაა.")
        with self.assertRaises(Exception):
            self.store.get(self.cid, first["turn_id"])
        self.assertEqual(self.chat.vin_service.calls, [VIN, VIN])
        self.assertNotIn(VIN, json.dumps(self.script.requests, ensure_ascii=False, default=str))
        from automotive.conversations import ConversationError

        with self.assertRaises(ConversationError) as caught:
            self.store.submit(self.cid, VIN, vin="WVWZZZ1JZXW000001")
        self.assertEqual(caught.exception.status, 422)

    def test_a_failed_turn_is_marked_failed_and_logged_with_what_was_searched(self):
        log = []
        self.store.chat_log = SimpleNamespace(
            turn=lambda identifier, **record: log.append(record),
            session_started=lambda identifier: None, session_ended=lambda *args: None)
        _, result = self.turn(
            "Camry ხუნდები",
            says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
            anthropic.APITimeoutError(REQUEST))
        self.assertEqual((result["status"], result["engine_status"]), ("FAILED", "TIMEOUT"))
        self.assertTrue(result["answer"])
        until = time.monotonic() + 2
        while not log and time.monotonic() < until:
            time.sleep(0.005)
        diagnostics = log[0]["payload"]["diagnostics"]
        self.assertEqual(diagnostics["engine"], "assistant")
        self.assertEqual(diagnostics["tools"][0]["args"]["part_terms"], ["ხუნდი"])
        self.assertEqual(log[0]["payload"]["usage"]["model_requests"], 1)

    def test_opening_a_conversation_builds_the_index_off_the_request_path(self):
        from automotive import assistant_catalog

        store = CatalogStore("", self.chat.rules)
        store.replace(Catalog.from_rows(ROWS, self.chat.rules))
        self.chat.catalog_store = store
        self.assertNotIn(store.current(), assistant_catalog._INDEXES)
        self.store.create()
        until = time.monotonic() + 2
        while store.current() not in assistant_catalog._INDEXES and time.monotonic() < until:
            time.sleep(0.005)
        self.assertIn(store.current(), assistant_catalog._INDEXES)
        # A store whose chat service cannot prepare, or fails to, opens conversations too.
        plain_store = ConversationStore(SimpleNamespace(settings=Settings(), prepare=1 / 2), None)
        self.addCleanup(plain_store.close)
        self.assertTrue(plain_store.create()["conversation_id"])
        self.chat.catalog_store = SimpleNamespace(loaded=True, current=lambda: 1 / 0)
        self.assertTrue(self.store.create()["conversation_id"])

    def test_the_public_store_marks_its_conversations(self):
        self.store.audience = "public"
        self.turn("გამარჯობა", done())
        self.assertEqual(context_of(self.script.requests[0])["audience"], "public")
        self.assertEqual(ConversationStore(self.chat, None).audience, "staff")
        # A visitor has no staff form, whatever a request carries.
        self.turn("ხუნდები", done(), vehicle={"make": "Toyota", "model": "Camry"})
        self.assertNotIn("staff_form", context_of(self.script.requests[1]))

    def test_the_staff_form_is_context_that_a_message_overrides(self):
        """The staff page sends its vehicle form and its VIN field with every message, also
        when they are left over from the customer before."""
        self.chat.rules = Rules(never_offer=(
            NeverOffer("30023433", "TOYOTA", "Camry", "wrong pad"),
            NeverOffer("2040026342", "HONDA", "Fit", "not for the Fit")))
        form = {"make": "Toyota", "model": "Camry", "model_year": 2015, "trim": "SE"}
        decoded = {"make": "TOYOTA", "model": "Camry", "model_year": 2015,
                   "displacement_l": "2.5", "fuel": "Gasoline"}
        conversation = self.store._conversations[self.cid]

        def search_first(request):
            sent = context_of(request)
            # Nobody typed a VIN: it is the form's, and so is what it decodes to.
            self.assertNotIn("vin", sent)
            self.assertEqual(sent["staff_form"], {
                "car": {"trim": "SE"}, "vin_field": {"decoded": True, "facts": decoded}})
            self.assertEqual(sent["customer_message"], "Honda Fit 2008 წინა ხუნდები")
            return says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"])))

        def offer(request):
            found = handles(request)
            return done(reply="Honda Fit 2008-ის ხუნდები ქვემოთაა.", outcome="offered",
                        car_label="Honda Fit 2008",
                        products=[product(found["PN1521"], found["37677"])])

        _, one = self.turn("Honda Fit 2008 წინა ხუნდები", search_first, offer, vehicle=form,
                           vin=VIN)
        self.assertEqual((one["status"], one["car_label"]), ("COMPLETE", "Honda Fit 2008"))
        # The car the message names decides the staff vetoes, not the car in the form: the
        # rule for the Fit holds and the rule for the Camry does not.
        self.assertEqual([card["product"]["source_product_id"] for card in one["cards"]],
                         ["p1"])
        self.assertEqual(one["diagnostics"]["removed"], [
            {"product": "p2", "rule": "never_offer", "reason": "not for the Fit"}])
        # The form stays what it was: the page shows the model's own name for the car.
        self.assertEqual((conversation["vin"], one["vehicle"]["make"], one["vehicle"]["trim"]),
                         (VIN, "TOYOTA", "SE"))

        def follow_up(request):
            sent = context_of(request)
            self.assertNotIn("vin", sent)
            self.assertEqual(sent["staff_form"]["car"], {"trim": "SE"})
            self.assertEqual(sent["car_label"], "Honda Fit 2008")
            return done(reply="უკანა ხუნდებს ვეძებ.", car_label="Honda Fit 2008")

        # The page sends back what the reply put into its form, numbers as numbers.
        echoed = {"make": "TOYOTA", "model": "Camry", "model_year": 2015, "trim": "SE",
                  "displacement_l": 2.5, "fuel": "Gasoline"}
        self.turn("უკანაც მინდა", follow_up, vehicle=echoed, vin=VIN)
        self.assertEqual(conversation["vin"], VIN)

        typed = "WVWZZZ1JZXW000001"

        def with_typed_vin(request):
            sent = context_of(request)
            # A VIN typed in a message is the customer's car, not a field of the form.
            self.assertEqual(sent["vin"], {"decoded": True, "facts": decoded})
            self.assertNotIn("staff_form", sent)
            return done(reply="მანქანა ამოვიცანი.", car_label="Toyota Camry 2015")

        self.turn(f"ამ მანქანაზე მინდა {typed}", with_typed_vin, vin=typed)

        def with_a_stale_form(request):
            sent = context_of(request)
            self.assertEqual(sent["vin"], {"decoded": True, "facts": decoded})
            self.assertEqual(sent["staff_form"], {
                "car": {"make": "Honda", "model": "Fit", "model_year": 2008}})
            return done(reply="ზეთის ფილტრს ვეძებ.", car_label="Toyota Camry 2015")

        # A form left over from another car comes with the next message: the typed VIN
        # stays the conversation's, and the form is only what the form says.
        self.turn("ზეთის ფილტრი", with_a_stale_form,
                  vehicle={"make": "Honda", "model": "Fit", "model_year": 2008}, vin=typed)
        self.assertEqual(conversation["vin"], typed)
        self.assertNotIn(VIN, json.dumps(self.script.requests, ensure_ascii=False, default=str))
        self.assertNotIn(typed, json.dumps(self.script.requests, ensure_ascii=False,
                                           default=str))
        for told in ("staff_form", "outranks the", "left over from the customer before"):
            self.assertIn(told, assistant_prompt.SYSTEM_PROMPT)
        # A form whose VIN cannot be decoded says so as the form's, not as the customer's.
        self.chat.vin_service.error = RuntimeError("decoder down")
        other = self.store.create()["conversation_id"]
        self.script.steps.append(done())
        submitted = self.store.submit(other, "ხუნდები", vin=VIN)
        until = time.monotonic() + 5
        while self.store.get(other, submitted["turn_id"])["status"] == "RUNNING" \
                and time.monotonic() < until:
            time.sleep(0.005)
        note = context_of(self.script.requests[-1])["staff_form"]["vin_field"]
        self.assertEqual(note["decoded"], False)
        self.assertIn("The staff form holds a VIN", note["note"])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_staff_api_carries_the_new_fields_and_nothing_internal(self):
        from automotive.api import create_app

        asked = {"text": "წინა თუ უკანა?", "options": [
            option("წინა", "front pads: r1"), option("უკანა", "rear pads")]}
        script = Script(
            done(reply="Camry-ზე ხუნდები გვაქვს.", question=asked, suggestions=[],
                 outcome="asked", car_label="Toyota Camry 2015"),
            lambda request: done(
                reply="წინა ხუნდი ქვემოთაა.", outcome="offered", car_label="Toyota Camry 2015",
                products=[product(handles(request)["PN1521"])],
                suggestions=[option(f"ნაბიჯი {number}") for number in range(6)]))
        settings = Settings(chat_engine="assistant", anthropic_api_key=SECRET,
                            vin_hmac_key="h" * 32)
        chat = ChatService(Repository(), Vin(), settings,
                           SimpleNamespace(messages=SimpleNamespace(create=script.create)))
        app = create_app(settings=settings, repository=chat.repository,
                         vin_service=chat.vin_service, chat_service=chat,
                         search_service=Search(), site={"catalog": Catalog.from_rows(ROWS)})
        self.addCleanup(app.state.conversations.close)
        if app.state.site.chat is not None:
            self.addCleanup(app.state.site.chat.close)
        # The engine searches the snapshot the site holds.
        self.assertIs(chat.catalog_store, app.state.site.catalog_store)

        async def turn(client, base, body):
            posted = await client.post(base + "/messages", json=body)
            self.assertEqual(posted.status_code, 202, posted.text)
            for _ in range(400):
                reply = await client.get(base + "/turns/" + posted.json()["turn_id"])
                if reply.json()["status"] != "RUNNING":
                    return reply
                await asyncio.sleep(0.005)
            self.fail("The turn did not finish")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8767") as client:
            cid = (await client.post("/api/v1/chat/conversations")).json()["conversation_id"]
            base = "/api/v1/chat/conversations/" + cid
            for wrong in (0, 9, "2", 1.5):
                refused = await client.post(base + "/messages",
                                            json={"message": "წინა", "choice": wrong})
                self.assertEqual(refused.status_code, 422)
            first = (await turn(client, base, {"message": "Camry 2015 ხუნდები PN1521"})).json()
            self.assertEqual(first["question"], {"text": "წინა თუ უკანა?", "options": [
                {"label": "წინა", "value": "წინა"}, {"label": "უკანა", "value": "უკანა"}]})
            self.assertEqual((first["outcome"], first["act"], first["car_label"]),
                             ("asked", "clarify", "Toyota Camry 2015"))
            self.assertEqual(first["quick_replies"], ["წინა", "უკანა"])
            reply = await turn(client, base, {"message": "წინა", "choice": 1})
            second = reply.json()
        self.assertEqual(context_of(script.requests[1])["tapped_option"]["index"], 1)
        self.assertEqual((second["status"], second["outcome"], second["question"]),
                         ("COMPLETE", "offered", None))
        self.assertEqual(len(second["suggestions"]), 4)
        self.assertEqual(second["groups"], [{"id": "g1", "label": "წინა ხუნდები", "count": 1}])
        card = second["cards"][0]
        self.assertEqual((card["group"], card["confidence"], card["evidence"]),
                         ("g1", "listed", "CAMRY 2006->"))
        self.assertEqual(card["reason"], "2006 წლიდან, მოიცავს 2015-ს.")
        self.assertEqual(card["product"]["price"], 85.0)
        for private in ("memory", "pending", "diagnostics", "meaning", "front pads: r1",
                        "handles", SECRET):
            self.assertNotIn(private, reply.text)

    def test_an_older_engines_card_and_extra_buttons_still_validate(self):
        payload = clamp_turn({
            "conversation_id": "c", "turn_id": "t", "status": "COMPLETE",
            "quick_replies": [str(number) for number in range(12)],
            "clarification_choices": list(range(12)),
            "suggestions": [{"label": str(number)} for number in range(9)],
            "question": {"text": "?", "options": [{"label": str(n)} for n in range(12)]},
            "cards": [{"product": {"source_product_id": "p1"}, "match_source": "x",
                       "fitment_status": "UNKNOWN", "evidence": [{"evidence_id": "ev_1"}]}]})
        turn = ConversationTurn.model_validate(payload)
        self.assertEqual((len(turn.quick_replies), len(turn.clarification_choices),
                          len(turn.suggestions), len(turn.question.options)), (8, 8, 4, 8))
        self.assertIsNone(turn.cards[0].evidence)  # Evidence records stay on the server.
        self.assertEqual(ConversationTurn(conversation_id="c", turn_id="t",
                                          status="RUNNING").model_dump()["question"], None)


class ConversationKeptTests(unittest.TestCase):
    """The model's own answer reaches the customer, and a changed car keeps the conversation."""

    def test_the_models_oil_answer_is_not_replaced_by_a_canned_question(self):
        camry = {"followup": {"vehicle": {"make": "TOYOTA", "model": "Camry", "model_year": 2015}}}
        asked_year = {"question": {"act": "clarify", "question": "რომელი წლისაა მანქანა?",
                                   "options": []}, "oil_question": "year"}
        for message, memory in (("ტოიოტა ქემრი 2.5 2018, რომელი ზეთი უხდება?", {}),
                                ("2015 წლის", asked_year), ("შელის ზეთი გაქვთ?", camry)):
            with self.subTest(message):
                script = Script(
                    says(use("oil_guide", car(make="Toyota", model="Camry", year=2018))),
                    done(reply="აი, ეს ზეთი გვაქვს.", outcome="offered",
                         products=[product("r5", confidence="spec", evidence="5W-30")]))
                result = run(service(script), message,
                             dialogue={"memory": {**memory, "handles": {"r5": "p5"}}})
                self.assertEqual(result["answer"], "აი, ეს ზეთი გვაქვს.")
                self.assertIsNone(result["question"])
                self.assertEqual([card["product"]["source_product_id"]
                                  for card in result["cards"]], ["p5"])

    def test_a_changed_year_or_engine_keeps_the_conversation(self):
        opening = "Toyota Camry 2015, engine code 2AR-FE, წინა ხუნდები"
        first = run(service(Script(done(reply="ხუნდები ქვემოთაა.", notes="Camry front pads"))),
                    opening)
        for later in ("Toyota Camry 2016", "engine code: 2AR-FXE"):
            with self.subTest(later):
                script = Script(done())
                run(service(script), later, dialogue={"memory": copy.deepcopy(first["memory"])})
                self.assertEqual(script.requests[0]["messages"][0]["content"], opening)
                self.assertEqual(context_of(script.requests[0])["notes"], "Camry front pads")


class CustomerHandoverTests(unittest.TestCase):
    def test_a_customer_gets_the_models_own_engine_question(self):
        source = Search(fluids=[{"provider": "Shell", "status": "CONFLICTED",
                                 "candidates": ["Camry 2.5 133kW", "Camry 2.5 Hybrid"]}])
        script = Script(
            says(use("oil_guide", car(make="Toyota", model="Camry", year=2018))),
            done(reply="ერთი დეტალი წყვეტს ზეთს.", question="ძრავი ჰიბრიდულია?",
                 options=["ჰიბრიდი", "ჩვეულებრივი"], outcome="asked"))
        result = run(service(script), "Toyota Camry 2018 2.5 ზეთი", search_service=source,
                     dialogue={"audience": "public", "memory": {}})
        self.assertEqual(result["question"]["text"], "ძრავი ჰიბრიდულია?")
        self.assertEqual(result["quick_replies"], ["ჰიბრიდი", "ჩვეულებრივი"])

    def test_a_customer_handed_to_staff_has_been_asked_for_a_phone(self):
        public = {"audience": "public", "memory": {}}
        script = Script(done(reply="ზეთს გუნდი შეგირჩევთ. მოგვწერეთ ტელეფონი.",
                             outcome="handoff"))
        result = run(service(script), "Camry ზეთი, ძრავი არ ვიცი", dialogue=public)
        self.assertEqual(result["memory"]["phone"], "asked")
        # A turn that failed asked for nothing.
        failed = run(service(Script(anthropic.APITimeoutError(REQUEST))), "Camry ზეთი",
                     dialogue=public)
        self.assertEqual((failed["status"], failed["memory"]["phone"]), ("TIMEOUT", ""))
        self.assertIn("or outcome is handoff", assistant_prompt.SYSTEM_PROMPT)


class EngineSelectionTests(unittest.TestCase):
    def test_direct_settings_keep_the_engine_they_always_selected(self):
        self.assertEqual(Settings().chat_engine, "")
        self.assertEqual(engine_name(Settings()), "scripted")
        self.assertEqual(engine_name(Settings(chat_orchestration=True)), "orchestrated")
        self.assertEqual(engine_name(Settings(chat_engine="assistant")), "assistant")
        self.assertEqual(engine_name(Settings(chat_engine="scripted", chat_orchestration=True)),
                         "scripted")
        self.assertEqual(engine_name(None), "scripted")
        for settings, led in ((Settings(), False), (Settings(chat_orchestration=True), True),
                              (Settings(chat_engine="assistant"), True)):
            store = ConversationStore(SimpleNamespace(settings=settings), None)
            self.addCleanup(store.close)
            self.assertIs(store.ai_led, led)

    def test_each_engine_is_dispatched_by_the_chat_service(self):
        for settings, turn in ((Settings(), "automotive.conversational.ConversationTurn"),
                               (Settings(chat_orchestration=True),
                                "automotive.orchestration.OrchestratedTurn"),
                               (Settings(chat_engine="orchestrated"),
                                "automotive.orchestration.OrchestratedTurn"),
                               (Settings(chat_engine="assistant"),
                                "automotive.assistant.AssistantTurn")):
            with self.subTest(turn=turn), patch(turn) as chosen:
                chosen.return_value.run.return_value = {"status": "OK"}
                ChatService(Repository(), Vin(), settings).respond_conversation("hello")
                chosen.assert_called_once()

    def test_the_environment_selects_the_assistant_by_default(self):
        def from_env(**environment):
            with patch.dict(os.environ, environment, clear=True), \
                    patch("automotive.config.load_dotenv"):
                return Settings.from_env()

        settings = from_env(ANTHROPIC_MODEL="claude-sonnet-5-5")
        self.assertEqual((settings.chat_engine, settings.assistant_model,
                          settings.assistant_effort, settings.assistant_turn_seconds,
                          settings.assistant_max_requests, settings.assistant_max_tool_calls),
                         ("assistant", "claude-sonnet-5-5", "low", 30, 4, 10))
        self.assertEqual((settings.assistant_cache_ttl, Settings().assistant_cache_ttl,
                          from_env(AUTOMOTIVE_ASSISTANT_CACHE_TTL=" 5M ").assistant_cache_ttl),
                         ("1h", "1h", "5m"))
        # The readings of the part names: a file by default, none when built directly.
        self.assertEqual((settings.assistant_names_path, Settings().assistant_names_path,
                          from_env(AUTOMOTIVE_ASSISTANT_NAMES_PATH=" ").assistant_names_path,
                          from_env(AUTOMOTIVE_ASSISTANT_NAMES_PATH="/srv/names.json")
                          .assistant_names_path),
                         ("data/assistant/part_names.json", "", "", "/srv/names.json"))
        self.assertEqual(from_env(AUTOMOTIVE_ASSISTANT_THINKING=" Off ").assistant_thinking,
                         "off")
        # The model the test conversations chose is the default; empty means ANTHROPIC_MODEL.
        self.assertEqual(from_env().assistant_model, "claude-sonnet-5-5")
        self.assertEqual(from_env(ANTHROPIC_MODEL="claude-haiku-5-5").assistant_model,
                         "claude-sonnet-5-5")
        self.assertEqual(from_env(AUTOMOTIVE_ASSISTANT_MODEL="",
                                  ANTHROPIC_MODEL="claude-haiku-5-5").assistant_model,
                         "claude-haiku-5-5")
        chosen = from_env(AUTOMOTIVE_CHAT_ENGINE=" Orchestrated ",
                          AUTOMOTIVE_ASSISTANT_MODEL="claude-haiku-5-5",
                          AUTOMOTIVE_ASSISTANT_EFFORT="")
        self.assertEqual((chosen.chat_engine, chosen.assistant_model, chosen.assistant_effort),
                         ("orchestrated", "claude-haiku-5-5", ""))
        self.assertEqual(engine_name(from_env(AUTOMOTIVE_CHAT_ENGINE="",
                                              AUTOMOTIVE_CHAT_ORCHESTRATION="false")),
                         "scripted")
        for name, value in (("AUTOMOTIVE_CHAT_ENGINE", "rules"),
                            ("AUTOMOTIVE_ASSISTANT_EFFORT", "max"),
                            ("AUTOMOTIVE_ASSISTANT_THINKING", "sometimes"),
                            ("AUTOMOTIVE_ASSISTANT_TURN_SECONDS", "5"),
                            ("AUTOMOTIVE_ASSISTANT_MAX_REQUESTS", "9"),
                            ("AUTOMOTIVE_ASSISTANT_MAX_TOOL_CALLS", "0"),
                            ("AUTOMOTIVE_ASSISTANT_CACHE_TTL", "2h")):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                from_env(**{name: value})
            self.assertIn(name, str(caught.exception))

    def test_the_new_modules_import_none_of_the_rule_modules(self):
        from pathlib import Path

        for name in ("assistant", "assistant_tools", "assistant_prompt"):
            source = (Path(__file__).parents[1] / "automotive" / f"{name}.py").read_text("utf-8")
            imports = [line for line in source.splitlines()
                       if line.lstrip().startswith(("import ", "from "))]
            for banned in ("dialogue", "categories", "replies", "grounding",
                           "conversational", "orchestration"):
                self.assertFalse([line for line in imports if banned in line], (name, banned))


if __name__ == "__main__":
    unittest.main()
