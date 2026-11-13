"""Dashboard API for the Facebook page's activity: comments, page stats, posts, ads, pictures.

Everything but the last class runs through the assembled application (install_site, the site
guard included) with in-memory doubles: the feature modules' dashboard helpers are replaced, so
these tests pin what the routes do with them. No PostgreSQL, no Meta. The last class runs the
real helpers on MetaStore as a member of garage_site, on a disposable database (skipped without
WAREHOUSE_TEST_ADMIN_DATABASE_URL), so every query the routes make is checked against migration
0013's grants.
"""

from __future__ import annotations

import contextlib
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from automotive import meta_ads, meta_comments, meta_page_stats
from automotive.meta_comments import CommentActionError
from automotive.meta_config import MetaSettings
from automotive.meta_crypto import TokenCipher
from automotive.meta_graph import GraphError
from automotive.meta_store import MetaStore
from automotive.site_meta_activity import (
    MIN_SPEND,
    PREFIX,
    REPLY_LIMIT,
    build_meta_activity_router,
)
from tests.test_meta_store_database import ROLES_SQL, RoleDatabase
from tests.test_site_admin import AdminCase
from tests.test_site_guard import ORIGIN
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
APP_ID, PAGE_ID = "220000000000002", "110000000000001"
POST_ID = f"{PAGE_ID}_900000000000001"
COMMENT_ID = "900000000000001_800000000000001"
KEY = "ab" * 32
# A fixture token, readable on purpose; it is not a real credential.
PAGE_TOKEN = "EAAActivityPageFixture0004xyz"  # gitleaks:allow
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7"
       b"5\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
NOW = datetime(2026, 11, 11, 16, 0, tzinfo=UTC)
SETTINGS = MetaSettings(app_id=APP_ID, app_secret="s" * 32, verify_token="v" * 40,
                        token_key=bytes(range(32)), public_url="https://garage.example.test")


def comment(comment_id: str = COMMENT_ID, **changes) -> dict:
    """One row as meta_comments.list_comments shapes it."""
    return {"comment_id": comment_id, "post_id": POST_ID, "parent_id": "",
            "author_name": "Giorgi", "body": "F30 B46 ზეთის ფილტრი გაქვთ?",
            "created_time": "2026-11-11T09:00:00+00:00", "is_hidden": False,
            "permalink": "", "source": "webhook", "reply_status": "sent", "replied": True,
            "replied_psid": "7000000000001234", "public_reply_id": "",
            "updated_at": "2026-11-11T09:01:00+00:00", "post_excerpt": "ახალი ფილტრები",
            "post_permalink": f"https://www.facebook.com/{PAGE_ID}/posts/900000000000001",
            "link": "https://www.facebook.com/110000000000001/posts/900000000000001"
                    "?comment_id=800000000000001", **changes}


def ad(ad_id: str = "6000000000001", **changes) -> dict:
    """One row as meta_ads.ad_results shapes it."""
    return {"ad_id": ad_id, "account_id": "1383535660550763", "currency": "GEL",
            "name": "Oil filters", "status": "ACTIVE", "effective_status": "ACTIVE",
            "campaign_id": "5000000000001", "campaign_name": "Garage November",
            "objective": "OUTCOME_ENGAGEMENT", "adset_id": "5100000000001",
            "adset_name": "Tbilisi", "title": "", "body": "ზეთის შეცვლა", "story_id": "",
            "image_key": KEY, "created_time": "2026-11-01T08:00:00+00:00", "spend": 24.5,
            "impressions": 4000, "reach": 3100, "clicks": 80, "ctr": 2.0,
            "messaging_started": 7, "cost_per_chat": 3.5, "chats": 5, "chats_handled": 4,
            "ads_manager_url": "https://adsmanager.facebook.com/adsmanager/manage/ads?"
                               "act=1383535660550763&selected_ad_ids=6000000000001",
            **changes}


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    @contextlib.contextmanager
    def transaction(self):
        self.database.savepoints += 1
        yield self


class FakeDatabase:
    """Counts the logins (connections) a route opens; the helpers using it are doubles too."""

    def __init__(self) -> None:
        self.opened = 0
        self.savepoints = 0

    @contextlib.contextmanager
    def transaction(self):
        self.opened += 1
        yield FakeConnection(self)


class FakeStore:
    """The MetaStore methods these routes call themselves."""

    def __init__(self) -> None:
        self.db = FakeDatabase()
        self.calls: list = []
        self.events: list[dict] = []
        self.row = {"comments_enabled": True, "comments_enabled_at": NOW - timedelta(days=2),
                    "bot_paused_reason": "", "bot_enabled": False, "version": 3}

    def settings(self) -> dict:
        self.calls.append("settings")
        return dict(self.row)

    def recent_events(self, limit: int = 20) -> list[dict]:
        self.calls.append(("recent_events", limit))
        return [dict(row) for row in self.events[:limit]]


