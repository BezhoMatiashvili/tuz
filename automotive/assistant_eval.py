"""Evaluation of the assistant engine on the real model and catalogue.

Scripted conversations (``tests/fixtures/assistant/conversations.json``) go through the same
``ConversationStore`` the chats use, with the configured database, sources and model. Each
turn is checked against outcome expectations: rows that should and must not be offered (by
brand number), whether the reply asks and with how many answer buttons, questions about
things already known, the car label, the reply language, model requests and seconds.

Three checks hold for every turn without being asked for: the turn finished, a reply that
asks anything carries two to eight answer buttons, and no reply is a dead end.

The catalogue-wide check asks a wider question: does the engine find what the shop stocks?
Rows in stock are sampled across the ways the catalogue spells its makes and names its
parts (found by counting, never from a list). A writer model that is not the engine phrases
what a customer who needs exactly that row would type and answers the engine's questions
from the row; the engine must show the row, or a row sharing two or more numbers with it,
within three turns. A stronger model then reads a sample of the turns that showed cards
and marks every offered row, and that sample is exported for a parts person.

Model calls cost money. Every run appends its cost to a ledger file and no run starts, or
goes on, once the ledger total reaches the budget. Scenarios that need a VIN name a
placeholder (``{VIN:name}``) that is filled from a private ``vins.csv``. A name the file
does not hold gets a stand-in: a VIN of no car, decoded by the run itself to the car the
scenario file writes for that name, so the VIN path is tested without a customer's VIN; a
name with neither is skipped and counted. A VIN is never printed or written.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import math
import os
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from automotive.assistant_catalog import CatalogIndex, first_word, opening_of
from automotive.assistant_names import (
    MODEL_SECONDS,
    READER_SYSTEM,
    ROW_READING,
    Asker,
    object_schema,
    read_names,
)
from automotive.assistant_tools import SEARCH_ROWS, Toolbox, ToolError, row_numbers

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCENARIOS = ROOT / "tests" / "fixtures" / "assistant" / "conversations.json"
DEFAULT_LEDGER = ROOT / "data" / "assistant_eval" / "cost_ledger.jsonl"
DEFAULT_VINS = ROOT / "vins.csv"
DEFAULT_BUDGET = 40.0
# USD per million tokens: input, output, cache read, cache write kept five minutes. A write
# kept for an hour costs twice the input price.
PRICES = {"haiku": (0.10, 0.50, 0.01, 0.125), "sonnet": (2.0, 10.0, 0.20, 2.50),
          "opus": (4.0, 20.0, 0.20, 5.0)}
HOUR_WRITE = 2.0
# What a request that never answered is assumed to have cost: the cached prompt read once
# and a full reply written.
LOST_REQUEST = {"cache_read_input_tokens": 33_000, "output_tokens": 3_000}
# Opening the application has the provider read the fixed prompt once: priced as written.
WARM_REQUEST = {"cache_creation_input_tokens": 33_000}
# The last key is the part of cache_creation_input_tokens that was written for an hour.
USAGE_KEYS = ("model_requests", "tool_executions", "input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens",
              "cache_creation_1h_input_tokens")
PLACEHOLDER = re.compile(r"\{VIN:([A-Za-z0-9_]+)\}")
VIN = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])", re.IGNORECASE)
PIECES = re.compile(r"[()\[\]{}/,;\s]+")
SENTENCE = re.compile(r"[^.!?…\n]*[.!?…\n]?")
TURN_KEYS = {"say", "tap", "kind", "expect", "note"}
ROW_CHECKS = ("offers_any", "offers_all", "offers_none", "offers_only", "soft_offers",
              "soft_none")
EXPECTATIONS = {*ROW_CHECKS, "asks", "options_min", "no_question_about", "car_label_has",
                "car_label_empty", "max_requests", "max_seconds", "reply_language",
                "outcome_in", "answer_has", "answer_lacks", "cards_min", "cards_max",
                "groups_min", "searched", "not_same_question", "either"}
KINDS = ("part", "quick")
# The speed the owner asked for, in seconds, per kind of turn.
TARGETS = {"part": {"median": 6.0, "p90": 12.0}, "quick": {"median": 3.0, "p90": 3.0}}
FAILED_STATUSES = {"TIMEOUT", "PROVIDER_UNAVAILABLE", "PROVIDER_RATE_LIMITED",
                   "LIMIT_EXHAUSTED"}
# The provider's account refuses every request (no working key, no access, no credit).
# Such a turn is billed nothing, and running on would only fail the same way: the run stops.
REFUSED_STATUSES = {"AUTHENTICATION_ERROR", "ACCESS_DENIED", "PROVIDER_BILLING"}
MIN_OPTIONS, MAX_OPTIONS = 2, 8


class ScenarioError(ValueError):
    """The scenario file is not usable; the message says where."""


# -- money -----------------------------------------------------------------------------------


def cost(model: str, usage: dict | None) -> float:
    """What one turn's token counts cost in USD. An unknown model is priced as the dearest."""
    name = str(model or "").casefold()
    rates = next((value for key, value in PRICES.items() if key in name), PRICES["opus"])
    usage = usage or {}
    written = float(usage.get("cache_creation_input_tokens") or 0)
    hour = min(written, float(usage.get("cache_creation_1h_input_tokens") or 0))
    counts = (usage.get("input_tokens"), usage.get("output_tokens"),
              usage.get("cache_read_input_tokens"), written - hour)
    return (sum(float(count or 0) * rate for count, rate in zip(counts, rates, strict=True))
            + hour * HOUR_WRITE * rates[0]) / 1e6


def warm_request(settings: Any) -> dict:
    """What opening the application writes into the provider's cache, as usage: the fixed
    prompt once, for as long as the settings keep it."""
    hour = getattr(settings, "assistant_cache_ttl", "") == "1h"
    return {**WARM_REQUEST,
            "cache_creation_1h_input_tokens": WARM_REQUEST["cache_creation_input_tokens"]
            if hour else 0}


def opening_cost(service: Any, model: str) -> float:
    """What opening the application cost: the request that has the provider read the fixed
    prompt, as the provider counted it (a prompt it still held is read, not written). As
    assumed when the request left no count."""
    from automotive.assistant import warm_usage

    usage = warm_usage(service, model)
    counted = usage is not None and any(usage.values())
    return cost(model, usage if counted else warm_request(service.settings))


class Ledger:
    """One JSON object per run with ``usd``; shared by every tool that spends on the model."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def total(self) -> float:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0.0
        spent = 0.0
        for line in lines:
            try:
                spent += float(json.loads(line).get("usd") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
        return spent

    def append(self, model: str, what: str, usd: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model,
                                     "what": what, "usd": round(usd, 4)}) + "\n")


# -- scenarios -------------------------------------------------------------------------------


def load_scenarios(path: Path | str = DEFAULT_SCENARIOS) -> dict:
    """The scenario file as ``{"topics": {...}, "scenarios": [...]}``, checked for shape."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise ScenarioError("The file needs an object with a `scenarios` list.")
    topics = data.get("topics") or {}
    for name, patterns in topics.items():
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ScenarioError(f"Topic {name!r}: {pattern!r} is not a pattern ({exc}).") \
                    from None
    seen: set[str] = set()
    for scenario in data["scenarios"]:
        name = scenario.get("id") if isinstance(scenario, dict) else None
        if not isinstance(name, str) or not name or name in seen:
            raise ScenarioError(f"Scenario id {name!r} is missing or used twice.")
        seen.add(name)
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ScenarioError(f"{name}: `turns` must be a list with at least one turn.")
        for number, turn in enumerate(turns, 1):
            where = f"{name} turn {number}"
            if not isinstance(turn, dict) or not isinstance(turn.get("say"), str) \
                    or not turn["say"].strip():
                raise ScenarioError(f"{where}: `say` must be the customer's message.")
            if set(turn) - TURN_KEYS:
                raise ScenarioError(f"{where}: unknown keys {sorted(set(turn) - TURN_KEYS)}.")
            if turn.get("kind") not in (None, *KINDS):
                raise ScenarioError(f"{where}: kind must be one of {KINDS}.")
            _check_expectation(turn.get("expect") or {}, topics, where)
    cars = data.get("stand_in_cars") or {}
    if not isinstance(cars, dict) or not all(
            isinstance(car, dict) and car.get("make") and car.get("model")
            and isinstance(car.get("model_year"), int) for car in cars.values()):
        raise ScenarioError("`stand_in_cars` needs make, model and model_year for each name.")
    return {"topics": topics, "scenarios": data["scenarios"], "cars": cars}


def _check_expectation(expect: Any, topics: dict, where: str) -> None:
    if not isinstance(expect, dict):
        raise ScenarioError(f"{where}: `expect` must be an object.")
    unknown = set(expect) - EXPECTATIONS
    if unknown:
        raise ScenarioError(f"{where}: unknown expectations {sorted(unknown)}.")
    for topic in expect.get("no_question_about") or []:
        if topic not in topics:
            raise ScenarioError(f"{where}: topic {topic!r} is not defined in `topics`.")
    for key in ("answer_has", "answer_lacks"):
        for pattern in expect.get(key) or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ScenarioError(f"{where}: {pattern!r} is not a pattern ({exc}).") from None
    for alternative in expect.get("either") or []:
        if "either" in alternative:
            raise ScenarioError(f"{where}: `either` cannot be nested.")
        _check_expectation(alternative, topics, where)


def select(scenarios: list[dict], only: str | None) -> list[dict]:
    """Scenarios whose id or one of whose tags is in the comma-separated ``only``."""
    wanted = {item.strip() for item in (only or "").split(",") if item.strip()}
    if not wanted:
        return list(scenarios)
    return [scenario for scenario in scenarios
            if scenario["id"] in wanted or wanted & set(scenario.get("tags") or [])]


def read_vins(path: Path | str = DEFAULT_VINS) -> dict[str, str]:
    """``name,VIN`` lines of the private file; a missing file is no VINs at all."""
    found: dict[str, str] = {}
    try:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                cells = [cell.strip() for cell in row]
                if len(cells) >= 2 and VIN.fullmatch(cells[1]):
                    found[cells[0].casefold()] = cells[1].upper()
    except OSError:
        return {}
    return found


def fill(text: str, vins: dict[str, str]) -> str | None:
    """The message with its VIN placeholders filled, or None when one has no VIN."""
    missing = [name for name in PLACEHOLDER.findall(text) if name.casefold() not in vins]
    if missing:
        return None
    return PLACEHOLDER.sub(lambda match: vins[match.group(1).casefold()], text)


def needed_vins(scenario: dict) -> list[str]:
    return list(dict.fromkeys(name for turn in scenario["turns"]
                              for name in PLACEHOLDER.findall(turn["say"])))


# -- stand-in VINs -----------------------------------------------------------------------------

VIN_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # a VIN has no I, O or Q
# What a typed car may tell the outside sources.
TYPED_CAR = ("make", "model", "model_year", "trim", "engine_code", "displacement_l", "fuel",
             "body")


def stand_in_vin(name: str) -> str:
    """A VIN that belongs to no car, settled by the placeholder's name. No maker's code
    begins with 0, so it cannot be a customer's."""
    digest = hashlib.sha256(f"stand-in VIN {name.casefold()}".encode()).digest()
    return "0" + "".join(VIN_LETTERS[byte % len(VIN_LETTERS)] for byte in digest[:16])


def stand_ins(cars: dict, scenarios: list[dict], vins: dict[str, str]) -> dict[str, dict]:
    """Give every placeholder that has no VIN but a stand-in car its stand-in VIN (added to
    ``vins``). Returns the cars by stand-in VIN, for the run's decoder."""
    by_name = {str(name).casefold(): car for name, car in (cars or {}).items()}
    decoded: dict[str, dict] = {}
    for scenario in scenarios:
        for name in needed_vins(scenario):
            key = name.casefold()
            if key not in vins and key in by_name:
                vins[key] = stand_in_vin(key)
                decoded[vins[key]] = dict(by_name[key])
    return decoded


