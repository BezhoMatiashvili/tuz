"""The admin's on/off switch for the Garage Messenger bot, end to end, on loopback and at $0.

The owner's requirement: the admin can turn the chatbot on and off. This file proves it through
the real code paths, on the harness of tests/test_messenger_e2e.py:

- PostgreSQL: a database this file creates on WAREHOUSE_TEST_ADMIN_DATABASE_URL, with the roles
  script and every migration, and one LOGIN role per process, each a member of one capability
  role only (the receiver's of garage_webhook, the worker's of garage_messenger, the
  dashboard's of garage_site). The whole file skips without that URL.
- Meta's side: the real receiver on uvicorn, on an ephemeral loopback port, with a test app
  secret (every customer message below is a delivery signed as Meta signs it), and a fake Graph
  API on loopback that records every Send API call the worker makes.
- The bot: the real MessengerWorker on its own threads, with the production Graph client and a
  scripted engine (no model, no bill).
- The admin: the assembled Garage application, signed in as in tests/test_site_meta_admin.py
  (session cookie, Origin and CSRF header), its Facebook section on the real MetaStore through
  the garage_site login. The switch the admin flips is the row the worker reads.

The scenario, in order: the page is connected as Connect stores it; the fresh install answers
nobody; the admin turns the bot on (the next message is answered once), off (nothing is sent)
and on again (answered); the owner turns it off and on from their own Messenger with
``#bot off`` and ``#bot on``, while the same words from a customer change nothing; an employee
can do neither. Every customer message is written after the switch moment it tests, and a
negative ("nothing was sent") is asserted only once the reply attempt it concerns is final.

While the test runs, the e2e harness refuses every socket off loopback and a hook of this file
refuses one to the laptop's live system. Audit hooks see Python's sockets only: the database
logins (libpq) stay off the developer database because every URL here is made from
WAREHOUSE_TEST_ADMIN_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import sys
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import requests
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from automotive.messenger_render import bot_switched_text
from automotive.messenger_worker import JOB_NAMES, MessengerWorker
from automotive.meta_config import REQUIRED_SCOPES, SUBSCRIBED_FIELDS, WEBHOOK_PATH, MetaSettings
from automotive.meta_context import build_context
from automotive.meta_crypto import TokenCipher
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphClient, appsecret_proof
from automotive.meta_receiver import create_receiver_app
from automotive.meta_store import MetaStore
from tests import test_messenger_e2e as e2e
from tests.test_site_meta_admin import FacebookCase
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = e2e.ADMIN_URL
APP, PAGE, PSID, ADMIN_PSID = e2e.APP, e2e.PAGE, e2e.PSID, e2e.ADMIN_PSID
OTHER_PSID = "7000000000005678"  # another customer, who types the admin's words
OWNER = "4400000000000001"  # the owner's Facebook login, whose token Connect keeps as well
PAGE_NAME = "Garage • ფარეხი 1886"
# Made for this run only, like the harness's page token; not a credential anywhere.
USER_TOKEN = "EAAe2eUserToken" + uuid.uuid4().hex
ANSWER = {"text": e2e.ANSWER}  # the scripted engine's answer, as the Send API carries it
# Meta stamps a message in milliseconds, the database stamps a switch in microseconds: a message
# written "after" a switch moment is written this much after it.
AFTER = timedelta(milliseconds=50)
# The laptop's live system: the Garage services (8767, 8770, 8771) and the developer database
# (55432). Nothing in this file may connect to them.
LIVE_PORTS = frozenset({8767, 8770, 8771, 55432})


# -- never the live system -------------------------------------------------------------------------

_LIVE_GUARD = threading.Event()
_TOUCHED: list[str] = []
_HOOKED: list[bool] = []


def _never_live(event: str, args: tuple) -> None:
    """While this file's test runs, no socket may connect to a port of the live system."""
    if not _LIVE_GUARD.is_set() or event != "socket.connect":
        return
    address = args[1]
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in LIVE_PORTS:
        _TOUCHED.append(f"{address[0]}:{address[1]}")
        raise PermissionError("this test never connects to the live system")


def _install_guards() -> None:
    """Audit hooks cannot be removed: each is added once and acts only while its event is set,
    this one during this file's test, the harness's during the end-to-end tests."""
    e2e._install_guard()
    if not _HOOKED:
        sys.addaudithook(_never_live)
        _HOOKED.append(True)


