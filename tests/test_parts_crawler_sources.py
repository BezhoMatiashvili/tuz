"""Adapter parsing against trimmed real pages in tests/fixtures/parts_crawler/."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from parts_crawler.har import har_pages
from parts_crawler.records import PartRef, year_range
from parts_crawler.sources import ADAPTERS, get_adapters
from parts_crawler.sources.base import CrawledPage, make_priority
from parts_crawler.sources.catcar import decode_step, step_labels
from parts_crawler.sources.generic import EpcAdapter, part_table_rows
from parts_crawler.sources.mann import split_application

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parts_crawler"
LEAF = ("https://www.rockauto.com/en/catalog/"
        "toyota,2015,camry,2.5l+l4,3308939,engine,oil+filter,5340")


def page(name: str, url: str, kind: str = "page", context: dict | None = None,
         content_type: str = "text/html; charset=utf-8") -> CrawledPage:
    return CrawledPage(url, kind, context or {}, 200, (FIXTURES / name).read_bytes(),
                       content_type)


class RockAutoTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["rockauto"]()

    def test_leaf_listings_become_parts_fitments_and_cross_references(self):
        result = self.adapter.parse(page("rockauto_leaf.html", LEAF, "parttype"))
        self.assertEqual([part.brand for part in result.parts],
                         ["ECOGARD", "ULTRA-POWER", "PRO-TEC"])
        self.assertEqual(result.vehicles[0].key, "3308939")
        self.assertEqual(result.vehicles[0].year_from, 2015)
        self.assertEqual({fitment.position for fitment in result.fitments}, {"Oil Filter"})
        self.assertIn(("oe", PartRef("TOYOTA", "0415231090")),
                      {(ref.relation, ref.other) for ref in result.cross_refs})
        self.assertIn(PartRef("", "PF2259"),
                      {ref.other for ref in result.cross_refs if ref.relation == "replaces"})

    def test_engine_page_queues_only_its_categories(self):
        url = "https://www.rockauto.com/en/catalog/toyota,2015,camry,2.5l+l4,3308939"
        result = self.adapter.parse(page("rockauto_engine.html", url, "engine"))
        self.assertTrue(result.links)
        self.assertTrue(all(link.kind == "category" for link in result.links))
        self.assertTrue(all(link.url.startswith(url + ",") for link in result.links))

    def test_classify_and_priority(self):
        self.assertEqual(self.adapter.classify(LEAF), "parttype")
        self.assertIsNone(self.adapter.classify("https://www.rockauto.com/info/x.jpg"))
        self.assertLess(make_priority("Toyota"), make_priority("Allard"))


class MannTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["mann"]()

    def test_product_json_ld(self):
        url = ("https://www.mann-filter.com/en/catalog/search-results/product.html/"
               "w712/96_mann-filter.html")
        result = self.adapter.parse(page("mann_product.html", url, "product"))
        self.assertEqual(result.parts[0].number, "W 712/96")
        self.assertEqual(result.parts[0].name, "Oil Filter")
        self.assertEqual({ref.other.number for ref in result.cross_refs},
                         {"CC1046088", "42579368"})
        self.assertTrue(result.vehicles)
        self.assertEqual(len(result.vehicles), len(result.fitments))

    def test_sitemap_links_only_products(self):
        result = self.adapter.parse(page("mann_sitemap.xml", "https://www.mann-filter.com/"
                                         "en.sitemap-commerce.xml", "sitemap",
                                         content_type="application/xml"))
        self.assertTrue(result.links)
        self.assertTrue(all(link.kind == "product" for link in result.links))

    def test_application_make_split(self):
        self.assertEqual(split_application("BMW 3 (F30)"), ("BMW", "3 (F30)"))
        self.assertEqual(split_application("HEULIEZ BUS (IVECO BUS) GX-Serie"),
                         ("HEULIEZ BUS (IVECO BUS)", "GX-Serie"))


class NgkTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["ngk"]()

    def test_year_page_results(self):
        url = "https://www.ngkpartfinder.co.uk/catalogues/cars/search/glow-plugs/AUDI/100/1990/"
        result = self.adapter.parse(page("ngk_year.html", url, "year"))
        self.assertEqual(result.links, [])
        self.assertEqual({part.number for part in result.parts}, {"Y-918J", "Y-732J"})
        vehicle = result.vehicles[0]
        self.assertEqual((vehicle.make, vehicle.model), ("AUDI", "100"))
        self.assertEqual((vehicle.year_from, vehicle.year_to), (1982, 1991))
        self.assertTrue(all(ref.other.brand == "NGK" for ref in result.cross_refs))

    def test_make_page_queues_models_of_that_make_only(self):
        url = "https://www.ngkpartfinder.co.uk/catalogues/cars/search/glow-plugs/AUDI/"
        result = self.adapter.parse(page("ngk_make.html", url, "make"))
        self.assertTrue(result.links)
        self.assertTrue(all(link.url.startswith(url) and link.kind == "model"
                            for link in result.links))


class CatCarAndGenericTests(unittest.TestCase):
    def test_step_parameter_decodes_breadcrumb(self):
        raw = "sts=={\"10\":\"Market\",\"20\":\"EUROPE\"}||st==20||market==euro"
        url = "https://www.catcar.info/toyota/?l=" + base64.b64encode(raw.encode()).decode()
        self.assertEqual(decode_step(url)["market"], "euro")
        self.assertEqual(step_labels(url), ["Market", "EUROPE"])

    def test_catcar_market_links_go_one_step_down(self):
        adapter = ADAPTERS["catcar"]()
        result = adapter.parse(page("catcar/toyota_markets.html",
                                    "https://www.catcar.info/toyota/", "step"))
        self.assertEqual(len(result.links), 4)
        self.assertTrue(all(link.url.startswith("https://www.catcar.info/toyota/?l=")
                            for link in result.links))
        self.assertEqual({decode_step(link.url)["st"] for link in result.links}, {"20"})
        # Rebuilt URLs carry no breadcrumb, so one page has one URL whatever led to it.
        self.assertTrue(all("sts" not in decode_step(link.url) for link in result.links))
        self.assertEqual(len(result.links[0].context["path"]), 1)

    def test_generic_part_table_and_fitment(self):
        html = """<html><body><ul class="breadcrumb"><li><a>Home</a></li><li><a>Toyota</a></li>
            <li><a>Camry 2015</a></li><li><span>Oil filter</span></li></ul>
            <table><tr><th>No.</th><th>Part number</th><th>Name</th><th>Qty</th></tr>
            <tr><td>1</td><td>04152-YZZA1</td><td>Element kit, oil filter</td><td>1</td></tr>
            <tr><td>2</td><td>see note</td><td>Gasket</td><td>1</td></tr></table></body></html>"""

        class Example(EpcAdapter):
            name = "example"
            hosts = frozenset({"example.com"})
            follow_prefixes = ("/",)

        result = Example().parse(CrawledPage("https://example.com/x", "page", {}, 200,
                                             html.encode(), "text/html"))
        self.assertEqual([part.number for part in result.parts], ["04152-YZZA1"])
        self.assertEqual(result.parts[0].brand, "Toyota")
        self.assertEqual(result.vehicles[0].year_from, 2015)
        self.assertEqual(result.fitments[0].position, "Oil filter")
        self.assertEqual(part_table_rows(BeautifulSoup("<table></table>", "html.parser")), [])

    def test_year_ranges(self):
        self.assertEqual(year_range("10/91→12/94"), (1991, 1994))
        self.assertEqual(year_range("2015→"), (2015, None))
        self.assertEqual(year_range("no years"), (None, None))

    def test_registry_and_unknown_source(self):
        manual = {"realoem", "partsouq"}
        self.assertTrue(all(ADAPTERS[name].manual_capture for name in manual))
        # "all" leaves out sources that can only be filled from recorded HAR files.
        self.assertEqual({adapter.name for adapter in get_adapters("all")},
                         set(ADAPTERS) - manual)
        self.assertEqual([adapter.name for adapter in get_adapters("realoem")], ["realoem"])
        self.assertNotIn("sevenzap", ADAPTERS)
        with self.assertRaises(SystemExit):
            get_adapters("nope")


class HarTests(unittest.TestCase):
    def test_har_keeps_only_successful_catalog_pages(self):
        adapter = ADAPTERS["rockauto"]()
        entries = [
            {"request": {"method": "GET", "url": LEAF, "headers": [{"name": "Cookie",
                                                                    "value": "secret"}]},
             "response": {"status": 200, "content": {"mimeType": "text/html",
                                                     "text": "<html>leaf</html>"}}},
            {"request": {"method": "GET", "url": "https://www.rockauto.com/info/1.jpg"},
             "response": {"status": 200, "content": {"text": "x"}}},
            {"request": {"method": "GET", "url": LEAF + "x"},
             "response": {"status": 404, "content": {"text": "missing"}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
            pages = list(har_pages(path, adapter))
        self.assertEqual([(url, kind) for url, kind, _, _ in pages], [(LEAF, "parttype")])
        self.assertNotIn(b"secret", pages[0][2])


if __name__ == "__main__":
    unittest.main()
