"""Web research for the assistant: what leaves the process (no VIN, no phone number), what
comes back (numbers their own quotes show, no blocked site), the backends' requests, and
the background jobs and their cache. Fakes only: no model, network or subscription is used.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import anthropic
import httpx

from automotive import assistant_bench
from automotive.assistant_research import (
    BLOCKED_DOMAINS,
    FINDINGS_SCHEMA,
    MAX_FINDINGS,
    PENDING,
    RESEARCH_SYSTEM,
    ApiBackend,
    BenchBackend,
    OpenRouterBackend,
    ResearchError,
    ResearchService,
    cache_key,
    clean_findings,
    clean_question,
    numbers_of,
    question_text,
)
from automotive.public_http import VIN_IN_TEXT

VIN = "WVWZZZ1KZ6W000001"
LOGGER_NAME = "automotive.assistant_research"
REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
CAR = {"kind": "numbers", "make": "Toyota", "model": "Corolla", "year": 2015, "trim": "1.6",
       "engine_code": "1ZR-FAE", "body": "sedan", "part": "front brake pads",
       "position": "front", "numbers": [], "question": ""}
# Failed jobs log a warning; keep those lines out of the test output.
QUIET = logging.NullHandler()


def setUpModule() -> None:
    logging.getLogger(LOGGER_NAME).addHandler(QUIET)


def tearDownModule() -> None:
    logging.getLogger(LOGGER_NAME).removeHandler(QUIET)


def bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(message, response=httpx.Response(400, request=REQUEST),
                                     body=None)


def finding(**changes) -> dict:
    found = {"number": "04465-02220", "brand": "Toyota", "kind": "oe",
             "part": "Front disc brake pad kit", "position": "front",
             "fits": ["Toyota Corolla E150 2006-2013"],
             "source_urls": ["https://www.toyotapartsdeal.com/oem/04465-02220.html"],
             "quote": "Genuine Toyota pad kit 04465-02220 for Corolla 2006-2013, front axle."}
    found.update(changes)
    return found


def raw(*findings, answer: str = "", note: str = "") -> dict:
    return {"findings": list(findings), "answer": answer, "note": note}


# -- a Messages API stand-in ------------------------------------------------------------


class Client:
    """Answers from a script and keeps every request."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def block(kind: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def response(*blocks, stop: str = "tool_use", searches: int = 0) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=SimpleNamespace(
        input_tokens=50, output_tokens=20,
        server_tool_use=SimpleNamespace(web_search_requests=searches)))


def report(*findings, answer: str = "", note: str = "") -> SimpleNamespace:
    return block("tool_use", id="toolu_1", name="report_findings",
                 input=raw(*(findings or (finding(),)), answer=answer, note=note))


def searched(identifier: str = "srvtoolu_1") -> list[SimpleNamespace]:
    return [block("server_tool_use", id=identifier, name="web_search",
                  input={"query": "corolla 2015 front pads"}),
            block("web_search_tool_result", tool_use_id=identifier, content=[])]


# -- a research backend stand-in --------------------------------------------------------


class Backend:
    """Records each run, can hold it until released, and answers from a script: a raw
    findings dict, or an exception to raise. The last answer repeats."""

    def __init__(self, *answers, name: str = "fake", held: bool = False) -> None:
        self.answers = list(answers) or [raw(finding())]
        self.name = name
        self.calls: list[tuple] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not held:
            self.release.set()
        self._lock = threading.Lock()

    def run(self, question, system, timeout):
        with self._lock:
            self.calls.append((question, system, timeout))
            answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        self.started.set()
        self.release.wait(10)
        if isinstance(answer, BaseException):
            raise answer
        return answer, {"web_search_requests": 2}


