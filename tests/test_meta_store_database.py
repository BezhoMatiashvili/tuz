"""MetaStore on a disposable PostgreSQL database, under the real capability roles.

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in a database and roles
this file creates and drops; the database named in that URL is only connected to. Every store
call runs as a NOLOGIN role that is a member of garage_messenger (the worker) or garage_site (the
dashboard), so a query outside 0013's grants fails here as it would in production.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from automotive.meta_crypto import TokenCipher
from automotive.meta_db import MetaDatabase
from automotive.meta_store import (
    AutoReplyNotConfirmed,
    InvalidFields,
    MetaConflict,
    MetaStore,
    bot_state,
    forgotten_key,
    forgotten_since,
    mask_psid,
)
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
PAGE, APP, OTHER_APP = "110000000000001", "220000000000002", "330000000000003"
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
ADMIN = {"user_id": 7, "username": "boss"}
PAGE_TOKEN, USER_TOKEN = "EAAfixturePageToken0001", "EAAfixtureUserToken0002"
TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads", "messenger_messages",
          "messenger_replies", "meta_sync_runs", "meta_job_requests", "meta_comments",
          "meta_forgotten", "live_alerts", "live_web_chats", "live_web_messages", "audit_log")
FORGET_UP = (ROOT / "migrations/0014_meta_forget.up.sql").read_text(encoding="utf-8")
FORGET_DOWN = (ROOT / "migrations/0014_meta_forget.down.sql").read_text(encoding="utf-8")
AUTHOR, OTHER_AUTHOR = "900000000000001", "900000000000009"


# Fixed once, so every at() in a test agrees to the microsecond.
BASE_TIME = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)


def at(minutes: float) -> datetime:
    """A fixed point in time, ``minutes`` after a base well inside the retention window."""
    return BASE_TIME + timedelta(minutes=minutes)


class RoleDatabase(MetaDatabase):
    """A MetaDatabase whose every connection runs as ``role`` (SET ROLE, committed at once so a
    later rollback cannot fall back to the administrator)."""

    def __init__(self, url: str, role: str) -> None:
        super().__init__(url, application_name="meta-store-test")
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


def tokens(page_token: str = PAGE_TOKEN, user_token: str = USER_TOKEN,
           page_id: str = PAGE) -> dict:
    return {
        "page": {"token": page_token, "subject_id": page_id, "subject_name": "Garage",
                 "scopes": ["pages_messaging", "pages_show_list"], "expires_at": None,
                 "data_access_expires_at": at(60 * 24 * 80)},
        "user": {"token": user_token, "subject_id": "4400000001", "subject_name": "Owner",
                 "scopes": ["ads_read"], "expires_at": at(60 * 24 * 60),
                 "data_access_expires_at": at(60 * 24 * 80)},
    }


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class MetaStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("meta_store_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"), ("hook", "garage_webhook"),
            ("outsider", None))}
        cls.cipher = TokenCipher(os.urandom(32))
        cls.worker = MetaStore(RoleDatabase(cls.url, cls.roles["worker"]), cls.cipher)
        cls.site = MetaStore(RoleDatabase(cls.url, cls.roles["site"]), cls.cipher)

    @classmethod
    def _drop_everything(cls) -> None:
        for name in reversed(cls.databases):
            cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        for name in reversed(cls.cluster_roles):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin.close()

    @classmethod
    def create_database(cls, name: str) -> str:
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cls.databases.append(name)
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = name
        return make_conninfo(**info)

    @classmethod
    def create_role(cls, key: str, capability: str | None) -> str:
        name = f"meta_{key}_{cls.suffix}"
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name)))
        cls.cluster_roles.append(name)
        if capability:
            cls.admin.execute(sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(capability), sql.Identifier(name)))
        cls.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(name)))
        return name

    def setUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")

    @contextlib.contextmanager
    def acting_as(self, role: str):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            yield connection

    def admin_sql(self, statement: str, parameters: tuple = ()):
        with psycopg.connect(self.url) as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def assert_denied(self, role: str, statement: str, parameters: tuple = ()) -> None:
        with self.acting_as(self.roles[role]) as connection:
            with self.assertRaises(errors.InsufficientPrivilege, msg=f"{role}: {statement}"):
                connection.execute(statement, parameters)

    def deliver(self, payload: dict | None = None, *, field: str = "messages") -> str:
        """Append one webhook item exactly as the receiver's role can."""
        key = "msg:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        with self.acting_as(self.roles["hook"]) as connection:
            connection.execute(
                "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (key, field, PAGE, Jsonb(payload or {"sender": {"id": PSID}})))
        return key

    def audits(self) -> list[tuple]:
        return [tuple(row) for row in self.admin_sql(
            "SELECT actor_id, actor_username, action, target, detail FROM garage.audit_log "
            "ORDER BY audit_id")]

    # -- switches ----------------------------------------------------------------------------

    def test_the_bot_switch_needs_the_auto_reply_confirmation_and_stamps_only_off_to_on(self):
        site, worker = self.site, self.worker
        self.assertEqual(bot_state(site.settings()), "off")
        with self.assertRaises(AutoReplyNotConfirmed):
            site.set_bot(True, ADMIN)
        self.assertEqual((site.settings()["bot_enabled"], self.audits()), (False, []))
        on = site.set_bot(True, ADMIN, auto_reply_confirmed=True)
        stamp = on["bot_enabled_at"]
        self.assertEqual((on["bot_enabled"], on["auto_reply_confirmed_off"], on["bot_changed_by"],
                          bot_state(on)), (True, True, "boss", "on"))
        self.assertIsNotNone(stamp)
        self.assertEqual(self.audits()[-1][:4], (7, "boss", "messenger.bot_on", "settings"))
        # ON again (confirmation already stored): no new stamp, so no backlog becomes eligible.
        self.assertEqual(site.set_bot(True, ADMIN)["bot_enabled_at"], stamp)
        # The worker pauses the bot when the AI account fails; only the first call changes it.
        self.assertTrue(worker.pause_for_provider())
        self.assertFalse(worker.pause_for_provider())
        paused = worker.settings()
        self.assertEqual((bot_state(paused), paused["bot_paused_reason"]),
                         ("paused", "ai_provider"))
        self.assertEqual(self.audits()[-1][1:4], ("worker", "messenger.bot_paused", "settings"))
        resumed = site.resume_bot(ADMIN)
        self.assertEqual((bot_state(resumed), resumed["bot_paused_at"],
                          resumed["bot_enabled_at"]), ("on", None, stamp))
        self.assertEqual(self.audits()[-1][2], "messenger.bot_resumed")
        audits = len(self.audits())
        site.resume_bot(ADMIN)  # nothing paused: nothing changes, nothing audited
        self.assertEqual(len(self.audits()), audits)
        # set_bot(True) also clears a pause, still without restamping an ON switch.
        worker.pause_for_provider()
        self.assertEqual((bot_state(site.set_bot(True, ADMIN)), site.settings()["bot_enabled_at"]),
                         ("on", stamp))
        off = site.set_bot(False, ADMIN)
        self.assertEqual((off["bot_enabled"], off["bot_enabled_at"]), (False, stamp))
        self.assertGreater(site.set_bot(True, ADMIN)["bot_enabled_at"], stamp)
        # Unticking the confirmation refuses ON and changes nothing.
        with self.assertRaises(AutoReplyNotConfirmed):
            site.set_bot(True, ADMIN, auto_reply_confirmed=False)
        self.assertTrue(site.settings()["auto_reply_confirmed_off"])
        comments = site.set_comments(True, ADMIN)
        self.assertEqual((comments["comments_enabled"], comments["comments_changed_by"]),
                         (True, "boss"))
        self.assertIsNotNone(comments["comments_enabled_at"])
        self.assertEqual(site.set_comments(True, ADMIN)["comments_enabled_at"],
                         comments["comments_enabled_at"])
        self.assertFalse(site.set_comments(False, "cli")["comments_enabled"])
        self.assertEqual(self.audits()[-1][:3], (None, "cli", "messenger.comments_off"))

    def test_settings_use_versions_and_refuse_bad_values_before_writing(self):
        site = self.site
        version = site.settings()["version"]
        saved = site.update_settings({"daily_turn_limit": 20, "fallback_en": "  Call us  "},
                                     ADMIN, expected_version=version)
        self.assertEqual((saved["daily_turn_limit"], saved["fallback_en"], saved["version"]),
                         (20, "Call us", version + 1))
        with self.assertRaises(MetaConflict):
            site.update_settings({"daily_turn_limit": 30}, ADMIN, expected_version=version)
        for fields, problem in (({"bot_enabled": True}, {"bot_enabled": "UNKNOWN"}),
                                ({"daily_turn_limit": 5001}, {"daily_turn_limit": "INVALID"}),
                                ({"retention_days": True}, {"retention_days": "INVALID"}),
                                ({"retention_days": 366}, {"retention_days": "INVALID"}),
                                ({"fallback_ka": "x" * 501}, {"fallback_ka": "INVALID"}),
                                ({}, {"fields": "EMPTY"})):
            with self.assertRaises(InvalidFields) as refused:
                site.update_settings(fields, ADMIN)
            self.assertEqual(refused.exception.fields, problem)
        self.assertEqual(site.settings()["daily_turn_limit"], 20)
        self.assertEqual(self.audits()[-1][2:], (
            "messenger.settings_changed", "settings",
            {"fields": ["daily_turn_limit", "fallback_en"], "version": version + 1}))

    # -- connection --------------------------------------------------------------------------

    def test_connection_tokens_are_sealed_and_only_the_dashboard_login_stores_them(self):
        site, worker = self.site, self.worker
        page = site.save_connection(PAGE, "Garage • ფარეხი 1886", APP, tokens(), ADMIN)
        self.assertEqual((page["page_id"], page["page_name"], page["app_id"], page["version"],
                          page["subscribed_fields"]), (PAGE, "Garage • ფარეხი 1886", APP, 1, []))
        self.assertNotIn("token_enc", page)
        self.assertEqual((site.token("page"), worker.token("user")), (PAGE_TOKEN, USER_TOKEN))
        self.assertIsNone(worker.token("send_page"))
        stored = " ".join(row[0] for row in self.admin_sql(
            "SELECT token_enc FROM garage.meta_tokens"))
        self.assertNotIn(PAGE_TOKEN, stored)
        self.assertNotIn(USER_TOKEN, stored)
        rows = worker.token_rows()
        self.assertEqual([row["purpose"] for row in rows], ["page", "user"])
        self.assertTrue(all("token_enc" not in row and "token" not in row for row in rows))
        self.assertEqual((rows[0]["status"], rows[0]["expires_at"], rows[0]["subject_id"]),
                         ("ok", None, PAGE))
        self.assertEqual(self.audits()[-1][2:], ("messenger.connected", PAGE,
                                                 {"purposes": ["page", "user"],
                                                  "page_changed": False}))
        # The worker records health and subscriptions, but cannot store or drop a connection.
        self.assertTrue(worker.mark_token("user", "broken", "code_190"))
        self.assertTrue(worker.mark_token("page", "ok", expiry={
            "scopes": ["pages_messaging"], "expires_at": None,
            "data_access_expires_at": at(60 * 24 * 90)}))
        self.assertEqual({row["purpose"]: (row["status"], row["status_detail"])
                          for row in site.token_rows()},
                         {"page": ("ok", ""), "user": ("broken", "code_190")})
        self.assertTrue(worker.set_subscription(["messages", "feed"]))
        for call in (lambda: worker.save_connection(PAGE, "x", APP, tokens(), "worker"),
                     lambda: worker.disconnect("worker")):
            with self.assertRaises(errors.InsufficientPrivilege):
                call()
        # Reconnecting the same page through the same app keeps the subscription and the
        # send-only token; a different page starts clean.
        site.save_connection(PAGE, "Garage", OTHER_APP, {"send_page": {
            "token": "EAAsendOnly0003", "app_id": OTHER_APP, "subject_id": PAGE}}, ADMIN)
        self.assertEqual(site.page()["subscribed_fields"], [])  # the receiving app changed
        site.save_connection(PAGE, "Garage", APP, tokens(), ADMIN)
        site.set_subscription(["messages", "feed"])
        again = site.save_connection(PAGE, "Garage", APP, tokens(page_token="EAArenewed0004"),
                                     ADMIN)
        self.assertEqual((again["subscribed_fields"], again["version"]), (["messages", "feed"], 4))
        self.assertEqual((site.token("page"), site.token("send_page")),
                         ("EAArenewed0004", "EAAsendOnly0003"))
        moved = site.save_connection("110000000000009", "Other", APP,
                                     tokens(page_id="110000000000009"), ADMIN)
        self.assertEqual(moved["subscribed_fields"], [])
        self.assertIsNone(site.token("send_page"))
        self.assertEqual(self.audits()[-1][4], {"purposes": ["page", "user"],
                                                "page_changed": True})
        removed = site.disconnect(ADMIN)
        self.assertEqual(removed, {"page_id": "110000000000009",
                                   "tokens_removed": ["page", "user"]})
        self.assertEqual((site.page(), site.token_rows(), site.token("page")), (None, [], None))
        self.assertEqual(self.audits()[-1][2:], ("messenger.disconnected", "110000000000009",
                                                 {"tokens": ["page", "user"]}))

    def test_a_token_sealed_for_one_purpose_or_key_cannot_be_read_as_another(self):
        self.site.save_connection(PAGE, "Garage", APP, tokens(), ADMIN)
        self.admin_sql("UPDATE garage.meta_tokens SET token_enc = (SELECT token_enc FROM "
                       "garage.meta_tokens WHERE purpose = 'page') WHERE purpose = 'user'")
        from automotive.meta_crypto import TokenCipherError
        with self.assertRaises(TokenCipherError):
            self.worker.token("user")
        stranger = MetaStore(self.worker.db, TokenCipher(os.urandom(32)))
        with self.assertRaises(TokenCipherError):
            stranger.token("page")

    # -- inbox ---------------------------------------------------------------------------------

    def test_claim_inbox_skips_locked_rows_and_leases_what_it_hands_out(self):
        worker = self.worker
        first, second, third = (self.deliver({"sender": {"id": PSID}, "n": n}) for n in range(3))
        del first, second, third
        with psycopg.connect(self.url) as holder:
            holder.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles["worker"])))
            holder.execute("SELECT inbox_id FROM garage.meta_inbox WHERE inbox_id = 1 FOR UPDATE")
            claimed = worker.claim_inbox(10)  # row 1 is locked by another transaction
            self.assertEqual([row["inbox_id"] for row in claimed], [2, 3])
            holder.rollback()
        self.assertEqual(set(claimed[0]), {"inbox_id", "event_key", "field", "page_id",
                                           "payload", "received_at", "attempts"})
        self.assertEqual((claimed[0]["payload"], claimed[0]["attempts"], claimed[0]["page_id"]),
                         ({"sender": {"id": PSID}, "n": 1}, 1, PAGE))
        # Rows 2 and 3 are leased; only row 1 is left for the next poll.
        self.assertEqual([row["inbox_id"] for row in worker.claim_inbox(10)], [1])
        self.assertEqual(worker.claim_inbox(10), [])
        self.assertTrue(worker.finish_inbox(2, "stored"))
        self.assertTrue(worker.fail_inbox(3, "graph_code_2"))  # will be retried
        retried = worker.claim_inbox(10)
        self.assertEqual([(row["inbox_id"], row["attempts"]) for row in retried], [(3, 2)])
        self.assertFalse(worker.fail_inbox(3, "graph_code_2", max_attempts=2))  # gives up
        # A lease the worker never finished (it died) is handed out again after it expires.
        self.admin_sql("UPDATE garage.meta_inbox SET processed_at = now() - interval '1 hour' "
                       "WHERE inbox_id = 1")
        self.assertEqual([(row["inbox_id"], row["attempts"]) for row in worker.claim_inbox(10)],
                         [(1, 2)])
        self.admin_sql("UPDATE garage.meta_inbox SET processed_at = now() - interval '1 hour', "
                       "attempts = 5 WHERE inbox_id = 1")
        self.assertEqual(worker.claim_inbox(10), [])  # out of attempts: ends as failed
        events = self.site.recent_events(10)
        self.assertEqual([(row["inbox_id"], row["outcome"]) for row in events],
                         [(3, "failed: graph_code_2"), (2, "stored"),
                          (1, "failed: lease_expired")])
        self.assertEqual(set(events[0]), {"inbox_id", "field", "received_at", "processed_at",
                                          "attempts", "outcome"})
        self.assertEqual(self.site.last_event_at()["field"], "messages")
        self.assertEqual(self.site.counts_today()["webhooks"], 3)

    # -- chats ----------------------------------------------------------------------------------

    def test_older_pages_follow_the_order_shown_even_when_a_reply_was_stored_late(self):
        worker, site = self.worker, self.site
        worker.add_message(PSID, "m_a", "customer", at(0), page_id=PAGE)
        worker.add_message(PSID, "m_c", "customer", at(0) + timedelta(seconds=5), page_id=PAGE)
        # The bot's reply to m_a is stored after m_c, stamped just after m_a (as _sent_at does).
        worker.add_message(PSID, "m_r", "bot", at(0) + timedelta(milliseconds=1), page_id=PAGE)
        seen, before = [], None
        for _ in range(4):
            page = site.messages(PSID, 1, before_id=before)
            if not page:
                break
            seen.append(page[0]["mid"])
            before = page[0]["message_id"]
        self.assertEqual(seen, ["m_c", "m_r", "m_a"])
        # An anchor that is gone (purged meanwhile) still pages by id.
        self.assertEqual([row["mid"] for row in site.messages(PSID, 5, before_id=999)],
                         ["m_a", "m_r", "m_c"])

    def test_messages_are_stored_once_and_unanswered_means_after_the_last_page_message(self):
        worker = self.worker
        add = worker.add_message
        self.assertTrue(add(PSID, "m_c1", "customer", at(1), page_id=PAGE, body="F30 B46 2.0",
                            attachments=[{"type": "image", "payload": {"url": "https://x"}}]))
        self.assertTrue(add(PSID, "m_c2", "customer", int(at(2).timestamp() * 1000),
                            page_id=PAGE, quick_reply="CHOICE:2"))
        self.assertFalse(add(PSID, "m_c2", "customer", at(2), page_id=PAGE))  # Meta retried
        self.assertEqual([row["mid"] for row in worker.unanswered_customer_messages(PSID)],
                         ["m_c1", "m_c2"])
        self.assertTrue(add(PSID, "m_s1", "shop", at(3), page_id=PAGE, body="Hand-typed",
                            app_id=OTHER_APP))
        self.assertEqual(worker.unanswered_customer_messages(PSID), [])
        self.assertTrue(add(PSID, "m_c3", "customer", at(4), page_id=PAGE))
        self.assertTrue(add(PSID, "m_s2", "shop", at(5), page_id=PAGE, app_id="263902037430900"))
        self.assertEqual(worker.unanswered_customer_messages(PSID), [])
        self.assertEqual([row["mid"] for row in worker.unanswered_customer_messages(
            PSID, ignore_app_ids=["263902037430900"])], ["m_c3"])
        self.assertEqual(worker.last_page_message_at(PSID), at(5))
        stored = self.site.messages(PSID)
        self.assertEqual([row["mid"] for row in stored], ["m_c1", "m_c2", "m_s1", "m_c3", "m_s2"])
        self.assertEqual((stored[0]["attachments"], stored[1]["quick_reply"]),
                         ([{"type": "image"}], "CHOICE:2"))
        self.assertEqual([row["mid"] for row in self.site.messages(PSID, 2)], ["m_c3", "m_s2"])
        thread = self.site.get_thread(PSID)
        self.assertEqual((thread["last_customer_at"], thread["last_shop_at"],
                          thread["last_bot_at"], thread["bot_enabled"]),
                         (at(4), at(5), None, True))
        self.assertNotIn("engine_state", thread)
        # An older message arriving late never moves the times backwards.
        add(PSID, "m_c0", "customer", at(0), page_id=PAGE)
        self.assertEqual(self.site.get_thread(PSID)["last_customer_at"], at(4))
        with self.assertRaises(errors.InsufficientPrivilege):
            self.site.add_message(PSID, "m_x", "customer", at(6), page_id=PAGE)

    def test_chat_switches_admin_flag_attention_and_engine_state(self):
        worker, site = self.worker, self.site
        created = worker.upsert_thread(PSID, PAGE, customer_name="Nino", source="ads",
                                       source_ad_id="555")
        self.assertEqual((created["customer_name"], created["source"], created["version"]),
                         ("Nino", "ads", 1))
        kept = worker.upsert_thread(PSID, PAGE, conversation_id="t_abc-123")
        self.assertEqual((kept["customer_name"], kept["conversation_id"]), ("Nino", "t_abc-123"))
        worker.upsert_thread(OTHER_PSID, PAGE)
        off = site.set_thread_switch(PSID, False, ADMIN, expected_version=1)
        self.assertEqual((off["bot_enabled"], off["version"]), (False, 2))
        with self.assertRaises(MetaConflict):
            site.set_thread_switch(PSID, True, ADMIN, expected_version=1)
        self.assertIsNone(site.set_thread_switch("7000000000000000", True, ADMIN))
        self.assertTrue(site.set_admin(OTHER_PSID, True, ADMIN)["is_admin"])
        self.assertEqual(self.audits()[-2][2:4], ("messenger.chat_off", mask_psid(PSID)))
        self.assertEqual(self.audits()[-1][2:4], ("messenger.chat_admin_on", "…5678"))
        self.assertTrue(worker.flag_attention(PSID, "engine_failed"))
        self.assertEqual([row["psid"] for row in site.list_threads("attention")], [PSID])
        self.assertEqual([row["psid"] for row in site.list_threads("off")], [PSID])
        self.assertEqual([row["psid"] for row in site.list_threads("admin")], [OTHER_PSID])
        self.assertEqual([row["psid"] for row in site.list_threads("ads")], [PSID])
        self.assertEqual(len(site.threads("all", limit=1)), 1)
        self.assertTrue(site.flag_attention(PSID, "", needs=False, actor=ADMIN))
        self.assertEqual(self.audits()[-1][2], "messenger.attention_cleared")
        self.assertEqual(site.list_threads("attention"), [])
        state = {"vehicle": {"make": "BMW"}, "history": [["user", "F30"]], "language": "ka"}
        self.assertTrue(worker.save_engine_state(PSID, state))
        self.assertEqual(worker.get_thread(PSID, with_state=True)["engine_state"], state)
        with self.assertRaises(ValueError):
            worker.save_engine_state(PSID, {"blob": "x" * 300_000})
        with self.assertRaises(errors.InsufficientPrivilege):
            site.save_engine_state(PSID, state)
        worker.add_message(PSID, "m_c1", "customer", at(1), page_id=PAGE, body="x" * 300)
        listed = site.list_threads()
        # A chat without customer messages sorts by when it appeared (just now).
        self.assertEqual([row["psid"] for row in listed], [OTHER_PSID, PSID])
        self.assertEqual((listed[1]["last_sender"], len(listed[1]["last_body"])), ("customer", 120))
        self.assertIsNone(listed[0]["last_sender"])
        self.assertEqual(worker.apply_conversation_names([
            {"psid": PSID, "name": "Nino B.", "conversation_id": "t_xyz"},
            {"psid": "7000000000000000", "name": "Stranger", "conversation_id": "t_q"},
            {"psid": OTHER_PSID, "name": None, "conversation_id": "not-a-conversation"}]), 1)
        self.assertEqual(site.get_thread(PSID)["customer_name"], "Nino B.")
        self.assertIsNone(site.get_thread("7000000000000000"))
        self.admin_sql("UPDATE garage.messenger_threads SET last_customer_at = now() - "
                       "interval '30 hours'")
        self.assertEqual([row["psid"] for row in site.probe_candidates(PAGE)], [PSID])

    # -- replies and caps ------------------------------------------------------------------------

    def test_a_trigger_is_answered_once_and_turns_count_engine_runs(self):
        worker = self.worker
        first = worker.start_reply("messenger", PSID, "m_2", ["m_1", "m_2"])
        self.assertIsInstance(first, int)
        self.assertIsNone(worker.start_reply("messenger", PSID, "m_2", ["m_2"]))
        comment = worker.start_reply("comment", None, "m_2")
        self.assertNotEqual(comment, first)
        self.assertTrue(worker.add_sent_mid(first, "m_out1"))
        self.assertTrue(worker.finish_reply(first, "sent", engine_status="ok",
                                            sent_mids=["m_out1", "m_out2"],
                                            usage={"input_tokens": 900}, cost_usd=0.031))
        # Closed before the engine ran (no engine_status): a switch, the cap, a dead token,
        # Meta refusing the typing indicator, a newer message. None of these is a turn.
        for status, engine in (("skipped_old", ""), ("skipped_off", "ok"),
                               ("skipped_cap", ""), ("failed", ""), ("blocked_by_meta", ""),
                               ("skipped_superseded", "")):
            reply = worker.start_reply("messenger", PSID, f"m_{status}")
            worker.finish_reply(reply, status, engine_status=engine)
        worker.start_reply("messenger", OTHER_PSID, "m_pending")
        worker.finish_reply(worker.start_reply("command", PSID, "m_cmd"), "sent")
        with self.assertRaises(ValueError):
            worker.finish_reply(first, "pending")
        with self.assertRaises(ValueError):
            worker.finish_reply(first, "sent", text="never stored")
        # sent + skipped_off after the engine ran + pending + the comment = 4; command and
        # pre-engine skips are not turns.
        self.assertEqual(worker.turns_today(), 4)
        self.assertEqual(worker.turns_today(PSID), 2)
        self.assertAlmostEqual(self.site.cost_today(), 0.031)
        row = self.site.replies(psid=PSID, channel="messenger", status="sent")[0]
        self.assertEqual((row["sent_mids"], row["usage"], row["engine_status"], row["cost_usd"]),
                         (["m_out1", "m_out2"], {"input_tokens": 900}, "ok", 0.031))
        self.assertIsNotNone(row["finished_at"])
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.admin_sql("UPDATE garage.messenger_replies SET started_at = now() - "
                       "interval '1 hour' WHERE trigger_key = 'm_pending'")
        self.assertEqual(worker.abandon_stale_replies(600), 1)
        counts = self.site.counts_today()
        # The abandoned attempt still counts as a turn: its engine may have run.
        self.assertEqual({key: counts[key] for key in ("replies_sent", "failed", "skipped",
                                                       "turns")},
                         {"replies_sent": 2, "failed": 3, "skipped": 4, "turns": 4})
        with self.assertRaises(errors.InsufficientPrivilege):
            self.site.start_reply("messenger", PSID, "m_site")

    # -- jobs -------------------------------------------------------------------------------------

    def test_job_requests_and_runs(self):
        site, worker = self.site, self.worker
        request = site.request_job("names", ADMIN)
        self.assertEqual(site.request_job("names", ADMIN), request)  # one open request per job
        site.request_job("ads", "cli")
        self.assertEqual([row["job"] for row in worker.take_job_requests()], ["names", "ads"])
        self.assertEqual(worker.take_job_requests(), [])
        self.assertEqual([row[2:4] for row in self.audits()],
                         [("messenger.job_requested", "names"), ("messenger.job_requested", "ads")])
        run = worker.start_run("names")
        self.assertTrue(worker.finish_run(run, "ok", 12, "updated"))
        worker.finish_run(worker.start_run("ads"), "error", 0, "graph_code_190")
        self.assertEqual([(row["job"], row["status"]) for row in site.last_runs()],
                         [("ads", "error"), ("names", "ok")])
        self.assertEqual([row["job"] for row in site.latest_runs()], ["ads", "names"])
        self.assertEqual(site.last_runs(job="names")[0]["records"], 12)
        for call in (lambda: site.start_run("names"), lambda: worker.request_job("x", "w")):
            with self.assertRaises(errors.InsufficientPrivilege):
                call()

    # -- retention and erasure ---------------------------------------------------------------------

    def test_purge_removes_only_what_retention_allows(self):
        worker = self.worker
        self.deliver()
        self.deliver()
        # The purge runs once a day, so a copy may wait a day past the cut: six days, so none
        # outlives the seven the privacy page promises.
        self.admin_sql("UPDATE garage.meta_inbox SET received_at = now() - interval "
                       "'6 days 1 hour' WHERE inbox_id = 1")
        old = datetime.now(UTC) - timedelta(days=400)
        worker.add_message(PSID, "m_old", "customer", old, page_id=PAGE)
        worker.add_message(OTHER_PSID, "m_new", "customer", at(1), page_id=PAGE)
        self.admin_sql("UPDATE garage.messenger_threads SET first_seen_at = %s "
                       "WHERE psid = %s", (old, PSID))
        worker.start_reply("messenger", PSID, "m_old")
        worker.start_reply("messenger", OTHER_PSID, "m_new")
        worker.finish_run(worker.start_run("names"), "ok")
        self.site.request_job("names", ADMIN)
        self.admin_sql("UPDATE garage.messenger_replies SET started_at = %s "
                       "WHERE trigger_key = 'm_old'", (old,))
        self.admin_sql("UPDATE garage.meta_sync_runs SET started_at = %s", (old,))
        self.admin_sql("UPDATE garage.meta_job_requests SET requested_at = %s", (old,))
        self.assertEqual(worker.purge(), {"inbox": 1, "replies": 1, "threads": 0, "runs": 1,
                                          "job_requests": 1, "forgotten": 0})
        # Messages are kept for good, so a 400-day-old one stays, and its chat with it.
        self.assertEqual(sorted(row["psid"] for row in self.site.list_threads()),
                         sorted([PSID, OTHER_PSID]))
        self.assertEqual(sorted(row[0] for row in self.admin_sql(
            "SELECT mid FROM garage.messenger_messages")), ["m_new", "m_old"])
        self.assertEqual(worker.purge(30), {"inbox": 0, "replies": 0, "threads": 0, "runs": 0,
                                            "job_requests": 0, "forgotten": 0})
        with self.assertRaises(errors.InsufficientPrivilege):
            self.site.purge()

    def test_delete_customer_erases_the_chat_with_either_login(self):
        worker, site = self.worker, self.site
        for psid in (PSID, OTHER_PSID):
            worker.add_message(psid, f"m_{psid}", "customer", at(1), page_id=PAGE, body="VIN")
            worker.start_reply("messenger", psid, f"m_{psid}")
            self.deliver({"sender": {"id": psid}, "message": {"text": "hello"}})
        self.deliver({"sender": {"id": PAGE}, "recipient": {"id": PSID}})  # an echo to them
        removed = site.delete_customer(PSID, ADMIN)
        self.assertEqual(removed, {"threads": 1, "messages": 1, "replies": 1, "comments": 0,
                                   "inbox": None})
        self.assertIsNone(site.get_thread(PSID))
        self.assertEqual(site.messages(PSID), [])
        self.assertEqual(site.replies(psid=PSID), [])
        self.assertEqual(self.audits()[-1][2:], ("messenger.customer_deleted", "…1234", removed))
        self.assertNotIn(PSID, str(self.audits()))
        # The worker may also erase the webhook items that name the customer.
        self.assertEqual(worker.delete_customer(OTHER_PSID, "worker"),
                         {"threads": 1, "messages": 1, "replies": 1, "comments": 0, "inbox": 1})
        self.assertEqual(worker.delete_customer(PSID, "worker")["inbox"], 2)
        self.assertEqual(self.admin_sql("SELECT count(*) FROM garage.meta_inbox")[0][0], 0)

    def add_comment(self, comment_id: str, author: str, minutes_ago: int,
                    replied_psid: str = "", body: str = "ფასი?") -> None:
        self.admin_sql(
            "INSERT INTO garage.meta_comments (comment_id, post_id, author_id, author_name, "
            "body, created_time, source, reply_status, replied_psid) VALUES (%s, '555_1', %s, "
            "'ნინო', %s, now() - make_interval(mins => %s), 'webhook', %s, %s)",
            (comment_id, author, body, minutes_ago, "sent" if replied_psid else "",
             replied_psid))

    def test_two_caught_up_messages_in_one_second_are_claimed_where_the_worker_looks(self):
        same = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
        result = self.worker.add_caught_up(PSID, PAGE, [
            {"mid": "m_a", "sender": "customer", "sent_at": same, "body": "first"},
            {"mid": "m_b", "sender": "customer", "sent_at": same, "body": "second"}])
        self.assertEqual(result, {"stored": 2, "claimed": 4})  # both, on both channels
        trigger = self.worker.unanswered_customer_messages(PSID)[-1]["mid"]  # the worker's pick
        claims = self.admin_sql("SELECT DISTINCT trigger_key FROM garage.messenger_replies "
                                "ORDER BY 1")
        self.assertEqual(claims, [("m_a",), ("m_b",)])
        self.assertIn((trigger,), claims)
        self.assertIs(self.worker.deleted_after(PSID, same), False)  # never deleted

    def test_a_late_webhook_copy_from_before_a_deletion_never_brings_the_chat_back(self):
        worker, site = self.worker, self.site
        old = datetime.now(UTC) - timedelta(minutes=30)
        self.assertTrue(worker.add_message(PSID, "m_1", "customer", old, page_id=PAGE))
        site.delete_customer(PSID, ADMIN)
        # The worker was stopped: an unprocessed webhook copy of a message sent before the
        # deletion is stored as a duplicate would be (False), and the chat stays deleted.
        self.assertFalse(worker.add_message(PSID, "m_0", "customer", old, page_id=PAGE))
        self.assertTrue(worker.deleted_after(PSID, old))
        self.assertFalse(worker.deleted_after(PSID, datetime.now(UTC) + timedelta(seconds=5)))
        self.assertEqual(self.admin_sql("SELECT psid FROM garage.messenger_threads"), [])
        # What the customer writes afterwards starts a new chat, as before.
        self.assertTrue(worker.add_message(PSID, "m_2", "customer", datetime.now(UTC),
                                           page_id=PAGE))
        self.assertEqual(self.admin_sql("SELECT mid FROM garage.messenger_messages"),
                         [("m_2",)])

    def test_delete_customer_also_erases_their_comments_and_keeps_them_out(self):
        worker, site = self.worker, self.site
        worker.add_message(PSID, "m_1", "customer", at(1), page_id=PAGE, body="hello")
        # A comment the bot answered into this chat, the same author's other comment, and
        # another person's comment under the same post.
        self.add_comment("555_1001", AUTHOR, 60, PSID, body="ჩემი ნომერი 599 00 00 00")
        self.add_comment("555_1002", AUTHOR, 50)
        self.add_comment("555_1003", OTHER_AUTHOR, 40)
        removed = site.delete_customer(PSID, ADMIN)
        self.assertEqual(removed, {"threads": 1, "messages": 1, "replies": 0, "comments": 2,
                                   "inbox": None})
        self.assertEqual(self.admin_sql("SELECT comment_id FROM garage.meta_comments"),
                         [("555_1003",)])
        self.assertEqual(self.audits()[-1][2:], ("messenger.customer_deleted", "…1234", removed))
        # What keeps them out names nobody: hashes of the author, of the two comments and of the
        # chat (the Messenger catch-up job never reads its messages back).
        kept = self.admin_sql("SELECT subject_key, forgotten_at FROM garage.meta_forgotten")
        self.assertEqual(sorted(row[0] for row in kept),
                         sorted(forgotten_key(subject) for subject in (
                             f"author:{AUTHOR}", "comment:555_1001", "comment:555_1002",
                             f"chat:{PSID}")))
        for raw in (AUTHOR, "555_1001", "555_1002", PSID):
            self.assertNotIn(raw, str(kept))
        # Either login can read when each was forgotten; the backfill and webhooks ask this.
        with worker.db.transaction() as connection:
            found = forgotten_since(connection, [f"author:{AUTHOR}", "comment:555_1002",
                                                 f"author:{OTHER_AUTHOR}", f"chat:{PSID}"])
        self.assertEqual(set(found), {f"author:{AUTHOR}", "comment:555_1002", f"chat:{PSID}"})
        # A second request later moves the moment forward.
        self.admin_sql("UPDATE garage.meta_forgotten SET forgotten_at = now() - interval '2 days'")
        self.add_comment("555_1004", AUTHOR, 1, PSID)
        self.assertEqual(site.delete_customer(PSID, ADMIN)["comments"], 1)
        with site.db.transaction() as connection:
            again = forgotten_since(connection, [f"author:{AUTHOR}"])[f"author:{AUTHOR}"]
        self.assertGreater(again, datetime.now(UTC) - timedelta(minutes=5))
        # The receiver's login reaches none of it.
        self.assert_denied("hook", "SELECT * FROM garage.meta_forgotten")
        self.assert_denied("hook", "DELETE FROM garage.meta_comments")
        self.assert_denied("outsider", "SELECT * FROM garage.meta_forgotten")

    def test_purge_drops_forgotten_ids_once_the_backfill_cannot_reach_their_comments(self):
        self.add_comment("555_1001", AUTHOR, 60, PSID)
        self.site.delete_customer(PSID, ADMIN)
        self.admin_sql("UPDATE garage.meta_forgotten SET forgotten_at = now() - interval "
                       "'32 days' WHERE subject_key = %s", (forgotten_key("comment:555_1001"),))
        self.assertEqual(self.worker.purge()["forgotten"], 1)
        # The author's and the chat's are younger and stay.
        self.assertEqual(len(self.admin_sql("SELECT 1 FROM garage.meta_forgotten")), 2)

    def test_before_0014_is_applied_a_deletion_leaves_comments_and_nothing_fails(self):
        url = self.create_database("meta_before_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0013_meta_messenger")
            connection.execute(
                "INSERT INTO garage.meta_comments (comment_id, post_id, author_id, created_time, "
                "source, replied_psid) VALUES ('555_1', '555', %s, now(), 'webhook', %s)",
                (AUTHOR, PSID))
        worker = MetaStore(RoleDatabase(url, self.roles["worker"]), self.cipher)
        site = MetaStore(RoleDatabase(url, self.roles["site"]), self.cipher)
        worker.add_message(PSID, "m_1", "customer", at(1), page_id=PAGE)
        self.assertEqual(site.delete_customer(PSID, ADMIN),
                         {"threads": 1, "messages": 1, "replies": 0, "comments": None,
                          "inbox": None})
        self.assertIsNone(worker.purge()["forgotten"])
        with worker.db.transaction() as connection:
            self.assertEqual(forgotten_since(connection, [f"author:{AUTHOR}"]), {})

    def test_the_0014_down_script_restores_0013_and_up_applies_again(self):
        url = self.create_database("meta_forget_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)

        def privileges(connection) -> tuple:
            return connection.execute(
                "SELECT to_regclass('garage.meta_forgotten') IS NOT NULL, "
                "has_table_privilege('garage_site', 'garage.meta_comments', 'DELETE'), "
                "has_sequence_privilege('garage_webhook', "
                "pg_get_serial_sequence('garage.meta_inbox', 'inbox_id'), 'USAGE')").fetchone()

        with psycopg.connect(url) as connection:
            self.assertEqual(privileges(connection), (True, True, False))
            connection.execute(FORGET_DOWN)
            self.assertEqual(privileges(connection), (False, False, True))
            connection.execute(FORGET_UP)
            self.assertEqual(privileges(connection), (True, True, False))
            # Rerunning provisioning keeps 0014's grants.
            connection.execute(ROLES_SQL)
            self.assertEqual(privileges(connection), (True, True, False))

    # -- least privilege ---------------------------------------------------------------------------

    def test_each_login_reaches_only_what_its_job_needs(self):
        self.worker.add_message(PSID, "m_c1", "customer", at(1), page_id=PAGE)
        self.site.save_connection(PAGE, "Garage", APP, tokens(), ADMIN)
        self.deliver()
        # The dashboard sees the inbox's bookkeeping, never a payload or an event key.
        with self.acting_as(self.roles["site"]) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*), max(received_at) IS NOT NULL FROM garage.meta_inbox "
                "WHERE processed_at IS NULL").fetchone(), (1, True))
            connection.execute("UPDATE garage.messenger_threads SET bot_enabled = false, "
                               "needs_attention = true, version = version + 1")
        denied = {
            "site": ("SELECT payload FROM garage.meta_inbox",
                     "SELECT event_key FROM garage.meta_inbox",
                     "SELECT * FROM garage.meta_inbox",
                     "UPDATE garage.meta_inbox SET outcome = 'x'",
                     "DELETE FROM garage.meta_inbox",
                     "UPDATE garage.messenger_threads SET customer_name = 'x'",
                     "UPDATE garage.messenger_threads SET engine_state = NULL",
                     "UPDATE garage.messenger_threads SET psid = '1'",
                     "INSERT INTO garage.messenger_threads (psid, page_id) VALUES ('1', '1')",
                     "UPDATE garage.messenger_messages SET body = 'forged'",
                     "INSERT INTO garage.meta_sync_runs (job) VALUES ('forged')",
                     "UPDATE garage.audit_log SET action = 'forged.entry'",
                     "DELETE FROM garage.audit_log",
                     "DELETE FROM garage.meta_forgotten",
                     "UPDATE garage.meta_comments SET body = 'forged'"),
            "worker": ("INSERT INTO garage.meta_tokens (purpose, app_id, subject_id, token_enc) "
                       "VALUES ('ads_user', '1', '1', 'v1.a.b')",
                       "DELETE FROM garage.meta_tokens",
                       "INSERT INTO garage.meta_page (page_id, app_id) VALUES ('1', '1')",
                       "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                       "VALUES ('msg:" + "0" * 64 + "', 'messages', '1', '{}')",
                       "INSERT INTO garage.meta_job_requests (job) VALUES ('forged')",
                       "SELECT * FROM garage.audit_log",
                       "SELECT * FROM garage.staff_users",
                       "SELECT * FROM garage.staff_sessions",
                       "UPDATE garage.site_sections SET version = 9",
                       "SELECT * FROM warehouse.warehouse_products"),
            "hook": ("SELECT inbox_id FROM garage.meta_inbox",
                     # An identity column needs no sequence privilege (0014 revoked it).
                     "SELECT nextval('garage.meta_inbox_inbox_id_seq')",
                     "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                     "VALUES ('msg:" + "1" * 64 + "', 'messages', '1', '{}') "
                     "ON CONFLICT (event_key) DO NOTHING",
                     "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload, "
                     "processed_at) VALUES ('msg:" + "2" * 64 + "', 'messages', '1', '{}', now())",
                     "UPDATE garage.meta_inbox SET outcome = 'x'",
                     "SELECT * FROM garage.meta_tokens",
                     "SELECT * FROM garage.messenger_messages",
                     "INSERT INTO garage.audit_log (action) VALUES ('forged.entry')"),
            "outsider": ("SELECT * FROM garage.meta_settings",
                         "SELECT * FROM garage.meta_tokens",
                         "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                         "VALUES ('msg:" + "3" * 64 + "', 'messages', '1', '{}')"),
        }
        for role, statements in denied.items():
            for statement in statements:
                self.assert_denied(role, statement)
        thread = self.admin_sql("SELECT bot_enabled, needs_attention, customer_name "
                                "FROM garage.messenger_threads")[0]
        self.assertEqual(tuple(thread), (False, True, ""))
        # The receiver's append (no conflict target) is idempotent per event key.
        key = "msg:" + "4" * 64
        for _ in range(2):
            with self.acting_as(self.roles["hook"]) as connection:
                connection.execute("INSERT INTO garage.meta_inbox (event_key, field, page_id, "
                                   "payload) VALUES (%s, 'messages', %s, '{}') "
                                   "ON CONFLICT DO NOTHING", (key, PAGE))
        self.assertEqual(self.admin_sql("SELECT count(*) FROM garage.meta_inbox "
                                        "WHERE event_key = %s", (key,))[0][0], 1)


if __name__ == "__main__":
    unittest.main()
