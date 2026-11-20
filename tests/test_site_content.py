"""Content schema, validation, localisation and the snapshot cache (no database)."""

from __future__ import annotations

import copy
import json
import re
import threading
import unittest
from decimal import Decimal

from jinja2 import Environment, StrictUndefined, Undefined

from automotive.site_content import (
    DEFAULT_SECTIONS,
    DEFAULT_SERVICES,
    SECTIONS,
    SERVICE_ICONS,
    SiteContent,
    clean,
    clean_service,
    default_snapshot,
    describe_schema,
    localized,
    missing_setup,
    pick,
)

CODES = {"INVALID", "TOO_LONG", "TOO_MANY_WORDS", "TOO_MANY", "REQUIRED", "UNKNOWN_SECTION"}
PATH = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
MARKER = "private-marker-9f3c"


def pair(ka: str, en: str) -> dict[str, str]:
    return {"ka": ka, "en": en}


def brand(**changes) -> dict:
    return {**copy.deepcopy(DEFAULT_SECTIONS["brand"]), **changes}


class CleanTests(unittest.TestCase):
    def test_packaged_defaults_satisfy_their_own_schema(self):
        self.assertEqual(set(DEFAULT_SECTIONS), set(SECTIONS))
        for key, document in DEFAULT_SECTIONS.items():
            with self.subTest(section=key):
                self.assertEqual(clean(key, copy.deepcopy(document)), (document, {}))
        for service in DEFAULT_SERVICES:
            fields, errors = clean_service(service)
            self.assertEqual(errors, {}, service["title_en"])
            self.assertEqual({name: fields[name] for name in service}, service)

    def test_unknown_sections_and_non_object_documents_are_refused(self):
        self.assertEqual(clean("no_such_section", {"title": pair("ა", "a")}),
                         ({}, {"": "UNKNOWN_SECTION"}))
        for document in (None, [], "text", 7, True):
            self.assertEqual(clean("hero", document), ({}, {"": "INVALID"}))

    def test_only_schema_fields_and_both_languages_survive(self):
        document = {"title": {"ka": "სათაური", "en": "Title", "fr": MARKER},
                    "subline": pair("ქვესათაური", "Subline"), "injected": MARKER}
        cleaned, errors = clean("hero", document)
        self.assertEqual(errors, {})
        self.assertEqual(cleaned, {"title": pair("სათაური", "Title"),
                                   "subline": pair("ქვესათაური", "Subline")})
        cleaned, errors = clean("hero", {})  # every field is always present afterwards
        self.assertEqual(cleaned, {"title": pair("", ""), "subline": pair("", "")})
        self.assertEqual(errors, {"title": "INVALID", "subline": "INVALID"})

    def test_text_longer_than_the_field_allows_is_refused(self):
        at_limit, errors = clean("hero", {"title": pair("ა" * 40, "a" * 40),
                                         "subline": pair("", "")})
        self.assertEqual(errors, {})
        self.assertEqual(len(at_limit["title"]["ka"]), 40)
        _, errors = clean("hero", {"title": pair("ა" * 41, "a" * 40), "subline": pair("", "")})
        self.assertEqual(errors, {"title.ka": "TOO_LONG"})
        _, errors = clean("brand", brand(currency="GEL  x"))
        self.assertEqual(errors, {"currency": "TOO_LONG"})

    def test_hero_subline_allows_twenty_words_per_language(self):
        twenty, twenty_one = " ".join(["word"] * 20), " ".join(["sit"] * 21)
        _, errors = clean("hero", {"title": pair("ა", "a"), "subline": pair(twenty, twenty)})
        self.assertEqual(errors, {})
        _, errors = clean("hero", {"title": pair("ა", "a"),
                                   "subline": pair(twenty, twenty_one)})
        self.assertEqual(errors, {"subline.en": "TOO_MANY_WORDS"})
        _, errors = clean("hero", {"title": pair("ა", "a"),
                                   "subline": pair(twenty_one, twenty_one)})
        self.assertEqual(errors, {"subline.ka": "TOO_MANY_WORDS",
                                  "subline.en": "TOO_MANY_WORDS"})

    def test_contact_fields_accept_real_values_and_stay_optional(self):
        accepted = brand(
            phone="+995 (32) 2 12-34-56", whatsapp="995599123456",
            email="Info@Garage1886.example", map_url="https://maps.app.goo.gl/AbC123?x=1#pin",
            facebook="https://www.facebook.com/garage1886",
            instagram="https://instagram.com:443/garage1886/", tiktok="https://tiktok.com")
        cleaned, errors = clean("brand", accepted)
        self.assertEqual(errors, {})
        self.assertEqual(cleaned, accepted)
        self.assertEqual(clean("brand", brand())[1], {})  # all of them may be empty

    def test_phone_email_and_link_patterns_refuse_everything_else(self):
        refused = {
            "phone": ["call me", "12345", "++995322123456", "+995-32-ABC", "(995) 32 212",
                      "+995 32 212;34"],
            "email": ["nope", "a@b", "two@@example.ge", "a b@example.ge", "x@exa mple.ge",
                      "<x>@example.ge"],
            "map_url": ["http://maps.example.ge/x", "javascript:alert(1)", "//maps.example.ge",
                        "https://", "https://user@evil.example/", "https://evil.example:8443/",
                        "https://maps.example.ge/a b", "https://maps.example.ge/<script>",
                        "https://maps.example.ge/\"onmouseover=", "data:text/html,x",
                        "ftp://maps.example.ge/"],
        }
        for field, values in refused.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(clean("brand", brand(**{field: value}))[1],
                                     {field: "INVALID"})
        for field in ("facebook", "instagram", "tiktok", "whatsapp"):
            value = "http://x.example" if field != "whatsapp" else "not a number"
            self.assertEqual(clean("brand", brand(**{field: value}))[1], {field: "INVALID"})

    def test_lists_are_bounded_and_report_the_row_that_is_wrong(self):
        row = {"days": pair("ორშ-პარ", "Mon-Fri"), "time": "10:00-19:00"}
        cleaned, errors = clean("brand", brand(hours=[row] * 7))
        self.assertEqual((errors, len(cleaned["hours"])), ({}, 7))
        cleaned, errors = clean("brand", brand(hours=[row] * 8))
        self.assertEqual(errors, {"hours": "TOO_MANY"})
        self.assertEqual(len(cleaned["hours"]), 7)  # never more than the page can show
        _, errors = clean("checklist", {"items": [{"label": pair("ა", "a")}] * 5})
        self.assertEqual(errors, {"items": "TOO_MANY"})
        for not_a_list in (None, "text", {"0": row}, 3):
            self.assertEqual(clean("brand", brand(hours=not_a_list))[1], {"hours": "INVALID"})
        cleaned, errors = clean("brand", brand(hours=[
            row, "not a row", {"days": pair("შაბ", "S" * 41), "time": 9}, {"time": "x"}]))
        self.assertEqual(errors, {"hours.1": "INVALID", "hours.2.days.en": "TOO_LONG",
                                  "hours.2.time": "INVALID", "hours.3.days": "INVALID"})
        self.assertEqual(cleaned["hours"][0], {**row, "days_code": ""})  # optional field

    def test_flags_must_be_real_booleans(self):
        chat = copy.deepcopy(DEFAULT_SECTIONS["chat"])
        for value in (False, True):
            cleaned, errors = clean("chat", {**chat, "enabled": value})
            self.assertEqual((errors, cleaned["enabled"]), ({}, value))
        for value in ("true", 1, 0, None, "", []):
            cleaned, errors = clean("chat", {**chat, "enabled": value})
            self.assertEqual(errors, {"enabled": "INVALID"})
            self.assertIs(cleaned["enabled"], False)  # a doubtful value never switches on

    def test_wrong_types_are_refused_not_coerced(self):
        _, errors = clean("hero", {"title": "plain text", "subline": pair(7, ["x"])})
        self.assertEqual(errors, {"title": "INVALID", "subline.ka": "INVALID",
                                  "subline.en": "INVALID"})
        _, errors = clean("brand", brand(phone=995322123456, address=["x"]))
        self.assertEqual(errors, {"phone": "INVALID", "address": "INVALID"})

    def test_errors_are_codes_keyed_by_field_path_and_never_echo_the_input(self):
        hostile = f"<script>{MARKER}</script>"
        documents = {
            "brand": brand(phone=hostile, email=hostile, map_url=hostile, currency=hostile,
                           address=hostile, hours=[{"days": pair(hostile * 3, "x"),
                                                    "time": hostile * 2}, hostile]),
            "hero": {"title": pair(hostile * 3, hostile * 3), "subline": pair(hostile * 9, "")},
            "chat": {"enabled": hostile, "launcher": hostile, "greeting": pair("x" * 201, "")},
            hostile: {"title": pair("a", "b")},
        }
        for key, document in documents.items():
            _, errors = clean(key, document)
            self.assertTrue(errors, key)
            self.assertNotIn(MARKER, json.dumps(errors, ensure_ascii=False))
            for path, code in errors.items():
                self.assertIn(code, CODES)
                self.assertTrue(path == "" or PATH.fullmatch(path), path)

    def test_control_characters_and_stray_whitespace_are_removed(self):
        cleaned, errors = clean("hero", {
            "title": pair("  Gar\x00age\x07  18\x1f86\x7f ", "tab\tand\nnewline"),
            "subline": pair("line separator gone", "\x00\x01\x02")})
        self.assertEqual(errors, {})
        self.assertEqual(cleaned, {"title": pair("Garage 1886", "tab and newline"),
                                   "subline": pair("lineseparatorgone", "")})
        # Stripping happens before the limit, so hidden characters cannot pad a value.
        _, errors = clean("hero", {"title": pair("\x00" * 60 + "a" * 40, "a"),
                                   "subline": pair("", "")})
        self.assertEqual(errors, {})

    def test_multiline_fields_keep_their_line_breaks(self):
        story = copy.deepcopy(DEFAULT_SECTIONS["story"])
        story["statement"] = pair("  first   line \n\n second\tline  \n", "one\r\ntwo")
        cleaned, errors = clean("story", story)
        self.assertEqual(errors, {})
        self.assertEqual(cleaned["statement"], pair("first line\n\nsecond line", "one\ntwo"))
        story["title"] = pair("no\nbreaks here", "x")
        self.assertEqual(clean("story", story)[0]["title"]["ka"], "no breaks here")


