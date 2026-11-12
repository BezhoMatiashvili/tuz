"""A randomised exam of the assistant engine, its right answers taken from data.

Every run draws new cases from the live catalogue and the crawled catalogues, so runs do not
repeat one another (the seed is written down; a dev set keeps one seed, so that rounds
compare on the same cases). A writer model words each request as a customer of that
language would, a customer model answers the bot's questions from what that customer knows,
and the bot is the real engine. Two numbers are reported and never blended:

- **outcome:** within three turns the conversation shows a stocked row of the case's truth,
  or, for a case whose truth is that the shop has nothing, shows nothing as fitting;
- **wrong rows:** shown rows in stock that the data does not settle go to a stronger model,
  and the share it calls wrong is reported, every such row listed for a person to check.

Kinds, and where their truth comes from:

========== ==================================================== =============================
text       a stocked row; the customer names a car its text      the row, and rows sharing two
           covers                                                or more numbers with it
vin        as text, the car given only as a VIN that the real    as text
           decoder decodes to that car
number     the customer types the row's own or a cross number,   the rows carrying the number
           in varied forms
alternative the customer's car is one a crawled catalogue lists  the rows carrying the number
           for a number of the row, the row's own text names     (the twin, for a twin case)
           another car; or the row naming the car is out of
           stock and an in-stock twin names another car
negative   a crawled number that no shop row carries, nor any    nothing may be shown as
           of its crawled equivalents                            fitting
question   a question about the shop the site content answers    the stored value
========== ==================================================== =============================

Run with ``python -m automotive.assistant_exam``; the runtime is the subscription bench
(``automotive/assistant_bench.py``) unless ``--runtime api`` is given. Long runs write a log,
show a progress bar and continue where they stopped.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from automotive.assistant_catalog import first_word
from automotive.assistant_eval import (
    JUDGE_MODEL,
    MAX_TURNS,
    VIN,
    WRITER_MODEL,
    Models,
    _exchange,
    card_row,
    draw,
    in_stock,
    next_move,
    numbers_of,
    question_of,
    row_view,
)
from automotive.assistant_names import object_schema
from automotive.codes import normalize_code

LOGGER = logging.getLogger("parts_crawler.exam")  # shares the crawler's log handlers
RUNS = Path("data/assistant_eval/exam")
SHARES = {"text": 0.30, "vin": 0.15, "number": 0.15, "alternative": 0.15,
          "negative": 0.10, "question": 0.15}
HEADLINE = ("text", "vin", "number", "alternative", "negative", "question")
REQUEST_SECONDS = 3.4  # median seconds of one Sonnet request on the production path
# The customer who answers the bot's questions must read their own car right (which years a
# generation covers): a small model tapped the wrong generation, so runs use Sonnet.
CUSTOMER_MODEL = "claude-sonnet-5-5"
TURN_TIMEOUT = 150.0
LANGUAGES = (("ka", 0.40), ("ka-latin", 0.25), ("ru", 0.20), ("en", 0.15))
TYPO_SHARE = 0.3
GARBLED = re.compile(r"\?{2,}")
WITH_CAR_SHARE = 0.4  # number cases that also name the car


# -- writer prompts for the new kinds ---------------------------------------------------------

STYLE = """\
- language: `ka` is Georgian in Georgian script; `ka-latin` is Georgian typed in Latin letters
  the way people text (minda, mchirdeba, gaqvt); `ru` is Russian; `en` is English.
- typos true: two or three typing mistakes of the kind made on a phone, never inside a part
  number, a year or a VIN.
- A bare phrase or one or two short sentences, the way people write in a chat."""

NUMBER_SYSTEM = """\
You write test messages for the chat of a car-parts shop in Georgia. Play a customer who has a
part number (from the old part, a box or a mechanic) and asks whether the shop has that part.
You get the number exactly as the customer writes it, and sometimes the car. Write the first
message: it must contain the number exactly as given, character for character. Mention the
car only when `car` is given; never invent one.
""" + STYLE + """
Return the message, and `car` as you mentioned it ("" if none)."""

VIN_SYSTEM = """\
You help build test data for a car-parts shop. Given a car (make, model, generation, year and
sometimes engine), propose real-looking 17-character VINs that a vehicle of exactly that
model, year and engine would carry: the right manufacturer prefix (WMI), the model and body
positions this maker uses for it, the model-year character for that year, a plausible plant
and serial, and, for cars built for North America, a valid check digit. Give five different
VINs. They are checked with a real decoder; only those that decode to this car are used."""

VIN_MESSAGE_SYSTEM = """\
You write test messages for the chat of a car-parts shop in Georgia. Play a customer who sends
the VIN of their car and asks for a part. Write the first message with the literal token
{VIN} where the VIN goes; do not name the make, model or year (the VIN is all you give).
The part in the words people use at a parts counter in that language, never a part number or
a brand.
""" + STYLE

ALTERNATIVE_SYSTEM = """\
You write test messages for the chat of a car-parts shop in Georgia. Play a customer who owns
the car given (make, model, years, engine) and needs the part described. Name the car the way
owners say it (make and model, and the year when given) and the part in the words people use
at a parts counter in that language; never a part number or a brand.
""" + STYLE + """
Return the message, and `car` and `part` in English as you meant them."""

QUESTION_SYSTEM = """\
You write test messages for the chat of a car-parts shop in Georgia. Play a customer who asks
the shop one practical question of the kind given (its phone number, where it is, when it is
open, whether it does a service). Write the first message only.
""" + STYLE

DISPUTE_SYSTEM = """\
You are an experienced parts-counter specialist checking a car-parts shop's chat assistant.
You get the conversation (what the customer asked for and said about their car), one row of
the shop's stock list that fits that request (a known right answer, not the only one), what
crawled manufacturer catalogues say, and the other rows the assistant showed. Judge each shown
row against what the customer asked for and the car as the customer described it:
- fits: it is the kind of part the customer asked for, for the position and side asked, and it
  fits that car (another brand of the same part, or the same part filed under another car,
  counts as fits when its numbers or the catalogues show it).
- plausible: it may well fit, but what you have does not settle it. When the customer did not
  know something that decides the part (the year, the body, the engine) and the assistant
  showed the variants as "needs checking" so the customer can choose, each variant that is
  for this model is plausible, not wrong.
- wrong: another kind of part than the one asked for, the other position or side, or a part
  for another car, generation or engine than the one the customer described.
Use part numbers when you recognise them. One sentence in English per row, saying why."""

SERVICE_CHECK = """\
You check a car-parts shop's chat reply. The customer asked whether the shop does the service
given. Answer says_yes true only when the reply says, in any language, that the shop does it
(or does that kind of work); false when it says no, dodges, or talks about something else."""
SERVICE_SCHEMA = object_schema({"says_yes": {"type": "boolean"}})
MESSAGE_SCHEMA = object_schema({"message": {"type": "string"}, "car": {"type": "string"},
                                "part": {"type": "string"}})
VIN_SCHEMA = object_schema({"vins": {"type": "array", "items": {"type": "string"}}})
DISPUTE_SCHEMA = object_schema({"rows": {"type": "array", "items": object_schema({
    "id": {"type": "string"},
    "verdict": {"type": "string", "enum": ["fits", "plausible", "wrong"]},
    "note": {"type": "string"}})}})


# -- small helpers --------------------------------------------------------------------------


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson interval of a pass rate, as fractions."""
    if total <= 0:
        return (0.0, 0.0)
    p = passed / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    denominator = 1 + z * z / total
    return (max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator))


