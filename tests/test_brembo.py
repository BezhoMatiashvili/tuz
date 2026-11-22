"""Brembo live lookup client against synthetic pages shaped like bremboparts.com.

The fixtures are hand-written imitations of the site's markup and JSON with made-up codes;
the site's own data is not copied into the repository.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from automotive.assistant_tools import Toolbox, brembo_answer
from automotive.brembo import BremboClient, engine_title, parse_results
from automotive.public_http import PublicSourceError
from automotive.vin_search import VINSearchCoordinator

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brembo"
BASE = "https://www.bremboparts.com/europe/en"
BRANDS = [{"brandName": "TOYOTA", "brandCode": "000111"},
          {"brandName": "MERCEDES-BENZ", "brandCode": "000074"},
          {"brandName": "MERCEDES-BENZ TRUCKS", "brandCode": "000075"}]
MODELS = [
    {"title": "COROLLA (_E12_) 01/01 - 04/08", "modelName": "COROLLA (_E12_)",
     "modelCode": "04851", "modelDateStart": "01/01", "modelDateEnd": "04/08"},
    {"title": "COROLLA Saloon (_E12_) 08/00 - 12/08", "modelName": "COROLLA Saloon (_E12_)",
     "modelCode": "04852", "modelDateStart": "08/00", "modelDateEnd": "12/08"},
    {"title": "COROLLA (_E15_) 11/06 - 10/14", "modelName": "COROLLA (_E15_)",
     "modelCode": "05001", "modelDateStart": "11/06", "modelDateEnd": "10/14"},
    {"title": "CAMRY (_V3_) 09/01 - 11/06", "modelName": "CAMRY (_V3_)",
     "modelCode": "04000", "modelDateStart": "09/01", "modelDateEnd": "11/06"},
]
TYPES = [
    {"title": "1.4 D (NDE120_) (66 kW/90 CV) 07/04 - 02/07", "typeName": "1.4 D (NDE120_)",
     "typeCodeAndVehicleType": "000018734-1", "typeDateStart": "07/04",
     "typeDateEnd": "02/07", "kw": "66"},
    {"title": "1.6 VVT-i (ZZE121_) (81 kW/110 CV) 01/02 - 12/06",
     "typeName": "1.6 VVT-i (ZZE121_)", "typeCodeAndVehicleType": "000016374-1",
     "typeDateStart": "01/02", "typeDateEnd": "12/06", "kw": "81"},
]


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code, self.body = status, body

    def iter_content(self, _size: int):
        yield self.body

    def close(self) -> None:
        pass


class Session:
    """Answers the site's calls; records method, path and headers."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.overrides = dict(overrides or {})

    def request(self, method, url, *, data=None, headers=None, **_kwargs):
        path = url.removeprefix(BASE)
        body = json.loads(data) if data else None
        self.calls.append((method, path, headers or {}, body))
        if (method, path) in self.overrides:
            answer = self.overrides[(method, path)]
            if isinstance(answer, list):
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
            return answer
        if method == "GET" and path == "":
            return Response(200, (FIXTURES / "home.html").read_bytes())
        if method == "GET" and path.startswith("/catalogue/toyota"):
            return Response(200, (FIXTURES / "results.html").read_bytes())
        answers = {
            "/catalogue/search/getsearchbrands": BRANDS,
            "/catalogue/search/getsearchmodels": MODELS,
            "/catalogue/search/getsearchtypes": TYPES,
            "/catalogue/search/searchtype": {
                "url": "/europe/en/catalogue/toyota-corolla-e12-1-4-d/000018734-1"},
            "/catalogue/getproductmanufacturerreferences": [
                {"code": "04465-TEST", "brandsName": "TOYOTA"}],
        }
        return Response(200, json.dumps(answers[path]).encode()) if path in answers \
            else Response(404, b"")


def client(session: Session) -> BremboClient:
    return BremboClient(session_factory=lambda: session)