class QuestionTests(unittest.TestCase):
    def test_a_vin_in_any_field_never_reaches_the_backend(self) -> None:
        backend = Backend()
        service = ResearchService(backend, cache_path=None, wait_seconds=5)
        args = {key: f"{VIN} x" for key in ("make", "model", "trim", "engine_code", "body",
                                             "part", "position")}
        args.update(kind="numbers", year=2015,
                    numbers=[VIN, VIN.lower(), f"VIN{VIN}", "04465-02220"],
                    question=f"Which pads for {VIN.lower()}, VIN:{VIN}?")
        self.assertEqual(service.ask(args)["status"], "answered")
        question, system, _ = backend.calls[0]
        sent = (json.dumps(question) + question_text(question) + system).casefold()
        self.assertNotIn(VIN.casefold(), sent)
        self.assertEqual(question["numbers"], ["04465-02220"])
        self.assertEqual(question["make"], "x")
        # Even uncleaned input handed straight to a backend is cleaned before it is sent.
        client = Client(response(report()))
        ApiBackend(client).run({"part": f"pads for {VIN}"}, RESEARCH_SYSTEM, 60)
        self.assertNotIn(VIN, json.dumps(client.requests[0]["messages"]))

    def test_phone_numbers_leave_text_fields_but_part_numbers_stay(self) -> None:
        question = clean_question({
            "kind": "verify", "part": "pads 0986494123, call +995 555 12 34 56",
            "question": "Fits 2010-2015 and 2016 2017 cars? My phone is (555) 123-4567",
            "numbers": ["11427566327", "0 986 494 123", "+995 555 123 456", "0986494123"]})
        # As written for every text field, part included...
        self.assertEqual(question["part"], "pads , call")
        # ...except runs of model years, which are no phone numbers.
        self.assertEqual(question["question"], "Fits 2010-2015 and 2016 2017 cars? My phone is")
        # BMW (eleven digits) and Bosch (ten, spaced) numbers have a phone's shape and stay;
        # a + marks a phone number; one number spelled twice is checked once.
        self.assertEqual(question["numbers"], ["11427566327", "0 986 494 123"])

    def test_the_question_is_typed_and_bounded(self) -> None:
        question = clean_question({
            "kind": "VERIFY", "make": "  Toyota ", "model": "Corolla", "year": "2015",
            "numbers": [f"PN{index:04d}" for index in range(12)] + [4465], "extra": "x",
            "question": "how long " * 60})
        self.assertEqual(question["kind"], "verify")
        self.assertEqual(question["make"], "Toyota")
        self.assertEqual(question["year"], 2015)
        self.assertEqual(len(question["numbers"]), 8)
        self.assertLessEqual(len(question["question"]), 300)
        self.assertNotIn("extra", question)
        self.assertEqual(clean_question(question), question)
        odd = clean_question({"kind": "anything", "year": 1700, "make": ["Toyota"],
                              "numbers": "04465-02220"})
        self.assertEqual((odd["kind"], odd["year"], odd["make"], odd["numbers"]),
                         ("numbers", 0, "", ["04465-02220"]))
        self.assertEqual(clean_question("not a dict")["part"], "")

    def test_the_cache_key_is_stable_and_ignores_case_spacing_and_order(self) -> None:
        base = {**CAR, "kind": "verify", "numbers": ["0 986 494 123", "04465-02220"]}
        same = {**base, "make": "TOYOTA", "model": " corolla ", "part": "Front  Brake Pads",
                "numbers": ["04465 02220", "0986494123"]}
        self.assertEqual(cache_key(base), cache_key(same))
        self.assertEqual(cache_key(base), cache_key(dict(reversed(list(base.items())))))
        self.assertRegex(cache_key(base), r"^[0-9a-f]{64}$")
        self.assertNotEqual(cache_key(base), cache_key({**base, "part": "rear brake pads"}))
        self.assertNotEqual(cache_key(base), cache_key({**base, "year": 2016}))
        self.assertNotEqual(cache_key(base), cache_key({**base, "kind": "fact"}))

    def test_the_research_model_reads_the_task_and_only_the_filled_fields(self) -> None:
        text = question_text({"kind": "verify", "make": "BMW", "numbers": ["11427566327"]})
        task, data = text.split("\n", 1)
        self.assertTrue(task.startswith("Check these part numbers"))
        self.assertEqual(json.loads(data), {"make": "BMW", "numbers": ["11427566327"]})