class CleanServiceTests(unittest.TestCase):
    VALID = {"icon": "gauge", "title_ka": "დიაგნოსტიკა", "title_en": "Diagnostics",
             "summary_ka": "აღწერა", "summary_en": "Summary", "price_from": 45,
             "is_published": False}

    def test_a_complete_service_is_normalised(self):
        fields, errors = clean_service({**self.VALID, "title_en": "  Diag\x00nostics  ",
                                        "service_id": 99, "position": 1, "version": 7})
        self.assertEqual(errors, {})
        self.assertEqual(fields, {**self.VALID, "price_from": 45.0})
        self.assertIsInstance(fields["price_from"], float)

    def test_both_titles_are_required_and_defaults_fill_the_rest(self):
        fields, errors = clean_service({"title_ka": "სათაური", "title_en": "Title"})
        self.assertEqual(errors, {})
        self.assertEqual(fields, {"title_ka": "სათაური", "title_en": "Title", "summary_ka": "",
                                  "summary_en": "", "icon": "wrench", "price_from": None,
                                  "is_published": True})
        for missing in ("title_ka", "title_en"):
            for blank in ("", "   ", "\x00", None):
                _, errors = clean_service({**self.VALID, missing: blank})
                self.assertEqual(errors, {missing: "REQUIRED"})
            _, errors = clean_service({key: value for key, value in self.VALID.items()
                                       if key != missing})
            self.assertEqual(errors, {missing: "REQUIRED"})
        self.assertEqual(clean_service({})[1], {"title_ka": "REQUIRED", "title_en": "REQUIRED"})

    def test_text_limits_and_types(self):
        _, errors = clean_service({**self.VALID, "title_ka": "ა" * 60, "summary_en": "s" * 200})
        self.assertEqual(errors, {})
        _, errors = clean_service({**self.VALID, "title_ka": "ა" * 61, "summary_en": "s" * 201,
                                   "title_en": ["Title"]})
        self.assertEqual(errors, {"title_ka": "TOO_LONG", "summary_en": "TOO_LONG",
                                  "title_en": "INVALID"})

    def test_icon_must_be_one_of_the_packaged_ones(self):
        for icon in SERVICE_ICONS:
            self.assertEqual(clean_service({**self.VALID, "icon": icon})[1], {})
        for icon in ("", "no-such-icon", "../wrench", "<svg>", None, 7, ["wrench"]):
            fields, errors = clean_service({**self.VALID, "icon": icon})
            self.assertEqual((errors, fields["icon"]), ({"icon": "INVALID"}, "wrench"))

    def test_price_is_an_optional_non_negative_amount(self):
        for price, stored in ((None, None), ("", None), (0, 0.0), (45, 45.0),
                              (12.345, 12.35), (9_999_999.99, 9_999_999.99)):
            fields, errors = clean_service({**self.VALID, "price_from": price})
            self.assertEqual((errors, fields["price_from"]), ({}, stored), price)
        for price in (-1, 1e7, "45", True, False, [45], {"amount": 45}, float("nan"),
                      float("inf")):
            fields, errors = clean_service({**self.VALID, "price_from": price})
            self.assertEqual((errors, fields["price_from"]), ({"price_from": "INVALID"}, None),
                             price)

    def test_published_must_be_a_boolean_and_other_payloads_are_refused(self):
        for value in ("yes", 1, None):
            fields, errors = clean_service({**self.VALID, "is_published": value})
            self.assertEqual((errors, fields["is_published"]),
                             ({"is_published": "INVALID"}, False))
        for payload in (None, [], "text", 3):
            self.assertEqual(clean_service(payload), ({}, {"": "INVALID"}))

    def test_errors_never_echo_the_submitted_text(self):
        hostile = f"<script>{MARKER}</script>" * 9
        _, errors = clean_service({"title_ka": hostile, "title_en": [hostile], "icon": hostile,
                                   "summary_ka": hostile, "price_from": hostile,
                                   "is_published": hostile})
        self.assertEqual(set(errors), {"title_ka", "title_en", "icon", "summary_ka",
                                       "price_from", "is_published"})
        self.assertNotIn(MARKER, json.dumps(errors))
        self.assertLessEqual(set(errors.values()), CODES)


