"""CatCar step crawler, MANN JSON adapter and bulk importers, against trimmed real data."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from parts_crawler.importers import fueleconomy, wikidata
from parts_crawler.records import (
    PartRef,
    ascii_url,
    bounded,
    clean,
    normalize_brand,
    normalize_name,
)
from parts_crawler.sources import ADAPTERS
from parts_crawler.sources.base import CrawledPage
from parts_crawler.sources.catcar import (
    CATALOGS,
    MAINTENANCE,
    decode_state,
    encode_url,
    step_of,
)
from parts_crawler.sources.mann_api import (
    BRANDS,
    LINKAGE,
    PRODUCTS,
    SERIES,
    TYPES,
    brand_name,
    graphql_url,
    part_number,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parts_crawler" / "catcar"
INDEX = {entry["name"]: entry for entry in json.loads((FIXTURES / "index.json").read_text())}
DEEP = {"vehicle": "toyota:x", "groups": ["11-01 ENGINE"], "matched": False, "filter": ""}


class CatCarTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["catcar"]()

    def parse(self, name: str, context: dict | None = None):
        entry = INDEX[name]
        return self.adapter.parse(CrawledPage(
            entry["url"], entry["kind"], context or {}, 200,
            (FIXTURES / f"{name}.html").read_bytes(), "text/html; charset=utf-8"))

    def walk(self, *names: str):
        """Parse pages in order, handing each the context its parent's link carried."""
        context: dict = {}
        result = None
        for position, name in enumerate(names):
            result = self.parse(name, context)
            if position + 1 < len(names):
                wanted = decode_state(INDEX[names[position + 1]]["url"])
                link = next(link for link in result.links if decode_state(link.url) == wanted)
                context = link.context
        return result

    def test_state_round_trip_drops_the_breadcrumb_and_keeps_key_order(self):
        url = INDEX["toyota_modifications"]["url"]
        state = decode_state(url)
        self.assertEqual(list(state), ["market", "st", "catalog", "rec"])
        self.assertNotIn("sts", state)
        rebuilt = encode_url("toyota", state)
        self.assertEqual(decode_state(rebuilt), state)
        self.assertLess(len(rebuilt), len(url))

    def test_home_links_only_the_profiled_catalogs(self):
        links = self.parse("home").links
        self.assertTrue(links)
        self.assertLessEqual({link.url.split("/")[3] for link in links}, set(CATALOGS))
        self.assertIn("https://www.catcar.info/toyota/", {link.url for link in links})

    def test_models_page_goes_deeper_only(self):
        result = self.walk("toyota_markets", "toyota_models")
        self.assertTrue(result.links)
        self.assertTrue(all(step_of(decode_state(link.url)) == 30 for link in result.links))
        self.assertEqual(result.vehicles, [])
        # Mazda lists model years as links to the same step; those are followed too.
        mazda = self.parse("mazda_models")
        self.assertTrue(any(decode_state(link.url).get("st") == "20" for link in mazda.links))
        self.assertTrue(all(step_of(decode_state(link.url)) >= 20 for link in mazda.links))
        self.assertTrue(all(len(link.url) <= 2048 for link in result.links))

    def test_modifications_become_vehicles_and_are_not_followed(self):
        result = self.walk("toyota_markets", "toyota_models", "toyota_modifications")
        self.assertEqual(result.links, [])
        vehicle = result.vehicles[0]
        self.assertEqual((vehicle.make, vehicle.model, vehicle.model_code, vehicle.engine_code),
                         ("TOYOTA", "COROLLA", "NDE180L-DEFDXW", "1NDTV"))
        self.assertEqual((vehicle.year_from, vehicle.year_to, vehicle.market),
                         (2013, 2016, "euro"))
        self.assertEqual((vehicle.displacement_cc, vehicle.fuel), (1400, "diesel"))
        self.assertEqual(vehicle.raw["catalog"], "toyota")
        self.assertEqual(vehicle.raw["state"]["st"], "60")

    def test_lexus_models_get_the_lexus_make(self):
        models = self.walk("toyota_markets", "toyota_models")
        link = next(link for link in models.links
                    if link.context["path"][-1]["label"].startswith("LEXUS "))
        catalog = CATALOGS["toyota"]
        state = dict(decode_state(link.url), st="60", modifname="ZWA10L-AHXBBW", engine="2ZRFXE")
        path = link.context["path"] + [{"st": 60, "label": "2ZRFXE", "cells": {}}]
        vehicle = self.adapter._vehicle(catalog, state, path, {})
        self.assertEqual((vehicle.make, vehicle.model), ("LEXUS", "CT200H"))

    def test_other_brands_read_their_own_fields(self):
        expected = {
            ("nissan_models",): ("NISSAN", "240SX", "S14", 1994, 1998),
            ("hyundai_models",): ("HYUNDAI", "ACCENT 15 (2014-) [GEN]", "KGENPSB15", 2014, None),
            ("bmw_regions",): ("BMW", "116d", "E81", None, None),
            ("mercedes_models",): ("MERCEDES-BENZ", "W 4 A 040 AUSTRALIEN", "722.313", None,
                                   None),
            ("honda_variants",): ("HONDA", "ACCORD", "24EXI", 2012, 2012),
            ("mazda_models",): ("MAZDA", "CX-5", "AUKS01", None, None),
            ("subaru_models", "subaru_variants"): ("SUBARU", "L SERIES", "A10", 1985, 1994),
            ("audivw_models", "audivw_years"): ("AUDI", "Audi A1", "A1", 2011, 2011),
            ("mitsubishi_models", "mitsubishi_codes", "mitsubishi_trims"):
                ("MITSUBISHI", "3000GT(EUR)", "Z11A", 1990, 2000),
        }
        for names, values in expected.items():
            with self.subTest(names[-1]):
                vehicle = self.walk(*names).vehicles[0]
                self.assertEqual((vehicle.make, vehicle.model, vehicle.model_code,
                                  vehicle.year_from, vehicle.year_to), values)
        honda = self.walk("honda_variants").vehicles[0]
        self.assertEqual((honda.transmission, honda.market), ("5AT", "KY"))

    def test_part_tables_use_the_article_column_not_the_callout(self):
        expected = {
            "nissan_parts": ("NISSAN", "10102-70FH0"), "hyundai_parts": ("HYUNDAI", "09110-1R000"),
            "bmw_parts": ("BMW", "51927896863"), "mercedes_parts": ("MERCEDES-BENZ",
                                                                      "A 1262501702"),
            "audivw_parts": ("VAG", "3C0010492K"), "honda_parts": ("HONDA", "16400-R44-A02"),
            "mazda_parts": ("MAZDA", "KD45-55-230B02"), "subaru_parts": ("SUBARU", "497123009"),
        }
        for name, (brand, number) in expected.items():
            with self.subTest(name):
                result = self.parse(name, DEEP)
                self.assertEqual((result.parts[0].brand, result.parts[0].number),
                                 (brand, number))
                self.assertEqual(result.parts[0].kind, "oe")
                self.assertEqual(result.fitments[0].vehicle_key, "toyota:x")
                self.assertEqual(result.fitments[0].position, "11-01 ENGINE")
                self.assertEqual(self.parse(name).parts, [])  # not a seeded vehicle

    def test_subgroup_rows_queue_article_requests(self):
        result = self.parse("toyota_subgroup", DEEP)
        articles = [link for link in result.links if link.kind == "articles"]
        self.assertEqual(len(articles), 3)
        self.assertTrue(all(link.url.endswith("&st=81") for link in articles))
        self.assertEqual((articles[0].context["callout"], articles[0].context["name"]),
                         ("09110", "JACK ASSY"))
        self.assertEqual(decode_state(articles[0].url)["st"], "80")
        self.assertEqual(result.parts, [])
        self.assertEqual(self.adapter.classify(articles[0].url), "articles")
        self.assertEqual(self.adapter.classify(INDEX["toyota_models"]["url"]), "step")

    def test_article_fragments_become_parts(self):
        context = {"vehicle": "toyota:x", "groups": ["09-01 TOOLS"], "name": "JACK ASSY"}
        result = self.parse("toyota_articles", context)
        self.assertEqual((result.parts[0].brand, result.parts[0].number, result.parts[0].name),
                         ("TOYOTA", "09111-05050", "JACK ASSY"))
        self.assertIn("06/2013-05/2016", result.fitments[0].note)
        self.assertEqual(result.fitments[0].position, "09-01 TOOLS")
        mitsubishi = self.parse("mitsubishi_articles", context)
        self.assertEqual(mitsubishi.parts[0].number, "MD994267")
        self.assertEqual(self.parse("toyota_articles").parts, [])

    def test_group_filter_and_tabs(self):
        seeded = {"vehicle": "toyota:x", "groups": [], "matched": False, "filter": "tool|набор"}
        result = self.parse("toyota_groups", seeded)
        tabs = [link for link in result.links if not link.context["groups"]]
        groups = [link for link in result.links if link.context["groups"]]
        self.assertTrue(tabs)
        self.assertTrue(all(step_of(decode_state(link.url)) == 60 for link in tabs))
        self.assertTrue(groups)
        self.assertTrue(all(link.context["matched"] for link in groups))
        everything = self.parse("toyota_groups", dict(seeded, filter=""))
        self.assertGreater(len(everything.links), len(result.links))
        self.assertEqual(self.parse("toyota_groups").links, [])  # taxonomy never opens groups

    def test_vehicle_seeds_and_legacy_rows(self):
        vehicle = self.walk("toyota_markets", "toyota_models", "toyota_modifications").vehicles[0]
        rows = [{"source_vehicle_id": vehicle.key, "raw": vehicle.raw}]
        [link] = self.adapter.vehicle_seeds(rows, MAINTENANCE)
        self.assertEqual(decode_state(link.url), vehicle.raw["state"])
        self.assertEqual((link.context["vehicle"], link.context["filter"]),
                         (vehicle.key, MAINTENANCE))
        self.assertEqual(self.adapter.vehicle_seeds([{"source_vehicle_id": "x", "raw": {}}],
                                                    None), [])
        legacy = CrawledPage(INDEX["toyota_models"]["url"], "page", {"trail": ["x"]}, 200,
                             (FIXTURES / "toyota_models.html").read_bytes(), "text/html")
        result = self.adapter.parse(legacy)
        self.assertEqual((result.links, result.vehicles, result.parts), ([], [], []))
        self.assertTrue(self.adapter.embeds_captcha_widget)
        self.assertEqual(self.adapter.min_delay, 10.0)