def pick_language(rng: random.Random) -> str:
    value, total = rng.random(), 0.0
    for language, share in LANGUAGES:
        total += share
        if value <= total:
            return language
    return LANGUAGES[-1][0]


def number_form(number: str, brand: str, rng: random.Random) -> str:
    """The number as a customer might type it: as written, squeezed, spaced, lower case or
    with the brand before it. Only the form changes, never the characters."""
    squeezed = re.sub(r"[\s.\-/]", "", number)
    forms = [number, squeezed, squeezed.lower()]
    if len(squeezed) >= 8:
        cut = len(squeezed) // 2
        forms.append(f"{squeezed[:cut]} {squeezed[cut:]}")
    if brand and rng.random() < 0.5:
        forms.append(f"{brand} {number}")
    return rng.choice(forms)


def twins(index: Any, row: Any) -> set[str]:
    """The row and the rows sharing two or more of its numbers: the same part."""
    found = {str(row.id)}
    for other in index.equivalents(str(row.id), limit=60):
        if len(other.get("shared_numbers") or ()) >= 2:
            found.add(_row_id(other))
    return {item for item in found if item}


def _row_id(entry: dict) -> str:
    row = entry.get("row")
    if isinstance(row, dict):
        return str(row.get("id"))
    if isinstance(row, list | tuple) and row:
        return str(row[0])
    return ""


def carriers(index: Any, numbers: list[str]) -> set[str]:
    """The ids of the rows that carry any of these numbers."""
    found: set[str] = set()
    hits = index.by_codes([n for n in numbers if len(normalize_code(n)) >= 4],
                          limit_per_number=50)
    for entries in hits.values():
        found.update(_row_id(entry) for entry in entries if entry.get("field") != "shop_code")
    return {item for item in found if item}


def crawled_truth(index: Any, numbers: list[str]) -> set[str]:
    """The rows carrying any of the numbers a crawled catalogue lists for the car, and
    their twins: a car often takes more than one number for a part (a spin-on filter or an
    element), and each is the part."""
    truth = carriers(index, numbers)
    for member in list(truth):
        row = index.row(member)
        if row is not None:
            truth |= twins(index, row)
    return truth


def listed_numbers(crawled: Any, vehicle: dict, number: str) -> list[str]:
    """The numbers the crawled catalogues list for a case's vehicle in its part group, the
    case's own number among them; that number alone when they cannot be read."""
    found: list[str] = []
    if crawled is not None and hasattr(crawled, "listed"):
        try:
            found = list(crawled.listed(str(vehicle.get("label") or ""),
                                        str(vehicle.get("part") or "")))
        except Exception as exc:  # The crawled database is optional for the exam.
            LOGGER.warning("exam crawled numbers unavailable: %s", type(exc).__name__)
    return sorted({number, *found})


def settles_a_year(readings: dict[str, str] | None, vehicle: Any) -> bool:
    """Whether a row's vehicle text ties it to a generation or years, by its reading: a
    case that pins the model year (a VIN) has data truth only then. Without readings every
    text counts, as before they existed."""
    if readings is None:
        return True
    reading = readings.get(str(vehicle or "").strip(), "").casefold()
    return bool(reading) and "not stated" not in reading


def stocked(index: Any, ids: set[str]) -> set[str]:
    return {item for item in ids if index.row(item) is not None and in_stock(index.row(item))}


# -- pools: what the data can make, no model call ---------------------------------------------


class Pools:
    """Everything the data offers for each kind, drawn lazily and reproducibly."""

    def __init__(self, index: Any, crawled: Any = None, site: Any = None) -> None:
        self.index, self.crawled, self.site = index, crawled, site

    def stocked_rows(self) -> list[Any]:
        """Rows in stock whose vehicle text is readable (some rows lost their letters to
        question marks in an old export: a request built from them names no car)."""
        return [row for row in self.index.rows() if in_stock(row) and not GARBLED.search(
            row.vehicle or "")]

    def numbered_rows(self) -> list[Any]:
        return [row for row in self.stocked_rows()
                if len(normalize_code(row.code or "")) >= 5 or row.cross_codes]

    def twin_pairs(self, rng: random.Random, limit: int) -> list[tuple[Any, Any]]:
        """Out-of-stock rows that name a car, each with an in-stock twin naming another."""
        candidates = [row for row in self.index.rows()
                      if not in_stock(row) and first_word(row.vehicle)]
        rng.shuffle(candidates)
        pairs = []
        for row in candidates:
            for other in self.index.equivalents(str(row.id), limit=30):
                if len(other.get("shared_numbers") or ()) < 2:
                    continue
                twin = self.index.row(_row_id(other))
                if twin is not None and in_stock(twin) and twin.vehicle != row.vehicle:
                    pairs.append((row, twin))
                    break
            if len(pairs) >= limit:
                break
        return pairs

    def crawled_alternatives(self, rng: random.Random, limit: int) -> list[dict]:
        """Stocked rows carrying a number that a crawled catalogue lists for a car the row's
        text does not name."""
        if self.crawled is None:
            return []
        numbers: dict[str, list[Any]] = defaultdict(list)
        for row in self.stocked_rows():
            for number in numbers_of(row.code, row.cross_codes):
                key = normalize_code(number)
                if len(key) >= 5:
                    numbers[key].append(row)
        try:
            fitted = self.crawled(sorted(numbers))
        except Exception as exc:  # The crawled database is optional for the exam.
            LOGGER.warning("exam crawled fitments unavailable: %s", type(exc).__name__)
            return []
        found = []
        for item in fitted:
            for row in numbers.get(item["normalized"], []):
                text = row.vehicle.casefold()
                model_word = (item["model"] or "").split()[:1]
                if model_word and model_word[0].casefold() in text:
                    continue  # the row's own text already names this car
                found.append({"row": row, "number": item["number"], "brand": item["brand"],
                              "part": item["part"], "vehicle": item})
        rng.shuffle(found)
        unique, seen = [], set()
        for entry in found:
            key = (str(entry["row"].id), entry["vehicle"]["label"])
            if key not in seen:
                seen.add(key)
                unique.append(entry)
            if len(unique) >= limit:
                break
        return unique

    def missing_numbers(self, rng: random.Random, limit: int) -> list[dict]:
        """Crawled numbers that no shop row carries, nor any crawled equivalent."""
        if self.crawled is None or not hasattr(self.crawled, "sample_parts"):
            return []
        try:
            parts = self.crawled.sample_parts(rng.randrange(1 << 30), limit * 6)
        except Exception as exc:
            LOGGER.warning("exam crawled parts unavailable: %s", type(exc).__name__)
            return []
        found = []
        for part in parts:
            if carriers(self.index, [part["number"], *part.get("equivalents", [])]):
                continue
            found.append(part)
            if len(found) >= limit:
                break
        return found

    def shop_facts(self) -> list[dict]:
        if self.site is None:
            return []
        try:
            from automotive.assistant_sources import shop_info
        except ImportError:
            return []
        info = shop_info(self.site, "en")
        georgian = shop_info(self.site, "ka")
        facts = []
        for key in ("phone", "address", "hours"):
            if info.get(key):
                # The reply is in the customer's language: the Georgian value counts too.
                facts.append({"topic": key, "value": info[key],
                              "value_ka": georgian.get(key)})
            else:  # not set: the honest answer is that the shop will tell, never a guess
                facts.append({"topic": key, "value": None, "missing": True})
        titles_ka = [item.get("title") for item in georgian.get("services") or []]
        for number, service in enumerate(info.get("services") or []):
            facts.append({"topic": "service", "value": service.get("title"),
                          "summary": service.get("summary") or "",
                          "title_ka": titles_ka[number] if number < len(titles_ka) else ""})
        return facts

    def sizes(self, rng: random.Random) -> dict[str, int]:
        return {"text": len(self.stocked_rows()), "vin": len(self.stocked_rows()),
                "number": len(self.numbered_rows()),
                "alternative_twins": len(self.twin_pairs(rng, 400)),
                "alternative_crawled": len(self.crawled_alternatives(rng, 400)),
                "negative": len(self.missing_numbers(rng, 60)),
                "question": len(self.shop_facts())}