class SchemaTests(unittest.TestCase):
    def test_schema_describes_every_section_for_the_dashboard(self):
        schema = describe_schema()
        json.dumps(schema)  # must be plain data
        self.assertEqual([section["key"] for section in schema["sections"]], list(SECTIONS))
        self.assertEqual(schema["icons"], list(SERVICE_ICONS))

        def walk(fields):
            for field in fields:
                self.assertIn(field["kind"], {"text", "i18n", "flag", "list"})
                self.assertEqual(set(field["label"]), {"ka", "en"})
                self.assertTrue(all(field["label"].values()), field["name"])
                if field["kind"] == "list":
                    self.assertGreater(field["items"], 0)
                    self.assertTrue(field["fields"])
                    walk(field["fields"])
                else:
                    self.assertEqual(field["fields"], [])

        for section in schema["sections"]:
            self.assertEqual(set(section["label"]), {"ka", "en"})
            walk(section["fields"])
        hero = next(section for section in schema["sections"] if section["key"] == "hero")
        subline = next(field for field in hero["fields"] if field["name"] == "subline")
        self.assertEqual((subline["kind"], subline["words"], subline["max"]), ("i18n", 20, 160))

    def test_the_limits_shown_to_the_dashboard_are_the_ones_enforced(self):
        schema = describe_schema()
        for section in schema["sections"]:
            document = copy.deepcopy(DEFAULT_SECTIONS[section["key"]])
            for field in section["fields"]:
                if field["kind"] != "i18n":
                    continue
                document[field["name"]] = pair("x" * (field["max"] + 1), "")
                _, errors = clean(section["key"], document)
                self.assertEqual(errors.get(field["name"] + ".ka"), "TOO_LONG",
                                 (section["key"], field["name"]))
                document[field["name"]] = pair("", "")
        for name, limit in schema["service_limits"].items():
            _, errors = clean_service({"title_ka": "a", "title_en": "b", name: "x" * (limit + 1)})
            self.assertEqual(errors, {name: "TOO_LONG"})


