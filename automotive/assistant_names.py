"""What the catalogue's part names mean, read by a model from the rows that carry them.

The shop names its parts in its own words. A name is not always what its literal translation
says, and the chat's model cannot be expected to know it. So a reader model is shown each
name with rows that carry it (vehicle texts, brands, part numbers) and says in plain English
what the part is. The readings of the guide's part names are kept in a JSON file and printed
beside the names in the catalogue guide; the evaluation uses the same reader for the rows
its customers are written from.

Nothing here is written by hand, and no customer data is involved: only catalogue rows.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

import anthropic

READER_MODEL = "claude-opus-5-5"
NAMES_PER_READING = 60  # part names in one question to the reader
READING_WORDS = 12  # of a reading printed in the guide
READING_CHARS = 300
READING_TOKENS = 12000  # the most one answer of the reader may take, thinking included
# The guide's names are weighed against their neighbours: fewer in one question, and more
# room for the answer.
OPENINGS_PER_READING, OPENING_TOKENS = 40, 16000
MODEL_SECONDS = 120.0
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens",
         "cache_creation_input_tokens")
UNCLEAR = "unclear"

ROW_READING = """\
HOW A ROW OF THE STOCK LIST IS WRITTEN
The list is typed by shop staff over years and heavily abbreviated.
- name: what the part is, in Georgian. Position and side are two letters: წნ front, უკ rear,
  მჯ right, მც left, ზდ upper, ქვ lower, შდ inner, გრ outer. სტაბ is the stabiliser (anti-roll
  bar), ამორტ the shock absorber.
- vehicle: the cars the part fits, in shop shorthand: a make or a group of makes, then models,
  chassis or generation codes, engine codes or sizes, and years. `2006->`, `06-` and `2011>`
  mean from that year on, `01-06` means from-to, and a mark before a year (`>2009`, `-> 2000`)
  is ambiguous: up to that year on some rows, from it on others. One row often lists several
  cars. An empty or universal vehicle text means the product is not tied to a car.
- brand, number, cross_numbers: who made the part, its part number, and the numbers of the
  same part at the car maker and at other brands.
"""

READER_SYSTEM = """\
You read part names from the stock list of a car-parts shop in Georgia, for a test of the shop's
chat. A weaker model will play customers who need these parts and must know exactly what each
part is.

""" + ROW_READING + """
For every name you are given, say in plain English what the part is: the precise trade term for
the part, then its position and side, then any size, grade or specification the name states.
The words of this list are not always the dictionary's: use the example rows of each name, their
vehicle texts and above all their part numbers, to settle what an abbreviated or ambiguous word
means here, for example whether a word names the part itself or something that belongs to it.
One line per name, no car, no brand, no number. When a name does not tell what the part is, say
what can be told and that the rest is unclear. Use the ids exactly as given.
"""

OPENING_SYSTEM = """\
You read the part names of the stock list of a car-parts shop in Georgia. Your readings are
printed beside the names in a guide for the shop's chat assistant. The assistant must match
what a customer asks for to the right name, and it does not know the shop's own words.

""" + ROW_READING + """
You get the opening words of the list's part names. Each comes with `written_as`, the names
written most often under it with the number of rows that carry each (the car, size or number
that a name ends in is cut off), and with example rows, each under its own name; `all_names`
lists every opening of the guide, so that you see which names stand next to each other.
For each opening say in plain English what the part is, in at most 12 words, the precise trade
term first and, where the trade has a second everyday word for it, that word in brackets. The
names under one opening are not always one part: when they are several, name them all, the one
with most rows first, in the form "X, Y or Z", and do not choose one of them. When a
neighbouring name, under another opening, is easily taken for this one (the part itself and a
part that belongs to it, two parts of one assembly, a part and its mounting), add what this one
is not, in the form "X, not Y". Never say "not Y" of a part that a name under the opening is.
The words of this list are not always the dictionary's. Settle what a word means here from the
example rows, above all from their part numbers: car makers number each kind of part in its own
group, and aftermarket brands give each kind its own series. Read the rows; do not translate
the name.
No car, no brand and no number in a reading; a position or a side only when every name under
the opening states the same one. When the rows do not let you tell what the part is, answer
"unclear" for that id. Use the ids exactly as given.
"""


def object_schema(properties: dict) -> dict:
    """The JSON schema of an object that has exactly these properties."""
    return {"type": "object", "additionalProperties": False, "required": list(properties),
            "properties": properties}


READING_SCHEMA = object_schema({"readings": {"type": "array", "items": object_schema({
    "id": {"type": "string"}, "part": {"type": "string"}})}})


def json_object(text: str) -> dict | None:
    """The JSON object of a reply, also when words stand around it."""
    for piece in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            value = json.loads(piece)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class Asker:
    """One client for questions that are answered as one JSON object, with what each model
    used. Safe to call from several threads."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.usage: dict[str, Counter] = defaultdict(Counter)
        self._lock = threading.Lock()
        self._plain: set[str] = set()  # models that refused a response format

    def used(self) -> dict[str, dict]:
        """What each model was asked and answered so far, in tokens."""
        with self._lock:
            return {model: dict(usage) for model, usage in self.usage.items()}

    def ask(self, model: str, system: str, case: dict, schema: dict, *, effort: str,
            max_tokens: int) -> dict | None:
        """One question to a model. None when it could not be asked, the model declined or
        the answer is no object."""
        for _ in range(2):
            config: dict = {"effort": effort}
            instructions = system
            if model in self._plain:
                instructions += ("\nReply with one JSON object and nothing else, matching "
                                 "this JSON schema:\n" + json.dumps(schema))
            else:
                config["format"] = {"type": "json_schema", "schema": schema}
            try:
                response = self.client.messages.create(
                    model=model, max_tokens=max_tokens, system=instructions,
                    messages=[{"role": "user",
                               "content": json.dumps(case, ensure_ascii=False)}],
                    extra_body={"output_config": config}, timeout=MODEL_SECONDS)
            except anthropic.BadRequestError:
                if model in self._plain:
                    break
                self._plain.add(model)  # Asked once more for JSON in plain text.
                continue
            except anthropic.APIError:
                break
            usage = getattr(response, "usage", None)
            with self._lock:
                self.usage[model]["requests"] += 1
                for key in USAGE:
                    self.usage[model][key] += int(getattr(usage, key, 0) or 0)
            if getattr(response, "stop_reason", None) == "refusal":
                return None
            return json_object("".join(
                str(getattr(block, "text", "") or "")
                for block in getattr(response, "content", None) or []
                if getattr(block, "type", "") == "text"))
        with self._lock:
            self.usage[model]["failed"] += 1
        return None