class StandInDecoder:
    """The VIN decoder of an evaluation run: a stand-in VIN decodes to its car, every
    other VIN goes to the real decoder."""

    def __init__(self, decoder: Any, cars: dict[str, dict]) -> None:
        self.decoder, self.cars = decoder, cars

    def decode(self, vin: str, model_year: int | None = None, *,
               deadline: float | None = None) -> dict:
        car = self.cars.get(str(vin).strip().upper())
        if car is None:
            return self.decoder.decode(vin, model_year, deadline=deadline)
        return {"status": "PROVISIONAL", "vehicle": dict(car), "warnings": [],
                "provider": "stand-in", "cached": True}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.decoder, name)


class StandInSearch:
    """The source federation of an evaluation run. A stand-in VIN never leaves this
    process: its car is put to the outside sources as a typed car."""

    def __init__(self, search: Any, cars: dict[str, dict]) -> None:
        self.search, self.cars = search, cars

    def prewarm(self, vin: str) -> Any:
        if vin in self.cars:
            return None
        return self.search.prewarm(vin)

    def start(self, vin: str | None, part_query: str = "", model_year: int | None = None,
              configuration: dict | None = None, **kwargs: Any) -> Any:
        car = self.cars.get(vin) if vin else None
        if car is None:
            return self.search.start(vin, part_query, model_year, configuration, **kwargs)
        typed = {key: car[key] for key in TYPED_CAR if car.get(key) not in (None, "")}
        typed.update(configuration or {})
        return self.search.start(None, part_query, typed.get("model_year"), typed, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.search, name)


# -- reading a reply -------------------------------------------------------------------------


def plain(value: Any) -> str:
    """Letters and digits only, upper case: how part numbers and brands are compared."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def parse_row(spec: str) -> tuple[str, str]:
    """``"SATO PN1521"`` is brand SATO, number PN1521; a single word is a number alone."""
    words = str(spec).split()
    if len(words) < 2:
        return "", plain(spec)
    return plain(" ".join(words[:-1])), plain(words[-1])


def code_keys(code: Any) -> set[str]:
    """The ways a row's own number may be named: whole, and each bracketed or slashed piece
    (``CL0311(CLKH65)`` is also CL0311)."""
    keys = {plain(code)}
    keys.update(plain(piece) for piece in PIECES.split(str(code or ""))
                if len(plain(piece)) >= 3)
    keys.discard("")
    return keys


def row_matches(brand: Any, code: Any, spec: str) -> bool:
    wanted_brand, wanted_number = parse_row(spec)
    if not wanted_number or wanted_number not in code_keys(code):
        return False
    have = plain(brand)
    return not wanted_brand or (bool(have) and (wanted_brand in have or have in wanted_brand))


def card_row(card: dict) -> dict:
    """What a card shows, cut to what a report needs."""
    product = card.get("product") if isinstance(card.get("product"), dict) else {}
    stock = product.get("stock") if isinstance(product.get("stock"), dict) else {}
    return {"brand": product.get("brand") or "", "number": product.get("original_code") or "",
            "name": product.get("product_name") or "",
            "vehicle": product.get("product_model") or "",
            "store": stock.get("store"), "warehouse": stock.get("tuz11"),
            "availability": card.get("availability_status"),
            "confidence": card.get("confidence"), "group": card.get("group") or "",
            "reason": card.get("reason") or "", "evidence": card.get("evidence") or ""}


def question_of(payload: dict) -> tuple[str, list[str]]:
    """The question text and the labels of its answer buttons."""
    question = payload.get("question") if isinstance(payload.get("question"), dict) else {}
    labels = [str(option.get("label") or "").strip()
              for option in question.get("options") or [] if isinstance(option, dict)]
    return str(question.get("text") or "").strip(), [label for label in labels if label]


def suggestions_of(payload: dict) -> list[str]:
    return [str(option.get("label") or "").strip()
            for option in payload.get("suggestions") or []
            if isinstance(option, dict) and str(option.get("label") or "").strip()]


def asks(payload: dict) -> bool:
    """Does the reply ask the customer anything: a question field, the outcome, or a
    question mark anywhere in the text."""
    text, _ = question_of(payload)
    return bool(text) or payload.get("outcome") == "asked" \
        or "?" in str(payload.get("answer") or "")


def asked_text(payload: dict) -> str:
    """Everything in the reply that is a question: the question field and each sentence of
    the text that ends with a question mark."""
    text, _ = question_of(payload)
    sentences = [match.group().strip() for match in SENTENCE.finditer(
        str(payload.get("answer") or "")) if match.group().strip().endswith("?")]
    return " ".join(dict.fromkeys([text, *sentences])).strip()


def visible_text(payload: dict) -> str:
    """Everything of a reply that the model wrote and the customer reads."""
    question, options = question_of(payload)
    parts = [payload.get("answer"), question, *options, *suggestions_of(payload),
             payload.get("car_label")]
    parts += [group.get("label") for group in payload.get("groups") or []
              if isinstance(group, dict)]
    parts += [card.get(key) for card in payload.get("cards") or [] if isinstance(card, dict)
              for key in ("reason", "evidence")]
    return "\n".join(str(part) for part in parts if isinstance(part, str))


def detect_language(text: str) -> str:
    """ka, ru or en by the script most of the letters are in; empty for no letters."""
    counts = {"ka": 0, "ru": 0, "en": 0}
    for char in str(text or ""):
        if "Ⴀ" <= char <= "ჿ" or "Ა" <= char <= "Ჿ":
            counts["ka"] += 1
        elif "Ѐ" <= char <= "ӿ":
            counts["ru"] += 1
        elif char.isascii() and char.isalpha():
            counts["en"] += 1
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] else ""


def searched_terms(payload: dict) -> set[str]:
    """Every term the model put into a catalogue search this turn, folded."""
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) \
        else {}
    found: set[str] = set()
    for call in diagnostics.get("tools") or []:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        for value in args.values():
            for term in value if isinstance(value, list) else [value]:
                if isinstance(term, str):
                    found.update(piece for piece in re.split(r"[\s,/]+", term.casefold())
                                 if piece)
    return found


# -- checking a turn -------------------------------------------------------------------------


def _row_sets(value: Any) -> list[list[str]]:
    """``offers_any`` as a list of alternative sets: a flat list is one set."""
    if not value:
        return []
    if all(isinstance(item, str) for item in value):
        return [list(value)]
    return [list(item) for item in value if isinstance(item, list) and item]


def check_expectation(expect: dict, payload: dict, *, seconds: float = 0.0,
                      stock: Any = None, topics: dict | None = None,
                      previous: dict | None = None) -> dict:
    """One expectation against one finished turn.

    ``stock(spec)`` says whether a row is in stock today: True, False, or None when the
    catalogue has no such row. A row that should be offered is only required while it is in
    stock. Returns failures (hard), notes (soft), the rows found and expected, the rows
    wrongly offered, and whether a known fact was asked for again.
    """
    stock = stock or (lambda spec: True)
    topics = topics or {}
    cards = [card_row(card) for card in payload.get("cards") or [] if isinstance(card, dict)]
    result: dict = {"failures": [], "notes": [], "found": 0, "expected": 0, "wrong": [],
                    "soft_wrong": [], "redundant": []}
    fail, note = result["failures"].append, result["notes"].append

    def offered(spec: str) -> bool:
        return any(row_matches(card["brand"], card["number"], spec) for card in cards)

    for specs in _row_sets(expect.get("offers_any")):
        stocked = [spec for spec in specs if stock(spec)]
        hits = [spec for spec in specs if offered(spec)]
        result["expected"] += len(stocked)
        result["found"] += sum(1 for spec in stocked if spec in hits)
        if hits:
            continue
        if stocked:
            fail("none offered of: " + ", ".join(stocked))
        else:
            note("not required, none in stock today: " + ", ".join(specs))
    for spec in expect.get("offers_all") or []:
        if stock(spec):
            result["expected"] += 1
            if offered(spec):
                result["found"] += 1
            else:
                fail(f"in stock but not offered: {spec}")
        elif not offered(spec):
            note(f"not required, not in stock today: {spec}")
    for spec in expect.get("soft_offers") or []:
        if stock(spec) and not offered(spec):
            note(f"soft: in stock but not offered: {spec}")
    for spec in expect.get("offers_none") or []:
        if offered(spec):
            result["wrong"].append(spec)
            fail(f"must-not row offered: {spec}")
    for spec in expect.get("soft_none") or []:
        if offered(spec):
            result["soft_wrong"].append(spec)
            note(f"soft: doubtful row offered: {spec}")
    allowed = expect.get("offers_only")
    if allowed is not None:
        for card in cards:
            if not any(row_matches(card["brand"], card["number"], spec) for spec in allowed):
                name = f"{card['brand']} {card['number']}".strip()
                result["wrong"].append(name)
                fail(f"row outside the allowed set offered: {name}")

    question, options = question_of(payload)
    asking = asks(payload)
    if expect.get("asks") is True and not asking:
        fail("expected a question, the reply asks nothing")
    if expect.get("asks") is False and asking:
        fail(f"expected no question, got: {asked_text(payload)[:120]}")
    if expect.get("options_min") is not None and len(options) < int(expect["options_min"]):
        fail(f"{len(options)} answer buttons, expected at least {expect['options_min']}")
    said = asked_text(payload)
    for topic in expect.get("no_question_about") or []:
        if any(re.search(pattern, said, re.IGNORECASE) for pattern in topics.get(topic, [])):
            result["redundant"].append(topic)
            fail(f"asks about {topic}, which is already known: {said[:120]}")
    if expect.get("not_same_question") and question and previous is not None \
            and question == question_of(previous)[0]:
        result["redundant"].append("same question")
        fail(f"the same question twice running: {question[:120]}")

    label = str(payload.get("car_label") or "").casefold()
    for piece in expect.get("car_label_has") or []:
        if str(piece).casefold() not in label:
            fail(f"car label {payload.get('car_label')!r} lacks {piece!r}")
    if expect.get("car_label_empty") and label.strip():
        fail(f"car label {payload.get('car_label')!r} names a car the customer did not")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if expect.get("max_requests") is not None \
            and int(usage.get("model_requests") or 0) > int(expect["max_requests"]):
        fail(f"{usage.get('model_requests')} model requests, at most "
             f"{expect['max_requests']} expected")
    if expect.get("max_seconds") is not None and seconds > float(expect["max_seconds"]):
        fail(f"{seconds:.1f} s, at most {expect['max_seconds']} s expected")
    answer = str(payload.get("answer") or "")
    if expect.get("reply_language") and detect_language(answer) != expect["reply_language"]:
        fail(f"reply is in {detect_language(answer) or 'no language'}, expected "
             f"{expect['reply_language']}")
    if expect.get("outcome_in") and payload.get("outcome") not in expect["outcome_in"]:
        fail(f"outcome {payload.get('outcome')!r}, expected one of {expect['outcome_in']}")
    for pattern in expect.get("answer_has") or []:
        if not re.search(pattern, answer, re.IGNORECASE):
            fail(f"reply lacks /{pattern}/")
    for pattern in expect.get("answer_lacks") or []:
        if re.search(pattern, answer, re.IGNORECASE):
            fail(f"reply contains /{pattern}/")
    if expect.get("cards_min") is not None and len(cards) < int(expect["cards_min"]):
        fail(f"{len(cards)} cards, at least {expect['cards_min']} expected")
    if expect.get("cards_max") is not None and len(cards) > int(expect["cards_max"]):
        fail(f"{len(cards)} cards, at most {expect['cards_max']} expected")
    if expect.get("groups_min") is not None \
            and len(payload.get("groups") or []) < int(expect["groups_min"]):
        fail(f"{len(payload.get('groups') or [])} groups, at least "
             f"{expect['groups_min']} expected")
    if expect.get("searched"):
        terms = searched_terms(payload)
        for wanted in expect["searched"]:
            if str(wanted).casefold() not in terms:
                fail(f"no catalogue search used the term {wanted!r}")
    return result


def check_turn(expect: dict | None, payload: dict, *, seconds: float = 0.0,
               stock: Any = None, topics: dict | None = None,
               previous: dict | None = None) -> dict:
    """The checks that hold for every turn, then the turn's own expectations. With
    ``either``, the alternative with the fewest failures counts."""
    expect = dict(expect or {})
    alternatives = expect.pop("either", None) or []
    result = check_expectation(expect, payload, seconds=seconds, stock=stock, topics=topics,
                               previous=previous)
    if alternatives:
        tried = [check_expectation(alternative, payload, seconds=seconds, stock=stock,
                                   topics=topics, previous=previous)
                 for alternative in alternatives]
        best = min(tried, key=lambda item: len(item["failures"]))
        if best["failures"]:
            result["failures"].append("no alternative held: " + " | ".join(
                "; ".join(item["failures"]) for item in tried)[:400])
        for key in ("notes", "wrong", "soft_wrong", "redundant"):
            result[key].extend(best[key])
        result["found"] += best["found"]
        result["expected"] += best["expected"]

    status = payload.get("status")
    engine = payload.get("engine_status") or status
    _, options = question_of(payload)
    asking = asks(payload)
    cards = payload.get("cards") or []
    result.update(asks=asking, failed=status != "COMPLETE",
                  timeout=engine == "TIMEOUT" or status == "RUNNING",
                  question_without_options=asking and not (
                      MIN_OPTIONS <= len(options) <= MAX_OPTIONS),
                  dead_end=not (cards or len(options) >= MIN_OPTIONS
                                or suggestions_of(payload)))
    universal = []
    if result["failed"]:
        universal.append(f"turn did not complete: {status}/{engine}")
    if not str(payload.get("answer") or "").strip():
        universal.append("empty reply")
    if result["question_without_options"]:
        universal.append(f"asks with {len(options)} answer buttons: "
                         f"{asked_text(payload)[:120]}")
    if result["dead_end"]:
        universal.append("dead end: no cards, no answer buttons, no next steps")
    if VIN.search(visible_text(payload)):
        universal.append("a VIN is in the reply")
    result["failures"] = universal + result["failures"]
    return result


# -- running ---------------------------------------------------------------------------------


def _tap(turn: dict, previous: dict | None) -> tuple[str, int | None]:
    """The message to send and the tapped option's number: the first answer button of the
    last reply whose label contains one of the turn's ``tap`` texts, else the typed text."""
    wanted = [str(text).casefold() for text in turn.get("tap") or [] if str(text).strip()]
    _, options = question_of(previous or {})
    for number, label in enumerate(options, 1):
        if any(text in label.casefold() for text in wanted):
            return label, number
    return turn["say"], None