def stored(key: str, modification: str, engine: str, year_to: int | None) -> dict:
    return {"source_vehicle_id": key, "year_from": 2000, "year_to": year_to,
            "raw": {"catalog": "toyota", "state": {
                "market": "euro", "st": "60", "catalog": "164520", "modifname": modification,
                "engine": engine}}}


class AutoPartsTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["catcar"]()

    def test_variants_of_one_frame_and_engine_are_one_family(self):
        left = self.adapter.family(stored("a", "NDE180L-DEFDXW", "1NDTV", 2016))
        right = self.adapter.family(stored("b", "NDE180R-DEMNXW", "1NDTV", 2016))
        other = self.adapter.family(stored("c", "NDE180L-DEFDXW", "1ZRFAE", 2016))
        self.assertEqual(left, right)
        self.assertNotEqual(left, other)
        self.assertIsNone(self.adapter.family({"raw": {}}))

    def test_auto_seeding_queues_the_newest_vehicle_of_each_family(self):
        from parts_crawler.scheduler import auto_part_links

        class FakeStore:
            def iter_vehicles(self, _source):
                return iter([stored("old", "NDE180L-AAA", "1NDTV", 2010),
                             stored("current", "NDE180R-BBB", "1NDTV", None),
                             stored("other", "ZRE181L-CCC", "1ZRFE", 2005)])

        links, scanned = auto_part_links(self.adapter, FakeStore())
        self.assertEqual(scanned, 3)
        self.assertEqual([link.context["vehicle"] for link in links], ["current", "other"])
        self.assertLess(links[0].priority, links[1].priority)  # still produced comes first
        self.assertTrue(all(11 < link.priority < self.adapter.fair_split for link in links))
        self.assertTrue(all(link.context["filter"] and link.context["parts"]
                            for link in links))

    def test_part_name_filter_limits_the_part_number_requests(self):
        entry = INDEX["toyota_subgroup"]
        page = CrawledPage(entry["url"], "step", dict(DEEP, parts="handle"), 200,
                           (FIXTURES / "toyota_subgroup.html").read_bytes(), "text/html")
        result = self.adapter.parse(page)
        self.assertEqual([link.context["name"] for link in result.links], ["HANDLE, JACK"])