def example_row(row: Any) -> dict:
    """What a reader sees of a row that carries a name."""
    return {"vehicle": row.vehicle, "brand": row.brand, "number": row.code,
            "cross_numbers": "/".join(row.cross_codes or ())}


def named_row(row: Any) -> dict:
    """The same with the row's own name: rows under one opening carry different names."""
    return {"name": row.name, **example_row(row)}


def read_names(asker: Asker, model: str, examples: dict[str, list[Any]], *,
               system: str = READER_SYSTEM, written: dict[str, list] | None = None,
               context: dict | None = None, show: Any = example_row, workers: int = 1,
               batch: int = NAMES_PER_READING,
               max_tokens: int = READING_TOKENS) -> tuple[dict[str, str], int]:
    """What each name means in plain English, read by a model from the name and the rows
    that carry it, at most ``batch`` names in one question and the questions of even size.
    ``written`` adds to a name the names written under it, ``context`` is given with every
    question and ``show`` says what the reader sees of a row. Returns the readings and how
    many questions got no answer; a name the reader said nothing of is left out."""
    names = sorted(examples)
    questions = max(1, math.ceil(len(names) / batch))
    size = max(1, math.ceil(len(names) / questions))
    batches = [names[start:start + size] for start in range(0, len(names), size)]

    def one(batch: list[str]) -> dict[str, str] | None:
        case = {**(context or {}), "names": [{
            "id": f"n{place}", "name": name,
            **({"written_as": written[name]} if written and written.get(name) else {}),
            "example_rows": [show(row) for row in examples[name]]}
            for place, name in enumerate(batch, 1)]}
        answer = asker.ask(model, system, case, READING_SCHEMA, effort="medium",
                           max_tokens=max_tokens)
        if answer is None:
            return None
        found: dict[str, str] = {}
        for item in answer.get("readings") or []:
            name = str(item.get("id") or "") if isinstance(item, dict) else ""
            part = " ".join(str(item.get("part") or "").split())[:READING_CHARS] \
                if isinstance(item, dict) else ""
            if name[:1] == "n" and name[1:].isdigit() and 1 <= int(name[1:]) <= len(batch) \
                    and part:
                found[batch[int(name[1:]) - 1]] = part
        return found

    if workers <= 1 or len(batches) <= 1:
        answers = [one(batch) for batch in batches]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(batches)),
                                thread_name_prefix="name-reader") as pool:
            answers = list(pool.map(one, batches))
    readings: dict[str, str] = {}
    for answer in answers:
        readings.update(answer or {})
    return readings, sum(1 for answer in answers if answer is None)


# -- the readings of the guide's part names ----------------------------------------------------


def clean(reading: Any) -> str:
    """A reading as the guide prints it: one line of at most twelve words, or empty when
    the reader could not tell. A longer one loses the item that the twelfth word cuts in
    the middle, not half of it. The guide parts its entries with semicolons, so a reading
    holds none."""
    words = str(reading or "").replace(";", ",").split()
    text = " ".join(words[:READING_WORDS])
    if len(words) > READING_WORDS:
        cut = max(text.rfind(","), text.rfind(" or "))
        text = text[:cut] if cut > 0 else text
        if text.count("(") > text.count(")"):
            text = text[:text.rfind("(")]
    text = text.strip(" .,")
    return "" if not text or text.casefold().startswith(UNCLEAR) else text