class FindingsTests(unittest.TestCase):
    def test_blocked_sites_are_never_cited_subdomains_included(self) -> None:
        found = clean_findings(raw(
            finding(source_urls=["https://www.partsouq.com/x", "https://7zap.com/y"]),
            finding(number="0 986 494 123", brand="Bosch", quote="Bosch 0986494123 pads",
                    source_urls=["https://shop.rockauto.com/z", "javascript:alert(1)",
                                 "https://user:secret@bosch.example.com/a",
                                 "https://www.bosch-automotive.com/p/0986494123"]),
            finding(number="P83151", brand="Brembo", quote="Brembo P 83 151 front",
                    source_urls=["https://not7zap.com/a", "https://www.realoem.com/b",
                                 "https://realoem.com.example.net/c"])))
        self.assertEqual([item["number"] for item in found["findings"]],
                         ["0 986 494 123", "P83151"])
        self.assertEqual(found["findings"][0]["source_urls"],
                         ["https://www.bosch-automotive.com/p/0986494123"])
        self.assertEqual(found["findings"][1]["source_urls"],
                         ["https://not7zap.com/a", "https://realoem.com.example.net/c"])
        hosts = {urlsplit(url).hostname for item in found["findings"]
                 for url in item["source_urls"]}
        for domain in BLOCKED_DOMAINS:
            self.assertFalse([host for host in hosts
                              if host == domain or host.endswith("." + domain)])

    def test_a_number_is_kept_only_when_its_own_quote_shows_it(self) -> None:
        found = clean_findings(raw(
            finding(number="W 712/75", brand="MANN", quote="MANN-FILTER w71275 oil filter"),
            finding(number="11-42-7-566-327", brand="BMW",
                    quote="BMW 11 42 7 566 327 oil filter element"),
            finding(number="0986494124", brand="Bosch", quote="Bosch 0986494123 front pads"),
            finding(quote="Toyota front pads, see the catalogue"),
            finding(number="ABCD", quote="ABCD pads")))
        self.assertEqual([item["number"] for item in found["findings"]],
                         ["W 712/75", "11-42-7-566-327"])

    def test_one_number_from_several_pages_is_one_finding(self) -> None:
        found = clean_findings(raw(
            finding(fits=["Toyota Corolla E150"]),
            finding(number="0446502220", brand="TOYOTA", kind="unknown", position="",
                    quote="OEM 0446502220 Corolla Auris",
                    fits=["toyota corolla e150", "Toyota Auris E150"],
                    source_urls=["https://toyotapartsdeal.com/b", "https://www.amayama.com/c"]),
            finding(brand="Akebono", kind="aftermarket",
                    source_urls=["https://www.amayama.com/c"])))
        first, second = found["findings"]
        self.assertEqual(first["source_urls"],
                         ["https://www.toyotapartsdeal.com/oem/04465-02220.html",
                          "https://toyotapartsdeal.com/b", "https://www.amayama.com/c"])
        self.assertEqual(first["fits"], ["Toyota Corolla E150", "Toyota Auris E150"])
        self.assertEqual((first["kind"], first["position"]), ("oe", "front"))
        # toyotapartsdeal.com with and without www is one site; amayama.com is the other.
        self.assertEqual(first["sources_agree"], 2)
        self.assertEqual((second["brand"], second["sources_agree"]), ("Akebono", 1))

    def test_types_lengths_and_counts_are_bounded(self) -> None:
        many = [finding(number=f"PN{index:05d}", quote=f"part PN{index:05d} here")
                for index in range(30)]
        found = clean_findings({"findings": many, "answer": 5, "note": None})
        self.assertEqual(len(found["findings"]), MAX_FINDINGS)
        self.assertEqual((found["answer"], found["note"]), ("5", ""))
        odd = clean_findings({"findings": ["junk", None, finding(
            kind="OEM", fits="Toyota Corolla", source_urls="https://www.toyota.com/p",
            quote="lorem " * 200 + "04465-02220" + " ipsum" * 200)]})
        item = odd["findings"][0]
        self.assertEqual((item["kind"], item["fits"], item["source_urls"]),
                         ("unknown", ["Toyota Corolla"], ["https://www.toyota.com/p"]))
        self.assertLessEqual(len(item["quote"]), 240)
        self.assertIn("04465-02220", item["quote"])
        self.assertEqual(clean_findings("not a dict"), {"findings": [], "answer": "", "note": ""})
        huge = [finding(number=f"PN{index:05d}", quote=f"PN{index:05d} " + "word " * 400,
                        fits=[f"Car {index} {'x ' * 40}{n}" for n in range(6)],
                        source_urls=[f"https://site{n}.example.com/{'p' * 300}"
                                     for n in range(5)]) for index in range(20)]
        self.assertLessEqual(len(json.dumps(clean_findings(raw(*huge)))), 13_000)

    def test_vin_shaped_text_never_comes_back(self) -> None:
        found = clean_findings(raw(
            finding(quote=f"VIN {VIN} takes pads 04465-02220", fits=[f"car {VIN}"],
                    source_urls=[f"https://decoder.example.com/{VIN}",
                                 "https://www.toyota.com/p"]),
            finding(number=VIN, quote=f"number {VIN}")))
        self.assertNotIn(VIN, json.dumps(found))
        self.assertEqual([item["source_urls"] for item in found["findings"]],
                         [["https://www.toyota.com/p"]])

    def test_numbers_of_lists_each_number_once(self) -> None:
        result = {"findings": [{"number": "04465-02220"}, {"number": "0446502220"},
                               {"number": "P83151"}, {"number": 5}, "junk"]}
        self.assertEqual(numbers_of(result), ["04465-02220", "P83151"])
        self.assertEqual(numbers_of({"status": "pending"}), [])
        self.assertEqual(numbers_of(None), [])


