"""What the catalogue's part names mean: the reader, the file of readings, the guide that
prints them and the command that has them read. The reader is a fake: nothing here uses
the network, a model or a database."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx

from automotive import assistant_names as names
from automotive import assistant_prompt
from automotive.assistant import catalogue_index
from automotive.assistant_catalog import NAMES_LEGEND, READ_LEGEND, CatalogIndex, index_for
from automotive.assistant_eval import Ledger, cost
from automotive.site_catalog import Catalog, CatalogStore

LINK, LINK_REAR = "სტაბ. კრონშტეინი წნ", "სტაბ. კრონშტეინი უკ"
BUSH, PAD = "სტაბ. მილისა წნ", "სამუხრუჭე ხუნდი წნ"
# The opening words the guide lists them under.
LINKS, BUSHES, PADS = "სტაბ კრონშტეინი", "სტაბ მილისა", "სამუხრუჭე ხუნდი"
IS_LINK = "stabiliser link (drop link), not the bar's bushing"
IS_BUSH = "stabiliser bar bushing, not the link"
READINGS = {LINKS: IS_LINK, BUSHES: IS_BUSH}


def row(sid, name, vehicle, brand, code, cross="", store="1"):
    """One row as site_catalog.SNAPSHOT_SQL returns it."""
    return {"source_product_id": str(sid), "product_code": f"30{sid}", "product_name": name,
            "product_model": vehicle, "original_code": code, "duplicate_code": cross,
            "brand": brand, "price_raw": "40", "stock_store": store, "stock_tuz11": "0",
            "last_observed_at": None}


ROWS = [
    *(row(number, LINK, f"TOYOTA/LEXUS CAMRY 200{number}->", "JIKIU", f"LS2100{number}",
          f"48820-0{number}010/CLT-{number}") for number in range(1, 7)),
    row(7, LINK_REAR, "HONDA CR-V 2007-", "CTR", "CLHO-38", "52320-SWA-A01"),
    row(8, LINK_REAR, "MB W212", "LEMFORDER", "35701", store="0"),
    *(row(number, BUSH, "TOYOTA/LEXUS PRADO J150", "JIKIU", f"BL211{number}",
          f"48815-603{number}") for number in (80, 81, 82)),
    row(12, PAD, "MB W204,W211,W212", "SATO", "PN0395W", "GDB1737"),
    row(13, PAD, "TOYOTA/LEXUS CAMRY 2006->", "ABS", "37677"),
]


def line_of(guide: str, opening: str) -> str:
    return next(line for line in guide.splitlines() if line.startswith(opening + " "))


class GuideTests(unittest.TestCase):
    def test_a_reading_is_printed_beside_its_name_and_the_series_hint_stays(self):
        plain = CatalogIndex.from_rows(ROWS).guide()
        read = CatalogIndex.from_rows(ROWS, readings=READINGS).guide()
        self.assertEqual(line_of(plain, LINKS), f"{LINKS} 8/7 e.g. {LINK}; JIKIU LS")
        self.assertEqual(line_of(read, LINKS),
                         f"{LINKS} 8/7 = {IS_LINK}; e.g. {LINK}; JIKIU LS")
        self.assertEqual(line_of(read, BUSHES), f"{BUSHES} 3/3 = {IS_BUSH}; e.g. {BUSH}")
        # A name without a reading is printed as it always was.
        self.assertEqual(line_of(read, PADS), line_of(plain, PADS))
        self.assertEqual(line_of(read, PADS), f"{PADS} 2/2 e.g. {PAD}")
        # The legend says what the words after = are, and only when there are any.
        self.assertIn(f"PART NAMES BY OPENING WORDS\n{NAMES_LEGEND}\n", plain)
        self.assertIn(f"PART NAMES BY OPENING WORDS\n{READ_LEGEND}\n", read)
        self.assertIn("read from the rows that carry the name", READ_LEGEND)
        self.assertNotIn(" = ", plain)
        # Nothing else of the guide follows the readings.
        self.assertEqual(read.replace(READ_LEGEND, NAMES_LEGEND)
                         .replace(f" = {IS_LINK};", "").replace(f" = {IS_BUSH};", ""), plain)

    def test_the_guide_version_follows_the_readings(self):
        plain = CatalogIndex.from_rows(ROWS)
        read = CatalogIndex.from_rows(ROWS, readings=READINGS)
        self.assertRegex(read.guide_version, r"^[0-9a-f]{12}$")
        self.assertNotEqual(read.guide_version, plain.guide_version)
        self.assertEqual(CatalogIndex.from_rows(ROWS, readings=dict(READINGS)).guide_version,
                         read.guide_version)
        changed = CatalogIndex.from_rows(ROWS, readings={**READINGS, LINKS: "stabiliser link"})
        self.assertNotIn(changed.guide_version, (read.guide_version, plain.guide_version))
        # Without a reading for a name it lists, the guide is the counted one.
        for nothing in (None, {}, {"ძრავის ზეთი": "engine oil"}, {LINKS: "  ", BUSHES: None}):
            with self.subTest(readings=nothing):
                index = CatalogIndex.from_rows(ROWS, readings=nothing)
                self.assertEqual((index.guide(), index.guide_version),
                                 (plain.guide(), plain.guide_version))

    def test_the_guide_is_printed_anew_and_the_search_stays_what_it_is(self):
        index = CatalogIndex.from_rows(ROWS)
        counted = (index.guide(), index.guide_version)
        found = index.search(make_terms=["TOYOTA"], part_terms=["სტაბ"])
        read = CatalogIndex.from_rows(ROWS, readings=READINGS)
        index.read_as(READINGS)
        self.assertEqual((index.guide(), index.guide_version), (read.guide(), read.guide_version))
        self.assertEqual(index.search(make_terms=["TOYOTA"], part_terms=["სტაბ"]), found)
        index.read_as({})
        self.assertEqual((index.guide(), index.guide_version), counted)

    def test_a_reading_stays_on_its_own_line(self):
        index = CatalogIndex.from_rows(ROWS, readings={
            LINKS: " link;\n not  the bushing ", BUSHES: "long " * 100})
        self.assertEqual(line_of(index.guide(), LINKS),
                         f"{LINKS} 8/7 = link, not the bushing; e.g. {LINK}; JIKIU LS")
        self.assertLess(len(line_of(index.guide(), BUSHES)), 180)
        self.assertEqual(len(index.guide().splitlines()),
                         len(CatalogIndex.from_rows(ROWS).guide().splitlines()))

    def test_a_reader_gets_the_names_written_under_an_opening_and_rows_that_carry_it(self):
        listed = CatalogIndex.from_rows(ROWS).part_names()
        # In the guide's order, and exactly the names the guide lists.
        self.assertEqual([entry["opening"] for entry in listed], [LINKS, BUSHES, PADS])
        links = listed[0]
        self.assertEqual(links["names"], [{"name": LINK, "rows": 6},
                                          {"name": LINK_REAR, "rows": 2}])
        # A row for each name in turn, the most written first; of a name's rows one with
        # cross numbers before one without. Six at most.
        self.assertEqual([(found.brand, found.code) for found in links["examples"]],
                         [("JIKIU", "LS21001"), ("CTR", "CLHO-38"), ("JIKIU", "LS21002"),
                          ("LEMFORDER", "35701"), ("JIKIU", "LS21003"), ("JIKIU", "LS21004")])
        self.assertEqual([len(entry["examples"]) for entry in listed], [6, 3, 2])
        self.assertEqual(CatalogIndex.from_rows([]).part_names(), [])

    def test_names_that_differ_only_in_the_car_written_after_them_are_one_name(self):
        index = CatalogIndex.from_rows([
            row(1, "გარე ღვედის დამჭიმი TOYOTA HILUX 15-", "", "DAYCO", "APV2483"),
            row(2, "გარე ღვედის დამჭიმი  MMC L200", "", "FEBEST", "0487-KB7T", "BT23065"),
            row(3, "გარე ღვედის დამჭიმი", "HYUNDAI TUCSON", "INA", "534033310"),
            row(4, "გარე ღვედის ამყოლი MB W204/W211 30x54", "", "INA", "532023410"),
            row(5, "გარე ღვედის ამყოლი (6PK) SUZUKI", "", "INA", "532057910")])
        entry = index.part_names()[0]
        # One opening can hold more than one part: the reader sees each with its weight.
        self.assertEqual((entry["opening"], entry["names"]), ("გარე ღვედის", [
            {"name": "გარე ღვედის დამჭიმი", "rows": 3},
            {"name": "გარე ღვედის ამყოლი", "rows": 2}]))
        self.assertEqual([found.code for found in entry["examples"]],
                         ["0487-KB7T", "532023410", "APV2483", "532057910", "534033310"])


class FileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "assistant" / "part_names.json"

    def test_readings_are_saved_whole_with_their_model_and_date(self):
        names.save(self.path, "claude-opus-5-5", {BUSHES: IS_BUSH, LINKS: IS_LINK})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"model": "claude-opus-5-5", "date": time.strftime("%Y-%m-%d"),
                                "readings": READINGS})
        self.assertEqual(list(data["readings"]), sorted(READINGS))
        self.assertIn(LINKS, self.path.read_text(encoding="utf-8"))  # Not escaped: readable.
        self.assertEqual(names.load(self.path), READINGS)
        # Replaced in one step: no other file is left beside it, and the application may
        # read it whoever wrote it.
        names.save(self.path, "claude-opus-5-5", {LINKS: "stabiliser link"})
        self.assertEqual(os.listdir(self.path.parent), ["part_names.json"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(names.load(self.path), {LINKS: "stabiliser link"})

    def test_a_save_that_fails_leaves_the_file_as_it_was(self):
        names.save(self.path, "claude-opus-5-5", READINGS)
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            names.save(self.path, "claude-opus-5-5", {LINKS: object()})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.path.parent), ["part_names.json"])

    def test_a_missing_or_broken_file_holds_no_reading(self):
        self.assertEqual(names.load(self.path), {})
        self.assertEqual(names.load(""), {})
        self.path.parent.mkdir(parents=True)
        for content in ("", "not json", "[1, 2]", '{"readings": ["a"]}', '{"model": "m"}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(names.load(self.path), {})

    def test_a_partial_file_gives_the_readings_it_has(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"readings": {
            LINKS: f"  {IS_LINK}. ", BUSHES: "Unclear: a rubber part of the stabiliser",
            PADS: "", "ფილტრი ჰაერის": None, "ძრავის საყრდენი": 7,
            "ამორტიზატორი წნ": " ".join(f"w{number}" for number in range(1, 20)),
            "წყლის ტუმბო": "water pump; engine coolant pump"}}, ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(names.load(self.path), {
            LINKS: IS_LINK, "ძრავის საყრდენი": "7",
            # Twelve words at most, and no semicolon: the guide parts its entries with it.
            "ამორტიზატორი წნ": "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10 w11 w12",
            "წყლის ტუმბო": "water pump, engine coolant pump"})
        # The guide prints what the file has and lists the other names as before.
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS))
        guide = index_for(store, self.path).guide()
        self.assertIn(f"{LINKS} 8/7 = {IS_LINK}; e.g. {LINK}; JIKIU LS\n", guide)
        self.assertEqual(line_of(guide, BUSHES), f"{BUSHES} 3/3 e.g. {BUSH}")
        self.assertEqual(line_of(guide, PADS), f"{PADS} 2/2 e.g. {PAD}")

    def test_the_index_reads_the_file_when_it_is_built_and_when_the_file_changes(self):
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS))
        counted = CatalogIndex.from_rows(ROWS)
        index = index_for(store, self.path)  # No file yet: the counted guide.
        self.assertEqual((index.guide(), index.guide_version),
                         (counted.guide(), counted.guide_version))
        names.save(self.path, "claude-opus-5-5", {LINKS: IS_LINK})
        self.assertIs(index_for(store, self.path), index)
        first = index.guide_version
        self.assertIn(f" = {IS_LINK}; ", index.guide())
        self.assertNotEqual(first, counted.guide_version)
        # Unchanged file: nothing is read or printed again.
        loads = []
        original = names.load
        names.load = lambda path: loads.append(path) or original(path)
        self.addCleanup(setattr, names, "load", original)
        self.assertIs(index_for(store, self.path), index)
        self.assertEqual((loads, index.guide_version), ([], first))
        # A caller that names no file leaves the guide as it is.
        self.assertEqual(index_for(store).guide_version, first)
        self.assertEqual(loads, [])
        names.save(self.path, "claude-opus-5-5", READINGS)
        self.assertIs(index_for(store, self.path), index)
        self.assertEqual(loads, [self.path])
        self.assertIn(f" = {IS_BUSH}; ", index.guide())
        self.assertNotIn(index.guide_version, (first, counted.guide_version))
        # A new snapshot is a new index, built with the readings.
        store.replace(Catalog.from_rows(ROWS[:8]))
        second = index_for(store, self.path)
        self.assertIsNot(second, index)
        self.assertIn(f"{LINKS} 8/7 = {IS_LINK}; ", second.guide())
        # The file is gone, or the setting is empty: the counted guide again.
        self.path.unlink()
        self.assertNotIn(" = ", index_for(store, self.path).guide())
        names.save(self.path, "claude-opus-5-5", READINGS)
        self.assertIn(" = ", index_for(store, self.path).guide())
        self.assertNotIn(" = ", index_for(store, "").guide())

    def test_the_assistant_reads_the_guide_with_the_readings_its_settings_name(self):
        names.save(self.path, "claude-opus-5-5", READINGS)
        store = CatalogStore("")
        store.replace(Catalog.from_rows(ROWS))
        service = SimpleNamespace(catalog_store=store, settings=SimpleNamespace(
            assistant_names_path=str(self.path)))
        guide = catalogue_index(service).guide()
        self.assertIn(f"{LINKS} 8/7 = {IS_LINK}; e.g. {LINK}; JIKIU LS\n", guide)
        blocks = assistant_prompt.system_blocks(guide, "1h")
        self.assertIn(IS_LINK, blocks[1]["text"])
        # The instructions say what such a reading is and how it is used.
        told = " ".join(assistant_prompt.SYSTEM_PROMPT.split())
        for passage in ("the words after `=` say what a name means",
                        "read from the rows that carry the name",
                        "the meaning of the name in this catalogue",
                        "match that part to the readings",
                        "is never offered in its place",
                        "A name the guide gives no reading for is settled by the numbers"):
            self.assertIn(passage, told)
        other = SimpleNamespace(catalog_store=CatalogStore(""), settings=SimpleNamespace())
        other.catalog_store.replace(Catalog.from_rows(ROWS))
        self.assertNotIn(" = ", catalogue_index(other).guide())


class Messages:
    """An Anthropic client that answers from a list and keeps what it was asked."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=answer)],
            usage=SimpleNamespace(input_tokens=50_000, output_tokens=4_000,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))