def api_page(kind: str, query: str, variables: dict, data: dict, context: dict | None = None):
    return CrawledPage(graphql_url(query, variables), kind, context or {}, 200,
                       json.dumps({"data": data}).encode(), "application/json")


class MannApiTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["mann_api"]()

    def test_urls_are_canonical_and_the_source_needs_an_exemption(self):
        left = graphql_url("query($a:Int!){ x(a:$a) }", {"b": 1, "a": 2})
        right = graphql_url("query($a:Int!){\n   x(a:$a)\n}", {"a": 2, "b": 1})
        self.assertEqual(left, right)  # key order and line breaks do not change the URL
        self.assertLess(len(graphql_url(PRODUCTS, {"page": 82, "size": 150})), 2048)
        self.assertTrue(self.adapter.needs_robots_exemption)
        self.assertEqual(self.adapter.robots_exempt_paths, ("/api/graphql/",))
        seeds = self.adapter.seeds()
        self.assertEqual(seeds[0].kind, "products")
        self.assertEqual(len({seed.url for seed in seeds}), len(seeds))

    def test_products_page_gives_parts_cross_references_and_the_other_pages(self):
        data = {"products": {"page_info": {"current_page": 1, "total_pages": 3}, "items": [{
            "sku": "W712/83_MANN-FILTER", "name": "W712/83_MANN-FILTER",
            "attributes_value": [
                {"key": "sales_designation", "value": "W 712/83"},
                {"key": "product_shape_group_product_type_group_name", "value": "Oil Filter"}],
            "oe_numbers": [{"label": "TOYOTA", "value": ["90915-YZZJ3", "04152-03002"]},
                           {"label": "MINI (BMW GROUP)", "value": ["11 42 0 306 483"]}],
            "comparison_numbers": [{"label": "AC DELCO", "value": ["PF 53"]}],
            "successor_product": [{"sales_designation": "W 712/95"}],
            "replaced_product": []}]}}
        result = self.adapter.parse(api_page("products", PRODUCTS, {"page": 1, "size": 150},
                                             data))
        part = result.parts[0]
        self.assertEqual((part.brand, part.number, part.name, part.kind),
                         ("MANN-FILTER", "W 712/83", "Oil Filter", "aftermarket"))
        pairs = {(ref.relation, ref.other) for ref in result.cross_refs}
        self.assertIn(("oe", PartRef("TOYOTA", "90915-YZZJ3")), pairs)
        self.assertIn(("oe", PartRef("MINI", "11 42 0 306 483")), pairs)
        self.assertIn(("equivalent", PartRef("AC DELCO", "PF 53")), pairs)
        self.assertIn(("replaced_by", PartRef("MANN-FILTER", "W 712/95")), pairs)
        self.assertEqual([link.kind for link in result.links], ["products", "products"])
        later = self.adapter.parse(api_page("products", PRODUCTS, {"page": 2, "size": 150}, {
            "products": {"page_info": {"current_page": 2, "total_pages": 3}, "items": []}}))
        self.assertEqual(later.links, [])

    def test_brand_search_keeps_road_vehicles_and_narrows_a_full_answer(self):
        found = [{"vehicle_brand_id": f"{index:014d}", "vehicle_brand_name": f"BRAND {index}",
                  "vehicle_application_segment_id": "03" if index == 0 else "01"}
                 for index in range(10)]
        result = self.adapter.parse(api_page(
            "brands", BRANDS, {"search": "a"}, {"brand_id_suggestion": {"suggestions": found}},
            {"search": "a"}))
        series = [link for link in result.links if link.kind == "series"]
        self.assertEqual(len(series), 9)  # the off-highway brand is left out
        self.assertTrue([link for link in result.links if link.kind == "brands"])
        short = self.adapter.parse(api_page(
            "brands", BRANDS, {"search": "toy"},
            {"brand_id_suggestion": {"suggestions": found[:2]}}, {"search": "toy"}))
        self.assertEqual([link.kind for link in short.links], ["series"])

    def test_vehicle_types_and_their_filters(self):
        series = self.adapter.parse(api_page(
            "series", SERIES, {"brand": "00000000000122"},
            {"modelCollectionByBrandId": {"models": [{
                "model_series_id": "00000000021404", "model_series_name": "Corolla",
                "model_series_date": "07-14"}]}}, {"brand": "TOYOTA"}))
        self.assertEqual(series.links[0].context["series"], "Corolla")
        types = self.adapter.parse(api_page("types", TYPES, {"series": "00000000021404"}, {
            "modelTypeCollection": {"allModelTypes": [{
                "model_type_id": "00000000349990", "vehicle_name": "1.4 D-4D",
                "engine_code": "1ND-TV", "fuel_type": "08", "ccm": "1364", "kw": "66",
                "bhp": "90", "model_code": "", "serial_number_range": "",
                "vehicle_manufactured_from": "2007-04-01",
                "vehicle_manufactured_to": "2014-07-01"}]}}, series.links[0].context))
        vehicle = types.vehicles[0]
        self.assertEqual((vehicle.key, vehicle.make, vehicle.model, vehicle.engine_code),
                         ("type:00000000349990", "TOYOTA", "Corolla", "1ND-TV"))
        self.assertEqual((vehicle.displacement_cc, vehicle.power_kw, vehicle.fuel,
                          vehicle.year_from, vehicle.year_to), (1364, 66, "diesel", 2007, 2014))
        linkage = self.adapter.parse(api_page(
            "linkage", LINKAGE, {"type": "00000000349990", "page": 1},
            {"productLinkageCollection": {"page_info": {"current_page": 1, "total_pages": 1},
                                          "items": [
                {"product_type": "Oil Filter", "product": {"sku": "W712/83_MANN-FILTER"},
                 "linkages": [{"date_interval": {"linkage_fits_from": "2007-04-01",
                                                 "linkage_fits_to": None}, "text": []}]},
                {"product_type": "Cabin Air Filter", "product": {"sku": "NOFILTER"},
                 "linkages": []}]}}, types.links[0].context))
        self.assertEqual([(fit.part.number, fit.vehicle_key, fit.position, fit.note)
                          for fit in linkage.fitments],
                         [("W712/83", "type:00000000349990", "Oil Filter", "2007-04")])

    def test_errors_fail_the_page_visibly(self):
        page = CrawledPage(graphql_url(PRODUCTS, {"page": 1, "size": 150}), "products", {}, 200,
                           b'{"errors": [{"message": "Cannot query field"}]}',
                           "application/json")
        with self.assertRaises(ValueError):
            self.adapter.parse(page)
        self.assertEqual(part_number("C2637_MANN-FILTER"), "C2637")
        self.assertEqual(brand_name("FIAT GROUP (ALFA/FIAT/LANCIA)"), "FIAT GROUP")


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {"results": {"bindings": []}}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected error status")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.queries = []

    def post(self, _url, data, timeout):
        self.queries.append(data["query"])
        return self.responses.pop(0)


