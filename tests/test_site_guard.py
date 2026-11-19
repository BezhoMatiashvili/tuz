"""Site guard, sign-in routes and cache policy through the assembled application.

Everything runs against in-memory doubles: no PostgreSQL, no provider, no network.
``FakeSiteRepository`` and ``SiteAppCase`` are shared with the other ``test_site_*`` files.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import itertools
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import psycopg

from automotive import site_auth, site_media
from automotive.api import create_app
from automotive.config import Settings
from automotive.site_auth import TOKEN, csrf_token, hash_password, new_token, token_digest
from automotive.site_catalog import Catalog
from automotive.site_guard import (
    CSP,
    LIMITS,
    LOOKUPS,
    WIDE,
    DailyBudget,
    SiteGuard,
    TokenBuckets,
    _aliased,
    rule_for,
)
from automotive.site_pages import Assets
from automotive.site_repository import (
    MEDIA_FIELDS,
    SERVICE_FIELDS,
    USER_FIELDS,
    Conflict,
    LastAdmin,
)

BASE_URL = "http://localhost:8767"
ORIGIN = {"Origin": BASE_URL}
PASSWORD = "test-password-1"
NEW_PASSWORD = "new-test-password-2"
STAMP = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
VIN = "1HGCM82633A004352"
IMMUTABLE = "public, max-age=31536000, immutable"


def fast_scrypt(password: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    """Replaces the 150 ms scrypt call; tests/test_site_auth.py exercises the real one."""
    return hashlib.sha256(password.encode() + salt + bytes((n_log2, r, p))).digest()


def session_of(response: httpx.Response) -> tuple[str, str]:
    """(cookie name, token) from a response that set the session cookie."""
    name, _, value = response.headers["set-cookie"].split(";")[0].partition("=")
    return name, value


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingBuckets(TokenBuckets):
    """Never limits; remembers the (category, client key) the guard computed."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[tuple] = []

    def allow(self, key: tuple, burst: float, per_minute: float) -> bool:
        self.keys.append(key)
        return True

    def allow_all(self, buckets: list[tuple[tuple, float, float]]) -> bool:
        self.keys.append(buckets[0][0])  # the client's own key, before any wider one
        return True


class FakeSiteRepository:
    """SiteRepository's methods, return shapes and failure types over dictionaries."""

    configured = True

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.sessions: dict[str, int] = {}
        self.sections: dict[str, dict] = {}
        self.services: dict[int, dict] = {}
        self.media: dict[int, dict] = {}
        self.audits: list[dict] = []
        self.calls: list[str] = []
        self.failing: set[str] = set()
        self.runs: list[dict] = []
        self.trade: dict | None = None
        self.sellers: list[dict] | None = None
        self._next = itertools.count(1)

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failing:
            raise psycopg.OperationalError("connection details withheld")

    # -- accounts ----------------------------------------------------------------------

    @staticmethod
    def _shown(row: dict) -> dict:
        return {key: value for key, value in row.items() if key != "password_hash"}

    def _store_user(self, username: str, display_name: str, role: str, password_hash: str,
                    active: bool = True) -> dict:
        user_id = next(self._next)
        self.users[user_id] = {
            "user_id": user_id, "username": username, "display_name": display_name,
            "role": role, "is_active": active, "created_at": STAMP, "updated_at": STAMP,
            "password_changed_at": STAMP, "last_login_at": None,
            "password_hash": password_hash}
        return self._shown(self.users[user_id])

    def add_user(self, username: str, role: str, password: str = PASSWORD, *,
                 active: bool = True) -> dict:
        """Test set-up helper; not part of SiteRepository."""
        return self._store_user(username, "", role, hash_password(password), active)

    def find_user(self, username: str) -> dict | None:
        self._call("find_user")
        return next((dict(row) for row in self.users.values()
                     if row["username"] == username), None)

    def user_with_hash(self, user_id: int) -> dict | None:
        self._call("user_with_hash")
        return dict(self.users[user_id]) if user_id in self.users else None

    def list_users(self) -> list[dict]:
        self._call("list_users")
        return sorted((self._shown(row) for row in self.users.values()),
                      key=lambda row: row["username"])

    def create_user(self, username: str, display_name: str, role: str,
                    password_hash: str) -> dict:
        self._call("create_user")
        if any(row["username"] == username for row in self.users.values()):
            raise Conflict("username")
        return self._store_user(username, display_name, role, password_hash)

    def update_user(self, user_id: int, fields: dict[str, Any]) -> dict | None:
        self._call("update_user")
        admins = sorted(key for key, row in self.users.items()
                        if row["role"] == "admin" and row["is_active"])
        loses_admin = (fields.get("role", "admin") != "admin"
                       or fields.get("is_active", True) is False)
        if loses_admin and admins == [user_id]:
            raise LastAdmin("last active admin")
        row = self.users.get(user_id)
        if row is None:
            return None
        row.update({key: fields[key] for key in USER_FIELDS if key in fields})
        if fields.get("is_active") is False or "role" in fields:
            self._end_sessions(user_id)
        return self._shown(row)

    def set_password(self, user_id: int, password_hash: str,
                     keep_session: str | None = None) -> bool:
        self._call("set_password")
        if user_id not in self.users:
            return False
        self.users[user_id]["password_hash"] = password_hash
        self._end_sessions(user_id, keep_session)
        return True

    # -- sessions ----------------------------------------------------------------------

    def _end_sessions(self, user_id: int, keep: str | None = None) -> int:
        ended = [digest for digest, owner in self.sessions.items()
                 if owner == user_id and digest != keep]
        for digest in ended:
            del self.sessions[digest]
        return len(ended)

    def create_session(self, token_hash: str, user_id: int, lifetime_seconds: int) -> None:
        self._call("create_session")
        self.sessions[token_hash] = user_id

    def resolve_session(self, token_hash: str, idle_seconds: int) -> dict | None:
        self._call("resolve_session")
        row = self.users.get(self.sessions.get(token_hash, 0))
        if not row or not row["is_active"]:
            return None
        return {key: row[key] for key in ("user_id", "username", "display_name", "role")}

    def delete_session(self, token_hash: str) -> None:
        self._call("delete_session")
        self.sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id: int) -> int:
        self._call("delete_user_sessions")
        return self._end_sessions(user_id)

    # -- content -----------------------------------------------------------------------

    def load_content(self) -> dict:
        self._call("load_content")
        return {
            "sections": copy.deepcopy(self.sections),
            "services": sorted((dict(row) for row in self.services.values()),
                               key=lambda row: (row["position"], row["service_id"])),
            "media": sorted((self._media_row(row) for row in self.media.values()),
                            key=lambda row: (row["position"], row["media_id"]))}

    def save_section(self, key: str, document: dict, expected_version: int,
                     actor_id: int | None) -> int:
        self._call("save_section")
        current = self.sections.get(key)
        if current is not None and current["version"] != expected_version:
            raise Conflict("version")
        version = current["version"] + 1 if current else 1
        self.sections[key] = {"section_key": key, "document": copy.deepcopy(document),
                              "version": version, "updated_at": STAMP, "updated_by": actor_id}
        return version

    def create_service(self, fields: dict[str, Any]) -> dict:
        self._call("create_service")
        service_id = next(self._next)
        position = max((row["position"] for row in self.services.values()), default=0) + 1
        self.services[service_id] = {
            "service_id": service_id, "icon": "wrench", "title_ka": "", "title_en": "",
            "summary_ka": "", "summary_en": "", "price_from": None, "position": position,
            "is_published": True, "version": 1, "updated_at": STAMP,
            **{key: fields[key] for key in SERVICE_FIELDS if key in fields}}
        return dict(self.services[service_id])

    def update_service(self, service_id: int, fields: dict[str, Any],
                       expected_version: int) -> dict | None:
        self._call("update_service")
        row = self.services.get(service_id)
        if row is None:
            return None
        if row["version"] != expected_version:
            raise Conflict("version")
        row.update({key: fields[key] for key in SERVICE_FIELDS if key in fields})
        row["version"] += 1
        return dict(row)

    def delete_service(self, service_id: int) -> bool:
        self._call("delete_service")
        return self.services.pop(service_id, None) is not None

    def reorder(self, table: str, identifiers: list[int]) -> None:
        self._call("reorder")
        rows = {"services": self.services, "media": self.media}[table]
        for position, identifier in enumerate(identifiers, start=1):
            if identifier in rows:
                rows[identifier]["position"] = position

    # -- media -------------------------------------------------------------------------

    @staticmethod
    def _media_row(row: dict) -> dict:
        return {**{key: value for key, value in row.items() if key not in ("data", "created_by")},
                "byte_size": len(row["data"])}

    def add_media(self, *, sha256: str, content_type: str, data: bytes, width: int,
                  height: int, alt_ka: str = "", alt_en: str = "", published: bool = False,
                  actor_id: int | None = None) -> dict:
        self._call("add_media")
        row = next((row for row in self.media.values() if row["sha256"] == sha256), None)
        if row is None:
            media_id = next(self._next)
            position = max((row["position"] for row in self.media.values()), default=0) + 1
            row = self.media[media_id] = {
                "media_id": media_id, "sha256": sha256, "content_type": content_type,
                "data": bytes(data), "width": width, "height": height, "alt_ka": alt_ka,
                "alt_en": alt_en, "position": position, "is_published": published,
                "created_at": STAMP, "created_by": actor_id}
        return self._media_row(row)

    def update_media(self, media_id: int, fields: dict[str, Any]) -> dict | None:
        self._call("update_media")
        row = self.media.get(media_id)
        if row is None:
            return None
        row.update({key: fields[key] for key in MEDIA_FIELDS if key in fields})
        return self._media_row(row)

    def delete_media(self, media_id: int) -> bool:
        self._call("delete_media")
        return self.media.pop(media_id, None) is not None

    def media_bytes(self, sha256: str, published_only: bool = True) -> dict | None:
        self._call("media_bytes")
        row = next((row for row in self.media.values() if row["sha256"] == sha256
                    and (row["is_published"] or not published_only)), None)
        return {"content_type": row["content_type"], "data": row["data"]} if row else None

    # -- audit and reports -------------------------------------------------------------

    def audit(self, actor: dict | None, action: str, target: str = "",
              detail: dict | None = None, client_ip: str | None = None) -> None:
        self._call("audit")
        self.audits.append({
            "audit_id": len(self.audits) + 1, "occurred_at": STAMP,
            "actor_username": str((actor or {}).get("username") or ""), "action": action,
            "target": target, "detail": detail or {}, "client_ip": client_ip})

    def audit_rows(self, limit: int = 50, before: int | None = None) -> list[dict]:
        self._call("audit_rows")
        rows = [{key: value for key, value in row.items() if key != "client_ip"}
                for row in reversed(self.audits) if before is None or row["audit_id"] < before]
        return rows[:limit]

    def actions(self) -> list[str]:
        """Test helper: the audited actions in order."""
        return [row["action"] for row in self.audits]

    def sync_runs(self, limit: int = 14) -> list[dict]:
        self._call("sync_runs")
        return list(self.runs)

    def trade_daily(self, days: int = 30) -> dict | None:
        self._call("trade_daily")
        return self.trade

    def best_sellers(self, days: int = 30, limit: int = 15) -> list[dict] | None:
        self._call("best_sellers")
        return self.sellers


