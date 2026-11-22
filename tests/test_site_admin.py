"""Dashboard API: the role split, content, services, gallery, accounts and the audit trail."""

from __future__ import annotations

import copy
import hashlib
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from automotive.site_auth import verify_password
from automotive.site_catalog import Catalog
from automotive.site_content import DEFAULT_SECTIONS, SECTIONS
from automotive.site_media import MAX_BYTES
from tests.test_site_catalog import ROWS
from tests.test_site_guard import NEW_PASSWORD, ORIGIN, PASSWORD, RecordingBuckets, SiteAppCase
from tests.test_site_media import GIF, SVG, jpeg, padded, png, webp_extended, webp_lossy
from tests.test_site_pages import Page

MARKER = "private-marker-9f3c"
SERVICE = {"icon": "gauge", "title_ka": "დიაგნოსტიკა", "title_en": "Diagnostics",
           "summary_ka": "აღწერა", "summary_en": "Summary", "price_from": 45,
           "is_published": True}
WRITES = {"save_section", "create_service", "update_service", "delete_service", "reorder",
          "add_media", "update_media", "delete_media", "create_user", "update_user",
          "set_password", "delete_user_sessions"}
ADMIN_READS = {"list_users", "audit_rows", "trade_daily", "best_sellers", "media_bytes"}


def hero(title_en: str = "New headline", **changes) -> dict:
    document = copy.deepcopy(DEFAULT_SECTIONS["hero"])
    document["title"]["en"] = title_en
    return {**document, **changes}


