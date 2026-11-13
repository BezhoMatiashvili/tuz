"""Page statistics, posts and the picture cache (``automotive.meta_page_stats``).

The Graph API is a fake that answers with recorded, Meta-shaped JSON; the picture host is a fake
session. Nothing leaves the process. The database tests run on a disposable PostgreSQL named by
WAREHOUSE_TEST_ADMIN_DATABASE_URL (skipped without it), as NOLOGIN roles that are members of the
real capability roles: the worker (garage_messenger) writes, the dashboard (garage_site) reads.

``FeatureDatabase``, ``FakeGraph`` and the constants here are shared with the comment and ads
tests.
"""

from __future__ import annotations

import copy
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import requests
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from requests.structures import CaseInsensitiveDict

from automotive.meta_config import MetaSettings
from automotive.meta_context import MetaContext, utc_now
from automotive.meta_crypto import TokenCipher
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphError
from automotive.meta_page_stats import (
    PAGE_METRICS,
    ImageFetcher,
    followers_series,
    image,
    image_key,
    insights_status,
    lifetime_values,
    metric_series,
    page_days,
    parse_post,
    read_insights,
    refuse_url,
    sniff,
    sync_page,
    sync_posts,
    top_posts,
)
from automotive.meta_store import MetaStore
from automotive.meta_sync import SyncCount, tbilisi_today
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
PAGE, APP, USER_ID = "61595318832800", "1234567890", "4400000001"
PAGE_TOKEN, USER_TOKEN = "EAApageFixtureToken000003", "EAAlongLivedFixtureToken0002"
SETTINGS = MetaSettings(app_id=APP, app_secret="s" * 32, verify_token="v" * 40,
                        token_key=b"k" * 32, public_url="https://hook.example")
ADMIN = {"user_id": 7, "username": "boss"}
TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads", "messenger_messages",
          "messenger_replies", "meta_comments", "meta_page_daily", "meta_posts", "meta_images",
          "meta_ad_daily", "meta_ads", "meta_ad_accounts", "meta_sync_runs",
          "meta_job_requests", "meta_forgotten", "audit_log")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 200
WEBP = b"RIFF\x00\x01\x00\x00WEBPVP8 " + b"\x00" * 200
PICTURE = "https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg?oh=signed&oe=67"


def refused(code: int, subcode: int | None = None, status: int = 400,
            kind: str = "OAuthException") -> GraphError:
    return GraphError(status, code, subcode, kind, f"(#{code}) refused", "trace")


def meta_time(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


class WriteRefused(AssertionError):
    """A test's Graph fake was asked to write when it must only read."""


class FakeGraph:
    """Graph with recorded answers.

    ``answers`` maps a path to a dict, a list (for ``paged``), a GraphError to raise, or a
    function of the request's params returning one of those. Every call is recorded as
    ``(method, path, params)``. Writes (private and public replies, hiding) go to ``replies``,
    ``public`` and ``hidden`` and return ``reply_answer``/``public_answer``, or raise what
    ``write_errors`` names; with ``read_only`` any write raises WriteRefused."""

    def __init__(self, *, read_only: bool = False) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.read_only = read_only
        self.replies: list[tuple] = []
        self.public: list[tuple] = []
        self.hidden: list[tuple] = []
        self.write_errors: dict[str, Exception] = {}
        self.reply_answer = {"recipient_id": "7000000000001234", "message_id": "m_fake_0001"}
        self.public_answer = {"id": "111_909"}

    def _answer(self, method: str, path: str, params: Any) -> Any:
        values = dict(params or {})
        self.calls.append((method, path, values))
        answer = self.answers.get(path)
        if callable(answer):
            answer = answer(values)
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    def get(self, path, token, params=None, proof=True):
        answer = self._answer("GET", path, params)
        return answer if isinstance(answer, dict) else {}

    def paged(self, path, token, params=None, max_pages=10, *, proof=True):
        answer = self._answer("GET", path, params)
        return answer if isinstance(answer, list) else []

    def _write(self, name: str, record: list, entry: tuple, answer: dict) -> dict:
        self.calls.append(("POST", name, {}))
        if self.read_only:
            raise WriteRefused(name)
        if name in self.write_errors:
            raise self.write_errors[name]
        record.append(entry)
        return dict(answer)

    def private_reply(self, page_id, page_token, comment_id, message, proof=True):
        return self._write("private_reply", self.replies, (page_id, comment_id, message),
                           self.reply_answer)

    def comment_reply(self, page_token, comment_id, text, proof=True):
        return self._write("comment_reply", self.public, (comment_id, text), self.public_answer)

    def set_comment_hidden(self, page_token, comment_id, hidden, proof=True):
        return self._write("set_comment_hidden", self.hidden, (comment_id, hidden),
                           {"success": True})

    def post(self, *args, **kwargs):
        raise WriteRefused("post")

    def delete(self, *args, **kwargs):
        raise WriteRefused("delete")

    def send_message(self, *args, **kwargs):
        raise WriteRefused("send_message")

    def sender_action(self, *args, **kwargs):
        raise WriteRefused("sender_action")

    def methods(self) -> set[str]:
        return {method for method, _, _ in self.calls}

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


class RoleDatabase(MetaDatabase):
    """Every connection runs as ``role`` (SET ROLE, committed at once)."""

    def __init__(self, url: str, role: str) -> None:
        super().__init__(url, application_name="meta-features-test")
        self.role = role

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        connection = super().connect(autocommit=autocommit)
        try:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.role)))
            if not autocommit:
                connection.commit()
        except psycopg.Error:
            connection.close()
            raise
        return connection