class CrawledFitments:
    """Read-only queries on the crawled catalogues for the exam's truth."""

    def __init__(self, connect: Any) -> None:
        self.connect = connect

    def __call__(self, numbers: list[str]) -> list[dict]:
        sql = """
            SELECT p.normalized, p.number, p.brand, coalesce(p.name, p.kind, ''),
                   v.make, coalesce(v.model, ''), v.year_from, v.year_to,
                   coalesce(v.engine, ''), coalesce(v.engine_code, ''), coalesce(v.label, '')
            FROM parts_catalog.parts p
            JOIN parts_catalog.part_fitments f ON f.part_id = p.id
            JOIN parts_catalog.vehicles v ON v.id = f.vehicle_id
            WHERE p.normalized = ANY(%s) AND v.make IS NOT NULL
            LIMIT 20000"""
        with self.connect() as connection:
            rows = connection.execute(sql, (numbers,)).fetchall()
        return [{"normalized": r[0], "number": r[1], "brand": r[2], "part": r[3],
                 "make": r[4], "model": r[5], "year_from": r[6], "year_to": r[7],
                 "engine": r[8], "engine_code": r[9], "label": r[10]} for r in rows]

    def listed(self, label: str, part: str) -> list[str]:
        """Every number the crawled catalogues list for the vehicles of this label in this
        part group."""
        sql = """
            SELECT DISTINCT p.number
            FROM parts_catalog.vehicles v
            JOIN parts_catalog.part_fitments f ON f.vehicle_id = v.id
            JOIN parts_catalog.parts p ON p.id = f.part_id
            WHERE v.label = %s AND coalesce(p.name, p.kind, '') = %s"""
        with self.connect() as connection:
            rows = connection.execute(sql, (label, part)).fetchall()
        return sorted(str(r[0]) for r in rows if r[0])

    def sample_parts(self, seed: int, limit: int) -> list[dict]:
        sql = """
            SELECT p.number, p.brand, coalesce(p.name, p.kind, ''),
                   array(SELECT o.number FROM parts_catalog.part_cross_refs c
                         JOIN parts_catalog.parts o ON o.id = c.other_part_id
                         WHERE c.part_id = p.id LIMIT 20)
            FROM parts_catalog.parts p
            WHERE length(p.normalized) >= 6
            ORDER BY md5(p.id::text || %s) LIMIT %s"""
        with self.connect() as connection:
            rows = connection.execute(sql, (str(seed), limit)).fetchall()
        return [{"number": r[0], "brand": r[1], "part": r[2], "equivalents": list(r[3] or [])}
                for r in rows]


# -- building cases ------------------------------------------------------------------------