# -- the test --------------------------------------------------------------------------------------


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class AdminSwitchCycleTests(FacebookCase):
    """Receiver, worker and dashboard logins on one database; Meta and the engine faked."""

    @classmethod
    def setUpClass(cls) -> None:
        _install_guards()
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin_db = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop)
        name = "meta_cycle_" + cls.suffix
        cls.admin_db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cls.databases.append(name)
        cls.url = cls._conninfo(name)
        with psycopg.connect(cls.url) as connection:
            connection.execute(e2e.ROLES_SQL)
            apply_migrations_on_connection(connection)
        logins = {key: cls._login(key, capability) for key, capability in (
            ("hook", "garage_webhook"), ("worker", "garage_messenger"), ("site", "garage_site"))}
        # One Meta block for the three processes, which all read the one .env; FacebookCase
        # hands it to the dashboard's Facebook routers.
        cls.meta_settings = MetaSettings(
            app_id=APP, app_secret=e2e.SECRET, verify_token=e2e.VERIFY, token_key=e2e.KEY,
            graph_version=e2e.VERSION, public_url="https://garage-cycle.invalid",
            receiver_database_url=logins["hook"], database_url=logins["worker"])
        # The dashboard's store as install_site builds it: the real one, on the site login.
        cls.site_store = MetaStore(
            MetaDatabase(logins["site"], application_name="garage-cycle-site"),
            TokenCipher(e2e.KEY))
        cls.meta_api = e2e.FakeGraph(cls.meta_settings.webhook_url)
        cls.meta_api.start()
        cls.addClassCleanup(cls.meta_api.stop)
        cls.receiver = e2e._Served(create_receiver_app(cls.meta_settings))
        cls.receiver.start()
        cls.addClassCleanup(cls.receiver.stop)
        cls.http = requests.Session()
        cls.http.trust_env = False
        cls.addClassCleanup(cls.http.close)

    @classmethod
    def _drop(cls) -> None:
        for name in reversed(cls.databases):
            cls.admin_db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        for name in reversed(cls.cluster_roles):
            cls.admin_db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin_db.close()

    @classmethod
    def _conninfo(cls, database: str, **login: str) -> str:
        info = conninfo_to_dict(ADMIN_URL)
        info.update(dbname=database, **login)
        return make_conninfo(**info)

    @classmethod
    def _login(cls, key: str, capability: str) -> str:
        """A LOGIN role that is a member of ``capability`` only; the URL its process uses."""
        name, password = f"cycle_{key}_{cls.suffix}", uuid.uuid4().hex
        cls.admin_db.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name), sql.Literal(password)))
        cls.cluster_roles.append(name)
        cls.admin_db.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability),
                                                              sql.Identifier(name)))
        return cls._conninfo(cls.databases[-1], user=name, password=password)

    def make_store(self) -> MetaStore:
        return self.site_store

    def serve(self, settings=None, **kwargs):
        # The rest of the Facebook section on the same store: nothing reads the environment.
        kwargs.setdefault("meta_activity", {"store": self.store, "graph": self.graph,
                                            "meta_settings": self.meta_settings})
        return super().serve(settings, **kwargs)

    async def asyncSetUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in e2e.TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")
        await super().asyncSetUp()  # the application, the admin's and the employee's sessions
        self.meta_api.clear()
        e2e._BLOCKED.clear()
        _TOUCHED.clear()
        self.words: dict[str, str] = {}
        self.mids = itertools.count(1)
        # Cleanups run last first: this one after the worker has stopped.
        self.addCleanup(self._check_traffic)
        for guard in (e2e._GUARD, _LIVE_GUARD):
            guard.set()
            self.addCleanup(guard.clear)
        # Every log line of the receiver, the worker and the HTTP library, DEBUG included.
        self.logs = e2e._Lines()
        for name in ("automotive", "urllib3", "uvicorn"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            self.addCleanup(logger.removeHandler, self.logs)
            logger.addHandler(self.logs)
            logger.setLevel(logging.DEBUG)
        self.engine = e2e.FakeEngine()
        graph = GraphClient(self.meta_settings, session=e2e._FakeGraphSession(self.meta_api.base),
                            sleep=lambda seconds: None)
        self.ctx = build_context(self.meta_settings, self.meta_settings.database_url, graph=graph,
                                 application_name="garage-messenger-cycle")
        # SiteAppCase's self.worker is the employee's account; the Messenger worker is the bot.
        self.bot = MessengerWorker(self.ctx, self.engine, quiet_seconds=e2e.QUIET,
                                   max_wait_seconds=10, poll_seconds=0.2,
                                   jobs={name: e2e._no_job for name in JOB_NAMES})

    def _check_traffic(self) -> None:
        self.assertEqual(e2e._BLOCKED, [], "a connection tried to leave loopback")
        self.assertEqual(_TOUCHED, [], "a connection tried to reach the live system")
        self.assertEqual(self.meta_api.unknown, [], "Graph calls without a fixture")
        self.assertEqual(self.meta_api.refused, [], "Graph calls with a wrong token or proof")
        private = {"the page token": e2e.PAGE_TOKEN, "the user token": USER_TOKEN,
                   "the app secret": e2e.SECRET, "the verify token": e2e.VERIFY,
                   "the page token's proof": appsecret_proof(e2e.PAGE_TOKEN, e2e.SECRET),
                   "the answer": e2e.ANSWER,
                   **{f"customer id {psid[-4:]}": psid for psid in (PSID, ADMIN_PSID, OTHER_PSID)},
                   **{f"customer words {mid}": text for mid, text in self.words.items()}}
        # The test's own deliveries are Meta's side, logged by urllib3 in this process.
        lines = [line for line in self.logs.lines if self.receiver.url not in line]
        leaked = sorted({name for line in lines for name, value in private.items()
                         if value in line})
        self.assertEqual(leaked, [], "secrets or customer data in a log line")

    # -- looking: the database's owner reads, and never acts -----------------------------------

    def rows(self, statement: str, parameters: tuple | None = None) -> list[dict]:
        with psycopg.connect(self.url, row_factory=dict_row) as connection:
            return connection.execute(statement, parameters).fetchall()

    async def wait_for(self, check: Callable[[], Any], what: str) -> Any:
        deadline = time.monotonic() + e2e.WAIT
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.05)

    async def reply(self, channel: str, mid: str) -> dict:
        """The reply attempt ``mid`` triggered on ``channel`` once it is final: every send it
        made is done, and it makes no other."""
        def final() -> dict | None:
            found = self.rows(
                "SELECT status, engine_status, psid, sent_mids FROM garage.messenger_replies "
                "WHERE channel = %s AND trigger_key = %s", (channel, mid))
            return found[0] if found and found[0]["status"] != "pending" else None
        return await self.wait_for(final, f"the {channel} reply to {mid}")

    def mark(self) -> int:
        """Where the fake Graph API's record of calls stands now."""
        with self.meta_api.lock:
            return len(self.meta_api.calls)

    def sent_since(self, mark: int) -> list[tuple[str, Any]]:
        """Every Send API call since ``mark``, "seen" and "typing" included: (recipient, the
        message or the sender action)."""
        with self.meta_api.lock:
            calls = self.meta_api.calls[mark:]
        return [(json.loads(call.values.get("recipient") or "{}").get("id", ""),
                 json.loads(call.values["message"]) if "message" in call.values
                 else call.values.get("sender_action"))
                for call in calls if call.method == "POST" and call.path == f"{PAGE}/messages"]

    @staticmethod
    def answered(psid: str) -> list[tuple[str, Any]]:
        """The Send API calls of one answer to ``psid``: seen, typing, the answer itself."""
        return [(psid, "mark_seen"), (psid, "typing_on"), (psid, ANSWER)]

    # -- Meta's side ---------------------------------------------------------------------------

    async def say(self, text: str, *, psid: str = PSID, after: datetime | None = None) -> str:
        """``psid`` writes ``text`` to the page, plainly after the switch moment ``after``;
        Meta delivers it to the receiver, signed with the app secret. The message's mid."""
        if after is not None:
            while datetime.now(UTC) < after + AFTER:
                await asyncio.sleep(0.01)
        mid = f"m_cycle_{next(self.mids)}"
        self.words[mid] = text
        written = int(time.time() * 1000)
        item = {"sender": {"id": psid}, "recipient": {"id": PAGE}, "timestamp": written,
                "message": {"mid": mid, "text": text}}
        body = json.dumps({"object": "page", "entry": [
            {"id": PAGE, "time": written, "messaging": [item]}]}, ensure_ascii=False).encode()
        signature = "sha256=" + hmac.new(e2e.SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.http.post(self.receiver.url + WEBHOOK_PATH, data=body, timeout=10,
                                  headers={"Content-Type": "application/json",
                                           "X-Hub-Signature-256": signature})
        self.assertEqual((response.status_code, response.text), (200, "EVENT_RECEIVED"))
        return mid

    # -- the admin's side: the dashboard ------------------------------------------------------

    def connect_page(self) -> None:
        """What Connect stores once Meta has vouched for the pasted token (meta_connect.connect,
        steps 5-7), on the dashboard's login: the page and both tokens sealed in one
        transaction, then the webhook fields the page is subscribed to."""
        now = datetime.now(UTC)
        self.store.save_connection(PAGE, PAGE_NAME, APP, {
            "page": {"token": e2e.PAGE_TOKEN, "subject_id": PAGE, "subject_name": PAGE_NAME,
                     "scopes": ["pages_messaging", "pages_show_list", "pages_manage_metadata"],
                     "expires_at": None, "data_access_expires_at": now + timedelta(days=80)},
            "user": {"token": USER_TOKEN, "subject_id": OWNER, "subject_name": "Owner",
                     "scopes": list(REQUIRED_SCOPES), "expires_at": now + timedelta(days=60),
                     "data_access_expires_at": now + timedelta(days=80)},
        }, self.boss, client_ip="127.0.0.1")
        self.assertIs(self.store.set_subscription(SUBSCRIBED_FIELDS), True)

    async def switches(self) -> dict:
        """The switches as Status & switches shows them (GET /status)."""
        response = await self.fb("GET", "/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["switches"]

    async def flip(self, on: bool) -> dict:
        """The Status tab's button: Turn the bot on (its "Auto reply is off" tick confirmed) or
        Turn the bot off, with the version the tab last read. The switches it answers with."""
        version = (await self.switches())["version"]
        fields = ({"bot_enabled": True, "auto_reply_confirmed_off": True} if on
                  else {"bot_enabled": False})
        response = await self.fb("PUT", "/settings", {"version": version, **fields})
        self.assertEqual(response.status_code, 200, response.text)
        switches = response.json()["switches"]
        self.assertEqual((switches["bot_enabled"], switches["bot_state"],
                          switches["bot_changed_by"], switches["version"]),
                         (on, "on" if on else "off", "boss", version + 1))
        # The worker's own login reads the very row the dashboard wrote.
        self.assertIs(self.ctx.store.settings()["bot_enabled"], on)
        return switches

    async def chat(self, psid: str) -> list[tuple[str, str]]:
        """The chat as the admin reads it in Chats (GET /threads/{psid}): who wrote what."""
        response = await self.fb("GET", f"/threads/{psid}")
        self.assertEqual(response.status_code, 200, response.text)
        return [(row["sender"], row["body"]) for row in response.json()["messages"]]

    # -- the scenario -------------------------------------------------------------------------

    async def test_the_admin_turns_the_bot_on_and_off_and_the_worker_obeys(self):
        words = self.words

        # 1. The page is connected as Connect leaves it: the page row, both tokens sealed with
        #    the encryption key, the webhooks subscribed. Then the worker starts.
        self.connect_page()
        sealed = self.rows("SELECT purpose, app_id, status, token_enc FROM garage.meta_tokens "
                           "ORDER BY purpose")
        self.assertEqual([(row["purpose"], row["app_id"], row["status"]) for row in sealed],
                         [("page", APP, "ok"), ("user", APP, "ok")])
        for row, token in zip(sealed, (e2e.PAGE_TOKEN, USER_TOKEN), strict=True):
            self.assertRegex(row["token_enc"], r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
            self.assertNotIn(token, row["token_enc"])
        choice = self.ctx.send_token()  # the worker's login opens the page token with the key
        self.assertEqual((choice.token, choice.proof, choice.purpose),
                         (e2e.PAGE_TOKEN, True, "page"))
        response = await self.fb("GET", "/status")
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertEqual((status["connected"], status["page"]["page_id"],
                          status["page"]["subscribed_fields"]),
                         (True, PAGE, list(SUBSCRIBED_FIELDS)))
        self.assertEqual([(row["purpose"], row["status"]) for row in status["tokens"]],
                         [("page", "ok"), ("user", "ok")])
        self.assertIn("auto_reply_not_confirmed",
                      [warning["code"] for warning in status["warnings"]])
        for token in (e2e.PAGE_TOKEN, USER_TOKEN):
            self.assertNotIn(token, response.text)
        self.bot.start()
        self.addCleanup(self.bot.stop, 15)

        # 2. A fresh install: the bot ships off, and a customer writes all the same.
        fresh = await self.switches()
        self.assertEqual((fresh["bot_enabled"], fresh["bot_state"], fresh["bot_enabled_at"],
                          fresh["auto_reply_confirmed_off"]), (False, "off", None, False))
        self.assertIs(self.ctx.store.settings()["bot_enabled"], False)
        mark = self.mark()
        a = await self.say("Camry 2015-ის წინა ხუნდები გაქვთ?")
        reply = await self.reply("messenger", a)
        self.assertEqual((reply["status"], reply["engine_status"], reply["sent_mids"]),
                         ("skipped_off", "", []))
        # Decided once the customer had paused for the quiet window; nothing at all went out
        # (no message, no "seen", no "typing") and the engine never ran.
        (waited,) = self.rows(
            "SELECT r.started_at - (SELECT received_at FROM garage.meta_inbox "
            "ORDER BY inbox_id LIMIT 1) AS waited FROM garage.messenger_replies r "
            "WHERE r.trigger_key = %s", (a,))
        self.assertGreaterEqual(waited["waited"], timedelta(seconds=e2e.QUIET))
        self.assertEqual((self.sent_since(mark), self.engine.calls), ([], []))
        self.assertEqual(await self.chat(PSID), [("customer", words[a])])

        # 3. The admin turns the bot on: Status & switches, Turn the bot on, "Auto reply is off"
        #    ticked. Only messages written after this moment are ever answered.
        on = await self.flip(True)
        enabled_at = datetime.fromisoformat(on["bot_enabled_at"])
        self.assertLess(abs(datetime.now(UTC) - enabled_at), timedelta(minutes=1))
        self.assertIs(on["auto_reply_confirmed_off"], True)
        self.assertEqual(self.rows(
            "SELECT actor_username, action, target, detail, host(client_ip) AS client_ip "
            "FROM garage.audit_log WHERE action IN ('messenger.bot_on', 'messenger.bot_off')"),
            [{"actor_username": "boss", "action": "messenger.bot_on", "target": "settings",
              "detail": {"changed": True, "was_paused": False, "auto_reply_confirmed_off": True},
              "client_ip": "127.0.0.1"}])
        mark, turns = self.mark(), len(self.engine.calls)
        b = await self.say("Prius 2012-ის ზეთის ფილტრი გაქვთ?", after=enabled_at)
        reply = await self.reply("messenger", b)
        self.assertEqual((reply["status"], reply["engine_status"], len(reply["sent_mids"])),
                         ("sent", "OK", 1))
        self.assertEqual(self.sent_since(mark), self.answered(PSID))  # one answer, to them only
        self.assertEqual(len(self.engine.calls), turns + 1)
        # The turn reads A along with B; A itself is never answered on its own.
        self.assertTrue(self.engine.calls[-1]["text"].endswith(words[b]))
        self.assertEqual((await self.reply("messenger", a))["status"], "skipped_off")
        self.assertEqual(await self.chat(PSID), [("customer", words[a]), ("customer", words[b]),
                                                 ("bot", e2e.ANSWER)])

        # 4. The admin turns it off (Turn the bot off).
        off = await self.flip(False)
        self.assertEqual(off["bot_enabled_at"], on["bot_enabled_at"])  # when it last went on
        mark, turns = self.mark(), len(self.engine.calls)
        off_at = datetime.fromisoformat(off["bot_changed_at"])
        c = await self.say("NGK სანთლები გაქვთ?", after=off_at)
        reply = await self.reply("messenger", c)
        self.assertEqual((reply["status"], reply["engine_status"], reply["sent_mids"]),
                         ("skipped_off", "", []))
        self.assertEqual((self.sent_since(mark), len(self.engine.calls)), ([], turns))

        # 5. On again: the cycle repeats, from a new switch-on moment.
        again = await self.flip(True)
        enabled_again = datetime.fromisoformat(again["bot_enabled_at"])
        self.assertGreater(enabled_again, enabled_at)
        mark, turns = self.mark(), len(self.engine.calls)
        d = await self.say("ამორტიზატორი Corolla 2010-ზე?", after=enabled_again)
        reply = await self.reply("messenger", d)
        self.assertEqual((reply["status"], reply["engine_status"], len(reply["sent_mids"])),
                         ("sent", "OK", 1))
        self.assertEqual(self.sent_since(mark), self.answered(PSID))
        self.assertEqual(len(self.engine.calls), turns + 1)
        self.assertTrue(self.engine.calls[-1]["text"].endswith(words[d]))
        self.assertEqual((await self.reply("messenger", c))["status"], "skipped_off")

        # 6. The owner's phone. They write to the page from their own profile (answered: the
        #    bot is on), then mark that chat as theirs: Chats, Admin account.
        mark = self.mark()
        hello = await self.say("გამარჯობა", psid=ADMIN_PSID)
        self.assertEqual((await self.reply("messenger", hello))["status"], "sent")
        self.assertEqual(self.sent_since(mark), self.answered(ADMIN_PSID))
        response = await self.fb("GET", f"/threads/{ADMIN_PSID}")
        thread = response.json()["thread"]
        self.assertIs(thread["is_admin"], False)
        response = await self.fb("PUT", f"/threads/{ADMIN_PSID}",
                                 {"is_admin": True, "version": thread["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["thread"]["is_admin"], True)

        # '#bot off' from the owner's Messenger: switched at once, and confirmed to them.
        mark = self.mark()
        bot_off = await self.say("#bot off", psid=ADMIN_PSID)
        self.assertEqual((await self.reply("command", bot_off))["status"], "sent")
        self.assertEqual(self.sent_since(mark),
                         [(ADMIN_PSID, {"text": bot_switched_text("ka", False)})])
        phone_off = await self.switches()
        self.assertEqual((phone_off["bot_enabled"], phone_off["bot_state"],
                          phone_off["bot_changed_by"]), (False, "off", "messenger:9999"))
        self.assertIs(self.ctx.store.settings()["bot_enabled"], False)
        off_at = datetime.fromisoformat(phone_off["bot_changed_at"])
        # A customer's message is now left to the shop; another customer's '#bot on' is only
        # words: it reaches no command and the bot stays off.
        mark, turns = self.mark(), len(self.engine.calls)
        e = await self.say("ზეთი 5W-30 გაქვთ?", after=off_at)
        self.assertEqual((await self.reply("messenger", e))["status"], "skipped_off")
        wrong_on = await self.say("#bot on", psid=OTHER_PSID, after=off_at)
        self.assertEqual((await self.reply("messenger", wrong_on))["status"], "skipped_off")
        self.assertEqual((self.sent_since(mark), len(self.engine.calls)), ([], turns))
        self.assertEqual(await self.switches(), phone_off)

        # '#bot on' from the owner's Messenger: on again, from a new moment.
        mark = self.mark()
        bot_on = await self.say("#bot on", psid=ADMIN_PSID)
        self.assertEqual((await self.reply("command", bot_on))["status"], "sent")
        self.assertEqual(self.sent_since(mark),
                         [(ADMIN_PSID, {"text": bot_switched_text("ka", True)})])
        phone_on = await self.switches()
        self.assertEqual((phone_on["bot_enabled"], phone_on["bot_state"],
                          phone_on["bot_changed_by"]), (True, "on", "messenger:9999"))
        self.assertIs(self.ctx.store.settings()["bot_enabled"], True)
        enabled_phone = datetime.fromisoformat(phone_on["bot_enabled_at"])
        self.assertGreater(enabled_phone, enabled_again)
        mark, turns = self.mark(), len(self.engine.calls)
        f = await self.say("ჰაერის ფილტრი Fit 2009-ზე?", after=enabled_phone)
        reply = await self.reply("messenger", f)
        self.assertEqual((reply["status"], reply["engine_status"], len(reply["sent_mids"])),
                         ("sent", "OK", 1))
        self.assertEqual(self.sent_since(mark), self.answered(PSID))
        self.assertEqual(len(self.engine.calls), turns + 1)
        self.assertTrue(self.engine.calls[-1]["text"].endswith(words[f]))
        # Another customer's '#bot off' is only words: an ordinary answer, and the bot stays on.
        mark, turns = self.mark(), len(self.engine.calls)
        wrong_off = await self.say("#bot off", psid=OTHER_PSID, after=enabled_phone)
        self.assertEqual((await self.reply("messenger", wrong_off))["status"], "sent")
        self.assertEqual(self.sent_since(mark), self.answered(OTHER_PSID))
        self.assertEqual(len(self.engine.calls), turns + 1)
        self.assertEqual(await self.switches(), phone_on)

        # 7. An employee can neither stop the bot nor start it; the admin still can.
        response = await self.fb("PUT", "/settings", {"version": phone_on["version"],
                                                      "bot_enabled": False}, who=self.employee)
        self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual(await self.switches(), phone_on)
        self.assertIs(self.ctx.store.settings()["bot_enabled"], True)
        last_off = await self.flip(False)
        response = await self.fb("PUT", "/settings", {
            "version": last_off["version"], "bot_enabled": True, "auto_reply_confirmed_off": True},
            who=self.employee)
        self.assert_refused(response, 403, "ADMIN_ONLY")
        self.assertEqual(await self.switches(), last_off)
        # The worker, reading that row, leaves the next customer to the shop.
        mark, turns = self.mark(), len(self.engine.calls)
        off_at = datetime.fromisoformat(last_off["bot_changed_at"])
        g = await self.say("ხვალ ღია ხართ?", after=off_at)
        self.assertEqual((await self.reply("messenger", g))["status"], "skipped_off")
        self.assertEqual((self.sent_since(mark), len(self.engine.calls)), ([], turns))

        # Every reply attempt and every switch change of the scenario, and nothing else.
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT channel, psid, trigger_key, status FROM garage.messenger_replies "
            "ORDER BY reply_id")], [
            ("messenger", PSID, a, "skipped_off"), ("messenger", PSID, b, "sent"),
            ("messenger", PSID, c, "skipped_off"), ("messenger", PSID, d, "sent"),
            ("messenger", ADMIN_PSID, hello, "sent"), ("command", ADMIN_PSID, bot_off, "sent"),
            ("messenger", PSID, e, "skipped_off"),
            ("messenger", OTHER_PSID, wrong_on, "skipped_off"),
            ("command", ADMIN_PSID, bot_on, "sent"), ("messenger", PSID, f, "sent"),
            ("messenger", OTHER_PSID, wrong_off, "sent"), ("messenger", PSID, g, "skipped_off")])
        self.assertEqual([tuple(row.values()) for row in self.rows(
            "SELECT actor_username, action, target FROM garage.audit_log ORDER BY audit_id")], [
            ("boss", "messenger.connected", PAGE),
            ("boss", "messenger.bot_on", "settings"),
            ("boss", "messenger.bot_off", "settings"),
            ("boss", "messenger.bot_on", "settings"),
            ("boss", "messenger.chat_admin_on", "…9999"),
            ("messenger:9999", "messenger.bot_off", "settings"),
            ("messenger:9999", "messenger.bot_on", "settings"),
            ("boss", "messenger.bot_off", "settings")])
        # The chats as the admin reads them: each switched-on moment answered once, nothing
        # written while the bot was off ever answered on its own.
        self.assertEqual(await self.chat(PSID), [
            ("customer", words[a]), ("customer", words[b]), ("bot", e2e.ANSWER),
            ("customer", words[c]), ("customer", words[d]), ("bot", e2e.ANSWER),
            ("customer", words[e]), ("customer", words[f]), ("bot", e2e.ANSWER),
            ("customer", words[g])])
        self.assertEqual(await self.chat(ADMIN_PSID), [
            ("customer", words[hello]), ("bot", e2e.ANSWER),
            ("customer", "#bot off"), ("bot", bot_switched_text("ka", False)),
            ("customer", "#bot on"), ("bot", bot_switched_text("ka", True))])
        # The engine ran for the five answered messages only, and the switches never called Meta.
        self.assertEqual((len(self.engine.calls), self.store.turns_today()), (5, 5))
        self.assertEqual(self.graph.calls, [])


if __name__ == "__main__":
    unittest.main()