class AdminCase(SiteAppCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = self.serve(catalog=Catalog.from_rows(ROWS), buckets=RecordingBuckets())
        self.admin = await self.sign_in(self.client, "boss")
        self.employee = await self.sign_in(self.client, "worker")
        self.repo = self.site_repository
        self.repo.calls.clear()
        self.repo.audits.clear()

    async def call(self, method: str, path: str, body=None, *, who: dict | None = None,
                   **kwargs):
        headers = {**(who or self.admin), **kwargs.pop("headers", {})}
        if isinstance(body, bytes):
            kwargs["content"] = body
        elif body is not None:
            kwargs["json"] = body
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def page_text(self, path: str) -> str:
        """The visible text of a public page, whatever markup carries it."""
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return Page(response.text).text

    def assert_refused(self, response, status: int, code: str, fields: dict | None = None):
        expected = {"code": code, **({"fields": fields} if fields is not None else {})}
        self.assertEqual((response.status_code, response.json()),
                         (status, {"detail": expected}))


class RoleTests(AdminCase):
    def admin_only(self) -> list[tuple]:
        user_id = self.worker["user_id"]
        return [
            ("GET", "/api/admin/content", None),
            ("PUT", "/api/admin/content/hero", {"document": hero(), "version": 0}),
            ("GET", "/api/admin/services", None),
            ("POST", "/api/admin/services", SERVICE),
            ("PUT", "/api/admin/services/1", {**SERVICE, "version": 1}),
            ("DELETE", "/api/admin/services/1", None),
            ("POST", "/api/admin/services/order", {"ids": []}),
            ("GET", "/api/admin/media", None),
            ("GET", "/api/admin/media/file/" + "a" * 64, None),
            ("POST", "/api/admin/media", png()),
            ("PATCH", "/api/admin/media/1", {"is_published": True}),
            ("DELETE", "/api/admin/media/1", None),
            ("POST", "/api/admin/media/order", {"ids": []}),
            ("GET", "/api/admin/users", None),
            ("POST", "/api/admin/users", {"username": "newcomer", "role": "admin",
                                          "password": PASSWORD}),
            ("PATCH", f"/api/admin/users/{user_id}", {"role": "admin"}),
            ("POST", f"/api/admin/users/{user_id}/password", {"password": NEW_PASSWORD}),
            ("GET", "/api/admin/audit", None),
            ("GET", "/api/admin/reports/sales", None),
            ("GET", "/api/admin/reports/chat", None),
            # Last: for an admin this one ends the admin's own session.
            ("POST", f"/api/admin/users/{self.boss['user_id']}/sessions/revoke", None),
        ]

    async def test_an_employee_is_refused_everything_that_edits_or_shows_money(self):
        for method, path, body in self.admin_only():
            response = await self.call(method, path, body, who=self.employee)
            with self.subTest(method=method, path=path):
                self.assert_refused(response, 403, "ADMIN_ONLY")
        # Not one of those requests reached the data, and none was recorded as done.
        self.assertEqual(set(self.repo.calls) & (WRITES | ADMIN_READS), set())
        self.assertEqual(self.repo.audits, [])
        self.assertEqual(self.repo.users[self.worker["user_id"]]["role"], "employee")
        self.assertEqual(len(self.repo.users), 2)

    async def test_an_admin_passes_the_same_role_check(self):
        for method, path, body in self.admin_only():
            response = await self.call(method, path, body)
            with self.subTest(method=method, path=path):
                self.assertNotIn(response.status_code, (401, 403), response.text)

    async def test_overview_hides_money_and_setup_from_an_employee(self):
        self.repo.runs = [{"run_id": 9, "status": "running", "finished_at": None},
                          {"run_id": 8, "status": "succeeded",
                           "finished_at": datetime(2026, 11, 10, 6, 0, tzinfo=UTC)}]
        employee = await self.call("GET", "/api/admin/overview", who=self.employee)
        admin = await self.call("GET", "/api/admin/overview")
        self.assertEqual((employee.status_code, admin.status_code), (200, 200))
        seen, full = employee.json(), admin.json()
        self.assertEqual(set(full), {"user", "inventory", "catalog_ready", "sync", "setup"})
        self.assertEqual(set(seen), {"user", "inventory", "catalog_ready", "sync"})
        self.assertNotIn("stock_value", employee.text)
        self.assertNotIn("setup", employee.text)
        # The admin answer came second and still has the figure: the cached report is
        # copied, not edited, for the employee. 3 x 45.5 + 7 x 45.5 + 2 x 30 + 2.5 x 850.
        self.assertEqual(full["inventory"]["stock_value"], 2640.0)
        self.assertEqual({key: value for key, value in full["inventory"].items()
                          if key != "stock_value"}, seen["inventory"])
        self.assertEqual(seen["inventory"]["tiles"]["available"], 6)
        self.assertEqual((seen["user"]["role"], full["user"]["role"]), ("employee", "admin"))
        self.assertIs(seen["catalog_ready"], True)
        self.assertEqual((seen["sync"]["available"], seen["sync"]["last_status"]),
                         (True, "succeeded"))
        self.assertEqual(full["setup"], ["phone", "map_url", "address", "hours", "milestones",
                                         "service_prices"])

    async def test_overview_says_when_the_catalog_has_not_loaded_yet(self):
        self.repo.failing.add("sync_runs")
        client = self.serve(catalog=None)  # nothing installed: the first load is pending
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        body = (await client.get("/api/admin/overview", headers=cookie)).json()
        self.assertIs(body["catalog_ready"], False)
        self.assertEqual((body["inventory"]["tiles"]["items"], body["inventory"]["stock_value"]),
                         (0, 0.0))
        # Unreadable sync runs are reported as such instead of failing the whole page.
        self.assertEqual(body["sync"], {"available": False, "runs": []})

    async def test_both_roles_look_up_inventory_with_codes_and_quantities(self):
        for who in (self.employee, self.admin):
            response = await self.call("GET", "/api/admin/inventory", who=who,
                                       params={"q": "zq1234"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["items"], [{
                "code": "ZQ-1234", "name": "სამუხრუჭე ხუნდი წინა",
                "fits": "TOYOTA CAMRY 2012-2017", "brand": "SATO", "price": 45.5, "store": 3.0,
                "warehouse": 0.0, "state": "IN_STORE"}])
            self.assertEqual(set(body), {"items", "page", "total", "has_more", "options"})
        filters = (({"state": "out"}, 1), ({"state": "available"}, 6), ({}, 8),
                   ({"make": "Honda", "brand": " sakura "}, 2), ({"page": 2}, 8))
        for params, total in filters:
            response = await self.call("GET", "/api/admin/inventory", who=self.employee,
                                       params=params)
            self.assertEqual((response.status_code, response.json()["total"]), (200, total),
                             params)
        for params in ({"state": "everything"}, {"q": "x" * 81}, {"page": 0}, {"page": 2001}):
            response = await self.call("GET", "/api/admin/inventory", who=self.employee,
                                       params=params)
            self.assertEqual(response.status_code, 422, params)

    async def test_engine_oil_inventory_uses_current_rows_and_labels(self):
        today = datetime.now(UTC)
        old = {"source_product_id": "new-oil", "product_code": "NEW-OIL",
               "product_name": "Example 0W20 (VW 508.00) 1L", "brand": "Example",
               "stock": {"store": 2, "tuz11": 0}, "sae_grades": ["0W-20"],
               "last_observed_at": today, "price_raw": "30",
               "catalog_review_status": "NEEDS_APPROVAL_REVIEW"}
        other = {**old, "source_product_id": "other", "product_name": "Oil pump",
                 "catalog_review_status": "OTHER_PRODUCT"}
        with (
            patch.object(self.legacy, "oil_review_products", return_value=[old, other],
                         create=True) as scan,
            patch.object(self.legacy, "oil_approval_products", return_value=[], create=True),
        ):
            params = {"category": "engine_oil", "state": "available"}
            refused = await self.client.get("/api/admin/inventory", params=params)
            self.assertEqual(refused.status_code, 401)
            scan.assert_not_called()
            response = await self.call("GET", "/api/admin/inventory", who=self.employee,
                                       params=params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)
            item = response.json()["items"][0]
            self.assertEqual((item["code"], item["store"], item["price"]), ("NEW-OIL", 2, 30))
            self.assertEqual(item["oil"], {"grade": "0W-20", "status": "NEEDS_APPROVAL_REVIEW",
                                           "catalog_specs": "VW 508.00",
                                           "reviewed_approvals": ""})

            scan.return_value = [{**old, "stock": {"store": 0, "tuz11": 0}}, other]
            depleted = await self.call("GET", "/api/admin/inventory", who=self.employee,
                                       params=params)
            self.assertEqual(depleted.json()["items"], [])

    async def test_a_database_failure_is_a_plain_503(self):
        self.repo.failing.add("list_users")
        response = await self.call("GET", "/api/admin/users")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("withheld", response.text)


class ContentTests(AdminCase):
    async def test_content_is_read_with_its_schema_and_versions(self):
        response = await self.call("GET", "/api/admin/content")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"schema", "source", "sections"})
        self.assertEqual(list(body["sections"]), list(SECTIONS))
        self.assertEqual(body["sections"]["hero"], {"document": DEFAULT_SECTIONS["hero"],
                                                    "version": 0})
        self.assertEqual([section["key"] for section in body["schema"]["sections"]],
                         list(SECTIONS))

    async def test_saving_stores_audits_and_is_visible_at_once(self):
        await self.call("GET", "/api/admin/content")  # the snapshot is cached now
        document = hero("  New\x00 headline  ", unknown_field=MARKER)
        response = await self.call("PUT", "/api/admin/content/hero",
                                   {"document": document, "version": 0})
        self.assertEqual(response.status_code, 200)
        cleaned = hero("New headline")
        self.assertEqual(response.json(), {"version": 1, "document": cleaned})
        stored = self.repo.sections["hero"]
        self.assertEqual((stored["document"], stored["version"], stored["updated_by"]),
                         (cleaned, 1, self.boss["user_id"]))
        self.assertEqual(self.repo.audits, [{
            "audit_id": 1, "occurred_at": self.repo.audits[0]["occurred_at"],
            "actor_username": "boss", "action": "content.saved", "target": "hero",
            "detail": {}, "client_ip": "127.0.0.1"}])
        # No waiting for the cache: the editor and the public page read the new text.
        after = await self.call("GET", "/api/admin/content")
        self.assertEqual(after.json()["sections"]["hero"], {"document": cleaned, "version": 1})
        self.assertIn("New headline", await self.page_text("/en"))
        second = await self.call("PUT", "/api/admin/content/hero",
                                 {"document": hero("Second headline"), "version": 1})
        self.assertEqual(second.json()["version"], 2)

    async def test_invalid_fields_are_named_by_code_and_nothing_is_saved(self):
        hostile = f"<script>{MARKER}</script>"
        document = hero(hostile * 3, subline={"ka": " ".join([MARKER] * 21), "en": 7})
        response = await self.call("PUT", "/api/admin/content/hero",
                                   {"document": document, "version": 0})
        self.assert_refused(response, 422, "INVALID_FIELDS", {
            "title.en": "TOO_LONG", "subline.ka": "TOO_LONG", "subline.en": "INVALID"})
        self.assertNotIn(MARKER, response.text)
        brand = {**copy.deepcopy(DEFAULT_SECTIONS["brand"]), "phone": MARKER,
                 "map_url": "http://maps.example.ge/" + MARKER, "email": MARKER,
                 "hours": [{}] * 8}
        response = await self.call("PUT", "/api/admin/content/brand",
                                   {"document": brand, "version": 0})
        fields = response.json()["detail"]["fields"]
        self.assertEqual(response.status_code, 422)
        self.assertEqual({name: fields[name] for name in ("phone", "map_url", "email",
                                                          "hours")},
                         {"phone": "INVALID", "map_url": "INVALID", "email": "INVALID",
                          "hours": "TOO_MANY"})
        self.assertNotIn(MARKER, response.text)
        for body in ({"version": 0}, {"document": "text", "version": 0},
                     {"document": None, "version": 0}):
            response = await self.call("PUT", "/api/admin/content/hero", body)
            self.assert_refused(response, 422, "INVALID_FIELDS", {"": "INVALID"})
        self.assertNotIn("save_section", self.repo.calls)
        self.assertEqual((self.repo.sections, self.repo.audits), ({}, []))

    async def test_a_stale_version_is_a_conflict_and_changes_nothing(self):
        first = await self.call("PUT", "/api/admin/content/hero",
                                {"document": hero("Mine"), "version": 0})
        self.assertEqual(first.status_code, 200)
        for version in (0, 2, 7):
            response = await self.call("PUT", "/api/admin/content/hero",
                                       {"document": hero("Theirs"), "version": version})
            self.assert_refused(response, 409, "VERSION_CONFLICT")
        self.assertEqual(self.repo.sections["hero"]["document"]["title"]["en"], "Mine")
        self.assertEqual(self.repo.sections["hero"]["version"], 1)
        self.assertEqual(self.repo.actions(), ["content.saved"])

    async def test_the_version_and_the_section_must_be_valid(self):
        for version in (None, -1, "0", True, 1.5, [0]):
            response = await self.call("PUT", "/api/admin/content/hero",
                                       {"document": hero(), "version": version})
            self.assert_refused(response, 422, "INVALID_FIELDS", {"version": "INVALID"})
        response = await self.call("PUT", "/api/admin/content/hero", {"document": hero()})
        self.assert_refused(response, 422, "INVALID_FIELDS", {"version": "INVALID"})
        for key in ("no_such_section", "services", "Hero"):
            response = await self.call("PUT", f"/api/admin/content/{key}",
                                       {"document": hero(), "version": 0})
            self.assert_refused(response, 404, "UNKNOWN_SECTION")
        for body in ([hero()], "text"):
            response = await self.call("PUT", "/api/admin/content/hero", body)
            self.assertEqual(response.status_code, 422)
        self.assertNotIn("save_section", self.repo.calls)

    async def test_a_failed_audit_does_not_leave_the_editor_on_a_stale_version(self):
        await self.call("GET", "/api/admin/content")
        self.repo.failing.add("audit")
        response = await self.call("PUT", "/api/admin/content/hero",
                                   {"document": hero("Saved anyway"), "version": 0})
        self.assertEqual(response.status_code, 503)
        self.repo.failing.clear()
        # The text was stored before the audit failed, so the editor must see version 1;
        # with the cached version 0 every further save would be a conflict.
        after = await self.call("GET", "/api/admin/content")
        self.assertEqual(after.json()["sections"]["hero"]["version"], 1)
        self.assertEqual(after.json()["sections"]["hero"]["document"]["title"]["en"],
                         "Saved anyway")