def run_scenario(store: Any, scenario: dict, *, vins: dict[str, str], model: str,
                 stock: Any = None, topics: dict | None = None, poll: float = 0.05,
                 turn_timeout: float = 120.0, clock: Any = time.monotonic,
                 sleep: Any = time.sleep) -> dict:
    """One conversation from its first message to its last, every turn checked."""
    missing = [name for name in needed_vins(scenario) if name.casefold() not in vins]
    record: dict = {"id": scenario["id"], "tags": scenario.get("tags") or [], "turns": [],
                    "status": "passed", "cost_usd": 0.0}
    if missing:
        record.update(status="skipped", reason="no VIN for " + ", ".join(missing))
        return record
    conversation = store.create()["conversation_id"]
    previous: dict | None = None
    try:
        for turn in scenario["turns"]:
            typed, choice = _tap(turn, previous)
            message = fill(typed, vins) or typed
            started = clock()
            ticket = store.submit(conversation, message, language="auto",
                                  **({"choice": choice} if choice else {}))
            payload = store.get(conversation, ticket["turn_id"], 0, 100)
            while payload.get("status") == "RUNNING" and clock() - started < turn_timeout:
                sleep(poll)
                payload = store.get(conversation, ticket["turn_id"], 0, 100)
            seconds = clock() - started
            checked = check_turn(turn.get("expect"), payload, seconds=seconds, stock=stock,
                                 topics=topics, previous=previous)
            usage = {key: int((payload.get("usage") or {}).get(key) or 0)
                     for key in USAGE_KEYS}
            spent = cost(model, usage)
            if checked["timeout"] or payload.get("engine_status") in FAILED_STATUSES:
                spent += cost(model, LOST_REQUEST)
            diagnostics = payload.get("diagnostics") \
                if isinstance(payload.get("diagnostics"), dict) else {}
            question, options = question_of(payload)
            record["turns"].append({
                # The scenario's own text: a VIN stays a placeholder here.
                "say": typed, "tapped": choice, "kind": turn.get("kind"),
                "seconds": round(seconds, 2), "status": payload.get("status"),
                "engine_status": payload.get("engine_status"),
                "outcome": payload.get("outcome"), "car_label": payload.get("car_label"),
                "answer": VIN.sub("[VIN]", str(payload.get("answer") or "")),
                "question": question, "options": options,
                "suggestions": suggestions_of(payload),
                "groups": [group.get("label") for group in payload.get("groups") or []
                           if isinstance(group, dict)],
                "cards": [card_row(card) for card in payload.get("cards") or []
                          if isinstance(card, dict)],
                "usage": usage, "cost_usd": round(spent, 5), "warnings": payload.get("warnings"),
                "tools": diagnostics.get("tools") or [],
                "requests": diagnostics.get("requests") or [],
                "corrections": diagnostics.get("corrections") or [],
                "fallback": diagnostics.get("fallback"), "notes_kept": diagnostics.get("notes"),
                **checked})
            record["cost_usd"] += spent
            if checked["failures"]:
                record["status"] = "failed"
            previous = payload
    finally:
        with contextlib.suppress(Exception):  # It may have expired on its own.
            store.delete(conversation)
    record["cost_usd"] = round(record["cost_usd"], 5)
    return record


def turn_line(record: dict, number: int, turn: dict) -> str:
    """One line for one turn: what happened and what failed."""
    usage = turn["usage"]
    shown = ", ".join(f"{card['brand']} {card['number']}".strip()
                      for card in turn["cards"][:6])
    if len(turn["cards"]) > 6:
        shown += f" (+{len(turn['cards']) - 6})"
    what = f"{len(turn['cards'])} cards [{shown}]" if turn["cards"] else ""
    if turn["options"]:
        what += f" asks [{' | '.join(turn['options'])}]"
    elif turn["suggestions"]:
        what += f" next [{' | '.join(turn['suggestions'])}]"
    line = (f"{'FAIL' if turn['failures'] else 'ok  '} {record['id']} t{number} "
            f"{turn['seconds']:.1f}s req={usage['model_requests']} "
            f"out={usage['output_tokens']} ${turn['cost_usd']:.4f} {turn['outcome']}"
            f" {what.strip()}")
    for failure in turn["failures"]:
        line += f"\n       - {failure}"
    return line


def refused_status(record: dict) -> str:
    """The account status that ended one of a record's turns, or ''."""
    return next((str(turn.get("engine_status")) for turn in record.get("turns") or []
                 if turn.get("engine_status") in REFUSED_STATUSES), "")


def account_refusal(status: str) -> str:
    """Why the rest of a run was not started, in words for the report."""
    return (f"the provider's account refuses requests ({status}): "
            + ("add credit" if status == "PROVIDER_BILLING" else "check the API key"))


def evaluate(store: Any, scenarios: list[dict], *, vins: dict[str, str], model: str,
             out: TextIO, stock: Any = None, topics: dict | None = None,
             budget_left: float = float("inf"), parallel: int = 1,
             **options: Any) -> tuple[list[dict], float]:
    """Run the scenarios, at most ``parallel`` at a time, until all are done or the money
    is gone. Returns their records and what was spent."""
    lock = threading.Lock()
    spent = [0.0]
    refused: list[str] = []  # the account status that ended the run, once there is one

    def one(scenario: dict) -> dict:
        with lock:
            if refused:  # Said for each one: the report must not read as a run that passed.
                out.write(f"not_run {scenario['id']} ({account_refusal(refused[0])})\n")
            if refused or spent[0] >= budget_left:
                return {"id": scenario["id"], "tags": scenario.get("tags") or [],
                        "turns": [], "status": "not_run", "cost_usd": 0.0,
                        "reason": account_refusal(refused[0]) if refused
                        else "budget reached"}
        try:
            record = run_scenario(store, scenario, vins=vins, model=model, stock=stock,
                                  topics=topics, **options)
        except Exception as exc:  # noqa: BLE001 - one broken scenario must not end the run
            record = {"id": scenario["id"], "tags": scenario.get("tags") or [], "turns": [],
                      "status": "failed", "reason": f"runner error: {type(exc).__name__}",
                      "cost_usd": 0.0}
        with lock:
            spent[0] += record["cost_usd"]
            if refused_status(record) and not refused:
                refused.append(refused_status(record))
            if record["status"] in ("skipped", "not_run") or not record["turns"]:
                out.write(f"{record['status']:4s} {record['id']} "
                          f"({record.get('reason', '')})\n")
            for number, turn in enumerate(record["turns"], 1):
                out.write(turn_line(record, number, turn) + "\n")
            out.flush()
        return record

    if parallel <= 1:
        return [one(scenario) for scenario in scenarios], spent[0]
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="assistant-eval") as pool:
        return list(pool.map(one, scenarios)), spent[0]


# -- the summary -----------------------------------------------------------------------------