class LocalizedTests(unittest.TestCase):
    def snapshot(self) -> dict:
        snapshot = default_snapshot()
        sections = snapshot["sections"]
        sections["hero"]["title"] = pair("მხოლოდ ქართულად", "")
        sections["hero"]["subline"] = pair("", "English only")
        sections["brand"]["hours"] = [{"days": pair("ორშ-პარ", "Mon-Fri"),
                                       "time": "10:00-19:00"}]
        sections["brand"]["phone"] = "+995 32 212 34 56"
        snapshot["services"] = [
            {"service_id": 4, "icon": "disc", "title_ka": "მუხრუჭები", "title_en": "Brakes",
             "summary_ka": "", "summary_en": "Pads and discs", "price_from": Decimal("45.50"),
             "position": 1, "is_published": True, "version": 3},
            {"service_id": 5, "icon": "drop", "title_ka": "დამალული", "title_en": "Hidden",
             "summary_ka": "", "summary_en": "", "price_from": None, "position": 2,
             "is_published": False, "version": 1},
            {"service_id": 6, "icon": "gauge", "title_ka": "დიაგნოსტიკა",
             "title_en": "Diagnostics", "summary_ka": "ქართული აღწერა", "summary_en": "Checks",
             "price_from": None, "position": 3, "is_published": True, "version": 1},
        ]
        snapshot["media"] = [
            {"media_id": 1, "sha256": "a" * 64, "content_type": "image/webp", "width": 960,
             "height": 1280, "alt_ka": "ფარეხი", "alt_en": "The shop", "position": 1,
             "is_published": True, "byte_size": 1200},
            {"media_id": 2, "sha256": "b" * 64, "content_type": "image/png", "width": 10,
             "height": 10, "alt_ka": "", "alt_en": "English alt", "position": 2,
             "is_published": True, "byte_size": 80},
            {"media_id": 3, "sha256": "c" * 64, "content_type": "image/png", "width": 10,
             "height": 10, "alt_ka": "დამალული", "alt_en": "Hidden", "position": 3,
             "is_published": False, "byte_size": 80},
        ]
        return snapshot

    def test_pick_falls_back_to_the_other_language(self):
        self.assertEqual(pick(pair("ქართული", "English"), "ka"), "ქართული")
        self.assertEqual(pick(pair("ქართული", "English"), "en"), "English")
        self.assertEqual(pick(pair("ქართული", ""), "en"), "ქართული")
        self.assertEqual(pick(pair("", "English"), "ka"), "English")
        self.assertEqual(pick(pair("", ""), "ka"), "")
        self.assertEqual(pick(None, "ka"), "")
        self.assertEqual(pick("plain", "en"), "plain")

    def test_sections_become_single_language_text_with_fallback(self):
        snapshot = self.snapshot()
        ka, en = localized(snapshot, "ka"), localized(snapshot, "en")
        self.assertEqual(ka["hero"], {"title": "მხოლოდ ქართულად", "subline": "English only"})
        self.assertEqual(en["hero"], {"title": "მხოლოდ ქართულად", "subline": "English only"})
        self.assertEqual(ka["parts"]["title"], DEFAULT_SECTIONS["parts"]["title"]["ka"])
        self.assertEqual(en["parts"]["title"], DEFAULT_SECTIONS["parts"]["title"]["en"])
        self.assertEqual(ka["brand"]["hours"], [{"days": "ორშ-პარ", "time": "10:00-19:00"}])
        self.assertEqual(en["brand"]["hours"], [{"days": "Mon-Fri", "time": "10:00-19:00"}])
        self.assertEqual(en["brand"]["phone"], "+995 32 212 34 56")
        self.assertIs(en["chat"]["enabled"], True)
        self.assertEqual(en["checklist"]["items"][0], {"label": "Build"})
        # The stored snapshot itself is not rewritten.
        self.assertEqual(snapshot["sections"]["hero"]["title"], pair("მხოლოდ ქართულად", ""))

    def test_a_template_can_read_every_editable_field_as_section_dot_field(self):
        # Read the way Jinja resolves `c.gallery.title`: attribute first, then item. The
        # photo list once shared the key "gallery" and replaced that section outright.
        environment = Environment(undefined=StrictUndefined)
        snapshot = self.snapshot()
        snapshot["sections"]["gallery"]["title"] = pair("გალერეის სათაური", "Gallery heading")
        for language, heading in (("ka", "გალერეის სათაური"), ("en", "Gallery heading")):
            view = localized(snapshot, language)
            for key, section in SECTIONS.items():
                for field in section.fields:
                    value = environment.getattr(view[key], field.name)
                    self.assertFalse(isinstance(value, Undefined), f"c.{key}.{field.name}")
            self.assertEqual(environment.getattr(view["gallery"], "title"), heading)
            # The published photos have their own key.
            self.assertEqual([photo["sha256"] for photo in view["photos"]],
                             ["a" * 64, "b" * 64])
        empty = localized(default_snapshot(), "en")
        self.assertEqual(empty["photos"], [])
        self.assertEqual(environment.getattr(empty["gallery"], "title"), "Inside the shop")

    def test_only_published_services_are_shown_with_four_fields(self):
        ka, en = localized(self.snapshot(), "ka"), localized(self.snapshot(), "en")
        self.assertEqual(en["services"], [
            {"icon": "disc", "title": "Brakes", "summary": "Pads and discs",
             "price_from": 45.5},
            {"icon": "gauge", "title": "Diagnostics", "summary": "Checks", "price_from": None}])
        self.assertIsInstance(en["services"][0]["price_from"], float)
        # A missing Georgian summary falls back to the English one, never to a blank.
        self.assertEqual(ka["services"], [
            {"icon": "disc", "title": "მუხრუჭები", "summary": "Pads and discs",
             "price_from": 45.5},
            {"icon": "gauge", "title": "დიაგნოსტიკა", "summary": "ქართული აღწერა",
             "price_from": None}])

    def test_only_published_photos_are_shown_with_a_localised_alt(self):
        ka, en = localized(self.snapshot(), "ka"), localized(self.snapshot(), "en")
        self.assertEqual(en["photos"], [
            {"sha256": "a" * 64, "width": 960, "height": 1280, "alt": "The shop"},
            {"sha256": "b" * 64, "width": 10, "height": 10, "alt": "English alt"}])
        self.assertEqual([photo["alt"] for photo in ka["photos"]], ["ფარეხი", "English alt"])

    def test_missing_setup_lists_what_only_the_owner_can_supply(self):
        snapshot = default_snapshot()
        self.assertEqual(missing_setup(snapshot), ["phone", "map_url", "address", "hours",
                                                   "milestones", "service_prices"])
        brand_section, story = snapshot["sections"]["brand"], snapshot["sections"]["story"]
        brand_section.update(phone="+995 32 212 34 56", address=pair("", "12 Example St"))
        self.assertEqual(missing_setup(snapshot), ["map_url", "hours", "milestones",
                                                   "service_prices"])
        brand_section.update(map_url="https://maps.example.ge/x", hours=[
            {"days": pair("ორშ-პარ", "Mon-Fri"), "time": "10-19"}])
        milestone = {"year": "1886", "title": pair("ა", "a"), "text": pair("", "")}
        story["milestones"] = [milestone, milestone]
        self.assertEqual(missing_setup(snapshot), ["milestones", "service_prices"])
        story["milestones"].append(milestone)
        snapshot["services"][0]["price_from"] = Decimal("0")  # "from 0" is still a price
        self.assertEqual(missing_setup(snapshot), [])