class ParseTests(unittest.TestCase):
    def test_brake_products_with_axle_line_specs_and_variants(self):
        items = parse_results((FIXTURES / "results.html").read_text())
        # No caliper, and the repeated pad is listed once.
        self.assertEqual([item["product_code"] for item in items],
                         ["09.TEST.11", "P 99 001", "P 99 002", "S 99 501"])
        disc = items[0]
        self.assertEqual((disc["component"], disc["line"], disc["variants"]),
                         ("Front brake disc", "Prime", ["09.TEST.10"]))
        self.assertIn("Diameter 255 mm", disc["details"])
        self.assertEqual(items[2]["component"], "Rear brake pads")
        self.assertEqual(items[2]["note"], "Construction Year to:12/2006")
        self.assertEqual(items[3]["component"], "Rear brake shoes")


class LookupTests(unittest.TestCase):
    def test_one_car_end_to_end_with_the_site_token(self):
        session = Session()
        report = client(session).lookup(None, {
            "make": "Toyota", "model": "Corolla (_E12_) 01/01 - 04/08", "model_year": 2005,
            "displacement_l": 1.4})
        self.assertEqual(report["status"], "PROVISIONAL")
        self.assertIn("1.4 D", report["vehicle_label"])
        self.assertEqual(report["items"][1]["oem_codes"], ["04465-TEST"])
        self.assertEqual(report["items"][1]["oem_makers"], ["TOYOTA"])
        posts = [call for call in session.calls if call[0] == "POST"]
        self.assertTrue(all(call[2]["RequestVerificationToken"] == "synthetic-form-value"
                            for call in posts))
        self.assertEqual(posts[0][3], {"vehicleType": "Car"})
        # A VIN is never sent anywhere.
        self.assertNotIn("vin", json.dumps([call[3] for call in session.calls]).lower())

    def test_several_models_or_engines_are_offered_not_guessed(self):
        brembo = client(Session())
        report = brembo.lookup(None, {"make": "TOYOTA", "model": "Corolla", "model_year": 2005})
        self.assertEqual(report["status"], "CONFLICTED")
        self.assertEqual(report["candidates"], ["COROLLA (_E12_) 01/01 - 04/08",
                                                "COROLLA Saloon (_E12_) 08/00 - 12/08"])
        self.assertIn("as model", report["clarification"])
        report = brembo.lookup(None, {"make": "TOYOTA", "model": "COROLLA (_E12_) 01/01 - 04/08",
                                      "model_year": 2005})
        self.assertEqual(report["status"], "CONFLICTED")
        self.assertEqual(report["candidates"], ["1.4 D (NDE120_) 66 kW",
                                                "1.6 VVT-i (ZZE121_) 81 kW"])
        # The offered engine title, passed back as trim, chooses the engine.
        report = brembo.lookup(None, {"make": "TOYOTA", "model": "COROLLA (_E12_) 01/01 - 04/08",
                                      "model_year": 2005, "trim": "1.6 VVT-i (ZZE121_) 81 kW"})
        self.assertEqual(report["status"], "PROVISIONAL")
        self.assertIn("1.6 VVT-i", report["vehicle_label"])

    def test_unknown_model_lists_brembo_titles_closest_first(self):
        report = client(Session()).lookup(None, {"make": "TOYOTA", "model": "Corola E12",
                                                 "model_year": 2005})
        self.assertEqual(report["status"], "EMPTY")
        self.assertEqual(report["candidates"][:2], ["COROLLA (_E12_) 01/01 - 04/08",
                                                    "COROLLA Saloon (_E12_) 08/00 - 12/08"])

    def test_conflicting_engine_details_never_select_a_different_engine(self):
        session = Session()
        report = client(session).lookup(None, {
            "make": "TOYOTA", "model": "COROLLA (_E12_) 01/01 - 04/08", "model_year": 2005,
            "displacement_l": 1.4, "engine_code": "WRONG1",
        })
        self.assertEqual(report["status"], "EMPTY")
        self.assertEqual(report["items"], [])
        self.assertFalse(any(call[1] == "/catalogue/search/searchtype" for call in session.calls))

    def test_make_needs_one_clear_brand(self):
        self.assertEqual(client(Session()).lookup(None, {"make": "Lada", "model": "Niva",
                                                         "model_year": 2005})["status"], "EMPTY")
        # 'Mercedes' starts two Brembo brands: no guess.
        self.assertEqual(client(Session()).lookup(None, {"make": "Mercedes", "model": "C",
                                                         "model_year": 2005})["status"], "EMPTY")

    def test_access_refused_stops_and_a_stale_token_is_renewed_once(self):
        refused = Session({("POST", "/catalogue/search/getsearchbrands"): Response(403, b"")})
        with self.assertRaises(PublicSourceError) as caught:
            client(refused).lookup(None, {"make": "TOYOTA", "model": "Camry", "model_year": 2004})
        self.assertEqual(caught.exception.code, "ACCESS_REQUIRED")
        stale = Session({("POST", "/catalogue/search/getsearchbrands"): [
            Response(400, b""), Response(200, json.dumps(BRANDS).encode())]})
        report = client(stale).lookup(None, {"make": "TOYOTA", "model": "Camry",
                                             "model_year": 2004})
        self.assertEqual(report["status"], "CONFLICTED")  # reached the engine list
        self.assertEqual(sum(call[:2] == ("GET", "") for call in stale.calls), 2)

    def test_engine_title_fits_the_tool_field(self):
        self.assertEqual(engine_title(TYPES[0]), "1.4 D (NDE120_) 66 kW")
        self.assertLessEqual(len(engine_title(TYPES[1])), 60)


