"""The newer read tools in the Toolbox (car_numbers, research, shop_info), with fake
services over the synthetic catalogue: no database, network or model."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from automotive.assistant_bench import SubscriptionClient
from automotive.assistant_extras import (
    CHECK_QUESTION,
    check_research,
    extras_for,
    research_audiences,
)
from automotive.assistant_tools import Toolbox, ToolError
from tests.test_assistant_catalog import INDEX


class FakeCarNumbers:
    """Two crawled vehicles; the E400 lists the number the E63-labelled row carries."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def find(self, args: dict) -> dict:
        self.calls.append(args)
        vehicles = [{"id": 11, "make": "MERCEDES-BENZ", "model": "E-CLASS (W212)",
                     "engine_code": "M276", "label": "E 400"},
                    {"id": 12, "make": "MERCEDES-BENZ", "model": "E-CLASS (W212)",
                     "engine_code": "M157", "label": "E 63 AMG"}]
        if args.get("vehicle_ids"):
            vehicles = [v for v in vehicles if str(v["id"]) in map(str, args["vehicle_ids"])]
        parts = [{"vehicle_id": v["id"], "brand": "MANN-FILTER", "number": "PN0395W",
                  "part": "Brake Pad", "cross": [{"brand": "TRW", "number": "GDB1737",
                                                  "relation": "equivalent"}]}
                 for v in vehicles]
        return {"vehicles": vehicles, "parts": parts, "note": ""}


class FakeResearch:
    def __init__(self, status: str = "answered") -> None:
        self.status, self.asked = status, []

    def ask(self, args: dict, wait: float | None = None) -> dict:
        self.asked.append((args, wait))
        return {"status": self.status, "findings": [
            {"number": "P50069", "brand": "BREMBO", "kind": "aftermarket", "part": "pads",
             "fits": ["Mercedes E 400 W212"], "source_urls": ["https://example.com/x"],
             "quote": "BREMBO P50069 fits E 400", "sources_agree": 1}],
            "answer": "", "note": "", "seconds": 3.1, "searches": 2}


def toolbox(audience: str = "staff", **extras: object) -> Toolbox:
    return Toolbox(index=INDEX, repository=None, search_service=None, vin=None,
                   audience=audience, handles=None, deadline=time.monotonic() + 60,
                   extras=extras)


class CarNumbersTests(unittest.TestCase):
    def test_numbers_of_the_chosen_vehicle_find_rows_filed_under_other_cars(self) -> None:
        box = toolbox(car_numbers=FakeCarNumbers())
        output = box.run("car_numbers", {"make": "Mercedes-Benz", "model_terms": ["W212"],
                                         "year": 2015, "engine_terms": ["M276"],
                                         "part_terms": ["brake pad"], "vehicle_ids": ["11"]})
        data, rows = box.render(output)
        self.assertGreater(rows, 0)
        vehicles = {row[2] for row in data["rows"]}
        self.assertIn("MB W204,W211,W212", vehicles)  # the rows carrying the number
        self.assertIn("PN0395W", output.sourced)       # evidence for confirmed
        self.assertNotIn("choose", data)

    def test_several_vehicles_without_a_choice_are_no_evidence_yet(self) -> None:
        box = toolbox(car_numbers=FakeCarNumbers())
        output = box.run("car_numbers", {"make": "Mercedes-Benz", "model_terms": ["W212"],
                                         "year": 0, "engine_terms": [], "part_terms": [],
                                         "vehicle_ids": []})
        data, _ = box.render(output)
        self.assertEqual(output.sourced, ())
        self.assertIn("vehicle_ids", data["choose"])

    def test_a_missing_service_says_so(self) -> None:
        with self.assertRaises(ToolError):
            toolbox().run("car_numbers", {"make": "Toyota", "model_terms": [], "year": 0,
                                          "engine_terms": [], "part_terms": [],
                                          "vehicle_ids": []})