class ApiBackendTests(unittest.TestCase):
    def test_the_report_tool_answers_and_the_request_has_the_agreed_shape(self) -> None:
        client = Client(response(*searched(), report(), searches=2))
        question = clean_question(CAR)
        found, usage = ApiBackend(client).run(question, RESEARCH_SYSTEM, 60)
        self.assertEqual(found["findings"][0]["number"], "04465-02220")
        self.assertEqual(usage, {"web_search_requests": 2, "input_tokens": 50,
                                 "output_tokens": 20})
        request = client.requests[0]
        self.assertEqual((request["model"], request["max_tokens"], request["system"]),
                         ("claude-haiku-5-5", 3000, RESEARCH_SYSTEM))
        self.assertEqual(request["messages"],
                         [{"role": "user", "content": question_text(question)}])
        search, tool = request["tools"]
        self.assertEqual(search, {"type": "web_search_20250305", "name": "web_search",
                                  "max_uses": 3, "blocked_domains": list(BLOCKED_DOMAINS)})
        self.assertEqual((tool["name"], tool["strict"], tool["input_schema"]),
                         ("report_findings", True, FINDINGS_SCHEMA))
        self.assertEqual(request["tool_choice"], {"type": "auto"})
        self.assertEqual(request["extra_body"], {"output_config": {"effort": "low"}})
        self.assertAlmostEqual(request["timeout"], 60, delta=1)
        # A short job still never gets a read limit under 30 s (OpenRouter is silent first).
        client = Client(response(report()))
        ApiBackend(client).run(question, RESEARCH_SYSTEM, 10)
        self.assertEqual(client.requests[0]["timeout"], 30.0)

    def test_the_findings_schema_is_strict(self) -> None:
        def check(schema: dict) -> None:
            if schema.get("type") == "object":
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
                for child in schema["properties"].values():
                    check(child)
            elif schema.get("type") == "array":
                check(schema["items"])
        check(FINDINGS_SCHEMA)
        item = FINDINGS_SCHEMA["properties"]["findings"]["items"]["properties"]
        self.assertEqual(item["kind"]["enum"], ["oe", "aftermarket", "unknown"])

    def test_a_paused_turn_is_sent_back_unchanged_and_continued(self) -> None:
        paused = response(*searched(), stop="pause_turn", searches=1)
        client = Client(paused, response(report(), searches=1))
        found, usage = ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        first, second = client.requests
        self.assertEqual(len(first["messages"]), 1)  # the first request kept its own list
        self.assertEqual(second["messages"][0], first["messages"][0])
        self.assertEqual(second["messages"][1]["role"], "assistant")
        self.assertIs(second["messages"][1]["content"], paused.content)
        self.assertEqual(second["tools"], first["tools"])
        self.assertEqual(usage["web_search_requests"], 2)
        self.assertEqual(found["findings"][0]["number"], "04465-02220")

    def test_at_most_two_continuations(self) -> None:
        client = Client(*(response(*searched(), stop="pause_turn") for _ in range(3)))
        with self.assertRaises(TimeoutError):
            ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 600)
        self.assertEqual(len(client.requests), 3)

    def test_no_continuation_with_less_than_thirty_seconds_left(self) -> None:
        client = Client(response(*searched(), stop="pause_turn"))
        with self.assertRaises(TimeoutError):
            ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 20)
        self.assertEqual(len(client.requests), 1)

    def test_json_in_the_text_is_read_when_the_tool_is_not_called(self) -> None:
        text = "Found these:\n```json\n" + json.dumps(raw(finding())) + "\n```"
        client = Client(response(block("text", text=text), stop="end_turn"))
        found, _ = ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        self.assertEqual(found["findings"][0]["number"], "04465-02220")

    def test_neither_report_nor_json_is_an_empty_answer_with_a_note(self) -> None:
        client = Client(response(block("text", text="I found {nothing} useful."),
                                 stop="end_turn"))
        found, _ = ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        self.assertEqual((found["findings"], found["answer"]), ([], ""))
        self.assertTrue(found["note"])

    def test_a_refusal_or_a_cut_off_answer_is_a_failure(self) -> None:
        for stop in ("refusal", "max_tokens"):
            with self.subTest(stop=stop):
                client = Client(response(block("text", text="Partial"), stop=stop))
                with self.assertRaises(ResearchError):
                    ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 60)

    def test_the_sdk_sends_the_paused_content_back_exactly_as_received(self) -> None:
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "server_tool_use": {"web_search_requests": 1}}
        paused = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                 "input": {"query": "corolla 2015 front pads"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                    {"type": "web_search_result", "url": "https://www.toyotapartsdeal.com/a",
                     "title": "Pads", "encrypted_content": "opaque", "page_age": None}]},
                {"type": "text", "text": "Checking the catalogue.", "citations": None}],
            "stop_reason": "pause_turn", "stop_sequence": None, "usage": usage}
        done = {**paused, "id": "msg_2", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "report_findings",
             "input": raw(finding())}]}
        sent: list[dict] = []

        def answer(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=(paused, done)[len(sent) - 1])

        client = anthropic.Anthropic(
            api_key="fixture-key", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(answer)))
        self.addCleanup(client.close)
        found, totals = ApiBackend(client).run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        first, second = sent
        self.assertEqual(second["messages"], [first["messages"][0],
                                              {"role": "assistant",
                                               "content": paused["content"]}])
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertEqual(first["tools"][0]["blocked_domains"], list(BLOCKED_DOMAINS))
        self.assertIs(first["tools"][1]["strict"], True)
        self.assertNotIn("timeout", first)
        self.assertEqual(totals["web_search_requests"], 2)
        self.assertEqual(found["findings"][0]["number"], "04465-02220")