class FederationTests(unittest.TestCase):
    def test_brembo_runs_for_brake_questions_only(self):
        self.assertEqual(VINSearchCoordinator._scope("brake pads"), {"brembo"})
        self.assertEqual(VINSearchCoordinator._scope("brake discs"), {"brembo"})
        self.assertNotIn("brembo", VINSearchCoordinator._scope(""))
        self.assertNotIn("brembo", VINSearchCoordinator._scope("oil filter"))

    def report(self) -> dict:
        items = parse_results((FIXTURES / "results.html").read_text())
        for item in items:
            item["oem_codes"] = [f"OE-{item['product_code'].replace(' ', '')}"]
        return {"provider": "Brembo", "status": "PROVISIONAL", "vehicle_label": "TOYOTA COROLLA",
                "items": items, "candidates": [], "clarification": None}

    def test_tool_offers_brembo_for_brakes(self):
        search = SimpleNamespace(_external={"brembo": (object(), True)}, _catalog=None,
                                 motorium_enabled=False)
        tools = SimpleNamespace(search_service=search)
        self.assertEqual(Toolbox._able(tools, "brake_pads", False), ["brembo"])
        self.assertEqual(Toolbox._able(tools, "oil_filter", False), [])

    def test_pad_question_uses_pad_and_shoe_numbers_only(self):
        tools = SimpleNamespace(_catalog_articles=lambda _snapshot: [])
        numbers = Toolbox._supplier_numbers(tools, {"fluids": [self.report()]}, "brake_pads")
        by_number = {item["number"]: item for item in numbers}
        self.assertEqual(set(by_number), {"P 99 001", "OE-P99001", "P 99 002", "OE-P99002",
                                          "S 99 501", "OE-S99501"})
        self.assertEqual(by_number["OE-P99001"]["brand"], "OE")
        self.assertEqual(by_number["P 99 001"]["part"], "Front brake pads")

    def test_answer_quotes_products_or_choices(self):
        answer = brembo_answer({"fluids": [self.report()]}, "brake_discs")
        self.assertEqual([product["code"] for product in answer["products"]], ["09.TEST.11"])
        self.assertEqual(answer["products"][0]["part"], "Front brake disc")
        choices = brembo_answer({"fluids": [{
            "provider": "Brembo", "status": "CONFLICTED", "vehicle_label": "",
            "items": [], "candidates": ["1.4 D 66 kW"], "clarification": "Pick one."}]},
            "brake_pads")
        self.assertEqual((choices["status"], choices["choose_from"], choices["note"]),
                         ("conflicted", ["1.4 D 66 kW"], "Pick one."))
        self.assertIsNone(brembo_answer({"fluids": []}, "brake_pads"))