class Repository:
    configured = True

    def __init__(self) -> None:
        self.loads = 0
        self.error: Exception | None = None
        self.content: dict = {"sections": {}, "services": [], "media": []}

    def load_content(self) -> dict:
        self.loads += 1
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.content)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SiteContentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(5000.0)
        self.repository = Repository()
        self.repository.content["sections"]["hero"] = {
            "section_key": "hero", "version": 3,
            "document": {"title": pair("შენახული", "Stored"), "subline": pair("ქვე", "Sub")}}
        self.repository.content["services"] = [{"service_id": 9, "title_en": "Stored service"}]
        self.repository.content["media"] = [{"media_id": 2, "sha256": "a" * 64}]
        self.content = SiteContent(self.repository, clock=self.clock)

    def test_defaults_are_served_without_a_repository(self):
        for repository in (None, object()):
            snapshot = SiteContent(repository).snapshot()
            self.assertEqual(snapshot, default_snapshot())
            self.assertEqual(snapshot["source"], "defaults")
        self.repository.configured = False
        self.assertEqual(self.content.snapshot()["source"], "defaults")
        self.assertEqual(self.repository.loads, 0)

    def test_default_snapshot_is_complete_and_private_to_its_holder(self):
        snapshot = SiteContent(None).snapshot()
        self.assertEqual(set(snapshot["sections"]), set(SECTIONS))
        self.assertEqual(snapshot["versions"], dict.fromkeys(SECTIONS, 0))
        self.assertEqual([row["title_en"] for row in snapshot["services"]],
                         [row["title_en"] for row in DEFAULT_SERVICES])
        self.assertTrue(all(row["is_published"] for row in snapshot["services"]))
        self.assertEqual(snapshot["media"], [])
        snapshot["sections"]["hero"]["title"]["en"] = "changed by a caller"
        self.assertEqual(SiteContent(None).snapshot()["sections"]["hero"]["title"]["en"],
                         "More than a garage.")

    def test_stored_sections_replace_defaults_and_carry_their_version(self):
        snapshot = self.content.snapshot()
        self.assertEqual(snapshot["source"], "database")
        self.assertEqual(snapshot["sections"]["hero"],
                         {"title": pair("შენახული", "Stored"), "subline": pair("ქვე", "Sub")})
        self.assertEqual(snapshot["versions"]["hero"], 3)
        self.assertEqual(snapshot["sections"]["parts"], DEFAULT_SECTIONS["parts"])
        self.assertEqual(snapshot["versions"]["parts"], 0)  # not stored yet
        self.assertEqual(snapshot["services"], self.repository.content["services"])
        self.assertEqual(snapshot["media"], self.repository.content["media"])

    def test_stored_documents_are_normalised_on_the_way_out(self):
        self.repository.content["sections"]["hero"]["document"] = {
            "title": pair("  Sto\x00red  ", "ok"), "subline": pair("", ""),
            "legacy_field": MARKER}
        hero = self.content.snapshot()["sections"]["hero"]
        self.assertEqual(hero, {"title": pair("Stored", "ok"), "subline": pair("", "")})

    def test_a_field_the_stored_document_lacks_shows_its_packaged_text(self):
        # A row saved before a field existed: the new field appears with its default,
        # while a field the owner emptied on purpose (above) stays empty.
        self.repository.content["sections"]["hero"]["document"] = {"title": pair("ა", "a")}
        with self.assertLogs("automotive.site_content", "WARNING") as logs:
            hero = self.content.snapshot()["sections"]["hero"]
        self.assertEqual(hero, {"title": pair("ა", "a"),
                                "subline": DEFAULT_SECTIONS["hero"]["subline"]})
        self.assertEqual(len(logs.output), 1)
        # The same finding is not repeated on every reload.
        self.content.invalidate()
        with self.assertNoLogs("automotive.site_content", "WARNING"):
            self.content.snapshot()

    def test_stored_text_that_would_be_refused_on_save_is_not_shown(self):
        long = "x" * 500
        self.repository.content["sections"]["hero"]["document"] = {
            "title": pair(long, long), "subline": pair("Sub ka", long)}
        self.repository.content["sections"]["brand"] = {
            "section_key": "brand", "version": 2, "document": brand(
                phone="+995 32 212 34 56", email="not an address",
                hours=[{"days": pair("ორშ", "Mon"), "time": long},
                       {"days": pair(long, long), "time": long}])}
        with self.assertLogs("automotive.site_content", "WARNING") as logs:
            snapshot = self.content.snapshot()
        hero, contact = snapshot["sections"]["hero"], snapshot["sections"]["brand"]
        # Both languages of the headline were refused, so the packaged one stands in...
        self.assertEqual(hero["title"], DEFAULT_SECTIONS["hero"]["title"])
        # ...while one good language is enough for the page to show.
        self.assertEqual(hero["subline"], pair("Sub ka", ""))
        self.assertEqual((contact["phone"], contact["email"]), ("+995 32 212 34 56", ""))
        # In a list the bad cell is emptied and a row with nothing left is dropped.
        self.assertEqual(contact["hours"],
                         [{"days": pair("ორშ", "Mon"), "time": "", "days_code": ""}])
        self.assertNotIn("x" * 30, repr(snapshot["sections"]))
        self.assertEqual(snapshot["versions"]["hero"], 3)  # still there to be edited
        self.assertNotIn("x" * 30, " ".join(logs.output))

    def test_a_stored_document_of_the_wrong_shape_gives_way_to_the_default(self):
        for document in (["not", "an", "object"], "text", None, 7):
            with self.subTest(document=document):
                self.repository.content["sections"]["hero"]["document"] = document
                content = SiteContent(self.repository, clock=self.clock)
                with self.assertLogs("automotive.site_content", "WARNING"):
                    hero = content.snapshot()["sections"]["hero"]
                self.assertEqual(hero, DEFAULT_SECTIONS["hero"])

    def test_clean_blanks_refused_text_only_when_asked_to(self):
        document = {"title": pair("x" * 500, "Fine"), "subline": pair("", "")}
        kept, errors = clean("hero", document)
        self.assertEqual((kept["title"]["ka"], errors), ("x" * 500, {"title.ka": "TOO_LONG"}))
        blanked, errors = clean("hero", document, blank_invalid=True)
        self.assertEqual((blanked["title"], errors),
                         (pair("", "Fine"), {"title.ka": "TOO_LONG"}))

    def test_a_switch_that_cannot_be_read_stays_off(self):
        # The packaged default for the chat is on. A stored flag that is missing or is
        # not a real boolean must not be "repaired" into switching the public chat on.
        for document in ({"enabled": "false", "launcher": pair("ჩატი", "Chat"),
                          "greeting": pair("გამარჯობა", "Hello")},
                         {"launcher": pair("ჩატი", "Chat")}, {"enabled": None}, {"enabled": 1}):
            with self.subTest(document=document):
                self.repository.content["sections"]["chat"] = {
                    "section_key": "chat", "version": 1, "document": document}
                content = SiteContent(self.repository, clock=self.clock)
                with self.assertLogs("automotive.site_content", "WARNING"):
                    chat = content.snapshot()["sections"]["chat"]
                self.assertIs(chat["enabled"], False)
        self.assertIs(DEFAULT_SECTIONS["chat"]["enabled"], True)

    def test_an_out_of_date_copy_is_served_while_one_caller_reloads(self):
        self.content.snapshot()
        self.clock.now += 61
        entered, release = threading.Event(), threading.Event()
        load = self.repository.load_content

        def slow_load() -> dict:
            entered.set()
            release.wait(10)
            return load()

        self.repository.load_content = slow_load
        self.repository.content["sections"]["hero"]["document"]["title"] = pair("ახალი", "New")
        results: list[dict] = []
        loader = threading.Thread(target=lambda: results.append(self.content.snapshot()))
        loader.start()
        self.assertTrue(entered.wait(5))
        # Everyone else is answered at once from the copy they already had.
        for _ in range(5):
            self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"],
                             "Stored")
        release.set()
        loader.join(5)
        self.assertEqual(results[0]["sections"]["hero"]["title"]["en"], "New")
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "New")
        self.assertEqual(self.repository.loads, 2)

    def test_an_edit_made_during_a_reload_is_not_lost(self):
        self.content.snapshot()
        self.clock.now += 61
        load = self.repository.load_content

        def load_then_edit() -> dict:
            before = load()  # what the database held when this load started
            self.repository.content["sections"]["hero"]["document"]["title"] = pair("გვ", "Late")
            self.content.invalidate()  # the dashboard saved while the load was running
            return before

        self.repository.load_content = load_then_edit
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "Stored")
        self.repository.load_content = load
        # The copy just stored is already known to be out of date: the next reader waits
        # for a new one instead of being shown the old text for a minute.
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "Late")

    def test_after_an_edit_readers_wait_for_the_new_copy(self):
        self.content.snapshot()
        entered, release = threading.Event(), threading.Event()
        load = self.repository.load_content

        def slow_load() -> dict:
            entered.set()
            release.wait(10)
            return load()

        self.repository.content["sections"]["hero"]["document"]["title"] = pair("ახალი", "New")
        self.content.invalidate()
        self.repository.load_content = slow_load
        seen: list[str] = []
        readers = [threading.Thread(target=lambda: seen.append(
            self.content.snapshot()["sections"]["hero"]["title"]["en"])) for _ in range(4)]
        for reader in readers:
            reader.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(seen, [])  # nobody was handed the text from before the edit
        release.set()
        for reader in readers:
            reader.join(5)
        self.assertEqual(seen, ["New"] * 4)
        self.assertEqual(self.repository.loads, 2)  # and it was loaded once, not four times

    def test_snapshot_is_cached_for_the_ttl(self):
        first = self.content.snapshot()
        self.clock.now += 59.9
        self.assertIs(self.content.snapshot(), first)
        self.assertEqual(self.repository.loads, 1)
        self.clock.now += 0.2
        self.repository.content["sections"]["hero"]["version"] = 4
        self.assertEqual(self.content.snapshot()["versions"]["hero"], 4)
        self.assertEqual(self.repository.loads, 2)

    def test_invalidate_reloads_on_the_next_read(self):
        self.content.snapshot()
        self.repository.content["sections"]["hero"]["document"]["title"] = pair("ახალი", "New")
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "Stored")
        self.content.invalidate()
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "New")
        self.assertEqual(self.repository.loads, 2)

    def test_invalidate_works_whatever_the_clock_reads(self):
        # time.monotonic() counts from boot, so it can be smaller than the ttl.
        content = SiteContent(self.repository, clock=Clock(3.0))
        content.snapshot()
        self.repository.content["sections"]["hero"]["version"] = 8
        content.invalidate()
        self.assertEqual(content.snapshot()["versions"]["hero"], 8)

    def test_last_good_snapshot_is_served_while_the_repository_fails(self):
        good = self.content.snapshot()
        self.repository.error = RuntimeError("connection lost: password=private-fixture")
        self.content.invalidate()
        with self.assertLogs("automotive.site_content", "WARNING") as logs:
            self.assertEqual(self.content.snapshot(), good)
        self.assertNotIn("private-fixture", "\n".join(logs.output))
        self.assertEqual(self.content.snapshot()["sections"]["hero"]["title"]["en"], "Stored")
        self.assertEqual(self.repository.loads, 2)  # not retried on every page view
        self.repository.error = None
        self.repository.content["sections"]["hero"]["version"] = 5
        self.clock.now += 60.1
        self.assertEqual(self.content.snapshot()["versions"]["hero"], 5)

    def test_defaults_are_served_when_the_first_load_fails_and_until_it_recovers(self):
        self.repository.error = RuntimeError("database is starting up")
        with self.assertLogs("automotive.site_content", "WARNING"):
            snapshot = self.content.snapshot()
        self.assertEqual(snapshot["source"], "defaults")
        self.assertEqual(snapshot["sections"], DEFAULT_SECTIONS)
        self.repository.error = None
        self.clock.now += 60.1
        self.assertEqual(self.content.snapshot()["source"], "database")


if __name__ == "__main__":
    unittest.main()
