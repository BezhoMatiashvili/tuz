"""The Messenger bot's test mode on a disposable PostgreSQL database (migration 0018).

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and roles this
file creates and drops. Store calls run as NOLOGIN members of garage_messenger (the worker) and
garage_site (the dashboard), so anything outside the migration's grants fails here as it would in
production. Covered: the switch's stamps and its one-mode rule, a test-mode row read by code that
knows only ``bot_enabled`` (it answers nobody), the tester flag and its grant, the Live pages'
"waiting" rule in test mode, the comment path's linked chat, and the 0018 down and up scripts.
"""

from __future__ import annotations

import contextlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.live_store import LiveStore
from automotive.meta_comments import CommentStore, chat_skip
from automotive.meta_store import MetaStore, answered_since, bot_mode, bot_state
from tests.test_meta_store_database import ADMIN_URL, ROLES_SQL, RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
UP = (ROOT / "migrations/0018_messenger_test_mode.up.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations/0018_messenger_test_mode.down.sql").read_text(encoding="utf-8")
PAGE = "110000000000001"
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
AUTHOR = "900000000000001"
BOSS = {"user_id": 7, "username": "boss"}
TABLES = ("messenger_threads", "messenger_messages", "messenger_replies", "meta_comments",
          "live_alerts", "audit_log")


