"""Public pages and assets: the rules a page must keep, whatever its markup looks like.

The templates are being designed separately, so nothing here names an element, a class or
an id. A page passes when it obeys the Content-Security-Policy it is served with, escapes
stored content, shows what the dashboard stores and never links to the staff sign-in.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from automotive import site_pages
from automotive.site_catalog import Catalog
from automotive.site_content import DEFAULT_SECTIONS, DEFAULT_SERVICES, SECTIONS
from automotive.site_pages import Assets, MediaCache
from tests.test_site_catalog import ROWS
from tests.test_site_guard import BASE_URL, IMMUTABLE, Clock, SiteAppCase
from tests.test_site_media import png

SCRIPT = "<script>alert(1)</script>"
ATTRIBUTE = '"><img src=x onerror=alert(1)>'
BREAKOUT = "</script><script>alert(1)</script><!--"
DATA_TYPES = {"application/json", "application/ld+json"}
LOADING_RELS = {"stylesheet", "preload", "modulepreload", "prefetch", "icon", "manifest",
                "apple-touch-icon"}
FETCHING_TAGS = {"script", "img", "source", "video", "audio", "iframe", "track", "form"}
STAFF_PATH = re.compile(r"(?<![\w/.-])/(login|admin|staff)(?![\w.-])")


class Page(HTMLParser):
    """What the rules are about: tags with attributes, scripts, styles and visible text."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self.styles: list[str] = []
        self.title = ""
        self._texts: list[str] = []
        self._raw: tuple[str, dict, list[str]] | None = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag in ("script", "style", "title"):
            self._raw = (tag, attributes, [])

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if self._raw and self._raw[0] == tag:
            _, attributes, parts = self._raw
            content = "".join(parts)
            if tag == "script":
                self.scripts.append((attributes, content))
            elif tag == "style":
                self.styles.append(content)
            else:
                self.title = " ".join(content.split())
            self._raw = None

    def handle_data(self, data: str) -> None:
        (self._raw[2] if self._raw else self._texts).append(data)

    @property
    def text(self) -> str:
        """Visible text with whitespace collapsed, so markup between words does not matter."""
        return " ".join(" ".join(self._texts).split())

    @property
    def values(self) -> list[str]:
        return [value for _, attributes in self.tags for value in attributes.values() if value]

    def data_blocks(self) -> list:
        return [json.loads(content) for attributes, content in self.scripts
                if (attributes.get("type") or "").lower() in DATA_TYPES]

    def urls(self) -> list[tuple[str, dict, str, str]]:
        """(tag, its attributes, attribute name, url) wherever the browser fetches or goes."""
        found = []
        for tag, attributes in self.tags:
            for name in ("href", "src", "action", "formaction", "poster", "data"):
                if attributes.get(name):
                    found.append((tag, attributes, name, attributes[name].strip()))
            for candidate in (attributes.get("srcset") or "").split(","):
                if candidate.strip():
                    found.append((tag, attributes, "srcset", candidate.split()[0]))
        return found


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    return []


def same_origin(url: str) -> bool:
    parts = urlsplit(url)
    return (not parts.scheme and not parts.netloc) or url.startswith(BASE_URL + "/")


class PageRules(SiteAppCase):
    """Assertions shared by every page test."""

    def assert_obeys_the_policy(self, html: str) -> Page:
        """No inline style or script, no inline handler, nothing loaded from elsewhere."""
        page = Page(html)
        self.assertNotIn("<style", html.lower())
        self.assertNotIn('style="', html.lower())
        self.assertEqual(page.styles, [])
        for tag, attributes in page.tags:
            for name, value in attributes.items():
                self.assertNotEqual(name, "style", f"<{tag}> has an inline style")
                self.assertFalse(name.startswith("on"), f"<{tag} {name}> is an inline handler")
                self.assertFalse((value or "").strip().lower().startswith("javascript:"),
                                 f"<{tag} {name}> is a javascript: URL")
            self.assertNotIn(tag, ("object", "embed", "base"))
            if tag == "meta":
                self.assertNotEqual((attributes.get("http-equiv") or "").lower(), "refresh")
        for attributes, content in page.scripts:
            if (attributes.get("type") or "").lower() in DATA_TYPES:
                self.assertNotIn("src", attributes)
                try:
                    json.loads(content)
                except ValueError:
                    self.fail(f"a data block is not valid JSON: {content[:60]!r}")
            else:
                self.assertTrue(attributes.get("src"), f"inline <script>: {content[:60]!r}")
                self.assertEqual(content.strip(), "")
        for tag, attributes, name, url in page.urls():
            if tag == "link":  # canonical and alternate links name the site, they load nothing
                fetched = bool(LOADING_RELS & set((attributes.get("rel") or "").lower().split()))
            else:
                fetched = tag in FETCHING_TAGS or name in ("poster", "srcset", "formaction")
            if fetched:
                self.assertTrue(same_origin(url), f"<{tag} {name}={url!r}> leaves the origin")
                self.assertFalse(url.lower().startswith(("data:", "blob:")), url)
        return page

    def assert_no_staff_link(self, html: str) -> None:
        page = Page(html)
        for tag, _, name, url in page.urls():
            path = urlsplit(url).path.rstrip("/")
            self.assertNotIn(path, ("/login", "/admin", "/staff"), f"<{tag} {name}={url!r}>")
            self.assertFalse(path.startswith(("/login/", "/admin/", "/api/admin", "/api/auth")),
                             f"<{tag} {name}={url!r}>")
        self.assertIsNone(STAFF_PATH.search(html), "a staff path is named in the page")

    async def assert_assets_exist(self, client, page: Page) -> None:
        for _, _, _, url in page.urls():
            if urlsplit(url).path.startswith("/assets/"):
                response = await client.get(urlsplit(url).path)
                self.assertEqual(response.status_code, 200, f"{url} is referenced but missing")