class Builder:
    """Draws the cases of one exam and has the writer word them."""

    def __init__(self, pools: Pools, models: Any, *, decode: Any = None,
                 vehicle_readings: dict[str, str] | None = None,
                 name_readings: dict[str, str] | None = None) -> None:
        self.pools, self.models, self.decode = pools, models, decode
        # What the vehicle texts mean (assistant_vehicles); None: every row may make a VIN.
        self.vehicle_readings = vehicle_readings
        self.name_readings = name_readings
        # What each part name means, read from the rows that carry it: the writer does not
        # know the shop's Georgian words and misreads them without it.
        self.readings: dict[str, str] = {}

    def _write(self, row: Any, language: str, typos: bool) -> dict | None:
        return self.models.write(row, language, typos, self.readings.get(row.name, ""))

    def _ask(self, system: str, case: dict, schema: dict, model: str = WRITER_MODEL,
             max_tokens: int = 8000) -> dict | None:
        return self.models._asker.ask(model, system, case, schema, effort="low",
                                      max_tokens=max_tokens)

    def build(self, size: int, seed: int, shares: dict[str, float] | None = None,
              parallel: int = 8) -> list[dict]:
        """Draw the cases (reproducible for a seed) and have the writer word them, several
        at a time. Each case has its own random generator, so threads do not change it."""
        rng = random.Random(seed)  # nosec B311 - reproducible test draws
        shares = shares or SHARES
        wanted = {kind: max(0, round(size * share)) for kind, share in shares.items()}
        jobs: list[tuple[Any, tuple]] = []
        sample = draw(self.pools.stocked_rows(),
                      wanted.get("text", 0) + wanted.get("vin", 0), seed)
        jobs += [(self._text, (item,)) for item in sample[:wanted.get("text", 0)]]
        jobs += [(self._vin, (item,)) for item in sample[wanted.get("text", 0):]]
        numbered = self.pools.numbered_rows()
        rng.shuffle(numbered)
        jobs += [(self._number, (row,)) for row in numbered[:wanted.get("number", 0)]]
        alternatives = wanted.get("alternative", 0)
        crawled = self.pools.crawled_alternatives(rng, alternatives // 2 + 1)[:alternatives // 2]
        jobs += [(self._alternative, (entry,)) for entry in crawled]
        pairs = self.pools.twin_pairs(rng, alternatives - len(crawled))
        jobs += [(self._twin, pair) for pair in pairs]
        jobs += [(self._negative, (part,)) for part in self.pools.missing_numbers(
            rng, wanted.get("negative", 0))]
        facts = self.pools.shop_facts()
        jobs += [(self._question, (facts[number % len(facts)],))
                 for number in range(wanted.get("question", 0) if facts else 0)]

        written = [item["row"] for item in sample] + [row for row, _ in pairs] + [
            entry["row"] for entry in crawled]
        if written and hasattr(self.models, "read_names"):
            from automotive.assistant_eval import name_examples

            self.readings = self.models.read_names(name_examples(
                [{"row": row} for row in written], self.pools.index.rows()))
            LOGGER.info("exam part names read: %s of %s", len(self.readings),
                        len({row.name for row in written}))
        # The part-name readings (assistant_vehicles --names) were made for the positions and
        # with the rows' numbers: where one exists, the writer is told that one.
        for name, reading in (self.name_readings or {}).items():
            if name in self.readings or any(row.name == name for row in written):
                self.readings[name] = reading

        def work(numbered_job: tuple[int, tuple[Any, tuple]]) -> dict | None:
            number, (method, arguments) = numbered_job
            try:
                return method(*arguments, random.Random(seed * 100003 + number))  # nosec B311
            except Exception as exc:  # One case the writer could not make is left out.
                LOGGER.warning("exam case not built: %s %s %s", method.__name__,
                               type(exc).__name__, str(exc)[:200])
                return None

        with ThreadPoolExecutor(max(1, parallel)) as pool:
            cases = list(pool.map(work, enumerate(jobs)))
        built = [case for case in cases if case and case.get("message")]
        for position, case in enumerate(built, 1):
            case["id"] = f"{case['kind']}-{seed}-{position:03d}"
        return built

    def _style(self, rng: random.Random) -> dict:
        return {"language": pick_language(rng), "typos": rng.random() < TYPO_SHARE}

    def _text(self, item: dict, rng: random.Random) -> dict | None:
        row = item["row"]
        request = self._write(row, item["language"], item["typos"])
        if not request or not request.get("askable"):
            return None
        request["reading"] = self.readings.get(row.name, "")
        return {"kind": "text", "language": item["language"], "typos": item["typos"],
                "names": request.get("names") or "", "message": request["message"],
                "request": request, "row": row_view(row),
                "truth": {"rows": sorted(twins(self.pools.index, row)), "nothing": False}}

    def _vin(self, item: dict, rng: random.Random) -> dict | None:
        row = item["row"]
        if not settles_a_year(self.vehicle_readings, row.vehicle):
            # A VIN pins a model year; a row whose text names no generation or years cannot
            # be shown from data to fit that year, so it makes a text case instead.
            return self._text(item, rng)
        request = self._write(row, "en", False)
        if not request or not request.get("askable") or request.get("names") != "model" \
                or self.decode is None:
            return self._text(item, rng)  # a VIN needs a model to be made for
        try:
            answer = self._ask(VIN_SYSTEM, {"car": request["car"], "year": request.get("year"),
                                            "row_vehicle_text": row.vehicle}, VIN_SCHEMA)
        except Exception as exc:  # No VIN for this car: the case stays a text case.
            LOGGER.info("exam vin not proposed: %s", type(exc).__name__)
            return self._text(item, rng)
        vin, decoded = None, None
        for candidate in (answer or {}).get("vins") or []:
            candidate = str(candidate).strip().upper()
            if not VIN.fullmatch(candidate):
                continue
            try:
                facts = self.decode(candidate)
            except Exception:  # nosec B112 - an unreadable candidate VIN is not used
                continue
            if _decodes_to(facts, request):
                vin, decoded = candidate, facts
                break
        if vin is None:
            return self._text(item, rng)
        style = {"language": item["language"], "typos": item["typos"]}
        worded = self._ask(VIN_MESSAGE_SYSTEM, {**style, "part": request.get("part")},
                           MESSAGE_SCHEMA)
        message = str((worded or {}).get("message") or "")
        if "{VIN}" not in message:
            message = f"{{VIN}} {message}".strip()
        return {"kind": "vin", **style, "message": message.replace("{VIN}", vin),
                "request": {**request, "language": item["language"]}, "row": row_view(row),
                "decoded": {key: (decoded.get("vehicle") or decoded).get(key)
                            for key in ("make", "model", "model_year")},
                "truth": {"rows": sorted(twins(self.pools.index, row)), "nothing": False}}

    def _number(self, row: Any, rng: random.Random) -> dict | None:
        options = [(row.code, row.brand)] if len(normalize_code(row.code or "")) >= 5 else []
        options += [(cross, "") for cross in row.cross_codes if len(normalize_code(cross)) >= 5]
        if not options:
            return None
        number, brand = rng.choice(options)
        typed = number_form(number, brand, rng)
        style = self._style(rng)
        car = row.vehicle if rng.random() < WITH_CAR_SHARE else ""
        worded = self._ask(NUMBER_SYSTEM, {"number": typed, "car": car, **style},
                           MESSAGE_SCHEMA)
        message = str((worded or {}).get("message") or "")
        if normalize_code(typed) not in normalize_code(message):
            return None
        truth = carriers(self.pools.index, [number]) | twins(self.pools.index, row)
        return {"kind": "number", **style, "message": message, "typed": typed,
                "request": {"car": car, "year": 0, "part": row.name,
                            "language": style["language"]},
                "row": row_view(row), "truth": {"rows": sorted(truth), "nothing": False}}

    def _alternative(self, entry: dict, rng: random.Random) -> dict | None:
        vehicle, row = entry["vehicle"], entry["row"]
        style = self._style(rng)
        years = "-".join(str(year) for year in (vehicle.get("year_from"), vehicle.get("year_to"))
                         if year)
        car = {"make": vehicle["make"], "model": vehicle["model"], "years": years,
               "engine": vehicle.get("engine") or vehicle.get("engine_code") or ""}
        part = self.readings.get(row.name) or entry["part"] or row.name
        worded = self._ask(ALTERNATIVE_SYSTEM, {"car": car, "part": part, **style},
                           MESSAGE_SCHEMA)
        if not worded or not worded.get("message"):
            return None
        numbers = listed_numbers(self.pools.crawled, vehicle, entry["number"])
        truth = crawled_truth(self.pools.index, numbers)
        return {"kind": "alternative", "variant": "crawled", **style,
                "message": worded["message"],
                "request": {"car": worded.get("car") or vehicle["label"], "year": 0,
                            "part": worded.get("part") or entry["part"],
                            "reading": self.readings.get(row.name, ""),
                            "language": style["language"]},
                "row": row_view(row), "crawled": {"number": entry["number"],
                                                  "brand": entry["brand"], "vehicle": vehicle,
                                                  "numbers": numbers},
                "truth": {"rows": sorted(truth), "nothing": False}}

    def _twin(self, row: Any, twin: Any, rng: random.Random) -> dict | None:
        style = self._style(rng)
        request = self._write(row, style["language"], style["typos"])
        if not request or not request.get("askable"):
            return None
        truth = stocked(self.pools.index, twins(self.pools.index, row))
        return {"kind": "alternative", "variant": "twin", **style,
                "names": request.get("names") or "", "message": request["message"],
                "request": request, "row": row_view(row),
                "twin": row_view(twin), "truth": {"rows": sorted(truth), "nothing": False}}

    def _negative(self, part: dict, rng: random.Random) -> dict | None:
        style = self._style(rng)
        typed = number_form(part["number"], part.get("brand") or "", rng)
        worded = self._ask(NUMBER_SYSTEM, {"number": typed, "car": "", **style},
                           MESSAGE_SCHEMA)
        message = str((worded or {}).get("message") or "")
        if normalize_code(typed) not in normalize_code(message):
            return None
        row = {"id": "", "name": part.get("part") or "", "vehicle": "", "brand":
               part.get("brand") or "", "number": part["number"], "cross_numbers": "",
               "in_stock": False}
        return {"kind": "negative", **style, "message": message, "typed": typed,
                "request": {"car": "", "year": 0, "part": part.get("part") or "",
                            "language": style["language"]},
                "row": row, "truth": {"rows": [], "nothing": True}}

    def _question(self, fact: dict, rng: random.Random) -> dict | None:
        style = self._style(rng)
        kind = (f"whether the shop does this service: {fact['value']}"
                if fact["topic"] == "service" else fact["topic"])
        worded = self._ask(QUESTION_SYSTEM, {"kind": kind, **style}, MESSAGE_SCHEMA)
        if not worded or not worded.get("message"):
            return None
        return {"kind": "question", **style, "message": worded["message"],
                "request": {"car": "", "year": 0, "part": "", "language": style["language"]},
                "row": {}, "fact": fact, "truth": {"rows": [], "nothing": False}}


def valid_case(case: dict) -> str:
    """Why a built case cannot be answered, or "" when it can. Applied by rule before a run,
    never after looking at results: a typed number with no digit is no part number. (Whether
    a product without a car is askable is the writer's judgement, not a rule here.)"""
    if case.get("kind") in ("number", "negative") and not any(
            ch.isdigit() for ch in str(case.get("typed") or "")):
        return "number without a digit"
    return ""


def _decodes_to(facts: Any, request: dict) -> bool:
    """Whether the decoder's car is the writer's car: same make, a shared model word, and
    the same year when the writer gave one. Test-harness bookkeeping only."""
    if not isinstance(facts, dict):
        return False
    facts = facts.get("vehicle") if isinstance(facts.get("vehicle"), dict) else facts
    make = str(facts.get("make") or "").casefold()
    model = str(facts.get("model") or "").casefold()
    car = str(request.get("car") or "").casefold()
    if not make or make.split()[0] not in car:
        return False
    if not model or not any(word in car for word in re.findall(r"[a-z0-9]{2,}", model)):
        return False
    year = request.get("year") or 0
    return not year or str(facts.get("model_year") or "") in ("", str(year))


# -- running and grading -------------------------------------------------------------------


def shown(payload: dict) -> list[dict]:
    """The cards of a reply: product id, stock and the engine's judgement."""
    cards = []
    for card in payload.get("cards") or []:
        if not isinstance(card, dict) or not isinstance(card.get("product"), dict):
            continue
        product = card["product"]
        view = card_row(card)
        view["id"] = str(product.get("source_product_id") or "")
        stock = product.get("stock") if isinstance(product.get("stock"), dict) else {}
        view["in_stock"] = (stock.get("store") or 0) > 0 or (stock.get("tuz11") or 0) > 0
        cards.append(view)
    return cards


def tool_calls(payload: dict) -> list[str]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"),
                                                           dict) else {}
    return [str(tool.get("name")) for tool in diagnostics.get("tools") or []]


