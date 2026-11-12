"""What the catalogue's vehicle texts and part names mean, read once by a model, kept as data.

The shop's rows name their cars in staff shorthand: a make or a group of makes, models,
chassis or generation codes, engines and years, abbreviated and run together. Reading that
shorthand inside every chat turn is where the assistant goes wrong most often: a start year
taken for a span, a related model taken for a generation of another, a model without a
generation taken for one. A stronger model reads each distinct text once, with a few rows
that carry it, and says which cars, generations and years it names. Search results carry the
readings of their rows' texts (``vehicle_readings``); they are advice, and the row's own text
decides. No rule of the code says what a text means: the readings are the model's.

    uv run python -m automotive.assistant_vehicles [--names] [--all] [--workers 4] [--model ID]

With ``--names`` the part names are read the same way (``AUTOMOTIVE_NAME_READINGS_PATH``):
what the part is and every position its marks state, for a bush or mount which arm or beam
and which of its bushes; search results carry them as ``name_readings``.

Runs on the Claude subscription of the logged-in Claude Code (nothing is billed to the API),
keeps the readings in ``AUTOMOTIVE_VEHICLE_READINGS_PATH``, saves after every chunk, continues
where it stopped, logs to ``vehicle_readings.log`` beside the file and shows a progress bar.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from automotive.assistant_names import ROW_READING, Asker, read_names, save

LOGGER = logging.getLogger(__name__)
MODEL = "claude-opus-5-5"
TEXTS_PER_READING = 40  # vehicle texts in one question to the reader
CHUNK = 400  # texts read between two saves
EXAMPLES = 2  # rows shown with a text
READING_WORDS = 40
READING_TOKENS = 16000
NO_CAR = "no car"

VEHICLE_SYSTEM = """\
You read the vehicle texts of the stock list of a car-parts shop in Georgia. Your readings are
shown to the shop's chat assistant beside its search results, so that it knows which cars a
row is for. The assistant knows cars well but misreads the shop's shorthand.

""" + ROW_READING + """
For each vehicle text you get the text and a few rows that carry it. Say in plain English which
cars the text names, one after another, separated by "; ":
- each make and model in full, then its generation or chassis code and the years: the ones the
  text states, and where the text gives only a start year or a code, the generation that year or
  code belongs to, with that generation's years as you know them;
- an engine, a body or a drive only when the text states one;
- for a model the text names without any generation, chassis code or year, write "generation
  not stated" for it, and never choose one;
- a model whose name adds a word to another model's (a Sport, Grand, Verso, iO or Coupe
  version) is a separate model unless the text says otherwise: name it as written, never as a
  generation of the other;