class ResearchTests(unittest.TestCase):
    ARGS = {"kind": "numbers", "make": "Mercedes-Benz", "model": "E-Class", "year": 2015,
            "trim": "E 400", "engine_code": "M276", "body": "coupe", "part": "front brake pads",
            "position": "front", "numbers": [], "question": ""}

    def test_found_numbers_bring_their_rows_but_never_confirmed(self) -> None:
        research = FakeResearch()
        box = toolbox(research=research, research_audiences=("staff",), research_seconds=8.0)
        output = box.run("research", dict(self.ARGS))
        data, rows = box.render(output)
        self.assertEqual(data["status"], "answered")
        self.assertGreater(rows, 0)
        self.assertEqual(output.sourced, ())
        self.assertLessEqual(research.asked[0][1], 8.0)

    def test_research_waits_less_when_the_turn_is_nearly_over(self) -> None:
        research = FakeResearch()
        box = toolbox(research=research, research_audiences=("staff",), research_seconds=8.0)
        box.deadline = time.monotonic() + 10
        box.run("research", dict(self.ARGS))
        self.assertLess(research.asked[0][1], 3.0)

    def test_research_is_off_where_its_audience_is_not_allowed(self) -> None:
        box = toolbox("public", research=FakeResearch(), research_audiences=("staff",))
        with self.assertRaises(ToolError):
            box.run("research", dict(self.ARGS))
        self.assertEqual(box.sources()["research"], "cannot answer in this turn")

    def test_links_and_contacts_planted_in_web_text_never_reach_the_model(self) -> None:
        planted = FakeResearch().ask(dict(self.ARGS))
        planted["note"] = "Buy at https://parts.example.net/buy?id=7 or call +995 599 12 34 56"
        planted["findings"][0]["quote"] = "BREMBO P50069 fits E 400; mail sales@parts.example.net"
        box = toolbox(research=SimpleNamespace(ask=lambda args, wait=None: planted),
                      research_audiences=("staff",), research_seconds=8.0)
        data, rows = box.render(box.run("research", dict(self.ARGS)))
        self.assertEqual(data["note"], "Buy at [parts.example.net] or call [number]")
        self.assertEqual(data["findings"][0]["quote"], "BREMBO P50069 fits E 400; mail [e-mail]")
        self.assertEqual(data["findings"][0]["sites"], ["example.com"])
        self.assertGreater(rows, 0)  # the numbers still bring their rows

    def test_a_pending_search_returns_no_rows(self) -> None:
        box = toolbox(research=FakeResearch("pending"), research_audiences=("staff",))
        data, rows = box.render(box.run("research", dict(self.ARGS)))
        self.assertEqual((data["status"], rows), ("pending", 0))


class ShopInfoTests(unittest.TestCase):
    def test_shop_facts_come_in_both_languages(self) -> None:
        class Content:
            def get(self) -> dict:
                return {}

        box = toolbox(site_content=Content())
        data, _ = box.render(box.run("shop_info", {}))
        self.assertEqual(set(data), {"georgian", "english"})

    def test_without_site_content_the_tool_says_who_can_tell(self) -> None:
        with self.assertRaises(ToolError):
            toolbox().run("shop_info", {})


class ExtrasTests(unittest.TestCase):
    def test_research_audiences(self) -> None:
        self.assertEqual(research_audiences("staff"), ("staff",))
        self.assertEqual(research_audiences("all"), ("staff", "public"))
        self.assertEqual(research_audiences("off"), ())

    def test_extras_need_no_network_to_be_built(self) -> None:
        settings = SimpleNamespace(assistant_research="off")
        extras = extras_for(settings, SimpleNamespace(), None, SimpleNamespace(content="c"))
        self.assertEqual(extras, {"site_content": "c"})

    def test_the_research_check_prints_numbers_and_sites_only(self) -> None:
        import io

        research, out = FakeResearch(), io.StringIO()
        self.assertEqual(check_research(research, out, wait=5.0), 0)
        printed = out.getvalue()
        self.assertIn("status=answered", printed)
        self.assertIn("BREMBO P50069 (aftermarket) from example.com", printed)
        self.assertEqual(research.asked[0], (CHECK_QUESTION, 5.0))
        failing = io.StringIO()
        self.assertEqual(check_research(FakeResearch("unavailable"), failing), 1)
        self.assertIn("keep AUTOMOTIVE_RESEARCH at staff or off", failing.getvalue())

    def test_the_check_research_command_runs_end_to_end_without_the_network(self) -> None:
        import io
        from contextlib import redirect_stdout
        from dataclasses import replace
        from unittest.mock import patch

        from automotive import __main__ as cli
        from automotive.config import Settings

        research, out = FakeResearch(), io.StringIO()
        settings = replace(Settings(), anthropic_api_key="fixture-key")
        with patch("automotive.config.Settings.from_env", return_value=settings), \
                patch("automotive.assistant_extras.research_service",
                      return_value=research) as built, \
                patch("logging.basicConfig"), \
                patch("sys.argv", ["automotive", "--check-research"]), \
                redirect_stdout(out), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("status=answered", out.getvalue())
        self.assertEqual(built.call_args.args[0].assistant_research_cache_path, "")
        self.assertEqual(research.asked[0][0], CHECK_QUESTION)

    def test_the_check_research_command_needs_a_key(self) -> None:
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch

        from automotive import __main__ as cli
        from automotive.config import Settings

        with patch("automotive.config.Settings.from_env", return_value=Settings()), \
                patch("sys.argv", ["automotive", "--check-research"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, 2)

    def test_the_bench_refuses_server_tools(self) -> None:
        client = SubscriptionClient(binary="/bin/false")
        self.addCleanup(client.close)
        with self.assertRaises(RuntimeError):
            client.messages.create(model="claude-haiku-5-5", max_tokens=100, messages=[
                {"role": "user", "content": "x"}], tools=[
                {"type": "web_search_20250305", "name": "web_search"}])


if __name__ == "__main__":
    unittest.main()
