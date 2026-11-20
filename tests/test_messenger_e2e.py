"""Garage on Facebook end to end, on loopback and at $0.

A delivery signed as Meta signs it reaches the real receiver over HTTP, the real worker answers
it through the real Graph client, and nothing leaves the machine:

- PostgreSQL: a database this file creates on WAREHOUSE_TEST_ADMIN_DATABASE_URL, with the roles
  script and every migration, and one LOGIN role per process, each a member of one capability
  role only: the receiver's of garage_webhook, the worker's of garage_messenger, and the admin's
  dashboard actions' of garage_site. Every query runs inside migration 0013's grants, as in
  production. The whole file skips without that URL.
- The receiver: ``create_receiver_app`` with a throwaway app secret and its own database inbox,
  served by uvicorn on an ephemeral loopback port with the options its ``main()`` uses.
- Meta: a fake Graph API on loopback. It accepts only this run's tokens with the right
  ``appsecret_proof``, records every call, and answers from fixtures; a path without one is
  recorded and fails the test. The worker's ``GraphClient`` is the production class with its
  fixed https://graph.facebook.com prefix; only its requests session sends that prefix to the
  fake, and refuses any other address. While a test runs, an audit hook also refuses every
  socket connection and name lookup outside loopback.
- The engine: a fake with ``MessengerEngine``'s interface. No model, no catalogue, no bill.

The worker runs with its own threads (LISTEN plus a short poll, the burst scheduler, the turn,
command and comment pools), so a negative ("nothing was sent") is asserted only once the reply
attempt or inbox row it concerns has reached its final state, never after a sleep.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import itertools
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import psycopg
import requests
import uvicorn
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from automotive.messenger_engine import EngineResult, clean_state
from automotive.messenger_render import bot_switched_text
from automotive.messenger_worker import JOB_NAMES, MessengerWorker
from automotive.meta_config import GRAPH_HOST, SUBSCRIBED_FIELDS, WEBHOOK_PATH, MetaSettings
from automotive.meta_context import build_context
from automotive.meta_crypto import TokenCipher
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphClient, appsecret_proof
from automotive.meta_receiver import create_receiver_app
from automotive.meta_store import MetaStore
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
VERSION = "v26.0"
APP, PAGE = "1234567890123", "61595318832800"
PSID, ADMIN_PSID, COMMENTER = "7000000000001234", "7000000000009999", "7000000000004321"
AUTHOR = "5550000000000001"  # the commenter as the feed names them (not a page-scoped id)
POST = f"{PAGE}_1000000001"
# Made for this run only; none of these is a credential anywhere.
SECRET = uuid.uuid4().hex
VERIFY = uuid.uuid4().hex + uuid.uuid4().hex
KEY = os.urandom(32)
PAGE_TOKEN = "EAAe2ePageToken" + uuid.uuid4().hex
APP_TOKEN = f"{APP}|{SECRET}"
ACTOR = "e2e-admin"
QUIET = 1.0  # seconds; MESSENGER_QUIET_SECONDS is 5 in production
WAIT = 20.0
PHONE = "+995 596 33 33 74"
ANSWER = "გვაქვს: Camry 2015-ის წინა ხუნდები, 120 ₾."
VIEW = {"answer": ANSWER, "note": "", "cards": [], "groups": [], "total": 0, "question": None,
        "suggestions": [], "language": "ka"}
USAGE = {"model_requests": 1, "input_tokens": 1200, "output_tokens": 150, "cost_usd": 0.0123}
POST_TEXT = "ახალი ზეთები და ფილტრები ყველა მოდელისთვის."
TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads", "messenger_messages",
          "messenger_replies", "meta_comments", "meta_posts", "meta_page_daily",
          "meta_sync_runs", "meta_job_requests", "meta_forgotten", "audit_log")


# -- nothing leaves loopback ---------------------------------------------------------------------

_GUARD = threading.Event()
_BLOCKED: list[str] = []
_HOOKED: list[bool] = []


def _loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _loopback_only(event: str, args: tuple) -> None:
    """While an end-to-end test runs, no socket may reach or even resolve anything off this
    host: a Graph, OpenRouter or Anthropic call that slipped past the fakes fails here."""
    if not _GUARD.is_set() or event not in ("socket.connect", "socket.getaddrinfo"):
        return
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple):  # a Unix socket path
            return
        host = address[0]
    else:
        host = args[0]
    if not _loopback(host):
        _BLOCKED.append(str(host))
        raise PermissionError("an end-to-end test may only use loopback")


def _install_guard() -> None:
    """Audit hooks cannot be removed, so the hook is added once and acts only while _GUARD is
    set, during this file's tests."""
    if not _HOOKED:
        sys.addaudithook(_loopback_only)
        _HOOKED.append(True)


# -- a fake Graph API ----------------------------------------------------------------------------


def _error(code: int, message: str) -> dict:
    return {"error": {"message": message, "type": "OAuthException", "code": code,
                      "fbtrace_id": "e2e"}}


@dataclass(frozen=True)
class Call:
    method: str
    path: str  # after /v26.0/
    values: dict
    token: str  # which of this run's tokens: page, app, or unknown
    at: float  # time.monotonic(), the worker's own clock