def _spread(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {"count": len(ordered), "median": round(statistics.median(ordered), 2),
            "p90": round(ordered[max(0, -(-len(ordered) * 9 // 10) - 1)], 2),
            "max": round(ordered[-1], 2)}


def summarize(results: list[dict]) -> dict:
    """Totals of one run: what passed, what was found and wrongly offered, how the
    questions were asked, and what the turns cost in seconds, requests, tokens and money."""
    turns = [turn for record in results for turn in record["turns"]]
    statuses = [record["status"] for record in results]
    seconds = {"all": _spread([turn["seconds"] for turn in turns])}
    for kind in KINDS:
        seconds[kind] = _spread([turn["seconds"] for turn in turns if turn["kind"] == kind])
    requests = [turn["usage"]["model_requests"] for turn in turns]
    tokens = {key: sum(turn["usage"][key] for turn in turns) for key in USAGE_KEYS[2:]}
    spent = sum(record["cost_usd"] for record in results)
    targets = {}
    for kind, limits in TARGETS.items():
        for measure, limit in limits.items():
            value = seconds[kind][measure]
            targets[f"{kind}_{measure}_le_{limit:g}s"] = None if value is None \
                else value <= limit
    targets["no_failed_turns"] = not any(turn["failed"] for turn in turns)
    return {
        "scenarios": {"total": len(results), **{name: statuses.count(name) for name in (
            "passed", "failed", "skipped", "not_run")}},
        "turns": len(turns),
        "parts_found": sum(turn["found"] for turn in turns),
        "parts_expected": sum(turn["expected"] for turn in turns),
        "wrong_rows_offered": sum(len(turn["wrong"]) for turn in turns),
        "doubtful_rows_offered": sum(len(turn["soft_wrong"]) for turn in turns),
        "questions": sum(1 for turn in turns if turn["asks"]),
        "questions_without_options": sum(1 for turn in turns
                                         if turn["question_without_options"]),
        "redundant_questions": sum(1 for turn in turns if turn["redundant"]),
        "dead_ends": sum(1 for turn in turns if turn["dead_end"]),
        "failed_turns": sum(1 for turn in turns if turn["failed"]),
        "timeouts": sum(1 for turn in turns if turn["timeout"]),
        "seconds": seconds,
        "requests": {"median": statistics.median(requests) if requests else None,
                     "mean": round(statistics.fmean(requests), 2) if requests else None,
                     "max": max(requests, default=None)},
        "tokens": {**tokens, "output_per_turn_median": statistics.median(
            [turn["usage"]["output_tokens"] for turn in turns]) if turns else None},
        "cost_usd": round(spent, 4),
        "cost_per_turn_usd": round(spent / len(turns), 5) if turns else None,
        "targets": targets,
        "failed": [record["id"] for record in results if record["status"] == "failed"],
        "skipped": [record["id"] for record in results if record["status"] == "skipped"],
    }


def print_summary(summary: dict, out: TextIO, model: str = "") -> None:
    counts, seconds = summary["scenarios"], summary["seconds"]

    def spread(name: str) -> str:
        item = seconds[name]
        if not item["count"]:
            return "no turns"
        return (f"median {item['median']} s, p90 {item['p90']} s, max {item['max']} s "
                f"({item['count']} turns)")

    ran = counts["total"] - counts["skipped"] - counts["not_run"]
    rows = [
        ("model", model),
        ("scenarios passed", f"{counts['passed']} of {ran} ({counts['skipped']} skipped, "
                             f"{counts['not_run']} not run)"),
        ("parts found", f"{summary['parts_found']} of {summary['parts_expected']} in stock"),
        ("wrong rows offered", f"{summary['wrong_rows_offered']} "
                               f"(doubtful: {summary['doubtful_rows_offered']})"),
        ("questions without options", f"{summary['questions_without_options']} of "
                                      f"{summary['questions']} questions"),
        ("redundant questions", summary["redundant_questions"]),
        ("dead ends", summary["dead_ends"]),
        ("failed turns", f"{summary['failed_turns']} ({summary['timeouts']} timeouts)"),
        ("seconds, all turns", spread("all")),
        ("seconds, car and part", spread("part")),
        ("seconds, quick turns", spread("quick")),
        ("model requests per turn", f"median {summary['requests']['median']}, mean "
                                    f"{summary['requests']['mean']}, max "
                                    f"{summary['requests']['max']}"),
        ("tokens", f"in {summary['tokens']['input_tokens']}, cache read "
                   f"{summary['tokens']['cache_read_input_tokens']}, cache write "
                   f"{summary['tokens']['cache_creation_input_tokens']}, out "
                   f"{summary['tokens']['output_tokens']} (median per turn "
                   f"{summary['tokens']['output_per_turn_median']})"),
        ("cost", f"${summary['cost_usd']:.4f} in all, ${summary['cost_per_turn_usd'] or 0:.5f}"
                 " per turn"),
        ("targets", ", ".join(f"{name}: {'n/a' if met is None else 'yes' if met else 'NO'}"
                              for name, met in summary["targets"].items())),
    ]
    width = max(len(name) for name, _ in rows)
    out.write("\n")
    for name, value in rows:
        out.write(f"{name:<{width}}  {value}\n")
    if summary["failed"]:
        out.write("failed: " + ", ".join(summary["failed"]) + "\n")
    out.flush()


# -- the catalogue-wide check: the sample ----------------------------------------------------

WRITER_MODEL = "claude-haiku-5-5"
JUDGE_MODEL = "claude-opus-5-5"
SAMPLE_SIZE = 150
JUDGED_TURNS = 60
MAX_TURNS = 3
SHARED_NUMBERS = 2  # a shown row sharing this many numbers with the wanted one is that part
OTHER_ROWS = 40  # rows the judge reads beside the offered ones
EMPTY_SHARE = 0.03  # of a sample: at least this share of rows has no vehicle text
TYPO_SHARE = 0.3
# How the requests are spread over the ways customers write, and the script of each.
WRITING = (("ka", 0.40), ("ka-latin", 0.25), ("ru", 0.20), ("en", 0.15))
SCRIPT = {"ka": "ka", "ka-latin": "", "ru": "ru", "en": ""}
# The quota shared by the spellings too rare to expect a pick of their own. No first word of
# a vehicle text and no opening of a name can hold a space inside brackets.
RARE = "(rare spellings)"
WORDS = re.compile(r"[^\W_]+")


def in_stock(row: Any) -> bool:
    return (row.store or 0) > 0 or (row.warehouse or 0) > 0


def _lot(seed: int, *labels: Any) -> int:
    """A number settled by the seed and the labels alone: the same on every machine."""
    text = "\x1f".join(str(label) for label in (seed, *labels))
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _quotas(counts: Counter, size: int, floors: dict[str, int] | None = None) -> dict[str, int]:
    """How many of ``size`` picks each stratum gets. A stratum that expects half a pick or
    more is frequent: it gets at least one pick and otherwise its share of the rows. The
    rest share one quota under ``RARE``. Nothing here knows what a stratum is called."""
    total = sum(counts.values())
    if not total or size <= 0:
        return {}
    floors = {key: value for key, value in (floors or {}).items() if counts.get(key)}
    strata = {key: count for key, count in counts.items()
              if 2 * count * size >= total or key in floors}
    least = {key: max(1, floors.get(key, 0)) for key in strata}
    if total > sum(strata.values()):
        strata[RARE] = total - sum(strata.values())
        least[RARE] = 0
    ideal = {key: count * size / total for key, count in strata.items()}
    quota = {key: max(least[key], int(ideal[key])) for key in strata}
    keys = sorted(strata)
    # Rounding is settled where it distorts least: on the stratum with the most picks.
    while sum(quota.values()) < size:
        quota[max(keys, key=lambda key: (ideal[key] - quota[key], quota[key], key))] += 1
    while sum(quota.values()) > size:
        above = [key for key in keys if quota[key] > least[key]]
        if not above:
            break  # More frequent strata than picks: the first picks decide who is in.
        quota[min(above, key=lambda key: (ideal[key] - quota[key], -quota[key], key))] -= 1
    return quota


def _styles(size: int) -> list[str]:
    """``size`` ways of writing in the shares of ``WRITING``."""
    counts = {name: int(share * size) for name, share in WRITING}
    left = sorted(WRITING, key=lambda pair: (int(pair[1] * size) - pair[1] * size, pair[0]))
    for name, _ in left[:size - sum(counts.values())]:
        counts[name] += 1
    return [name for name, _ in WRITING for _ in range(counts[name])]


def draw(rows: Any, size: int, seed: int) -> list[dict]:
    """A sample of the rows that are in stock, the same for the same seed and catalogue.

    Stratified twice, by how a vehicle text spells its make (its first word) and by what a
    name calls the part (its opening words), both found by counting the rows: every frequent
    spelling and every frequent part name is in the sample, each about as often as in the
    stock, the rare ones share a quota, and a small share of rows has no vehicle text. The
    seed also settles how each row's customer writes: the language and whether with typos.
    """
    pool = sorted((row for row in rows if in_stock(row)), key=lambda row: str(row.id))
    size = max(0, min(int(size), len(pool)))
    if not size:
        return []
    make_of = {str(row.id): first_word(row.vehicle) for row in pool}
    part_of = {str(row.id): opening_of(row.name) for row in pool}
    makes, parts = Counter(make_of.values()), Counter(part_of.values())
    make_quota = _quotas(makes, size, {"": min(makes[""], math.ceil(EMPTY_SHARE * size))})
    part_quota = _quotas(parts, size)
    cells: dict[tuple[str, str], list] = defaultdict(list)
    for row in pool:
        make, part = make_of[str(row.id)], part_of[str(row.id)]
        cells[(make if make in make_quota else RARE,
               part if part in part_quota else RARE)].append(row)
    for members in cells.values():  # The last of a cell is picked first.
        members.sort(key=lambda row: _lot(seed, "row", row.id))
    order = sorted(cells)
    tie = {cell: _lot(seed, "cell", *cell) for cell in order}
    taken_make: Counter = Counter()
    taken_part: Counter = Counter()

    def urgency(cell: tuple[str, str]) -> tuple:
        """A cell whose make spelling and part name are both still missing from the sample
        goes first, then one that fills a quota, then the quota that is least filled."""
        make, part = cell
        need_make = make_quota.get(make, 0) - taken_make[make]
        need_part = part_quota.get(part, 0) - taken_part[part]
        return ((need_make > 0 and not taken_make[make]) + (need_part > 0 and not taken_part[part]),
                (need_make > 0) + (need_part > 0),
                max(need_make, 0) / max(make_quota.get(make, 0), 1)
                + max(need_part, 0) / max(part_quota.get(part, 0), 1),
                tie[cell])

    chosen = []
    while len(chosen) < size:
        cell = max((cell for cell in order if cells[cell]), key=urgency)
        chosen.append(cells[cell].pop())
        taken_make[cell[0]] += 1
        taken_part[cell[1]] += 1
    writing = dict(zip((str(row.id) for row in sorted(
        chosen, key=lambda row: _lot(seed, "writing", row.id))), _styles(size), strict=True))
    typos = {str(row.id) for row in sorted(
        chosen, key=lambda row: _lot(seed, "typos", row.id))[:round(TYPO_SHARE * size)]}
    return [{"row": row, "make": make_of[str(row.id)], "part": part_of[str(row.id)],
             "language": writing[str(row.id)], "typos": str(row.id) in typos}
            for row in chosen]


def row_view(row: Any) -> dict:
    """A catalogue row as the reports and the models of the check read it."""
    return {"id": str(row.id), "name": row.name, "vehicle": row.vehicle, "brand": row.brand,
            "number": row.code, "cross_numbers": "/".join(row.cross_codes or ()),
            "in_stock": in_stock(row)}


def numbers_of(code: Any, cross: Any) -> set[str]:
    """The part numbers a row carries, its own and its cross numbers, as letters and
    digits. A word without a digit (a brand, a product line) is not a number."""
    if isinstance(cross, str):
        cross = cross.split("/")
    carrier = SimpleNamespace(code=code or "", cross_codes=tuple(cross or ()))
    return {number for number in row_numbers(carrier) if any(char.isdigit() for char in number)}


def shown_as(row: Any, cards: list) -> dict | None:
    """How a reply shows the wanted row: as itself, or as a row that shares two or more
    numbers with it (the same part from another brand, or filed under another car). None
    when no card is either."""
    products = [card.get("product") for card in cards
                if isinstance(card, dict) and isinstance(card.get("product"), dict)]
    for product in products:
        if str(product.get("source_product_id")) == str(row.id):
            return {"how": "the row itself", "brand": product.get("brand") or "",
                    "number": product.get("original_code") or ""}
    wanted = numbers_of(row.code, row.cross_codes)
    for product in products:
        shared = sorted(wanted & numbers_of(product.get("original_code"),
                                            product.get("duplicate_code") or ""))
        if len(shared) >= SHARED_NUMBERS:
            return {"how": "shared numbers", "numbers": shared,
                    "brand": product.get("brand") or "",
                    "number": product.get("original_code") or ""}
    return None


# -- the catalogue-wide check: the customer and the judge ------------------------------------

WRITER_SYSTEM = """\
You write test messages for the chat of a car-parts shop in Georgia. You get one row of the
shop's stock list and play a customer who needs exactly that part and does not know the list.
`the_part_is`, when given, is what the row's name means, read for you by a parts specialist:
trust it over your own reading of the Georgian name.

""" + ROW_READING + """
THE MESSAGE
Write what such a customer would type into the chat as a first message, and nothing else.
- The car in everyday words, the way owners say it: make and model, not the shop's shorthand.
  When the row lists several cars, pick one of them. Use a chassis or generation code only when
  owners of that car really say it.
- A year only when the row lets you know one: inside its stated range (for an open range, one
  to four years after its start; for a mark before a year, that year itself), or inside the
  production years of the generation the row names. Give no year when the row tells neither.
  When the row names only a make, name only the make. Never name a model, a body or an engine
  that the row does not name.
- The part in the words people use at a parts counter in that language, never the stock list's
  own wording. In Georgian that is often a Russian trade word in Georgian letters. Customers
  usually say front or rear; the side, upper or lower, sizes and the engine they often leave
  out of a first message: the shop can ask.
- A product that is not tied to a car (oil, fluid, wiper, belt, bulb): ask for it by what its
  name states, such as type, grade, specification, size or volume.
- language: `ka` is Georgian in Georgian script; `ka-latin` is Georgian typed in Latin letters
  the way people text (minda, mchirdeba, gaqvt); `ru` is Russian; `en` is English.
- typos true: two or three typing mistakes of the kind made on a phone (a missed or doubled
  letter, a neighbouring key), never in a year or an engine size.
- A bare phrase or one or two short sentences. Never a part number, never the brand of the
  part, never a price.
When the row names no car and its name states nothing a customer could say to get exactly this
product (only the kind of part), set askable to false and say why in why_not.

Also report what you meant, in English: `car` as make, model and generation ("" if none),
`year` (0 if none), `part` with its position and side, and `names`: "model" when the message
names a car model or chassis, "make" when it names only a make, "product" when it names no car.
"""

ANSWER_SYSTEM = """\
You continue to play a customer in the chat of a car-parts shop in Georgia. You need exactly
the part of one row of the shop's stock list. You know that row; the shop's assistant does not
know which row you mean. You see what you asked for, the conversation, the assistant's last
question with its numbered answer buttons, and the parts it showed. `the_part_is`, when given,
is what the row's name means, read for you by a parts specialist.

""" + ROW_READING + """
WHAT THE CUSTOMER DOES NEXT
- The assistant asked something the row answers, or that follows from what you said: answer.
  When one button says exactly that, tap it: action "tap" with its number in `option`.
  Otherwise type the answer: action "say" with `text`.
- What the row states is what you know about your car: an engine size or code in its vehicle
  text is your engine, a chassis or generation code is your car's, and a range that begins in
  a year (`2015->`) means your car is the body that began in that year. Answer with it.
- The row does not tell what is asked: say that you do not know, or tap the button for not
  being sure when there is one. However often you are asked, never make up a year, an engine,
  a generation or a model. A year mark that is ambiguous (a mark before the year) does not
  tell which body or generation the car is.
- The assistant showed parts and asked nothing: when the cards visibly are not what you need
  (another part, another position or side, another car, generation or engine than yours), say
  what is wrong in one short sentence, the way a customer would. Otherwise action "done".
- The assistant found nothing and asked nothing: say what you need once more in other words,
  or action "done" when you already did.
You only know what a customer knows: the car as you named it, and what the row's name and
vehicle text tell about it. Stay in the language and the letters of your first message. Never
mention a part number, the brand of the part or a price. One short message.
`option` is 0 unless you tap; `text` is "" unless you say something.
"""

JUDGE_SYSTEM = """\
You check the work of the chat assistant of a car-parts shop in Georgia. You are an experienced
parts-counter specialist: you know which generations, chassis codes, engines and years of a
model share a part, and you recognise car makers' and aftermarket part numbers.

You get a customer's conversation with the shop, the car as the assistant understood it, the
rows of the shop's stock list that the assistant offered in its last reply (each with the
assistant's confidence, the fragment of the row it quoted as evidence, and its reason), and
other rows that its searches returned and it did not offer.

""" + ROW_READING + """
The assistant's confidence: `confirmed` means an outside catalogue named this number for the
car; `listed` that the row's own text names the car with its year or generation; `spec` that
the row is not tied to a car (an oil, a fluid, a part sold by size) and was chosen for its
grade, approval or size (the reason says which); `check` that the fit is uncertain and the
shop must verify it (the reason says what).

MARK EVERY OFFERED ROW
- fits: it is the requested part, for the requested position and side, for this car: its
  generation or year, and its engine or version where the part depends on them.
- plausible: it is the requested part and may well fit, but what you are given does not settle
  it: the row names no year or generation, or a fact the customer did not give decides it.
- wrong: another part, another position or side, or a row for another car, generation, engine
  or version than the customer's.
Judge the row, not its label: a doubtful row offered as `check` is plausible, and a row for
another generation is wrong whatever its label. Use the numbers when you recognise them. When
the customer left something open (which axle, which engine) and the assistant showed rows for
one answer, those rows are plausible, not wrong.
Give every offered row an `issue` (none, part, position, generation_or_year, model,
engine_or_fuel, other) and one sentence in English that says why.
Then list in `missed` the other rows that should have been shown: rows in stock that fit this
request as well as or better than what was offered. A row that is not in stock is missed only
when nothing that fits was offered. An empty list when there are none. Use the ids exactly as
given.
"""

VERDICTS = ("fits", "plausible", "wrong")
ISSUES = ("none", "part", "position", "generation_or_year", "model", "engine_or_fuel", "other")


NAMES = ("model", "make", "product")  # what a written request names of the car
REQUEST_SCHEMA = object_schema({
    "askable": {"type": "boolean"}, "why_not": {"type": "string"},
    "message": {"type": "string"}, "car": {"type": "string"}, "year": {"type": "integer"},
    "part": {"type": "string"}, "names": {"type": "string", "enum": list(NAMES)}})
ANSWER_SCHEMA = object_schema({
    "action": {"type": "string", "enum": ["tap", "say", "done"]},
    "option": {"type": "integer"}, "text": {"type": "string"}})
JUDGE_SCHEMA = object_schema({
    "offers": {"type": "array", "items": object_schema({
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
        "issue": {"type": "string", "enum": list(ISSUES)}, "note": {"type": "string"}})},
    "missed": {"type": "array", "items": object_schema({
        "id": {"type": "string"}, "note": {"type": "string"}})}})
NAME_EXAMPLES = 3


def name_examples(sample: list[dict], rows: Any = ()) -> dict[str, list[Any]]:
    """For every part name of a sample, up to three rows that carry it: the sampled ones,
    then others of the catalogue. The rows show a reader what the name means."""
    examples: dict[str, list[Any]] = {}
    for item in sample:
        found = examples.setdefault(item["row"].name, [])
        if len(found) < NAME_EXAMPLES:
            found.append(item["row"])
    short = {name for name, found in examples.items() if len(found) < NAME_EXAMPLES}
    for row in rows if short else ():
        if row.name in short and all(row.id != known.id for known in examples[row.name]):
            examples[row.name].append(row)
            if len(examples[row.name]) >= NAME_EXAMPLES:
                short.discard(row.name)
                if not short:
                    break
    return examples


def request_problems(row: Any, language: str, message: str) -> list[str]:
    """What a written message must not do, read from its letters: carry one of the row's
    numbers, name the brand of the part, or be in other letters than asked for. A number or
    a brand that the row's own name or vehicle text states is part of what the product is
    called, and may be said."""
    from automotive.assistant import written_in

    problems = []
    said, named = plain(message), plain(f"{row.name} {row.vehicle}")
    leaked = sorted(number for number in numbers_of(row.code, row.cross_codes)
                    if len(number) >= 5 and number in said and number not in named)
    if leaked:
        problems.append("it contains the part number " + ", ".join(leaked))
    words = set(WORDS.findall(message.casefold()))
    known = set(WORDS.findall(f"{row.name} {row.vehicle}".casefold()))
    if any(len(word) >= 3 and word in words and word not in known
           for word in WORDS.findall(str(row.brand).casefold())):
        problems.append("it names the brand of the part")
    if written_in(message) != SCRIPT.get(language, ""):
        problems.append(f"it is not written as language {language} asks")
    return problems


class Models:
    """The writer of the customers' messages and the judge of the offers: one client, and
    what each model used. Safe to call from several threads."""

    def __init__(self, client: Any, *, writer: str = WRITER_MODEL,
                 judge: str = JUDGE_MODEL) -> None:
        self.client, self.writer, self.judge_model = client, writer, judge
        self._asker = Asker(client)
        self.usage = self._asker.usage

    def spent(self) -> dict[str, float]:
        return {model: cost(model, usage) for model, usage in self._asker.used().items()}

    def total(self) -> float:
        return sum(self.spent().values())

    def _ask(self, model: str, system: str, case: dict, schema: dict, *, effort: str,
             max_tokens: int) -> dict | None:
        """One question to a model, answered as one JSON object. None when it could not be
        asked, the model declined or the answer is no object."""
        return self._asker.ask(model, system, case, schema, effort=effort,
                               max_tokens=max_tokens)

    def read_names(self, examples: dict[str, list[Any]]) -> dict[str, str]:
        """What each part name means in plain English, read by the judge model from the
        name and the rows that carry it: the writer model does not know the shop's words.
        A name the reader was not asked about, or said nothing of, is left out."""
        readings, _ = read_names(self._asker, self.judge_model, examples,
                                 system=READER_SYSTEM)
        return readings

    def write(self, row: Any, language: str, typos: bool, reading: str = "") -> dict | None:
        """The first message of a customer who needs exactly this row, with what the writer
        meant. Written once more when it leaks the row's number or brand or is in the wrong
        letters; what is still wrong after that is kept in ``problems``."""
        view = {key: value for key, value in row_view(row).items()
                if key not in ("id", "in_stock")}
        case = {"task": "write", "language": language, "typos": bool(typos), "row": view}
        if reading:
            case["the_part_is"] = reading
        request = _request(self._ask(self.writer, WRITER_SYSTEM, case, REQUEST_SCHEMA,
                                     effort="low", max_tokens=3000), language, typos)
        if request and request["askable"]:
            problems = request_problems(row, language, request["message"])
            if problems:
                again = _request(self._ask(
                    self.writer, WRITER_SYSTEM,
                    {**case, "your_last_message": request["message"],
                     "what_is_wrong_with_it": problems},
                    REQUEST_SCHEMA, effort="low", max_tokens=3000), language, typos)
                request = again or request
                request["problems"] = request_problems(row, language, request["message"]) \
                    if request["askable"] else []
        if request:
            request["reading"] = reading
        return request

    def answer(self, row: Any, request: dict, turns: list[dict], payload: dict) -> dict | None:
        """What the customer does after a reply that did not show the row: tap an answer
        button, say something, or nothing more."""
        question, options = question_of(payload)
        view = {key: value for key, value in row_view(row).items()
                if key not in ("id", "in_stock")}
        case = {
            "task": "answer", "row": view, "the_part_is": request.get("reading") or "",
            "you_asked_for": {key: request.get(key) for key in ("car", "year", "part",
                                                                 "language")},
            "conversation": [{"you": turn["say"], "assistant": turn["answer"]}
                             for turn in turns],
            "assistant_question": question,
            "answer_buttons": [f"{number}. {label}" for number, label in enumerate(options, 1)],
            "next_step_buttons": suggestions_of(payload),
            "parts_shown": [{"heading": card["group"], "name": card["name"],
                             "fits": card["vehicle"], "label": card["confidence"],
                             "note": card["reason"]} for card in shown_rows(payload)[:12]]}
        return self._ask(self.writer, ANSWER_SYSTEM, case, ANSWER_SCHEMA, effort="low",
                         max_tokens=2000)

    def judge(self, case: dict) -> dict | None:
        return self._ask(self.judge_model, JUDGE_SYSTEM, case, JUDGE_SCHEMA, effort="medium",
                         max_tokens=12000)


def _request(answer: dict | None, language: str, typos: bool) -> dict | None:
    """A writer's answer in the shape the check keeps, or None when it is none."""
    if not isinstance(answer, dict):
        return None
    message = " ".join(str(answer.get("message") or "").split())[:500]
    year = answer.get("year")
    return {"askable": answer.get("askable") is True and bool(message),
            "why_not": " ".join(str(answer.get("why_not") or "").split())[:300],
            "message": VIN.sub("[VIN]", message),
            "car": " ".join(str(answer.get("car") or "").split())[:120],
            "year": year if isinstance(year, int) and not isinstance(year, bool) else 0,
            "part": " ".join(str(answer.get("part") or "").split())[:160],
            "names": answer.get("names") if answer.get("names") in NAMES else "",
            "language": language, "typos": bool(typos), "problems": []}


def next_move(reply: dict | None, payload: dict) -> tuple[str, int | None] | None:
    """What the customer sends next: the text and, for a tapped answer button, its number.
    None when they have nothing to add."""
    if not isinstance(reply, dict):
        return None
    _, options = question_of(payload)
    number, action = reply.get("option"), reply.get("action")
    if action == "tap" and isinstance(number, int) and not isinstance(number, bool) \
            and 1 <= number <= len(options):
        return options[number - 1], number
    text = VIN.sub("[VIN]", " ".join(str(reply.get("text") or "").split())[:400])
    return (text, None) if action in ("tap", "say") and text else None


# -- the catalogue-wide check: one row -------------------------------------------------------


def shown_rows(payload: dict) -> list[dict]:
    """The cards of a reply as rows, each under the heading of its group."""
    headings = {group.get("id"): group.get("label") for group in payload.get("groups") or []
                if isinstance(group, dict)}
    rows = []
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        product = card.get("product") if isinstance(card.get("product"), dict) else {}
        view = card_row(card)
        view.update(id=str(product.get("source_product_id") or ""),
                    cross_numbers=product.get("duplicate_code") or "",
                    group=headings.get(view["group"]) or "")
        rows.append(view)
    return rows


def _exchange(store: Any, conversation: str, message: str, choice: int | None, *,
              poll: float, turn_timeout: float, clock: Any, sleep: Any) -> tuple[dict, float]:
    """Send one message and wait for its reply. Returns the reply and the seconds it took."""
    started = clock()
    ticket = store.submit(conversation, message, language="auto",
                          **({"choice": choice} if choice else {}))
    payload = store.get(conversation, ticket["turn_id"], 0, 100)
    while payload.get("status") == "RUNNING" and clock() - started < turn_timeout:
        sleep(poll)
        payload = store.get(conversation, ticket["turn_id"], 0, 100)
    return payload, clock() - started


def run_row(store: Any, models: Any, item: dict, *, model: str, poll: float = 0.05,
            turn_timeout: float = 120.0, clock: Any = time.monotonic,
            sleep: Any = time.sleep) -> dict:
    """One row of the sample: its customer asks, answers the engine's questions from the
    row, and the conversation ends when the row is shown, after three turns, when a turn
    fails or when the customer has nothing to add."""
    row = item["row"]
    record: dict = {"id": str(row.id), "make": item["make"], "part": item["part"],
                    "language": item["language"], "typos": bool(item["typos"]),
                    "row": row_view(row), "request": None, "names": "", "status": "missed",
                    "found_at": None, "found_as": None, "ended": "", "turns": [],
                    "cost_usd": 0.0}
    request = item.get("request") or models.write(row, item["language"], item["typos"],
                                                  item.get("reading") or "")
    if not isinstance(request, dict):
        record.update(status="not_written", ended="the writer gave no message")
        return record
    record["request"] = request
    record["names"] = request.get("names") or ""
    if not request.get("askable") or not request.get("message"):
        record.update(status="not_askable",
                      ended=request.get("why_not") or "the row cannot be asked for in words")
        return record
    conversation = store.create()["conversation_id"]
    message, choice = str(request["message"]), None
    try:
        for number in range(1, MAX_TURNS + 1):
            payload, seconds = _exchange(store, conversation, message, choice, poll=poll,
                                         turn_timeout=turn_timeout, clock=clock, sleep=sleep)
            usage = {key: int((payload.get("usage") or {}).get(key) or 0)
                     for key in USAGE_KEYS}
            spent = cost(model, usage)
            failed = payload.get("status") != "COMPLETE"
            if failed and (payload.get("status") == "RUNNING"
                           or payload.get("engine_status") in FAILED_STATUSES):
                spent += cost(model, LOST_REQUEST)
            diagnostics = payload.get("diagnostics") \
                if isinstance(payload.get("diagnostics"), dict) else {}
            question, options = question_of(payload)
            record["turns"].append({
                "say": message, "tapped": choice, "seconds": round(seconds, 2),
                "status": payload.get("status"), "engine_status": payload.get("engine_status"),
                "outcome": payload.get("outcome"), "car_label": payload.get("car_label") or "",
                "answer": VIN.sub("[VIN]", str(payload.get("answer") or "")),
                "question": question, "options": options,
                "suggestions": suggestions_of(payload), "cards": shown_rows(payload),
                "usage": usage, "cost_usd": round(spent, 5),
                "tools": diagnostics.get("tools") or [],
                "corrections": diagnostics.get("corrections") or [],
                "notes": diagnostics.get("notes") or "", "failed": failed,
                "asks": asks(payload),
                "question_without_options": asks(payload) and not (
                    MIN_OPTIONS <= len(options) <= MAX_OPTIONS)})
            record["cost_usd"] += spent
            found = shown_as(row, payload.get("cards") or [])
            if found:
                record.update(status="found", found_at=number, found_as=found)
                break
            if failed:
                record["ended"] = (f"turn {number} did not complete: {payload.get('status')}/"
                                   f"{payload.get('engine_status')}")
                break
            if number == MAX_TURNS:
                record["ended"] = f"{MAX_TURNS} turns used"
                break
            move = next_move(models.answer(row, request, record["turns"], payload), payload)
            if move is None:
                record["ended"] = "the customer had nothing to add"
                break
            message, choice = move
    finally:
        with contextlib.suppress(Exception):  # It may have expired on its own.
            store.delete(conversation)
    record["cost_usd"] = round(record["cost_usd"], 5)
    return record


# -- the catalogue-wide check: reading a miss ------------------------------------------------

GROUPS = ("make_terms", "vehicle_terms", "part_terms")


def _searcher(index: Any) -> Toolbox:
    """The engine's own search tool over an index, for running a turn's searches again."""
    return Toolbox(index=index, repository=None, search_service=None, vin=None,
                   audience="public", handles=None, deadline=float("inf"), crawled=object())


def searched_rows(index: Any, tools: list) -> list[Any]:
    """The rows a turn's catalogue searches and number look-ups returned to the model, in
    their order, found by running them again on the same snapshot."""
    searcher, found = _searcher(index), {}
    for call in tools:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        rows: list[Any] = []
        if call.get("name") == "search_catalog":
            with contextlib.suppress(ToolError):
                rows = searcher.run("search_catalog", args).data["rows"].rows
        elif call.get("name") == "lookup_codes":
            numbers = [number for number in args.get("numbers") or []
                       if isinstance(number, str)]
            rows = [index.row(hit["row"][0]) for hits in index.by_codes(numbers).values()
                    for hit in hits]
        found.update({str(row.id): row for row in rows if row is not None})
    return list(found.values())


def search_trace(index: Any, alone: Any, row: Any, call: dict) -> dict:
    """One tool call of a turn with where the wanted row was in its result. ``alone`` is an
    index of that row only: a term group that finds nothing in it does not match the row."""
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    trace: dict = {"tool": call.get("name"), "rows": call.get("rows")}
    if call.get("error"):
        trace["error"] = call["error"]
    if call.get("name") == "search_catalog":
        trace.update({name: args.get(name) or [] for name in GROUPS})
        try:
            found = _searcher(index).run("search_catalog", args).data["rows"].rows
        except ToolError as exc:
            trace["row"] = f"the search was refused: {exc.error}"
            return trace
        returned = [str(item.id) for item in found]
        if str(row.id) in returned:
            trace["row"] = f"returned at place {returned.index(str(row.id)) + 1}"
            return trace
        unmatched = []
        for name in GROUPS:
            terms = [term for term in args.get(name) or [] if isinstance(term, str)]
            try:
                if terms and not alone.search(**{name: terms}, limit=1)["total"]:
                    unmatched.append(name)
            except ValueError:
                unmatched.append(name)
        if unmatched == ["vehicle_terms"]:
            trace["row"] = ("matches the make and part terms but no vehicle term, and "
                            "more rows matched than the result holds")
        elif unmatched:
            trace["row"] = "not matched by " + ", ".join(unmatched)
        else:
            trace["row"] = f"matched, but past the {SEARCH_ROWS} rows of the result"
    elif call.get("name") == "lookup_codes":
        trace["numbers"] = args.get("numbers") or []
        returned = {str(found.id) for found in searched_rows(index, [call])}
        trace["row"] = "returned" if str(row.id) in returned else "not returned"
    else:
        trace["args"] = {key: value for key, value in args.items() if value not in ("", 0, [])}
    return trace


# What a miss comes down to, from the most telling search of the conversation to the least.
CAUSES = (
    ("returned at place", "the row was returned to the model and not offered"),
    ("returned", "the row was returned to the model and not offered"),
    ("matched, but past", "the row matched a search but was past the rows of its result"),
    ("matches the make and part terms", "no vehicle term of a search matched the row"),
    ("not matched by part_terms", "no part term of a search matched the row's name"),
    ("not matched by", "the make or several term groups of a search did not match the row"),
    ("not returned", "a number look-up did not return the row"),
    ("the search was refused", "the tool refused the terms of a search"),
)
NO_SEARCH = "no catalogue search was made"


def explain(index: Any, row: Any, record: dict) -> None:
    """Add to a record every search of its conversation with where the wanted row was, and
    what the miss comes down to in one line."""
    alone = CatalogIndex([row])
    states = []
    for turn in record["turns"]:
        turn["searches"] = [search_trace(index, alone, row, call) for call in turn["tools"]
                            if isinstance(call, dict)]
        states += [search["row"] for search in turn["searches"] if search.get("row")]
    record["cause"] = next((cause for mark, cause in CAUSES
                            if any(state.startswith(mark) for state in states)), NO_SEARCH)
    last = record["turns"][-1] if record["turns"] else {}
    record["ending"] = (
        "a turn failed" if last.get("failed") else
        "other rows were offered" if last.get("cards") else
        "it was still asking" if last.get("asks") else
        f"it said {str(last.get('outcome') or 'nothing').replace('_', ' ')}")


def catalogue_line(record: dict) -> str:
    """One line for one row as it finishes."""
    row, request = record["row"], record.get("request") or {}
    seconds = sum(turn["seconds"] for turn in record["turns"])
    head = {"found": f"found@{record['found_at']}", "missed": "MISS   "}.get(
        record["status"], record["status"])
    said = request.get("message") or record.get("ended") or ""
    line = (f"{head} {row['id']} [{record['language']}] {row['vehicle'] or '-'} | "
            f"{row['name']} | {row['brand']} {row['number']} :: {said[:110]} "
            f"({len(record['turns'])} turns, {seconds:.1f}s, ${record['cost_usd']:.4f})")
    if record["status"] == "missed":
        line += f"\n        {record.get('ending', '')}; {record.get('cause', record['ended'])}"
    return line


def check_catalogue(store: Any, models: Any, sample: list[dict], *, model: str, out: TextIO,
                    index: Any = None, budget_left: float = float("inf"), parallel: int = 1,
                    **options: Any) -> tuple[list[dict], float]:
    """Run the rows of a sample, at most ``parallel`` at a time, until all are done or the
    money is gone. Returns their records and what the engine's turns cost; what the writer
    cost is kept by ``models``."""
    lock = threading.Lock()
    spent = [0.0]
    before = models.total()
    refused: list[str] = []  # the account status that ended the run, once there is one

    def one(item: dict) -> dict:
        row = item["row"]
        with lock:
            if refused or spent[0] + models.total() - before >= budget_left:
                return {"id": str(row.id), "make": item["make"], "part": item["part"],
                        "language": item["language"], "typos": bool(item["typos"]),
                        "row": row_view(row), "request": item.get("request"),
                        "status": "not_run", "found_at": None, "found_as": None,
                        "ended": account_refusal(refused[0]) if refused
                        else "budget reached", "turns": [], "cost_usd": 0.0}
        try:
            record = run_row(store, models, item, model=model, **options)
        except Exception as exc:  # noqa: BLE001 - one broken row must not end the run
            record = {"id": str(row.id), "make": item["make"], "part": item["part"],
                      "language": item["language"], "typos": bool(item["typos"]),
                      "row": row_view(row), "request": item.get("request"), "status": "error",
                      "found_at": None, "found_as": None,
                      "ended": f"runner error: {type(exc).__name__}", "turns": [],
                      "cost_usd": 0.0}
        if record["status"] == "missed" and index is not None:
            explain(index, row, record)
        with lock:
            spent[0] += record["cost_usd"]
            if refused_status(record) and not refused:
                refused.append(refused_status(record))
            out.write(catalogue_line(record) + "\n")
            out.flush()
        return record

    if parallel <= 1:
        return [one(item) for item in sample], spent[0]
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="catalogue-eval") as pool:
        return list(pool.map(one, sample)), spent[0]


# -- the catalogue-wide check: the wrong-offer review ----------------------------------------

CSV_COLUMNS = ("request", "car", "brand", "number", "catalogue vehicle text", "confidence",
               "verdict", "judge note", "catalogue part name",
               "the row the request was written from")


def card_turns(records: list[dict], size: int, seed: int) -> list[tuple[dict, int]]:
    """Up to ``size`` of the turns that showed cards, as (record, turn number), drawn by
    the seed."""
    turns = [(record, number) for record in records
             for number, turn in enumerate(record["turns"], 1) if turn["cards"]]
    turns.sort(key=lambda pair: _lot(seed, "judge", pair[0]["id"], pair[1]))
    return turns[:max(0, int(size))]


def judge_case(index: Any, record: dict, number: int) -> tuple[dict, list[dict], list[Any]]:
    """What the judge reads about one turn: the conversation, the car as understood, the
    offered rows with the engine's judgement, and up to forty other rows that the
    conversation's searches returned, those in stock first. Also the offered cards and the
    other rows behind the ids it uses."""
    turns = record["turns"][:number]
    turn = turns[-1]
    offered = turn["cards"]
    taken = {card["id"] for card in offered}
    others = [row for row in searched_rows(index, [call for earlier in turns
                                                  for call in earlier["tools"]])
              if str(row.id) not in taken]
    others = sorted(others, key=lambda row: not in_stock(row))[:OTHER_ROWS]
    case = {
        "conversation": [{"customer": earlier["say"], "shop": earlier["answer"]}
                         for earlier in turns],
        "car_as_the_assistant_understood_it": turn["car_label"],
        "assistant_notes": turn.get("notes") or "",
        "offered": [{"id": f"o{place}", "name": card["name"], "vehicle": card["vehicle"],
                     "brand": card["brand"], "number": card["number"],
                     "cross_numbers": card["cross_numbers"], "heading": card["group"],
                     "confidence": card["confidence"], "evidence": card["evidence"],
                     "reason": card["reason"]} for place, card in enumerate(offered, 1)],
        "other_rows": [{**{key: value for key, value in row_view(row).items() if key != "id"},
                        "id": f"c{place}"} for place, row in enumerate(others, 1)]}
    return case, offered, others


def judge_turn(models: Any, index: Any, record: dict, number: int) -> dict:
    """One turn as the judge marked it: every offered row with its verdict, and the rows
    the judge says should have been shown."""
    case, offered, others = judge_case(index, record, number)
    answer = models.judge(case) or {}
    verdicts = {str(item.get("id")): item for item in answer.get("offers") or []
                if isinstance(item, dict)}
    wanted = SimpleNamespace(id=record["id"], code=record["row"]["number"],
                             cross_codes=record["row"]["cross_numbers"].split("/"))
    offers = []
    for place, card in enumerate(offered, 1):
        said = verdicts.get(f"o{place}") or {}
        offers.append({
            "brand": card["brand"], "number": card["number"], "name": card["name"],
            "vehicle": card["vehicle"], "confidence": card["confidence"],
            "evidence": card["evidence"], "reason": card["reason"],
            "verdict": said.get("verdict") if said.get("verdict") in VERDICTS else "unjudged",
            "issue": said.get("issue") if said.get("issue") in ISSUES else "",
            "note": " ".join(str(said.get("note") or "").split())[:400],
            # The judge is not told which row the customer was written for.
            "wanted": shown_as(wanted, [{"product": {
                "source_product_id": card["id"], "original_code": card["number"],
                "duplicate_code": card["cross_numbers"]}}]) is not None})
    missed = []
    for item in answer.get("missed") or []:
        name = str(item.get("id") or "") if isinstance(item, dict) else ""
        if name[:1] == "c" and name[1:].isdigit() and 1 <= int(name[1:]) <= len(others):
            row = others[int(name[1:]) - 1]
            missed.append({**row_view(row),
                           "note": " ".join(str(item.get("note") or "").split())[:400]})
    turns = record["turns"][:number]
    return {"row_id": record["id"], "turn": number, "language": record["language"],
            "request": " / ".join(turn["say"] for turn in turns),
            "car": turns[-1]["car_label"], "found_at": record["found_at"],
            "offers": offers, "missed": missed, "judged": bool(verdicts)}


def review(models: Any, index: Any, records: list[dict], *, size: int, seed: int,
           out: TextIO, budget_left: float = float("inf"), parallel: int = 1) -> list[dict]:
    """The wrong-offer review: a sample of the turns that showed cards, each read by the
    judge. Stops asking once the money is gone."""
    before = models.total()

    def one(pair: tuple[dict, int]) -> dict | None:
        if models.total() - before >= budget_left:
            return None
        judged = judge_turn(models, index, *pair)
        marks = Counter(offer["verdict"] for offer in judged["offers"])
        out.write(f"judged {judged['row_id']} t{judged['turn']}: "
                  + ", ".join(f"{count} {verdict}" for verdict, count in sorted(marks.items()))
                  + (f", {len(judged['missed'])} better rows missed" if judged["missed"]
                     else "") + f" :: {judged['request'][:90]}\n")
        out.flush()
        return judged

    turns = card_turns(records, size, seed)
    if parallel <= 1:
        done = [one(pair) for pair in turns]
    else:
        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="judge") as pool:
            done = list(pool.map(one, turns))
    return [judged for judged in done if judged is not None]


FITTING = ("listed", "confirmed")  # the confidences that present a row as fitting


def wrong_apart(offers: list[dict]) -> dict:
    """The rows a judge called wrong, told apart by what the mark measures.

    ``the_wanted_row``: the offered row is the row the request was written from, or the
    same part by its numbers. The judge disagrees with the catalogue, not with the
    assistant: a parts person must say who is right. ``offered_as_check``: another row,
    offered with the word that its fit is uncertain and what to verify. ``offered_as_spec``:
    another row, chosen for its specification. ``offered_as_fitting``: another row offered
    as listed or confirmed, a wrong part presented as fitting. Its rate is over all judged
    rows that were offered as listed or confirmed."""
    wrong = [offer for offer in offers if offer["verdict"] == "wrong"]
    others = [offer for offer in wrong if not offer["wanted"]]
    fitting = sum(1 for offer in offers if offer["confidence"] in FITTING)
    as_fitting = sum(1 for offer in others if offer["confidence"] in FITTING)
    return {
        "the_wanted_row": len(wrong) - len(others),
        "offered_as_check": sum(1 for offer in others if offer["confidence"] == "check"),
        "offered_as_spec": sum(1 for offer in others if offer["confidence"] == "spec"),
        "offered_as_fitting": as_fitting,
        "rows_offered_as_fitting": fitting,
        "wrong_as_fitting_rate": round(as_fitting / fitting, 3) if fitting else None,
    }


def offer_summary(judged: list[dict]) -> dict:
    """The wrong-offer rate of a review, with the marks by the engine's own confidence."""
    offers = [offer for entry in judged for offer in entry["offers"]
              if offer["verdict"] in VERDICTS]
    marks = Counter(offer["verdict"] for offer in offers)
    by_confidence: dict[str, Counter] = defaultdict(Counter)
    for offer in offers:
        by_confidence[str(offer["confidence"])][offer["verdict"]] += 1
    return {
        "wrong_apart": wrong_apart(offers),
        "turns": len(judged), "rows": len(offers),
        **{verdict: marks[verdict] for verdict in VERDICTS},
        "unjudged_rows": sum(1 for entry in judged for offer in entry["offers"]
                             if offer["verdict"] not in VERDICTS),
        "wrong_rate": round(marks["wrong"] / len(offers), 3) if offers else None,
        "turns_with_a_wrong_row": sum(1 for entry in judged if any(
            offer["verdict"] == "wrong" for offer in entry["offers"])),
        "by_confidence": {confidence: {verdict: counts[verdict] for verdict in VERDICTS}
                          for confidence, counts in sorted(by_confidence.items())},
        "wrong_by_issue": dict(Counter(
            offer["issue"] or "other" for offer in offers
            if offer["verdict"] == "wrong").most_common()),
        "wanted_rows_marked_wrong": sum(1 for offer in offers
                                        if offer["wanted"] and offer["verdict"] == "wrong"),
        "turns_with_better_rows_missed": sum(1 for entry in judged if entry["missed"]),
        "turns_with_stocked_rows_missed": sum(1 for entry in judged if any(
            row["in_stock"] for row in entry["missed"])),
    }


def write_csv(path: Path | str, judged: list[dict]) -> int:
    """The judged offers as a sheet for a parts person: one line per offered row. Every
    request is synthetic; a VIN, should one ever be in a cell, is blanked. Returns the
    number of lines."""
    lines = [(entry["request"], entry["car"], offer["brand"], offer["number"],
              offer["vehicle"], offer["confidence"], offer["verdict"], offer["note"],
              offer["name"], "yes" if offer.get("wanted") else "")
             for entry in judged for offer in entry["offers"]]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows([VIN.sub("[VIN]", str(cell if cell is not None else ""))
                          for cell in line] for line in lines)
    return len(lines)


# -- the catalogue-wide check: the summary ---------------------------------------------------


def found_by_turn(records: list[dict]) -> dict:
    """How many of the rows were shown after one, two and three turns."""
    counts = [sum(1 for record in records if record["found_at"] and record["found_at"] <= turn)
              for turn in range(1, MAX_TURNS + 1)]
    return {"rows": len(records), "found": counts,
            "rate": [round(count / len(records), 3) if records else None for count in counts]}


def grouped(records: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        groups[str(record.get(key) or "")].append(record)
    return sorted(({"name": name, **found_by_turn(members)} for name, members in groups.items()),
                  key=lambda group: (-group["rows"], group["name"]))


def weakest(groups: list[dict], count: int = 3, minimum: int = 3) -> list[dict]:
    """The groups found least often after three turns, among those with enough rows for
    the rate to mean something."""
    enough = [group for group in groups if group["rows"] >= minimum]
    return sorted(enough, key=lambda group: (group["rate"][-1], group["rate"][0],
                                             -group["rows"], group["name"]))[:count]


def summarize_catalogue(records: list[dict], judged: list[dict] | None = None) -> dict:
    """The headline numbers of a catalogue check."""
    asked = [record for record in records if record["status"] in ("found", "missed")]
    missed = [record for record in asked if record["status"] == "missed"]
    turns = [turn for record in records for turn in record["turns"]]
    statuses = Counter(record["status"] for record in records)
    spent = sum(record["cost_usd"] for record in records)
    by_make, by_part = grouped(asked, "make"), grouped(asked, "part")
    return {
        "rows": {"sampled": len(records), "asked": len(asked),
                 **{name: statuses[name] for name in ("found", "missed", "not_askable",
                                                      "not_written", "not_run", "error")}},
        "findability": found_by_turn(asked),
        "by_language": grouped(asked, "language"), "by_request": grouped(asked, "names"),
        "by_make": by_make, "by_part": by_part,
        "weakest_makes": weakest(by_make), "weakest_parts": weakest(by_part),
        "miss_causes": dict(Counter(record.get("cause") or record["ended"]
                                    for record in missed).most_common()),
        "miss_endings": dict(Counter(record.get("ending") or record["ended"]
                                     for record in missed).most_common()),
        "turns": len(turns), "failed_turns": sum(1 for turn in turns if turn["failed"]),
        "questions": sum(1 for turn in turns if turn["asks"]),
        "questions_without_options": sum(1 for turn in turns
                                         if turn["question_without_options"]),
        "seconds": _spread([turn["seconds"] for turn in turns]),
        "cost_usd": round(spent, 4),
        "cost_per_turn_usd": round(spent / len(turns), 5) if turns else None,
        "offers": offer_summary(judged) if judged is not None else None,
    }


def _rates(group: dict) -> str:
    return "  ".join(f"{count}/{group['rows']} ({rate:.0%})" if rate is not None else "-"
                     for count, rate in zip(group["found"], group["rate"], strict=True))


def print_catalogue(summary: dict, records: list[dict], out: TextIO, model: str = "") -> None:
    """The summary, then every miss with what was asked, shown and searched."""
    rows, seconds = summary["rows"], summary["seconds"]
    out.write(f"\nmodel {model}: {rows['sampled']} rows sampled, {rows['asked']} asked "
              f"({rows['not_askable']} cannot be asked for in words, {rows['not_written']} "
              f"not written, {rows['error']} errors, {rows['not_run']} not run)\n")
    out.write("shown after 1, 2 and 3 turns\n")
    out.write(f"  all rows{'':<28}{_rates(summary['findability'])}\n")
    for title, key in (("by language", "by_language"),
                       ("by what the request names of the car", "by_request"),
                       ("weakest makes", "weakest_makes"),
                       ("weakest part names", "weakest_parts")):
        out.write(f"{title}\n")
        for group in summary[key]:
            out.write(f"  {group['name'] or '(none)':<36}{_rates(group)}\n")
    for title, key in (("misses by cause", "miss_causes"), ("misses by ending", "miss_endings")):
        out.write(f"{title}\n")
        for cause, count in summary[key].items():
            out.write(f"  {count:>3}  {cause}\n")
    if seconds["count"]:
        out.write(f"turns {summary['turns']}: median {seconds['median']} s, p90 "
                  f"{seconds['p90']} s, max {seconds['max']} s; {summary['failed_turns']} "
                  f"failed; {summary['questions_without_options']} of {summary['questions']} "
                  "questions without answer buttons\n")
    out.write(f"engine cost ${summary['cost_usd']:.4f}, "
              f"${summary['cost_per_turn_usd'] or 0:.5f} per turn\n")
    offers = summary["offers"]
    if offers and offers["rows"]:
        out.write(f"wrong-offer review: {offers['rows']} offered rows in {offers['turns']} "
                  f"turns: {offers['fits']} fit, {offers['plausible']} plausible, "
                  f"{offers['wrong']} wrong ({offers['wrong_rate']:.1%}); by issue "
                  f"{offers['wrong_by_issue']}; by confidence {offers['by_confidence']}; "
                  f"{offers['turns_with_stocked_rows_missed']} turns left out a row in "
                  "stock that fits as well\n")
        apart = offers.get("wrong_apart")
        if apart:
            rate = apart["wrong_as_fitting_rate"]
            out.write(f"the wrong rows apart: {apart['the_wanted_row']} are the row the "
                      "request was written from (the judge against the catalogue), "
                      f"{apart['offered_as_check']} other rows were offered as check, "
                      f"{apart['offered_as_spec']} as spec, {apart['offered_as_fitting']} "
                      f"as listed or confirmed, of {apart['rows_offered_as_fitting']} rows "
                      "offered so" + (f" ({rate:.1%})" if rate is not None else "") + "\n")
    for record in records:
        if record["status"] != "missed":
            continue
        row, request = record["row"], record.get("request") or {}
        out.write(f"\nMISS {row['id']} [{record['language']}] {row['vehicle'] or '-'} | "
                  f"{row['name']} | {row['brand']} {row['number']}\n"
                  f"  meant: {request.get('car')} {request.get('year') or ''}, "
                  f"{request.get('part')}\n")
        for number, turn in enumerate(record["turns"], 1):
            shown = ", ".join(f"{card['brand']} {card['number']} ({card['vehicle']}; "
                              f"{card['confidence']})" for card in turn["cards"][:6])
            out.write(f"  t{number} customer: {turn['say']}\n"
                      f"     reply ({turn['outcome']}): {turn['answer'][:200]}\n")
            if turn["options"]:
                out.write(f"     buttons: {' | '.join(turn['options'])}\n")
            if turn["cards"]:
                out.write(f"     {len(turn['cards'])} cards: {shown}\n")
            for search in turn.get("searches") or []:
                terms = " ".join(f"{name.split('_')[0]}={search[name]}" for name in GROUPS
                                 if search.get(name))
                out.write(f"     {search['tool']} {terms or search.get('numbers') or ''} "
                          f"-> {search.get('rows')} rows; the row: "
                          f"{search.get('row', 'not looked for')}\n")
        out.write(f"  {record.get('ending', '')}; {record.get('cause', record['ended'])}\n")
    out.flush()


# -- the live application --------------------------------------------------------------------


def stock_reader(index: Any) -> Any:
    """``stock(spec)`` over the live catalogue: is a row with this brand number in the shop
    or its warehouse today (True), listed but not in stock (False), or not listed (None)."""
    known: dict[str, bool | None] = {}

    def stock(spec: str) -> bool | None:
        if spec not in known:
            _, number = parse_row(spec)
            hits = index.by_codes([number], limit_per_number=50).get(number) or []
            rows = [hit["row"] for hit in hits if hit.get("field") == "code"
                    and row_matches(hit["row"][3], hit["row"][4], spec)]
            known[spec] = any((row[7] or 0) > 0 or (row[8] or 0) > 0 for row in rows) \
                if rows else None
        return known[spec]

    return stock


def live(model: str | None, audience: str,
         cars: dict[str, dict] | None = None) -> tuple[Any, Any, Any, str]:
    """The application as the chats run it, with the assistant engine and no chat log, so
    test turns never reach the owner's logs. ``cars`` are the stand-in VINs of the run
    with the car each decodes to. Returns the app, the store of the audience, the
    catalogue index and the model in use."""
    from dataclasses import replace

    from automotive.api import create_app
    from automotive.assistant import catalogue_index
    from automotive.config import Settings

    changes: dict = {"chat_engine": "assistant", "chat_log_path": "", "site_chat_log_path": ""}
    if model:
        changes["assistant_model"] = model
    settings = replace(Settings.from_env(), **changes)
    app = create_app(settings=settings)
    app.state.site.catalog_store.refresh()
    public = getattr(app.state.site, "chat", None)
    if audience == "public" and public is None:
        raise RuntimeError("The public chat is not installed in this configuration.")
    store = public.store if audience == "public" else app.state.conversations
    index = catalogue_index(store.chat_service)
    if index is None:
        raise RuntimeError("The catalogue could not be loaded.")
    if cars:
        store.chat_service.vin_service = StandInDecoder(store.chat_service.vin_service, cars)
        store.search_service = StandInSearch(store.search_service, cars)
    # What opening a conversation does off the request path, done before the first turn:
    # the application has been running when a customer writes.
    store.chat_service.prepare()
    return app, store, index, settings.assistant_model or settings.anthropic_model


def earlier_rows(path: Path | str) -> dict[str, dict]:
    """The rows of an earlier catalogue report by id, each with the request written then."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("the file is not a catalogue report")
    return {str(record["id"]): record for record in rows
            if isinstance(record, dict) and record.get("id")}


def earlier_sample(index: Any, earlier: dict[str, dict], only: str | None) -> list[dict]:
    """The rows of an earlier report as a sample, each with its request as written then:
    all of them, the ones it missed (``misses``), or the ids named. Rows that have left
    the catalogue since are left out."""
    wanted = [item.strip() for item in (only or "").split(",") if item.strip()]
    if wanted == ["misses"]:
        wanted = [name for name, record in earlier.items() if record.get("status") == "missed"]
    sample = []
    for name in wanted or list(earlier):
        record, row = earlier.get(name), index.row(name)
        if record is None or row is None:
            continue
        sample.append({"row": row, "make": first_word(row.vehicle),
                       "part": opening_of(row.name),
                       "language": record.get("language") or "ka",
                       "typos": bool(record.get("typos")),
                       "request": record.get("request")
                       if isinstance(record.get("request"), dict) else None})
    return sample


def run_catalogue(parser: Any, args: Any, out: TextIO) -> None:
    """The ``--evaluate-assistant catalogue`` command."""
    ledger = Ledger(args.ledger or os.getenv("AUTOMOTIVE_EVAL_LEDGER") or DEFAULT_LEDGER)
    budget = DEFAULT_BUDGET if args.budget is None else float(args.budget)
    before = ledger.total()
    if before >= budget:
        parser.exit(2, f"Nothing was run: ${before:.2f} of the ${budget:.2f} budget is "
                       f"already spent ({ledger.path}).\n")
    earlier = None
    source = getattr(args, "requests_from", None)
    if source:
        try:
            earlier = earlier_rows(source)
        except (OSError, ValueError) as exc:
            parser.exit(2, f"The earlier report could not be read: {exc}\n")
    seed = int(getattr(args, "seed", None) or 1)
    size = int(getattr(args, "sample", None) or SAMPLE_SIZE)
    judging = getattr(args, "review", None)
    judging = JUDGED_TURNS if judging is None else max(0, int(judging))
    audience = args.audience or "public"
    app, store, index, model = live(args.model, audience)
    sample = earlier_sample(index, earlier, args.only) if earlier is not None \
        else draw(index.rows(), size, seed)
    if not sample:
        parser.exit(2, "No row to run: the catalogue has nothing in stock, or the earlier "
                       "report names no row that is still in it.\n")
    models = Models(store.chat_service._client().with_options(timeout=MODEL_SECONDS,
                                                              max_retries=2))
    out.write(f"{len(sample)} rows (seed {seed}), model {model}, writer {models.writer}, "
              f"judge {models.judge_model}, audience {audience}; ${before:.2f} of "
              f"${budget:.2f} spent so far\n")
    out.flush()
    records: list[dict] = []
    judged: list[dict] | None = None
    spent, warming = 0.0, opening_cost(store.chat_service, model)
    left = budget - before - warming
    workers = max(1, int(args.parallel or 1))
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        # The rows still to be written get their part names read first.
        unwritten = [item for item in sample if not item.get("request")]
        readings = models.read_names(name_examples(unwritten, index.rows()))
        for item in unwritten:
            item["reading"] = readings.get(item["row"].name, "")
        out.write(f"{len(readings)} part names read for the writer\n")
        out.flush()
        records, spent = check_catalogue(store, models, sample, model=model, out=out,
                                         index=index, budget_left=left - models.total(),
                                         parallel=workers)
        if judging:
            judged = review(models, index, records, size=judging, seed=seed, out=out,
                            budget_left=left - spent - models.total(), parallel=workers)
    finally:
        what = f"assistant-eval:catalogue:{audience}:{len(sample)} rows"
        ledger.append(model, what, spent + warming)
        for name, usd in models.spent().items():
            ledger.append(name, f"{what}:{'judge' if name == models.judge_model else 'writer'}",
                          usd)
    summary = summarize_catalogue(records, judged)
    summary["spent_usd"] = {model: round(spent + warming, 4),
                            **{name: round(usd, 4) for name, usd in models.spent().items()}}
    print_catalogue(summary, records, out, model)
    out.write(f"ledger: ${ledger.total():.2f} of ${budget:.2f} ({ledger.path})\n")
    if getattr(args, "csv_out", None) and judged is not None:
        out.write(f"{write_csv(args.csv_out, judged)} judged offers written to "
                  f"{args.csv_out}\n")
    if args.report_out:
        settings = store.chat_service.settings
        report = {"kind": "catalogue", "model": model, "writer": models.writer,
                  "judge": models.judge_model, "audience": audience, "seed": seed,
                  "started_at": started,
                  "settings": {key: getattr(settings, key, None) for key in (
                      "assistant_effort", "assistant_thinking", "assistant_turn_seconds",
                      "assistant_max_requests", "assistant_max_tool_calls")},
                  "summary": summary, "rows": records, "judged": judged or []}
        descriptor = os.open(args.report_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
        out.write(f"report written to {args.report_out}\n")
    out.flush()
    for closing in (getattr(app.state.site, "chat", None), app.state.conversations,
                    getattr(store, "search_service", None), store.chat_service):
        with contextlib.suppress(Exception):  # Closing is best effort at the end of a run.
            closing.close()


def run(parser: Any, args: Any, out: TextIO) -> None:
    """The ``--evaluate-assistant`` command: the test conversations, or the catalogue-wide
    check when it is asked for."""
    if getattr(args, "evaluate_assistant", None) == "catalogue":
        run_catalogue(parser, args, out)
        return
    ledger = Ledger(args.ledger or os.getenv("AUTOMOTIVE_EVAL_LEDGER") or DEFAULT_LEDGER)
    budget = DEFAULT_BUDGET if args.budget is None else float(args.budget)
    before = ledger.total()
    if before >= budget:
        parser.exit(2, f"Nothing was run: ${before:.2f} of the ${budget:.2f} budget is "
                       f"already spent ({ledger.path}).\n")
    try:
        data = load_scenarios(args.scenarios or DEFAULT_SCENARIOS)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Scenarios could not be read: {exc}\n")
    scenarios = select(data["scenarios"], args.only)
    if not scenarios:
        parser.exit(2, "No scenario has that id or tag.\n")
    vins = read_vins(args.vins or DEFAULT_VINS)
    cars = stand_ins(data["cars"], scenarios, vins)
    audience = args.audience or "public"
    app, store, index, model = live(args.model, audience, cars)
    out.write(f"{len(scenarios)} scenarios, model {model}, audience {audience}; "
              f"{len(cars)} stand-in VINs; ${before:.2f} of ${budget:.2f} spent so far\n")
    out.flush()
    results: list[dict] = []
    spent, warming = 0.0, opening_cost(store.chat_service, model)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        results, spent = evaluate(
            store, scenarios, vins=vins, model=model, out=out, stock=stock_reader(index),
            topics=data["topics"], budget_left=budget - before - warming,
            parallel=max(1, int(args.parallel or 1)))
    finally:
        ledger.append(model, f"assistant-eval:{audience}:{len(scenarios)} scenarios",
                      spent + warming)
    summary = summarize(results)
    print_summary(summary, out, model)
    out.write(f"ledger: ${ledger.total():.2f} of ${budget:.2f} ({ledger.path})\n")
    if args.report_out:
        settings = store.chat_service.settings
        report = {"model": model, "audience": audience, "started_at": started,
                  "settings": {key: getattr(settings, key, None) for key in (
                      "assistant_effort", "assistant_thinking", "assistant_turn_seconds",
                      "assistant_max_requests", "assistant_max_tool_calls")},
                  "summary": summary, "scenarios": results}
        descriptor = os.open(args.report_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
        out.write(f"report written to {args.report_out}\n")
    out.flush()
    for closing in (getattr(app.state.site, "chat", None), app.state.conversations,
                    getattr(store, "search_service", None), store.chat_service):
        with contextlib.suppress(Exception):  # Closing is best effort at the end of a run.
            closing.close()
