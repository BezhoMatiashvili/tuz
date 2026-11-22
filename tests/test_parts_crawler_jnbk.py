"""JNBK (NiBK) brake catalogue adapter against trimmed real pages."""

from __future__ import annotations

import unittest
from pathlib import Path

from parts_crawler.records import PartRef
from parts_crawler.sources import ADAPTERS, get_adapters
from parts_crawler.sources.base import CrawledPage
from parts_crawler.sources.jnbk import jnbk_years

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parts_crawler"
ROOT = "https://www.jnbk-brakes.com"
CATALOGUE = f"{ROOT}/catalogue/cars"
MODELS = f"{ROOT}/application/get_application_models/88/1/0"
APPLICATIONS = f"{ROOT}/application/get_applications/2434/1/0/0/0"
PART = f"{ROOT}/catalogue/cars/brake/1383/PN1524"


def page(name: str | None, url: str, context: dict | None = None,
         body: bytes | None = None) -> CrawledPage:
    adapter = ADAPTERS["jnbk"]()
    data = body if body is not None else (FIXTURES / name).read_bytes()
    return CrawledPage(url, adapter.classify(url) or "page", context or {}, 200, data,
                       "text/html; charset=UTF-8")


class JnbkYearTests(unittest.TestCase):
    def test_month_dot_year_ranges(self):
        self.assertEqual(jnbk_years("01.77~11.79"), (1977, 1979))
        self.assertEqual(jnbk_years("02.19~12.22"), (2019, 2022))
        self.assertEqual(jnbk_years("03.20~"), (2020, None))
        self.assertEqual(jnbk_years("05.04"), (2004, 2004))
        self.assertEqual(jnbk_years(""), (None, None))


class JnbkAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ADAPTERS["jnbk"]()

    def test_registered_and_crawled_with_the_site_header(self):
        self.assertIn("jnbk", {adapter.name for adapter in get_adapters("all")})
        self.assertEqual(self.adapter.request_headers, {"X-Requested-With": "XMLHttpRequest"})
        self.assertTrue(self.adapter.embeds_captcha_widget)
        self.assertEqual([link.url for link in self.adapter.seeds()], [CATALOGUE])

    def test_classify(self):
        self.assertEqual(self.adapter.classify(CATALOGUE), "catalogue")
        self.assertEqual(self.adapter.classify(MODELS), "models")
        self.assertEqual(self.adapter.classify(APPLICATIONS), "applications")
        self.assertEqual(self.adapter.classify(PART), "part")
        self.assertEqual(self.adapter.classify(f"{ROOT}/catalogue/cars/brake/1/PN1524/E"),
                         "part")
        self.assertIsNone(self.adapter.classify(f"{ROOT}/application/get_years/2434/0/1"))
        self.assertIsNone(self.adapter.classify("https://www.nibk.jp/catalogue/cars"))

    def test_catalogue_queues_every_maker_popular_first(self):
        result = self.adapter.parse(page("jnbk_catalogue.html", CATALOGUE))
        by_make = {link.context["make"]: link for link in result.links}
        self.assertEqual(set(by_make), {"ACURA", "BMW", "TOYOTA", "TOYOTA (FAW)"})
        self.assertEqual(by_make["TOYOTA"].url, MODELS)
        self.assertTrue(all(link.kind == "models" for link in result.links))
        self.assertLess(by_make["TOYOTA"].priority, by_make["BMW"].priority)
        self.assertLess(by_make["BMW"].priority, by_make["ACURA"].priority)

    def test_models_queue_one_applications_call_each(self):
        result = self.adapter.parse(page("jnbk_models.json", MODELS, {"make": "TOYOTA"}))
        links = {link.context["model"]: link for link in result.links}
        self.assertEqual(set(links), {"4 Runner", "Camry", "Corolla"})
        self.assertEqual(links["Corolla"].url, APPLICATIONS)
        self.assertEqual(links["Corolla"].context["make"], "TOYOTA")
        self.assertTrue(all(link.kind == "applications" for link in result.links))

    def test_applications_become_vehicles_parts_and_fitments(self):
        result = self.adapter.parse(page("jnbk_applications.html", APPLICATIONS,
                                         {"make": "TOYOTA", "model": "Corolla"}))
        first = result.vehicles[0]
        self.assertEqual((first.make, first.model), ("TOYOTA", "Corolla"))
        self.assertEqual((first.year_from, first.year_to), (1977, 1979))
        self.assertEqual((first.displacement_cc, first.engine_code, first.model_code),
                         (1200, "3KC", "KE50"))
        self.assertEqual(len({vehicle.key for vehicle in result.vehicles}), 3)
        self.assertIn((2016, None), {(v.year_from, v.year_to) for v in result.vehicles})

        parts = {(part.brand, part.number): part for part in result.parts}
        self.assertEqual(parts[("NiBK", "RN1822")].name, "Rotor disc")
        self.assertEqual(parts[("NiBK", "RN1822")].kind, "aftermarket")
        # A plain number in a cell is Toyota's own part for that position.
        self.assertEqual(parts[("TOYOTA", "4351202340")].kind, "oe")
        fitments = {(f.part.number, f.vehicle_key): f.position for f in result.fitments}
        self.assertEqual(fitments[("RN1822", first.key)], "Front Rotor")
        self.assertEqual({f.position for f in result.fitments
                          if f.part == PartRef("TOYOTA", "0446602350")}, {"Rear Brake"})

        part_links = {link.url for link in result.links}
        self.assertIn(f"{ROOT}/catalogue/cars/brake/615249/RN1822", part_links)
        self.assertTrue(all(link.kind == "part" for link in result.links))
        self.assertEqual(len(part_links), len(result.links))

    def test_part_page_cross_references_and_variants(self):
        result = self.adapter.parse(page("jnbk_part.html", PART, {"make": "TOYOTA"}))
        self.assertEqual([(p.brand, p.number, p.name) for p in result.parts],
                         [("NiBK", "PN1524", "Brake pads")])
        relations = {(ref.other.brand, ref.other.number): ref.relation
                     for ref in result.cross_refs}
        # Car makers named on the page, and the group in 'PONTIAC (GM)', are OE.
        self.assertEqual(relations[("TOYOTA", "04465-02220")], "oe")
        self.assertEqual(relations[("LEXUS", "04465-42160-79")], "oe")
        self.assertEqual(relations[("GM", "19169642")], "oe")
        self.assertEqual(relations[("AKEBONO", "AN742K")], "equivalent")
        self.assertEqual(relations[("FMSI", "D1210-8330")], "equivalent")
        self.assertEqual(relations[("NiBK", "PN1524/E")], "equivalent")
        self.assertEqual({link.url for link in result.links}, {
            f"{ROOT}/catalogue/cars/brake/776250/PN1524S",
            f"{ROOT}/catalogue/cars/brake/111533895/PN1524/E"})
        self.assertEqual(result.vehicles, [])

    def test_slash_number_whose_title_drops_the_slash(self):
        # Seen live: /brake/103420188/PN1543/E is titled '... : BRAKE PADS : PN1543E'.
        body = (FIXTURES / "jnbk_part.html").read_bytes().replace(
            b": PN1524</title>", b": PN1524E</title>")
        result = self.adapter.parse(page(None, f"{ROOT}/catalogue/cars/brake/111533895/PN1524/E",
                                         body=body))
        self.assertEqual([(p.brand, p.number) for p in result.parts], [("NiBK", "PN1524/E")])
        # Its own number in the variant list is not cross-referenced to itself.
        self.assertNotIn(("NiBK", "PN1524/E"),
                         {(ref.other.brand, ref.other.number) for ref in result.cross_refs})

    def test_soft_failures_raise_instead_of_saving_an_empty_page(self):
        with self.assertRaises(ValueError):  # /application/ call without the XHR header
            self.adapter.parse(page(None, MODELS, body=b""))
        with self.assertRaises(ValueError):  # the site's home page for an unknown URL
            self.adapter.parse(page("jnbk_catalogue.html", APPLICATIONS))
        with self.assertRaises(ValueError):  # a part page that shows another part
            self.adapter.parse(page("jnbk_part.html", f"{ROOT}/catalogue/cars/brake/1/RN1822"))
        with self.assertRaises(ValueError):
            self.adapter.parse(page(None, CATALOGUE, body=b"<html><body></body></html>"))

    def test_model_without_applications_is_empty_not_an_error(self):
        result = self.adapter.parse(page(None, APPLICATIONS, body=b"\n<!-- divApp -->\n"))
        self.assertEqual((result.vehicles, result.links), ([], []))


if __name__ == "__main__":
    unittest.main()