class Features:
    """Doubles of the feature modules' dashboard helpers; each call is recorded with the
    database handle it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.listing = {"comments": [comment()], "total": 1, "unreplied": 0, "limit": 30,
                        "offset": 0}
        self.followers = [{"day": "2026-11-09", "followers": 150},
                          {"day": "2026-11-10", "followers": 152},
                          {"day": "2026-11-11", "followers": 157}]
        self.metrics = {"page_media_view": [{"day": "2026-11-10", "value": 120},
                                            {"day": "2026-11-11", "value": 80}],
                        "page_follows": [{"day": "2026-11-11", "value": 2.5}]}
        self.status = {"page": {"day": "2026-11-11", "followers": 157, "fetched_at": "x",
                                "refused": None, "unavailable": ["page_post_engagements"]},
                       "posts": None}
        self.posts = [{"post_id": POST_ID, "created_time": "2026-11-10T10:00:00+00:00",
                       "message": "ახალი ფილტრები", "image_key": KEY, "reactions": 9,
                       "comments": 3, "shares": 1, "engagement": 13, "views": 400,
                       "viewers": 310,
                       "permalink": f"https://www.facebook.com/{PAGE_ID}/posts/1"},
                      {"post_id": f"{PAGE_ID}_2", "created_time": "2026-11-09T10:00:00+00:00",
                       "message": "", "image_key": None, "reactions": 0, "comments": 0,
                       "shares": 0, "engagement": 0, "views": None, "viewers": None,
                       "permalink": "https://evil.example/posts/2"}]
        self.images = {KEY: ("image/png", PNG)}
        self.ads = [ad(), ad("6000000000002", currency="USD", image_key=None,
                         ads_manager_url="javascript:alert(1)")]
        self.best = [ad()]
        self.accounts = [{"account_id": "1383535660550763", "name": "Owner", "currency": "GEL",
                          "timezone_name": "Asia/Tbilisi", "account_status": 1,
                          "garage_ads": 2, "synced_at": "2026-11-11T06:00:00+00:00"}]
        self.daily = [{"day": "2026-11-10", "currency": "GEL", "spend": 12.25,
                       "impressions": 2000, "clicks": 40, "messaging_started": 3}]
        self.action_error: CommentActionError | None = None

    def install(self, case: unittest.TestCase) -> None:
        for module, name, double in (
                (meta_comments, "list_comments", self.list_comments),
                (meta_comments, "set_hidden", self.set_hidden),
                (meta_comments, "public_reply", self.public_reply),
                (meta_page_stats, "followers_series", self.followers_series),
                (meta_page_stats, "metric_series", self.metric_series),
                (meta_page_stats, "insights_status", self.insights_status),
                (meta_page_stats, "top_posts", self.top_posts),
                (meta_page_stats, "image", self.image),
                (meta_ads, "ad_results", self.ad_results),
                (meta_ads, "best_ads", self.best_ads),
                (meta_ads, "ad_accounts", self.ad_accounts),
                (meta_ads, "daily_spend", self.daily_spend)):
            replaced = patch.object(module, name, double)
            replaced.start()
            case.addCleanup(replaced.stop)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def list_comments(self, db, limit=50, offset=0, only_unreplied=False):
        self.calls.append(("list_comments", db, limit, offset, only_unreplied))
        return dict(self.listing)

    def set_hidden(self, ctx, comment_id, hidden, actor, *, client_ip=None):
        self.calls.append(("set_hidden", comment_id, hidden, actor["username"], client_ip))
        if self.action_error is not None:
            raise self.action_error
        return {"comment_id": comment_id, "hidden": hidden}

    def public_reply(self, ctx, comment_id, text, actor, *, client_ip=None):
        self.calls.append(("public_reply", comment_id, text, actor["username"], client_ip))
        if self.action_error is not None:
            raise self.action_error
        return {"comment_id": comment_id, "public_reply_id": "900000000000001_811"}

    def followers_series(self, db, days=30):
        self.calls.append(("followers_series", db, days))
        return list(self.followers)

    def metric_series(self, db, metric, days=30):
        self.calls.append(("metric_series", db, metric, days))
        return list(self.metrics.get(metric, []))

    def insights_status(self, db):
        self.calls.append(("insights_status", db))
        return dict(self.status)

    def top_posts(self, db, days=30, limit=10):
        self.calls.append(("top_posts", db, days, limit))
        return [dict(row) for row in self.posts]

    def image(self, db, key):
        self.calls.append(("image", key))
        return self.images.get(key)

    def ad_results(self, db, days=30):
        self.calls.append(("ad_results", db, days))
        return [dict(row) for row in self.ads]

    def best_ads(self, db, days=30, min_spend=10.0, limit=10):
        self.calls.append(("best_ads", db, days, min_spend, limit))
        return [dict(row) for row in self.best]

    def ad_accounts(self, db):
        self.calls.append(("ad_accounts", db))
        return [dict(row) for row in self.accounts]

    def daily_spend(self, db, days=30):
        self.calls.append(("daily_spend", db, days))
        return [dict(row) for row in self.daily]


def concrete(path: str) -> str:
    return path.replace("{comment_id}", COMMENT_ID).replace("{key}", KEY)


BODIES = {"/comments/{comment_id}/hide": {"hidden": True},
          "/comments/{comment_id}/reply": {"text": "გმადლობთ, მოგწერეთ პირადში."}}


def activity_routes() -> list[tuple[str, str, dict | None]]:
    """Every (method, path, body) of this router, from its own route table."""
    found = []
    for route in build_meta_activity_router(object()).routes:
        for method in sorted(route.methods - {"HEAD"}):
            template = route.path.removeprefix(PREFIX)
            found.append((method, concrete(template), BODIES.get(template)))
    return sorted(found)


class ActivityCase(AdminCase):
    """AdminCase with this router wired to the doubles (``site={"meta_activity": ...}``)."""

    meta_settings: MetaSettings | None = SETTINGS

    def make_store(self):
        return FakeStore()

    async def asyncSetUp(self) -> None:
        self.store = self.make_store()
        self.features = Features()
        self.features.install(self)
        await super().asyncSetUp()

    def serve(self, settings=None, **kwargs):
        kwargs.setdefault("meta_activity", {"store": self.store, "graph": object(),
                                            "meta_settings": self.meta_settings,
                                            "clock": lambda: NOW})
        return super().serve(settings, **kwargs)

    async def fb(self, method: str, path: str, body=None, *, who: dict | None = None,
                 **kwargs):
        return await self.call(method, PREFIX + path, body, who=who, **kwargs)


class AccessTests(ActivityCase):
    def test_the_route_table_is_the_one_the_dashboard_uses(self):
        self.assertEqual([(method, path) for method, path, _ in activity_routes()], [
            ("GET", "/ads"), ("GET", "/ads/daily"), ("GET", "/comments"), ("GET", "/events"),
            ("GET", f"/images/{KEY}"), ("GET", "/page-stats"), ("GET", "/posts"),
            ("POST", f"/comments/{COMMENT_ID}/hide"), ("POST", f"/comments/{COMMENT_ID}/reply")])

    async def test_an_employee_is_refused_every_route_before_anything_is_read(self):
        for method, path, body in activity_routes():
            response = await self.fb(method, path, body, who=self.employee)
            with self.subTest(method=method, path=path):
                self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual((self.store.calls, self.features.calls, self.store.db.opened),
                         ([], [], 0))

    async def test_install_site_mounts_every_route_and_an_admin_passes(self):
        # The application comes from create_app -> install_site; nothing is included by hand.
        for method, path, body in activity_routes():
            response = await self.fb(method, path, body)
            with self.subTest(method=method, path=path):
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(self.features.names()), {
            "ad_results", "best_ads", "ad_accounts", "daily_spend", "list_comments", "image",
            "followers_series", "metric_series", "insights_status", "top_posts", "set_hidden",
            "public_reply"})

    async def test_writes_need_the_csrf_header_and_the_origin(self):
        cookie = {"Cookie": self.admin["Cookie"]}
        for method, path, body in activity_routes():
            if method == "GET":
                continue
            for headers, code in (({**cookie, **ORIGIN}, "CSRF_REQUIRED"),
                                  ({**cookie, "X-CSRF-Token": self.admin["X-CSRF-Token"]},
                                   "ORIGIN_REQUIRED")):
                response = await self.client.request(method, PREFIX + path, json=body,
                                                     headers=headers)
                with self.subTest(method=method, path=path, code=code):
                    self.assertEqual((response.status_code, response.json()),
                                     (403, {"detail": {"code": code}}))
        self.assertEqual(self.features.calls, [])

    async def test_reads_need_a_session(self):
        for method, path, _ in activity_routes():
            if method != "GET":
                continue
            response = await self.client.get(PREFIX + path)
            with self.subTest(path=path):
                self.assertEqual((response.status_code, response.json()),
                                 (401, {"detail": {"code": "NOT_SIGNED_IN"}}))

    async def test_without_the_meta_block_install_site_leaves_the_routes_off(self):
        client = self.serve(meta_activity=None)  # the real wiring: environment, site login
        admin = await self.sign_in(client)
        for method, path, body in activity_routes():
            response = await client.request(method, PREFIX + path, json=body, headers=admin)
            with self.subTest(method=method, path=path):
                self.assertEqual((response.status_code, response.json()),
                                 (503, {"detail": {"code": "META_NOT_CONFIGURED"}}))
        self.assertEqual(self.features.calls, [])
        # The rest of the dashboard is unaffected.
        self.assertEqual((await client.get("/api/admin/overview", headers=admin)).status_code,
                         200)


class NotConfiguredTests(ActivityCase):
    meta_settings = MetaSettings(app_id=APP_ID)

    async def test_every_route_is_503_for_an_admin_and_403_for_an_employee(self):
        for method, path, body in activity_routes():
            with self.subTest(method=method, path=path):
                self.assert_refused(await self.fb(method, path, body), 503,
                                    "META_NOT_CONFIGURED")
                self.assert_refused(await self.fb(method, path, body, who=self.employee), 403,
                                    "ADMIN_ONLY")
        self.assertEqual((self.store.calls, self.features.calls), ([], []))


class EventTests(ActivityCase):
    async def test_events_show_bookkeeping_and_what_became_of_each_item(self):
        received = NOW - timedelta(minutes=5)
        self.store.events = [
            {"inbox_id": 6, "field": "messages", "received_at": received, "processed_at": None,
             "attempts": 0, "outcome": ""},
            {"inbox_id": 5, "field": "messages", "received_at": received,
             "processed_at": received, "attempts": 1, "outcome": "claimed"},
            {"inbox_id": 4, "field": "feed", "received_at": received, "processed_at": None,
             "attempts": 2, "outcome": "retry: database_08006"},
            {"inbox_id": 3, "field": "feed", "received_at": received,
             "processed_at": received, "attempts": 5, "outcome": "failed: graph_code_190"},
            {"inbox_id": 2, "field": "messages", "received_at": received,
             "processed_at": received, "attempts": 1, "outcome": "comment: comment_replied"},
        ]
        response = await self.fb("GET", "/events", params={"limit": 5})
        body = response.json()
        self.assertEqual([(row["inbox_id"], row["state"]) for row in body["events"]], [
            (6, "waiting"), (5, "working"), (4, "retrying"), (3, "failed"), (2, "done")])
        self.assertEqual(set(body["events"][0]), {"inbox_id", "field", "received_at",
                                                  "processed_at", "attempts", "outcome",
                                                  "state"})
        self.assertEqual(body["events"][0]["received_at"], received.isoformat())
        self.assertEqual(body["checked_at"], NOW.isoformat())
        self.assertEqual(self.store.calls, [("recent_events", 5)])

    async def test_the_limit_is_bounded(self):
        for limit in (0, 201, "many"):
            response = await self.fb("GET", "/events", params={"limit": limit})
            self.assertEqual(response.status_code, 422, limit)
        self.assertEqual(self.store.calls, [])
        await self.fb("GET", "/events")
        self.assertEqual(self.store.calls, [("recent_events", 50)])


class CommentListTests(ActivityCase):
    async def test_the_list_carries_counts_paging_and_the_comment_switch(self):
        self.features.listing.update(total=31, unreplied=4, limit=30, offset=0)
        response = await self.fb("GET", "/comments",
                                 params={"limit": 30, "offset": 0, "unreplied": "true"})
        body = response.json()
        self.assertEqual((body["total"], body["unreplied"], body["has_more"]), (31, 4, True))
        self.assertEqual(body["comments"][0]["body"], "F30 B46 ზეთის ფილტრი გაქვთ?")
        self.assertEqual(body["switch"], {
            "comments_enabled": True,
            "comments_enabled_at": (NOW - timedelta(days=2)).isoformat(),
            "bot_paused_reason": ""})
        (name, db, limit, offset, unreplied), = self.features.calls
        self.assertEqual((name, limit, offset, unreplied), ("list_comments", 30, 0, True))
        self.assertIs(type(db).__name__, "_Connection")
        self.assertEqual(self.store.db.opened, 1)

    async def test_the_unreplied_list_ends_where_the_unreplied_comments_do(self):
        # 50 comments, 5 unreplied: "Show more" must go once the 5 are shown.
        self.features.listing.update(comments=[comment()] * 5, total=50, unreplied=5,
                                     offset=0)
        body = (await self.fb("GET", "/comments", params={"unreplied": "true"})).json()
        self.assertFalse(body["has_more"])
        self.features.listing.update(comments=[], offset=5)
        body = (await self.fb("GET", "/comments",
                              params={"unreplied": "true", "offset": 5})).json()
        self.assertFalse(body["has_more"])
        self.features.listing.update(comments=[comment()] * 5, offset=0)
        self.assertTrue((await self.fb("GET", "/comments")).json()["has_more"])  # all 50

    async def test_only_facebook_https_links_reach_the_page(self):
        self.features.listing["comments"] = [comment(
            link="javascript:alert(1)", permalink="https://www.facebook.com.evil.test/x",
            post_permalink="http://www.facebook.com/plain-http")]
        shown = (await self.fb("GET", "/comments")).json()["comments"][0]
        self.assertEqual((shown["link"], shown["permalink"], shown["post_permalink"]),
                         ("", "", ""))
        self.features.listing["comments"] = [comment()]
        shown = (await self.fb("GET", "/comments")).json()["comments"][0]
        self.assertTrue(shown["link"].startswith("https://www.facebook.com/"))

    async def test_paging_is_bounded(self):
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"unreplied": "maybe"}):
            response = await self.fb("GET", "/comments", params=params)
            self.assertEqual(response.status_code, 422, params)
        self.assertEqual(self.features.calls, [])


class CommentActionTests(ActivityCase):
    async def test_hiding_passes_the_admin_and_the_address_on(self):
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/hide", {"hidden": True})
        self.assertEqual(response.json(), {"comment_id": COMMENT_ID, "hidden": True})
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/hide", {"hidden": False})
        self.assertEqual(response.json(), {"comment_id": COMMENT_ID, "hidden": False})
        self.assertEqual([call[:4] for call in self.features.calls], [
            ("set_hidden", COMMENT_ID, True, "boss"), ("set_hidden", COMMENT_ID, False, "boss")])
        self.assertEqual(self.features.calls[0][4], "127.0.0.1")

    async def test_bodies_are_checked_and_named_by_code(self):
        cases = [
            ("hide", None, {"hidden": "REQUIRED"}),
            ("hide", {"hidden": "true"}, {"hidden": "INVALID"}),
            ("hide", {"hidden": 1}, {"hidden": "INVALID"}),
            ("hide", {"hidden": True, "comment_id": "1"}, {"comment_id": "UNKNOWN"}),
            ("reply", {}, {"text": "REQUIRED"}),
            ("reply", {"text": "   "}, {"text": "REQUIRED"}),
            ("reply", {"text": "x" * (REPLY_LIMIT + 1)}, {"text": "TOO_LONG"}),
            ("reply", {"text": "bell\x07"}, {"text": "INVALID"}),
            ("reply", {"text": 5}, {"text": "INVALID"}),
        ]
        for action, body, fields in cases:
            response = await self.fb("POST", f"/comments/{COMMENT_ID}/{action}", body)
            with self.subTest(action=action, body=body):
                self.assert_refused(response, 422, "INVALID_FIELDS", fields)
        for raw in (b"[1]", b"{nope", b'"text"'):
            response = await self.fb("POST", f"/comments/{COMMENT_ID}/reply", raw,
                                     headers={"Content-Type": "application/json"})
            self.assert_refused(response, 422, "INVALID_REQUEST")
        self.assertEqual(self.features.calls, [])

    async def test_comment_ids_are_checked_in_the_path(self):
        for bad in ("abc", "1_2_3_4_5", "1-2", "_1"):
            response = await self.fb("POST", f"/comments/{bad}/hide", {"hidden": True})
            self.assertEqual(response.status_code, 422, bad)
        self.assertEqual(self.features.calls, [])

    async def test_a_public_reply_is_posted_once_with_the_typed_text(self):
        text = "გმადლობთ!\nმოგწერეთ პირადში."
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/reply", {"text": f"  {text} "})
        self.assertEqual(response.json(), {"comment_id": COMMENT_ID,
                                           "public_reply_id": "900000000000001_811"})
        self.assertEqual(self.features.calls, [("public_reply", COMMENT_ID, text, "boss",
                                                "127.0.0.1")])

    async def test_failures_map_to_statuses_and_carry_metas_codes_without_a_token(self):
        meta = {"http_status": 400, "code": 10, "subcode": None, "type": "OAuthException",
                "fbtrace_id": "Axyz", "message": f"Bad token {PAGE_TOKEN} for this call"}
        cases = [
            (CommentActionError("NOT_FOUND"), 404, "NOT_FOUND", None),
            (CommentActionError("NOT_CONNECTED", {"reason": "NotConnected"}), 409,
             "NOT_CONNECTED", None),
            (CommentActionError("NOT_CONNECTED", {"reason": "TokenCipherError"}), 409,
             "TOKEN_UNREADABLE", None),
            (CommentActionError("INVALID", {"field": "text"}), 422, "INVALID_FIELDS",
             {"text": "INVALID"}),
            (CommentActionError("META_REFUSED", {"meta": meta, "maybe_applied": False}), 502,
             "META_REFUSED", {"meta": {**meta, "message": "Bad token [redacted] for this call"},
                              "maybe_applied": False}),
            (CommentActionError("TOKEN_DEAD", {"meta": {**meta, "code": 190},
                                               "maybe_applied": False}), 409, "TOKEN_DEAD",
             {"meta": {**meta, "code": 190, "message": "Bad token [redacted] for this call"},
              "maybe_applied": False}),
        ]
        for error, status, code, fields in cases:
            self.features.action_error = error
            for action, body in (("hide", {"hidden": True}), ("reply", {"text": "Hello"})):
                response = await self.fb("POST", f"/comments/{COMMENT_ID}/{action}", body)
                with self.subTest(code=error.code, action=action):
                    self.assert_refused(response, status, code, fields)
                    self.assertNotIn(PAGE_TOKEN, response.text)

    async def test_a_lost_answer_says_the_reply_may_be_posted_and_is_not_repeated(self):
        meta = {"http_status": 0, "code": None, "subcode": None, "type": "network",
                "fbtrace_id": "", "message": "read_timeout"}
        self.features.action_error = CommentActionError("NETWORK", {"meta": meta,
                                                                    "maybe_applied": True})
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/reply", {"text": "Hello"})
        self.assert_refused(response, 502, "NETWORK", {"meta": meta, "maybe_applied": True})
        self.assertEqual(self.features.names(), ["public_reply"])


class PageTests(ActivityCase):
    async def test_page_stats_read_every_metric_on_one_connection(self):
        response = await self.fb("GET", "/page-stats", params={"days": 7})
        body = response.json()
        self.assertEqual(body["days"], 7)
        self.assertEqual(list(body["metrics"]), list(meta_page_stats.PAGE_METRICS))
        self.assertEqual(body["metrics"]["page_media_view"][1], {"day": "2026-11-11",
                                                                "value": 80})
        self.assertEqual(body["status"]["page"]["unavailable"], ["page_post_engagements"])
        self.assertEqual(body["summary"], {
            "followers": 157, "followers_change": 7,
            "totals": {"page_media_view": 200, "page_total_media_view_unique": None,
                       "page_post_engagements": None, "page_follows": 2.5,
                       "page_daily_follows_unique": None},
            "latest": {"page_media_view": 80, "page_total_media_view_unique": None,
                       "page_post_engagements": None, "page_follows": 2.5,
                       "page_daily_follows_unique": None}})
        handles = {id(call[1]) for call in self.features.calls}
        self.assertEqual((len(handles), self.store.db.opened), (1, 1))
        self.assertEqual({call[-1] for call in self.features.calls
                          if call[0] != "insights_status"}, {7})

    async def test_only_the_offered_ranges_are_accepted(self):
        for days in (30, 90):
            self.assertEqual((await self.fb("GET", "/page-stats",
                                            params={"days": days})).json()["days"], days)
        for days in (1, 14, 60, 365):
            for path in ("/page-stats", "/posts", "/ads", "/ads/daily"):
                response = await self.fb("GET", path, params={"days": days})
                with self.subTest(path=path, days=days):
                    self.assert_refused(response, 422, "INVALID_FIELDS", {"days": "INVALID"})
        for days in (0, 401, "week"):
            response = await self.fb("GET", "/page-stats", params={"days": days})
            self.assertEqual(response.status_code, 422, days)

    async def test_a_single_day_has_no_change_and_no_days_have_no_figures(self):
        self.features.followers = [{"day": "2026-11-11", "followers": 157}]
        summary = (await self.fb("GET", "/page-stats")).json()["summary"]
        self.assertEqual((summary["followers"], summary["followers_change"]), (157, None))
        self.features.followers, self.features.metrics = [], {}
        summary = (await self.fb("GET", "/page-stats")).json()["summary"]
        self.assertEqual((summary["followers"], summary["followers_change"]), (None, None))
        self.assertEqual(set(summary["totals"].values()) | set(summary["latest"].values()),
                         {None})

    async def test_posts_point_at_their_cached_pictures_and_keep_only_facebook_links(self):
        response = await self.fb("GET", "/posts", params={"days": 30, "limit": 5})
        first, second = response.json()["posts"]
        self.assertEqual(first["image_url"], f"{PREFIX}/images/{KEY}")
        self.assertEqual(first["permalink"], f"https://www.facebook.com/{PAGE_ID}/posts/1")
        self.assertEqual((second["image_url"], second["permalink"]), (None, ""))
        self.assertEqual(self.features.calls[0][2:], (30, 5))
        for limit in (0, 51):
            response = await self.fb("GET", "/posts", params={"limit": limit})
            self.assertEqual(response.status_code, 422, limit)


class ImageTests(ActivityCase):
    async def test_a_picture_loads_like_an_img_tag_with_safe_headers(self):
        # A browser's <img> request: the session cookie, no CSRF header, no Origin.
        response = await self.client.get(f"{PREFIX}/images/{KEY}",
                                         headers={"Cookie": self.admin["Cookie"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])

    async def test_unknown_and_unexpected_pictures_are_not_found(self):
        self.features.images = {KEY: ("image/svg+xml", b"<svg onload=alert(1)/>"),
                                "cd" * 32: ("text/html", b"<script></script>")}
        for key in (KEY, "cd" * 32, "ef" * 32):
            response = await self.fb("GET", f"/images/{key}")
            with self.subTest(key=key):
                self.assert_refused(response, 404, "NOT_FOUND")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_keys_are_checked_before_the_cache_is_read(self):
        for key in ("AB" * 32, "ab" * 31, "ab" * 33, "zz" * 32):
            response = await self.fb("GET", f"/images/{key}")
            self.assertEqual(response.status_code, 422, key)
        self.assertEqual(self.features.calls, [])

    async def test_an_employee_cannot_load_a_picture(self):
        response = await self.client.get(f"{PREFIX}/images/{KEY}",
                                         headers={"Cookie": self.employee["Cookie"]})
        self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual(self.features.calls, [])


class AdsTests(ActivityCase):
    async def test_ads_best_ads_and_accounts_come_from_one_connection(self):
        body = (await self.fb("GET", "/ads", params={"days": 90})).json()
        self.assertEqual(body["days"], 90)
        self.assertEqual([row["ad_id"] for row in body["ads"]],
                         ["6000000000001", "6000000000002"])
        self.assertEqual(body["ads"][0]["image_url"], f"{PREFIX}/images/{KEY}")
        self.assertTrue(body["ads"][0]["ads_manager_url"].startswith(
            "https://adsmanager.facebook.com/"))
        self.assertEqual((body["ads"][1]["image_url"], body["ads"][1]["ads_manager_url"]),
                         (None, ""))
        self.assertEqual([row["ad_id"] for row in body["best"]], ["6000000000001"])
        self.assertEqual(body["accounts"][0]["garage_ads"], 2)
        self.assertEqual((body["min_spend"], body["currencies"]), (MIN_SPEND, ["GEL", "USD"]))
        self.assertEqual(self.features.names(), ["ad_results", "best_ads", "ad_accounts"])
        self.assertEqual(self.features.calls[1][2:], (90, MIN_SPEND, 10))
        self.assertEqual((len({id(call[1]) for call in self.features.calls}),
                          self.store.db.opened), (1, 1))

    async def test_daily_spend_keeps_each_currency(self):
        body = (await self.fb("GET", "/ads/daily", params={"days": 7})).json()
        self.assertEqual(body, {"days": 7, "daily": self.features.daily})
        self.assertEqual(self.features.calls[0][2], 7)

    async def test_no_ads_yet_is_an_empty_answer(self):
        self.features.ads, self.features.best, self.features.accounts = [], [], []
        body = (await self.fb("GET", "/ads")).json()
        self.assertEqual((body["ads"], body["best"], body["accounts"], body["currencies"]),
                         ([], [], [], []))


class FakeGraph:
    """The Graph calls the comment actions make; nothing reaches Meta."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: GraphError | None = None

    def set_comment_hidden(self, page_token, comment_id, hidden, proof=True) -> dict:
        self.calls.append(("hide", page_token, comment_id, hidden))
        if self.error is not None:
            raise self.error
        return {"success": True}

    def comment_reply(self, page_token, comment_id, text, proof=True) -> dict:
        self.calls.append(("reply", page_token, comment_id, text))
        if self.error is not None:
            raise self.error
        return {"id": f"{COMMENT_ID}_reply"}


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class DatabaseRouteTests(AdminCase):
    """The routes on the real helpers and MetaStore as a NOLOGIN member of garage_site, in a
    database this class creates and drops: a query outside migration 0013's grants fails here
    as it would in production."""

    TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads", "meta_comments",
              "meta_page_daily", "meta_posts", "meta_images", "meta_ad_daily", "meta_ads",
              "meta_ad_accounts", "audit_log")

    @classmethod
    def setUpClass(cls) -> None:
        suffix = uuid.uuid4().hex[:12]
        cls.admin_db = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.cluster_roles: list[str] = []
        cls.database = "meta_activity_" + suffix
        cls.admin_db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls._drop_everything)
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.database
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.site_role = f"meta_activity_site_{suffix}"
        cls.admin_db.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(cls.site_role)))
        cls.cluster_roles.append(cls.site_role)
        cls.admin_db.execute(sql.SQL("GRANT garage_site TO {}").format(
            sql.Identifier(cls.site_role)))
        cls.admin_db.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(
            sql.Identifier(cls.site_role)))

    @classmethod
    def _drop_everything(cls) -> None:
        cls.admin_db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(cls.database)))
        for name in reversed(cls.cluster_roles):
            cls.admin_db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin_db.close()

    async def asyncSetUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in self.TABLES)))
            self.seed(connection)
        self.store = MetaStore(RoleDatabase(self.url, self.site_role),
                               TokenCipher(SETTINGS.token_key))
        self.graph = FakeGraph()
        await super().asyncSetUp()
        self.store.save_connection(PAGE_ID, "Garage", APP_ID, {"page": {
            "token": PAGE_TOKEN, "subject_id": PAGE_ID, "subject_name": "Garage",
            "scopes": ["pages_manage_engagement"], "expires_at": None,
            "data_access_expires_at": None}}, "test")

    def serve(self, settings=None, **kwargs):
        kwargs.setdefault("meta_activity", {"store": self.store, "graph": self.graph,
                                            "meta_settings": SETTINGS})
        return super().serve(settings, **kwargs)

    async def fb(self, method: str, path: str, body=None, **kwargs):
        response = await self.call(method, PREFIX + path, body, **kwargs)
        self.assertNotIn(PAGE_TOKEN, response.text)
        return response

    @staticmethod
    def seed(connection) -> None:
        today = datetime.now(UTC)
        day = today.date()
        connection.execute("INSERT INTO garage.meta_images (image_key, content_type, data) "
                           "VALUES (%s, 'image/png', %s)", (KEY, PNG))
        connection.execute(
            "INSERT INTO garage.meta_posts (post_id, created_time, message, permalink, "
            "image_key, reactions, comments, shares, metrics) VALUES "
            "(%s, %s, 'ახალი ფილტრები', %s, %s, 9, 3, 1, %s), "
            "(%s, %s, 'ძველი', '', NULL, 1, 0, 0, '{}')",
            (POST_ID, today - timedelta(days=1),
             f"https://www.facebook.com/{PAGE_ID}/posts/900000000000001", KEY,
             Jsonb({"post_media_view": 400, "post_total_media_view_unique": 310}),
             f"{PAGE_ID}_900000000000002", today - timedelta(days=3)))
        for back, followers, views in ((2, 150, 120), (1, 152, 80), (0, 157, 60)):
            connection.execute(
                "INSERT INTO garage.meta_page_daily (day, followers, metrics) VALUES (%s, %s, %s)",
                (day - timedelta(days=back), followers, Jsonb({"page_media_view": views,
                                                               "fan_count": followers})))
        connection.execute(
            "INSERT INTO garage.meta_comments (comment_id, post_id, author_id, author_name, "
            "body, created_time, from_page, source, reply_status) VALUES "
            "(%s, %s, '4400000002', 'Giorgi', 'F30 ფილტრი?', %s, FALSE, 'webhook', 'sent'), "
            "(%s, %s, '4400000003', 'Nino', 'ფასი?', %s, FALSE, 'backfill', ''), "
            "(%s, %s, %s, 'Garage', 'გამოგიგზავნეთ', %s, TRUE, 'webhook', '')",
            (COMMENT_ID, POST_ID, today - timedelta(hours=3),
             "900000000000001_800000000000002", POST_ID, today - timedelta(hours=2),
             "900000000000001_800000000000003", POST_ID, PAGE_ID, today - timedelta(hours=1)))
        connection.execute(
            "INSERT INTO garage.meta_ad_accounts (account_id, name, currency, timezone_name, "
            "account_status, garage_ads) VALUES ('1383535660550763', 'Owner', 'GEL', "
            "'Asia/Tbilisi', 1, 1)")
        connection.execute(
            "INSERT INTO garage.meta_ads (ad_id, account_id, name, status, effective_status, "
            "campaign_name, body, image_key) VALUES ('6000000000001', '1383535660550763', "
            "'Oil filters', 'ACTIVE', 'ACTIVE', 'Garage November', 'ზეთის შეცვლა', %s)", (KEY,))
        connection.execute(
            "INSERT INTO garage.meta_ad_daily (ad_id, day, spend, impressions, reach, clicks, "
            "messaging_started) VALUES ('6000000000001', %s, 12.50, 2000, 1500, 40, 4), "
            "('6000000000001', %s, 12.00, 2000, 1600, 40, 3)",
            (day - timedelta(days=1), day))
        connection.execute(
            "INSERT INTO garage.messenger_threads (psid, page_id, source, source_ad_id, "
            "last_bot_at) VALUES ('7000000000001234', %s, 'ads', '6000000000001', now())",
            (PAGE_ID,))
        connection.execute(
            "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload, processed_at, "
            "attempts, outcome) VALUES (%s, 'messages', %s, '{}', now(), 1, 'stored'), "
            "(%s, 'feed', %s, '{}', NULL, 0, '')",
            ("msg:" + "1" * 64, PAGE_ID, "chg:" + "2" * 64, PAGE_ID))

    def audits(self) -> list[tuple]:
        with psycopg.connect(self.url) as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT action, target, detail, host(client_ip) FROM garage.audit_log "
                "WHERE action LIKE 'messenger.comment%' ORDER BY audit_id").fetchall()]

    async def test_every_read_stays_within_the_site_logins_grants(self):
        events = (await self.fb("GET", "/events")).json()["events"]
        self.assertEqual([(row["field"], row["state"]) for row in events],
                         [("feed", "waiting"), ("messages", "done")])
        listing = (await self.fb("GET", "/comments")).json()
        self.assertEqual([row["author_name"] for row in listing["comments"]], ["Nino", "Giorgi"])
        self.assertEqual((listing["total"], listing["unreplied"], listing["has_more"]),
                         (2, 1, False))
        self.assertTrue(listing["comments"][1]["link"].startswith("https://www.facebook.com/"))
        unreplied = (await self.fb("GET", "/comments", params={"unreplied": "true"})).json()
        self.assertEqual([row["author_name"] for row in unreplied["comments"]], ["Nino"])
        stats = (await self.fb("GET", "/page-stats", params={"days": 7})).json()
        self.assertEqual([row["followers"] for row in stats["followers"]], [150, 152, 157])
        self.assertEqual(stats["summary"]["followers_change"], 7)
        self.assertEqual(stats["summary"]["totals"]["page_media_view"], 260)
        self.assertEqual(stats["status"]["page"]["followers"], 157)
        posts = (await self.fb("GET", "/posts")).json()["posts"]
        self.assertEqual([row["post_id"] for row in posts],
                         [POST_ID, f"{PAGE_ID}_900000000000002"])
        self.assertEqual((posts[0]["views"], posts[0]["image_url"]),
                         (400, f"{PREFIX}/images/{KEY}"))
        picture = await self.client.get(posts[0]["image_url"],
                                        headers={"Cookie": self.admin["Cookie"]})
        self.assertEqual((picture.status_code, picture.headers["content-type"],
                          picture.content), (200, "image/png", PNG))
        ads = (await self.fb("GET", "/ads", params={"days": 7})).json()
        (only,) = ads["ads"]
        self.assertEqual((only["spend"], only["messaging_started"], only["cost_per_chat"],
                          only["chats"], only["chats_handled"], only["currency"]),
                         (24.5, 7, 3.5, 1, 1, "GEL"))
        self.assertEqual([row["ad_id"] for row in ads["best"]], ["6000000000001"])
        daily = (await self.fb("GET", "/ads/daily", params={"days": 7})).json()["daily"]
        self.assertEqual([(row["spend"], row["messaging_started"]) for row in daily],
                         [(12.5, 4), (12.0, 3)])

    async def test_hiding_and_a_public_reply_change_facebook_then_the_row_and_are_audited(self):
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/hide", {"hidden": True})
        self.assertEqual(response.json(), {"comment_id": COMMENT_ID, "hidden": True})
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/reply", {"text": "გმადლობთ!"})
        self.assertEqual(response.json(), {"comment_id": COMMENT_ID,
                                           "public_reply_id": f"{COMMENT_ID}_reply"})
        self.assertEqual(self.graph.calls, [("hide", PAGE_TOKEN, COMMENT_ID, True),
                                            ("reply", PAGE_TOKEN, COMMENT_ID, "გმადლობთ!")])
        with psycopg.connect(self.url) as connection:
            row = connection.execute("SELECT is_hidden, public_reply_id FROM garage.meta_comments "
                                     "WHERE comment_id = %s", (COMMENT_ID,)).fetchone()
        self.assertEqual(tuple(row), (True, f"{COMMENT_ID}_reply"))
        self.assertEqual(self.audits(), [
            ("messenger.comment_hidden", COMMENT_ID, {}, "127.0.0.1"),
            ("messenger.comment_public_reply", COMMENT_ID, {"chars": 9, "stored": True},
             "127.0.0.1")])

    async def test_a_refusal_changes_nothing_here_and_is_audited(self):
        self.graph.error = GraphError(400, 100, None, "OAuthException", "Unsupported request")
        response = await self.fb("POST", f"/comments/{COMMENT_ID}/hide", {"hidden": True})
        self.assert_refused(response, 502, "META_REFUSED", {
            "meta": {"http_status": 400, "code": 100, "subcode": None, "type": "OAuthException",
                     "fbtrace_id": "", "message": "Unsupported request"},
            "maybe_applied": False})
        with psycopg.connect(self.url) as connection:
            hidden = connection.execute("SELECT is_hidden FROM garage.meta_comments "
                                        "WHERE comment_id = %s", (COMMENT_ID,)).fetchone()[0]
        self.assertIs(hidden, False)
        self.assertEqual([row[0] for row in self.audits()], ["messenger.comment_action_failed"])
        # The page's own comments and unknown ids are not the dashboard's to act on.
        self.graph.error = None
        for target in ("900000000000001_800000000000003", "900000000000001_899"):
            response = await self.fb("POST", f"/comments/{target}/hide", {"hidden": True})
            self.assert_refused(response, 404, "NOT_FOUND")
        self.assertEqual(len(self.graph.calls), 1)

    async def test_unknown_pictures_are_not_found(self):
        response = await self.fb("GET", f"/images/{'cd' * 32}")
        self.assert_refused(response, 404, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