class ToolTurnTests(unittest.TestCase):
    """supplier_parts through a whole assistant turn (scripted model, no live call)."""

    def federation(self, report: dict):
        from tests import test_assistant as assistant

        search = assistant.Search(status="COMPLETE", fluids=[report], sources=[
            {"source_id": "vpic", "status": "SKIPPED", "error_code": "VIN_REQUIRED"},
            {"source_id": "brembo", "status": "OK", "count": 1, "error_code": None}])
        search._external = {name: (None, False) for name in ("fuchs", "shell", "pakelo",
                                                             "jsfilter")}
        search._external["brembo"] = (object(), True)
        search._catalog, search.motorium_enabled = None, False
        return assistant, search

    def test_brake_groups_are_offered_when_brembo_is_the_only_source(self):
        _assistant, search = self.federation({})
        box = Toolbox(index=None, repository=None, search_service=search, vin=None,
                      audience="staff", handles=None, deadline=time.monotonic() + 30)
        self.assertEqual(box.sources()["supplier_parts"],
                         ["brake_pads", "brake_discs", "brakes"])

    def test_brembo_oe_number_finds_the_shop_row_and_quotes_the_product(self):
        report = {"provider": "Brembo", "status": "PROVISIONAL", "vehicle_label": "TOYOTA CAMRY",
                  "candidates": [], "clarification": None, "items": [{
                      "kind": "brake", "component": "Rear brake pads", "product_code": "P 99 001",
                      "line": "Prime", "details": "Width 96 mm", "variants": [],
                      "oem_codes": ["04466-33180"], "note": None}]}
        assistant, search = self.federation(report)
        content, is_error = assistant.ToolTests.one(self, "supplier_parts", {
            "part_group": "brake_pads", **assistant.car(make="Toyota", model="Camry",
                                                        year=2015)}, search_service=search)
        self.assertFalse(is_error)
        self.assertEqual(content["status"], "complete")
        # Brembo's OE number is the shop's Toyota row's own number.
        self.assertEqual([row[-1] for row in content["rows"]], ["OE 04466-33180 = own number"])
        self.assertEqual(content["brembo"]["products"][0]["code"], "P 99 001")
        self.assertEqual(content["brembo"]["products"][0]["oe_numbers"], ["04466-33180"])

    def test_a_choice_from_brembo_reaches_the_model(self):
        report = {"provider": "Brembo", "status": "CONFLICTED", "vehicle_label": "", "items": [],
                  "candidates": ["2.5 Hybrid 131 kW", "2.0 VVT-i 112 kW"],
                  "clarification": "Several Brembo engines fit."}
        assistant, search = self.federation(report)
        content, is_error = assistant.ToolTests.one(self, "supplier_parts", {
            "part_group": "brake_discs", **assistant.car(make="Toyota", model="Camry",
                                                         year=2015)}, search_service=search)
        self.assertFalse(is_error)
        # The federation job finished; only Brembo's own answer asks for a choice.
        self.assertEqual(content["status"], "complete")
        self.assertEqual(content["brembo"]["status"], "conflicted")
        self.assertEqual(content["brembo"]["choose_from"],
                         ["2.5 Hybrid 131 kW", "2.0 VVT-i 112 kW"])


if __name__ == "__main__":
    unittest.main()