class ServiceTests(AdminCase):
    async def create(self, **changes) -> dict:
        response = await self.call("POST", "/api/admin/services", {**SERVICE, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def listed(self) -> list[str]:
        response = await self.call("GET", "/api/admin/services")
        return [row["title_en"] for row in response.json()["services"]]

    async def test_create_validates_stores_audits_and_refreshes_the_list(self):
        self.assertEqual(await self.listed(), [])
        created = await self.create(title_en="  Tyre\x00 fitting ", service_id=99, position=50)
        self.assertEqual({key: created[key] for key in SERVICE},
                         {**SERVICE, "title_en": "Tyre fitting", "price_from": 45.0})
        self.assertEqual((created["version"], created["position"]), (1, 1))
        self.assertNotEqual(created["service_id"], 99)
        self.assertEqual(self.repo.audits[-1]["action"], "service.created")
        self.assertEqual(self.repo.audits[-1]["target"], str(created["service_id"]))
        self.assertEqual(await self.listed(), ["Tyre fitting"])
        self.assertIn("Tyre fitting", await self.page_text("/en"))

    async def test_invalid_services_are_refused_with_field_codes(self):
        hostile = f"<script>{MARKER}</script>"
        response = await self.call("POST", "/api/admin/services", {
            "title_ka": "", "title_en": hostile * 5, "icon": hostile, "price_from": hostile,
            "summary_ka": hostile * 20, "is_published": hostile})
        self.assert_refused(response, 422, "INVALID_FIELDS", {
            "title_ka": "REQUIRED", "title_en": "TOO_LONG", "summary_ka": "TOO_LONG",
            "icon": "INVALID", "price_from": "INVALID", "is_published": "INVALID"})
        self.assertNotIn(MARKER, response.text)
        self.assertEqual((self.repo.services, self.repo.audits), ({}, []))

    async def test_update_needs_the_current_version(self):
        created = await self.create()
        path = f"/api/admin/services/{created['service_id']}"
        updated = await self.call("PUT", path, {**SERVICE, "title_en": "Checks",
                                                "price_from": None, "version": 1})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual((updated.json()["title_en"], updated.json()["price_from"],
                          updated.json()["version"]), ("Checks", None, 2))
        stale = await self.call("PUT", path, {**SERVICE, "title_en": "Stale", "version": 1})
        self.assert_refused(stale, 409, "VERSION_CONFLICT")
        self.assertEqual(await self.listed(), ["Checks"])
        missing = await self.call("PUT", "/api/admin/services/999999", {**SERVICE, "version": 1})
        self.assert_refused(missing, 404, "NOT_FOUND")
        invalid = await self.call("PUT", path, {**SERVICE, "title_en": "", "version": 2})
        self.assert_refused(invalid, 422, "INVALID_FIELDS", {"title_en": "REQUIRED"})
        unversioned = await self.call("PUT", path, SERVICE)
        self.assert_refused(unversioned, 422, "INVALID_FIELDS", {"version": "INVALID"})
        self.assertEqual(self.repo.actions(), ["service.created", "service.saved"])

    async def test_unpublishing_takes_a_service_off_the_public_page(self):
        created = await self.create(title_en="Wheel alignment")
        self.assertIn("Wheel alignment", await self.page_text("/en"))
        response = await self.call("PUT", f"/api/admin/services/{created['service_id']}",
                                   {**SERVICE, "title_en": "Wheel alignment",
                                    "is_published": False, "version": 1})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Wheel alignment", await self.page_text("/en"))

    async def test_delete_and_reorder(self):
        ids = [(await self.create(title_en=name))["service_id"] for name in ("A", "B", "C")]
        response = await self.call("POST", "/api/admin/services/order",
                                   {"ids": [ids[2], ids[0], ids[1]]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(await self.listed(), ["C", "A", "B"])
        self.assertEqual((await self.call("DELETE", f"/api/admin/services/{ids[0]}")).status_code,
                         204)
        self.assertEqual(await self.listed(), ["C", "B"])
        again = await self.call("DELETE", f"/api/admin/services/{ids[0]}")
        self.assert_refused(again, 404, "NOT_FOUND")
        self.assertEqual(self.repo.actions()[-2:], ["service.reordered", "service.deleted"])
        for payload in ({}, {"ids": "1,2"}, {"ids": [1, "2"]}, {"ids": [True]},
                        {"ids": [1.5]}, {"ids": list(range(501))}):
            response = await self.call("POST", "/api/admin/services/order", payload)
            self.assert_refused(response, 422, "INVALID_FIELDS", {"ids": "INVALID"})
        self.assertEqual(self.repo.calls.count("reorder"), 1)


class MediaTests(AdminCase):
    async def upload(self, data: bytes, **headers) -> dict:
        response = await self.call("POST", "/api/admin/media", data, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_png_jpeg_and_webp_uploads_are_stored_unpublished(self):
        images = ((png(4, 3), "image/png"), (jpeg(4, 3), "image/jpeg"),
                  (webp_lossy(4, 3), "image/webp"), (webp_extended(4, 3), "image/webp"))
        for position, (data, content_type) in enumerate(images, start=1):
            row = await self.upload(data)
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual({key: row[key] for key in (
                "sha256", "content_type", "width", "height", "alt_ka", "alt_en", "position",
                "is_published", "byte_size", "url")}, {
                "sha256": digest, "content_type": content_type, "width": 4, "height": 3,
                "alt_ka": "", "alt_en": "", "position": position, "is_published": False,
                "byte_size": len(data), "url": f"/api/admin/media/file/{digest}"})
            self.assertEqual(self.repo.media[row["media_id"]]["data"], data)
            self.assertEqual(self.repo.media[row["media_id"]]["created_by"],
                             self.boss["user_id"])
            self.assertEqual(self.repo.audits[-1]["action"], "media.uploaded")
            self.assertEqual(self.repo.audits[-1]["target"], digest[:16])
        listed = await self.call("GET", "/api/admin/media")
        self.assertEqual([row["content_type"] for row in listed.json()["media"]],
                         [content_type for _, content_type in images])
        self.assertNotIn("data", listed.json()["media"][0])

    async def test_the_type_comes_from_the_bytes_not_from_the_request(self):
        row = await self.upload(png(), **{"Content-Type": "image/svg+xml"})
        self.assertEqual(row["content_type"], "image/png")
        response = await self.call("POST", "/api/admin/media", SVG,
                                   headers={"Content-Type": "image/png"})
        self.assert_refused(response, 422, "INVALID_IMAGE")

    async def test_svg_truncated_and_oversized_uploads_are_refused(self):
        refused = {"svg": SVG, "gif": GIF, "empty": b"", "png signature only": png()[:8],
                   "png cut inside its header": png()[:20], "jpeg cut before its frame":
                   jpeg()[:40], "webp cut before its size": webp_lossy()[:24],
                   "over 4096 px": png(4097, 10, pixels=False),
                   "over the stored size": padded(png(), MAX_BYTES + 1)}
        for name, data in refused.items():
            response = await self.call("POST", "/api/admin/media", data)
            with self.subTest(name):
                self.assert_refused(response, 422, "INVALID_IMAGE")
        too_big = await self.call("POST", "/api/admin/media", padded(png(), 1_700_001))
        self.assert_refused(too_big, 413, "REQUEST_TOO_LARGE")
        self.assertNotIn("add_media", self.repo.calls)
        self.assertEqual((self.repo.media, self.repo.audits), ({}, []))
        largest = await self.upload(padded(png(), MAX_BYTES))
        self.assertEqual(largest["byte_size"], MAX_BYTES)

    async def test_the_same_image_is_stored_once(self):
        first, again = await self.upload(png(2, 2)), await self.upload(png(2, 2))
        self.assertEqual(first["media_id"], again["media_id"])
        self.assertEqual(len(self.repo.media), 1)

    async def test_publishing_controls_what_the_public_gallery_serves(self):
        data = png(2, 2)
        row = await self.upload(data)
        public, preview = f"/media/{row['sha256']}", row["url"]
        self.assertEqual((await self.client.get(public)).status_code, 404)
        # The dashboard can preview an unpublished image; nobody else can.
        shown = await self.call("GET", preview)
        self.assertEqual((shown.status_code, shown.content, shown.headers["content-type"]),
                         (200, data, "image/png"))
        self.assertEqual(shown.headers["x-content-type-options"], "nosniff")
        self.assertEqual((await self.client.get(preview)).status_code, 401)
        self.assert_refused(await self.call("GET", preview, who=self.employee), 403,
                            "ADMIN_ONLY")
        patched = await self.call("PATCH", f"/api/admin/media/{row['media_id']}", {
            "is_published": True, "alt_ka": "  ფარეხი   შიგნით ", "alt_en": "Inside"})
        self.assertEqual(patched.status_code, 200)
        self.assertEqual((patched.json()["is_published"], patched.json()["alt_ka"],
                          patched.json()["alt_en"]), (True, "ფარეხი შიგნით", "Inside"))
        served = await self.client.get(public)
        self.assertEqual((served.status_code, served.content, served.headers["content-type"]),
                         (200, data, "image/png"))
        self.assertEqual(served.headers["cache-control"], "public, max-age=3600")
        # Unpublishing takes effect at once, although the image had been cached in memory.
        await self.call("PATCH", f"/api/admin/media/{row['media_id']}", {"is_published": False})
        self.assertEqual((await self.client.get(public)).status_code, 404)
        await self.call("PATCH", f"/api/admin/media/{row['media_id']}", {"is_published": True})
        self.assertEqual((await self.client.get(public)).status_code, 200)
        deleted = await self.call("DELETE", f"/api/admin/media/{row['media_id']}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual((await self.client.get(public)).status_code, 404)
        self.assert_refused(await self.call("GET", preview), 404, "NOT_FOUND")
        self.assertEqual(self.repo.actions(), ["media.uploaded", "media.saved", "media.saved",
                                               "media.saved", "media.deleted"])

    async def test_media_edits_are_validated(self):
        row = await self.upload(png())
        path = f"/api/admin/media/{row['media_id']}"
        cases = (({}, {"": "REQUIRED"}), ({"position": 3}, {"": "REQUIRED"}),
                 ({"alt_ka": "x" * 161}, {"alt_ka": "TOO_LONG"}),
                 ({"alt_en": 5, "is_published": "yes"},
                  {"alt_en": "INVALID", "is_published": "INVALID"}))
        for payload, fields in cases:
            self.assert_refused(await self.call("PATCH", path, payload), 422, "INVALID_FIELDS",
                                fields)
        self.assertNotIn("update_media", self.repo.calls)
        missing = await self.call("PATCH", "/api/admin/media/999999", {"alt_en": "x"})
        self.assert_refused(missing, 404, "NOT_FOUND")
        self.assert_refused(await self.call("DELETE", "/api/admin/media/999999"), 404,
                            "NOT_FOUND")
        for digest in ("a" * 64, "short", "z" * 64, "a" * 63 + "!"):
            response = await self.call("GET", f"/api/admin/media/file/{digest}")
            self.assert_refused(response, 404, "NOT_FOUND")

    async def test_gallery_order(self):
        ids = [(await self.upload(png(size, size)))["media_id"] for size in (1, 2, 3)]
        response = await self.call("POST", "/api/admin/media/order", {"ids": ids[::-1]})
        self.assertEqual(response.status_code, 204)
        listed = await self.call("GET", "/api/admin/media")
        self.assertEqual([row["media_id"] for row in listed.json()["media"]], ids[::-1])
        bad = await self.call("POST", "/api/admin/media/order", {"ids": ["1"]})
        self.assert_refused(bad, 422, "INVALID_FIELDS", {"ids": "INVALID"})


class AccountTests(AdminCase):
    async def cached_session(self, username: str = "worker") -> dict:
        """Sign in and make one request, so the session sits in the 30 s auth cache."""
        headers = await self.sign_in(self.client, username)
        cookie = {"Cookie": headers["Cookie"]}
        self.assertEqual((await self.client.get("/api/auth/session", headers=cookie)).status_code,
                         200)
        return cookie

    async def signed_in(self, cookie: dict) -> bool:
        return (await self.client.get("/api/auth/session", headers=cookie)).status_code == 200

    async def test_accounts_are_listed_without_their_hashes(self):
        response = await self.call("GET", "/api/admin/users")
        self.assertEqual([row["username"] for row in response.json()["users"]],
                         ["boss", "worker"])
        self.assertEqual(response.json()["users"][0]["role"], "admin")
        for private in ("password_hash", "scrypt$", PASSWORD):
            self.assertNotIn(private, response.text)

    async def test_create_stores_a_hash_and_the_account_can_sign_in(self):
        response = await self.call("POST", "/api/admin/users", {
            "username": "  NewComer ", "display_name": "  New   Comer ", "role": "employee",
            "password": NEW_PASSWORD})
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual((created["username"], created["display_name"], created["role"],
                          created["is_active"]), ("newcomer", "New Comer", "employee", True))
        stored = self.repo.users[created["user_id"]]["password_hash"]
        self.assertTrue(verify_password(NEW_PASSWORD, stored))
        self.assertEqual(self.repo.audits[-1], {
            **self.repo.audits[-1], "actor_username": "boss", "action": "user.created",
            "target": "newcomer", "detail": {"role": "employee"}})
        for text in (response.text, str(self.repo.audits)):
            self.assertNotIn(NEW_PASSWORD, text)
            self.assertNotIn("scrypt$", text)
        headers = await self.sign_in(self.client, "newcomer", NEW_PASSWORD)
        overview = await self.call("GET", "/api/admin/overview", who=headers)
        self.assertEqual(overview.json()["user"]["role"], "employee")

    async def test_create_reports_every_invalid_field_at_once(self):
        response = await self.call("POST", "/api/admin/users", {
            "username": "No Such Name!", "role": "owner", "display_name": "d" * 81,
            "password": "short"})
        self.assert_refused(response, 422, "INVALID_FIELDS", {
            "username": "INVALID", "role": "INVALID", "display_name": "INVALID",
            "password": "TOO_SHORT"})
        cases = (
            ({"username": "ab"}, {"username": "INVALID"}),
            ({"username": "-leading"}, {"username": "INVALID"}),
            ({"username": "u" * 33}, {"username": "INVALID"}),
            ({"username": None}, {"username": "INVALID"}),
            ({"role": None}, {"role": "INVALID"}),
            ({"role": ["admin"]}, {"role": "INVALID"}),
            ({"display_name": 5}, {"display_name": "INVALID"}),
            ({"password": None}, {"password": "REQUIRED"}),
            ({"password": 1234567890}, {"password": "REQUIRED"}),
            ({"password": "aaaaaaaaaaaa"}, {"password": "TOO_SIMPLE"}),
            ({"password": "x" * 129}, {"password": "TOO_LONG"}),
            ({"username": "administrator", "password": "Administrator"},
             {"password": "SAME_AS_USERNAME"}),
        )
        valid = {"username": "newcomer", "role": "employee", "password": NEW_PASSWORD}
        for change, fields in cases:
            response = await self.call("POST", "/api/admin/users", {**valid, **change})
            with self.subTest(change=change):
                self.assert_refused(response, 422, "INVALID_FIELDS", fields)
        self.assertNotIn("create_user", self.repo.calls)
        self.assertEqual(len(self.repo.users), 2)

    async def test_a_taken_name_is_a_conflict(self):
        for username in ("worker", " WORKER "):
            response = await self.call("POST", "/api/admin/users", {
                "username": username, "role": "admin", "password": NEW_PASSWORD})
            self.assert_refused(response, 409, "USERNAME_TAKEN", {"username": "TAKEN"})
        self.assertEqual(self.repo.users[self.worker["user_id"]]["role"], "employee")
        self.assertEqual(self.repo.audits, [])

    async def test_the_only_admin_cannot_be_demoted_or_disabled(self):
        mine = f"/api/admin/users/{self.boss['user_id']}"
        for change in ({"role": "employee"}, {"is_active": False},
                       {"role": "employee", "is_active": False}):
            self.assert_refused(await self.call("PATCH", mine, change), 409, "LAST_ADMIN")
        self.assertEqual(self.repo.users[self.boss["user_id"]]["role"], "admin")
        self.assertTrue(await self.signed_in({"Cookie": self.admin["Cookie"]}))
        self.assertEqual(self.repo.audits, [])
        # Other edits to that account are fine, and so is demotion once there is a second.
        renamed = await self.call("PATCH", mine, {"display_name": " The  Boss "})
        self.assertEqual((renamed.status_code, renamed.json()["display_name"]),
                         (200, "The Boss"))
        promoted = await self.call("PATCH", f"/api/admin/users/{self.worker['user_id']}",
                                   {"role": "admin"})
        self.assertEqual(promoted.json()["role"], "admin")
        demoted = await self.call("PATCH", mine, {"role": "employee"})
        self.assertEqual((demoted.status_code, demoted.json()["role"]), (200, "employee"))
        self.assertEqual(self.repo.audits[-1]["detail"], {"role": "employee"})

    async def test_disabling_or_rescoping_an_account_signs_it_out_at_once(self):
        target = f"/api/admin/users/{self.worker['user_id']}"
        for change in ({"is_active": False}, {"role": "admin"}):
            self.repo.users[self.worker["user_id"]].update(is_active=True, role="employee")
            cookie = await self.cached_session()
            response = await self.call("PATCH", target, change)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(await self.signed_in(cookie), change)
        # A change of display name alone leaves the session alone.
        self.repo.users[self.worker["user_id"]].update(is_active=True, role="employee")
        cookie = await self.cached_session()
        await self.call("PATCH", target, {"display_name": "Worker"})
        self.assertTrue(await self.signed_in(cookie))

    async def test_account_edits_are_validated(self):
        target = f"/api/admin/users/{self.worker['user_id']}"
        cases = (({}, {"": "REQUIRED"}), ({"username": "renamed"}, {"": "REQUIRED"}),
                 ({"password_hash": "scrypt$forged"}, {"": "REQUIRED"}),
                 ({"role": "owner"}, {"role": "INVALID"}),
                 ({"is_active": "no"}, {"is_active": "INVALID"}),
                 ({"display_name": "d" * 81, "role": None},
                  {"display_name": "INVALID", "role": "INVALID"}))
        for payload, fields in cases:
            self.assert_refused(await self.call("PATCH", target, payload), 422,
                                "INVALID_FIELDS", fields)
        self.assertNotIn("update_user", self.repo.calls)
        missing = await self.call("PATCH", "/api/admin/users/999999", {"display_name": "x"})
        self.assert_refused(missing, 404, "NOT_FOUND")

    async def test_a_password_reset_ends_the_sessions_and_replaces_the_password(self):
        cookie = await self.cached_session()
        target = f"/api/admin/users/{self.worker['user_id']}/password"
        for payload, problem in (({}, "REQUIRED"), ({"password": "short"}, "TOO_SHORT"),
                                 ({"password": ["x"]}, "REQUIRED")):
            self.assert_refused(await self.call("POST", target, payload), 422,
                                "INVALID_FIELDS", {"password": problem})
        self.assertTrue(await self.signed_in(cookie))
        response = await self.call("POST", target, {"password": NEW_PASSWORD})
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertFalse(await self.signed_in(cookie))  # at once, not when the cache expires
        self.assertTrue(await self.signed_in({"Cookie": self.admin["Cookie"]}))
        old = await self.client.post("/api/auth/login", headers=ORIGIN,
                                     json={"username": "worker", "password": PASSWORD})
        self.assertEqual(old.status_code, 401)
        await self.sign_in(self.client, "worker", NEW_PASSWORD)
        reset = [row for row in self.repo.audits if row["action"] == "user.password_reset"]
        self.assertEqual([(row["actor_username"], row["target"]) for row in reset],
                         [("boss", "worker")])
        self.assertNotIn(NEW_PASSWORD, str(self.repo.audits))
        missing = await self.call("POST", "/api/admin/users/999999/password",
                                  {"password": NEW_PASSWORD})
        self.assert_refused(missing, 404, "NOT_FOUND")

    async def test_revoking_sessions_signs_the_account_out_everywhere_at_once(self):
        first, second = await self.cached_session(), await self.cached_session()
        response = await self.call(
            "POST", f"/api/admin/users/{self.worker['user_id']}/sessions/revoke")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(await self.signed_in(first))
        self.assertFalse(await self.signed_in(second))
        self.assertTrue(await self.signed_in({"Cookie": self.admin["Cookie"]}))
        self.assertEqual(self.repo.audits[-1]["action"], "user.sessions_revoked")
        await self.sign_in(self.client, "worker")  # the password itself is unchanged

    async def test_audit_trail_is_newest_first_and_pages_backwards(self):
        for name in ("A", "B", "C"):
            await self.call("POST", "/api/admin/services", {**SERVICE, "title_en": name})
        response = await self.call("GET", "/api/admin/audit")
        rows = response.json()["rows"]
        self.assertEqual([row["audit_id"] for row in rows], [3, 2, 1])
        self.assertEqual({row["actor_username"] for row in rows}, {"boss"})
        self.assertEqual(set(rows[0]), {"audit_id", "occurred_at", "actor_username", "action",
                                        "target", "detail"})
        older = await self.call("GET", "/api/admin/audit", params={"before": 2})
        self.assertEqual([row["audit_id"] for row in older.json()["rows"]], [1])
        self.assertEqual((await self.call("GET", "/api/admin/audit",
                                          params={"before": 0})).status_code, 422)

    async def test_reports_degrade_when_their_sources_are_missing(self):
        sales = await self.call("GET", "/api/admin/reports/sales")
        self.assertEqual(sales.json(), {"available": False})
        chat = await self.call("GET", "/api/admin/reports/chat", params={"days": 7})
        self.assertEqual(chat.json(), {"days": 7, "staff": None, "site": None})
        self.repo.trade = {"sales": [], "purchases": [], "since": None}
        self.repo.sellers = [{"source_product_id": "sid-0001", "units": 4, "amount": 182}]
        sales = await self.call("GET", "/api/admin/reports/sales", params={"days": 90})
        self.assertEqual(sales.json(), {
            "available": True, "days": 90, "since": None, "sales": [], "purchases": [],
            "best_sellers": [{"name": "სამუხრუჭე ხუნდი წინა", "brand": "SATO",
                              "fits": "TOYOTA CAMRY 2012-2017", "units": 4, "amount": 182}]})
        for path in ("/api/admin/reports/sales", "/api/admin/reports/chat"):
            for days in (6, 366, "month"):
                response = await self.call("GET", path, params={"days": days})
                self.assertEqual(response.status_code, 422, (path, days))


if __name__ == "__main__":
    unittest.main()
