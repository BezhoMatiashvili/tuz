"""Clearing a tester's bot memory on a disposable PostgreSQL database (migration 0023).

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and roles this
file creates and drops. Store calls run as NOLOGIN members of garage_messenger (the worker) and
garage_site (the dashboard), so anything outside the migration's grants fails here as it would in
production. Covered: what a clear changes and audits, who may clear and which chats, the cutoff on
the messages the bot reads, a state saved under an older read (a turn or a comment reply under way
during the clear), the Live pages' "waiting" rule, and the 0023 down and up scripts.
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
from automotive.meta_comments import CommentStore
from automotive.meta_store import MetaConflict, MetaStore, NotTester
from tests.test_meta_store_database import ADMIN_URL, ROLES_SQL, RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
UP = (ROOT / "migrations/0023_messenger_clear_context.up.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations/0023_messenger_clear_context.down.sql").read_text(encoding="utf-8")
PAGE = "110000000000001"
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
BOSS = {"user_id": 7, "username": "boss"}
STATE = {"language": "ka", "vehicle": {"make": "Toyota", "model": "Camry"},
         "history": [{"question": "Camry 2015 წინა ხუნდები", "answer": "გვაქვს", "shown": []}]}
TABLES = ("messenger_threads", "messenger_messages", "messenger_replies", "meta_comments",
          "live_alerts", "audit_log")


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class ClearContextDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("clear_context_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"),
            ("hook", "garage_webhook"), ("outsider", None))}

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
        name = f"clearctx_{key}_{cls.suffix}"
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

    def say(self, psid: str, text: str, *, minutes_ago: float = 0) -> str:
        mid = f"m_{uuid.uuid4().hex}"
        self.worker.add_message(psid, mid, "customer",
                                datetime.now(UTC) - timedelta(minutes=minutes_ago),
                                page_id=PAGE, body=text)
        return mid

    def chat(self, psid: str, *, tester: bool = True, state: dict | None = None) -> None:
        """A chat the bot has a memory of; a tester's unless said otherwise."""
        self.worker.upsert_thread(psid, PAGE, customer_name=f"Customer {psid[-4:]}")
        self.worker.save_engine_state(psid, STATE if state is None else state)
        if tester:
            self.site.set_tester(psid, True, BOSS)

    def stored(self, psid: str = PSID) -> tuple:
        return self.admin_sql(
            "SELECT engine_state, context_cleared_at FROM garage.messenger_threads "
            "WHERE psid = %s", (psid,))[0]

    def unread(self, psid: str = PSID) -> list[str]:
        return [row["body"] for row in self.worker.unanswered_customer_messages(psid)]

    def waiting(self) -> dict[str, bool]:
        live = LiveStore(RoleDatabase(self.url, self.roles["site"]))
        return {chat["key"]: chat["waiting"] for chat in live.chats("all")
                if chat["channel"] == "messenger"}

    # -- the clear -------------------------------------------------------------------------------

    def test_the_dashboard_clears_a_testers_memory_and_it_is_audited(self):
        self.chat(PSID)
        before = self.site.get_thread(PSID)
        self.assertEqual(self.stored(), (STATE, None))
        row = self.site.clear_context(PSID, BOSS, expected_version=before["version"],
                                      client_ip="203.0.113.7")
        self.assertEqual(row["version"], before["version"] + 1)
        self.assertIsNotNone(row["context_cleared_at"])
        self.assertGreater(row["engine_state_at"], before["engine_state_at"])
        self.assertEqual(self.stored(), (None, row["context_cleared_at"]))
        self.assertIsNone(self.worker.get_thread(PSID, with_state=True)["engine_state"])
        self.assertEqual(self.admin_sql(
            "SELECT action, target, detail FROM garage.audit_log ORDER BY audit_id DESC LIMIT 1"),
            [("messenger.chat_context_cleared", "…1234", {"version": row["version"]})])
        # The worker's login may clear too, and a second clear moves the stamp on.
        again = self.worker.clear_context(PSID, "worker")
        self.assertGreater(again["context_cleared_at"], row["context_cleared_at"])

    def test_the_messages_stay_for_staff(self):
        self.chat(PSID)
        self.say(PSID, "Camry 2015 წინა ხუნდები", minutes_ago=2)
        self.worker.add_message(PSID, "m_bot", "bot", datetime.now(UTC) - timedelta(minutes=1),
                                page_id=PAGE, body="გვაქვს")
        self.site.clear_context(PSID, BOSS)
        self.assertEqual([row["body"] for row in self.site.messages(PSID)],
                         ["Camry 2015 წინა ხუნდები", "გვაქვს"])

    def test_only_a_testers_chat_is_cleared_and_a_stale_version_is_refused(self):
        self.chat(PSID)
        self.chat(OTHER_PSID, tester=False)
        with self.assertRaises(NotTester):
            self.site.clear_context(OTHER_PSID, BOSS)
        self.assertEqual(self.stored(OTHER_PSID), (STATE, None))
        with self.assertRaises(MetaConflict):
            self.site.clear_context(PSID, BOSS, expected_version=99)
        self.assertEqual(self.stored(), (STATE, None))
        self.assertIsNone(self.site.clear_context("7000000000000000", BOSS))
        self.assertEqual(self.admin_sql(
            "SELECT count(*) FROM garage.audit_log "
            "WHERE action = 'messenger.chat_context_cleared'"), [(0,)])

    def test_the_dashboard_login_can_empty_a_testers_memory_and_nothing_more(self):
        self.chat(PSID)
        self.chat(OTHER_PSID, tester=False)
        with self.acting_as("site") as connection:
            for statement in ("UPDATE garage.messenger_threads SET engine_state = '{}'::jsonb",
                              "UPDATE garage.messenger_threads SET engine_state_at = now()",
                              "UPDATE garage.messenger_threads SET context_cleared_at = now()"):
                with self.assertRaises(errors.InsufficientPrivilege):
                    connection.execute(statement)
            # The function itself refuses a chat that is not a tester's.
            self.assertEqual(connection.execute(
                "SELECT garage.messenger_clear_context(%s, NULL)", (OTHER_PSID,)).fetchone(),
                (None,))
        self.assertEqual(self.stored(OTHER_PSID), (STATE, None))
        # Neither a stranger nor the webhook receiver's login, which may use the schema.
        for role in ("outsider", "hook"):
            with self.acting_as(role) as connection:
                with self.assertRaises(errors.InsufficientPrivilege):
                    connection.execute("SELECT garage.messenger_clear_context(%s, NULL)",
                                       (PSID,))
        self.assertEqual(self.stored(), (STATE, None))
        # Provisioning again (a role created after the migration) keeps the grant.
        self.admin_sql("SELECT garage.meta_provision()")
        self.assertIsNotNone(self.site.clear_context(PSID, BOSS))

    # -- what the bot reads ------------------------------------------------------------------------

    def test_the_bot_reads_nothing_sent_before_the_clear(self):
        self.chat(PSID)
        self.say(PSID, "ძველი კითხვა 1", minutes_ago=3)
        self.say(PSID, "ძველი კითხვა 2", minutes_ago=2)
        self.assertEqual(self.unread(), ["ძველი კითხვა 1", "ძველი კითხვა 2"])
        self.site.clear_context(PSID, BOSS)
        self.assertEqual(self.unread(), [])
        # One sent before the clear and stored only now (Meta retried, or the catch-up job).
        self.say(PSID, "გვიან მოსული ძველი", minutes_ago=1)
        self.assertEqual(self.unread(), [])
        self.admin_sql("UPDATE garage.messenger_threads "
                       "SET context_cleared_at = now() - interval '30 seconds'")
        self.say(PSID, "ახალი კითხვა")
        self.assertEqual(self.unread(), ["ახალი კითხვა"])

    def test_a_chat_never_cleared_reads_as_before(self):
        self.chat(PSID)
        self.say(PSID, "კითხვა", minutes_ago=5)
        self.assertEqual(self.unread(), ["კითხვა"])

    def test_a_state_saved_under_an_older_read_does_not_bring_the_memory_back(self):
        self.chat(PSID)
        read = self.worker.get_thread(PSID, with_state=True)  # a turn starts from this
        self.assertIsNone(read["context_cleared_at"])
        cleared = self.site.clear_context(PSID, BOSS)
        self.assertFalse(self.worker.save_engine_state(
            PSID, {**STATE, "history": [*STATE["history"], {"question": "კიდევ"}]},
            cleared_at=read["context_cleared_at"]))
        self.assertIsNone(self.stored()[0])
        # A turn that began after the clear saves as ever, and so does a second clear's.
        new = {"language": "ka", "history": [{"question": "ახალი", "answer": "", "shown": []}]}
        self.assertTrue(self.worker.save_engine_state(
            PSID, new, cleared_at=cleared["context_cleared_at"]))
        self.assertEqual(self.stored()[0], new)
        again = self.site.clear_context(PSID, BOSS)
        self.assertFalse(self.worker.save_engine_state(
            PSID, new, cleared_at=cleared["context_cleared_at"]))
        self.assertTrue(self.worker.save_engine_state(
            PSID, new, cleared_at=again["context_cleared_at"]))
        # Without a read to compare with, the save is unconditional, as before 0023.
        self.assertTrue(self.worker.save_engine_state(PSID, STATE))
        self.assertFalse(self.worker.save_engine_state("7000000000000000", STATE,
                                                       cleared_at=None))

    def test_a_comment_reply_that_read_the_old_state_does_not_put_it_back(self):
        self.chat(PSID)
        comments = CommentStore(RoleDatabase(self.url, self.roles["worker"]))
        read = comments.thread(PSID)
        self.site.clear_context(PSID, BOSS)
        self.assertFalse(comments.save_state(PSID, read["engine_state"], read["engine_state_at"]))
        self.assertIsNone(self.stored()[0])

    # -- the Live pages --------------------------------------------------------------------------

    def test_a_message_the_bot_will_never_answer_stops_waiting_after_the_clear(self):
        self.chat(PSID)
        self.site.set_bot_mode("test", BOSS)
        self.say(PSID, "ხუნდები გაქვთ?", minutes_ago=10)  # past the bot's grace: staff's now
        self.assertEqual(self.waiting(), {f"m-{PSID}": True})
        self.site.clear_context(PSID, BOSS)
        self.assertEqual(self.waiting(), {f"m-{PSID}": False})
        # A message stored after the clear waits again once the bot has had its time.
        self.say(PSID, "ახალი კითხვა", minutes_ago=5)
        self.assertEqual(self.waiting(), {f"m-{PSID}": True})

    # -- the scripts -----------------------------------------------------------------------------

    def test_the_0023_down_script_drops_the_clear_and_up_applies_again(self):
        url = self.create_database("clear_context_down_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
            connection.execute("INSERT INTO garage.messenger_threads (psid, page_id, bot_tester, "
                               "engine_state) VALUES (%s, %s, TRUE, '{}'::jsonb)", (PSID, PAGE))
            self.assertEqual(connection.execute(
                "SELECT garage.messenger_clear_context(%s)", (PSID,)).fetchone(), (2,))
            connection.execute(DOWN)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'garage' "
                "AND column_name = 'context_cleared_at'").fetchone(), (0,))
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'messenger_clear_context'"
            ).fetchone(), (0,))
            connection.execute("SELECT garage.meta_provision()")  # 0019's, without the function
            connection.execute(UP)
            self.assertEqual(connection.execute(
                "SELECT engine_state, context_cleared_at FROM garage.messenger_threads"
            ).fetchall(), [(None, None)])
            self.assertEqual(connection.execute(
                "SELECT garage.messenger_clear_context(%s)", (PSID,)).fetchone(), (3,))

    def test_before_0023_the_function_and_the_column_are_not_there(self):
        url = self.create_database("clear_context_old_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0022_oil_specification_research")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'garage' "
                "AND column_name = 'context_cleared_at'").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