def uri(identifier: str) -> dict:
    return {"type": "uri", "value": wikidata.ENTITY + identifier}


class ImporterTests(unittest.TestCase):
    def test_fueleconomy_row_becomes_a_vehicle_with_engine_detail(self):
        vehicle = fueleconomy.vehicle({
            "id": "35502", "make": "Toyota", "model": "Camry", "year": "2015", "displ": "2.5",
            "cylinders": "4", "trany": "Automatic (S6)", "drive": "Front-Wheel Drive",
            "fuelType1": "Regular Gasoline", "VClass": "Midsize Cars", "eng_dscr": "SIDI"})
        self.assertEqual((vehicle.key, vehicle.make, vehicle.model, vehicle.year_from),
                         ("fe:35502", "Toyota", "Camry", 2015))
        self.assertEqual((vehicle.displacement_cc, vehicle.fuel, vehicle.transmission,
                          vehicle.drive, vehicle.market),
                         (2500, "Regular Gasoline", "Automatic (S6)", "Front-Wheel Drive", "US"))
        self.assertEqual(vehicle.engine, "2.5L 4 cyl SIDI")
        self.assertIsNone(fueleconomy.vehicle({"id": "", "make": "Toyota"}))
        electric = fueleconomy.vehicle({"id": "1", "make": "Tesla", "model": "S",
                                        "year": "2020", "displ": "NA", "cylinders": ""})
        self.assertIsNone(electric.displacement_cc)

    def test_wikidata_rows_are_grouped_and_named(self):
        models = wikidata.collect_models([
            {"item": uri("Q1"), "maker": uri("Q10"), "brand": uri("Q11"),
             "inception": {"value": "1989-01-01T00:00:00Z"}, "followed": uri("Q2")},
            {"item": uri("Q1"), "maker": uri("Q10"), "brand": uri("Q11"),
             "discontinued": {"value": "2000-01-01T00:00:00Z"}},
            {"item": {"type": "uri", "value": "http://example.com/Q9"}}])
        self.assertEqual(list(models), ["Q1"])
        self.assertEqual((models["Q1"]["from"], models["Q1"]["to"], models["Q1"]["followed"]),
                         ([1989], [2000], ["Q2"]))
        names = wikidata.collect_names([
            {"item": uri("Q1"), "kind": {"value": "label"},
             "name": {"xml:lang": "en", "value": "Lexus  LS"}},
            {"item": uri("Q1"), "kind": {"value": "alias"},
             "name": {"xml:lang": "ka", "value": "ლექსუს LS"}},
            {"item": uri("Q10"), "kind": {"value": "label"},
             "name": {"xml:lang": "en", "value": "Toyota Motor Corporation"}},
            {"item": uri("Q11"), "kind": {"value": "label"},
             "name": {"xml:lang": "en", "value": "Lexus"}}])
        rows = wikidata.build(models, names)
        model_rows = [row for row in rows if row.kind == "model"]
        self.assertEqual({(row.language, row.name_kind, row.name) for row in model_rows},
                         {("en", "label", "Lexus LS"), ("ka", "alias", "ლექსუს LS")})
        self.assertTrue(all(row.make == "Lexus" and row.year_from == 1989
                            and row.year_to == 2000 for row in model_rows))
        self.assertEqual({row.ref for row in rows if row.kind == "make"}, {"Q10", "Q11"})

    def test_wikidata_prefers_the_make_the_model_name_starts_with(self):
        known = ["Ford Performance", "Ford", "Toyota"]
        self.assertEqual(wikidata.make_of("Ford Focus", "Ford Motor Company", known), "Ford")
        self.assertEqual(wikidata.make_of("Corolla", "Toyota Motor Corporation", known),
                         "Toyota Motor Corporation")
        self.assertEqual(wikidata.qid("http://www.wikidata.org/entity/Q53268"), "Q53268")
        self.assertEqual(wikidata.qid("http://www.wikidata.org/.well-known/genid/x"), "")
        self.assertIsNone(wikidata.year("unknown"))

    def test_wikidata_client_waits_out_a_rate_limit(self):
        waits = []
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}),
                               FakeResponse(200, {"results": {"bindings": [{"n": 1}]}})])
        client = wikidata.Wikidata("test-agent", pause=0.0, session=session,
                                   sleeper=waits.append)
        self.assertEqual(client.ask("SELECT 1"), [{"n": 1}])
        self.assertEqual(len(session.queries), 2)
        self.assertTrue(any(6.0 < wait <= 7.0 for wait in waits))