class Parser:
    """The command's way out: argparse ends the process, this one says how."""

    def exit(self, status=0, message=None):
        raise SystemExit(f"{status}: {message}")


def said(**readings):
    return json.dumps({"readings": [{"id": name, "part": part}
                                    for name, part in readings.items()]})


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "data" / "part_names.json"
        self.ledger = Path(folder.name) / "ledger.jsonl"
        self.settings = SimpleNamespace(assistant_names_path=str(self.path))
        self.index = CatalogIndex.from_rows(ROWS)

    def read(self, client, **options):
        args = SimpleNamespace(**{"all": False, "model": None, "budget": 5.0,
                                  "ledger": str(self.ledger), **options})
        out = io.StringIO()
        names.run(Parser(), args, out, index=self.index, asker=names.Asker(client),
                  settings=self.settings)
        return out.getvalue()

    def test_the_reader_is_shown_every_name_with_its_rows_and_the_readings_are_kept(self):
        # Sorted by name: n1 the pads, n2 the links, n3 the bushings.
        client = Messages(said(n1="Brake pad set", n2=f"{IS_LINK}.", n3="unclear",
                               n9="no such name"))
        printed = self.read(client)
        sent = client.requests[0]
        self.assertEqual((len(client.requests), sent["model"], sent["system"]),
                         (1, "claude-opus-5-5", names.OPENING_SYSTEM))
        self.assertEqual(sent["extra_body"]["output_config"], {
            "effort": "medium", "format": {"type": "json_schema",
                                           "schema": names.READING_SCHEMA}})
        case = json.loads(sent["messages"][0]["content"])
        self.assertEqual(case["all_names"], [LINKS, BUSHES, PADS])
        self.assertEqual([(entry["id"], entry["name"]) for entry in case["names"]],
                         [("n1", PADS), ("n2", LINKS), ("n3", BUSHES)])
        self.assertEqual(case["names"][1]["written_as"], [{"name": LINK, "rows": 6},
                                                          {"name": LINK_REAR, "rows": 2}])
        self.assertEqual(case["names"][1]["example_rows"][:2], [
            {"name": LINK, "vehicle": "TOYOTA/LEXUS CAMRY 2001->", "brand": "JIKIU",
             "number": "LS21001", "cross_numbers": "48820-01010/CLT-1"},
            {"name": LINK_REAR, "vehicle": "HONDA CR-V 2007-", "brand": "CTR",
             "number": "CLHO-38", "cross_numbers": "52320-SWA-A01"}])
        self.assertEqual(len(case["names"][1]["example_rows"]), 6)
        # Only catalogue rows reach the reader: no price, no stock, no id.
        self.assertEqual({key for entry in case["names"] for found in entry["example_rows"]
                          for key in found},
                         {"name", "vehicle", "brand", "number", "cross_numbers"})
        # An unclear name gets no reading; the file names the model and the day.
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"model": "claude-opus-5-5", "date": time.strftime("%Y-%m-%d"),
                                "readings": {PADS: "Brake pad set", LINKS: IS_LINK}})
        spent = cost("claude-opus-5-5", {"input_tokens": 50_000, "output_tokens": 4_000})
        self.assertIn("Reading 3 of the guide's 3 part names with claude-opus-5-5", printed)
        self.assertIn(f"2 readings written, 1 names stayed unclear, 2 in the file "
                      f"({self.path}); ${spent:.2f} spent", printed)
        self.assertAlmostEqual(Ledger(self.ledger).total(), spent, places=4)
        entry = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual((entry["model"], entry["what"]),
                         ("claude-opus-5-5", "read-part-names:3 names"))

    def test_only_the_names_without_a_reading_are_read_unless_all_are_asked_for(self):
        names.save(self.path, "claude-opus-5-5", {LINKS: IS_LINK, "ძველი სახელი": "old part"})
        client = Messages(said(n1="brake pad set", n2=IS_BUSH))
        self.read(client, model="claude-sonnet-5-5")
        case = json.loads(client.requests[0]["messages"][0]["content"])
        self.assertEqual([entry["name"] for entry in case["names"]], [PADS, BUSHES])
        self.assertEqual(case["all_names"], [LINKS, BUSHES, PADS])
        self.assertEqual(client.requests[0]["model"], "claude-sonnet-5-5")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # What was read before is kept, also for a name the guide no longer lists.
        self.assertEqual((data["model"], data["readings"]), ("claude-sonnet-5-5", {
            LINKS: IS_LINK, BUSHES: IS_BUSH, PADS: "brake pad set", "ძველი სახელი": "old part"}))
        # Everything has a reading: nothing is asked and nothing is spent.
        spent = Ledger(self.ledger).total()
        idle = Messages()
        self.assertIn("Every one of the guide's 3 part names has a reading", self.read(idle))
        self.assertEqual((idle.requests, Ledger(self.ledger).total()), ([], spent))
        # --all reads every name anew and keeps only what the reader says now.
        again = Messages(said(n1="brake pads", n2="stabiliser link", n3="unclear"))
        self.read(again, all=True)
        self.assertEqual(len(json.loads(again.requests[0]["messages"][0]["content"])["names"]),
                         3)
        self.assertEqual(names.load(self.path), {PADS: "brake pads", LINKS: "stabiliser link"})

    def test_a_reader_that_fails_leaves_the_file_as_it_was(self):
        names.save(self.path, "claude-opus-5-5", {LINKS: IS_LINK})
        before = self.path.read_bytes()
        down = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        for answer in (down, "no json at all"):
            with self.subTest(answer=answer), self.assertRaises(SystemExit) as stopped:
                self.read(Messages(answer))
            self.assertIn("1: 1 of the reader's answers did not come; the file was left as it "
                          "was", str(stopped.exception))
            self.assertEqual(self.path.read_bytes(), before)
        # What the failed answer cost is in the ledger all the same.
        self.assertAlmostEqual(Ledger(self.ledger).total(), cost(
            "claude-opus-5-5", {"input_tokens": 50_000, "output_tokens": 4_000}), places=4)

    def test_one_failed_answer_of_several_keeps_all_of_them_out_of_the_file(self):
        letters = "აბგდევზთიკლმნოპ"
        rows = [row(number, f"ნაწილ{letters[number // 15]}{letters[number % 15]} სახელი",
                    "MB W212", "SATO", f"PN{number}")
                for number in range(1, names.OPENINGS_PER_READING + 6)]
        self.index = CatalogIndex.from_rows(rows)
        self.assertEqual(len(self.index.part_names()), names.OPENINGS_PER_READING + 5)

        first = min(entry["opening"] for entry in self.index.part_names())

        class Half(Messages):
            """Answers every question but the one that holds the first name."""

            def create(self, **request):
                asked = json.loads(request["messages"][0]["content"])["names"]
                text = "junk" if any(entry["name"] == first for entry in asked) \
                    else said(**{entry["id"]: "a part" for entry in asked})
                self.requests.append(request)
                return SimpleNamespace(
                    stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                          cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0))

        client = Half()
        with self.assertRaises(SystemExit):
            self.read(client)
        self.assertEqual(len(client.requests), 2)
        self.assertFalse(self.path.exists())

    def test_nothing_is_read_past_the_budget_or_without_a_file_to_write(self):
        Ledger(self.ledger).append("claude-opus-5-5", "earlier", 5.0)
        client = Messages()
        with self.assertRaises(SystemExit) as stopped:
            self.read(client)
        self.assertIn("2: Nothing was read: $5.00 of the $5.00 budget is already spent",
                      str(stopped.exception))
        self.settings.assistant_names_path = ""
        with self.assertRaises(SystemExit) as stopped:
            self.read(client, budget=50.0)
        self.assertIn("AUTOMOTIVE_ASSISTANT_NAMES_PATH is empty", str(stopped.exception))
        self.assertEqual((client.requests, self.path.exists()), ([], False))


