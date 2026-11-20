"""Pages for search engines: the parts pages, the sitemap and the landing's structured data."""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from automotive import site_seo
from automotive.site_catalog import Catalog
from automotive.site_seo import business_data, day_names, money, opening_hours, parts_path
from tests.test_site_catalog import row
from tests.test_site_guard import BASE_URL
from tests.test_site_pages import Page, PageRules

CAMRY = [row(f"sid-c{index}", f"Brake pad {index}", "TOYOTA CAMRY 2012-2017", "SATO",
             f"{40 + index},50", "3", "0", code=f"SECRET-CODE-{index}") for index in range(6)]
RAV4 = [row(f"sid-r{index}", f"Oil filter {index}", "TOYOTA RAV4 2013-2018", "JAPANPARTS",
            "0", "2", "0", code=f"SECRET-RAV-{index}") for index in range(2)]
HONDA = [row(f"sid-h{index}", f"Air filter {index}", "HONDA CR-V 2007-2011", "SAKURA",
             "30", "1", "0") for index in range(3)]
ROWS = CAMRY + RAV4 + HONDA


class HelperTests(unittest.TestCase):
    def test_day_codes_become_schema_day_names(self):
        self.assertEqual(day_names("Mo-Sa"), ["Monday", "Tuesday", "Wednesday", "Thursday",
                                              "Friday", "Saturday"])
        self.assertEqual(day_names("mo-fr, su"), [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Sunday"])
        self.assertEqual(day_names("Sa-Mo"), ["Saturday", "Sunday", "Monday"])
        for unreadable in ("", "Monday", "Mo-Xx"):
            self.assertEqual(day_names(unreadable), [])

    def test_only_rows_with_a_day_code_and_readable_times_become_opening_hours(self):
        rows = [{"days": {"ka": "ორშ-შაბ", "en": "Mon-Sat"}, "time": "09:30 – 19:00",
                 "days_code": "Mo-Sa"},
                {"days": {"ka": "კვ", "en": "Sun"}, "time": "closed", "days_code": "Su"},
                {"days": {"ka": "x", "en": "x"}, "time": "9.00-13.00", "days_code": ""}]
        self.assertEqual(opening_hours(rows), [{
            "@type": "OpeningHoursSpecification",
            "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "opens": "09:30", "closes": "19:00"}])

    def test_prices_are_written_as_the_page_scripts_write_them(self):
        self.assertEqual(money(1250.5, "ka", "₾", "ask"), "1 250.50 ₾")
        self.assertEqual(money(25, "en", "₾", "ask"), "₾25")
        self.assertEqual(money(None, "en", "₾", "ask"), "ask")
        self.assertEqual(money(0, "ka", "₾", "ask"), "ask")

    def test_paths(self):
        self.assertEqual(parts_path("ka"), "/parts")
        self.assertEqual(parts_path("en", "land rover", "RANGEROVER", 3),
                         "/en/parts/land-rover/rangerover?page=3")

    def test_business_data_states_only_what_the_dashboard_holds(self):
        view = {"brand": {"phone": "", "address": "", "hours": []},
                "seo": {"description": "d"}}
        empty = business_data(view, "https://g.example", "i", "l")
        self.assertEqual([node["@type"] for node in empty["@graph"]], ["WebSite"])
        view["brand"] = {
            "phone": "+995 596 33 33 74", "address": "17 Kavtaradze St", "city": "Tbilisi",
            "postal_code": "0186", "geo": "41.75903, 44.71058", "email": "",
            "map_url": "https://maps.example/x", "facebook": "https://facebook.com/g",
            "instagram": "", "tiktok": "",
            "hours": [{"days": "Mon-Sat", "time": "09:30-19:00", "days_code": "Mo-Sa"}]}
        graph = business_data(view, "https://g.example", "i", "l")["@graph"]
        business = graph[1]
        self.assertEqual(business["@type"], ["AutoRepair", "AutoPartsStore"])
        self.assertEqual(business["address"], {
            "@type": "PostalAddress", "streetAddress": "17 Kavtaradze St",
            "addressCountry": "GE", "addressLocality": "Tbilisi", "postalCode": "0186"})
        self.assertEqual(business["geo"]["latitude"], 41.75903)
        self.assertEqual(business["sameAs"], ["https://facebook.com/g"])
        self.assertNotIn("email", business)
        self.assertNotIn("aggregateRating", business)  # Google's own reviews are not ours
        self.assertEqual(graph[0]["publisher"], {"@id": "https://g.example/#business"})


class PartsPageTests(PageRules):
    def serve_parts(self):
        return self.serve(repository=None, catalog=Catalog.from_rows(ROWS))

    async def test_a_make_page_lists_its_stock_without_any_script(self):
        client = self.serve_parts()
        response = await client.get("/parts/toyota")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("x-robots-tag", response.headers)
        page = self.assert_obeys_the_policy(response.text)
        self.assert_no_staff_link(response.text)
        await self.assert_assets_exist(client, page)
        for index in range(6):
            self.assertIn(f"Brake pad {index}", page.text)
        self.assertIn("40.50 ₾", response.text)
        self.assertIn("Oil filter 0", page.text)  # a zero price is "price on request"
        self.assertNotIn("SECRET", response.text)  # shop codes never reach a page
        self.assertIn("Toyota", page.title)
        links = {(attributes.get("rel"), attributes.get("hreflang")): attributes["href"]
                 for tag, attributes in page.tags if tag == "link"}
        self.assertEqual(links[("canonical", None)], BASE_URL + "/parts/toyota")
        self.assertEqual(links[("alternate", "en")], BASE_URL + "/en/parts/toyota")
        crumbs = page.data_blocks()[0]
        self.assertEqual(crumbs["@type"], "BreadcrumbList")
        self.assertEqual([item["item"] for item in crumbs["itemListElement"]],
                         [BASE_URL + "/", BASE_URL + "/parts", BASE_URL + "/parts/toyota"])
        hrefs = [attributes.get("href") for _, attributes in page.tags]
        self.assertIn("/parts/toyota/camry", hrefs)  # six items: a model page
        self.assertNotIn("/parts/toyota/rav4", hrefs)  # two: none

    async def test_pages_exist_only_where_there_is_enough_stock(self):
        client = self.serve_parts()
        for path, status in (("/parts", 200), ("/en/parts", 200), ("/en/parts/toyota", 200),
                             ("/parts/toyota/camry", 200), ("/parts/honda", 404),
                             ("/parts/toyota/rav4", 404), ("/parts/nosuch", 404),
                             ("/parts/toyota?page=2", 404), ("/parts/toyota?page=x", 404),
                             ("/parts/toyota?page=0", 404), ("/parts?page=2", 404),
                             ("/parts/toyota?page=%C2%B2", 404),
                             ("/parts/toyota?page=" + "9" * 50, 404)):
            self.assertEqual((await client.get(path)).status_code, status, path)
        empty = self.serve(repository=None)
        self.assertEqual((await empty.get("/parts")).status_code, 404)

    async def test_long_lists_are_split_into_linked_pages(self):
        client = self.serve_parts()
        with patch.object(site_seo, "PER_PAGE", 4):
            first = Page((await client.get("/parts/toyota")).text)
            second_response = await client.get("/parts/toyota?page=2")
            self.assertEqual((await client.get("/parts/toyota?page=3")).status_code, 404)
        second = Page(second_response.text)
        self.assertIn("/parts/toyota?page=2",
                      [a.get("href") for t, a in first.tags if a.get("rel") == "next"])
        self.assertIn("/parts/toyota", [a.get("href") for t, a in second.tags
                                        if a.get("rel") == "prev"])
        canonical = [a["href"] for t, a in second.tags if a.get("rel") == "canonical"]
        self.assertEqual(canonical, [BASE_URL + "/parts/toyota?page=2"])
        self.assertIn("2", second.title)

    async def test_the_landing_page_links_to_the_parts_pages(self):
        client = self.serve_parts()
        for path, prefix in (("/", ""), ("/en", "/en")):
            page = Page((await client.get(path)).text)
            hrefs = [attributes.get("href") for _, attributes in page.tags]
            self.assertIn(f"{prefix}/parts/toyota", hrefs)
            self.assertIn(f"{prefix}/parts", hrefs)
            self.assertNotIn(f"{prefix}/parts/honda", hrefs)
            graphs = [block["@graph"] for block in page.data_blocks() if "@graph" in block]
            self.assertEqual(graphs[0][0]["@type"], "WebSite")

    async def test_the_sitemap_lists_every_parts_page_with_its_other_language(self):
        client = self.serve_parts()
        text = (await client.get("/sitemap.xml")).text
        locations = re.findall(r"<loc>([^<]+)</loc>", text)
        self.assertEqual(locations[:2], [BASE_URL + "/", BASE_URL + "/en"])
        for path in ("/parts", "/en/parts", "/parts/toyota", "/en/parts/toyota",
                     "/parts/toyota/camry"):
            self.assertIn(BASE_URL + path, locations)
        self.assertNotIn(BASE_URL + "/parts/honda", locations)
        self.assertIn(f'hreflang="en" href="{BASE_URL}/en/parts/toyota"', text)
        self.assertIn("<lastmod>2026-11-10</lastmod>", text)


class SharedModelTests(PageRules):
    async def test_a_model_written_with_two_makes_gets_a_page_under_its_own_make_only(self):
        shared = [row(f"sid-x{index}", f"Shock {index}", "TOYOTA/LEXUS RX300", "GSP", "75",
                      "1", "0") for index in range(6)]
        own = [row("sid-l1", "Mount", "LEXUS RX300", "JIKIU", "40", "1", "0")]
        client = self.serve(repository=None, catalog=Catalog.from_rows(shared + own + CAMRY))
        self.assertEqual((await client.get("/parts/lexus/rx300")).status_code, 200)
        self.assertEqual((await client.get("/parts/toyota/rx300")).status_code, 404)
        toyota = (await client.get("/parts/toyota")).text
        self.assertIn("Shock 0", toyota)  # the part still fits a Toyota and is listed there
        self.assertNotIn("/parts/toyota/rx300", toyota)
        self.assertIn("/parts/toyota/camry", toyota)