class FakeGraph:
    """Meta's Graph API as far as the worker and its jobs use it, on a loopback port."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self.lock = threading.Lock()
        self.calls: list[Call] = []
        self.unknown: list[str] = []
        self.refused: list[str] = []
        self.mids = itertools.count(1)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base = ""
        # What the page's one conversation holds (the catch-up job reads it back).
        self.conversation_updated = "2026-11-11T10:00:00+0000"
        self.conversation_messages: list[dict] = []

    def start(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GraphHandler)
        self.server.graph = self  # type: ignore[attr-defined]
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={
            "poll_interval": 0.1}, name="e2e-graph", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(5)

    def clear(self) -> None:
        with self.lock:
            self.calls.clear()
            self.unknown.clear()
            self.refused.clear()
            self.mids = itertools.count(1)
            self.conversation_updated = "2026-11-11T10:00:00+0000"
            self.conversation_messages = []

    def answer(self, method: str, raw_path: str, values: dict) -> tuple[int, dict]:
        prefix = f"/{VERSION}/"
        path = raw_path.removeprefix(prefix)
        token = values.pop("access_token", "")
        proof = values.pop("appsecret_proof", None)
        kind = {PAGE_TOKEN: "page", APP_TOKEN: "app"}.get(token, "unknown")
        with self.lock:
            self.calls.append(Call(method, path, dict(values), kind, time.monotonic()))
        if kind == "unknown":
            self._note(self.refused, f"{method} {path}: token")
            return 400, _error(190, "Invalid OAuth access token.")
        # Every token here is minted through our app, so every call must carry its proof.
        if proof is None or not hmac.compare_digest(proof, appsecret_proof(token, SECRET)):
            self._note(self.refused, f"{method} {path}: appsecret_proof")
            return 400, _error(100, "Invalid appsecret_proof provided in the API argument")
        found = self._route(method, path, values) if raw_path.startswith(prefix) else None
        if found is None:
            self._note(self.unknown, f"{method} {path}")
            return 400, _error(100, "Unknown path components")
        return 200, found

    def _note(self, into: list[str], what: str) -> None:
        with self.lock:
            into.append(what)

    def _route(self, method: str, path: str, values: dict) -> dict | None:
        if method == "POST" and path == f"{PAGE}/messages":
            return self._send(values)
        if method != "GET":
            return None
        if path == "debug_token":
            return {"data": {
                "app_id": APP, "type": "PAGE", "application": "GARAGE 1886",
                "is_valid": values.get("input_token") == PAGE_TOKEN, "profile_id": PAGE,
                "scopes": ["pages_messaging", "pages_show_list", "pages_manage_metadata"],
                "expires_at": 0, "data_access_expires_at": int(time.time()) + 80 * 86400}}
        if path == f"{PAGE}/subscribed_apps":
            return {"data": [{"id": APP, "name": "GARAGE 1886",
                              "subscribed_fields": list(SUBSCRIBED_FIELDS)}]}
        if path == f"{APP}/subscriptions":
            return {"data": [{"object": "page", "callback_url": self.webhook_url,
                              "active": True, "fields": [{"name": name, "version": VERSION}
                                                         for name in SUBSCRIBED_FIELDS]}]}
        if path == f"{PAGE}/conversations":
            return {"data": [{"id": "t_10001", "updated_time": self.conversation_updated,
                              "participants": {"data": [
                                  {"id": PSID, "name": "ნინო"},
                                  {"id": PAGE, "name": "Garage • ფარეხი 1886"}]}}]}
        if path == "t_10001/messages":
            return {"data": list(self.conversation_messages)}
        if path == PAGE:
            return {"id": PAGE, "name": "Garage • ფარეხი 1886", "followers_count": 157,
                    "fan_count": 150}
        if path == POST:
            return {"id": POST, "message": POST_TEXT, "created_time": "2026-11-10T09:00:00+0000",
                    "permalink_url": f"https://www.facebook.com/{PAGE}/posts/1000000001"}
        if path in (f"{PAGE}/posts", f"{PAGE}/insights"):
            return {"data": []}
        return None

    def _send(self, values: dict) -> dict:
        """The Send API: a typing indicator, a message to a chat, or a private reply to a
        comment (Meta answers with the commenter's page-scoped id)."""
        recipient = json.loads(values.get("recipient") or "{}")
        if "sender_action" in values:
            return {"recipient_id": recipient.get("id", "")}
        message_id = f"m_e2e_{next(self.mids)}"
        if "comment_id" in recipient:
            return {"recipient_id": COMMENTER, "message_id": message_id}
        return {"recipient_id": recipient.get("id", ""), "message_id": message_id}

    # -- what the tests read -------------------------------------------------------------------

    def _send_calls(self) -> list[tuple[Call, dict]]:
        with self.lock:
            calls = list(self.calls)
        return [(call, json.loads(call.values.get("recipient") or "{}")) for call in calls
                if call.method == "POST" and call.path == f"{PAGE}/messages"]

    def sends(self, *, psid: str | None = None, comment_id: str | None = None) -> list[dict]:
        """Messages that went out (typing indicators and mark_seen are not messages)."""
        found = []
        for call, recipient in self._send_calls():
            if "message" not in call.values:
                continue
            if psid is not None and recipient.get("id") != psid:
                continue
            if comment_id is not None and recipient.get("comment_id") != comment_id:
                continue
            found.append({"recipient": recipient, "message": json.loads(call.values["message"]),
                          "messaging_type": call.values.get("messaging_type"), "at": call.at})
        return found

    def actions(self, psid: str) -> list[str]:
        return [call.values["sender_action"] for call, recipient in self._send_calls()
                if "sender_action" in call.values and recipient.get("id") == psid]

    def calls_to(self, psid: str) -> list[Call]:
        """Every Send API call addressed to ``psid``, typing indicators included."""
        return [call for call, recipient in self._send_calls() if recipient.get("id") == psid]

    def reads(self, path: str) -> list[Call]:
        with self.lock:
            return [call for call in self.calls if call.method == "GET" and call.path == path]


class _GraphHandler(BaseHTTPRequestHandler):
    """Hands each request to the FakeGraph of its server, form or query alike; logs nothing."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_DELETE(self) -> None:
        self._answer("DELETE")

    def _answer(self, method: str) -> None:
        parts = urlsplit(self.path)
        values = dict(parse_qsl(parts.query, keep_blank_values=True))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            values.update(parse_qsl(self.rfile.read(length).decode("utf-8"),
                                    keep_blank_values=True))
        status, document = self.server.graph.answer(method, parts.path, values)
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeGraphSession(requests.Session):
    """The worker's GraphClient session: the client still builds every URL on the fixed
    https://graph.facebook.com prefix and checks it; this sends that prefix to the fake and
    refuses any other address before a connection is tried."""

    def __init__(self, base: str) -> None:
        super().__init__()
        self.trust_env = False
        self.base = base

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> requests.Response:
        if not isinstance(url, str) or not url.startswith(GRAPH_HOST + "/"):
            raise AssertionError("the worker may only call the Graph API")
        return super().request(method, self.base + url[len(GRAPH_HOST):], *args, **kwargs)


# -- the receiver, served ------------------------------------------------------------------------


class _Served:
    """An ASGI app on uvicorn in a thread of this process, on an ephemeral loopback port, with
    the receiver main()'s options."""

    def __init__(self, app: Any) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        config = uvicorn.Config(app, loop="asyncio", http="h11", ws="none", lifespan="off",
                                log_config=None, access_log=False, proxy_headers=False,
                                server_header=False, limit_concurrency=64, timeout_keep_alive=5,
                                timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]},
                                       name="e2e-receiver", daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the receiver did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()