class FeatureDatabase:
    """A database of its own with every migration, and a NOLOGIN role per login under test
    (worker, site), each a member of its real capability role only. Dropped by ``close``."""

    def __init__(self, label: str) -> None:
        self.suffix = uuid.uuid4().hex[:12]
        self.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        self.name = f"{label}_{self.suffix}"
        self.roles: dict[str, str] = {}
        self.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = self.name
        self.url = make_conninfo(**info)
        with psycopg.connect(self.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        for key, capability in (("worker", "garage_messenger"), ("site", "garage_site")):
            role = f"feat_{key}_{self.suffix}"
            self.admin.execute(sql.SQL(
                "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            ).format(sql.Identifier(role)))
            self.roles[key] = role
            self.admin.execute(sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(capability), sql.Identifier(role)))
            self.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        self.cipher = TokenCipher(os.urandom(32))

    def close(self) -> None:
        self.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(self.name)))
        for role in self.roles.values():
            self.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        self.admin.close()

    def store(self, key: str) -> MetaStore:
        return MetaStore(RoleDatabase(self.url, self.roles[key]), self.cipher)

    def context(self, key: str, graph: Any, now: Any = utc_now) -> MetaContext:
        return MetaContext(SETTINGS, self.store(key), graph, self.cipher, now=now)

    def reset(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")

    def connect_page(self, *, user: bool = True) -> None:
        """Store the page and its tokens the way the dashboard's Connect step does."""
        tokens = {"page": {"token": PAGE_TOKEN, "subject_id": PAGE, "subject_name": "Garage",
                           "scopes": ["pages_messaging"], "expires_at": None,
                           "data_access_expires_at": None}}
        if user:
            tokens["user"] = {"token": USER_TOKEN, "subject_id": USER_ID,
                              "subject_name": "Owner", "scopes": ["ads_read", "read_insights"],
                              "expires_at": None, "data_access_expires_at": None}
        self.store("site").save_connection(PAGE, "Garage • ფარეხი 1886", APP, tokens, ADMIN)

    def rows(self, statement: str, parameters: Any = ()) -> list[dict]:
        with psycopg.connect(self.url, row_factory=dict_row) as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else []


# -- the picture host --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int = 200, headers: dict | None = None, body: bytes = b"") -> None:
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.body = body
        self.read = 0
        self.closed = False

    def iter_content(self, size: int):
        for start in range(0, len(self.body), size):
            self.read += len(self.body[start:start + size])
            yield self.body[start:start + size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        answer = self.answers.get(url) or FakeResponse(404)
        if isinstance(answer, Exception):
            raise answer
        return answer


def picture(body: bytes = JPEG, kind: str = "image/jpeg", **headers: str) -> FakeResponse:
    return FakeResponse(200, {"Content-Type": kind, **headers}, body)


class ImageFetcherTests(unittest.TestCase):
    def fetch(self, url: str, answers: dict[str, Any] | None = None,
              **options: Any) -> tuple[Any, FakeSession]:
        session = FakeSession(answers or {})
        return ImageFetcher(session, **options).fetch(url), session

    def test_only_https_on_metas_own_hosts_is_ever_requested(self):
        for url in ("http://scontent.xx.fbcdn.net/a.jpg", "https://example.com/a.jpg",
                    "https://evilfbcdn.net/a.jpg", "https://fbcdn.net.evil.com/a.jpg",
                    "https://user:pass@scontent.fbcdn.net/a.jpg",
                    "https://scontent.fbcdn.net:8443/a.jpg", "ftp://scontent.fbcdn.net/a.jpg",
                    "https://127.0.0.1/a.jpg", "", None, "https://[::1/a.jpg"):
            got, session = self.fetch(url)
            self.assertIsNone(got, url)
            self.assertEqual(session.requests, [], url)
        self.assertEqual(refuse_url("https://scontent.xx.fbcdn.net/a.jpg"), "")
        self.assertEqual(refuse_url("https://lookaside.facebook.com/a.jpg"), "")
        self.assertEqual(refuse_url("https://fbcdn.net./a.jpg"), "")

    def test_a_jpeg_is_kept_and_nothing_but_the_address_is_sent(self):
        got, session = self.fetch(PICTURE, {PICTURE: picture()})
        self.assertEqual(got, ("image/jpeg", JPEG))
        (url, options), = session.requests
        self.assertEqual(url, PICTURE)
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertNotIn("params", options)
        self.assertNotIn("auth", options)
        self.assertNotIn("headers", options)

    def test_each_type_is_checked_against_its_first_bytes(self):
        for body, kind in ((PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp"),
                           (JPEG, "image/jpeg; charset=binary")):
            got, _ = self.fetch(PICTURE, {PICTURE: picture(body, kind)})
            self.assertEqual(got, (kind.split(";")[0], body))
        for body, kind in ((PNG, "image/jpeg"), (b"<html>hello</html>" * 20, "image/png"),
                           (JPEG, "text/html"), (JPEG, "image/svg+xml"), (b"", "image/jpeg")):
            got, _ = self.fetch(PICTURE, {PICTURE: picture(body, kind)})
            self.assertIsNone(got, kind)
        self.assertEqual((sniff(JPEG), sniff(b"GIF87a..."), sniff(b"\x00\x01")),
                         ("image/jpeg", "image/gif", ""))

    def test_more_than_the_cap_is_never_kept(self):
        declared = picture(JPEG, **{"Content-Length": str(2 * 1024 * 1024)})
        got, _ = self.fetch(PICTURE, {PICTURE: declared})
        self.assertIsNone(got)
        self.assertEqual(declared.read, 0)  # refused on the header, before any byte
        streamed = picture(JPEG + b"\x00" * 5000)
        got, _ = self.fetch(PICTURE, {PICTURE: streamed}, max_bytes=1000)
        self.assertIsNone(got)
        self.assertLessEqual(streamed.read, 1000 + 65536)
        self.assertTrue(streamed.closed)

    def test_redirects_stay_on_metas_hosts(self):
        moved = "https://scontent-b.xx.fbcdn.net/v/2.jpg"
        got, session = self.fetch(PICTURE, {
            PICTURE: FakeResponse(302, {"Location": moved}), moved: picture()})
        self.assertEqual(got, ("image/jpeg", JPEG))
        self.assertEqual([url for url, _ in session.requests], [PICTURE, moved])
        for target in ("https://evil.example/x.jpg", "http://scontent.fbcdn.net/x.jpg",
                       "//evil.example/x.jpg"):
            got, session = self.fetch(PICTURE, {PICTURE: FakeResponse(301, {"Location": target})})
            self.assertIsNone(got, target)
            self.assertEqual([url for url, _ in session.requests], [PICTURE], target)
        loop = FakeResponse(302, {"Location": PICTURE})
        got, session = self.fetch(PICTURE, {PICTURE: loop})
        self.assertIsNone(got)
        self.assertEqual(len(session.requests), 4)

    def test_errors_and_other_answers_are_skipped(self):
        for answer in (FakeResponse(404, {"Content-Type": "image/jpeg"}, JPEG),
                       FakeResponse(500), requests.ConnectionError("down"),
                       requests.ReadTimeout("slow")):
            got, _ = self.fetch(PICTURE, {PICTURE: answer})
            self.assertIsNone(got)
        self.assertEqual(image_key("post:1_2"), image_key("post:1_2"))
        self.assertNotEqual(image_key("post:1_2"), image_key("post:1_3"))


# -- insights --------------------------------------------------------------------------------


def day_values(name: str, values: list[tuple[datetime, Any]]) -> dict:
    return {"name": name, "period": "day", "title": name, "description": "",
            "id": f"{PAGE}/insights/{name}/day",
            "values": [{"value": value, "end_time": meta_time(end)} for end, value in values]}


def insights_answer(known: dict[str, list[tuple[datetime, Any]]], *,
                    invalid: set[str] = frozenset(), refusal: GraphError | None = None):
    """Meta's insights edge: one entry per known metric; a request naming an invalid metric
    fails whole with code 100, as Meta does."""

    def answer(params: dict) -> Any:
        if refusal is not None:
            return refusal
        names = params["metric"].split(",")
        if any(name in invalid for name in names):
            return refused(100, None, kind="OAuthException")
        return {"data": [day_values(name, known[name]) for name in names if name in known],
                "paging": {"previous": "https://graph.facebook.com/x?since=1",
                           "next": "https://graph.facebook.com/x?until=2"}}

    return answer


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph(read_only=True)
        self.end = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)

    def test_all_metrics_in_one_request(self):
        self.graph.answers[f"{PAGE}/insights"] = insights_answer(
            {name: [(self.end, 5)] for name in PAGE_METRICS})
        got = read_insights(self.graph, PAGE_TOKEN, PAGE, PAGE_METRICS, {"period": "day"})
        self.assertEqual((len(got.data), got.unavailable, got.refused), (5, [], None))
        (_, _, params), = self.graph.calls
        self.assertEqual(params, {"period": "day", "metric": ",".join(PAGE_METRICS)})

    def test_an_invalid_metric_costs_only_itself(self):
        self.graph.answers[f"{PAGE}/insights"] = insights_answer(
            {"page_media_view": [(self.end, 40)], "page_follows": [(self.end, 157)]},
            invalid={"page_total_media_view_unique"})
        got = read_insights(self.graph, PAGE_TOKEN, PAGE,
                            ["page_media_view", "page_total_media_view_unique", "page_follows"])
        self.assertEqual(sorted(entry["name"] for entry in got.data),
                         ["page_follows", "page_media_view"])
        self.assertEqual((got.unavailable, got.refused), (["page_total_media_view_unique"], None))
        self.assertEqual(len(self.graph.calls), 4)  # the whole request, then one per metric

    def test_a_refusal_stops_at_once_and_is_recorded_by_code(self):
        for code in (10, 200, 230):
            graph = FakeGraph(read_only=True)
            graph.answers[f"{PAGE}/insights"] = insights_answer({}, refusal=refused(code, 1234))
            got = read_insights(graph, PAGE_TOKEN, PAGE, PAGE_METRICS)
            self.assertEqual(got.refused, {"code": code, "subcode": 1234,
                                           "type": "OAuthException"})
            self.assertEqual((got.data, got.unavailable, len(graph.calls)), ([], [], 1))

    def test_other_errors_are_raised(self):
        self.graph.answers[f"{PAGE}/insights"] = GraphError(500, 2, None, "OAuthException")
        with self.assertRaises(GraphError):
            read_insights(self.graph, PAGE_TOKEN, PAGE, PAGE_METRICS)

    def test_values_go_to_the_day_they_count_and_plain_numbers_only(self):
        data = [day_values("page_media_view", [
            (datetime(2026, 11, 10, 8, 0, tzinfo=UTC), 12),
            (datetime(2026, 11, 11, 7, 0, tzinfo=UTC), 15.0),
            (datetime(2026, 11, 12, 8, 0, tzinfo=UTC), {"is_from_ads": 3}),
            (datetime(2026, 11, 13, 8, 0, tzinfo=UTC), True)])]
        days = page_days(data)
        self.assertEqual({day.isoformat(): values for day, values in days.items()},
                         {"2026-11-09": {"page_media_view": 12},
                          "2026-11-10": {"page_media_view": 15}})
        self.assertEqual(lifetime_values([{"name": "post_media_view", "period": "lifetime",
                                           "values": [{"value": 3}, {"value": 41}]}]),
                         {"post_media_view": 41})

    def test_a_post_is_read_defensively(self):
        post = parse_post({
            "id": f"{PAGE}_555", "created_time": "2026-11-10T09:15:00+0000",
            "message": "BMW F30 ხუნდები\x00 ახალი", "full_picture": PICTURE,
            "permalink_url": "https://www.facebook.com/61595318832800/posts/555",
            "shares": {"count": 2}, "reactions": {"data": [], "summary": {"total_count": 9}},
            "comments": {"data": [], "summary": {"total_count": 4, "can_comment": True}}})
        self.assertEqual((post["post_id"], post["message"], post["reactions"], post["comments"],
                          post["shares"], post["picture_url"]),
                         (f"{PAGE}_555", "BMW F30 ხუნდები ახალი", 9, 4, 2, PICTURE))
        self.assertEqual(parse_post({"id": "1_2", "created_time": "2026-11-10T09:15:00+0000",
                                     "permalink_url": "javascript:alert(1)"})["permalink"], "")
        for item in ({"id": "x", "created_time": "2026-11-10T09:15:00+0000"},
                     {"id": "1_2"}, None, "1_2"):
            self.assertIsNone(parse_post(item))


# -- the jobs on the database ----------------------------------------------------------------


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class PageStatsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = FeatureDatabase("meta_page_stats")
        cls.addClassCleanup(cls.db.close)

    def setUp(self) -> None:
        self.db.reset()
        self.db.connect_page()
        self.graph = FakeGraph(read_only=True)
        self.worker = self.db.context("worker", self.graph)
        self.site = self.db.store("site")
        now = datetime.now(UTC)
        self.ends = [now.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=n)
                     for n in (0, 1, 2)]
        self.graph.answers[PAGE] = {"id": PAGE, "followers_count": 157, "fan_count": 140,
                                    "name": "Garage"}

    def test_followers_and_daily_views_are_kept_and_read_by_the_dashboard(self):
        self.graph.answers[f"{PAGE}/insights"] = insights_answer({
            "page_media_view": [(end, 100 + n) for n, end in enumerate(self.ends)],
            "page_follows": [(end, 157) for end in self.ends]},
            invalid={"page_daily_follows_unique"})
        result = sync_page(self.worker)
        self.assertIsInstance(result, SyncCount)
        self.assertEqual((result.status, int(result) >= 3), ("ok", True))
        self.assertIn("unavailable=page_daily_follows_unique", result.detail)
        today = tbilisi_today(datetime.now(UTC))
        followers = followers_series(self.site, 7)
        self.assertEqual(followers[-1], {"day": today.isoformat(), "followers": 157})
        views = metric_series(self.site, "page_media_view", 30)
        self.assertEqual([point["value"] for point in views], [102, 101, 100])
        self.assertEqual(views[-1]["day"],
                         (self.ends[0] - timedelta(days=1)).date().isoformat())
        status = insights_status(self.site)
        self.assertEqual((status["page"]["followers"], status["page"]["refused"],
                          status["page"]["unavailable"], status["posts"]),
                         (157, None, ["page_daily_follows_unique"], None))
        with self.assertRaises(ValueError):
            metric_series(self.site, "_refused")
        # Only page calls, and only reads.
        self.assertEqual(self.graph.methods(), {"GET"})

    def test_a_refused_read_insights_keeps_the_followers_and_says_so(self):
        self.graph.answers[f"{PAGE}/insights"] = insights_answer({}, refusal=refused(10))
        result = sync_page(self.worker)
        self.assertIn("insights_refused code=10", result.detail)
        status = insights_status(self.site)
        self.assertEqual((status["page"]["followers"], status["page"]["refused"]["code"]),
                         (157, 10))
        # Granted later: the refusal is cleared, the numbers arrive.
        self.graph.answers[f"{PAGE}/insights"] = insights_answer(
            {"page_media_view": [(self.ends[0], 33)]})
        sync_page(self.worker)
        self.assertIsNone(insights_status(self.site)["page"]["refused"])
        self.assertEqual(metric_series(self.site, "page_media_view")[-1]["value"], 33)

    def test_a_failed_insights_read_keeps_the_followers_and_what_was_known(self):
        self.graph.answers[f"{PAGE}/insights"] = insights_answer({}, refusal=refused(10))
        sync_page(self.worker)
        self.graph.answers[PAGE] = {"id": PAGE, "followers_count": 160}
        self.graph.answers[f"{PAGE}/insights"] = GraphError(500, 1, None, "OAuthException")
        result = sync_page(self.worker)
        self.assertIn("insights_error code=1", result.detail)
        status = insights_status(self.site)["page"]
        self.assertEqual((status["followers"], status["refused"]["code"]), (160, 10))

    def test_a_dead_page_token_is_marked_and_raised(self):
        self.graph.answers[PAGE] = refused(190, 460)
        with self.assertRaises(GraphError):
            sync_page(self.worker)
        status = {row["purpose"]: row["status"] for row in self.site.token_rows()}
        self.assertEqual(status["page"], "broken")

    def test_posts_counts_views_and_pictures(self):
        created = [datetime.now(UTC) - timedelta(days=n, hours=1) for n in (1, 2, 3)]
        evil = "https://evil.example/p.jpg"
        self.graph.answers[f"{PAGE}/posts"] = {"data": [
            {"id": f"{PAGE}_1", "created_time": meta_time(created[0]), "message": "ზეთის შეცვლა",
             "full_picture": PICTURE, "permalink_url": "https://www.facebook.com/p/1",
             "shares": {"count": 1}, "reactions": {"summary": {"total_count": 10}},
             "comments": {"summary": {"total_count": 3}}},
            {"id": f"{PAGE}_2", "created_time": meta_time(created[1]), "message": "ხუნდები",
             "full_picture": evil, "reactions": {"summary": {"total_count": 2}},
             "comments": {"summary": {"total_count": 0}}},
            {"id": f"{PAGE}_3", "created_time": meta_time(created[2]),
             "reactions": {"summary": {"total_count": 0}}},
            {"id": "not-a-post"}], "paging": {"cursors": {"after": "x"}}}

        def post_insights(params: dict) -> Any:
            if "post_total_media_view_unique" in params["metric"].split(","):
                return refused(100)
            return {"data": [{"name": "post_media_view", "period": "lifetime",
                              "values": [{"value": 321}]}]}

        for number in (1, 2, 3):
            self.graph.answers[f"{PAGE}_{number}/insights"] = post_insights
        session = FakeSession({PICTURE: picture()})
        result = sync_posts(self.worker, fetcher=ImageFetcher(session))
        self.assertEqual((int(result), result.status), (3, "ok"))
        self.assertIn("unavailable=post_total_media_view_unique", result.detail)
        self.assertEqual([url for url, _ in session.requests], [PICTURE])  # never the evil one
        # The unavailable metric is asked once (then dropped); one request per later post.
        insight_calls = [path for path in self.graph.paths() if path.endswith("/insights")]
        self.assertEqual(len(insight_calls), 3 + 2)
        top = top_posts(self.site, 30, 5)
        self.assertEqual([post["post_id"] for post in top], [f"{PAGE}_1", f"{PAGE}_2",
                                                              f"{PAGE}_3"])
        first = top[0]
        self.assertEqual((first["reactions"], first["comments"], first["shares"],
                          first["engagement"], first["views"], first["viewers"]),
                         (10, 3, 1, 14, 321, None))
        self.assertEqual(first["image_key"], image_key(f"post:{PAGE}_1"))
        self.assertIsNone(top[1]["image_key"])
        self.assertEqual(image(self.site, first["image_key"]), ("image/jpeg", JPEG))
        self.assertIsNone(image(self.site, "0" * 64))
        self.assertIsNone(image(self.site, "../etc/passwd"))
        self.assertEqual(insights_status(self.site)["posts"]["unavailable"],
                         ["post_total_media_view_unique"])
        # An hour later the picture is still fresh: it is not fetched again.
        sync_posts(self.worker, fetcher=ImageFetcher(session))
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(self.graph.methods(), {"GET"})

    def test_a_refusal_on_posts_is_asked_once_and_old_numbers_stay(self):
        created = meta_time(datetime.now(UTC) - timedelta(days=1))
        self.graph.answers[f"{PAGE}/posts"] = {"data": [
            {"id": f"{PAGE}_{n}", "created_time": created} for n in (1, 2)]}
        for number in (1, 2):
            self.graph.answers[f"{PAGE}_{number}/insights"] = {"data": [
                {"name": "post_media_view", "values": [{"value": 50 + number}]},
                {"name": "post_total_media_view_unique", "values": [{"value": 40}]}]}
        sync_posts(self.worker, fetcher=ImageFetcher(FakeSession({})))
        for number in (1, 2):
            self.graph.answers[f"{PAGE}_{number}/insights"] = refused(10)
        before = len(self.graph.calls)
        result = sync_posts(self.worker, fetcher=ImageFetcher(FakeSession({})))
        self.assertIn("insights_refused code=10", result.detail)
        self.assertEqual(len(self.graph.calls) - before, 2)  # the posts, one insights call
        views = {post["post_id"]: post["views"] for post in top_posts(self.site)}
        self.assertEqual(views, {f"{PAGE}_1": 51, f"{PAGE}_2": 52})
        self.assertEqual(insights_status(self.site)["posts"]["refused"]["code"], 10)


if __name__ == "__main__":
    unittest.main()