def load(path: Path | str) -> dict[str, str]:
    """The readings a file holds, by opening words. A file that is missing or is not what
    ``save`` writes holds none."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    readings = data.get("readings") if isinstance(data, dict) else None
    if not isinstance(readings, dict):
        return {}
    return {str(name): text for name, reading in readings.items() if (text := clean(reading))}


def save(path: Path | str, model: str, readings: dict[str, str]) -> None:
    """Write the readings with the model that made them and the date. The file is replaced
    in one step, so a reader of it never sees half of it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"model": model, "date": time.strftime("%Y-%m-%d"),
                       "readings": dict(sorted(readings.items()))},
                      ensure_ascii=False, indent=1)
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        # No secret is in it, and the application may run as another user than the one
        # who had the names read.
        os.chmod(name, 0o644)
        os.replace(name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def read_openings(asker: Asker, model: str, entries: list[dict], every: list[str],
                  workers: int = 4) -> tuple[dict[str, str], int, int]:
    """Readings for the guide's part names. ``entries`` are what ``CatalogIndex.part_names``
    returns for the names to read, ``every`` all the guide's openings. Returns the readings
    that say what a part is, how many names stayed unclear, and how many batches failed."""
    examples = {entry["opening"]: entry["examples"] for entry in entries}
    written = {entry["opening"]: entry["names"] for entry in entries}
    found, failed = read_names(asker, model, examples, system=OPENING_SYSTEM, written=written,
                               context={"all_names": every}, show=named_row, workers=workers,
                               batch=OPENINGS_PER_READING, max_tokens=OPENING_TOKENS)
    readings = {name: text for name, reading in found.items() if (text := clean(reading))}
    return readings, len(entries) - len(readings), failed


def run(parser: Any, args: Any, out: TextIO, *, index: Any = None, asker: Asker | None = None,
        settings: Any = None) -> None:
    """The ``--read-part-names`` command: read the guide's part names that have no reading
    yet (all of them with ``--all``) and store the readings. The tests pass the index, the
    asker and the settings; the command builds them."""
    from automotive.assistant_eval import DEFAULT_BUDGET, DEFAULT_LEDGER, Ledger, cost

    if settings is None:
        from automotive.config import Settings

        settings = Settings.from_env()
    path = str(getattr(settings, "assistant_names_path", "") or "")
    if not path:
        parser.exit(2, "AUTOMOTIVE_ASSISTANT_NAMES_PATH is empty: there is no file to "
                       "write the readings to.\n")
    ledger = Ledger(args.ledger or os.getenv("AUTOMOTIVE_EVAL_LEDGER") or DEFAULT_LEDGER)
    budget = DEFAULT_BUDGET if args.budget is None else float(args.budget)
    before = ledger.total()
    if before >= budget:
        parser.exit(2, f"Nothing was read: ${before:.2f} of the ${budget:.2f} budget is "
                       f"already spent ({ledger.path}).\n")
    if index is None:
        from automotive.assistant_catalog import index_for
        from automotive.site_catalog import CatalogStore

        store = CatalogStore(settings.database_url)
        store.refresh()
        index = index_for(store)
    names = index.part_names()
    known = {} if getattr(args, "all", False) else load(path)
    todo = [entry for entry in names if entry["opening"] not in known]
    if not todo:
        out.write(f"Every one of the guide's {len(names)} part names has a reading "
                  f"({path}). Use --all to read them again.\n")
        return
    if asker is None:
        headers = {}
        if getattr(settings, "anthropic_workspace_id", ""):
            headers["anthropic-workspace-id"] = settings.anthropic_workspace_id
        asker = Asker(anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                          default_headers=headers, timeout=MODEL_SECONDS,
                                          max_retries=2))
    model = args.model or READER_MODEL
    out.write(f"Reading {len(todo)} of the guide's {len(names)} part names with {model}\n")
    out.flush()
    readings: dict[str, str] = {}
    unclear = failed = 0
    try:
        readings, unclear, failed = read_openings(
            asker, model, todo, [entry["opening"] for entry in names])
    finally:
        spent = sum(cost(name, usage) for name, usage in asker.used().items())
        ledger.append(model, f"read-part-names:{len(todo)} names", spent)
    if failed:
        parser.exit(1, f"{failed} of the reader's answers did not come; the file was left "
                       f"as it was ({path}). ${spent:.2f} spent.\n")
    kept = {**known, **readings}
    save(path, model, kept)
    out.write(f"{len(readings)} readings written, {unclear} names stayed unclear, "
              f"{len(kept)} in the file ({path}); ${spent:.2f} spent, "
              f"ledger ${ledger.total():.2f} of ${budget:.2f}\n")
    out.flush()