class OpenRouterBackendTests(unittest.TestCase):
    def test_its_own_search_tool_and_anthropic_as_the_only_provider(self) -> None:
        made: list[Client] = []

        def factory() -> Client:
            made.append(Client(response(report()), response(report())))
            return made[-1]

        backend = OpenRouterBackend(factory)
        backend.run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        backend.run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        self.assertEqual(len(made), 1)  # made when first needed, then kept
        request = made[0].requests[0]
        self.assertEqual(request["model"], "anthropic/claude-haiku-5.5")
        self.assertEqual(request["tools"][0], {
            "type": "openrouter:web_search",
            "parameters": {"max_uses": 3, "excluded_domains": list(BLOCKED_DOMAINS)}})
        self.assertEqual(request["tools"][1]["name"], "report_findings")
        self.assertEqual(request["extra_body"], {
            "output_config": {"effort": "low"},
            "provider": {"only": ["anthropic"], "allow_fallbacks": False}})

    def test_without_a_client_factory_it_is_disabled(self) -> None:
        with self.assertRaises(ResearchError):
            OpenRouterBackend().run(clean_question(CAR), RESEARCH_SYSTEM, 60)
        with self.assertRaises(ResearchError):
            OpenRouterBackend(lambda: None).run(clean_question(CAR), RESEARCH_SYSTEM, 60)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "cache.sqlite3"

    def held(self, *answers, **options) -> Backend:
        backend = Backend(*answers, held=True, **options)
        self.addCleanup(backend.release.set)
        return backend

    def test_a_cached_answer_is_served_without_the_backend(self) -> None:
        first = ResearchService(Backend(), cache_path=self.path, wait_seconds=5)
        answered = first.ask(CAR)
        self.assertEqual((answered["status"], answered["cached"]), ("answered", False))
        backend = Backend(AssertionError("must not run"))
        result = ResearchService(backend, cache_path=self.path).ask(
            {**CAR, "make": "TOYOTA", "part": "Front Brake Pads"})
        self.assertEqual((result["status"], result["cached"]), ("answered", True))
        self.assertEqual(numbers_of(result), ["04465-02220"])
        self.assertEqual(result["searches"], 2)
        self.assertEqual(backend.calls, [])

    def test_answers_are_kept_thirty_days_and_empty_searches_one(self) -> None:
        service = ResearchService(Backend(raw(finding()), raw()), cache_path=self.path,
                                  wait_seconds=5)
        self.assertEqual(service.ask(CAR)["status"], "answered")
        empty = service.ask({**CAR, "part": "rear brake pads"})
        self.assertEqual(empty["status"], "no_findings")
        self.assertTrue(empty["note"])
        with closing(sqlite3.connect(self.path)) as db:
            left = sorted(expires - time.time() for (expires,) in db.execute(
                "SELECT expires_at FROM source_cache WHERE namespace = 'research.assistant'"))
        self.assertEqual(len(left), 2)
        self.assertAlmostEqual(left[0], 24 * 3600, delta=120)
        self.assertAlmostEqual(left[1], 30 * 24 * 3600, delta=120)

    def test_a_slow_job_is_pending_and_then_fills_the_cache(self) -> None:
        backend = self.held()
        service = ResearchService(backend, cache_path=self.path, wait_seconds=0.05)
        first = service.ask(CAR)
        self.assertEqual((first["status"], first["note"], first["findings"]),
                         ("pending", PENDING, []))
        self.assertTrue(backend.started.wait(5))
        backend.release.set()
        self.assertEqual(service.ask(CAR, wait=5)["status"], "answered")
        later = Backend(AssertionError("must not run"))
        cached = ResearchService(later, cache_path=self.path).ask(CAR)
        self.assertEqual((cached["status"], cached["cached"]), ("answered", True))
        self.assertEqual((len(backend.calls), later.calls), (1, []))

    def test_identical_questions_asked_together_run_once(self) -> None:
        backend = self.held()
        service = ResearchService(backend, cache_path=self.path, wait_seconds=10)
        results: list[dict] = []
        asks = [CAR, {**CAR, "make": "TOYOTA"}, {**CAR, "part": "FRONT BRAKE PADS"}] * 2
        threads = [threading.Thread(target=lambda args=args: results.append(service.ask(args)))
                   for args in asks]
        for thread in threads:
            thread.start()
        self.assertTrue(backend.started.wait(5))
        backend.release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual([result["status"] for result in results], ["answered"] * 6)
        self.assertEqual(len(backend.calls), 1)

    def test_failures_are_not_cached(self) -> None:
        limit = anthropic.RateLimitError("slow down", body=None,
                                         response=httpx.Response(429, request=REQUEST))
        backend = Backend(RuntimeError("down"), assistant_bench.SubscriptionLimited("limit"),
                          limit, raw(finding()))
        service = ResearchService(backend, cache_path=self.path, wait_seconds=5)
        statuses = [service.ask(CAR)["status"] for _ in range(5)]
        self.assertEqual(statuses, ["unavailable", "limited", "limited", "answered", "answered"])
        self.assertEqual(len(backend.calls), 4)
        failed = ResearchService(Backend(RuntimeError("down")), cache_path=None,
                                 wait_seconds=5).ask(CAR)
        self.assertEqual((failed["findings"], failed["searches"]), ([], 0))
        self.assertTrue(failed["note"])

    def test_a_question_without_part_numbers_or_question_is_unusable(self) -> None:
        backend = Backend()
        service = ResearchService(backend, cache_path=None)
        result = service.ask({**CAR, "part": "", "question": f"{VIN}", "numbers": ["+995555"]})
        self.assertEqual(result["status"], "unusable")
        self.assertTrue(result["note"])
        self.assertEqual(service.ask("not a dict")["status"], "unusable")
        self.assertEqual(backend.calls, [])

    def test_a_refused_search_tool_is_tried_once_on_the_fallback(self) -> None:
        refused = bad_request("tools.0: Input tag 'web_search_20250305' found using 'type' "
                              "does not match any of the expected tags")
        primary, fallback = Backend(refused, name="api"), Backend(name="openrouter")
        service = ResearchService(primary, cache_path=None, wait_seconds=5, fallback=fallback)
        with self.assertLogs(LOGGER_NAME, "INFO") as logs:
            result = service.ask(CAR)
        self.assertEqual(result["status"], "answered")
        self.assertEqual((len(primary.calls), len(fallback.calls)), (1, 1))
        self.assertLessEqual(fallback.calls[0][2], 60)
        self.assertIn("backend=api,openrouter", logs.output[-1])

    def test_other_refusals_never_reach_the_fallback_and_are_not_cached(self) -> None:
        for message in ("tools.1.custom.strict: Extra inputs are not permitted",
                        "output_config: Extra inputs are not permitted",
                        "Your credit balance is too low to access the Anthropic API."):
            with self.subTest(message=message):
                primary = Backend(bad_request(message), name="api")
                fallback = Backend(name="openrouter")
                service = ResearchService(primary, cache_path=self.path, wait_seconds=5,
                                          fallback=fallback)
                self.assertEqual(service.ask(CAR)["status"], "unavailable")
                self.assertEqual(service.ask(CAR)["status"], "unavailable")
                self.assertEqual((len(primary.calls), fallback.calls), (2, []))

    def test_the_fallback_is_tried_once_even_when_it_is_refused_too(self) -> None:
        refused = bad_request("web search is not enabled for this organization")
        primary, fallback = Backend(refused, name="api"), Backend(refused, name="openrouter")
        service = ResearchService(primary, cache_path=None, wait_seconds=5, fallback=fallback)
        self.assertEqual(service.ask(CAR)["status"], "unavailable")
        self.assertEqual((len(primary.calls), len(fallback.calls)), (1, 1))
        # Without a fallback the same refusal is simply a failure.
        alone = ResearchService(Backend(refused), cache_path=None, wait_seconds=5)
        self.assertEqual(alone.ask(CAR)["status"], "unavailable")

    def test_one_log_line_per_job_without_the_question(self) -> None:
        service = ResearchService(Backend(), cache_path=None, wait_seconds=5)
        with self.assertLogs(LOGGER_NAME, "INFO") as logs:
            service.ask({**CAR, "question": "private words of the customer"})
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r"assistant_research status=answered seconds=\d+\.\d "
                                         r"searches=2 key=[0-9a-f]{12} findings=1 "
                                         r"backend=fake stored=yes$")
        for word in ("Corolla", "private", "brake"):
            self.assertNotIn(word, logs.output[0])

    def test_an_answer_counts_only_for_a_question(self) -> None:
        words = raw(answer="I could not find a number for this car.")
        service = ResearchService(Backend(words, raw(answer="0W-20, 4.2 litres")),
                                  cache_path=self.path, wait_seconds=5)
        numbers = service.ask(CAR)
        self.assertEqual((numbers["status"], numbers["answer"]), ("no_findings", ""))
        fact = service.ask({**CAR, "kind": "fact", "part": "",
                            "question": "Which engine oil and how much?"})
        self.assertEqual((fact["status"], fact["answer"]), ("answered", "0W-20, 4.2 litres"))

    def test_a_cut_that_leaves_a_vin_shaped_token_is_masked_and_still_cached(self) -> None:
        # Cut at 100 characters, the 20-letter token is left exactly 17 long at the end.
        fit = "w" * 82 + " " + "ABCDEFGHJKLMNPRSTUVW"
        service = ResearchService(Backend(raw(finding(fits=[fit]))), cache_path=self.path,
                                  wait_seconds=5)
        result = service.ask(CAR)
        self.assertIsNone(VIN_IN_TEXT.search(json.dumps(result)))
        self.assertTrue(result["findings"][0]["fits"][0].endswith("[VIN]"))
        later = ResearchService(Backend(AssertionError("must not run")), cache_path=self.path)
        self.assertIs(later.ask(CAR)["cached"], True)

    def test_after_close_no_job_starts_but_the_cache_still_answers(self) -> None:
        backend = Backend()
        service = ResearchService(backend, cache_path=None, wait_seconds=5)
        self.assertEqual(service.ask(CAR)["status"], "answered")
        service.close()
        self.assertIs(service.ask(CAR)["cached"], True)
        self.assertEqual(service.ask({**CAR, "part": "oil filter"})["status"], "unavailable")
        self.assertEqual(len(backend.calls), 1)