- a year mark that can be read two ways (`>2009`) is said to be ambiguous.
Use the rows' part numbers to settle a generation where they can. Say only what the text and
the rows support or what is well known about these cars; when you do not know a generation's
years, give what the text states and say the rest is unclear. At most 40 words, no part, brand
or number. When the text names no car (empty, universal, a part number), answer "no car". Use
the ids exactly as given.
"""


class SafeAsker(Asker):
    """An Asker whose failed request (a bench process that ended without an answer, the
    subscription's usage limit) counts as a question without an answer: its texts stay
    unread and are read when the job runs again, instead of the job ending."""

    def ask(self, *args: Any, **kwargs: Any) -> dict | None:
        try:
            return super().ask(*args, **kwargs)
        except (RuntimeError, TimeoutError, OSError) as exc:
            LOGGER.warning("vehicle reading failed: %s", type(exc).__name__)
            return None


NAME_SYSTEM = """\
You read the part names of the stock list of a car-parts shop in Georgia. Your readings are
shown to the shop's chat assistant beside its search results, so that it knows exactly which
part a row is. The assistant knows parts well but misreads the shop's abbreviations, above all
the position marks.

""" + ROW_READING + """
For each part name you get the name and a few rows that carry it. Say in plain English what
the part is: the precise trade term first, then every position the name states (the axle, the
level, the side, inner or outer) and, for a bush, mount or joint of an arm or a beam, which arm
or beam it belongs to and which of its bushes it is, then any size, grade or specification the
name states. When a mark can be read two ways, say so. Use the rows' part numbers and vehicles
to settle what the name means where they can. At most 30 words; no car, brand or number. When
the name does not tell what the part is, answer "unclear". Use the ids exactly as given.
"""


def name_example(row: Any) -> dict:
    """What the reader sees of a row that carries a part name: the car and the numbers."""
    return {"vehicle": row.vehicle, "brand": row.brand, "number": row.code,
            "cross_numbers": "/".join((row.cross_codes or ())[:4])}


def clean(reading: Any) -> str:
    """A reading as the search results carry it: one line of at most forty words, or empty
    when the reader could not tell or the text names no car."""
    words = str(reading or "").split()
    text = " ".join(words[:READING_WORDS]).strip(" .")
    lowered = text.casefold()
    if not text or lowered.startswith("unclear") or lowered.rstrip(".") == NO_CAR:
        return ""
    return text


def load(path: Path | str) -> dict[str, str]:
    """The readings a file holds, by vehicle text. A missing or unreadable file holds none."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    readings = data.get("readings") if isinstance(data, dict) else None
    if not isinstance(readings, dict):
        return {}
    found: dict[str, str] = {}
    for text, value in readings.items():
        reading = clean(value)
        if reading:
            found[str(text)] = reading
    return found


def example_row(row: Any) -> dict:
    """What the reader sees of a row that carries a text: the part and its numbers."""
    return {"name": row.name, "brand": row.brand, "number": row.code,
            "cross_numbers": "/".join((row.cross_codes or ())[:4])}


def texts_of(rows: Any, in_stock: Any = None, field: str = "vehicle") -> dict[str, list[Any]]:
    """The distinct vehicle texts (or part names, with ``field="name"``) of these rows, most
    rows first, each with a few rows that carry it (rows in stock first). With ``in_stock``,
    only texts that a row in stock has."""
    count: Counter = Counter()
    carriers: dict[str, list[Any]] = {}
    for row in rows:
        text = (getattr(row, field, "") or "").strip()
        if not text:
            continue
        count[text] += 1
        carriers.setdefault(text, []).append(row)
    texts: dict[str, list[Any]] = {}
    for text, _ in count.most_common():
        rows_of = carriers[text]
        if in_stock is not None:
            stocked = [row for row in rows_of if in_stock(row)]
            if not stocked:
                continue
            rows_of = stocked + [row for row in rows_of if not in_stock(row)]
        texts[text] = rows_of[:EXAMPLES]
    return texts


def read(asker: Asker, model: str, examples: dict[str, list[Any]],
         workers: int = 4, *, names: bool = False) -> tuple[dict[str, str], int]:
    """Readings of these texts (part names with ``names``) and how many questions got no
    answer. A text the reader said nothing of, or said names no car, is left out."""
    found, failed = read_names(asker, model, examples,
                               system=NAME_SYSTEM if names else VEHICLE_SYSTEM,
                               show=name_example if names else example_row, workers=workers,
                               batch=TEXTS_PER_READING, max_tokens=READING_TOKENS)
    return {text: reading for text, value in found.items() if (reading := clean(value))}, failed


def run(argv: list[str] | None = None, *, rows: Any = None, client: Any = None) -> int:
    """Read the texts that have no reading yet (all of them with --all) and keep the file up
    to date after every chunk. ``rows`` and ``client`` replace the catalogue and the
    subscription client in tests."""
    from parts_crawler.term import Tracker, configure_logging, stop_on_sigterm

    parser = argparse.ArgumentParser(prog="python -m automotive.assistant_vehicles")
    parser.add_argument("--all", action="store_true",
                        help="Read every text again, not only those without a reading")
    parser.add_argument("--everything", action="store_true",
                        help="Also the texts that no row in stock carries")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--out", help="The readings file (default: the setting)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Read at most this many texts (the ones with most rows first)")
    parser.add_argument("--names", action="store_true",
                        help="Read the part names instead of the vehicle texts")
    args = parser.parse_args(argv)
    from automotive.assistant_eval import in_stock
    from automotive.config import Settings

    settings = Settings.from_env()
    kind = "name readings" if args.names else "vehicle readings"
    target = Path(args.out or (
        (settings.assistant_name_readings_path or "data/assistant/name_readings.json")
        if args.names else (settings.assistant_vehicle_readings_path
                            or "data/assistant/vehicle_readings.json")))
    target.parent.mkdir(parents=True, exist_ok=True)
    if rows is None:
        configure_logging(SimpleNamespace(data_dir=target.parent,
                                          log_path=target.with_suffix(".log")))
        stop_on_sigterm()
        rows = _catalogue_rows(settings)
    texts = texts_of(rows, None if args.everything else in_stock,
                     "name" if args.names else "vehicle")
    kept = {} if args.all else _stored(target)
    todo = {text: examples for text, examples in texts.items() if text not in kept}
    if args.limit > 0:
        todo = dict(list(todo.items())[:args.limit])
        texts = {text: texts[text] for text in [*kept, *todo] if text in texts}
    own = client is None
    if own:
        from automotive.assistant_bench import SubscriptionClient

        client = SubscriptionClient()
    asker = SafeAsker(client)
    failed = 0
    try:
        names = list(todo)
        with Tracker(kind, len(texts), unit="texts",
                     done=len(texts) - len(todo), logger=LOGGER) as tracker:
            for start in range(0, len(names), CHUNK):
                chunk = {text: todo[text] for text in names[start:start + CHUNK]}
                found, missed = read(asker, args.model, chunk, workers=args.workers,
                                     names=args.names)
                failed += missed
                kept.update(found)
                save(target, args.model, kept)
                tracker.advance(len(chunk))
    finally:
        if own:
            client.close()
    LOGGER.info("%s: %d kept in %s, %d questions without an answer",
                kind, len(kept), target, failed)
    print(f"{len(kept)} readings in {target}; {failed} questions got no answer")
    return 0 if not failed else 1


def _stored(path: Path) -> dict[str, str]:
    """The raw readings already in the file, kept as they are when the job continues."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    readings = data.get("readings") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in readings.items()} if isinstance(readings, dict) else {}


def _catalogue_rows(settings: Any) -> list[Any]:
    """The live catalogue's rows, as the assistant searches them."""
    from dataclasses import replace

    from automotive.api import create_app
    from automotive.assistant import catalogue_index

    app = create_app(settings=replace(settings, chat_engine="assistant", chat_log_path="",
                                      site_chat_log_path=""))
    app.state.site.catalog_store.refresh()
    index = catalogue_index(app.state.site.chat.store.chat_service)
    if index is None:
        raise SystemExit("The catalogue could not be loaded.")
    return list(index.rows())


if __name__ == "__main__":
    sys.exit(run())