def old_rule_answers(settings, thread, sent_at) -> bool:
    """The worker's rule before migration 0018 (an older worker still running, or a rollback):
    it knows only bot_enabled and bot_enabled_at."""
    enabled_at = settings.get("bot_enabled_at")
    return bool(settings.get("bot_enabled") and not settings.get("bot_paused_reason")
                and thread.get("bot_enabled") and enabled_at is not None
                and sent_at > enabled_at)


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class TestModeDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("test_mode_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"), ("outsider", None))}

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
        name = f"tmode_{key}_{cls.suffix}"
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
            connection.execute("INSERT INTO garage.meta_settings (settings_id, "
                               "auto_reply_confirmed_off) VALUES (1, TRUE)")
        self.worker = MetaStore(RoleDatabase(self.url, self.roles["worker"]))
        self.site = MetaStore(RoleDatabase(self.url, self.roles["site"]))

    @contextlib.contextmanager
    def acting_as(self, role: str):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles[role])))
            yield connection

    def admin_sql(self, statement: str, parameters: tuple = ()):
        with psycopg.connect(self.url) as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def chat(self, psid: str, *, minutes_ago: float = 1, tester: bool = False) -> None:
        """A chat whose customer wrote ``minutes_ago`` and got no answer yet."""
        self.worker.upsert_thread(psid, PAGE, customer_name=f"Customer {psid[-4:]}")
        self.worker.add_message(psid, f"m_{uuid.uuid4().hex}", "customer",
                                datetime.now(UTC) - timedelta(minutes=minutes_ago),
                                page_id=PAGE, body="ხუნდები გაქვთ?")
        if tester:
            self.site.set_tester(psid, True, BOSS)

    def waiting(self) -> dict[str, bool]:
        live = LiveStore(RoleDatabase(self.url, self.roles["site"]))
        return {chat["key"]: chat["waiting"] for chat in live.chats("all")
                if chat["channel"] == "messenger"}

    # -- the switch ------------------------------------------------------------------------------

    def test_the_modes_stamp_who_is_answered_since_when(self):
        test = self.site.set_bot_mode("test", BOSS)
        self.assertEqual((bot_mode(test), bot_state(test), test["bot_enabled"],
                          test["bot_test_mode"]), ("test", "test", False, True))
        self.assertIsNotNone(test["bot_test_at"])
        self.assertIsNone(test["bot_enabled_at"])
        self.assertEqual(answered_since(test, tester=True), test["bot_test_at"])
        self.assertIsNone(answered_since(test, tester=False))
        self.assertIs(test["auto_reply_confirmed_off"], False)  # asked again before everyone
        on = self.site.set_bot_mode("on", BOSS, True)
        self.assertEqual((bot_mode(on), on["bot_test_mode"]), ("on", False))
        self.assertEqual(on["bot_test_at"], test["bot_test_at"])  # a tester's turn goes on
        self.assertGreaterEqual(on["bot_enabled_at"], test["bot_test_at"])
        self.assertEqual(answered_since(on, tester=False), on["bot_enabled_at"])
        self.assertEqual(answered_since(on, tester=True), test["bot_test_at"])
        narrowed = self.site.set_bot_mode("test", BOSS)
        self.assertEqual((narrowed["bot_test_at"], narrowed["bot_enabled_at"]),
                         (test["bot_test_at"], on["bot_enabled_at"]))
        off = self.worker.set_bot(False, "messenger:9999")
        self.assertEqual((bot_mode(off), off["bot_test_mode"], off["bot_enabled"]),
                         ("off", False, False))
        again = self.site.set_bot_mode("test", BOSS)
        self.assertGreater(again["bot_test_at"], test["bot_test_at"])
        self.assertEqual([row[0] for row in self.admin_sql(
            "SELECT action FROM garage.audit_log ORDER BY audit_id")],
            ["messenger.bot_test", "messenger.bot_on", "messenger.bot_test",
             "messenger.bot_off", "messenger.bot_test"])
        detail = self.admin_sql("SELECT detail FROM garage.audit_log ORDER BY audit_id")[1][0]
        self.assertEqual((detail["from"], detail["changed"]), ("test", True))

    def test_test_mode_needs_no_auto_reply_tick_and_everyone_still_does(self):
        self.admin_sql("UPDATE garage.meta_settings SET auto_reply_confirmed_off = FALSE")
        self.assertEqual(bot_mode(self.site.set_bot_mode("test", BOSS)), "test")
        from automotive.meta_store import AutoReplyNotConfirmed
        with self.assertRaises(AutoReplyNotConfirmed):
            self.site.set_bot_mode("on", BOSS)
        self.assertEqual(bot_mode(self.site.settings()), "test")
        with self.assertRaises(ValueError):
            self.site.set_bot_mode("everyone", BOSS)

    def test_the_database_never_holds_both_modes(self):
        with self.assertRaises(errors.CheckViolation):
            self.admin_sql("UPDATE garage.meta_settings SET bot_enabled = TRUE, "
                           "bot_test_mode = TRUE")

    def test_code_that_knows_only_bot_enabled_answers_nobody_in_test_mode(self):
        self.chat(PSID, tester=True)
        settings = self.site.set_bot_mode("test", BOSS)
        thread = self.worker.get_thread(PSID)
        later = datetime.now(UTC) + timedelta(seconds=5)
        self.assertFalse(old_rule_answers(settings, thread, later))
        settings = self.site.set_bot_mode("on", BOSS, True)
        self.assertTrue(old_rule_answers(settings, thread, later))

    def test_a_provider_pause_shows_as_paused_and_resume_returns_to_test_mode(self):
        self.site.set_bot_mode("test", BOSS)
        self.assertTrue(self.worker.pause_for_provider())
        self.assertEqual(bot_state(self.site.settings()), "paused")
        self.assertEqual(bot_state(self.site.resume_bot(BOSS)), "test")

    # -- testers ---------------------------------------------------------------------------------

    def test_testers_are_marked_by_the_dashboard_audited_and_listed(self):
        self.chat(PSID)
        self.chat(OTHER_PSID)
        row = self.site.set_tester(PSID, True, BOSS, expected_version=1)
        self.assertIs(row["bot_tester"], True)
        self.assertEqual([(item["psid"], item["customer_name"]) for item in self.site.testers()],
                         [(PSID, "Customer 1234")])
        self.assertEqual([item["psid"] for item in self.site.list_threads("tester")], [PSID])
        self.assertEqual(self.admin_sql(
            "SELECT action, target FROM garage.audit_log ORDER BY audit_id"),
            [("messenger.chat_tester_on", "…1234")])
        self.assertIsNone(self.site.set_tester("7000000000000000", True, BOSS))
        self.site.set_tester(PSID, False, BOSS)
        self.assertEqual(self.worker.testers(), [])

    def test_only_the_dashboard_and_the_worker_may_change_the_tester_flag(self):
        self.chat(PSID)
        with self.acting_as("site") as connection:
            connection.execute("UPDATE garage.messenger_threads SET bot_tester = TRUE")
        with self.acting_as("outsider") as connection:
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("UPDATE garage.messenger_threads SET bot_tester = FALSE")
        # Provisioning again (a role created after the migration) keeps the grant.
        self.admin_sql("SELECT garage.meta_provision()")
        with self.acting_as("site") as connection:
            connection.execute("UPDATE garage.messenger_threads SET bot_tester = FALSE")
        self.assertEqual(self.admin_sql("SELECT bot_tester FROM garage.messenger_threads"),
                         [(False,)])

    def test_a_reply_attempt_may_end_as_not_a_tester(self):
        self.chat(PSID)
        reply_id = self.worker.start_reply("messenger", PSID, "m_trigger")
        self.assertTrue(self.worker.finish_reply(reply_id, "skipped_not_tester"))
        with self.assertRaises(errors.CheckViolation):
            self.admin_sql("UPDATE garage.messenger_replies SET status = 'skipped_everyone'")

    # -- the Live pages --------------------------------------------------------------------------

    def test_in_test_mode_everyone_but_the_testers_waits_for_staff_at_once(self):
        self.chat(PSID, tester=True)
        self.chat(OTHER_PSID)
        self.site.set_bot_mode("on", BOSS)
        self.assertEqual(self.waiting(), {f"m-{PSID}": False, f"m-{OTHER_PSID}": False})
        self.site.set_bot_mode("test", BOSS)
        self.assertEqual(self.waiting(), {f"m-{PSID}": False, f"m-{OTHER_PSID}": True})
        live = LiveStore(RoleDatabase(self.url, self.roles["site"]))
        self.assertTrue(live.pages_ready())
        pulse = live.pulse()
        self.assertEqual((pulse["messenger_bot"], pulse["waiting"]), ("test", 1))
        self.site.set_bot(False, BOSS)
        self.assertEqual(self.waiting(), {f"m-{PSID}": True, f"m-{OTHER_PSID}": True})

    def test_a_testers_unanswered_message_still_waits_after_the_bots_grace(self):
        self.chat(PSID, minutes_ago=10, tester=True)
        self.site.set_bot_mode("test", BOSS)
        self.assertEqual(self.waiting(), {f"m-{PSID}": True})

    # -- comments --------------------------------------------------------------------------------

    def test_a_comment_reply_in_test_mode_needs_a_linked_tester_chat(self):
        comments = CommentStore(RoleDatabase(self.url, self.roles["worker"]))
        self.chat(PSID)
        settings = self.site.set_bot_mode("test", BOSS)
        self.assertIsNone(comments.linked_chat(AUTHOR))
        self.assertEqual(chat_skip(settings, comments.linked_chat(AUTHOR)), "skipped_not_tester")
        comments.save({"comment_id": "555_1001", "post_id": "555", "parent_id": "",
                       "author_id": AUTHOR, "author_name": "Giorgi", "body": "ფასი?",
                       "created_time": datetime.now(UTC), "hidden": False, "permalink": ""},
                      "webhook")
        comments.set_status("555_1001", "sent", force=True, replied_psid=PSID)
        self.assertEqual(chat_skip(settings, comments.linked_chat(AUTHOR)), "skipped_not_tester")
        self.site.set_tester(PSID, True, BOSS)
        self.assertEqual(chat_skip(settings, comments.linked_chat(AUTHOR)), "")
        self.assertEqual(chat_skip(self.site.set_bot(False, BOSS), None), "")  # off: the
        # comment switch alone decides, as before test mode existed
        self.site.set_thread_switch(PSID, False, BOSS)
        self.assertEqual(chat_skip(settings, comments.linked_chat(AUTHOR)), "skipped_chat_off")

    # -- the migration ---------------------------------------------------------------------------

    def test_the_0018_down_script_ends_test_mode_and_up_applies_again(self):
        url = self.create_database("test_mode_down_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
            connection.execute("INSERT INTO garage.messenger_threads (psid, page_id, bot_tester) "
                               "VALUES (%s, %s, TRUE)", (PSID, PAGE))
            connection.execute("INSERT INTO garage.messenger_replies (channel, psid, "
                               "trigger_key, status) VALUES ('messenger', %s, 'm_1', "
                               "'skipped_not_tester')", (PSID,))
            connection.execute("UPDATE garage.meta_settings SET bot_test_mode = TRUE, "
                               "bot_test_at = now()")
            connection.execute(DOWN)
            settings = connection.execute(
                "SELECT bot_enabled FROM garage.meta_settings").fetchone()
            self.assertEqual(settings, (False,))  # test mode left: the bot is off
            self.assertEqual(connection.execute(
                "SELECT status FROM garage.messenger_replies").fetchall(), [("skipped_off",)])
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'garage' "
                "AND column_name IN ('bot_tester', 'bot_test_mode', 'bot_test_at')").fetchone(),
                (0,))
            with self.assertRaises(errors.CheckViolation):
                with connection.transaction():
                    connection.execute("UPDATE garage.messenger_replies "
                                       "SET status = 'skipped_not_tester'")
            connection.execute(UP)
            self.assertEqual(connection.execute(
                "SELECT bot_tester FROM garage.messenger_threads").fetchall(), [(False,)])
            connection.execute("UPDATE garage.messenger_replies SET status = 'skipped_not_tester'")

    def test_before_0018_the_live_pages_say_not_set_up(self):
        url = self.create_database("test_mode_old_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0017_live_alert_detail")
        live = LiveStore(RoleDatabase(url, self.roles["site"]))
        self.assertTrue(live.ready())
        self.assertFalse(live.pages_ready())


if __name__ == "__main__":
    unittest.main()