class RecordHelperTests(unittest.TestCase):
    def test_brand_and_name_normalisation(self):
        self.assertEqual(normalize_brand("Citroën"), "CITROEN")
        self.assertEqual(normalize_brand("Škoda"), "SKODA")
        self.assertEqual(normalize_brand("Mercedes-Benz"), "MERCEDESBENZ")
        self.assertEqual(normalize_name("ტოიოტა Corolla"), "ტოიოტაcorolla")
        self.assertEqual(normalize_name(" - "), "")

    def test_urls_text_and_ranges(self):
        plain = "https://www.catcar.info/toyota/?l=bWFy%3D&st=81"
        self.assertEqual(ascii_url(plain), plain)
        encoded = ascii_url("https://example.com/provent²700 x.html")
        self.assertEqual(encoded, "https://example.com/provent%C2%B2700%20x.html")
        self.assertEqual(ascii_url(encoded), encoded)
        self.assertNotIn("\x00", ascii_url("https://example.com/a\x00b"))
        self.assertEqual(clean("a\x00 b\n c"), "a b c")
        self.assertEqual((bounded(2015, 1900, 2100), bounded(3015, 1900, 2100),
                          bounded("2015", 1900, 2100), bounded(True, 0, 5)),
                         (2015, None, None, None))


if __name__ == "__main__":
    unittest.main()
