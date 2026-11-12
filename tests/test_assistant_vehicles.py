"""The vehicle-text readings: what the reader is asked, what is kept, the file, the job's
resume, and the readings beside search results. A fake model, no network."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from automotive import assistant_vehicles as vehicles
from automotive.assistant_names import Asker
from automotive.assistant_tools import Toolbox
from tests.test_assistant_catalog import INDEX
from tests.test_assistant_names import Messages, said


def row(vehicle: str, store: int = 1, name: str = "pad", code: str = "X1") -> SimpleNamespace:
    return SimpleNamespace(vehicle=vehicle, name=name, brand="B", code=code,
                           cross_codes=("C1", "C2"), store=store, warehouse=0)


def in_stock(item: SimpleNamespace) -> bool:
    return (item.store or 0) > 0 or (item.warehouse or 0) > 0


class ReadingTests(unittest.TestCase):
    def test_a_reading_is_one_line_or_nothing(self) -> None:
        self.assertEqual(vehicles.clean("  Make Model, gen X (2008-2014).\n"),
                         "Make Model, gen X (2008-2014)")
        self.assertEqual(len(vehicles.clean(" ".join(["w"] * 80)).split()),
                         vehicles.READING_WORDS)
        for nothing in ("", None, "no car", "No car.", "unclear", "Unclear: maybe"):
            self.assertEqual(vehicles.clean(nothing), "", nothing)

    def test_texts_come_most_rows_first_with_rows_in_stock_as_examples(self) -> None:
        rows = [row("A 2008", store=0, code="old"), row("A 2008", code="new"),
                row("A 2008", store=0), row("B"), row("C", store=0), row("")]
        texts = vehicles.texts_of(rows, in_stock)
        self.assertEqual(list(texts), ["A 2008", "B"])  # C has no row in stock
        self.assertEqual(texts["A 2008"][0].code, "new")
        self.assertEqual(len(texts["A 2008"]), vehicles.EXAMPLES)
        self.assertEqual(list(vehicles.texts_of(rows)), ["A 2008", "B", "C"])

    def test_the_reader_is_asked_with_its_instructions_and_no_car_is_dropped(self) -> None:
        client = Messages(said(n1="Make Model, gen X (2008-2014)", n2="no car"))
        found, failed = vehicles.read(Asker(client), "m", {"A 2008": [row("A 2008")],
                                                            "UNIVERSAL": [row("UNIVERSAL")]},
                                      workers=1)
        self.assertEqual((found, failed), ({"A 2008": "Make Model, gen X (2008-2014)"}, 0))
        request = client.requests[0]
        self.assertIn("generation not stated", " ".join(request["system"].split()))
        sent = json.loads(request["messages"][0]["content"])["names"][0]
        self.assertEqual(sent["example_rows"][0]["number"], "X1")

    def test_the_readers_instructions_name_no_car_of_this_catalogue(self) -> None:
        told = vehicles.VEHICLE_SYSTEM.casefold()
        for word in ("murano", "pajero", "camry", "w212", "e60"):
            self.assertNotIn(word, told)


class JobTests(unittest.TestCase):
    def test_the_job_saves_continues_and_reads_only_what_is_new(self) -> None:
        rows = [row("A 2008"), row("A 2008"), row("B")]
        with tempfile.TemporaryDirectory() as folder:
            out = str(Path(folder) / "readings.json")
            client = Messages(said(n1="Make A gen 1 (2008-2014)", n2="Make B, generation "
                                   "not stated"))
            self.assertEqual(vehicles.run(["--out", out, "--workers", "1"], rows=rows,
                                          client=client), 0)
            self.assertEqual(vehicles.load(out), {"A 2008": "Make A gen 1 (2008-2014)",
                                                  "B": "Make B, generation not stated"})
            stored = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertEqual(set(stored), {"model", "date", "readings"})
            # Run again: everything has a reading, so nothing is asked.
            idle = Messages()
            self.assertEqual(vehicles.run(["--out", out], rows=rows, client=idle), 0)
            self.assertEqual(idle.requests, [])
            # A new text is read alone and added to what was kept.
            more = Messages(said(n1="Make C gen 2 (2015-2020)"))
            vehicles.run(["--out", out], rows=[*rows, row("C 2015")], client=more)
            self.assertEqual(len(vehicles.load(out)), 3)
            self.assertEqual(len(json.loads(more.requests[0]["messages"][0]["content"])
                                 ["names"]), 1)

    def test_a_failed_request_leaves_its_texts_for_the_next_run(self) -> None:
        rows = [row("A 2008"), row("B")]
        with tempfile.TemporaryDirectory() as folder:
            out = str(Path(folder) / "readings.json")
            broken = Messages(RuntimeError("bench request failed: "))
            self.assertEqual(vehicles.run(["--out", out, "--workers", "1"], rows=rows,
                                          client=broken), 1)
            self.assertEqual(vehicles.load(out), {})
            fixed = Messages(said(n1="Make A gen 1", n2="Make B, generation not stated"))
            self.assertEqual(vehicles.run(["--out", out], rows=rows, client=fixed), 0)
            self.assertEqual(len(vehicles.load(out)), 2)

    def test_part_names_are_read_with_their_own_instructions_into_their_own_file(self) -> None:
        rows = [row("A 2008", name="მილისა ბერკეტის წნ/ქვ/უკ"), row("B", name="ხუნდი წნ")]
        with tempfile.TemporaryDirectory() as folder:
            out = str(Path(folder) / "names.json")
            client = Messages(said(n1="Control arm bush, front lower arm, rear bush",
                                   n2="Brake pads, front axle"))
            self.assertEqual(vehicles.run(["--names", "--out", out, "--workers", "1"],
                                          rows=rows, client=client), 0)
            self.assertEqual(set(vehicles.load(out)), {"მილისა ბერკეტის წნ/ქვ/უკ", "ხუნდი წნ"})
            request = client.requests[0]
            self.assertIn("which of its bushes", " ".join(request["system"].split()))
            sent = json.loads(request["messages"][0]["content"])["names"][0]
            self.assertIn("vehicle", sent["example_rows"][0])

    def test_a_missing_file_holds_no_readings(self) -> None:
        self.assertEqual(vehicles.load("/nonexistent/readings.json"), {})


class ResultTests(unittest.TestCase):
    def search(self, extras: dict) -> dict:
        box = Toolbox(index=INDEX, repository=None, search_service=None, vin=None,
                      audience="public", handles=None, deadline=time.monotonic() + 60,
                      extras=extras)
        data, _ = box.render(box.run("search_catalog", {
            "make_terms": ["MB"], "vehicle_terms": [], "part_terms": ["ხუნდ"],
            "exclude_terms": [], "in_stock_only": False}))
        return data

    def test_results_carry_the_readings_of_their_rows_texts(self) -> None:
        texts = {item.vehicle for item in INDEX.rows() if item.vehicle}
        readings = {text: f"reading of {text}" for text in texts}
        names = {item.name: f"part {item.name}" for item in INDEX.rows() if item.name}
        data = self.search({"vehicle_readings": readings, "name_readings": names})
        shown = {item[2] for item in data["rows"]}
        self.assertTrue(shown)
        self.assertEqual(data["vehicle_readings"],
                         {text: f"reading of {text}" for text in shown})
        self.assertEqual(data["name_readings"],
                         {item[1]: f"part {item[1]}" for item in data["rows"]})
        bare = self.search({})
        self.assertNotIn("vehicle_readings", bare)
        self.assertNotIn("name_readings", bare)


if __name__ == "__main__":
    unittest.main()