def estimated_seconds(payload: dict) -> float:
    """A production estimate: the measured median per model request, times the requests,
    plus the tools' time."""
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"),
                                                           dict) else {}
    requests = len(diagnostics.get("requests") or [])
    tools_ms = sum(int(tool.get("ms") or 0) for tool in diagnostics.get("tools") or [])
    return round(requests * REQUEST_SECONDS + tools_ms / 1000, 2)


def converse(store: Any, models: Any, case: dict, *, turn_timeout: float = TURN_TIMEOUT,
             clock: Any = time.monotonic, sleep: Any = time.sleep) -> list[dict]:
    """The conversation of one case, up to three turns, stopping once a truth row shows."""
    truth = set(case["truth"]["rows"])
    conversation = store.create()["conversation_id"]
    turns: list[dict] = []
    message, choice = case["message"], None
    try:
        for _ in range(MAX_TURNS):
            payload, seconds = _exchange(store, conversation, message, choice, poll=0.1,
                                         turn_timeout=turn_timeout, clock=clock, sleep=sleep)
            question, options = question_of(payload)
            cards = shown(payload)
            turns.append({"say": message, "tapped": choice, "seconds": round(seconds, 2),
                          "estimated_seconds": estimated_seconds(payload),
                          "status": payload.get("status"), "outcome": payload.get("outcome"),
                          "engine_status": payload.get("engine_status"),
                          "answer": payload.get("answer") or "", "question": question,
                          "options": options, "cards": cards, "tools": tool_calls(payload),
                          "tool_calls": [
                              {key: tool.get(key) for key in ("name", "args", "rows", "ms",
                                                              "error")}
                              for tool in (payload.get("diagnostics") or {}).get("tools") or []],
                          "requests": len((payload.get("diagnostics") or {}).get("requests")
                                          or []),
                          "car_label": payload.get("car_label") or ""})
            if case["kind"] == "question" or payload.get("status") != "COMPLETE":
                break
            if any(card["id"] in truth and card["in_stock"] for card in cards):
                break
            reply = models.answer(SimpleNamespace(**_customer_row(case)), case["request"],
                                  turns, payload)
            move = next_move(reply, payload)
            if move is None:
                break
            message, choice = move
    finally:
        with contextlib.suppress(Exception):
            store.delete(conversation)
    return turns


def _customer_row(case: dict) -> dict:
    """What the customer knows: the part, and their own car. For a crawled alternative the
    customer's car is the crawled one, not the car the row's text names."""
    row = _row_object(case)
    if case.get("variant") == "crawled":
        row["vehicle"] = str(case["request"].get("car") or "")
        row["code"], row["cross_codes"] = "", ()
    return row


def _row_object(case: dict) -> dict:
    row = case.get("row") or {}
    cross = str(row.get("cross_numbers") or "")
    return {"id": row.get("id") or "", "name": row.get("name") or "",
            "vehicle": row.get("vehicle") or "", "brand": row.get("brand") or "",
            "code": row.get("number") or "", "cross_codes": tuple(
                part for part in cross.split("/") if part),
            # The customer does not see stock; the runner's row view reads it.
            "store": 1 if row.get("in_stock") else 0, "warehouse": 0}


def grade(case: dict, turns: list[dict]) -> dict:
    """The data-settled outcome of one case and the rows the data does not settle."""
    truth = set(case["truth"]["rows"])
    shown_in_stock: dict[str, dict] = {}
    for turn in turns:
        for card in turn["cards"]:
            if card["in_stock"]:
                shown_in_stock.setdefault(card["id"], card)
    found_at = next((number for number, turn in enumerate(turns, 1)
                     if any(card["id"] in truth and card["in_stock"] for card in turn["cards"])),
                    0)
    failed_turns = [turn for turn in turns if turn["status"] != "COMPLETE"]
    if case["kind"] == "question":
        ok = None  # settled by the fact check
    elif case["truth"].get("nothing"):
        ok = not any(card.get("confidence") in ("listed", "confirmed")
                     for card in shown_in_stock.values())
    else:
        ok = bool(found_at)
    undecided = [card for identifier, card in shown_in_stock.items() if identifier not in truth]
    return {"outcome_ok": ok, "found_at_turn": found_at, "turns": len(turns),
            "failed_turns": len(failed_turns), "shown": len(shown_in_stock),
            "undecided": undecided}


def check_fact(case: dict, turns: list[dict], models: Any = None) -> bool:
    """Whether the reply states the shop fact: its digits or words appear in the reply. For
    a fact the site does not hold, whether the reply invents none (no phone-like number, no
    clock time). A service is checked by a small model reading the reply."""
    if not turns:
        return False
    reply = turns[0]["answer"]
    fact = case.get("fact", {})
    if fact.get("missing"):
        return not re.search(r"\d[\d\s()+-]{6,}\d", reply) and not re.search(
            r"\b\d{1,2}[:.]\d{2}\b", reply)
    if fact.get("topic") == "service":
        if models is None:
            return False
        answer = models._asker.ask(WRITER_MODEL, SERVICE_CHECK, {
            "service": fact.get("value"), "summary": fact.get("summary"),
            "georgian_name": fact.get("title_ka"), "reply": reply}, SERVICE_SCHEMA,
            effort="low", max_tokens=2000)
        return bool((answer or {}).get("says_yes"))
    values = [fact.get("value"), fact.get("value_ka")]
    if isinstance(values[0], list):  # opening hours: every time of one range said, any order
        said = clock_times(reply)
        ranges = [clock_times(item.get("time")) for value in values if isinstance(value, list)
                  for item in value if isinstance(item, dict)]
        return any(times and times <= said for times in ranges)
    return any(states(str(value), reply) for value in values if value)