class LandingPageTests(PageRules):
    async def test_both_languages_render_without_a_site_database(self):
        client = self.serve(repository=None)
        for path, language in (("/", "ka"), ("/en", "en")):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response.headers["content-type"].startswith("text/html"))
            page = self.assert_obeys_the_policy(response.text)
            self.assert_no_staff_link(response.text)
            await self.assert_assets_exist(client, page)
            self.assertEqual(dict(page.tags)["html"].get("lang"), language)
            self.assertEqual(page.title, DEFAULT_SECTIONS["seo"]["title"][language])
            # The packaged texts and services are what a fresh installation shows.
            self.assertIn(DEFAULT_SECTIONS["hero"]["title"][language], page.text)
            for service in DEFAULT_SERVICES:
                self.assertIn(service[f"title_{language}"], page.text)

    async def test_the_wall_that_explains_the_name_is_shown_and_described(self):
        client = self.serve(repository=None)
        for path, language in (("/", "ka"), ("/en", "en")):
            page = Page((await client.get(path)).text)
            described = site_pages.STRINGS[language]["story.wall_alt"]
            walls = [attributes for tag, attributes in page.tags
                     if tag == "img" and attributes.get("alt") == described]
            self.assertEqual(len(walls), 1, path)
            # Sized in the markup, so the text beside it does not move while it loads.
            self.assertTrue(walls[0].get("width") and walls[0].get("height"), path)
            for candidate in walls[0]["srcset"].split(","):
                url, _, width = candidate.strip().partition(" ")
                self.assertTrue(width.endswith("w"), candidate)
                self.assertEqual((await client.get(urlsplit(url).path)).status_code, 200, url)

    async def test_rules_hold_with_the_chat_on_and_a_loaded_catalog(self):
        client = self.serve(public_search=object(), catalog=Catalog.from_rows(ROWS))
        self.addCleanup(self.app.state.site.chat.close)
        for path in ("/", "/en"):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200)
            page = self.assert_obeys_the_policy(response.text)
            self.assert_no_staff_link(response.text)
            await self.assert_assets_exist(client, page)
            # Nothing from the catalogue's private side is embedded in the page.
            for private in ("sid-000", "ZQ-1234", "04465"):
                self.assertNotIn(private, response.text)

    async def test_the_page_tells_its_scripts_the_language_stock_and_chat_state(self):
        def site_data(html: str) -> dict:
            return next(block for block in Page(html).data_blocks()
                        if isinstance(block, dict) and "inStock" in block)

        client = self.serve(repository=None, catalog=None)
        pending = site_data((await client.get("/en")).text)
        self.assertEqual((pending["lang"], pending["inStock"], pending["currency"]),
                         ("en", None, "₾"))
        self.assertEqual(pending["chat"], {
            "enabled": False, "greeting": DEFAULT_SECTIONS["chat"]["greeting"]["en"]})
        self.assertTrue(pending["strings"])
        client = self.serve(repository=None, public_search=object(),
                            catalog=Catalog.from_rows(ROWS))
        self.addCleanup(self.app.state.site.chat.close)
        loaded = site_data((await client.get("/")).text)
        self.assertEqual((loaded["lang"], loaded["inStock"], loaded["chat"]["enabled"]),
                         ("ka", 6, True))
        self.assertNotEqual(loaded["strings"], pending["strings"])  # Georgian, not English

    def store_hostile_content(self) -> None:
        """Every editable text carries markup, as a careless or hostile editor might."""
        # Some of it is longer than its field allows, which the loader reports once.
        self.enterContext(self.assertLogs("automotive.site_content", "WARNING"))
        def both(text: str) -> dict[str, str]:
            return {"ka": text, "en": text}

        documents = {
            "brand": {"phone": "+995 32 212 34 56", "whatsapp": "+995 599 12 34 56",
                      "email": "info@garage.example", "address": both(ATTRIBUTE),
                      "map_url": "https://maps.example.ge/garage", "currency": "<b>",
                      "hours": [{"days": both(SCRIPT), "time": ATTRIBUTE}],
                      "facebook": "https://facebook.example/garage", "instagram": "",
                      "tiktok": ""},
            "hero": {"title": both(SCRIPT), "subline": both(ATTRIBUTE)},
            "checklist": {"items": [{"label": both(SCRIPT)}] * 4},
            "engine": {name: both(SCRIPT if name.endswith("title") else ATTRIBUTE)
                       for name in DEFAULT_SECTIONS["engine"]},
            "parts": {"title": both(SCRIPT), "intro": both(ATTRIBUTE)},
            "services_intro": {"title": both(SCRIPT), "intro": both(ATTRIBUTE)},
            "story": {"title": both(SCRIPT), "statement": both(ATTRIBUTE), "milestones": [
                {"year": "1886", "title": both(SCRIPT), "text": both(ATTRIBUTE)}] * 3},
            "gallery": {"title": both(SCRIPT)},
            "visit": {"title": both(SCRIPT), "note": both(ATTRIBUTE)},
            "seo": {"title": both(SCRIPT), "description": both(ATTRIBUTE)},
            "chat": {"enabled": True, "launcher": both(SCRIPT), "greeting": both(BREAKOUT)},
        }
        self.assertEqual(set(documents), set(SECTIONS))
        for key, document in documents.items():
            self.site_repository.sections[key] = {"section_key": key, "document": document,
                                                  "version": 1}
        self.site_repository.create_service({
            "icon": "wrench", "title_ka": SCRIPT, "title_en": SCRIPT, "summary_ka": ATTRIBUTE,
            "summary_en": ATTRIBUTE, "price_from": 45, "is_published": True})
        self.site_repository.add_media(
            sha256="a" * 64, content_type="image/png", data=png(), width=1, height=1,
            alt_ka=ATTRIBUTE, alt_en=ATTRIBUTE, published=True)

    async def test_stored_markup_is_shown_as_text_never_run(self):
        self.store_hostile_content()
        client = self.serve(public_search=object())
        self.addCleanup(self.app.state.site.chat.close)
        for path in ("/", "/en"):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.text
            page = self.assert_obeys_the_policy(html)
            # The markup did not become elements...
            self.assertNotIn(SCRIPT, html)
            self.assertNotIn("<img src=x", html)
            for tag, attributes in page.tags:
                self.assertNotIn("onerror", attributes, tag)
                self.assertNotEqual(attributes.get("src"), "x", tag)
            for attributes, content in page.scripts:
                if (attributes.get("type") or "").lower() not in DATA_TYPES:
                    self.assertNotIn("alert(1)", content)
            # ...it is on the page as the text the editor typed.
            self.assertIn(SCRIPT, page.text)
            self.assertEqual(page.title, SCRIPT)
            self.assertTrue(ATTRIBUTE in page.text or ATTRIBUTE in page.values)

    async def test_stored_markup_cannot_break_out_of_the_data_block(self):
        self.store_hostile_content()
        client = self.serve(public_search=object())
        self.addCleanup(self.app.state.site.chat.close)
        for path in ("/", "/en"):
            html = (await client.get(path)).text
            page = Page(html)
            blocks = [(attributes, content) for attributes, content in page.scripts
                      if (attributes.get("type") or "").lower() in DATA_TYPES]
            self.assertTrue(blocks, "the page hands no data block to its scripts")
            carried = []
            for _, content in blocks:
                # Inside a script element only `</script` ends it; none may appear raw.
                self.assertNotIn("</script", content.lower())
                self.assertNotIn("<script", content.lower())
                self.assertNotIn("<!--", content)
                carried += strings_in(json.loads(content))
            # The greeting survives the round trip exactly, so it was encoded, not dropped.
            self.assertIn(BREAKOUT, carried)
            self.assertIn("<b>", carried)

    async def test_a_service_price_is_written_the_way_each_language_writes_money(self):
        self.site_repository.create_service({
            "icon": "disc", "title_ka": "სერვისი", "title_en": "Service", "summary_ka": "",
            "summary_en": "", "price_from": 1250, "is_published": True})
        client = self.serve()
        english, georgian = (await client.get("/en")).text, (await client.get("/")).text
        # Georgian: the amount, then the sign. English: the sign first. The spaces are
        # non-breaking, so a price never wraps in the middle.
        self.assertIn("1\u00a0250\u00a0₾-დან", georgian)
        self.assertIn("from ₾1\u00a0250", english)
        self.assertNotIn("₾1", georgian)

    async def test_what_the_dashboard_stores_is_what_the_page_shows(self):
        hero = {"title": {"ka": "ახალი სათაური", "en": "A brand new headline"},
                "subline": {"ka": "ახალი ქვესათაური", "en": "A brand new subline"}}
        seo = {"title": {"ka": "ახალი გვერდი", "en": "A brand new page title"},
               "description": {"ka": "აღწერა", "en": "Description"}}
        for key, document in (("hero", hero), ("seo", seo)):
            self.site_repository.sections[key] = {"section_key": key, "document": document,
                                                  "version": 4}
        shown = {"icon": "disc", "title_ka": "ხილული სერვისი", "title_en": "Visible service",
                 "summary_ka": "", "summary_en": "", "price_from": None, "is_published": True}
        self.site_repository.create_service(shown)
        self.site_repository.create_service({**shown, "title_ka": "დამალული სერვისი",
                                             "title_en": "Hidden service",
                                             "is_published": False})
        # One published photo is enough to show the gallery, with its editable heading.
        self.site_repository.sections["gallery"] = {
            "section_key": "gallery", "version": 2,
            "document": {"title": {"ka": "ჩვენი კედლები", "en": "Our painted walls"}}}
        for name, published in (("a", True), ("b", False)):
            self.site_repository.add_media(
                sha256=name * 64, content_type="image/png", data=png(), width=1, height=1,
                alt_ka="კედელი", alt_en="A painted wall", published=published)
        client = self.serve()
        english, georgian = Page((await client.get("/en")).text), Page((await client.get("/")).text)
        self.assertIn("Our painted walls", english.text)
        self.assertIn("ჩვენი კედლები", georgian.text)
        for page, alt in ((english, "A painted wall"), (georgian, "კედელი")):
            photos = {attributes.get("src"): attributes.get("alt")
                      for tag, attributes in page.tags if tag == "img"}
            self.assertEqual(photos.get("/media/" + "a" * 64), alt)
            self.assertNotIn("/media/" + "b" * 64, photos)  # unpublished stays out
        self.assertIn("A brand new headline", english.text)
        self.assertIn("ახალი სათაური", georgian.text)
        self.assertEqual((english.title, georgian.title),
                         ("A brand new page title", "ახალი გვერდი"))
        self.assertIn("Visible service", english.text)
        self.assertIn("ხილული სერვისი", georgian.text)
        for page in (english, georgian):
            self.assertNotIn("Hidden service", page.text)
            self.assertNotIn("დამალული სერვისი", page.text)
            # The packaged defaults are gone once the database has its own rows.
            self.assertNotIn(DEFAULT_SECTIONS["hero"]["title"]["en"], page.text)
            self.assertNotIn("Suspension and steering", page.text)
            self.assertNotIn("სავალი ნაწილი", page.text)

    async def test_the_page_still_renders_when_the_site_database_fails(self):
        self.site_repository.failing.add("load_content")
        client = self.serve()
        with self.assertLogs("automotive.site_content", "WARNING"):
            response = await client.get("/en")
        self.assertEqual(response.status_code, 200)
        self.assertIn(DEFAULT_SECTIONS["hero"]["title"]["en"], Page(response.text).text)

    async def test_head_and_compressed_requests_serve_the_same_page(self):
        client = self.serve(repository=None)
        head = await client.head("/")
        self.assertEqual((head.status_code, head.content), (200, b""))
        self.assertTrue(head.headers["content-type"].startswith("text/html"))
        plain = await client.get("/en", headers={"Accept-Encoding": "identity"})
        packed = await client.get("/en", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.text, packed.text)
        self.assertIn("Accept-Encoding", packed.headers["vary"])
        if len(plain.content) > 1024:
            self.assertEqual(packed.headers["content-encoding"], "gzip")

    async def test_links_to_the_site_itself_use_the_configured_public_origin(self):
        origin = "https://garage.example"
        settings = replace(
            self.settings, public_origins=(origin,),
            allowed_origins=(*self.settings.allowed_origins, origin),
            allowed_hosts=(*self.settings.allowed_hosts, "garage.example"))
        client = self.serve(settings, repository=None)
        robots = await client.get("/robots.txt")
        self.assertIn(f"Sitemap: {origin}/sitemap.xml", robots.text)
        sitemap = await client.get("/sitemap.xml")
        self.assertEqual(re.findall(r"<loc>([^<]+)</loc>", sitemap.text),
                         [origin + "/", origin + "/en"])
        for path in ("/", "/en"):
            html = (await client.get(path)).text
            # The request came in as localhost; that name must not become a site link.
            for _, attributes in Page(html).tags:
                if (attributes.get("rel") or "").lower() in ("canonical", "alternate"):
                    self.assertTrue(attributes["href"].startswith(origin + "/"), attributes)
            self.assertNotIn("localhost", html)