# The fake `claude`: records what it was given beside itself (its environment is the bench's
# whitelist, so nothing can be handed to it), then prints Claude Code's JSON result line.
FAKE = textwrap.dedent('''
    #!PYTHON
    import json, os, sys
    record = {"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(),
              "stdin": sys.stdin.read()}
    with open(RECORD, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    if MODE == "limit":
        print(json.dumps({"type": "result", "is_error": True,
                          "result": "Claude usage limit reached"}))
        sys.exit(1)
    if MODE == "broken":
        print("no json here")
        sys.exit(2)
    findings = json.loads(FINDINGS)
    print("a line before the result")
    if MODE == "text":
        print(json.dumps({"type": "result", "is_error": False,
                          "result": "Found: " + json.dumps(findings) + " after 429 pages"}))
    else:
        print(json.dumps({"type": "result", "is_error": False, "result": "",
                          "structured_output": findings,
                          "usage": {"server_tool_use": {"web_search_requests": 2}}}))
''')


class BenchBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.record = self.folder / "record.json"

    def backend(self, mode: str = "ok") -> BenchBackend:
        binary = self.folder / f"claude-{mode}"
        binary.write_text(FAKE.lstrip()
                          .replace("#!PYTHON", f"#!{sys.executable}")
                          .replace("RECORD", repr(str(self.record)))
                          .replace("MODE", repr(mode))
                          .replace("FINDINGS", repr(json.dumps(raw(finding())))),
                          encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        return BenchBackend(binary=str(binary), work_dir=self.folder / "work")

    def test_the_command_the_environment_and_the_answer(self) -> None:
        question = clean_question({**CAR, "part": f"front brake pads {VIN}"})
        keys = {"ANTHROPIC_API_KEY": "fixture-anthropic", "OPENROUTER_API_KEY": "fixture-or",
                "ANTHROPIC_AUTH_TOKEN": "fixture-token"}
        with patch.dict(os.environ, keys):
            found, usage = self.backend().run(question, RESEARCH_SYSTEM, 30)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        argv = record["argv"]

        def value(flag: str) -> str:
            return argv[argv.index(flag) + 1]

        self.assertEqual(argv[0], "-p")
        self.assertEqual((value("--output-format"), value("--model"), value("--effort")),
                         ("json", "claude-haiku-5-5", "low"))
        self.assertEqual((value("--tools"), value("--allowedTools")),
                         ("WebSearch,WebFetch", "WebSearch WebFetch"))
        self.assertEqual(json.loads(value("--settings")), {"permissions": {"deny": [
            f"WebFetch(domain:{domain})" for domain in BLOCKED_DOMAINS]}})
        self.assertEqual(json.loads(value("--json-schema")), FINDINGS_SCHEMA)
        self.assertEqual((value("--permission-mode"), value("--max-turns")), ("dontAsk", "10"))
        for flag in ("--restricted", "--no-session-persistence"):
            self.assertIn(flag, argv)
        prompt = Path(value("--system-prompt-file"))
        self.assertEqual(prompt.parent, self.folder / "work" / "prompts")
        self.assertRegex(prompt.name, r"^research-[0-9a-f]{20}\.txt$")
        self.assertEqual(prompt.read_text(encoding="utf-8"), RESEARCH_SYSTEM)
        self.assertEqual(Path(record["cwd"]).resolve(),
                         (self.folder / "work" / "cwd").resolve())
        self.assertEqual(record["stdin"], question_text(question))
        self.assertNotIn(VIN.casefold(), record["stdin"].casefold())
        for key in keys:
            self.assertNotIn(key, record["env"])
        self.assertEqual(found["findings"][0]["number"], "04465-02220")
        self.assertEqual(usage["web_search_requests"], 2)

    def test_a_usage_limit_is_the_subscriptions_limit(self) -> None:
        backend = self.backend("limit")
        with self.assertRaises(assistant_bench.SubscriptionLimited):
            backend.run(clean_question(CAR), RESEARCH_SYSTEM, 30)
        service = ResearchService(backend, cache_path=None, wait_seconds=30)
        self.assertEqual(service.ask(CAR)["status"], "limited")

    def test_a_text_answer_is_read_and_a_429_in_it_is_no_limit(self) -> None:
        found, usage = self.backend("text").run(clean_question(CAR), RESEARCH_SYSTEM, 30)
        self.assertEqual(found["findings"][0]["number"], "04465-02220")
        self.assertEqual(usage["web_search_requests"], 0)

    def test_a_broken_run_is_a_failure(self) -> None:
        with self.assertRaises(ResearchError):
            self.backend("broken").run(clean_question(CAR), RESEARCH_SYSTEM, 30)


if __name__ == "__main__":
    unittest.main()