class ReaderTests(unittest.TestCase):
    def test_a_reading_is_one_line_of_twelve_words_or_nothing(self):
        self.assertEqual(names.clean(f"  {IS_LINK}.\n"), IS_LINK)
        self.assertEqual(names.clean("a; b"), "a, b")
        self.assertEqual(names.clean(" ".join(["word"] * 30)), " ".join(["word"] * 12))
        # A reading that is too long loses its unfinished last item, not half a phrase.
        self.assertEqual(names.clean("Valve cover gasket, gasket set or spark plug tube "
                                     "seal, not the valve cover"),
                         "Valve cover gasket, gasket set or spark plug tube seal")
        self.assertEqual(names.clean("Front strut top mount, dust boot kit, top mount "
                                     "bearing or bump stop"),
                         "Front strut top mount, dust boot kit, top mount bearing")
        self.assertEqual(names.clean("one two three four five six seven eight nine (ten, "
                                     "eleven or twelve thirteen)"),
                         "one two three four five six seven eight nine")
        self.assertEqual(names.clean("Rear shock absorber, its top mount, bump stop, dust "
                                     "boot or bush"),
                         "Rear shock absorber, its top mount, bump stop, dust boot or bush")
        for nothing in ("", "  ", None, "unclear", "Unclear.", "UNCLEAR: perhaps a clip"):
            self.assertEqual(names.clean(nothing), "", nothing)

    def test_the_readers_instructions_name_no_part_of_this_catalogue(self):
        # The readings are the model's: the instructions give it the form, not an answer.
        told = names.OPENING_SYSTEM.casefold()
        for word in ("link", "bushing", "pad", "filter", "bearing"):
            self.assertNotIn(word, told.replace("stabiliser (anti-roll", ""))
        self.assertIn('"x, not y"', told)
        self.assertIn('"x, y or z"', told)
        self.assertIn("at most 12 words", told)
        self.assertIn('answer\n"unclear"', told)

    def test_batches_are_asked_side_by_side_and_a_failed_one_is_counted(self):
        examples = {f"name {number:03d}": [] for number in range(names.NAMES_PER_READING + 1)}
        client = Messages(said(n1="first"), "junk")
        readings, failed = names.read_names(names.Asker(client), "claude-opus-5-5", examples,
                                            workers=4)
        self.assertEqual((len(client.requests), failed, len(readings)), (2, 1, 1))
        # The questions are of even size: no question for one left-over name.
        self.assertEqual(sorted(len(json.loads(sent["messages"][0]["content"])["names"])
                                for sent in client.requests), [30, 31])
        self.assertEqual(names.read_names(names.Asker(Messages()), "m", {}), ({}, 0))


if __name__ == "__main__":
    unittest.main()