class OtherPageTests(PageRules):
    async def test_login_page_obeys_the_policy_and_reflects_nothing(self):
        client = self.serve(repository=None)
        for path in ("/login", "/login?next=staff", "/login?next=https://evil.example/x",
                     "/login?next=%22%3E%3Cscript%3Ealert(1)%3C/script%3E"):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200, path)
            page = self.assert_obeys_the_policy(response.text)
            await self.assert_assets_exist(client, page)
            self.assertNotIn("evil.example", response.text)
            self.assertNotIn("alert(1)", response.text)
            self.assertIn("noindex", response.headers["x-robots-tag"])
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((await client.head("/login")).status_code, 200)

    async def test_dashboard_shell_obeys_the_policy(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        for path in ("/admin", "/admin/content", "/admin/users/3"):
            response = await client.get(path, headers=cookie)
            self.assertEqual(response.status_code, 200, path)
            page = self.assert_obeys_the_policy(response.text)
            await self.assert_assets_exist(client, page)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("noindex", response.headers["x-robots-tag"])
            # The shell carries no session or account secret; its scripts ask the API.
            for private in ("scrypt$", cookie["Cookie"].partition("=")[2]):
                self.assertNotIn(private, response.text)

    async def test_robots_and_sitemap_do_not_advertise_staff_paths(self):
        client = self.serve(repository=None)
        robots = await client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertTrue(robots.headers["content-type"].startswith("text/plain"))
        self.assertIn("User-agent: *", robots.text)
        self.assertIn(f"Sitemap: {BASE_URL}/sitemap.xml", robots.text)
        sitemap = await client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertEqual(re.findall(r"<loc>([^<]+)</loc>", sitemap.text),
                         [BASE_URL + "/", BASE_URL + "/en"])
        for text in (robots.text, sitemap.text):
            for staff in ("/login", "/admin", "/staff", "/api"):
                self.assertNotIn(staff, text.replace(BASE_URL, ""))

    async def test_assets_are_served_by_type_and_only_from_the_asset_roots(self):
        client = self.serve(repository=None)
        script = "export const answer = 42;\n" * 60
        assets = self.use_assets({
            "site/js/app.js": script, "site/css/app.css": "body { margin: 0 }",
            "site/dev/harness.js": "private-dev-fixture", "site/notes.md": "private-notes",
            "admin/js/admin.js": "export {};", "secret.txt": "private-outside-fixture"})
        build = assets.build()
        served = await client.get(f"/assets/{build}/site/js/app.js")
        self.assertEqual((served.status_code, served.text), (200, script))
        self.assertEqual(served.headers["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(served.headers["content-encoding"], "gzip")
        self.assertEqual(served.headers["cache-control"], IMMUTABLE)
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        plain = await client.get(f"/assets/{build}/site/js/app.js",
                                 headers={"Accept-Encoding": "identity"})
        self.assertEqual((plain.content, plain.headers.get("content-encoding")),
                         (script.encode(), None))
        self.assertEqual((await client.get(f"/assets/{build}/admin/js/admin.js")).status_code,
                         200)
        self.assertEqual((await client.head(f"/assets/{build}/site/css/app.css")).status_code,
                         200)
        refused = ("site/dev/harness.js", "site/notes.md", "site/missing.js", "secret.txt",
                   "site/%2e%2e/secret.txt", "site/..%2fsecret.txt", "site/js/", "site",
                   "other/js/app.js", "site//js/app.js", "site/js/app.js%00.css")
        for path in refused:
            response = await client.get(f"/assets/{build}/{path}")
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("private-", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_published_gallery_images_are_public_and_others_are_not(self):
        client = self.serve()
        data = png(3, 2)
        shown = self.site_repository.add_media(
            sha256="a" * 64, content_type="image/png", data=data, width=3, height=2,
            published=True)
        self.site_repository.add_media(
            sha256="b" * 64, content_type="image/png", data=data, width=3, height=2)
        response = await client.get(f"/media/{shown['sha256']}")
        self.assertEqual((response.status_code, response.content), (200, data))
        self.assertEqual(response.headers["content-type"], "image/png")
        # An hour: a photo taken off the site must not live on in caches for a year.
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.site_repository.calls.clear()
        for name in ("b" * 64, "c" * 64):
            self.assertEqual((await client.get(f"/media/{name}")).status_code, 404, name)
        for name in ("A" * 64, "a" * 63, "a" * 65, "a" * 63 + "g", "../" + "a" * 64):
            response = await client.get(f"/media/{name}")
            self.assertEqual(response.status_code, 404, name)
            self.assertEqual(response.headers["cache-control"], "no-store")
        # Nothing but a published hash reaches the database: not an unpublished image,
        # not a guess, however many are tried.
        for index in range(40):
            await client.get(f"/media/{index:064x}")
        self.assertEqual(self.site_repository.calls, [])

    async def test_unknown_pages_are_plain_404s(self):
        client = self.serve(repository=None)
        for path in ("/no-such-page", "/en/", "/ka", "/index.html", "/.env", "/login.html"):
            response = await client.get(path, follow_redirects=False)
            self.assertIn(response.status_code, (404, 307), path)
            self.assertEqual(response.headers["cache-control"], "no-store")


class FakeTime:
    """Stands in for the ``time`` module inside site_pages, so the 1 s build cache can pass."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = FakeTime()
        self.enterContext(patch.object(site_pages, "time", self.clock))
        self.big = "export const answer = 42;\n" * 60
        for name, text in {
                "site/js/app.js": self.big, "site/css/app.css": "body { margin: 0 }",
                "site/img/mark.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
                "site/dev/harness.js": "private-dev-fixture", "site/notes.md": "private",
                "site/js/app.js.map": "private", "admin/js/admin.js": "export {};",
                "secret.txt": "private-outside-fixture"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "site/img/photo.webp").write_bytes(b"RIFF" + bytes(2000))
        self.assets = Assets({"site": self.root / "site", "admin": self.root / "admin"})

    def rebuilt(self) -> str:
        self.clock.now += 1.5  # past the one-second reuse of the last build id
        return self.assets.build()

    def test_files_are_read_with_their_type_and_compressed_when_it_pays(self):
        raw, packed, content_type = self.assets.read("site/js/app.js")
        self.assertEqual((raw, content_type), (self.big.encode(),
                                               "text/javascript; charset=utf-8"))
        self.assertEqual(gzip.decompress(packed), raw)
        self.assertLess(len(packed), len(raw))
        self.assertEqual(self.assets.read("site/css/app.css"),
                         (b"body { margin: 0 }", None, "text/css; charset=utf-8"))  # too small
        raw, packed, content_type = self.assets.read("site/img/photo.webp")
        self.assertEqual((len(raw), packed, content_type), (2004, None, "image/webp"))
        self.assertEqual(self.assets.read("site/img/mark.svg")[2], "image/svg+xml")
        self.assertEqual(self.assets.read("admin/js/admin.js")[0], b"export {};")
        self.assertTrue(self.assets.exists("site/js/app.js"))

    def test_an_edited_file_is_served_fresh(self):
        path = self.root / "site/css/app.css"
        self.assertEqual(self.assets.read("site/css/app.css")[0], b"body { margin: 0 }")
        path.write_text("body { margin: 1px }", encoding="utf-8")
        stamp = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(stamp, stamp))
        self.assertEqual(self.assets.read("site/css/app.css")[0], b"body { margin: 1px }")

    def test_traversal_unknown_types_and_the_dev_tree_are_refused(self):
        outside = str(self.root / "secret.txt")
        refused = [
            "site/../secret.txt", "site/js/../../secret.txt", "../secret.txt", "secret.txt",
            "site/%2e%2e/secret.txt", "site/..%2fsecret.txt", outside, "/site/js/app.js",
            "site//js/app.js", "site/js/app.js/", "site\\js\\app.js", "site/js/app.js\x00",
            "site/js/app.js?x=1", "site/js/ app.js",
            "site/dev/harness.js", "site/dev/../js/app.js",
            # A second spelling of a served file is refused too: one file, one address.
            "site/./js/app.js", "site/js/./app.js", "./site/js/app.js", "site/./dev/harness.js",
            "site/js/../js/app.js", "SITE/js/app.js", "site/JS/app.js",
            "site/notes.md", "site/js/app.js.map", "site/js", "site", "site/", "",
            "other/js/app.js", "site/missing.js",
        ]
        for path in refused:
            with self.subTest(path=path):
                self.assertIsNone(self.assets.read(path))
                self.assertFalse(self.assets.exists(path))

    def test_build_records_and_unpublished_packaged_photos_are_not_served(self):
        gallery = self.root / "site/img/gallery"
        gallery.mkdir(parents=True)
        for name in ("shop.webp", "rival-logo.webp", "unlisted.webp"):
            (gallery / name).write_bytes(b"RIFF" + bytes(64))
        (gallery / "manifest.json").write_text(json.dumps([
            {"file": "shop.webp", "published": True},
            {"file": "rival-logo.webp", "published": False}]), encoding="utf-8")
        (self.root / "site/img/ASSETS.json").write_text("{}", encoding="utf-8")
        (self.root / "site/img/pins.json").write_text("[]", encoding="utf-8")
        (self.root / "site/vendor").mkdir()
        (self.root / "site/vendor/VENDOR.json").write_text("{}", encoding="utf-8")
        (self.root / "site/vendor/LICENSE.txt").write_text("licence", encoding="utf-8")
        first = self.rebuilt()
        served = {"site/img/gallery/shop.webp", "site/img/pins.json",
                  "site/vendor/LICENSE.txt"}
        hidden = {"site/img/gallery/rival-logo.webp", "site/img/gallery/unlisted.webp",
                  "site/img/gallery/manifest.json", "site/img/ASSETS.json",
                  "site/vendor/VENDOR.json"}
        for path in served:
            self.assertIsNotNone(self.assets.read(path), path)
        for path in hidden:
            self.assertIsNone(self.assets.read(path), path)
        # What is never served does not move the build id either.
        (gallery / "rival-logo.webp").write_bytes(b"RIFF" + bytes(99))
        self.assertEqual(self.rebuilt(), first)
        # Without a readable manifest no packaged photo is shown at all.
        (gallery / "manifest.json").write_text("not json", encoding="utf-8")
        self.rebuilt()
        self.assertIsNone(self.assets.read("site/img/gallery/shop.webp"))

    def test_the_packaged_tree_serves_only_what_its_manifest_publishes(self):
        packaged = Assets()
        manifest = json.loads((site_pages.ASSET_ROOTS["site"] / "img/gallery/manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertTrue(manifest)
        for entry in manifest:
            path = f"site/img/gallery/{entry['file']}"
            self.assertEqual(packaged.exists(path), bool(entry.get("published")), path)
        for path in ("site/img/gallery/manifest.json", "site/img/ASSETS.json",
                     "site/fonts/FONTS.json", "site/vendor/phosphor/VENDOR.json"):
            self.assertFalse(packaged.exists(path), path)
        self.assertTrue(packaged.exists("site/img/engine/pins.json"))  # the engine loads it

    def test_other_spellings_cannot_grow_the_cache(self):
        self.assertIsNotNone(self.assets.read("site/js/app.js"))
        for index in range(300):
            dots = "./" * (index % 7 + 1)
            self.assertIsNone(self.assets.read(f"site/{dots}js/app.js"))
            self.assertIsNone(self.assets.read(f"site/js/{dots}app.js"))
        self.assertEqual(set(self.assets._cache), {"site/js/app.js"})

    def test_a_removed_file_stops_being_served_and_gives_its_bytes_back(self):
        self.assertIsNotNone(self.assets.read("site/css/app.css"))
        (self.root / "site/css/app.css").unlink()
        self.assertIsNone(self.assets.read("site/css/app.css"))  # at once, without an error
        self.rebuilt()
        self.assertNotIn("site/css/app.css", self.assets._cache)
        self.assertFalse(self.assets.exists("site/css/app.css"))

    def test_a_link_out_of_the_asset_root_is_not_followed(self):
        link = self.root / "site/js/leak.txt"
        try:
            link.symlink_to(self.root / "secret.txt")
        except OSError:
            self.skipTest("symbolic links are not available here")
        self.assertIsNone(self.assets.read("site/js/leak.txt"))

    def test_build_id_changes_when_any_served_file_changes(self):
        first = self.assets.build()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(self.rebuilt(), first)  # nothing changed
        self.assertEqual(self.assets.url("site/js/app.js"), f"/assets/{first}/site/js/app.js")
        path = self.root / "site/css/app.css"
        path.write_text("body { margin: 0; padding: 0 }", encoding="utf-8")
        second = self.rebuilt()
        self.assertNotEqual(second, first)
        # Same size, later modification time.
        path.write_text("body { margin: 9; padding: 9 }", encoding="utf-8")
        stamp = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(stamp, stamp))
        third = self.rebuilt()
        self.assertNotEqual(third, second)
        (self.root / "admin/js/extra.js").write_text("export {};", encoding="utf-8")
        fourth = self.rebuilt()
        self.assertNotEqual(fourth, third)
        (self.root / "admin/js/extra.js").unlink()
        self.assertEqual(self.rebuilt(), third)
        self.assertEqual(self.assets.url("site/js/app.js"), f"/assets/{third}/site/js/app.js")

    def test_files_that_are_never_served_do_not_move_the_build_id(self):
        first = self.assets.build()
        (self.root / "site/dev/harness.js").write_text("changed dev harness", encoding="utf-8")
        (self.root / "site/notes.md").write_text("changed notes", encoding="utf-8")
        (self.root / "secret.txt").write_text("changed outside file", encoding="utf-8")
        self.assertEqual(self.rebuilt(), first)

    def test_a_missing_root_is_an_empty_tree_not_an_error(self):
        assets = Assets({"site": self.root / "nowhere"})
        self.assertRegex(assets.build(), r"^[0-9a-f]{12}$")
        self.assertIsNone(assets.read("site/js/app.js"))


class Images:
    configured = True

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, bool]] = []
        self.error: Exception | None = None

    def add(self, name: str, size: int = 100) -> str:
        self.rows[name * 64] = {"content_type": "image/png", "data": memoryview(bytes(size))}
        return name * 64

    def media_bytes(self, sha256: str, published_only: bool = True) -> dict | None:
        self.calls.append((sha256, published_only))
        if self.error is not None:
            raise self.error
        return self.rows.get(sha256)


class MediaCacheTests(unittest.TestCase):
    def setUp(self):
        self.images = Images()
        self.first, self.second, self.third = (self.images.add(name) for name in "abc")

    def test_an_image_is_read_once_and_only_if_published(self):
        cache = MediaCache(self.images)
        self.assertEqual(cache.get(self.first), ("image/png", bytes(100)))
        self.assertIsInstance(cache.get(self.first)[1], bytes)
        self.assertEqual(self.images.calls, [(self.first, True)])

    def test_missing_images_errors_and_a_missing_database_are_just_absent(self):
        clock = Clock()
        cache = MediaCache(self.images, clock=clock)
        self.assertIsNone(cache.get("f" * 64))
        self.images.error = RuntimeError("connection lost")
        self.images.calls.clear()
        for _ in range(20):
            self.assertIsNone(cache.get(self.first))
        # During an outage the database is asked once, not once per request...
        self.assertEqual(len(self.images.calls), 1)
        self.images.error = None
        self.assertIsNone(cache.get(self.first))
        # ...and a few seconds later the image is back.
        clock.advance(MediaCache.ERROR_SECONDS + 0.1)
        self.assertIsNotNone(cache.get(self.first))
        self.assertLess(MediaCache.ERROR_SECONDS, MediaCache.MISS_SECONDS)
        self.images.calls.clear()
        self.images.configured = False
        self.assertIsNone(MediaCache(self.images).get(self.first))
        self.assertIsNone(MediaCache(None).get(self.first))
        self.assertEqual(self.images.calls, [])

    def test_the_byte_budget_evicts_the_least_recently_used_image(self):
        cache = MediaCache(self.images, budget=250)
        cache.get(self.first)
        cache.get(self.second)
        cache.get(self.first)  # the first is now the more recently used of the two
        cache.get(self.third)  # 300 bytes: the second has to go
        self.images.calls.clear()
        cache.get(self.first)
        cache.get(self.third)
        self.assertEqual(self.images.calls, [])
        cache.get(self.second)
        self.assertEqual(self.images.calls, [(self.second, True)])

    def test_an_image_larger_than_the_budget_is_served_but_not_kept(self):
        cache = MediaCache(self.images, budget=50)
        self.assertEqual(len(cache.get(self.first)[1]), 100)
        self.assertEqual(len(cache.get(self.first)[1]), 100)
        self.assertEqual(len(self.images.calls), 2)

    def test_a_miss_is_remembered_briefly_so_guessed_hashes_do_not_query_each_time(self):
        clock = [0.0]
        cache = MediaCache(self.images, clock=lambda: clock[0])
        self.assertIsNone(cache.get("f" * 64))
        self.assertIsNone(cache.get("f" * 64))
        self.assertEqual(self.images.calls, [("f" * 64, True)])
        clock[0] += MediaCache.MISS_SECONDS + 1
        self.assertIsNone(cache.get("f" * 64))
        self.assertEqual(len(self.images.calls), 2)

    def test_clear_forgets_everything(self):
        cache = MediaCache(self.images)
        cache.get(self.first)
        del self.images.rows[self.first]  # unpublished or deleted in the dashboard
        self.assertIsNotNone(cache.get(self.first))
        cache.clear()
        self.assertIsNone(cache.get(self.first))


if __name__ == "__main__":
    unittest.main()