def clock_times(text: Any) -> set[tuple[int, int]]:
    """The clock times a text names, as (hour, minute): `09:30` and `9.30` are one time."""
    return {(int(hour), int(minute))
            for hour, minute in re.findall(r"\b(\d{1,2})[:.](\d{2})\b", str(text or ""))}


LATIN = str.maketrans({
    **dict(zip("აბგდევზთიკლმნოპრსტუფქყჰ", "abgdevztiklmnoprstupkqh", strict=True)),
    "ჟ": "zh", "ღ": "gh", "შ": "sh", "ჩ": "ch", "ც": "ts", "ძ": "dz", "წ": "ts", "ჭ": "ch",
    "ხ": "kh", "ჯ": "j",
    **dict(zip("абвгдезийклмнопрстуфыэ", "abvgdeziiklmnoprstufye", strict=True)),
    "ж": "zh", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ь": "",
    "ю": "yu", "я": "ya", "ё": "e"})


def latin(text: str) -> str:
    """A text with its Georgian and Cyrillic letters written in Latin ones, lower case: a
    reply in any of the customers' languages compares with a fact kept in another."""
    return str(text or "").casefold().translate(LATIN)


def states(text: str, reply: str) -> bool:
    """Whether a reply states a fact: the last six digits of a number, or half or more of
    its words, in whatever script and grammatical ending (the first six letters, compared
    in Latin letters)."""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 6:
        return digits[-6:] in re.sub(r"\D", "", reply)
    words = [latin(word)[:6] for word in re.findall(r"\w{4,}", text.casefold())]
    said = latin(reply)
    return bool(words) and sum(word in said for word in words) >= max(1, len(words) // 2)


def judge(models: Any, case: dict, rows: list[dict], turns: list[dict] | None = None
          ) -> list[dict]:
    if not rows:
        return []
    question = {"request": case["message"], "car": case["request"].get("car"),
                "conversation": [{"customer": turn["say"], "assistant": turn["answer"]}
                                 for turn in turns or []],
                "the_right_row": case.get("row"), "crawled": case.get("crawled"),
                "shown_rows": [{key: row.get(key) for key in (
                    "id", "brand", "number", "name", "vehicle", "confidence", "reason")}
                    for row in rows]}
    answer = models._asker.ask(models.judge_model, DISPUTE_SYSTEM, question, DISPUTE_SCHEMA,
                               effort="medium", max_tokens=4000)
    verdicts = {str(entry.get("id")): entry for entry in (answer or {}).get("rows") or []
                if isinstance(entry, dict)}
    return [{**row, "verdict": (verdicts.get(row["id"]) or {}).get("verdict") or "unsure",
             "note": (verdicts.get(row["id"]) or {}).get("note") or ""} for row in rows]


class Exam:
    """Runs a frozen case list against a store, case by case, resumable."""

    def __init__(self, folder: Path, store: Any, models: Any, *, client: Any = None,
                 parallel: int = 4, judge_rows: bool = True) -> None:
        self.folder = folder
        self.store, self.models, self.client = store, models, client
        self.parallel, self.judge_rows = max(1, parallel), judge_rows
        self.lock = threading.Lock()
        self.paused = threading.Event()

    def done_ids(self) -> set[str]:
        path = self.folder / "results.jsonl"
        if not path.exists():
            return set()
        done = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                done.add(json.loads(line)["id"])
        return done

    def _append(self, name: str, record: dict) -> None:
        with self.lock, (self.folder / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def one(self, case: dict) -> None:
        while self.paused.is_set():
            time.sleep(5)
        started = time.monotonic()
        limited_before = getattr(self.client, "limited_at", None)
        try:
            turns = converse(self.store, self.models, case)
        except Exception as exc:
            self._append("errors.jsonl", {"id": case["id"], "error": type(exc).__name__,
                                          "detail": str(exc)[:300]})
            return
        limited = getattr(self.client, "limited_at", None)
        if limited is not None and limited != limited_before:
            self._append("errors.jsonl", {"id": case["id"], "error": "subscription_limit"})
            self.paused.set()
            return
        if turns and all(turn["status"] != "COMPLETE" for turn in turns):
            self._append("errors.jsonl", {"id": case["id"], "error": "turn_failed",
                                          "engine_status": turns[-1].get("engine_status")})
            return
        result = grade(case, turns)
        try:
            if case["kind"] == "question":
                result["outcome_ok"] = check_fact(case, turns, self.models)
            judged = judge(self.models, case, result.pop("undecided"), turns) \
                if self.judge_rows else []
        except Exception as exc:  # A grader that fails is the harness's fault, not the bot's:
            self._append("errors.jsonl", {  # the case is run again when the exam resumes.
                "id": case["id"], "error": f"grading_{type(exc).__name__}",
                "detail": str(exc)[:300]})
            return
        record = result_record(case, result, judged, turns,
                               round(time.monotonic() - started, 1))
        (self.folder / "traces").mkdir(exist_ok=True)
        (self.folder / "traces" / f"{case['id']}.json").write_text(json.dumps(
            {"case": case, "turns": turns, "judged": judged}, ensure_ascii=False, indent=1,
            default=str), encoding="utf-8")
        self._append("results.jsonl", record)

    def run(self, cases: list[dict]) -> None:
        from parts_crawler.term import Tracker

        done = self.done_ids()
        skipped = {case["id"]: valid_case(case) for case in cases if valid_case(case)}
        if skipped:
            LOGGER.info("exam cases left out as unanswerable: %s", skipped)
        cases = [case for case in cases if case["id"] not in skipped]
        todo = [case for case in cases if case["id"] not in done]
        with Tracker("exam", len(cases), unit="cases", done=len(done),
                     logger=LOGGER) as tracker, ThreadPoolExecutor(self.parallel) as pool:
            for _ in pool.map(self._tracked(tracker), todo):
                pass

    def _tracked(self, tracker: Any) -> Any:
        def work(case: dict) -> None:
            self.one(case)
            with self.lock:
                tracker.advance() if hasattr(tracker, "advance") else tracker.update(1)
        return work


# -- reports -----------------------------------------------------------------------------


def rejudge(folder: Path, models: Any, parallel: int = 4) -> int:
    """Grade the stored conversations of a run again with the current judge (no bot turn
    is run) and rewrite its results. Returns how many cases were judged again."""
    traces = sorted((folder / "traces").glob("*.json"))

    def one(path: Path) -> dict | None:
        trace = json.loads(path.read_text(encoding="utf-8"))
        case, turns = trace["case"], trace["turns"]
        result = grade(case, turns)
        if case["kind"] == "question":
            result["outcome_ok"] = check_fact(case, turns, models)
        judged = judge(models, case, result.pop("undecided"), turns)
        trace["judged"] = judged
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        return result_record(case, result, judged, turns)

    with ThreadPoolExecutor(max(1, parallel)) as pool:
        records = [record for record in pool.map(one, traces) if record]
    with (folder / "results.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return len(records)


def regrade(folder: Path, index: Any, crawled: Any, facts: list[dict] | None = None) -> int:
    """Grade the crawled-alternative cases of a finished run again against every number the
    crawled catalogues list for the case's vehicle and part (runs built before 2026-11-11
    15:55 held only the number the case was drawn from), and the shop questions about the
    phone, address and hours against the fact in both languages (``facts``: the site's
    facts, for cases built before 2026-11-11 19:40 that hold the English value alone). No
    model is asked: a row that stays outside the truth keeps the judge's stored verdict.
    Returns the cases changed."""
    stored = {record["id"]: record for record in load_results(folder)}
    known = {fact.get("topic"): fact for fact in facts or []}
    changed = 0
    for path in sorted((folder / "traces").glob("*.json")):
        trace = json.loads(path.read_text(encoding="utf-8"))
        case, turns = trace["case"], trace["turns"]
        if case["id"] not in stored:
            continue
        fact = case.get("fact") or {}
        if case.get("kind") == "question" and fact.get("topic") in ("phone", "address", "hours"):
            if not fact.get("value_ka") and (known.get(fact["topic"]) or {}).get("value_ka"):
                fact["value_ka"] = known[fact["topic"]]["value_ka"]
            passed = check_fact(case, turns)
            if passed != stored[case["id"]].get("outcome_ok"):
                stored[case["id"]] = {**stored[case["id"]], "outcome_ok": passed,
                                      "outcome_or_judged_fit": passed}
                path.write_text(json.dumps(trace, ensure_ascii=False, indent=1,
                                           default=str), encoding="utf-8")
                changed += 1
            continue
        if case.get("variant") != "crawled":
            continue
        info = case.get("crawled") or {}
        numbers = listed_numbers(crawled, info.get("vehicle") or {}, str(info.get("number")))
        truth = sorted(crawled_truth(index, numbers) | set(case["truth"]["rows"]))
        if truth == sorted(case["truth"]["rows"]):
            continue
        info["numbers"], case["truth"]["rows"] = numbers, truth
        result = grade(case, turns)
        verdicts = {str(row.get("id")): row for row in trace.get("judged") or []}
        judged = [{**row, "verdict": (verdicts.get(str(row["id"])) or {}).get("verdict")
                   or "unsure", "note": (verdicts.get(str(row["id"])) or {}).get("note") or ""}
                  for row in result.pop("undecided")]
        trace["judged"] = judged
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        stored[case["id"]] = result_record(case, result, judged, turns,
                                           stored[case["id"]].get("seconds"))
        changed += 1
    with (folder / "results.jsonl").open("w", encoding="utf-8") as handle:
        for record in stored.values():
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return changed


def result_record(case: dict, result: dict, judged: list[dict], turns: list[dict],
                  seconds: float | None = None) -> dict:
    """One case's line in results.jsonl: its grade, the judged rows and the turn figures."""
    judged_fit = any(row["verdict"] == "fits" for row in judged)
    return {"id": case["id"], "kind": case["kind"], "variant": case.get("variant"),
            "language": case["language"], "names": case.get("names") or "", **result,
            "outcome_or_judged_fit": bool(result.get("outcome_ok")) or (
                judged_fit and not case["truth"].get("nothing")
                and case["kind"] != "question"),
            "wrong_rows": [row for row in judged if row["verdict"] == "wrong"],
            "unsure_rows": [row for row in judged if row["verdict"] == "unsure"],
            "judged": len(judged), "seconds": seconds,
            "turn_estimates": [turn["estimated_seconds"] for turn in turns],
            "tools": sorted({tool for turn in turns for tool in turn["tools"]}),
            "requests": [turn["requests"] for turn in turns]}


def load_results(folder: Path) -> list[dict]:
    path = folder / "results.jsonl"
    if not path.exists():
        return []
    results = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            results[record["id"]] = record
    return list(results.values())


def right(item: dict) -> bool:
    """A case's right outcome: the data truth shown, or a stocked row the judge says fits."""
    return bool(item.get("outcome_ok") or item.get("outcome_or_judged_fit"))


def summarize(results: list[dict]) -> dict:
    """The two headline numbers with intervals, per kind and language, and the speed."""
    def block(items: list[dict]) -> dict:
        scored = [item for item in items if item.get("outcome_ok") is not None]
        # Right (the owner's rule of 2026-11-12): a stocked row of the data truth, or a stocked
        # row the dispute judge says fits, shown within three turns. The data alone is kept.
        passed = sum(1 for item in scored if right(item))
        low, high = wilson(passed, len(scored))
        shown_rows = sum(item.get("shown") or 0 for item in items)
        wrong = sum(len(item.get("wrong_rows") or ()) for item in items)
        firm = sum(1 for item in items for row in item.get("wrong_rows") or ()
                   if row.get("confidence") in ("listed", "confirmed"))
        from_data = sum(1 for item in scored if item["outcome_ok"])
        return {"cases": len(scored), "passed": passed, "passed_from_data": from_data,
                "rate": round(passed / len(scored), 4) if scored else None,
                "rate_from_data": round(from_data / len(scored), 4) if scored else None,
                "interval": [round(low, 4), round(high, 4)], "shown_rows": shown_rows,
                "wrong_rows": wrong, "wrong_as_listed_or_confirmed": firm,
                "wrong_rate": round(wrong / shown_rows, 4) if shown_rows else 0.0,
                "unsure_rows": sum(len(item.get("unsure_rows") or ()) for item in items)}

    headline = [item for item in results if item["kind"] in HEADLINE
                and item.get("names") != "make" and not item.get("left_out")]
    estimates = [seconds for item in results for seconds in item.get("turn_estimates") or ()]
    tools = Counter(tool for item in results for tool in item.get("tools") or ())
    return {
        "all": block(headline),
        "by_kind": {kind: block([i for i in results if i["kind"] == kind])
                    for kind in sorted({i["kind"] for i in results})},
        "by_language": {language: block([i for i in results if i["language"] == language])
                        for language in sorted({i["language"] for i in results})},
        "make_only": block([i for i in results if i.get("names") == "make"]),
        "outcome_or_judged_fit": {
            "passed": sum(1 for i in headline if i.get("outcome_or_judged_fit")
                          or i.get("outcome_ok")),
            "cases": sum(1 for i in headline if i.get("outcome_ok") is not None)},
        "estimated_turn_seconds": {
            "median": round(statistics.median(estimates), 2) if estimates else None,
            "p90": round(sorted(estimates)[int(0.9 * (len(estimates) - 1))], 2)
            if estimates else None},
        "found_at_turn": dict(Counter(str(i.get("found_at_turn")) for i in headline)),
        "tool_use": {tool: count for tool, count in tools.most_common()},
    }


def compare(before: list[dict], after: list[dict]) -> dict:
    """Paired flips between two runs of the same cases."""
    old = {item["id"]: item for item in before}
    new = {item["id"]: item for item in after}
    common = sorted(set(old) & set(new))
    fixed = [i for i in common if not old[i].get("outcome_ok") and new[i].get("outcome_ok")]
    broken = [i for i in common if old[i].get("outcome_ok") and not new[i].get("outcome_ok")]
    return {"cases": len(common), "fixed": fixed, "broken": broken,
            "net": len(fixed) - len(broken)}


def print_summary(summary: dict, out: Any = sys.stdout) -> None:
    whole = summary["all"]
    out.write(f"right outcome: {whole['passed']} of {whole['cases']} = "
              f"{(whole['rate'] or 0) * 100:.1f}% (95% {whole['interval'][0] * 100:.1f}-"
              f"{whole['interval'][1] * 100:.1f}%; the data truth or a stocked row the judge "
              f"says fits)\n")
    if whole.get("passed_from_data") is not None:
        out.write(f"  from the data truth alone: {whole['passed_from_data']} of "
                  f"{whole['cases']} = {(whole['rate_from_data'] or 0) * 100:.1f}%\n")
    out.write(f"wrong rows: {whole['wrong_rows']} of {whole['shown_rows']} shown = "
              f"{whole['wrong_rate'] * 100:.1f}% ({whole['wrong_as_listed_or_confirmed']} of them "
              f"shown as listed or confirmed; unsure {whole['unsure_rows']})\n")
    for kind, item in summary["by_kind"].items():
        out.write(f"  {kind:12s} {item['passed']:4d}/{item['cases']:<4d} "
                  f"{(item['rate'] or 0) * 100:5.1f}%  wrong {item['wrong_rows']}/"
                  f"{item['shown_rows']}\n")
    for language, item in summary["by_language"].items():
        out.write(f"  [{language:8s}] {item['passed']:4d}/{item['cases']:<4d} "
                  f"{(item['rate'] or 0) * 100:5.1f}%\n")
    make_only = summary.get("make_only") or {}
    if make_only.get("cases"):
        out.write(f"  make-only requests (not in the headline): {make_only['passed']}/"
                  f"{make_only['cases']}\n")
    seconds = summary["estimated_turn_seconds"]
    out.write(f"estimated production seconds per turn: median {seconds['median']}, "
              f"p90 {seconds['p90']}\n")
    out.write(f"found at turn: {summary['found_at_turn']}; tools: {summary['tool_use']}\n")


# -- the command line ----------------------------------------------------------------------


def _setup_logging(folder: Path) -> None:
    from parts_crawler.term import configure_logging, stop_on_sigterm

    folder.mkdir(parents=True, exist_ok=True)
    configure_logging(SimpleNamespace(data_dir=folder, log_path=folder / "exam.log"))
    stop_on_sigterm()


def _app(runtime: str, audience: str, research: str = "off",
         customer: str = WRITER_MODEL) -> tuple[Any, Any, Any, Any, Any]:
    from automotive.assistant_eval import live

    client = None
    if runtime == "subscription":
        from automotive.assistant_bench import SubscriptionClient

        client = SubscriptionClient()
    app, store, index, model = live(None, audience, client=client)
    extras = getattr(store.chat_service, "assistant_extras", None)
    if isinstance(extras, dict):
        if research == "on" and extras.get("research") is not None:
            extras["research_audiences"] = ("staff", "public")  # measured in this run
        elif research == "off":
            extras.pop("research", None)
    models = Models(client or store.chat_service._client(), writer=customer,
                    judge=JUDGE_MODEL)
    return app, store, index, models, client


def _connector() -> Any:
    url = os.getenv("PARTS_CRAWLER_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    if not url:
        return None
    import psycopg

    def connect() -> Any:
        connection = psycopg.connect(url, autocommit=True)
        connection.execute("SET statement_timeout = '20s'")
        connection.execute("SET default_transaction_read_only = on")
        return connection

    return connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automotive.assistant_exam")
    parser.add_argument("command", choices=("pools", "build", "run", "report", "compare",
                                            "rejudge", "regrade"))
    parser.add_argument("--name", help="Run folder under data/assistant_eval/exam/")
    parser.add_argument("--cases", help="A frozen cases.json to run (default: the run's own)")
    parser.add_argument("--size", type=int, default=150)
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed of the draw (default: a new random seed, written down)")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--runtime", choices=("subscription", "api"), default="subscription")
    parser.add_argument("--audience", choices=("public", "staff"), default="public")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--shares", help='Kinds and shares as JSON, e.g. {"question": 1}')
    parser.add_argument("--customer-model", default=None,
                        help=f"Model that plays the customer in runs (default {CUSTOMER_MODEL}); "
                             f"builds write with {WRITER_MODEL}")
    parser.add_argument("--research", choices=("on", "off"), default="off",
                        help="Web research in the bot's turns (on the bench: subscription)")
    parser.add_argument("--against", help="compare: the other run folder")
    args = parser.parse_args(argv)
    folder = RUNS / (args.name or time.strftime("run-%Y%m%d-%H%M%S"))
    if args.command in ("report", "compare"):
        results = load_results(folder)
        if args.command == "compare":
            other = load_results(RUNS / args.against) if args.against else []
            print(json.dumps(compare(other, results), indent=1))
            return 0
        summary = summarize(results)
        (folder / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print_summary(summary)
        return 0
    _setup_logging(folder)
    if args.command == "rejudge":
        from automotive.assistant_bench import SubscriptionClient

        client = SubscriptionClient()
        try:
            count = rejudge(folder, Models(client, writer=WRITER_MODEL, judge=JUDGE_MODEL),
                            args.parallel)
        finally:
            client.close()
        summary = summarize(load_results(folder))
        (folder / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"{count} cases judged again")
        print_summary(summary)
        return 0
    customer = args.customer_model or (WRITER_MODEL if args.command == "build"
                                       else CUSTOMER_MODEL)
    app, store, index, models, client = _app(args.runtime, args.audience, args.research,
                                             customer)
    connect = _connector()
    pools = Pools(index, CrawledFitments(connect) if connect else None,
                  getattr(app.state.site, "content", None))
    try:
        if args.command == "pools":
            print(json.dumps(pools.sizes(random.Random(1)), indent=1))  # nosec B311
            return 0
        if args.command == "regrade":
            count = regrade(folder, index, pools.crawled, pools.shop_facts())
            summary = summarize(load_results(folder))
            (folder / "summary.json").write_text(json.dumps(summary, indent=1),
                                                 encoding="utf-8")
            print(f"{count} cases graded again (crawled alternatives, shop questions)")
            print_summary(summary)
            return 0
        cases_path = Path(args.cases) if args.cases else folder / "cases.json"
        if args.command == "build" or not cases_path.exists():
            seed = args.seed if args.seed is not None else random.randrange(1, 10**6)  # nosec B311
            decode = getattr(store.chat_service, "vin_service", None)
            from automotive.assistant_vehicles import load as load_readings

            settings = store.chat_service.settings
            readings = load_readings(settings.assistant_vehicle_readings_path) \
                if settings.assistant_vehicle_readings_path else {}
            names = load_readings(settings.assistant_name_readings_path) \
                if settings.assistant_name_readings_path else {}
            builder = Builder(pools, models, decode=decode.decode if decode else None,
                              vehicle_readings=readings or None, name_readings=names or None)
            shares = json.loads(args.shares) if args.shares else None
            cases = builder.build(args.size, seed, shares)
            cases_path.write_text(json.dumps({"seed": seed, "cases": cases},
                                             ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")
            LOGGER.info("exam built %s cases (seed %s): %s", len(cases), seed,
                        dict(Counter(case["kind"] for case in cases)))
            if args.command == "build":
                print(f"{len(cases)} cases, seed {seed}: "
                      f"{dict(Counter(case['kind'] for case in cases))} -> {cases_path}")
                return 0
        cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
        exam = Exam(folder, store, models, client=client, parallel=args.parallel,
                    judge_rows=not args.no_judge)
        exam.run(cases)
        summary = summarize(load_results(folder))
        (folder / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print_summary(summary)
        return 0
    finally:
        for closing in (getattr(app.state.site, "chat", None), app.state.conversations,
                        getattr(store, "search_service", None), store.chat_service, client):
            with contextlib.suppress(Exception):
                closing.close()


if __name__ == "__main__":
    sys.exit(main())