class LegacyInventory:
    """The staff repository behind /api/v1; one route is enough to probe the gate."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_parts(self, code: str, manufacturer: str | None = None, limit: int = 30) -> dict:
        self.calls.append(code)
        return {"status": "UNKNOWN", "warnings": [], "items": []}


class StaticVin:
    """A decoder that answers from memory and counts its calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.result: dict = {"status": "PROVISIONAL", "warnings": [], "vehicle": {
            "make": "TOYOTA", "model": "Camry", "model_year": 2015, "displacement_l": 2.5,
            "fuel": "Gasoline", "trim": "XLE", "plant_country": "private-plant-fixture"}}

    def decode(self, vin: str, model_year: int | None = None, *,
               deadline: float | None = None) -> dict:
        self.calls.append(vin)
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.result)


class QuietChat:
    def respond(self, message: str, vehicle: dict | None = None, language: str = "ka") -> dict:
        return {"status": "UNKNOWN", "answer": "", "records": []}


class NoSearch:
    """Stands in for the staff search coordinator, which would build provider clients."""

    def close(self) -> None:
        return None


class SiteAppCase(unittest.IsolatedAsyncioTestCase):
    """The real application around in-memory doubles; ``serve()`` builds a fresh one."""

    async def asyncSetUp(self) -> None:
        self._clients: list[httpx.AsyncClient] = []
        # The test loop starts in asyncio debug mode, which records a stack trace for every
        # task a request creates (a large part of the run time here) and reports each 60 ms
        # application build as a slow callback. Neither helps these tests.
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 5.0  # this very step is still timed in debug mode
        loop.set_debug(False)
        self._heartbeat = asyncio.create_task(self._pulse())
        hashing = patch.object(site_auth, "_scrypt", fast_scrypt)
        hashing.start()
        self.addCleanup(hashing.stop)
        self.site_repository = FakeSiteRepository()
        self.boss = self.site_repository.add_user("boss", "admin")
        self.worker = self.site_repository.add_user("worker", "employee")
        self.legacy = LegacyInventory()
        self.vin = StaticVin()
        self.settings = replace(
            Settings(), staff_auth=True, automotive_cache_path="", fuchs_enabled=False,
            shell_enabled=False, pakelo_enabled=False, jsfilter_enabled=False)

    async def asyncTearDown(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._heartbeat.cancel()
        try:
            await self._heartbeat
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _pulse() -> None:
        # As in tests/test_automotive_api.py: periodic timers let the synchronous routes
        # and the guard's session lookup finish without sockets.
        while True:
            await asyncio.sleep(0.01)

    def serve(self, settings: Settings | None = None, *, peer: tuple[str, int] | None = None,
              base_url: str = BASE_URL, chat_service: Any = None,
              **site: Any) -> httpx.AsyncClient:
        """Build an application (kept as ``self.app``) and return a client for it.

        ``site`` overrides the doubles handed to ``install_site``; ``repository=None``
        leaves the site database unconfigured."""
        overrides = {"repository": self.site_repository, "catalog": Catalog.from_rows([]),
                     **site}
        self.app = create_app(
            settings=settings or self.settings, repository=self.legacy, vin_service=self.vin,
            chat_service=chat_service or QuietChat(), search_service=NoSearch(),
            site={key: value for key, value in overrides.items() if value is not None})
        return self.connect(peer=peer, base_url=base_url)

    def connect(self, *, peer: tuple[str, int] | None = None,
                base_url: str = BASE_URL) -> httpx.AsyncClient:
        """Another client for ``self.app``, optionally from a different peer address."""
        transport = (httpx.ASGITransport(app=self.app, client=peer) if peer
                     else httpx.ASGITransport(app=self.app))
        client = httpx.AsyncClient(transport=transport, base_url=base_url)
        self._clients.append(client)
        return client

    async def sign_in(self, client: httpx.AsyncClient, username: str = "boss",
                      password: str = PASSWORD, origin: str = BASE_URL) -> dict[str, str]:
        """Sign in; the headers a browser would then send with a same-origin write."""
        response = await client.post("/api/auth/login", headers={"Origin": origin},
                                     json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        client.cookies.clear()  # Every request in a test states its own cookie.
        name, token = session_of(response)
        return {"Cookie": f"{name}={token}", "Origin": origin,
                "X-CSRF-Token": response.json()["csrf"]}

    def use_assets(self, files: dict[str, str]) -> Assets:
        """Serve a throwaway asset tree, so a build id never moves under a test."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assets = Assets({"site": root / "site", "admin": root / "admin"})
        self.app.state.site.pages.assets = assets
        return assets


def _token(headers: dict[str, str]) -> str:
    return headers["Cookie"].partition("=")[2]


async def _stream(total: int, piece: int = 1024):
    for _ in range(total // piece):
        yield b"x" * piece


class RuleTests(unittest.TestCase):
    def test_dashboard_api_always_needs_session_origin_and_csrf(self):
        for path in ("/api/admin/overview", "/api/admin/users/3/password", "/api/admin/media"):
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                for staff_auth in (True, False):
                    rule = rule_for(path, method, staff_auth)
                    self.assertEqual(
                        (rule.session, rule.csrf, rule.origin, rule.guarded, rule.blocked),
                        ("required", True, True, True, False), (path, method, staff_auth))
        self.assertGreaterEqual(rule_for("/api/admin/media", "POST", True).body_cap,
                                site_media.MAX_BYTES)
        self.assertLess(rule_for("/api/admin/content/hero", "PUT", True).body_cap, 1_000_000)

    def test_sign_in_needs_an_origin_and_the_other_auth_routes_also_csrf(self):
        login = rule_for("/api/auth/login", "POST", True)
        self.assertEqual((login.category, login.session, login.origin, login.csrf),
                         ("login", "optional", True, False))
        for path in ("/api/auth/logout", "/api/auth/password", "/api/auth/session"):
            rule = rule_for(path, "POST", True)
            self.assertEqual((rule.category, rule.session, rule.origin, rule.csrf),
                             ("auth", "optional", True, True))
            self.assertLessEqual(rule.body_cap, 4096)

    def test_public_api_is_anonymous_capped_and_split_into_limit_categories(self):
        conversation = "/api/site/chat/conversations/" + "c" * 43
        categories = {
            ("GET", "/api/site/parts/search"): "site_api",
            ("GET", "/api/site/parts/options"): "site_api",
            ("POST", "/api/site/vin"): "vin",
            ("POST", "/api/site/chat/conversations"): "chat_create",
            ("POST", conversation + "/messages"): "chat_message",
            ("GET", conversation + "/turns/" + "t" * 32): "chat_poll",
            ("DELETE", conversation): "site_api",
        }
        for (method, path), category in categories.items():
            rule = rule_for(path, method, True)
            self.assertEqual((rule.category, rule.session, rule.guarded, rule.body_cap),
                             (category, "none", True, 16_384), path)
        # The costly categories are the tight ones.
        for costly in ("vin", "chat_create", "chat_message", "login"):
            self.assertLessEqual(LIMITS[costly][0], 5)
            self.assertLess(LIMITS[costly][1], LIMITS["site_api"][1])

    def test_staff_pages_and_legacy_api_follow_the_staff_auth_switch(self):
        for path, login in (("/admin", "admin"), ("/admin/content", "admin")):
            for staff_auth in (True, False):  # the dashboard is never open
                rule = rule_for(path, "GET", staff_auth)
                self.assertEqual((rule.session, rule.login), ("required", login))
        self.assertEqual(rule_for("/staff", "GET", True).session, "required")
        self.assertEqual(rule_for("/staff", "GET", True).login, "staff")
        self.assertEqual(rule_for("/staff", "GET", False).session, "none")
        for path in ("/api/v1/inventory/search", "/api/v1/chat", "/api/v1/status",
                     "/openapi.json", "/docs", "/redoc", "/static/app.js"):
            self.assertEqual(rule_for(path, "GET", True).session, "required", path)
            self.assertEqual(rule_for(path, "GET", False).session, "none", path)
        self.assertTrue(rule_for("/api/v1/chat", "POST", True).origin)
        self.assertEqual(rule_for("/api/v1/health", "GET", True).session, "none")
        # Health stays public, so with the staff login on the guard limits it itself and
        # keeps it away from the legacy limiter. Without the login nothing changes.
        self.assertTrue(rule_for("/api/v1/health", "GET", True).guarded)
        self.assertFalse(rule_for("/api/v1/health", "GET", False).guarded)

    def test_api_paths_outside_the_known_namespaces_are_blocked(self):
        for path in ("/api", "/api/", "/api/x", "/api/v2/health", "/api/sitemap",
                     "/api/administrator"):
            for staff_auth in (True, False):
                self.assertTrue(rule_for(path, "GET", staff_auth).blocked, path)
        for path in ("/api/site/parts/options", "/api/auth/session", "/api/admin/overview",
                     "/api/v1/health", "/api/v1/status", "/apiary"):
            self.assertFalse(rule_for(path, "GET", True).blocked, path)

    def test_a_path_with_empty_or_dot_segments_counts_as_an_alias(self):
        for path in ("/static//site/js/site.js", "/static/./site/js/site.js",
                     "/static/x/../site/js/site.js", "/assets/abc/site/./dev/harness.html",
                     "//", "/.", "/..", "/api/v1//health", "/admin/..",
                     "/static/site\\dev\\engine.js", "/static\\site/js/site.js"):
            self.assertTrue(_aliased(path), path)
        for path in ("/", "/en", "/admin/", "/assets/abc/site/vendor/three@0.187.0/three.js",
                     "/media/" + "a" * 64, "/.well-known/security.txt", "/a..b/c.d"):
            self.assertFalse(_aliased(path), path)

    def test_operator_routes_are_loopback_only_in_both_modes(self):
        for staff_auth in (True, False):
            rule = rule_for("/api/v1/operator/enrichment/jobs", "POST", staff_auth)
            self.assertTrue(rule.loopback)
            self.assertEqual(rule.session, "none")  # bearer token, not a browser session
        self.assertFalse(rule_for("/api/v1/inventory/search", "GET", True).loopback)

    def test_source_trees_are_blocked_and_public_files_are_indexable(self):
        for path in ("/static/site/js/app.js", "/static/site/", "/static/admin/js/dom.js"):
            self.assertTrue(rule_for(path, "GET", False).blocked, path)
        for path in ("/assets/abc/site/app.css", "/media/" + "a" * 64, "/", "/en",
                     "/robots.txt", "/sitemap.xml"):
            rule = rule_for(path, "GET", True)
            self.assertEqual((rule.session, rule.noindex, rule.blocked), ("none", False, False),
                             path)
        self.assertTrue(rule_for("/login", "GET", True).noindex)
        self.assertTrue(rule_for("/api/site/parts/search", "GET", True).noindex)


class TokenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.key = ("login", "203.0.113.7")

    def test_burst_then_one_request_per_refill_interval(self):
        buckets = TokenBuckets(clock=self.clock)
        self.assertEqual([buckets.allow(self.key, 5, 5) for _ in range(7)],
                         [True] * 5 + [False] * 2)
        self.clock.advance(11.9)  # five a minute is one every twelve seconds
        self.assertFalse(buckets.allow(self.key, 5, 5))
        self.clock.advance(0.2)
        self.assertTrue(buckets.allow(self.key, 5, 5))
        self.assertFalse(buckets.allow(self.key, 5, 5))

    def test_idle_time_never_saves_up_more_than_the_burst(self):
        buckets = TokenBuckets(clock=self.clock)
        self.assertTrue(buckets.allow(self.key, 5, 5))
        self.clock.advance(86_400)
        self.assertEqual([buckets.allow(self.key, 5, 5) for _ in range(6)],
                         [True] * 5 + [False])

    def test_each_address_and_category_has_its_own_bucket(self):
        buckets = TokenBuckets(clock=self.clock)
        self.assertEqual([buckets.allow(self.key, 1, 1) for _ in range(2)], [True, False])
        self.assertTrue(buckets.allow(("login", "198.51.100.9"), 1, 1))
        self.assertTrue(buckets.allow(("vin", "203.0.113.7"), 1, 1))

    def test_has_looks_without_taking(self):
        buckets = TokenBuckets(clock=self.clock)
        for _ in range(5):
            self.assertTrue(buckets.has(self.key, 1, 6))
        self.assertEqual(buckets._buckets, {})  # looking leaves no trace either
        self.assertTrue(buckets.allow(self.key, 1, 6))
        self.assertFalse(buckets.has(self.key, 1, 6))
        self.clock.advance(10.1)  # one token per ten seconds
        self.assertTrue(buckets.has(self.key, 1, 6))

    def test_a_flood_of_new_addresses_evicts_the_oldest_instead_of_denying(self):
        buckets = TokenBuckets(maximum=3, clock=self.clock)
        self.assertEqual([buckets.allow(self.key, 1, 1) for _ in range(2)], [True, False])
        for index in range(50):
            self.assertTrue(buckets.allow(("login", f"198.51.100.{index}"), 1, 1))
        self.assertLessEqual(len(buckets._buckets), 3)
        self.assertTrue(buckets.allow(self.key, 1, 1))  # forgotten, so served again

    def test_a_full_table_forgets_a_refilled_bucket_before_one_still_owed(self):
        # A bucket that has refilled says nothing a new one would not. One still owed tokens
        # is the only memory of a client being limited, so it is the last to go.
        buckets = TokenBuckets(maximum=3, clock=self.clock)
        self.assertEqual([buckets.allow(self.key, 1, 0.01) for _ in range(2)], [True, False])
        for index in range(2):
            self.assertTrue(buckets.allow(("login", f"198.51.100.{index}"), 1, 60))
        self.clock.advance(2)  # the two newer buckets are full again, the oldest is not
        self.assertTrue(buckets.allow(("login", "198.51.100.200"), 1, 60))
        self.assertLessEqual(len(buckets._buckets), 3)
        self.assertFalse(buckets.allow(self.key, 1, 0.01))  # still remembered

    def test_allow_all_takes_from_every_bucket_or_from_none(self):
        buckets = TokenBuckets(clock=self.clock)
        narrow, wide = ("vin", "203.0.113.7"), ("vin", "*")
        self.assertTrue(buckets.allow(wide, 1, 1))  # the wide bucket is now empty
        self.assertFalse(buckets.allow_all([(narrow, 1, 1), (wide, 1, 1)]))
        self.assertTrue(buckets.allow(narrow, 1, 1))  # the refusal took nothing from it
        self.assertFalse(buckets.allow(narrow, 1, 1))


class DailyBudgetTests(unittest.TestCase):
    def test_an_hourly_pace_keeps_one_burst_from_spending_the_day(self):
        clock = Clock(86_400 * 20_000 + 3_600 * 5)
        budget = DailyBudget(10, per_hour=3, clock=clock)
        self.assertEqual([budget.take() for _ in range(4)], [True] * 3 + [False])
        budget.refund()
        self.assertTrue(budget.take())  # a refund within the hour gives its share back
        for _ in range(2):
            clock.advance(3_600)
            self.assertEqual([budget.take() for _ in range(4)], [True] * 3 + [False])
        clock.advance(3_600)
        self.assertEqual([budget.take() for _ in range(2)], [True, False])  # the day is spent
        self.assertEqual(budget.used, 10)

    def test_allowance_is_per_utc_day(self):
        clock = Clock(86_400 * 20_000 + 86_000)  # 400 s before midnight UTC
        budget = DailyBudget(2, clock=clock)
        self.assertEqual([budget.take() for _ in range(3)], [True, True, False])
        self.assertEqual(budget.used, 2)
        clock.advance(399)
        self.assertFalse(budget.take())
        clock.advance(2)  # past midnight
        self.assertEqual(budget.used, 0)
        self.assertEqual([budget.take() for _ in range(3)], [True, True, False])

    def test_amounts_are_all_or_nothing_and_a_zero_limit_refuses(self):
        budget = DailyBudget(3, clock=Clock(86_400.0))
        self.assertTrue(budget.take(2))
        self.assertFalse(budget.take(2))
        self.assertTrue(budget.take(1))
        self.assertFalse(DailyBudget(0, clock=Clock()).take())
        self.assertFalse(DailyBudget(-5, clock=Clock()).take())


class StaffGateTests(SiteAppCase):
    async def test_legacy_api_needs_a_session_when_the_staff_login_is_on(self):
        client = self.serve()
        search = {"code": "AP104/2"}
        response = await client.get("/api/v1/inventory/search", params=search)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": {"code": "NOT_SIGNED_IN"}})
        self.assertEqual(self.legacy.calls, [])
        for username in ("worker", "boss"):
            headers = await self.sign_in(client, username)
            response = await client.get("/api/v1/inventory/search", params=search,
                                        headers={"Cookie": headers["Cookie"]})
            self.assertEqual(response.status_code, 200, username)
        self.assertEqual(self.legacy.calls, ["AP104/2", "AP104/2"])

    async def test_legacy_writes_also_need_an_allow_listed_origin(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client, "worker"))["Cookie"]}
        body = {"message": "hello"}
        for headers in (cookie, {**cookie, "Origin": "https://evil.example"},
                        {**cookie, "Origin": "null"}):
            response = await client.post("/api/v1/chat", json=body, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"detail": {"code": "ORIGIN_REQUIRED"}})
        response = await client.post("/api/v1/chat", json=body, headers={**cookie, **ORIGIN})
        self.assertEqual(response.status_code, 200)
        response = await client.post("/api/v1/chat", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)

    async def test_without_staff_auth_the_legacy_api_opens_but_the_dashboard_does_not(self):
        client = self.serve(replace(self.settings, staff_auth=False))
        response = await client.get("/api/v1/inventory/search", params={"code": "AP104/2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await client.get("/staff")).status_code, 200)
        self.assertEqual((await client.get("/admin")).status_code, 303)
        self.assertEqual((await client.get("/api/admin/overview")).status_code, 401)
        self.assertEqual((await client.get("/api/admin/users")).status_code, 401)

    async def test_staff_pages_redirect_to_the_login_with_a_fixed_next(self):
        client = self.serve()
        for path, target in (("/staff", "/login?next=staff"), ("/admin", "/login?next=admin"),
                             ("/admin/content", "/login?next=admin")):
            for request in (client.get, client.head):
                response = await request(path)
                self.assertEqual((response.status_code, response.headers["location"]),
                                 (303, target), path)
        # Anything that is not a page load is refused, not redirected.
        self.assertEqual((await client.post("/staff", headers=ORIGIN)).status_code, 401)
        self.assertEqual((await client.get("/static/app.js")).status_code, 401)
        self.assertEqual((await client.get("/openapi.json")).status_code, 401)

    async def test_signed_in_staff_reach_the_workspace_and_the_dashboard_shell(self):
        client = self.serve()
        for username in ("worker", "boss"):
            cookie = {"Cookie": (await self.sign_in(client, username))["Cookie"]}
            for path in ("/staff", "/admin", "/admin/content", "/static/app.js"):
                response = await client.get(path, headers=cookie)
                self.assertEqual(response.status_code, 200, (username, path))

    async def test_health_stays_open(self):
        client = self.serve()
        response = await client.get("/api/v1/health")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "OK"))

    async def test_operator_routes_answer_only_direct_loopback_callers(self):
        url = "/api/v1/operator/enrichment/jobs/job-1"
        client = self.serve()
        # 401 is the route asking for its bearer token: the guard let the caller through.
        self.assertEqual((await client.get(url)).status_code, 401)
        for forwarded in ("203.0.113.9", "127.0.0.1"):
            response = await client.get(url, headers={"X-Forwarded-For": forwarded})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": {"code": "NOT_FOUND"}})
        remote = self.connect(peer=("203.0.113.9", 1234))
        self.assertEqual((await remote.get(url)).status_code, 404)
        response = await remote.post("/api/v1/operator/enrichment/jobs", headers=ORIGIN,
                                     json={"kind": "index"})
        self.assertEqual(response.status_code, 404)
        # Behind a proxy on this host a local caller cannot be told from a forwarded one.
        proxied = self.serve(replace(self.settings, trusted_proxies=("127.0.0.1",)))
        self.assertEqual((await proxied.get(url)).status_code, 404)
        # Unless the proxy has its own source address, as the example configuration sets.
        local = self.serve(replace(self.settings, trusted_proxies=("127.0.0.2",)))
        self.assertEqual((await local.get(url)).status_code, 401)
        through_proxy = self.connect(peer=("127.0.0.2", 4444))
        for headers in ({"X-Forwarded-For": "203.0.113.9"}, {}):
            self.assertEqual((await through_proxy.get(url, headers=headers)).status_code, 404)

    async def test_a_disabled_account_loses_access(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client, "worker"))["Cookie"]}
        self.assertEqual((await client.get("/api/auth/session", headers=cookie)).status_code,
                         200)
        self.site_repository.users[self.worker["user_id"]]["is_active"] = False
        self.app.state.site.auth.forget(user_id=self.worker["user_id"])
        self.assertEqual((await client.get("/api/auth/session", headers=cookie)).status_code,
                         401)
        self.assertEqual((await client.get("/staff", headers=cookie)).status_code, 303)


class SignInRouteTests(SiteAppCase):
    def credentials(self, username: str = "boss", password: str = PASSWORD, **extra) -> dict:
        return {"username": username, "password": password, **extra}

    async def test_login_sets_a_strict_http_only_cookie_and_stores_only_a_digest(self):
        client = self.serve()
        response = await client.post("/api/auth/login", headers=ORIGIN,
                                     json=self.credentials())
        self.assertEqual(response.status_code, 200)
        name, token = session_of(response)
        self.assertEqual(name, "garage_session")
        self.assertTrue(TOKEN.fullmatch(token))
        attributes = {part.strip() for part in response.headers["set-cookie"].split(";")[1:]}
        self.assertEqual(attributes, {"Path=/", "HttpOnly", "SameSite=Strict",
                                      "Max-Age=604800"})
        body = response.json()
        self.assertEqual(body, {
            "user": {"user_id": self.boss["user_id"], "username": "boss", "display_name": "",
                     "role": "admin"},
            "csrf": csrf_token(token), "next": "/admin"})
        self.assertNotIn(token, response.text)  # the token travels in the cookie only
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.site_repository.sessions,
                         {token_digest(token): self.boss["user_id"]})
        self.assertEqual(self.site_repository.audits[-1]["action"], "auth.login")
        self.assertEqual(self.site_repository.audits[-1]["client_ip"], "127.0.0.1")

    async def test_a_public_https_origin_gets_a_host_bound_secure_cookie(self):
        origin = "https://garage.example"
        settings = replace(
            self.settings, public_origins=(origin,),
            allowed_origins=(*self.settings.allowed_origins, origin),
            allowed_hosts=(*self.settings.allowed_hosts, "garage.example"))
        client = self.serve(settings, base_url=origin)
        response = await client.post("/api/auth/login", headers={"Origin": origin},
                                     json=self.credentials())
        client.cookies.clear()
        self.assertEqual(response.status_code, 200)
        name, token = session_of(response)
        self.assertEqual(name, "__Host-garage_session")
        attributes = {part.strip() for part in response.headers["set-cookie"].split(";")[1:]}
        self.assertEqual(attributes, {"Path=/", "HttpOnly", "SameSite=Strict", "Secure",
                                      "Max-Age=604800"})
        self.assertEqual(response.headers["strict-transport-security"], "max-age=31536000")
        # Only the prefixed name is read back: a sibling subdomain cannot plant that one.
        for cookie, status in ((f"garage_session={token}", 401),
                               (f"__Host-garage_session={token}", 200)):
            response = await client.get("/api/auth/session", headers={"Cookie": cookie})
            self.assertEqual(response.status_code, status, cookie.split("=")[0])

    async def test_a_second_login_ends_the_session_the_browser_presented(self):
        client = self.serve()
        first = await self.sign_in(client)
        response = await client.post(
            "/api/auth/login", headers={**ORIGIN, "Cookie": first["Cookie"]},
            json=self.credentials("worker"))
        client.cookies.clear()
        self.assertEqual(response.status_code, 200)
        name, second = session_of(response)
        self.assertNotEqual(second, _token(first))
        self.assertEqual(self.site_repository.sessions,
                         {token_digest(second): self.worker["user_id"]})
        old = await client.get("/api/auth/session", headers={"Cookie": first["Cookie"]})
        self.assertEqual(old.status_code, 401)
        new = await client.get("/api/auth/session", headers={"Cookie": f"{name}={second}"})
        self.assertEqual(new.json()["user"]["username"], "worker")

    async def test_a_failed_login_leaves_the_presented_session_alone(self):
        client = self.serve()
        first = await self.sign_in(client)
        response = await client.post(
            "/api/auth/login", headers={**ORIGIN, "Cookie": first["Cookie"]},
            json=self.credentials(password="wrong-password-1"))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        still = await client.get("/api/auth/session", headers={"Cookie": first["Cookie"]})
        self.assertEqual(still.status_code, 200)

    async def test_next_in_the_login_response_is_only_ever_admin_or_staff(self):
        client = self.serve(buckets=RecordingBuckets())
        landings = {"staff": "/staff", "admin": "/admin", "": "/admin", "STAFF": "/admin",
                    "/staff": "/admin", "//evil.example": "/admin",
                    "https://evil.ge": "/admin", "javascript:x": "/admin"}
        for requested, landing in landings.items():
            response = await client.post("/api/auth/login", headers=ORIGIN,
                                         json=self.credentials(next=requested))
            client.cookies.clear()
            self.assertEqual((response.status_code, response.json()["next"]), (200, landing),
                             requested)
        response = await client.post("/api/auth/login", headers=ORIGIN,
                                     json=self.credentials())
        self.assertEqual(response.json()["next"], "/admin")
        too_long = await client.post(
            "/api/auth/login", headers=ORIGIN,
            json=self.credentials(next="https://evil.example/landing"))
        self.assertEqual(too_long.status_code, 422)
        self.assertNotIn("set-cookie", too_long.headers)
        self.assertNotIn("evil.example", too_long.text)

    async def test_login_needs_an_allow_listed_origin(self):
        client = self.serve()
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": BASE_URL + "/"}, {"Referer": BASE_URL + "/login"}):
            response = await client.post("/api/auth/login", headers=headers,
                                         json=self.credentials())
            self.assertEqual(response.status_code, 403, headers)
            self.assertEqual(response.json(), {"detail": {"code": "ORIGIN_REQUIRED"}})
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.site_repository.sessions, {})
        self.assertNotIn("find_user", self.site_repository.calls)

    async def test_unknown_disabled_and_wrong_password_get_the_same_answer(self):
        self.site_repository.add_user("gone", "employee", active=False)
        client = self.serve()
        answers = set()
        for username, password in (("nobody", PASSWORD), ("gone", PASSWORD),
                                   ("boss", "wrong-password-1"), ("no such name!", PASSWORD)):
            response = await client.post("/api/auth/login", headers=ORIGIN,
                                         json=self.credentials(username, password))
            self.assertEqual(response.status_code, 401, username)
            self.assertNotIn("set-cookie", response.headers)
            answers.add(response.text)
        self.assertEqual(len(answers), 1)
        self.assertEqual(response.json(), {"detail": {"code": "INVALID_CREDENTIALS"}})
        self.assertEqual(self.site_repository.sessions, {})
        # One audit row per address a minute, not one per attempt.
        self.assertEqual(self.site_repository.actions(), ["auth.login_failed"])

    async def test_sign_in_slows_down_after_five_failures_without_a_lockout(self):
        client = self.serve(buckets=RecordingBuckets())
        for _ in range(5):
            response = await client.post("/api/auth/login", headers=ORIGIN,
                                         json=self.credentials(password="wrong-password-1"))
            self.assertEqual(response.status_code, 401)
        response = await client.post("/api/auth/login", headers=ORIGIN,
                                     json=self.credentials())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": {"code": "TOO_MANY_ATTEMPTS"}})
        self.assertNotIn("set-cookie", response.headers)
        # A colleague at the same address and the same account elsewhere still get in.
        await self.sign_in(client, "worker")
        await self.sign_in(self.connect(peer=("203.0.113.9", 1234)), "boss")

    async def test_login_reports_a_missing_or_unreachable_site_database(self):
        client = self.serve(repository=None)
        response = await client.post("/api/auth/login", headers=ORIGIN,
                                     json=self.credentials())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": {"code": "SITE_AUTH_NOT_CONFIGURED"}})
        self.site_repository.failing.add("find_user")
        client = self.serve()
        response = await client.post("/api/auth/login", headers=ORIGIN,
                                     json=self.credentials())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": {"code": "SITE_AUTH_UNAVAILABLE"}})
        self.assertNotIn("withheld", response.text)

    async def test_login_body_is_validated_without_echoing_it(self):
        client = self.serve(buckets=RecordingBuckets())
        secret = "private-fixture-password"
        for body in ({"username": "boss"}, {"password": secret},
                     self.credentials(password=secret, remember=True),
                     self.credentials("b" * 65, secret), self.credentials(password=""),
                     self.credentials(password=secret + "x" * 512), [secret], secret):
            response = await client.post("/api/auth/login", headers=ORIGIN, json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertNotIn(secret, response.text)
        self.assertNotIn("find_user", self.site_repository.calls)

    async def test_session_endpoint_reports_the_account_and_its_csrf_value(self):
        client = self.serve()
        headers = await self.sign_in(client, "worker")
        response = await client.get("/api/auth/session", headers={"Cookie": headers["Cookie"]})
        self.assertEqual(response.json(), {
            "user": {"user_id": self.worker["user_id"], "username": "worker",
                     "display_name": "", "role": "employee"},
            "csrf": headers["X-CSRF-Token"]})
        self.assertEqual(response.headers["cache-control"], "no-store")
        unknown = site_auth.new_token()
        for cookie in (None, "garage_session=not-a-token", f"garage_session={unknown}",
                       f"other={_token(headers)}"):
            response = await client.get("/api/auth/session",
                                        headers={"Cookie": cookie} if cookie else {})
            self.assertEqual(response.status_code, 401, cookie)
            self.assertEqual(response.json(), {"detail": {"code": "NOT_SIGNED_IN"}})

    async def test_logout_needs_origin_and_csrf_and_then_ends_the_session(self):
        client = self.serve()
        headers = await self.sign_in(client)
        other = await self.sign_in(client, "worker")
        cookie = {"Cookie": headers["Cookie"]}
        refused = (
            (cookie, "ORIGIN_REQUIRED"),
            ({**cookie, "X-CSRF-Token": headers["X-CSRF-Token"]}, "ORIGIN_REQUIRED"),
            ({**headers, "Origin": "https://evil.example"}, "ORIGIN_REQUIRED"),
            ({**cookie, **ORIGIN}, "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": ""}, "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": "0" * 64}, "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": other["X-CSRF-Token"]}, "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": _token(headers)}, "CSRF_REQUIRED"),
        )
        for sent, code in refused:
            response = await client.post("/api/auth/logout", headers=sent)
            self.assertEqual(response.status_code, 403, sent)
            self.assertEqual(response.json(), {"detail": {"code": code}})
        self.assertEqual((await client.get("/api/auth/session", headers=cookie)).status_code,
                         200)
        response = await client.post("/api/auth/logout", headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertTrue(response.headers["set-cookie"].startswith("garage_session=;"))
        self.assertEqual((await client.get("/api/auth/session", headers=cookie)).status_code,
                         401)
        self.assertEqual(list(self.site_repository.sessions),
                         [token_digest(_token(other))])

    async def test_logout_without_a_session_only_clears_the_cookie(self):
        client = self.serve()
        response = await client.post("/api/auth/logout", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertNotIn("delete_session", self.site_repository.calls)
        self.assertEqual((await client.post("/api/auth/logout")).status_code, 403)

    async def test_password_change_rotates_the_cookie_and_the_csrf_value(self):
        client = self.serve()
        headers = await self.sign_in(client, "worker")
        body = {"current": PASSWORD, "new": NEW_PASSWORD}
        for sent, status in (({"Origin": BASE_URL}, 401),
                             ({"Cookie": headers["Cookie"], **ORIGIN}, 403)):
            response = await client.post("/api/auth/password", json=body, headers=sent)
            self.assertEqual(response.status_code, status)
        for payload, status, code in (
                ({"current": "wrong-password-1", "new": NEW_PASSWORD}, 401,
                 "INVALID_CREDENTIALS"),
                ({"current": PASSWORD, "new": "short"}, 422, "TOO_SHORT"),
                ({"current": PASSWORD, "new": "aaaaaaaaaaaa"}, 422, "TOO_SIMPLE")):
            response = await client.post("/api/auth/password", json=payload, headers=headers)
            self.assertEqual((response.status_code, response.json()),
                             (status, {"detail": {"code": code}}))
        response = await client.post("/api/auth/password", json=body, headers=headers)
        client.cookies.clear()
        self.assertEqual(response.status_code, 200)
        name, fresh = session_of(response)
        self.assertNotEqual(fresh, _token(headers))
        self.assertEqual(response.json(), {"csrf": csrf_token(fresh)})
        self.assertNotIn(NEW_PASSWORD, response.text)
        old = await client.get("/api/auth/session", headers={"Cookie": headers["Cookie"]})
        self.assertEqual(old.status_code, 401)
        new = await client.get("/api/auth/session", headers={"Cookie": f"{name}={fresh}"})
        self.assertEqual(new.status_code, 200)
        self.assertEqual(self.site_repository.audits[-1]["action"], "auth.password_changed")
        await self.sign_in(client, "worker", NEW_PASSWORD)


class WriteProtectionTests(SiteAppCase):
    async def test_dashboard_writes_need_session_origin_and_csrf(self):
        client = self.serve()
        headers = await self.sign_in(client)
        other = await self.sign_in(client, "worker")
        cookie = {"Cookie": headers["Cookie"]}
        body = {"ids": []}
        refused = (
            ({}, 401, "NOT_SIGNED_IN"),
            (ORIGIN, 401, "NOT_SIGNED_IN"),
            ({**ORIGIN, "X-CSRF-Token": headers["X-CSRF-Token"]}, 401, "NOT_SIGNED_IN"),
            (cookie, 403, "ORIGIN_REQUIRED"),
            ({**cookie, "X-CSRF-Token": headers["X-CSRF-Token"]}, 403, "ORIGIN_REQUIRED"),
            ({**headers, "Origin": "https://evil.example"}, 403, "ORIGIN_REQUIRED"),
            ({**cookie, **ORIGIN}, 403, "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": other["X-CSRF-Token"]}, 403,
             "CSRF_REQUIRED"),
            ({**cookie, **ORIGIN, "X-CSRF-Token": headers["X-CSRF-Token"][:-1]}, 403,
             "CSRF_REQUIRED"),
        )
        for sent, status, code in refused:
            response = await client.post("/api/admin/services/order", json=body, headers=sent)
            self.assertEqual((response.status_code, response.json()),
                             (status, {"detail": {"code": code}}), sent)
        for method, path in (("PUT", "/api/admin/content/hero"),
                             ("PATCH", "/api/admin/users/1"),
                             ("DELETE", "/api/admin/services/1")):
            response = await client.request(method, path, headers={**cookie, **ORIGIN})
            self.assertEqual(response.status_code, 403, method)
        self.assertEqual(self.site_repository.calls.count("reorder"), 0)
        response = await client.post("/api/admin/services/order", json=body, headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.site_repository.calls.count("reorder"), 1)

    async def test_dashboard_reads_need_only_the_session(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        self.assertEqual((await client.get("/api/admin/overview", headers=cookie)).status_code,
                         200)
        self.assertEqual((await client.get("/api/admin/overview")).status_code, 401)

    async def test_a_non_ascii_csrf_header_is_refused_like_any_wrong_value(self):
        client = self.serve()
        headers = await self.sign_in(client)
        for value in (b"\xe9" * 64, "é".encode()):
            response = await client.post("/api/admin/services/order", json={"ids": []},
                                         headers={**headers, "X-CSRF-Token": value})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"detail": {"code": "CSRF_REQUIRED"}})

    async def test_a_body_over_the_cap_is_refused_before_the_route(self):
        client = self.serve()
        headers = await self.sign_in(client)
        login = b'{"username": "boss", "password": "' + b"x" * 5000 + b'"}'
        refused = [
            await client.post("/api/auth/login", content=login,
                              headers={**ORIGIN, "Content-Type": "application/json"}),
            # No Content-Length at all: the stream itself is counted.
            await client.post("/api/auth/login", content=_stream(8192), headers=ORIGIN),
            await client.post("/api/site/vin", content=_stream(20_480)),
            await client.post("/api/admin/services", content=b"x" * 300_000, headers=headers),
            await client.post("/api/admin/media", content=_stream(1_800_192), headers=headers),
        ]
        for response in refused:
            self.assertEqual(response.status_code, 413, response.request.url.path)
            self.assertEqual(response.json(), {"detail": {"code": "REQUEST_TOO_LARGE"}})
        self.assertEqual(self.site_repository.calls.count("find_user"), 1)  # the sign-in above
        self.assertEqual(self.vin.calls, [])
        self.assertNotIn("create_service", self.site_repository.calls)
        self.assertNotIn("add_media", self.site_repository.calls)


class RateLimitTests(SiteAppCase):
    async def test_sign_in_bursts_are_limited_per_address_until_the_bucket_refills(self):
        clock = Clock()
        client = self.serve(buckets=TokenBuckets(clock=clock))
        body = {"username": "boss", "password": PASSWORD}
        for _ in range(LIMITS["login"][0]):
            response = await client.post("/api/auth/login", headers=ORIGIN, json=body)
            client.cookies.clear()
            self.assertEqual(response.status_code, 200)
        lookups = self.site_repository.calls.count("find_user")
        response = await client.post("/api/auth/login", headers=ORIGIN, json=body)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": {"code": "RATE_LIMITED"}})
        self.assertEqual(response.headers["retry-after"], "10")
        self.assertEqual(response.headers["content-security-policy"], CSP)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.site_repository.calls.count("find_user"), lookups)
        # Another address and another kind of request are not affected.
        await self.sign_in(self.connect(peer=("203.0.113.9", 1234)))
        self.assertEqual((await client.get("/")).status_code, 200)
        clock.advance(60 / LIMITS["login"][1] + 0.1)
        response = await client.post("/api/auth/login", headers=ORIGIN, json=body)
        self.assertEqual(response.status_code, 200)

    async def test_vin_decodes_are_limited_before_the_provider_is_called(self):
        client = self.serve(buckets=TokenBuckets(clock=Clock()))
        for _ in range(LIMITS["vin"][0]):
            self.assertEqual((await client.post("/api/site/vin", json={"vin": VIN})).status_code,
                             200)
        response = await client.post("/api/site/vin", json={"vin": VIN})
        self.assertEqual((response.status_code, response.json()),
                         (429, {"detail": {"code": "RATE_LIMITED"}}))
        self.assertEqual(len(self.vin.calls), LIMITS["vin"][0])
        # Search has its own, larger allowance.
        self.assertEqual((await client.get("/api/site/parts/options")).status_code, 200)

    async def test_ipv6_clients_share_a_bucket_per_64(self):
        client = self.serve(buckets=TokenBuckets(clock=Clock()),
                            peer=("2001:db8:1:2::10", 4000))
        neighbour = self.connect(peer=("2001:db8:1:2:ffff::99", 4001))
        elsewhere = self.connect(peer=("2001:db8:1:3::10", 4002))
        for index in range(LIMITS["vin"][0]):
            sender = client if index % 2 else neighbour
            self.assertEqual((await sender.post("/api/site/vin", json={"vin": VIN})).status_code,
                             200)
        self.assertEqual((await neighbour.post("/api/site/vin", json={"vin": VIN})).status_code,
                         429)
        self.assertEqual((await elsewhere.post("/api/site/vin", json={"vin": VIN})).status_code,
                         200)


class RotationTests(SiteAppCase):
    """Changing addresses must not multiply an allowance (docs/garage-site.md, "Rate limits")."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings = replace(self.settings, trusted_proxies=("10.0.0.0/8",))
        self.clock = Clock()  # never advanced: every request below arrives at once
        self.use(TokenBuckets(clock=self.clock))

    def use(self, buckets: TokenBuckets) -> None:
        self.serve(buckets=buckets)
        self.proxy = self.connect(peer=("10.1.2.3", 4444))

    async def decode(self, address: str) -> int:
        response = await self.proxy.post("/api/site/vin", json={"vin": VIN},
                                         headers={"X-Forwarded-For": address})
        return response.status_code

    async def test_the_64s_of_one_ipv6_48_share_a_wider_allowance(self):
        burst = LIMITS["vin"][0]
        sixty_fours = [f"2001:db8:1:{index:x}::7" for index in range(WIDE + 2)]
        codes = [await self.decode(address) for address in sixty_fours for _ in range(burst)]
        self.assertEqual(codes.count(200), WIDE * burst)
        self.assertEqual(set(codes[WIDE * burst:]), {429})
        self.assertEqual(await self.decode("2001:db8:2::7"), 200)  # another /48 is untouched
        self.assertEqual(await self.decode("203.0.113.7"), 200)

    async def test_cycling_more_64s_than_the_table_holds_still_meets_the_48(self):
        # The /64 buckets churn through a table of 50, but every request touches the /48's,
        # which therefore stays and keeps counting.
        self.use(TokenBuckets(maximum=50, clock=self.clock))
        codes = [await self.decode(f"2001:db8:1:{index:x}::7") for index in range(200)]
        self.assertEqual(codes.count(200), WIDE * LIMITS["vin"][0])

    async def test_a_flood_of_sign_ins_from_many_addresses_never_locks_out_another(self):
        # No bucket is shared by the whole site, so addresses taking turns cannot use one up
        # and keep staff from signing in (the LoginThrottle docstring).
        for index in range(200):
            await self.proxy.post(
                "/api/auth/login", json={"username": "boss", "password": "wrong-password-1"},
                headers={**ORIGIN, "X-Forwarded-For": f"198.51.{index // 200}.{index % 200 + 1}"})
        response = await self.proxy.post(
            "/api/auth/login", json={"username": "boss", "password": PASSWORD},
            headers={**ORIGIN, "X-Forwarded-For": "203.0.113.77"})
        self.assertEqual(response.status_code, 200, response.text)


class HardeningTests(SiteAppCase):
    async def test_anonymous_requests_never_reach_the_legacy_limiter(self):
        # That limiter refuses everyone once its table is full, so only signed-in staff
        # may add to it.
        with patch("automotive.api.RateLimiter.allow", autospec=True,
                   return_value=True) as allow:
            client = self.serve()
            for path, status in (("/api/v1/health", 200), ("/api/x", 404),
                                 ("/api/v2/health", 404), ("/api/v1/status", 401),
                                 ("/api/site/parts/options", 200), ("/", 200),
                                 ("/login", 200)):
                self.assertEqual((await client.get(path)).status_code, status, path)
            allow.assert_not_called()
            cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
            response = await client.get("/api/v1/inventory/search", params={"code": "AP104/2"},
                                        headers=cookie)
            self.assertEqual(response.status_code, 200)
            allow.assert_called()

    async def test_unknown_api_paths_are_not_found_for_everyone(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        for headers in ({}, cookie):
            for path in ("/api", "/api/x", "/api/v2/health"):
                response = await client.get(path, headers=headers)
                self.assertEqual((response.status_code, response.json()),
                                 (404, {"detail": {"code": "NOT_FOUND"}}), path)

    async def test_a_second_spelling_of_a_path_is_not_found(self):
        client = self.serve()
        assets = self.use_assets({"site/app.css": "body { margin: 0 }",
                                  "site/dev/harness.js": "private-dev-fixture"})
        build = assets.build()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        self.assertEqual((await client.get(f"/assets/{build}/site/app.css")).status_code, 200)
        # %2e is how a dot segment survives the client's own tidying of the address.
        for path in (f"/assets/{build}/site/%2e/app.css", f"/assets/{build}/site//app.css",
                     f"/assets/{build}/site/x/%2e%2e/app.css",
                     f"/assets/{build}/site/%2e/dev/harness.js",
                     f"/assets/{build}/site/dev/harness.js",
                     "/static//site/js/site.js", "/static/%2e/site/js/site.js",
                     "/static/x/%2e%2e/site/js/site.js", "/api/v1//health"):
            response = await client.get(path, headers=cookie)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("private-dev-fixture", response.text)
        # However many spellings were tried, one file is held in memory once.
        self.assertEqual(set(assets._cache), {"site/app.css"})

    def stranger(self) -> dict[str, str]:
        """A well-formed session cookie nobody was ever given."""
        return {"Cookie": f"garage_session={new_token()}"}

    async def test_invented_session_cookies_are_rationed_before_the_database(self):
        clock = Clock()
        client = self.serve(buckets=TokenBuckets(clock=clock))
        burst = LIMITS["session"][0]
        self.assertLess(burst, LIMITS["page"][0])  # or the page limit would hide this one
        self.site_repository.calls.clear()
        for _ in range(burst):
            self.assertEqual((await client.get("/admin", headers=self.stranger())).status_code,
                             303)
        self.assertEqual(self.site_repository.calls.count("resolve_session"), burst)
        # The allowance for wrong cookies is spent: no more look-ups from this address.
        refused = await client.get("/api/admin/overview", headers=self.stranger())
        self.assertEqual((refused.status_code, refused.json()),
                         (429, {"detail": {"code": "RATE_LIMITED"}}))
        self.assertEqual(refused.headers["retry-after"], "10")
        # A page still goes to the sign-in form, which works whatever the cookie was...
        page = await client.get("/admin", headers=self.stranger())
        self.assertEqual((page.status_code, page.headers["location"]),
                         (303, "/login?next=admin"))
        # ...and so does signing in itself, with the stale cookie simply not looked at.
        signed = await client.post("/api/auth/login", headers={**ORIGIN, **self.stranger()},
                                   json={"username": "boss", "password": PASSWORD})
        self.assertEqual(signed.status_code, 200)
        self.assertEqual(self.site_repository.calls.count("resolve_session"), burst)
        # A request without a cookie has nothing to look up, and another address is not
        # affected at all.
        client.cookies.clear()
        self.assertEqual((await client.get("/admin")).status_code, 303)
        elsewhere = self.connect(peer=("203.0.113.9", 1234))
        self.assertEqual((await elsewhere.get("/api/admin/overview",
                                              headers=self.stranger())).status_code, 401)
        clock.advance(60 / LIMITS["session"][1] + 0.1)
        self.assertEqual((await client.get("/api/admin/overview",
                                           headers=self.stranger())).status_code, 401)

    async def test_wrong_cookies_from_its_own_address_never_hold_a_real_session_back(self):
        # A guest on the office network sends invented cookies; the member of staff beside
        # them, at the same address, must not notice.
        client = self.serve(buckets=TokenBuckets(clock=Clock()))
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        for _ in range(LIMITS["session"][0] + 5):
            await client.get("/admin", headers=self.stranger())
        self.assertEqual((await client.get("/api/admin/overview",
                                           headers=self.stranger())).status_code, 429)
        self.assertEqual((await client.get("/api/admin/overview", headers=cookie)).status_code,
                         200)
        # Also once its cached answer has run out and it is due a re-check.
        auth = self.app.state.site.auth
        digest = token_digest(_token(cookie))
        _, known_until, account = auth._cache[digest]
        auth._cache[digest] = (0.0, known_until, account)
        self.site_repository.calls.clear()
        self.assertEqual((await client.get("/api/admin/overview", headers=cookie)).status_code,
                         200)
        self.assertEqual(self.site_repository.calls.count("resolve_session"), 1)

    async def test_a_session_that_resolves_costs_no_allowance_even_when_unknown(self):
        buckets = RecordingBuckets()
        client = self.serve(buckets=buckets)
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        self.site_repository.calls.clear()
        for _ in range(10):
            self.assertEqual((await client.get("/admin", headers=cookie)).status_code, 200)
        # Known since sign-in: nothing is asked and nothing is spent.
        self.assertEqual(self.site_repository.calls.count("resolve_session"), 0)
        # After a restart the cache is empty and the same cookie has to be looked up, but
        # a cookie that turns out to be real is never charged.
        for _ in range(40):
            self.app.state.site.auth._cache.clear()
            self.assertEqual((await client.get("/admin", headers=cookie)).status_code, 200)
        self.assertEqual(self.site_repository.calls.count("resolve_session"), 40)
        self.assertNotIn("session", {key[0] for key in buckets.keys})
        await client.get("/admin", headers=self.stranger())
        self.assertEqual([key[0] for key in buckets.keys if key[0].startswith("session")],
                         ["session", "session_all"])

    async def test_look_ups_of_unknown_cookies_cannot_take_the_worker_threads(self):
        # With a slow database every look-up holds a worker thread. Only a few may run at
        # once; the rest are answered at once, and the public pages keep answering.
        release, running, peak = threading.Event(), [], []

        def slow(token_hash: str, idle_seconds: int) -> None:
            running.append(token_hash)
            peak.append(len(running))
            release.wait(10)
            running.remove(token_hash)

        self.serve(buckets=RecordingBuckets())
        self.site_repository.resolve_session = slow
        callers = [self.connect(peer=(f"198.51.100.{index}", 4000)) for index in range(12)]
        pending = [asyncio.create_task(caller.get("/api/admin/overview",
                                                  headers=self.stranger()))
                   for caller in callers]
        try:
            for _ in range(200):
                if len(running) == LOOKUPS:
                    break
                await asyncio.sleep(0.01)
            done = [task for task in pending if task.done()]
            for _ in range(200):
                done = [task for task in pending if task.done()]
                if len(done) == len(pending) - LOOKUPS:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(running), LOOKUPS)
            self.assertEqual({task.result().status_code for task in done}, {429})
            self.assertEqual(len(done), len(pending) - LOOKUPS)
            home = await asyncio.wait_for(callers[0].get("/"), timeout=5)
            self.assertEqual(home.status_code, 200)
        finally:
            release.set()
        responses = await asyncio.gather(*pending)
        self.assertEqual(sorted(response.status_code for response in responses),
                         [401] * LOOKUPS + [429] * (len(pending) - LOOKUPS))
        self.assertEqual(max(peak), LOOKUPS)

    async def test_a_database_blip_does_not_send_signed_in_staff_to_the_login(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        auth = self.app.state.site.auth
        digest = token_digest(_token(cookie))
        _, known_until, account = auth._cache[digest]
        auth._cache[digest] = (0.0, known_until, account)  # its cached answer has run out
        self.site_repository.failing.add("resolve_session")
        self.site_repository.calls.clear()
        page = await client.get("/admin", headers=cookie, follow_redirects=False)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.site_repository.calls.count("resolve_session"), 1)  # it was tried
        # A cookie the outage finds unknown is still sent to the sign-in form.
        stranger = await client.get("/admin", headers=self.stranger(), follow_redirects=False)
        self.assertEqual(stranger.status_code, 303)

    async def test_a_client_that_leaves_mid_body_is_not_passed_on_as_a_short_request(self):
        reached, sent = [], []

        async def inner(scope, receive, send):
            reached.append(scope["path"])

        messages = iter([{"type": "http.request", "body": b'{"vin": "1HG', "more_body": True},
                         {"type": "http.disconnect"}])

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        guard = SiteGuard(inner, auth=site_auth.SiteAuth(self.site_repository),
                          settings=self.settings)
        await guard({"type": "http", "method": "POST", "path": "/api/site/vin",
                     "scheme": "http", "query_string": b"", "client": ("203.0.113.7", 5000),
                     "headers": [(b"content-type", b"application/json")]}, receive, send)
        self.assertEqual((reached, sent), ([], []))
        # The same request with its whole body does reach the application.
        messages = iter([{"type": "http.request", "body": b'{"vin": "1HG', "more_body": True},
                         {"type": "http.request", "body": b'CM"}', "more_body": False}])
        await guard({"type": "http", "method": "POST", "path": "/api/site/vin",
                     "scheme": "http", "query_string": b"", "client": ("203.0.113.7", 5000),
                     "headers": [(b"content-type", b"application/json")]}, receive, send)
        self.assertEqual(reached, ["/api/site/vin"])


class ForwardedAddressTests(SiteAppCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.buckets = RecordingBuckets()
        self.settings = replace(self.settings,
                                trusted_proxies=("10.0.0.0/8", "2001:db8:ffff::/48"))
        self.serve(buckets=self.buckets)
        self.proxy = self.connect(peer=("10.1.2.3", 4444))
        self.stranger = self.connect(peer=("203.0.113.9", 1234))

    async def seen(self, client: httpx.AsyncClient, headers: Any) -> str:
        await client.get("/", headers=headers)
        return self.buckets.keys[-1][1]

    async def test_the_right_most_hop_outside_our_proxies_is_the_client(self):
        forwarded = {"X-Forwarded-For": "198.51.100.7, 203.0.113.50, 10.0.0.2"}
        self.assertEqual(await self.seen(self.proxy, forwarded), "203.0.113.50")
        self.assertEqual(await self.seen(self.proxy, {"X-Forwarded-For": "203.0.113.50"}),
                         "203.0.113.50")
        chained = {"X-Forwarded-For": "203.0.113.50, 2001:db8:ffff::2, 10.0.0.2"}
        self.assertEqual(await self.seen(self.proxy, chained), "203.0.113.50")

    async def test_a_header_from_an_untrusted_peer_is_ignored(self):
        forwarded = {"X-Forwarded-For": "198.51.100.7"}
        self.assertEqual(await self.seen(self.stranger, forwarded), "203.0.113.9")
        self.assertEqual(await self.seen(self.stranger, {}), "203.0.113.9")
        # With no proxy configured at all, nobody's header counts, not even loopback's.
        local = self.serve(replace(self.settings, trusted_proxies=()), buckets=self.buckets)
        self.assertEqual(await self.seen(local, forwarded), "127.0.0.1")

    async def test_a_chain_of_only_our_proxies_falls_back_to_the_peer(self):
        self.assertEqual(await self.seen(self.proxy, {"X-Forwarded-For": "10.0.0.9, 10.0.0.2"}),
                         "10.1.2.3")
        self.assertEqual(await self.seen(self.proxy, {}), "10.1.2.3")

    async def test_a_header_line_the_client_sent_cannot_outrank_the_proxy_line(self):
        # A proxy that appends its own X-Forwarded-For line leaves the client's line first.
        lines = [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-For", "203.0.113.50")]
        self.assertEqual(await self.seen(self.proxy, lines), "203.0.113.50")
        lines = [("X-Forwarded-For", "198.51.100.7, 192.0.2.1"),
                 ("X-Forwarded-For", "203.0.113.50, 10.0.0.2")]
        self.assertEqual(await self.seen(self.proxy, lines), "203.0.113.50")

    async def test_forwarded_ipv6_is_grouped_and_garbage_is_one_unknown_client(self):
        self.assertEqual(
            await self.seen(self.proxy, {"X-Forwarded-For": "2001:db8:1:2:3:4:5:6"}),
            "2001:db8:1:2::/64")
        self.assertEqual(await self.seen(self.proxy, {"X-Forwarded-For": "not-an-address"}),
                         "unknown")

    async def test_the_application_sees_the_same_client_as_the_limiter(self):
        response = await self.proxy.post(
            "/api/auth/login", json={"username": "boss", "password": "wrong-password-1"},
            headers={**ORIGIN, "X-Forwarded-For": "198.51.100.7, 203.0.113.50"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.site_repository.audits[-1]["client_ip"], "203.0.113.50")
        self.assertEqual(self.buckets.keys[-1], ("login", "203.0.113.50"))

    async def test_forwarded_proto_counts_only_through_a_trusted_proxy(self):
        forwarded = {"X-Forwarded-For": "203.0.113.50", "X-Forwarded-Proto": "https"}
        response = await self.proxy.get("/", headers=forwarded)
        self.assertEqual(response.headers["strict-transport-security"], "max-age=31536000")
        response = await self.stranger.get("/", headers=forwarded)
        self.assertNotIn("strict-transport-security", response.headers)
        response = await self.proxy.get("/", headers={**forwarded,
                                                      "X-Forwarded-Proto": "javascript"})
        self.assertNotIn("strict-transport-security", response.headers)


class ResponseHeaderTests(SiteAppCase):
    async def test_refusals_made_by_the_guard_carry_the_security_headers(self):
        client = self.serve()
        credentials = {"username": "boss", "password": PASSWORD}
        refusals = {
            401: await client.get("/api/admin/overview"),
            303: await client.get("/admin"),
            404: await client.get("/static/site/anything"),
            403: await client.post("/api/auth/login", json=credentials),
            413: await client.post("/api/auth/login", content=b"x" * 5000, headers=ORIGIN),
        }
        for status, response in refusals.items():
            with self.subTest(status=status):
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["content-security-policy"], CSP)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertIn("noindex", response.headers["x-robots-tag"])
                self.assertEqual(response.headers["content-length"],
                                 str(len(response.content)))

    async def test_the_policy_allows_no_inline_code_and_no_foreign_host(self):
        directives = dict(part.strip().split(" ", 1) for part in CSP.split(";"))
        for name in ("default-src", "script-src", "style-src", "connect-src", "img-src",
                     "form-action"):
            self.assertEqual(directives[name], "'self'", name)
        for name in ("base-uri", "frame-ancestors", "object-src"):
            self.assertEqual(directives[name], "'none'", name)
        client = self.serve()
        for path in ("/", "/login", "/api/site/parts/options", "/api/v1/health"):
            response = await client.get(path)
            self.assertEqual(response.headers["content-security-policy"], CSP, path)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff", path)
            self.assertEqual(response.headers["cross-origin-opener-policy"], "same-origin")
            self.assertNotIn("strict-transport-security", response.headers)  # plain http

    async def test_packaged_source_trees_are_not_served_from_static(self):
        client = self.serve()
        cookie = {"Cookie": (await self.sign_in(client))["Cookie"]}
        for path in ("/static/site/anything", "/static/site/js/engine/parts.js",
                     "/static/site/", "/static/admin/js/dom.js"):
            for headers in ({}, cookie):
                response = await client.get(path, headers=headers)
                self.assertEqual(response.status_code, 404, path)
                self.assertEqual(response.json(), {"detail": {"code": "NOT_FOUND"}})

    async def test_cache_policy_follows_the_kind_of_response(self):
        client = self.serve()
        assets = self.use_assets({"site/app.css": "body { margin: 0 }"})
        current = f"/assets/{assets.build()}/site/app.css"
        expected = {
            "/": "no-cache", "/en": "no-cache",
            "/login": "no-store",
            "/api/site/parts/options": "no-store",
            "/api/site/parts/search?q=filter": "no-store",
            "/api/v1/health": "no-store",
            current: IMMUTABLE,
            "/assets/000000000000/site/app.css": "no-store",  # a stale build id
            f"/assets/{assets.build()}/site/missing.css": "no-store",
            "/robots.txt": "public, max-age=300",
            "/sitemap.xml": "public, max-age=300",
            "/no-such-page": "no-store",
        }
        for path, policy in expected.items():
            response = await client.get(path)
            self.assertEqual(response.headers["cache-control"], policy, path)
            self.assertNotIn("x-site-cache", response.headers, path)
        self.assertEqual((await client.get(current)).text, "body { margin: 0 }")
        self.assertEqual((await client.get("/assets/000000000000/site/app.css")).status_code,
                         200)
        self.assertEqual((await client.head("/")).headers["cache-control"], "no-cache")
        self.assertEqual((await client.head(current)).headers["cache-control"], IMMUTABLE)

    async def test_only_the_login_page_among_public_pages_is_kept_out_of_indexes(self):
        client = self.serve()
        self.assertIn("noindex", (await client.get("/login")).headers["x-robots-tag"])
        for path in ("/", "/en", "/robots.txt"):
            self.assertNotIn("x-robots-tag", (await client.get(path)).headers, path)


if __name__ == "__main__":
    unittest.main()