# -- a fake engine -------------------------------------------------------------------------------


class FakeEngine:
    """``MessengerEngine``'s interface without a model: every turn answers ANSWER. ``during``
    runs after the "model" answered and before the result goes back, where an admin's switch
    can change."""

    ready = True

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.during: Callable[[], Any] | None = None

    def prepare(self) -> bool:
        return False

    def shop_phone(self) -> str:
        return PHONE

    def respond(self, state: Any, text: Any, *, choice: int | None = None,
                language: str = "auto", timeout: float = 60.0,
                cancel: threading.Event | None = None) -> EngineResult:
        with self.lock:
            self.calls.append({"state": state, "text": text, "choice": choice,
                               "language": language})
        if self.during is not None:
            self.during()
        prior = clean_state(state)
        unsent = clean_state({**prior, "history": [
            *prior["history"], {"question": text, "answer": "", "shown": []}]})
        answered = clean_state({**prior, "language": "ka", "history": [
            *prior["history"], {"question": text, "answer": ANSWER, "shown": []}]})
        return EngineResult(status="ok", public=dict(VIEW), state=answered, engine_status="OK",
                            language="ka", usage=dict(USAGE), error_code="",
                            unsent_state=unsent)


def _no_job(ctx: Any) -> int:
    return 0


class _Lines(logging.Handler):
    """Every log line the receiver, the worker and the HTTP library write during a test, as a
    handler would print it (a traceback included), so a test can show none of them carries a
    token, a secret or a customer's words."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


# -- the tests -----------------------------------------------------------------------------------


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class MessengerEndToEndTests(unittest.TestCase):
    """Receiver, worker and dashboard logins on one database; Meta and the engine faked."""

    @classmethod
    def setUpClass(cls) -> None:
        _install_guard()
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop)
        name = "meta_e2e_" + cls.suffix
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cls.databases.append(name)
        cls.url = cls._conninfo(name)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        logins = {key: cls._login(key, capability) for key, capability in (
            ("hook", "garage_webhook"), ("worker", "garage_messenger"), ("site", "garage_site"))}
        cls.settings = MetaSettings(
            app_id=APP, app_secret=SECRET, verify_token=VERIFY, token_key=KEY,
            graph_version=VERSION, public_url="https://garage-e2e.invalid",
            receiver_database_url=logins["hook"], database_url=logins["worker"])
        cls.site_url = logins["site"]
        cls.graph = FakeGraph(cls.settings.webhook_url)
        cls.graph.start()
        cls.addClassCleanup(cls.graph.stop)
        cls.receiver = _Served(create_receiver_app(cls.settings))
        cls.receiver.start()
        cls.addClassCleanup(cls.receiver.stop)
        cls.http = requests.Session()
        cls.http.trust_env = False
        cls.addClassCleanup(cls.http.close)

    @classmethod
    def _drop(cls) -> None:
        for name in reversed(cls.databases):
            cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        for name in reversed(cls.cluster_roles):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin.close()

    @classmethod
    def _conninfo(cls, database: str, **login: str) -> str:
        info = conninfo_to_dict(ADMIN_URL)
        info.update(dbname=database, **login)
        return make_conninfo(**info)

    @classmethod
    def _login(cls, key: str, capability: str) -> str:
        """A LOGIN role that is a member of ``capability`` only; the URL its process uses."""
        name, password = f"e2e_{key}_{cls.suffix}", uuid.uuid4().hex
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name), sql.Literal(password)))
        cls.cluster_roles.append(name)
        cls.admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability),
                                                           sql.Identifier(name)))
        return cls._conninfo(cls.databases[-1], user=name, password=password)

    def setUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")
        # The dashboard's login stores the connection, as Connect does after Meta's checks.
        self.site = MetaStore(MetaDatabase(self.site_url, application_name="garage-e2e-site"),
                              TokenCipher(KEY))
        self.site.save_connection(PAGE, "Garage • ფარეხი 1886", APP, {"page": {
            "token": PAGE_TOKEN, "subject_id": PAGE, "subject_name": "Garage • ფარეხი 1886",
            "scopes": ["pages_messaging", "pages_show_list", "pages_manage_metadata"],
            "expires_at": None,
            "data_access_expires_at": datetime.now(UTC) + timedelta(days=80)}}, ACTOR)
        self.graph.clear()
        _BLOCKED.clear()
        self.said: list[str] = []
        self.addCleanup(self._check_traffic)
        _GUARD.set()
        self.addCleanup(_GUARD.clear)
        # Every log line, DEBUG included: urllib3 prints each request line with its query
        # string, where a Graph read carries the token (meta_graph filters it out).
        self.logs = _Lines()
        for name in ("automotive", "urllib3", "uvicorn"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            self.addCleanup(logger.removeHandler, self.logs)
            logger.addHandler(self.logs)
            logger.setLevel(logging.DEBUG)
        self.engine = FakeEngine()
        client = GraphClient(self.settings, session=_FakeGraphSession(self.graph.base),
                             sleep=lambda seconds: None)
        self.ctx = build_context(self.settings, self.settings.database_url, graph=client,
                                 application_name="garage-messenger-e2e")
        self.worker = MessengerWorker(self.ctx, self.engine, quiet_seconds=QUIET,
                                      max_wait_seconds=10, poll_seconds=0.2,
                                      jobs={name: _no_job for name in JOB_NAMES})
        self.worker.start()
        self.addCleanup(self.worker.stop, 15)

    def _check_traffic(self) -> None:
        self.assertEqual(_BLOCKED, [], "a connection tried to leave loopback")
        self.assertEqual(self.graph.unknown, [], "Graph calls without a fixture")
        self.assertEqual(self.graph.refused, [], "Graph calls with a wrong token or proof")
        private = {"the page token": PAGE_TOKEN, "the app secret": SECRET,
                   "the verify token": VERIFY,
                   "the page token's proof": appsecret_proof(PAGE_TOKEN, SECRET),
                   "the app token's proof": appsecret_proof(APP_TOKEN, SECRET),
                   "the answer": ANSWER, "the post": POST_TEXT,
                   **{f"customer id {psid[-4:]}": psid for psid in (PSID, ADMIN_PSID, COMMENTER)},
                   **{f"customer words {index}": text for index, text in enumerate(self.said)}}
        # The test's own requests to the receiver are Meta's side (the handshake's query holds
        # the verify token), logged by urllib3 in this process; the system's lines are the rest.
        lines = [line for line in self.logs.lines if self.receiver.url not in line]
        leaked = sorted({name for line in lines for name, value in private.items()
                         if value in line})
        self.assertEqual(leaked, [], "secrets or customer data in a log line")

    # -- helpers ------------------------------------------------------------------------------

    def rows(self, statement: str, parameters: tuple | None = None) -> list[dict]:
        with psycopg.connect(self.url, row_factory=dict_row) as connection:
            return connection.execute(statement, parameters).fetchall()

    def execute(self, statement: str) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(statement)

    def bot_on(self) -> None:
        """The admin's switch with Auto reply confirmed off, as the dashboard sets it; stamped
        a minute back so a message written now is plainly after it."""
        self.site.set_bot(True, ACTOR, auto_reply_confirmed=True)
        self.execute("UPDATE garage.meta_settings "
                     "SET bot_enabled_at = now() - interval '1 minute'")

    @staticmethod
    def body(*items: dict, field: str = "messaging") -> bytes:
        entry = {"id": PAGE, "time": int(time.time() * 1000), field: list(items)}
        return json.dumps({"object": "page", "entry": [entry]}, ensure_ascii=False).encode()

    def message(self, text: str, *, mid: str, psid: str = PSID,
                sent: datetime | None = None) -> dict:
        self.said.append(text)
        when = sent or datetime.now(UTC)
        return {"sender": {"id": psid}, "recipient": {"id": PAGE},
                "timestamp": int(when.timestamp() * 1000), "message": {"mid": mid, "text": text}}

    @staticmethod
    def echo(text: str, *, mid: str, psid: str, app_id: str) -> dict:
        return {"sender": {"id": PAGE}, "recipient": {"id": psid},
                "timestamp": int(time.time() * 1000),
                "message": {"mid": mid, "is_echo": True, "app_id": int(app_id), "text": text}}

    def feed(self, comment_id: str, text: str) -> bytes:
        self.said.append(text)
        change = {"field": "feed", "value": {
            "item": "comment", "verb": "add", "comment_id": comment_id, "post_id": POST,
            "parent_id": POST, "from": {"id": AUTHOR, "name": "ნინო"}, "message": text,
            "created_time": int(time.time())}}
        return self.body(change, field="changes")

    def post(self, body: bytes, *, signature: str | None = None,
             signed: bool = True) -> requests.Response:
        """A delivery to the receiver, signed with the app secret as Meta signs it, unless a
        ``signature`` is given or ``signed`` is false (no signature header at all)."""
        headers = {"Content-Type": "application/json"}
        if signature is None and signed:
            signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.http.post(self.receiver.url + WEBHOOK_PATH, data=body, headers=headers,
                              timeout=10)

    def say(self, text: str, *, mid: str, psid: str = PSID,
            sent: datetime | None = None) -> None:
        response = self.post(self.body(self.message(text, mid=mid, psid=psid, sent=sent)))
        self.assertEqual((response.status_code, response.text), (200, "EVENT_RECEIVED"))

    def wait_for(self, check: Callable[[], Any], what: str) -> Any:
        deadline = time.monotonic() + WAIT
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.05)

    def wait_reply(self, channel: str, trigger: str) -> dict:
        """The reply attempt for ``trigger`` once it is final: every send it made is done."""
        def final() -> dict | None:
            found = self.rows(
                "SELECT status, engine_status, psid, sent_mids, cost_usd::float8 AS cost_usd "
                "FROM garage.messenger_replies WHERE channel = %s AND trigger_key = %s",
                (channel, trigger))
            return found[0] if found and found[0]["status"] != "pending" else None
        return self.wait_for(final, f"the {channel} reply to {trigger}")

    def wait_outcomes(self, count: int) -> list[str]:
        """The outcomes of the inbox rows once all ``count`` of them are handled."""
        def handled() -> list[str] | None:
            found = self.rows("SELECT processed_at, outcome FROM garage.meta_inbox "
                              "ORDER BY inbox_id")
            if len(found) != count or any(row["processed_at"] is None
                                          or row["outcome"] == "claimed" for row in found):
                return None
            return [row["outcome"] for row in found]
        return self.wait_for(handled, f"{count} handled inbox rows")

    # -- the bot ------------------------------------------------------------------------------

    def test_a_signed_message_is_answered_once_after_the_quiet_window(self):
        self.bot_on()
        body = self.body(self.message("Camry 2015-ის წინა ხუნდები გაქვთ?", mid="m_c1"))
        posted = time.monotonic()
        response = self.post(body)
        self.assertEqual((response.status_code, response.text), (200, "EVENT_RECEIVED"))
        reply = self.wait_reply("messenger", "m_c1")
        self.assertEqual(
            (reply["status"], reply["engine_status"], reply["sent_mids"], reply["cost_usd"]),
            ("sent", "OK", ["m_e2e_1"], 0.0123))
        (sent,) = self.graph.sends()
        self.assertEqual((sent["recipient"], sent["message"], sent["messaging_type"]),
                         ({"id": PSID}, {"text": ANSWER}, "RESPONSE"))
        self.assertEqual(self.graph.actions(PSID), ["mark_seen", "typing_on"])
        # Nothing at all went to the customer before the quiet window ended.
        first = min(call.at for call in self.graph.calls_to(PSID))
        self.assertGreaterEqual(first - posted, QUIET)
        self.assertEqual([(call["state"], call["text"]) for call in self.engine.calls],
                         [(None, "Camry 2015-ის წინა ხუნდები გაქვთ?")])
        # Meta retrying the same delivery is the same event: stored once, answered once.
        self.assertEqual(self.post(body).status_code, 200)
        # Meta's echo of the bot's own message is the bot's, never the shop's or a new turn.
        self.assertEqual(self.post(self.body(self.echo(
            ANSWER, mid="m_e2e_1", psid=PSID, app_id=APP))).status_code, 200)
        self.assertEqual(self.wait_outcomes(2), ["stored", "duplicate"])
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT mid, sender, body, app_id FROM garage.messenger_messages "
            "ORDER BY message_id")],
            [("m_c1", "customer", "Camry 2015-ის წინა ხუნდები გაქვთ?", ""),
             ("m_e2e_1", "bot", ANSWER, APP)])
        self.assertEqual(len(self.graph.sends()), 1)
        self.assertEqual(self.site.turns_today(), 1)
        state = self.rows("SELECT engine_state FROM garage.messenger_threads")[0]["engine_state"]
        self.assertEqual(state["history"][-1]["answer"], ANSWER)
        # The log names the chat by its last four digits only (the cleanup checks the rest).
        self.assertIn("messenger_reply psid=…1234 kind=turn status=sent parts=1 engine=OK",
                      "\n".join(self.logs.lines))

    def test_with_the_bot_switch_off_nothing_is_sent(self):
        # The bot ships off; a customer writes all the same.
        self.say("ზეთის ფილტრი გაქვთ?", mid="m_off")
        reply = self.wait_reply("messenger", "m_off")
        self.assertEqual((reply["status"], reply["engine_status"]), ("skipped_off", ""))
        self.assertEqual(self.wait_outcomes(1), ["stored"])
        self.assertEqual((self.graph.calls_to(PSID), self.engine.calls), ([], []))
        self.assertEqual(self.site.turns_today(), 0)

    def test_switching_the_bot_off_between_the_answer_and_the_send_sends_nothing(self):
        self.bot_on()
        self.engine.during = lambda: self.site.set_bot(False, ACTOR)
        self.say("NGK სანთლები?", mid="m_mid_turn")
        reply = self.wait_reply("messenger", "m_mid_turn")
        self.assertEqual((reply["status"], reply["engine_status"]), ("skipped_off", "OK"))
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.graph.sends(), [])  # typing went first; no message did
        self.assertEqual(self.site.turns_today(), 1)  # the engine ran: the turn counts

    def test_a_message_written_before_the_bot_was_switched_on_is_never_answered(self):
        self.bot_on()  # a minute ago
        self.say("ამორტიზატორი გაქვთ?", mid="m_backlog",
                 sent=datetime.now(UTC) - timedelta(minutes=10))
        reply = self.wait_reply("messenger", "m_backlog")
        self.assertEqual((reply["status"], reply["engine_status"]), ("skipped_off", ""))
        self.assertEqual((self.graph.calls_to(PSID), self.engine.calls), ([], []))
        # The customer's next message, written now, is answered: once.
        self.say("Prius 2012-ზე", mid="m_now")
        self.assertEqual(self.wait_reply("messenger", "m_now")["status"], "sent")
        self.assertEqual(len(self.graph.sends(psid=PSID)), 1)
        self.assertEqual(self.rows("SELECT status FROM garage.messenger_replies "
                                   "WHERE trigger_key = 'm_backlog'"),
                         [{"status": "skipped_off"}])

    def test_bot_off_from_the_admins_own_chat_turns_the_bot_off(self):
        self.bot_on()
        self.say("გამარჯობა", psid=ADMIN_PSID, mid="m_admin_1")
        self.assertEqual(self.wait_reply("messenger", "m_admin_1")["status"], "sent")
        self.site.set_admin(ADMIN_PSID, True, ACTOR)  # the dashboard marks the owner's chat
        self.say("#bot off", psid=ADMIN_PSID, mid="m_admin_2")
        self.assertEqual(self.wait_reply("command", "m_admin_2")["status"], "sent")
        self.assertFalse(self.site.settings()["bot_enabled"])
        self.assertEqual(self.graph.sends(psid=ADMIN_PSID)[-1]["message"],
                         {"text": bot_switched_text("ka", False)})
        self.assertEqual(len(self.engine.calls), 1)  # the command never reached the engine
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT actor_username, action, target FROM garage.audit_log "
            "WHERE actor_username LIKE 'messenger:%' ORDER BY audit_id")],
            [("messenger:9999", "messenger.bot_off", "settings")])
        # From now on customers are left to the shop.
        self.say("ზეთი 5W-30?", mid="m_after")
        self.assertEqual(self.wait_reply("messenger", "m_after")["status"], "skipped_off")
        self.assertEqual(self.graph.calls_to(PSID), [])

    # -- comments -----------------------------------------------------------------------------

    def test_a_feed_comment_gets_one_private_reply(self):
        self.site.set_comments(True, ACTOR)
        self.execute("UPDATE garage.meta_settings "
                     "SET comments_enabled_at = now() - interval '1 minute'")
        comment_id = f"{POST}_901"
        body = self.feed(comment_id, "ეს ზეთი Prius-ზე მოერგება?")
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.wait_outcomes(1), ["comment: comment_replied"])
        (reply,) = self.graph.sends()
        self.assertEqual((reply["recipient"], reply["message"]),
                         ({"comment_id": comment_id}, {"text": ANSWER}))
        (turn,) = self.engine.calls
        self.assertIsNone(turn["state"])
        self.assertTrue(turn["text"].startswith("ეს ზეთი Prius-ზე მოერგება?"))
        self.assertIn(POST_TEXT, turn["text"])  # the post, read once from Graph, as context
        self.assertEqual(len(self.graph.reads(POST)), 1)
        self.assertEqual(self.rows("SELECT reply_status, replied_psid, source, author_id "
                                   "FROM garage.meta_comments"),
                         [{"reply_status": "sent", "replied_psid": COMMENTER,
                           "source": "webhook", "author_id": AUTHOR}])
        self.assertEqual(self.rows(
            "SELECT channel, psid, status, engine_status, sent_mids, cost_usd::float8 AS cost "
            "FROM garage.messenger_replies"),
            [{"channel": "comment", "psid": COMMENTER, "status": "sent", "engine_status": "OK",
              "sent_mids": ["m_e2e_1"], "cost": 0.0123}])
        thread = self.site.get_thread(COMMENTER)
        self.assertEqual((thread["source"], thread["customer_name"]), ("comment", "ნინო"))
        # Meta's retry is the same event; the reply's echo is the bot's own message.
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post(self.body(self.echo(
            ANSWER, mid="m_e2e_1", psid=COMMENTER, app_id=APP))).status_code, 200)
        self.assertEqual(self.wait_outcomes(2), ["comment: comment_replied", "duplicate"])
        self.assertEqual((len(self.graph.sends()), len(self.engine.calls)), (1, 1))
        self.assertEqual(self.site.turns_today(), 1)

    # -- the receiver -------------------------------------------------------------------------

    def test_a_wrong_or_missing_signature_is_refused_and_stores_nothing(self):
        self.bot_on()
        body = self.body(self.message("ხუნდები", mid="m_forged"))
        forged = "sha256=" + hmac.new(b"not-the-app-secret", body, hashlib.sha256).hexdigest()
        signed = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        attempts = {
            "another key": self.post(body, signature=forged),
            "no signature": self.post(body, signed=False),
            "sha1": self.post(body, signature="sha1=" + "0" * 40),
            "changed body": self.post(body.replace(b"m_forged", b"m_forgeD"), signature=signed),
        }
        self.assertEqual({name: response.status_code for name, response in attempts.items()},
                         dict.fromkeys(attempts, 401))
        self.assertEqual(self.rows("SELECT count(*) AS n FROM garage.meta_inbox"), [{"n": 0}])
        # Meta's subscription handshake: the verify token, compared in constant time.
        url = self.receiver.url + WEBHOOK_PATH
        good = self.http.get(url, params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                          "hub.challenge": "e2e_challenge"}, timeout=10)
        self.assertEqual((good.status_code, good.text), (200, "e2e_challenge"))
        bad = self.http.get(url, params={"hub.mode": "subscribe", "hub.verify_token": "x" * 64,
                                         "hub.challenge": "e2e_challenge"}, timeout=10)
        self.assertEqual(bad.status_code, 403)
        self.assertEqual((self.graph.sends(), self.engine.calls), ([], []))

    # -- jobs -----------------------------------------------------------------------------------

    def test_every_job_runs_with_the_worker_login_against_graph(self):
        self.worker.stop(15)  # its no-op jobs are done; the real ones run here, in this thread
        self.ctx.store.upsert_thread(PSID, PAGE)  # a chat the names job can name
        before = self.rows("SELECT COALESCE(max(run_id), 0) AS id FROM garage.meta_sync_runs")
        jobs = MessengerWorker(self.ctx, self.engine)  # the real job functions
        results = {name: jobs.run_job(name, requested=True) for name in JOB_NAMES}
        self.assertEqual(results, {"subscription": "ok", "catchup": "ok", "names": "ok",
                                   "comments": "ok", "posts": "ok", "page_stats": "ok",
                                   "ads": "skipped", "token_health": "ok", "purge": "ok"})
        runs = {row["job"]: (row["status"], row["records"], row["detail"]) for row in self.rows(
            "SELECT job, status, records, detail FROM garage.meta_sync_runs "
            "WHERE run_id > %s ORDER BY run_id", (before[0]["id"],))}
        self.assertEqual(runs["ads"], ("skipped", 0, "no_ads_token"))  # SyncCount reaches it
        self.assertEqual(runs["names"], ("ok", 1, "conversations=1 updated=1"))
        # The page's only conversation is older than its connection: nothing to read back.
        self.assertEqual(runs["catchup"],
                         ("ok", 0, "conversations=0 messages=0 stored=0 claimed=0"))
        self.assertEqual(runs["subscription"],
                         ("ok", 0, "page_resubscribed=False app_resubscribed=False"))
        self.assertEqual(runs["token_health"], ("ok", 1, "tokens=1 broken=0 warn=0"))
        thread = self.site.get_thread(PSID)
        self.assertEqual((thread["customer_name"], thread["conversation_id"]),
                         ("ნინო", "t_10001"))
        self.assertEqual(sorted(self.site.page()["subscribed_fields"]),
                         sorted(SUBSCRIBED_FIELDS))
        (token,) = self.site.token_rows()
        self.assertEqual(token["status"], "ok")
        self.assertIsNotNone(token["checked_at"])
        self.assertEqual([row["followers"] for row in self.rows(
            "SELECT followers FROM garage.meta_page_daily")], [157])
        self.assertEqual(self.graph.sends(), [])  # jobs read and subscribe; they never send
        # urllib3 logged each read's request line, query string included, with the token out.
        requested = [line for line in self.logs.lines if "GET /v26.0/debug_token?" in line]
        self.assertTrue(requested)
        self.assertTrue(all("access_token=[redacted]" in line and "input_token=[redacted]" in line
                            for line in requested))

    def test_a_message_the_webhook_missed_is_stored_and_never_answered(self):
        self.worker.stop(15)
        self.bot_on()
        self.execute("UPDATE garage.meta_settings SET bot_enabled_at = now() - interval '1 hour'")
        self.execute("UPDATE garage.meta_page SET connected_at = now() - interval '1 hour'")
        now = datetime.now(UTC)

        def stamp(moment: datetime) -> str:
            return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")

        # The laptop slept: Meta never delivered these, but the conversation holds them
        # (older than the five minutes catch-up leaves to the webhook).
        self.graph.conversation_updated = stamp(now)
        self.graph.conversation_messages = [  # newest first, as Meta lists them
            {"id": "m_cu_2", "created_time": stamp(now - timedelta(minutes=12)),
             "from": {"id": PSID, "name": "ნინო"}, "message": "ამორტიზატორი გაქვთ?"},
            # The same words a second earlier: another message, kept too (measured: 39 such).
            {"id": "m_cu_2a", "created_time": stamp(now - timedelta(minutes=12, seconds=1)),
             "from": {"id": PSID, "name": "ნინო"}, "message": "ამორტიზატორი გაქვთ?"},
            {"id": "m_cu_1", "created_time": stamp(now - timedelta(minutes=15)),
             "from": {"id": PAGE, "name": "Garage • ფარეხი 1886"}, "message": "გამარჯობა"},
        ]
        jobs = MessengerWorker(self.ctx, self.engine)  # the real job functions
        self.assertEqual(jobs.run_job("catchup", requested=True), "ok")
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT mid, sender FROM garage.messenger_messages ORDER BY sent_at")],
            [("m_cu_1", "shop"), ("m_cu_2a", "customer"), ("m_cu_2", "customer")])
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT channel, status, error_text FROM garage.messenger_replies "
            "WHERE trigger_key = 'm_cu_2' ORDER BY channel")],
            [("messenger", "skipped_old", "catchup"), ("notice", "skipped_old", "catchup")])
        run = self.rows("SELECT records, detail FROM garage.meta_sync_runs "
                        "WHERE job = 'catchup' ORDER BY run_id DESC LIMIT 1")[0]
        self.assertEqual((run["records"], run["detail"]),
                         (3, "conversations=1 messages=3 stored=3 claimed=4"))
        # A rerun reads them again and stores nothing twice.
        self.assertEqual(jobs.run_job("catchup", requested=True), "ok")
        self.assertEqual(len(self.rows("SELECT mid FROM garage.messenger_messages")), 3)
        # A restart's recovery looks at the chat again (its newest message is a customer's and
        # young enough): the claim stops it, so no late bot reply goes out.
        restarted = MessengerWorker(self.ctx, self.engine, quiet_seconds=QUIET)
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(restarted._burst(PSID), "duplicate")
        self.assertEqual((self.graph.sends(), self.engine.calls), ([], []))
        # "Delete data" erases the chat, and the next run never reads it back.
        self.site.delete_customer(PSID, ACTOR)
        self.assertEqual(jobs.run_job("catchup", requested=True), "ok")
        self.assertEqual(self.rows("SELECT mid FROM garage.messenger_messages"), [])
        self.assertEqual(self.rows("SELECT psid FROM garage.messenger_threads"), [])

    def test_two_caught_up_messages_in_one_second_never_get_a_late_reply(self):
        self.worker.stop(15)
        self.bot_on()
        self.execute("UPDATE garage.meta_settings SET bot_enabled_at = now() - interval '1 hour'")
        self.execute("UPDATE garage.meta_page SET connected_at = now() - interval '1 hour'")
        now = datetime.now(UTC)
        same = (now - timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.graph.conversation_updated = now.strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.graph.conversation_messages = [  # newest first, as Meta lists them; whole seconds
            {"id": "m_t_b", "created_time": same, "from": {"id": PSID, "name": "x"},
             "message": "second"},
            {"id": "m_t_a", "created_time": same, "from": {"id": PSID, "name": "x"},
             "message": "first"}]
        jobs = MessengerWorker(self.ctx, self.engine)
        self.assertEqual(jobs.run_job("catchup", requested=True), "ok")
        self.assertEqual([row["mid"] for row in self.rows(
            "SELECT mid FROM garage.messenger_messages ORDER BY sent_at, message_id")],
            ["m_t_a", "m_t_b"])  # their true order
        restarted = MessengerWorker(self.ctx, self.engine, quiet_seconds=QUIET)
        restarted.recover()
        restarted._burst(PSID)
        self.assertEqual((self.graph.sends(), self.engine.calls), ([], []))

    def test_the_owners_own_chat_gets_no_late_reply_after_a_caught_up_command(self):
        self.worker.stop(15)
        self.ctx.store.upsert_thread(PSID, PAGE)
        self.site.set_admin(PSID, True, ACTOR)
        self.bot_on()
        self.execute("UPDATE garage.meta_settings SET bot_enabled_at = now() - interval '1 hour'")
        self.execute("UPDATE garage.meta_page SET connected_at = now() - interval '1 hour'")
        now = datetime.now(UTC)

        def stamp(moment: datetime) -> str:
            return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")

        self.graph.conversation_updated = stamp(now)
        self.graph.conversation_messages = [  # newest first: a question, then a command
            {"id": "m_adm_2", "created_time": stamp(now - timedelta(minutes=12)),
             "from": {"id": PSID}, "message": "#bot status"},
            {"id": "m_adm_1", "created_time": stamp(now - timedelta(minutes=13)),
             "from": {"id": PSID}, "message": "hello, brake pads for a Prius?"}]
        jobs = MessengerWorker(self.ctx, self.engine)
        self.assertEqual(jobs.run_job("catchup", requested=True), "ok")
        restarted = MessengerWorker(self.ctx, self.engine, quiet_seconds=QUIET)
        restarted.recover()
        restarted._burst(PSID)
        self.assertEqual((self.graph.sends(), self.engine.calls), ([], []))

    def test_a_late_oversized_copy_from_before_a_deletion_never_brings_the_chat_back(self):
        import psycopg

        old = datetime.now(UTC) - timedelta(minutes=3)
        self.ctx.store.add_message(PSID, "m_o1", "customer", old, page_id=PAGE, body="hi")
        self.site.delete_customer(PSID, ACTOR)
        marker = {"_oversized": True, "_bytes": 70000, "_kind": "msg",
                  "ids": {"sender": PSID, "recipient": PAGE,
                          "timestamp": int(old.timestamp() * 1000), "mid": "m_o0"}}
        key = "msg:" + hashlib.sha256(b"late-oversized-copy").hexdigest()
        with psycopg.connect(self.url) as connection:
            connection.execute(
                "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                "VALUES (%s, 'messaging', %s, %s::jsonb)", (key, PAGE, json.dumps(marker)))
        self.assertEqual(self.wait_outcomes(1), ["ignored: deleted_chat"])
        self.assertEqual(self.rows("SELECT psid FROM garage.messenger_threads"), [])

    def test_a_late_copy_from_before_a_deletion_never_brings_the_chat_back(self):
        old = datetime.now(UTC) - timedelta(minutes=3)
        self.ctx.store.add_message(PSID, "m_p1", "customer", old, page_id=PAGE, body="hi")
        self.site.delete_customer(PSID, ACTOR)
        self.say("late copy", mid="m_p0", sent=old)  # an unprocessed webhook copy, or a retry
        self.assertEqual(self.wait_outcomes(1), ["ignored: deleted_chat"])
        self.assertEqual(self.rows("SELECT psid FROM garage.messenger_threads"), [])
        self.assertEqual(self.rows("SELECT mid FROM garage.messenger_messages"), [])
        self.assertEqual((self.graph.sends(), self.engine.calls), ([], []))


if __name__ == "__main__":
    unittest.main()
