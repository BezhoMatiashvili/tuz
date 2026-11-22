"""Dashboard API for the Facebook section: admin only, the bot switch, the connection, chats.

Everything but the last class runs against in-memory doubles of the Meta store and the Graph
API, through the assembled application (install_site): no PostgreSQL, no Meta, no AI provider.
The last class repeats the flows on the real MetaStore as a member of garage_site, on a
disposable database (skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL), so every query the
section makes is checked against migration 0013's grants.
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import json
import logging
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from automotive.meta_config import REQUIRED_SCOPES, SUBSCRIBED_FIELDS, MetaSettings
from automotive.meta_crypto import TokenCipher
from automotive.meta_graph import GraphError
from automotive.meta_store import (
    SETTINGS_FIELDS,
    AutoReplyNotConfirmed,
    InvalidFields,
    MetaConflict,
    MetaStore,
    NotTester,
    bot_mode,
    mask_psid,
)
from automotive.site_meta_admin import JOBS, PREFIX
from tests.test_meta_store_database import ROLES_SQL, RoleDatabase
from tests.test_site_admin import AdminCase
from tests.test_site_guard import ORIGIN
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
APP_ID, PAGE_ID, OTHER_PAGE, OWNER_ID = ("220000000000002", "110000000000001",
                                         "110000000000009", "4400000001")
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
# Fixture tokens, readable on purpose; none of them is a real credential.
PASTED = "EAAPastedExplorerFixture0001"  # gitleaks:allow
LONG_TOKEN = "EAALongLivedUserFixture0002"  # gitleaks:allow
PAGE_TOKEN = "EAAPageTokenFixture0003abc"  # gitleaks:allow
SECRETS = (PASTED, LONG_TOKEN, PAGE_TOKEN)
SETTINGS = MetaSettings(app_id=APP_ID, app_secret="s" * 32, verify_token="v" * 40,
                        token_key=bytes(range(32)), public_url="https://garage.example.test")
DEFAULTS = {
    "settings_id": 1, "bot_enabled": False, "bot_enabled_at": None, "bot_changed_at": None,
    "bot_test_mode": False, "bot_test_at": None,
    "bot_changed_by": "", "bot_paused_reason": "", "bot_paused_at": None,
    "auto_reply_confirmed_off": False, "comments_enabled": False, "comments_enabled_at": None,
    "comments_changed_at": None, "comments_changed_by": "", "max_reply_age_minutes": 1440,
    "daily_turn_limit": 150, "customer_daily_turn_limit": 30, "comment_public_reply": "",
    "fallback_ka": "", "fallback_en": "", "retention_days": 365, "version": 1,
    "updated_at": datetime(2026, 11, 11, 6, 0, tzinfo=UTC)}


def ago(**delta: float) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


def _name(actor) -> str:
    return str(actor.get("username") or "") if isinstance(actor, dict) else str(actor or "")


class FakeMetaStore:
    """The MetaStore methods the dashboard and meta_connect use, over dictionaries, with the
    store's rules: the auto-reply confirmation, enabled_at stamped on off->on only, versions,
    and one audit row per change. tests/test_meta_store_database.py proves those rules on
    PostgreSQL; the last class here runs these routes on the real store."""

    def __init__(self) -> None:
        self.row = dict(DEFAULTS)
        self.page_row: dict | None = None
        self.sealed: dict[str, dict] = {}  # purpose -> metadata and the token (the store seals it)
        self.threads: dict[str, dict] = {}
        self.chat: dict[str, list[dict]] = {}
        self.events: list[dict] = []
        self.runs: list[dict] = []
        self.requests: list[dict] = []
        self.audits: list[dict] = []
        self.calls: list[str] = []
        self.boom: dict[str, Exception] = {}
        self.counts = {"webhooks": 4, "customer_messages": 3, "new_chats": 1, "replies_sent": 2,
                       "fallbacks": 0, "failed": 0, "skipped": 1, "attention": 1, "turns": 2,
                       "cost_usd": 0.06}
        self._ids = itertools.count(1)

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.boom:
            raise self.boom[name]

    def _record(self, actor, action: str, target: str, detail: dict | None,
                client_ip: str | None) -> None:
        self.audits.append({"actor": _name(actor), "action": action, "target": target,
                            "detail": json.loads(json.dumps(detail or {}, default=str)),
                            "client_ip": client_ip})

    def actions(self) -> list[str]:
        return [row["action"] for row in self.audits]

    def audit(self, actor, action: str, target: str = "", detail=None, *, client_ip=None,
              connection=None) -> None:
        self._call("audit")
        self._record(actor, action, str(target or ""), detail, client_ip)

    # -- settings ----------------------------------------------------------------------------

    def _bump(self) -> None:
        self.row["version"] += 1
        self.row["updated_at"] = datetime.now(UTC)

    def settings(self) -> dict:
        self._call("settings")
        return dict(self.row)

    def update_settings(self, fields, actor, expected_version=None, *, client_ip=None) -> dict:
        self._call("update_settings")
        unknown = {name: "UNKNOWN" for name in fields if name not in SETTINGS_FIELDS}
        if unknown:
            raise InvalidFields(unknown)
        if expected_version is not None and expected_version != self.row["version"]:
            raise MetaConflict("settings")
        self.row.update(fields)
        self._bump()
        self._record(actor, "messenger.settings_changed", "settings",
                     {"fields": sorted(fields), "version": self.row["version"]}, client_ip)
        return dict(self.row)

    def set_bot(self, enabled, actor, auto_reply_confirmed=None, *, client_ip=None) -> dict:
        self._call("set_bot")
        return self._set_mode("on" if enabled else "off", actor, auto_reply_confirmed, client_ip)

    def set_bot_mode(self, mode, actor, auto_reply_confirmed=None, *, client_ip=None) -> dict:
        self._call("set_bot_mode")
        return self._set_mode(mode, actor, auto_reply_confirmed, client_ip)

    def _set_mode(self, mode, actor, auto_reply_confirmed, client_ip) -> dict:
        if mode not in ("off", "test", "on"):
            raise ValueError("mode")
        confirmed = (bool(auto_reply_confirmed) if auto_reply_confirmed is not None
                     else mode != "test" and self.row["auto_reply_confirmed_off"])
        if mode == "on" and not confirmed:
            raise AutoReplyNotConfirmed("confirm first")
        was, was_paused = bot_mode(self.row), bool(self.row["bot_paused_reason"])
        now = datetime.now(UTC)
        if mode == "on" and was != "on":
            self.row["bot_enabled_at"] = now
        if mode != "off" and was == "off":
            self.row["bot_test_at"] = now
        self.row.update(bot_enabled=mode == "on", bot_test_mode=mode == "test",
                        bot_changed_at=now, bot_changed_by=_name(actor), bot_paused_reason="",
                        bot_paused_at=None, auto_reply_confirmed_off=confirmed)
        self._bump()
        self._record(actor, f"messenger.bot_{mode}", "settings",
                     {"changed": mode != was, "from": was, "was_paused": was_paused,
                      "auto_reply_confirmed_off": confirmed}, client_ip)
        return dict(self.row)

    def testers(self, limit=200) -> list[dict]:
        self._call("testers")
        rows = sorted((row for row in self.threads.values() if row.get("bot_tester")),
                      key=lambda row: (row["customer_name"].lower(), row["psid"]))
        return [{key: row[key] for key in ("psid", "customer_name", "version",
                                            "last_customer_at", "bot_enabled")}
                for row in rows][:limit]

    def resume_bot(self, actor, *, client_ip=None) -> dict:
        self._call("resume_bot")
        if not self.row["bot_paused_reason"]:
            return dict(self.row)
        reason = self.row["bot_paused_reason"]
        self.row.update(bot_paused_reason="", bot_paused_at=None)
        self._bump()
        self._record(actor, "messenger.bot_resumed", "settings", {"reason": reason}, client_ip)
        return dict(self.row)

    def set_comments(self, enabled, actor, *, client_ip=None) -> dict:
        self._call("set_comments")
        was_on = self.row["comments_enabled"]
        now = datetime.now(UTC)
        if enabled and not was_on:
            self.row["comments_enabled_at"] = now
        self.row.update(comments_enabled=bool(enabled), comments_changed_at=now,
                        comments_changed_by=_name(actor))
        self._bump()
        self._record(actor, "messenger.comments_on" if enabled else "messenger.comments_off",
                     "settings", {"changed": bool(enabled) != was_on}, client_ip)
        return dict(self.row)

    # -- connection --------------------------------------------------------------------------

    def page(self) -> dict | None:
        self._call("page")
        return copy.deepcopy(self.page_row)

    def save_connection(self, page_id, page_name, app_id, tokens, actor, *,
                        client_ip=None) -> dict:
        self._call("save_connection")
        now = datetime.now(UTC)
        self.page_row = {"page_id": page_id, "page_name": page_name, "app_id": app_id,
                         "subscribed_fields": [], "subscribed_at": None, "connected_at": now,
                         "connected_by": _name(actor), "version": 1}
        for purpose, entry in tokens.items():
            self.sealed[purpose] = {
                "purpose": purpose, "app_id": entry.get("app_id") or app_id,
                "subject_id": entry["subject_id"], "subject_name": entry.get("subject_name", ""),
                "token": entry["token"], "scopes": sorted(entry.get("scopes") or ()),
                "expires_at": entry.get("expires_at"),
                "data_access_expires_at": entry.get("data_access_expires_at"),
                "status": "ok", "status_detail": "", "checked_at": now, "connected_at": now,
                "connected_by": _name(actor)}
        self._record(actor, "messenger.connected", page_id,
                     {"purposes": sorted(tokens), "page_changed": False}, client_ip)
        return dict(self.page_row)

    def token(self, purpose: str) -> str | None:
        self._call("token")
        entry = self.sealed.get(purpose)
        return entry["token"] if entry else None

    def token_rows(self) -> list[dict]:
        self._call("token_rows")
        return [{key: value for key, value in entry.items() if key != "token"}
                for _, entry in sorted(self.sealed.items())]

    def mark_token(self, purpose, status, detail="", *, expiry=None) -> bool:
        self._call("mark_token")
        entry = self.sealed.get(purpose)
        if entry is None:
            return False
        entry.update(status=status, status_detail=detail, checked_at=datetime.now(UTC))
        if expiry is not None:
            entry.update(scopes=sorted(expiry.get("scopes") or ()),
                         expires_at=expiry.get("expires_at"),
                         data_access_expires_at=expiry.get("data_access_expires_at"))
        return True

    def set_subscription(self, fields) -> bool:
        self._call("set_subscription")
        if self.page_row is None:
            return False
        self.page_row.update(subscribed_fields=list(fields),
                             subscribed_at=datetime.now(UTC) if fields else None)
        return True

    def disconnect(self, actor, *, client_ip=None) -> dict:
        self._call("disconnect")
        page_id = self.page_row["page_id"] if self.page_row else ""
        purposes = sorted(self.sealed)
        self.page_row, self.sealed = None, {}
        self._record(actor, "messenger.disconnected", page_id, {"tokens": purposes}, client_ip)
        return {"page_id": page_id, "tokens_removed": purposes}

    # -- inbox, counts, jobs -------------------------------------------------------------------

    def recent_events(self, limit: int = 20) -> list[dict]:
        self._call("recent_events")
        return [dict(row) for row in self.events[:limit]]

    def last_event_at(self) -> dict | None:
        self._call("last_event_at")
        return ({"received_at": self.events[0]["received_at"], "field": self.events[0]["field"]}
                if self.events else None)

    def counts_today(self) -> dict:
        self._call("counts_today")
        return dict(self.counts)

    def latest_runs(self) -> list[dict]:
        self._call("latest_runs")
        return [dict(row) for row in self.runs]

    def request_job(self, job, actor, *, client_ip=None) -> int:
        self._call("request_job")
        request_id = next(self._ids)
        self.requests.append({"request_id": request_id, "job": job})
        self._record(actor, "messenger.job_requested", job, {}, client_ip)
        return request_id

    # -- threads and messages ------------------------------------------------------------------

    def add_thread(self, psid: str, *, name: str = "", last: datetime | None = None,
                   **fields) -> dict:
        """Test helper: a chat as the worker would have stored it."""
        row = {"psid": psid, "page_id": PAGE_ID, "customer_name": name, "conversation_id": "",
               "bot_enabled": True, "is_admin": False, "bot_tester": False, "source": "",
               "source_ad_id": "", "first_seen_at": ago(days=3), "last_customer_at": last,
               "last_shop_at": None, "last_bot_at": None, "needs_attention": False,
               "attention_reason": "", "engine_state_at": None, "context_cleared_at": None,
               "updated_at": ago(days=1), "version": 1, **fields}
        self.threads[psid] = row
        self.chat.setdefault(psid, [])
        return row

    def add_message(self, psid: str, sender: str, body: str, sent_at: datetime) -> None:
        """Test helper."""
        self.chat[psid].append({
            "message_id": next(self._ids), "mid": f"m_{uuid.uuid4().hex}", "psid": psid,
            "sender": sender, "body": body, "attachments": [], "quick_reply": "",
            "referral_ad_id": "", "app_id": "", "sent_at": sent_at, "stored_at": sent_at})

    def list_threads(self, filter: str = "all", limit: int = 50, offset: int = 0) -> list[dict]:
        self._call("list_threads")
        keep = {"all": lambda row: True, "attention": lambda row: row["needs_attention"],
                "off": lambda row: not row["bot_enabled"], "admin": lambda row: row["is_admin"],
                "ads": lambda row: row["source"] == "ads",
                "tester": lambda row: row["bot_tester"]}[filter]
        rows = sorted((row for row in self.threads.values() if keep(row)),
                      key=lambda row: row["last_customer_at"] or row["first_seen_at"],
                      reverse=True)
        shown = []
        for row in rows[offset:offset + limit]:
            last = max(self.chat[row["psid"]], key=lambda item: item["sent_at"], default=None)
            shown.append({**row, "last_sender": last and last["sender"],
                          "last_body": last and last["body"][:120],
                          "last_message_at": last and last["sent_at"]})
        return shown

    def get_thread(self, psid: str, *, with_state: bool = False) -> dict | None:
        self._call("get_thread")
        row = self.threads.get(psid)
        return dict(row) if row else None

    def messages(self, psid: str, limit: int = 50, *, before_id: int | None = None) -> list:
        self._call("messages")
        rows = [row for row in sorted(self.chat.get(psid, []), key=lambda item: item["sent_at"])
                if before_id is None or row["message_id"] < before_id]
        return [dict(row) for row in rows[-limit:]]

    def _switch(self, column, psid, value, actor, action, expected_version, client_ip):
        row = self.threads.get(psid)
        if row is None:
            return None
        if expected_version is not None and expected_version != row["version"]:
            raise MetaConflict("thread")
        row[column] = bool(value)
        row["version"] += 1
        self._record(actor, action, mask_psid(psid), {"version": row["version"]}, client_ip)
        return dict(row)

    def set_thread_switch(self, psid, enabled, actor, *, expected_version=None,
                          client_ip=None) -> dict | None:
        self._call("set_thread_switch")
        return self._switch("bot_enabled", psid, enabled, actor,
                            "messenger.chat_on" if enabled else "messenger.chat_off",
                            expected_version, client_ip)

    def set_admin(self, psid, is_admin, actor, *, expected_version=None,
                  client_ip=None) -> dict | None:
        self._call("set_admin")
        return self._switch("is_admin", psid, is_admin, actor,
                            "messenger.chat_admin_on" if is_admin else "messenger.chat_admin_off",
                            expected_version, client_ip)

    def set_tester(self, psid, tester, actor, *, expected_version=None,
                   client_ip=None) -> dict | None:
        self._call("set_tester")
        return self._switch("bot_tester", psid, tester, actor,
                            "messenger.chat_tester_on" if tester
                            else "messenger.chat_tester_off", expected_version, client_ip)

    def clear_context(self, psid, actor, *, expected_version=None,
                      client_ip=None) -> dict | None:
        self._call("clear_context")
        row = self.threads.get(psid)
        if row is None:
            return None
        if not row["bot_tester"]:
            raise NotTester("thread")
        if expected_version is not None and expected_version != row["version"]:
            raise MetaConflict("thread")
        row["context_cleared_at"] = datetime.now(UTC)
        row["version"] += 1
        self._record(actor, "messenger.chat_context_cleared", mask_psid(psid),
                     {"version": row["version"]}, client_ip)
        return dict(row)

    def flag_attention(self, psid, reason, *, needs=True, actor=None, client_ip=None) -> bool:
        self._call("flag_attention")
        row = self.threads.get(psid)
        if row is None:
            return False
        row.update(needs_attention=bool(needs), attention_reason=reason if needs else "")
        if actor is not None:
            self._record(actor, "messenger.attention_flagged" if needs
                         else "messenger.attention_cleared", mask_psid(psid), {}, client_ip)
        return True

    def delete_customer(self, psid, actor, *, client_ip=None) -> dict:
        self._call("delete_customer")
        counts = {"threads": int(self.threads.pop(psid, None) is not None),
                  "messages": len(self.chat.pop(psid, [])), "replies": 0, "inbox": None}
        self._record(actor, "messenger.customer_deleted", mask_psid(psid), counts, client_ip)
        return counts

    def probe_candidates(self, page_id, *, quiet_hours=25, limit=5) -> list[dict]:
        self._call("probe_candidates")
        cutoff = ago(hours=quiet_hours)
        rows = [row for row in self.threads.values() if not row["is_admin"]
                and row["last_customer_at"] is not None and row["last_customer_at"] < cutoff]
        return [{"psid": row["psid"], "last_customer_at": row["last_customer_at"]}
                for row in rows[:limit]]


class FakeGraph:
    """The GraphClient calls meta_connect makes, answered from memory. Nothing leaves."""

    def __init__(self) -> None:
        later = int((datetime.now(UTC) + timedelta(days=80)).timestamp())
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.tokens = {
            PASTED: {"is_valid": True, "app_id": APP_ID, "type": "USER", "user_id": OWNER_ID,
                     "scopes": list(REQUIRED_SCOPES), "data_access_expires_at": later},
            LONG_TOKEN: {"is_valid": True, "app_id": APP_ID, "type": "USER",
                         "user_id": OWNER_ID, "scopes": list(REQUIRED_SCOPES),
                         "expires_at": later, "data_access_expires_at": later},
            PAGE_TOKEN: {"is_valid": True, "app_id": APP_ID, "type": "PAGE",
                         "profile_id": PAGE_ID, "expires_at": 0, "scopes": ["pages_messaging"],
                         "data_access_expires_at": later}}
        self.accounts = [{"id": PAGE_ID, "name": "Garage • ფარეხი 1886",
                          "access_token": PAGE_TOKEN, "tasks": ["MESSAGING", "MODERATE"]}]
        self.page_apps = [{"id": APP_ID, "subscribed_fields": list(SUBSCRIBED_FIELDS)}]
        self.app_subs = [{"object": "page", "active": True, "callback_url": SETTINGS.webhook_url,
                          "fields": [{"name": name} for name in SUBSCRIBED_FIELDS]}]
        self.actions: list[tuple[str, str]] = []
        self.action_error: GraphError | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def debug_token(self, input_token) -> dict:
        self._call("debug_token")
        token = getattr(input_token, "token", input_token)
        return copy.deepcopy(self.tokens.get(token, {"is_valid": False, "error": {"code": 190}}))

    def exchange_user_token(self, short_token) -> dict:
        self._call("exchange_user_token")
        return {"access_token": LONG_TOKEN, "expires_in": 60 * 86400}

    def get(self, path, token, params=None, proof=True) -> dict:
        self._call("get")
        return {"id": OWNER_ID, "name": "Owner"} if path == "me" else {}

    def paged(self, path, token, params=None, max_pages=10, *, proof=True) -> list[dict]:
        self._call("paged")
        return copy.deepcopy(self.accounts) if path == "me/accounts" else []

    def subscribe_app(self, callback_url, verify_token, fields) -> dict:
        self._call("subscribe_app")
        return {"success": True}

    def app_subscriptions(self) -> dict:
        self._call("app_subscriptions")
        return {"data": copy.deepcopy(self.app_subs)}

    def subscribe_page(self, page_id, page_token, fields, proof=True) -> dict:
        self._call("subscribe_page")
        return {"success": True}

    def page_subscriptions(self, page_id, page_token, proof=True) -> dict:
        self._call("page_subscriptions")
        return {"data": copy.deepcopy(self.page_apps)}

    def unsubscribe_page(self, page_id, page_token, proof=True) -> dict:
        self._call("unsubscribe_page")
        return {"success": True}

    def sender_action(self, page_id, page_token, psid, action, proof=True) -> dict:
        self._call("sender_action")
        self.actions.append((psid, action))
        if self.action_error is not None:
            raise self.action_error
        return {"recipient_id": psid, "message_id": ""}


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@contextlib.contextmanager
def captured_logs():
    """Every log line of every logger, at every level, while the block runs."""
    handler, root = Captured(), logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


class FacebookCase(AdminCase):
    """AdminCase with the Facebook section wired to the doubles (``site={"meta": ...}``)."""

    meta_settings: MetaSettings | None = SETTINGS

    def make_store(self):
        return FakeMetaStore()

    async def asyncSetUp(self) -> None:
        self.store = self.make_store()
        self.graph = FakeGraph()
        await super().asyncSetUp()

    def serve(self, settings=None, **kwargs):
        kwargs.setdefault("meta", {"store": self.store, "graph": self.graph,
                                   "meta_settings": self.meta_settings})
        return super().serve(settings, **kwargs)

    async def fb(self, method: str, path: str, body=None, *, who: dict | None = None,
                 **kwargs):
        return await self.call(method, PREFIX + path, body, who=who, **kwargs)

    def assert_no_secret(self, *texts: str) -> None:
        for text in texts:
            for secret in SECRETS:
                self.assertNotIn(secret, text)

    def routes(self) -> list[tuple]:
        return [
            ("GET", "/status", None),
            ("PUT", "/settings", {"version": 1, "bot_enabled": False}),
            ("POST", "/resume", None),
            ("POST", "/connect", {"user_token": PASTED}),
            ("POST", "/check", None),
            ("POST", "/subscribe", None),
            ("POST", "/disconnect", {"confirm": True}),
            ("POST", "/probe", {}),
            ("POST", "/jobs/names", None),
            ("GET", "/threads", None),
            ("GET", f"/threads/{PSID}", None),
            ("PUT", f"/threads/{PSID}", {"bot_enabled": False}),
            ("DELETE", f"/threads/{PSID}", {"confirm": True}),
            ("POST", f"/threads/{PSID}/clear-context", {"confirm": True}),
        ]


class AccessTests(FacebookCase):
    async def test_an_employee_is_refused_every_route_before_anything_is_read(self):
        self.store.add_thread(PSID, last=ago(hours=2))
        for method, path, body in self.routes():
            response = await self.fb(method, path, body, who=self.employee)
            with self.subTest(method=method, path=path):
                self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual((self.store.calls, self.graph.calls, self.store.audits), ([], [], []))

    async def test_an_admin_passes_the_same_check(self):
        self.store.add_thread(PSID, last=ago(hours=2))
        for method, path, body in self.routes():
            response = await self.fb(method, path, body)
            with self.subTest(method=method, path=path):
                self.assertNotIn(response.status_code, (401, 403), response.text)

    async def test_writes_need_the_csrf_header_and_the_origin(self):
        self.store.add_thread(PSID, last=ago(hours=2))
        cookie = {"Cookie": self.admin["Cookie"]}
        for method, path, body in self.routes():
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
        self.assertEqual((self.store.calls, self.graph.calls), ([], []))

    async def test_reads_need_a_session(self):
        response = await self.client.get(PREFIX + "/status")
        self.assertEqual((response.status_code, response.json()),
                         (401, {"detail": {"code": "NOT_SIGNED_IN"}}))

    async def test_install_site_mounts_the_section_and_leaves_it_off_without_the_meta_block(self):
        client = self.serve(meta=None)  # the real wiring: environment and site login
        admin = await self.sign_in(client)
        body = (await client.get(PREFIX + "/status", headers=admin)).json()
        self.assertIs(body["configured"], False)
        self.assertIn("AUTOMOTIVE_SITE_DATABASE_URL", body["missing"])
        response = await client.put(PREFIX + "/settings", headers=admin,
                                    json={"version": 1, "bot_enabled": True})
        self.assertEqual(response.json(), {"detail": {"code": "META_NOT_CONFIGURED"}})
        # The rest of the dashboard is unaffected.
        self.assertEqual((await client.get("/api/admin/overview", headers=admin)).status_code,
                         200)


class NotConfiguredTests(FacebookCase):
    meta_settings = MetaSettings(app_id=APP_ID)

    async def test_status_names_what_is_missing_and_everything_else_is_503(self):
        response = await self.fb("GET", "/status")
        self.assertEqual((response.status_code, response.json()), (200, {
            "configured": False, "invalid": [],
            "missing": ["META_APP_SECRET", "META_TOKEN_ENCRYPTION_KEY",
                        "META_WEBHOOK_VERIFY_TOKEN", "MESSENGER_PUBLIC_URL"]}))
        for method, path, body in self.routes()[1:]:
            response = await self.fb(method, path, body)
            with self.subTest(method=method, path=path):
                self.assert_refused(response, 503, "META_NOT_CONFIGURED")
            response = await self.fb(method, path, body, who=self.employee)
            with self.subTest(method=method, path=path, who="employee"):
                self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual((self.store.calls, self.graph.calls), ([], []))

    async def test_a_malformed_variable_is_named_but_its_value_never_shown(self):
        with patch.dict(os.environ, {"META_APP_ID": "not-a-number-7f3c"}):
            client = self.serve(meta={"store": self.store, "graph": self.graph})
        admin = await self.sign_in(client)
        response = await client.get(PREFIX + "/status", headers=admin)
        self.assertEqual(response.json(), {"configured": False, "missing": [],
                                           "invalid": ["META_APP_ID"]})
        self.assertNotIn("7f3c", response.text)


class SwitchTests(FacebookCase):
    async def put_settings(self, **fields):
        return await self.fb("PUT", "/settings", {"version": self.store.row["version"],
                                                  **fields})

    async def test_status_reports_switches_connection_numbers_and_warnings(self):
        now = datetime.now(UTC)
        self.store.page_row = {"page_id": PAGE_ID, "page_name": "Garage", "app_id": APP_ID,
                               "subscribed_fields": [], "subscribed_at": None,
                               "connected_at": ago(days=5), "connected_by": "boss",
                               "version": 1}
        self.store.sealed = {
            "page": {"purpose": "page", "app_id": APP_ID, "subject_id": PAGE_ID,
                     "subject_name": "Garage", "token": PAGE_TOKEN, "scopes": ["pages_messaging"],
                     "expires_at": None, "data_access_expires_at": now + timedelta(days=60),
                     "status": "ok", "status_detail": "", "checked_at": now,
                     "connected_at": now, "connected_by": "boss"},
            "user": {"purpose": "user", "app_id": APP_ID, "subject_id": OWNER_ID,
                     "subject_name": "Owner", "token": LONG_TOKEN,
                     "scopes": ["pages_messaging", "pages_manage_metadata", "pages_show_list"],
                     "expires_at": now + timedelta(days=10, hours=1),
                     "data_access_expires_at": now - timedelta(days=1), "status": "ok",
                     "status_detail": "", "checked_at": now, "connected_at": now,
                     "connected_by": "boss"},
            "send_page": {"purpose": "send_page", "app_id": "330000000000003",
                          "subject_id": PAGE_ID, "subject_name": "Garage", "token": "EAAother1",
                          "scopes": [], "expires_at": None, "data_access_expires_at": None,
                          "status": "broken", "status_detail": "meta_code_190",
                          "checked_at": now, "connected_at": now, "connected_by": "boss"}}
        self.store.events = [{"inbox_id": 9, "field": "messages", "received_at": ago(hours=30),
                              "processed_at": ago(hours=30), "attempts": 1, "outcome": "done"}]
        self.store.runs = [{"run_id": 3, "job": "purge", "started_at": ago(hours=5),
                            "finished_at": ago(hours=5), "status": "ok", "records": 4,
                            "detail": ""},
                           {"run_id": 2, "job": "names", "started_at": ago(hours=6),
                            "finished_at": None, "status": "running", "records": 0,
                            "detail": ""}]
        self.store.row.update(bot_paused_reason="ai_provider", bot_enabled=True,
                              bot_paused_at=ago(minutes=5))
        response = await self.fb("GET", "/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"configured", "connected", "switches", "page", "tokens",
                                     "last_event", "events", "today", "runs", "jobs",
                                     "required_scopes", "warnings", "checked_at", "testers"})
        self.assertIs(body["connected"], True)
        switches = body["switches"]
        self.assertEqual((switches["bot_state"], switches["version"],
                          switches["daily_turn_limit"], switches["auto_reply_confirmed_off"]),
                         ("paused", 1, 150, False))
        self.assertNotIn("settings_id", switches)
        self.assertEqual(body["page"]["page_id"], PAGE_ID)
        tokens = {row["purpose"]: row for row in body["tokens"]}
        self.assertEqual(set(tokens["user"]), {
            "purpose", "subject_name", "scopes", "expires_at", "data_access_expires_at",
            "status", "status_detail", "checked_at", "connected_at", "connected_by",
            "days_left", "data_access_days_left", "other_app", "missing_scopes", "warn"})
        self.assertEqual((tokens["user"]["days_left"], tokens["user"]["warn"]), (10, True))
        self.assertEqual(tokens["user"]["missing_scopes"], [
            name for name in REQUIRED_SCOPES
            if name not in ("pages_messaging", "pages_manage_metadata", "pages_show_list")])
        self.assertEqual((tokens["page"]["warn"], tokens["page"]["missing_scopes"]), (False, []))
        self.assertEqual((tokens["send_page"]["other_app"], tokens["page"]["other_app"]),
                         (True, False))
        self.assertEqual(datetime.fromisoformat(tokens["user"]["expires_at"]),
                         self.store.sealed["user"]["expires_at"])
        self.assertEqual([warning["code"] for warning in body["warnings"]], [
            "bot_paused", "not_subscribed", "no_recent_webhook", "token_broken",
            "token_expiring", "data_access_expired", "auto_reply_not_confirmed"])
        self.assertEqual(body["warnings"][3]["purpose"], "send_page")
        self.assertEqual((body["warnings"][4]["days"], body["warnings"][5]["days"]), (10, 0))
        self.assertEqual(body["events"][0]["outcome"], "done")
        self.assertEqual([run["job"] for run in body["runs"]], ["names", "purge"])
        self.assertEqual((body["today"]["turns"], body["jobs"]), (2, list(JOBS)))
        self.assert_no_secret(response.text)
        self.assertNotIn("EAAother1", response.text)
        self.assertNotIn("token_enc", response.text)

    async def test_status_of_a_fresh_install_asks_to_connect(self):
        body = (await self.fb("GET", "/status")).json()
        self.assertEqual([warning["code"] for warning in body["warnings"]],
                         ["not_connected", "auto_reply_not_confirmed"])
        self.assertEqual((body["page"], body["tokens"], body["last_event"], body["connected"]),
                         (None, [], None, False))
        self.assertEqual(body["required_scopes"], list(REQUIRED_SCOPES))

    async def test_turning_the_bot_on_needs_the_auto_reply_confirmation(self):
        for extra in ({}, {"auto_reply_confirmed_off": False}):
            response = await self.put_settings(bot_enabled=True, **extra)
            with self.subTest(extra=extra):
                self.assert_refused(response, 409, "AUTO_REPLY_NOT_CONFIRMED")
        # Refused before anything was written, limits sent along included.
        response = await self.put_settings(bot_enabled=True, daily_turn_limit=99)
        self.assert_refused(response, 409, "AUTO_REPLY_NOT_CONFIRMED")
        self.assertEqual(self.store.row, DEFAULTS)
        self.assertEqual(self.store.audits, [])

    async def test_on_with_the_confirmation_stamps_enabled_at_once_and_is_audited(self):
        response = await self.put_settings(bot_enabled=True, auto_reply_confirmed_off=True)
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["bot_enabled"], switches["bot_state"],
                          switches["bot_changed_by"], switches["auto_reply_confirmed_off"],
                          switches["version"]), (True, "on", "boss", True, 2))
        stamped = datetime.fromisoformat(switches["bot_enabled_at"])
        self.assertLess(datetime.now(UTC) - stamped, timedelta(minutes=1))
        self.assertEqual(self.store.audits, [{
            "actor": "boss", "action": "messenger.bot_on", "target": "settings",
            "detail": {"changed": True, "from": "off", "was_paused": False,
                       "auto_reply_confirmed_off": True},
            "client_ip": "127.0.0.1"}])
        # Already on: nothing is written, so enabled_at stays put and nothing is audited.
        response = await self.put_settings(bot_enabled=True)
        self.assertEqual(response.json()["switches"]["bot_enabled_at"],
                         switches["bot_enabled_at"])
        self.assertEqual(len(self.store.audits), 1)

    async def test_off_always_works_even_from_a_stale_page_and_is_audited(self):
        await self.put_settings(bot_enabled=True, auto_reply_confirmed_off=True)
        self.store.row["version"] += 5  # the worker paused and resumed meanwhile
        response = await self.fb("PUT", "/settings", {"version": 1, "bot_enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["switches"]["bot_state"], "off")
        self.assertEqual(self.store.actions(), ["messenger.bot_on", "messenger.bot_off"])
        self.assertEqual(self.store.audits[-1]["detail"]["changed"], True)
        # Anything else from that stale page is refused.
        for fields in ({"bot_enabled": True, "auto_reply_confirmed_off": True},
                       {"bot_enabled": False, "daily_turn_limit": 10},
                       {"comments_enabled": True}):
            response = await self.fb("PUT", "/settings", {"version": 1, **fields})
            with self.subTest(fields=fields):
                self.assert_refused(response, 409, "VERSION_CONFLICT")
        self.assertIs(self.store.row["bot_enabled"], False)
        self.assertEqual(len(self.store.audits), 2)

    async def test_limits_and_texts_are_checked_against_the_database_bounds(self):
        refused = [
            ({"daily_turn_limit": 5001}, {"daily_turn_limit": "OUT_OF_RANGE"}),
            ({"customer_daily_turn_limit": -1}, {"customer_daily_turn_limit": "OUT_OF_RANGE"}),
            ({"max_reply_age_minutes": 4}, {"max_reply_age_minutes": "OUT_OF_RANGE"}),
            ({"retention_days": 29}, {"retention_days": "OUT_OF_RANGE"}),
            # Reply attempts, job runs and alarms: 30-365 days (messages are kept for good).
            ({"retention_days": 366}, {"retention_days": "OUT_OF_RANGE"}),
            ({"daily_turn_limit": True}, {"daily_turn_limit": "INVALID"}),
            ({"max_reply_age_minutes": "60"}, {"max_reply_age_minutes": "INVALID"}),
            ({"bot_enabled": "yes"}, {"bot_enabled": "INVALID"}),
            ({"fallback_ka": "x" * 501}, {"fallback_ka": "TOO_LONG"}),
            ({"comment_public_reply": "y" * 301}, {"comment_public_reply": "TOO_LONG"}),
            ({"fallback_en": "line\x00break"}, {"fallback_en": "INVALID"}),
            ({"fallback_ka": None}, {"fallback_ka": "INVALID"}),
            ({"surprise": 1}, {"surprise": "UNKNOWN"}),
            ({}, {"": "REQUIRED"}),
        ]
        for fields, codes in refused:
            response = await self.put_settings(**fields)
            with self.subTest(fields=fields):
                self.assert_refused(response, 422, "INVALID_FIELDS", codes)
        response = await self.fb("PUT", "/settings", {"bot_enabled": False})
        self.assert_refused(response, 422, "INVALID_FIELDS", {"version": "REQUIRED"})
        for raw in (b"[1, 2]", b"{not json", b'"text"'):
            response = await self.fb("PUT", "/settings", raw,
                                     headers={"Content-Type": "application/json"})
            self.assert_refused(response, 422, "INVALID_REQUEST")
        self.assertEqual((self.store.row, self.store.audits), (DEFAULTS, []))

        response = await self.put_settings(daily_turn_limit=80, retention_days=90,
                                           fallback_ka="  მოგვწერეთ ტელეფონზე.\nმადლობა  ")
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["daily_turn_limit"], switches["retention_days"],
                          switches["fallback_ka"], switches["version"]),
                         (80, 90, "მოგვწერეთ ტელეფონზე.\nმადლობა", 2))
        self.assertEqual(self.store.audits[0]["action"], "messenger.settings_changed")
        self.assertEqual(self.store.audits[0]["detail"]["fields"],
                         ["daily_turn_limit", "fallback_ka", "retention_days"])
        # The version just read must come back with the next change.
        response = await self.fb("PUT", "/settings", {"version": 1, "daily_turn_limit": 70})
        self.assert_refused(response, 409, "VERSION_CONFLICT")

    async def test_the_comment_switch_and_the_confirmation_have_their_own_rules(self):
        response = await self.put_settings(comments_enabled=True)
        self.assertEqual(response.json()["switches"]["comments_enabled"], True)
        self.assertEqual(self.store.actions(), ["messenger.comments_on"])
        # The confirmation can be given on its own while the bot is off...
        response = await self.put_settings(auto_reply_confirmed_off=True)
        self.assertEqual(response.json()["switches"]["auto_reply_confirmed_off"], True)
        await self.put_settings(bot_enabled=True)
        # ...but not withdrawn while the bot answers, unless it goes off in the same change.
        response = await self.put_settings(auto_reply_confirmed_off=False)
        self.assert_refused(response, 409, "AUTO_REPLY_NOT_CONFIRMED")
        response = await self.put_settings(bot_enabled=False, auto_reply_confirmed_off=False)
        self.assertEqual((response.json()["switches"]["bot_enabled"],
                          response.json()["switches"]["auto_reply_confirmed_off"]),
                         (False, False))

    async def test_resume_clears_a_provider_pause_and_is_audited(self):
        self.store.row.update(bot_enabled=True, auto_reply_confirmed_off=True,
                              bot_paused_reason="ai_provider", bot_paused_at=ago(minutes=3))
        # Saying "on" again does not lift the pause: that takes the explicit resume.
        response = await self.fb("PUT", "/settings", {"version": 1, "bot_enabled": True,
                                                      "auto_reply_confirmed_off": True})
        self.assertEqual(response.json()["switches"]["bot_state"], "paused")
        self.assertEqual(self.store.audits, [])
        response = await self.fb("POST", "/resume")
        self.assertEqual(response.json()["switches"]["bot_state"], "on")
        self.assertEqual(self.store.audits[0]["action"], "messenger.bot_resumed")
        # Nothing to resume: nothing written.
        await self.fb("POST", "/resume")
        self.assertEqual(len(self.store.audits), 1)


class ConnectionTests(FacebookCase):
    async def connect_page(self, **body):
        return await self.fb("POST", "/connect", {"user_token": PASTED, **body})

    async def test_connect_stores_the_tokens_and_no_answer_audit_or_log_carries_them(self):
        with captured_logs() as logs:
            response = await self.connect_page()
            status = await self.fb("GET", "/status")
            check = await self.fb("POST", "/check")
        self.assertEqual(response.status_code, 200, response.text)
        connected = response.json()["connected"]
        self.assertEqual((connected["page_id"], connected["page_name"],
                          connected["missing_scopes"], connected["subscription"]["ok"]),
                         (PAGE_ID, "Garage • ფარეხი 1886", [], True))
        self.assertIsInstance(connected["user_expires_at"], str)
        self.assertEqual({purpose: entry["token"] for purpose, entry in self.store.sealed.items()},
                         {"page": PAGE_TOKEN, "user": LONG_TOKEN})
        self.assertEqual(status.json()["page"]["page_id"], PAGE_ID)
        self.assertEqual([row["purpose"] for row in status.json()["tokens"]], ["page", "user"])
        self.assertEqual([row["status"] for row in check.json()["tokens"]], ["ok", "ok"])
        self.assertEqual(self.store.actions(), ["messenger.connected", "messenger.tokens_checked"])
        self.assertEqual(self.store.audits[1]["detail"],
                         {"page": "ok", "user": "ok", "warn": []})
        self.assert_no_secret(response.text, status.text, check.text,
                              json.dumps(self.store.audits, ensure_ascii=False), *logs.lines)

    async def test_connect_failures_come_back_as_codes_with_safe_detail(self):
        cases = []
        cases.append(("missing", {}, 422, "INVALID_FIELDS", {"user_token": "REQUIRED"}))
        cases.append(("format", {"user_token": "short"}, 422, "TOKEN_INVALID",
                      {"reason": "format"}))
        cases.append(("unknown key", {"user_token": PASTED, PASTED: 1}, 422, "INVALID_FIELDS",
                      {"": "UNKNOWN"}))
        cases.append(("page id", {"user_token": PASTED, "page_id": "abc"}, 422,
                      "INVALID_FIELDS", {"page_id": "INVALID"}))
        for label, body, status, code, fields in cases:
            response = await self.fb("POST", "/connect", body)
            with self.subTest(label):
                self.assert_refused(response, status, code, fields)

        self.graph.tokens[PASTED]["app_id"] = "999999"
        self.assert_refused(await self.connect_page(), 422, "WRONG_APP", {"app_id": "999999"})
        self.graph.tokens[PASTED].update(app_id=APP_ID, scopes=["pages_show_list", "ads_read"])
        response = await self.connect_page()
        self.assertEqual(response.json()["detail"]["code"], "MISSING_SCOPES")
        self.assertEqual(response.json()["detail"]["fields"]["essential"],
                         ["pages_messaging", "pages_manage_metadata"])
        self.graph.tokens[PASTED]["scopes"] = list(REQUIRED_SCOPES)

        self.graph.accounts.append({"id": OTHER_PAGE, "name": "TNLU", "access_token": "EAAx1",
                                    "tasks": []})
        response = await self.connect_page()
        self.assert_refused(response, 409, "MULTIPLE_PAGES", {"choices": [
            {"id": PAGE_ID, "name": "Garage • ფარეხი 1886"}, {"id": OTHER_PAGE, "name": "TNLU"}]})
        response = await self.connect_page(page_id=PAGE_ID)
        self.assertEqual(response.json()["connected"]["page_id"], PAGE_ID)
        self.assertNotIn("EAAx1", response.text)
        self.graph.accounts.pop()

        # Even if Meta's message carried the pasted token, it does not come back.
        self.graph.fail["debug_token"] = GraphError(
            400, 190, None, "OAuthException", f"Malformed access token {PASTED}", "trace1")
        response = await self.connect_page()
        detail = response.json()["detail"]
        self.assertEqual((response.status_code, detail["code"], detail["fields"]["meta"]["code"]),
                         (422, "TOKEN_INVALID", 190))
        self.assertIn("[redacted]", detail["fields"]["meta"]["message"])
        del self.graph.fail["debug_token"]

        # Nor does an unexpected failure's text reach the answer or the log.
        self.store.boom["save_connection"] = RuntimeError(f"boom {PASTED}")
        with captured_logs() as logs:
            response = await self.connect_page()
        self.assert_refused(response, 502, "CONNECT_FAILED")
        self.assertTrue(any("RuntimeError" in line for line in logs.lines), logs.lines)
        self.assert_no_secret(*logs.lines)

        failed = [row["detail"] for row in self.store.audits
                  if row["action"] == "messenger.connect_failed"]
        self.assertEqual([detail["code"] for detail in failed],
                         ["TOKEN_INVALID", "WRONG_APP", "MISSING_SCOPES", "MULTIPLE_PAGES",
                          "TOKEN_INVALID"])
        self.assert_no_secret(json.dumps(self.store.audits, ensure_ascii=False))

    async def test_before_connecting_the_meta_actions_say_so(self):
        for path in ("/subscribe", "/probe"):
            response = await self.fb("POST", path, {})
            with self.subTest(path=path):
                self.assert_refused(response, 409, "NOT_CONNECTED")
        response = await self.fb("POST", "/check")
        self.assertEqual(response.json(), {"tokens": []})
        self.assertEqual(self.graph.calls, [])

    async def test_webhook_repair_and_probe_report_meta_and_are_audited(self):
        await self.connect_page()
        self.store.audits.clear()
        response = await self.fb("POST", "/subscribe")
        self.assertEqual(response.json(), {"subscription": {
            "page_id": PAGE_ID, "page_resubscribed": False, "app_resubscribed": False}})
        self.graph.page_apps = []
        response = await self.fb("POST", "/subscribe")
        self.assertIs(response.json()["subscription"]["page_resubscribed"], True)
        self.graph.fail["page_subscriptions"] = GraphError(503, 2, None, "OAuthException",
                                                           "Service temporarily unavailable")
        response = await self.fb("POST", "/subscribe")
        detail = response.json()["detail"]
        self.assertEqual((response.status_code, detail["code"], detail["fields"]["meta"]["code"]),
                         (502, "SUBSCRIBE_FAILED", 2))
        self.assertEqual([(row["action"], row["detail"]["ok"]) for row in self.store.audits],
                         [("messenger.subscribe", True)] * 2 + [("messenger.subscribe", False)])

        response = await self.fb("POST", "/probe", {})
        self.assertEqual((response.json()["probe"]["result"], response.json()["probe"]["reason"]),
                         ("inconclusive", "no_candidate"))
        self.store.add_thread(PSID, last=ago(hours=30))
        self.graph.action_error = GraphError(400, 10, 2018278, "OAuthException", "window")
        response = await self.fb("POST", "/probe", {})
        probe = response.json()["probe"]
        self.assertEqual((probe["result"], probe["psid"]), ("passed", mask_psid(PSID)))
        self.assertEqual(self.graph.actions, [(PSID, "typing_on")])
        self.assert_refused(await self.fb("POST", "/probe", {"psid": "12ab"}), 422,
                            "INVALID_FIELDS", {"psid": "INVALID"})
        self.assertEqual(self.store.actions()[-2:], ["messenger.probe", "messenger.probe"])

    async def test_disconnect_needs_a_confirmation_and_switches_the_bot_off_first(self):
        await self.connect_page()
        self.store.row.update(bot_enabled=True, auto_reply_confirmed_off=True,
                              comments_enabled=True)
        self.store.audits.clear()
        for body in ({}, {"confirm": False}):
            response = await self.fb("POST", "/disconnect", body)
            self.assert_refused(response, 422, "CONFIRM_REQUIRED")
        self.assertIsNotNone(self.store.page_row)
        self.assertEqual(self.store.audits, [])
        response = await self.fb("POST", "/disconnect", {"confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["switches"]["bot_enabled"], body["switches"]["comments_enabled"]),
                         (False, False))
        self.assertEqual((body["disconnected"]["page_id"], body["disconnected"]["tokens_removed"],
                          body["disconnected"]["unsubscribed"]),
                         (PAGE_ID, ["page", "user"], None))
        self.assertEqual(self.store.actions(), ["messenger.bot_off", "messenger.comments_off",
                                                "messenger.disconnected"])
        self.assertEqual((self.store.page_row, self.store.sealed), (None, {}))
        self.assertNotIn("unsubscribe_page", self.graph.calls)
        # A reconnect finds the bot off: nothing waiting is answered.
        await self.connect_page()
        self.assertIs(self.store.row["bot_enabled"], False)

    async def test_jobs_are_queued_by_name_only(self):
        response = await self.fb("POST", "/jobs/names")
        self.assertEqual(response.json(), {"job": "names", "request_id": 1})
        self.assertEqual((self.store.audits[0]["action"], self.store.audits[0]["target"]),
                         ("messenger.job_requested", "names"))
        for job in ("everything", "NAMES", "names2", "page"):
            response = await self.fb("POST", f"/jobs/{job}")
            self.assertEqual(response.status_code, 422, job)
        self.assertEqual(len(self.store.requests), 1)

    def test_the_jobs_offered_are_exactly_the_ones_the_worker_runs(self):
        try:
            from automotive.messenger_worker import JOB_NAMES
        except ImportError as error:  # the worker is another module's; its absence is not ours
            self.skipTest(f"messenger_worker is not importable: {type(error).__name__}")
        self.assertEqual(JOBS, JOB_NAMES)


class TestModeRouteTests(ConnectionTests):
    """The main switch's test mode and the per-chat tester flag."""

    async def put_settings(self, **fields):
        return await self.fb("PUT", "/settings", {"version": self.store.row["version"],
                                                  **fields})

    async def test_test_mode_starts_without_the_auto_reply_tick_and_lists_the_testers(self):
        self.store.add_thread(PSID, name="Giorgi", last=ago(minutes=5), bot_tester=True)
        self.store.add_thread(OTHER_PSID, name="Nino", last=ago(minutes=3))
        response = await self.put_settings(bot_mode="test")
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["bot_mode"], switches["bot_state"], switches["bot_enabled"],
                          switches["bot_test_mode"]), ("test", "test", False, True))
        self.assertIsNotNone(switches["bot_test_at"])
        self.assertIsNone(switches["bot_enabled_at"])
        self.assertEqual(self.store.actions(), ["messenger.bot_test"])
        body = (await self.fb("GET", "/status")).json()
        self.assertEqual([(row["psid"], row["customer_name"]) for row in body["testers"]],
                         [(PSID, "Giorgi")])
        codes = [warning["code"] for warning in body["warnings"]]
        self.assertNotIn("test_no_testers", codes)
        self.assertIn("auto_reply_not_confirmed", codes)
        # Everyone still needs the tick; a refused body changes nothing.
        self.assert_refused(await self.put_settings(bot_mode="on"), 409,
                            "AUTO_REPLY_NOT_CONFIRMED")
        self.assertEqual(bot_mode(self.store.row), "test")
        response = await self.put_settings(bot_mode="on", auto_reply_confirmed_off=True)
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["bot_mode"], switches["bot_test_mode"]), ("on", False))
        self.assertIsNotNone(switches["bot_enabled_at"])
        # Back to testers only: nothing needs confirming, and the testers' start is kept.
        test_at = switches["bot_test_at"]
        response = await self.put_settings(bot_mode="test")
        self.assertEqual(response.json()["switches"]["bot_test_at"], test_at)
        # Test mode runs with Auto reply on: the tick is asked again before everyone.
        self.assertIs(response.json()["switches"]["auto_reply_confirmed_off"], False)
        self.assertEqual(self.store.actions(), ["messenger.bot_test", "messenger.bot_on",
                                                "messenger.bot_test"])

    async def test_off_from_a_stale_page_ends_test_mode_too(self):
        for stop in ({"bot_enabled": False}, {"bot_mode": "off"}):
            with self.subTest(stop=stop):
                await self.put_settings(bot_mode="test")
                self.store.row["version"] += 5  # changed meanwhile
                response = await self.fb("PUT", "/settings", {"version": 1, **stop})
                self.assertEqual(response.status_code, 200, response.text)
                switches = response.json()["switches"]
                self.assertEqual((switches["bot_state"], switches["bot_test_mode"]),
                                 ("off", False))
        # Anything else from a stale page is refused, test mode included.
        self.assert_refused(await self.fb("PUT", "/settings", {"version": 1, "bot_mode": "test"}),
                            409, "VERSION_CONFLICT")

    async def test_the_mode_is_said_one_way_with_a_known_value(self):
        for fields, codes in (({"bot_mode": "test", "bot_enabled": True}, {"bot_mode": "INVALID"}),
                              ({"bot_mode": "everyone"}, {"bot_mode": "INVALID"}),
                              ({"bot_mode": 1}, {"bot_mode": "INVALID"})):
            with self.subTest(fields=fields):
                response = await self.put_settings(**fields)
                self.assertEqual((response.status_code, response.json()), (422, {"detail": {
                    "code": "INVALID_FIELDS", "fields": codes}}))
        self.assertEqual((bot_mode(self.store.row), self.store.audits), ("off", []))

    async def test_test_mode_with_no_tester_warns_that_the_bot_answers_nobody(self):
        await self.put_settings(bot_mode="test")
        body = (await self.fb("GET", "/status")).json()
        self.assertEqual(body["testers"], [])
        self.assertEqual(body["warnings"][0]["code"], "test_no_testers")

    async def test_a_chat_is_marked_as_a_tester_audited_and_filtered(self):
        row = self.store.add_thread(PSID, name="Giorgi", last=ago(minutes=5))
        self.store.add_thread(OTHER_PSID, last=ago(minutes=3))
        response = await self.fb("PUT", f"/threads/{PSID}",
                                 {"bot_tester": True, "version": row["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["thread"]["bot_tester"], True)
        self.assertEqual(self.store.actions(), ["messenger.chat_tester_on"])
        body = (await self.fb("GET", "/threads", params={"filter": "tester"})).json()
        self.assertEqual([item["psid"] for item in body["threads"]], [PSID])
        self.assert_refused(await self.fb("PUT", f"/threads/{PSID}",
                                          {"bot_tester": False, "version": 1}),
                            409, "VERSION_CONFLICT")
        response = await self.fb("PUT", f"/threads/{PSID}", {"bot_tester": False})
        self.assertIs(response.json()["thread"]["bot_tester"], False)
        self.assertEqual(self.store.actions()[-1], "messenger.chat_tester_off")

    async def test_a_testers_bot_memory_is_cleared_with_a_confirmation_and_audited(self):
        row = self.store.add_thread(PSID, name="Giorgi", last=ago(minutes=5), bot_tester=True)
        self.store.add_thread(OTHER_PSID, last=ago(minutes=3))
        path = f"/threads/{PSID}/clear-context"
        self.assertIsNone((await self.fb("GET", f"/threads/{PSID}")).json()["thread"][
            "context_cleared_at"])
        for body in (None, {}, {"confirm": False}, {"confirm": "yes"}):
            self.assertEqual((await self.fb("POST", path, body)).status_code, 422, body)
        self.assert_refused(await self.fb("POST", path, {"confirm": True, "version": 9}),
                            409, "VERSION_CONFLICT")
        # Only a tester's chat: a customer's memory is never cleared from here.
        self.assert_refused(await self.fb("POST", f"/threads/{OTHER_PSID}/clear-context",
                                          {"confirm": True}), 409, "NOT_TESTER")
        self.assert_refused(await self.fb("POST", "/threads/7000000000009999/clear-context",
                                          {"confirm": True}), 404, "NOT_FOUND")
        self.assertEqual(self.store.actions(), [])
        response = await self.fb("POST", path, {"confirm": True, "version": row["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        thread = response.json()["thread"]
        self.assertEqual(thread["version"], 2)
        self.assertIsNotNone(thread["context_cleared_at"])
        self.assertEqual(self.store.actions(), ["messenger.chat_context_cleared"])
        # The chat page reads the stamp to draw the line the bot reads nothing above.
        self.assertEqual((await self.fb("GET", f"/threads/{PSID}")).json()["thread"][
            "context_cleared_at"], thread["context_cleared_at"])

    async def test_disconnect_ends_test_mode_first(self):
        await self.connect_page()
        await self.put_settings(bot_mode="test")
        response = await self.fb("POST", "/disconnect", {"confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["switches"]["bot_state"], "off")
        self.assertIs(self.store.row["bot_test_mode"], False)


class ChatTests(FacebookCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        first = self.store.add_thread(PSID, name="Giorgi", last=ago(minutes=10),
                                      needs_attention=True, attention_reason="fallback")
        self.store.add_message(PSID, "customer", "F30 B46 სანთლები გაქვთ?", ago(minutes=30))
        self.store.add_message(PSID, "bot", "დიახ, NGK — 45 ₾", ago(minutes=29))
        self.store.add_message(PSID, "shop", "მოგვწერეთ ნომერი", ago(minutes=20))
        self.store.add_message(PSID, "customer", "555 12 34 56", ago(minutes=10))
        self.first = first
        self.store.add_thread(OTHER_PSID, last=ago(hours=5), source="ads",
                              source_ad_id="120000000000001", bot_enabled=False)
        self.store.add_message(OTHER_PSID, "customer", "ფასი?", ago(hours=5))

    async def test_the_list_filters_and_pages(self):
        body = (await self.fb("GET", "/threads")).json()
        self.assertEqual([row["psid"] for row in body["threads"]], [PSID, OTHER_PSID])
        self.assertEqual((body["has_more"], body["filter"], body["offset"]), (False, "all", 0))
        first = body["threads"][0]
        self.assertEqual((first["customer_name"], first["last_sender"], first["last_body"],
                          first["needs_attention"]), ("Giorgi", "customer", "555 12 34 56", True))
        self.assertNotIn("engine_state", first)
        for kind, expected in (("attention", [PSID]), ("off", [OTHER_PSID]),
                               ("ads", [OTHER_PSID]), ("admin", [])):
            body = (await self.fb("GET", "/threads", params={"filter": kind})).json()
            self.assertEqual([row["psid"] for row in body["threads"]], expected, kind)
        body = (await self.fb("GET", "/threads", params={"limit": 1})).json()
        self.assertEqual(([row["psid"] for row in body["threads"]], body["has_more"]),
                         ([PSID], True))
        body = (await self.fb("GET", "/threads", params={"limit": 1, "offset": 1})).json()
        self.assertEqual(([row["psid"] for row in body["threads"]], body["has_more"]),
                         ([OTHER_PSID], False))
        for params in ({"filter": "everyone"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = await self.fb("GET", "/threads", params=params)
            self.assertEqual(response.status_code, 422, params)

    async def test_a_chat_reads_oldest_first_with_who_wrote_each_message(self):
        body = (await self.fb("GET", f"/threads/{PSID}")).json()
        self.assertEqual([(row["sender"], row["body"]) for row in body["messages"]], [
            ("customer", "F30 B46 სანთლები გაქვთ?"), ("bot", "დიახ, NGK — 45 ₾"),
            ("shop", "მოგვწერეთ ნომერი"), ("customer", "555 12 34 56")])
        self.assertEqual(set(body["messages"][0]), {"message_id", "sender", "body",
                                                    "attachments", "referral_ad_id", "sent_at",
                                                    "stored_at"})
        self.assertIs(body["has_older"], False)
        self.assertEqual(body["thread"]["attention_reason"], "fallback")
        body = (await self.fb("GET", f"/threads/{PSID}", params={"limit": 2})).json()
        self.assertEqual(([row["body"] for row in body["messages"]], body["has_older"]),
                         (["მოგვწერეთ ნომერი", "555 12 34 56"], True))
        before = body["messages"][0]["message_id"]
        body = (await self.fb("GET", f"/threads/{PSID}",
                              params={"limit": 2, "before": before})).json()
        self.assertEqual(([row["sender"] for row in body["messages"]], body["has_older"]),
                         (["customer", "bot"], False))
        self.assert_refused(await self.fb("GET", "/threads/7000000000009999"), 404, "NOT_FOUND")
        for path in ("/threads/12ab", "/threads/" + "1" * 31):
            self.assertEqual((await self.fb("GET", path)).status_code, 422, path)
        for before in (0, 2 ** 63):  # outside bigint never reaches the database
            response = await self.fb("GET", f"/threads/{PSID}", params={"before": before})
            self.assertEqual(response.status_code, 422, before)

    async def test_the_chat_switch_admin_flag_and_attention_are_audited(self):
        response = await self.fb("PUT", f"/threads/{PSID}", {"bot_enabled": False, "version": 1})
        self.assertEqual(response.json()["thread"]["bot_enabled"], False)
        response = await self.fb("PUT", f"/threads/{PSID}", {"is_admin": True,
                                                            "needs_attention": False})
        thread = response.json()["thread"]
        self.assertEqual((thread["is_admin"], thread["needs_attention"], thread["version"]),
                         (True, False, 3))
        self.assertEqual([(row["action"], row["target"], row["actor"])
                          for row in self.store.audits],
                         [("messenger.chat_off", "…1234", "boss"),
                          ("messenger.chat_admin_on", "…1234", "boss"),
                          ("messenger.attention_cleared", "…1234", "boss")])
        # Both switches at once, version carried from one to the next.
        response = await self.fb("PUT", f"/threads/{PSID}", {"bot_enabled": True,
                                                            "is_admin": False, "version": 3})
        self.assertEqual(response.json()["thread"]["version"], 5)
        # Unchanged values write nothing; a stale version, an empty body or a stranger fail.
        before = len(self.store.audits)
        response = await self.fb("PUT", f"/threads/{PSID}", {"bot_enabled": True})
        self.assertEqual((response.status_code, len(self.store.audits)), (200, before))
        self.assert_refused(await self.fb("PUT", f"/threads/{PSID}",
                                          {"bot_enabled": False, "version": 2}),
                            409, "VERSION_CONFLICT")
        self.assert_refused(await self.fb("PUT", f"/threads/{PSID}", {}), 422, "INVALID_FIELDS",
                            {"": "REQUIRED"})
        self.assert_refused(await self.fb("PUT", f"/threads/{PSID}", {"bot_enabled": 0}), 422,
                            "INVALID_FIELDS", {"bot_enabled": "INVALID"})
        self.assert_refused(await self.fb("PUT", "/threads/7000000000009999",
                                          {"bot_enabled": False}), 404, "NOT_FOUND")
        self.assertEqual(len(self.store.audits), before)
        response = await self.fb("PUT", f"/threads/{OTHER_PSID}", {"needs_attention": True})
        self.assertEqual(response.json()["thread"]["attention_reason"], "admin")

    async def test_deleting_a_customer_needs_a_confirmation_and_is_audited(self):
        for body in (None, {}, {"confirm": False}, {"confirm": "yes"}):
            response = await self.fb("DELETE", f"/threads/{PSID}", body)
            self.assertEqual(response.status_code, 422, body)
        self.assertIn(PSID, self.store.threads)
        response = await self.fb("DELETE", f"/threads/{PSID}", {"confirm": True})
        self.assertEqual(response.json(), {"deleted": {"threads": 1, "messages": 4,
                                                       "replies": 0, "inbox": None}})
        self.assertNotIn(PSID, self.store.threads)
        self.assertEqual([(row["action"], row["target"]) for row in self.store.audits],
                         [("messenger.customer_deleted", "…1234")])
        self.assert_refused(await self.fb("DELETE", f"/threads/{PSID}", {"confirm": True}),
                            404, "NOT_FOUND")


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class DatabaseRouteTests(FacebookCase):
    """The same routes on MetaStore as a NOLOGIN member of garage_site, in a database this
    class creates and drops: a query outside migration 0013's grants fails here as it would in
    production."""

    TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads",
              "messenger_messages", "messenger_replies", "meta_sync_runs", "meta_job_requests",
              "meta_comments", "meta_forgotten", "audit_log")

    @classmethod
    def setUpClass(cls) -> None:
        suffix = uuid.uuid4().hex[:12]
        cls.admin_db = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.cluster_roles: list[str] = []
        cls.database = "meta_admin_" + suffix
        cls.admin_db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls._drop_everything)
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.database
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls._role(f"meta_admin_{key}_{suffix}", capability)
                     for key, capability in (("site", "garage_site"),
                                             ("worker", "garage_messenger"),
                                             ("hook", "garage_webhook"))}
        cipher = TokenCipher(SETTINGS.token_key)
        cls.site_store = MetaStore(RoleDatabase(cls.url, cls.roles["site"]), cipher)
        cls.worker_store = MetaStore(RoleDatabase(cls.url, cls.roles["worker"]), cipher)

    @classmethod
    def _role(cls, name: str, capability: str) -> str:
        cls.admin_db.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name)))
        cls.cluster_roles.append(name)
        cls.admin_db.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability),
                                                             sql.Identifier(name)))
        cls.admin_db.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(name)))
        return name

    @classmethod
    def _drop_everything(cls) -> None:
        cls.admin_db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(cls.database)))
        for name in reversed(cls.cluster_roles):
            cls.admin_db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin_db.close()

    def make_store(self):
        return self.site_store

    async def asyncSetUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in self.TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")
        await super().asyncSetUp()

    def admin_sql(self, statement: str, parameters: tuple = ()) -> list:
        with psycopg.connect(self.url) as connection:
            return connection.execute(statement, parameters).fetchall()

    async def test_every_route_stays_within_the_site_logins_grants(self):
        body = (await self.fb("GET", "/status")).json()
        self.assertEqual([warning["code"] for warning in body["warnings"]],
                         ["not_connected", "auto_reply_not_confirmed"])
        version = body["switches"]["version"]
        self.assert_refused(await self.fb("PUT", "/settings", {"version": version,
                                                              "bot_enabled": True}),
                            409, "AUTO_REPLY_NOT_CONFIRMED")
        response = await self.fb("PUT", "/settings", {
            "version": version, "bot_enabled": True, "auto_reply_confirmed_off": True,
            "daily_turn_limit": 90, "comments_enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["bot_state"], switches["daily_turn_limit"],
                          switches["comments_enabled"], switches["version"]),
                         ("on", 90, True, version + 3))
        self.assertIsNotNone(switches["bot_enabled_at"])

        # A webhook item appended by the receiver's role shows as bookkeeping only.
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles["hook"])))
            connection.execute(
                "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                "VALUES (%s, 'messages', %s, %s)",
                ("msg:" + "a" * 64, PAGE_ID, Jsonb({"sender": {"id": PSID}, "text": "secret"})))
        # Chats as the worker stores them.
        worker = self.worker_store
        worker.add_message(PSID, "m_1", "customer", ago(minutes=50), page_id=PAGE_ID,
                           body="F30 B46 სანთლები")
        worker.add_message(PSID, "m_2", "bot", ago(minutes=49), page_id=PAGE_ID, body="დიახ")
        worker.flag_attention(PSID, "fallback")
        status = await self.fb("GET", "/status")
        self.assertEqual(status.status_code, 200, status.text)
        events = status.json()["events"]
        self.assertEqual((len(events), events[0]["field"]), (1, "messages"))
        self.assertNotIn("secret", status.text)
        self.assertLessEqual({"webhooks", "customer_messages", "turns", "cost_usd"},
                             set(status.json()["today"]))

        body = (await self.fb("GET", "/threads", params={"filter": "attention"})).json()
        self.assertEqual([row["psid"] for row in body["threads"]], [PSID])
        body = (await self.fb("GET", f"/threads/{PSID}")).json()
        self.assertEqual([row["sender"] for row in body["messages"]], ["customer", "bot"])
        response = await self.fb("PUT", f"/threads/{PSID}", {
            "bot_enabled": False, "is_admin": True, "needs_attention": False,
            "version": body["thread"]["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["thread"]["bot_enabled"],
                          response.json()["thread"]["is_admin"]), (False, True))
        response = await self.fb("POST", "/jobs/names")
        self.assertEqual(response.json()["job"], "names")
        await self.fb("POST", "/resume")

        response = await self.fb("POST", "/connect", {"user_token": PASTED})
        self.assertEqual(response.status_code, 200, response.text)
        sealed = self.admin_sql("SELECT purpose, token_enc FROM garage.meta_tokens "
                                "ORDER BY purpose")
        self.assertEqual([row[0] for row in sealed], ["page", "user"])
        self.assert_no_secret(*(row[1] for row in sealed))
        checks = [await self.fb("GET", "/status"), await self.fb("POST", "/check"),
                  await self.fb("POST", "/subscribe"), await self.fb("POST", "/probe", {})]
        for response in checks:
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_no_secret(response.text)
        self.assertEqual(checks[0].json()["page"]["page_id"], PAGE_ID)

        response = await self.fb("DELETE", f"/threads/{PSID}", {"confirm": True})
        self.assertEqual(response.json()["deleted"]["threads"], 1)
        response = await self.fb("POST", "/disconnect", {"confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["switches"]["bot_enabled"],
                          response.json()["switches"]["comments_enabled"]), (False, False))
        self.assertEqual(self.admin_sql("SELECT count(*) FROM garage.meta_tokens"), [(0,)])

        actions = [row[0] for row in self.admin_sql(
            "SELECT action FROM garage.audit_log WHERE actor_username = 'boss' "
            "ORDER BY audit_id")]
        self.assertEqual(actions, [
            "messenger.settings_changed", "messenger.bot_on", "messenger.comments_on",
            "messenger.chat_off", "messenger.chat_admin_on", "messenger.attention_cleared",
            "messenger.job_requested", "messenger.connected", "messenger.tokens_checked",
            "messenger.subscribe", "messenger.probe", "messenger.customer_deleted",
            "messenger.bot_off", "messenger.comments_off", "messenger.disconnected"])
        details = json.dumps(self.admin_sql("SELECT detail::text FROM garage.audit_log"),
                             ensure_ascii=False)
        self.assert_no_secret(details)


if __name__ == "__main__":
    unittest.main()
