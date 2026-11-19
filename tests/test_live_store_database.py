"""LiveStore on a disposable PostgreSQL database, under the real capability roles.

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and roles this
file creates and drops. Every store call runs as a NOLOGIN role that is a member of
garage_messenger (the worker) or garage_site (the dashboard and the website process), so a query
outside migration 0015's grants fails here as it would in production.
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

from automotive.chat_log import ChatLogger
from automotive.live_alerts import AlertDraft, TurnAlerts
from automotive.live_store import (
    AlreadyOpen,
    LiveStore,
    parse_key,
    web_key,
)
from automotive.live_web import LiveRecorder
from automotive.meta_store import AlertsNotDeletable, MetaConflict, MetaStore
from tests.test_meta_store_database import ADMIN_URL, ROLES_SQL, RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
LIVE_UP = (ROOT / "migrations/0015_live_chats.up.sql").read_text(encoding="utf-8")
LIVE_DOWN = (ROOT / "migrations/0015_live_chats.down.sql").read_text(encoding="utf-8")
PAGE = "110000000000001"
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
WEB, OTHER_WEB = "a" * 32, "b" * 32
STAFF = {"user_id": 7, "username": "counter"}
BASE = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
TABLES = ("live_alerts", "live_web_chats", "live_web_messages", "messenger_threads",
          "messenger_messages", "messenger_replies", "meta_comments", "meta_forgotten",
          "audit_log")


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def draft(kind="part_not_found", reason="nothing_found", summary="front brake pads",
          car="", phone=""):
    return AlertDraft(kind, reason, summary, car, phone)


class KeyTests(unittest.TestCase):
    def test_keys_name_a_channel_and_a_chat(self):
        self.assertEqual(parse_key("m-" + PSID), ("messenger", PSID))
        self.assertEqual(parse_key("w-" + WEB), ("web", WEB))
        for bad in ("", "m-", "m-12ab", "w-" + "g" * 32, "w-" + "a" * 31, "x-1", None, 5,
                    "m-" + "1" * 31):
            with self.subTest(key=bad):
                self.assertIsNone(parse_key(bad))

    def test_a_website_chat_is_named_as_the_chat_log_names_it(self):
        self.assertEqual(web_key("abc-conversation-id"),
                         ChatLogger.session_key("abc-conversation-id"))
        self.assertEqual(len(web_key("x")), 32)


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class LiveStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("live_store_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"), ("hook", "garage_webhook"),
            ("outsider", None))}

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
        name = f"live_{key}_{cls.suffix}"
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
        # New stores per test: each keeps a short cache of its own.
        self.worker = LiveStore(RoleDatabase(self.url, self.roles["worker"]))
        self.site = LiveStore(RoleDatabase(self.url, self.roles["site"]))
        self.worker_chats = MetaStore(RoleDatabase(self.url, self.roles["worker"]))
        self.site_chats = MetaStore(RoleDatabase(self.url, self.roles["site"]))

    @contextlib.contextmanager
    def acting_as(self, role: str):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles[role])))
            yield connection

    def admin_sql(self, statement: str, parameters: tuple = ()):
        with psycopg.connect(self.url) as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def assert_denied(self, role: str, statement: str, parameters: tuple = ()) -> None:
        with self.acting_as(role) as connection:
            with self.assertRaises(errors.InsufficientPrivilege, msg=f"{role}: {statement}"):
                connection.execute(statement, parameters)

    def say(self, psid: str, mid: str, sender: str, minutes: float, body: str = "hello") -> None:
        self.worker_chats.add_message(psid, mid, sender, at(minutes), page_id=PAGE, body=body)

    def audits(self) -> list[tuple]:
        return [tuple(row) for row in self.admin_sql(
            "SELECT actor_username, action, target, detail FROM garage.audit_log "
            "ORDER BY audit_id")]

    # -- migration and privileges ----------------------------------------------------------------

    def test_the_0015_down_script_drops_the_tables_and_up_applies_again(self):
        url = self.create_database("live_down_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)

        def state(connection) -> tuple:
            return connection.execute(
                "SELECT to_regclass('garage.live_alerts') IS NOT NULL, "
                "to_regclass('garage.live_web_messages') IS NOT NULL, "
                "has_table_privilege('garage_site', 'garage.meta_comments', 'DELETE')"
            ).fetchone()

        with psycopg.connect(url) as connection:
            self.assertEqual(state(connection), (True, True, True))
            connection.execute(LIVE_DOWN)
            self.assertEqual(state(connection), (False, False, True))  # 0014's grants stay
            connection.execute(LIVE_UP)
            self.assertEqual(state(connection), (True, True, True))
            connection.execute(ROLES_SQL)  # provisioning again keeps 0015's grants
            self.assertTrue(connection.execute(
                "SELECT has_table_privilege('garage_messenger', 'garage.live_alerts', 'INSERT')"
            ).fetchone()[0])

    def test_each_login_reaches_only_what_its_job_needs(self):
        self.worker.raise_alert("messenger", PSID, draft())
        self.site.web_customer(WEB, "hello")
        denied = {
            "worker": ("SELECT * FROM garage.live_web_chats",
                       "SELECT * FROM garage.live_web_messages",
                       "INSERT INTO garage.live_web_chats (chat_key) VALUES ('" + "c" * 32 + "')",
                       "UPDATE garage.live_alerts SET chat_key = '1'",
                       "UPDATE garage.live_alerts SET kind = 'needs_staff'",
                       "UPDATE garage.live_alerts SET created_at = now()",
                       # Closing an alarm is the dashboard login's job, not the worker's.
                       "UPDATE garage.live_alerts SET status = 'done', handled_at = now()",
                       "UPDATE garage.live_alerts SET handled_by = 'worker'"),
            "site": ("UPDATE garage.live_alerts SET chat_key = '1'",
                     "UPDATE garage.live_alerts SET channel = 'web'",
                     "UPDATE garage.live_alerts SET kind = 'needs_staff'",
                     "UPDATE garage.live_alerts SET created_at = now()",
                     "UPDATE garage.live_web_messages SET body = 'forged'",
                     "UPDATE garage.live_web_chats SET chat_key = '" + "d" * 32 + "'",
                     "UPDATE garage.live_web_chats SET started_at = now()"),
            "hook": ("SELECT * FROM garage.live_alerts", "SELECT * FROM garage.live_web_chats",
                     "SELECT * FROM garage.live_web_messages",
                     "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
                     "VALUES ('messenger', '1', 'needs_staff', 'x')"),
            "outsider": ("SELECT * FROM garage.live_alerts",
                         "SELECT * FROM garage.live_web_messages"),
        }
        for role, statements in denied.items():
            for statement in statements:
                self.assert_denied(role, statement)
        # What each login may do, with the columns a repeat changes.
        with self.acting_as("worker") as connection:
            connection.execute("UPDATE garage.live_alerts SET occurrences = occurrences + 1, "
                               "phone = '599123456', updated_at = now(), version = version + 1")
        with self.acting_as("site") as connection:
            connection.execute("UPDATE garage.live_alerts SET status = 'open', handled_at = NULL")
            connection.execute("UPDATE garage.live_web_chats SET bot_enabled = false")
            connection.execute("DELETE FROM garage.live_web_messages")

    def test_the_tables_refuse_malformed_rows(self):
        bad = (
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
            "VALUES ('messenger', 'abc', 'needs_staff', 'x')",  # key shape per channel
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
            "VALUES ('web', '123', 'needs_staff', 'x')",
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
            "VALUES ('sms', '1', 'needs_staff', 'x')",
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
            "VALUES ('messenger', '1', 'party', 'x')",
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary) "
            "VALUES ('messenger', '1', 'needs_staff', '')",
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary, phone) "
            "VALUES ('messenger', '1', 'needs_staff', 'x', '12')",
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary, status) "
            "VALUES ('messenger', '1', 'needs_staff', 'x', 'done')",  # done needs handled_at
            "INSERT INTO garage.live_alerts (channel, chat_key, kind, summary, items) "
            "VALUES ('messenger', '1', 'needs_staff', 'x', '{}')",
            "INSERT INTO garage.live_web_chats (chat_key) VALUES ('short')",
        )
        for statement in bad:
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.admin_sql(statement)

    def test_before_0015_nothing_that_existed_fails(self):
        url = self.create_database("live_before_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0014_meta_forget")
        worker_db = RoleDatabase(url, self.roles["worker"])
        worker, site = MetaStore(worker_db), MetaStore(RoleDatabase(url, self.roles["site"]))
        worker.add_message(PSID, "m_1", "customer", at(1), page_id=PAGE)
        live = LiveStore(RoleDatabase(url, self.roles["site"]))
        self.assertFalse(live.ready())
        self.assertFalse(live.usable())
        self.assertEqual(site.delete_customer(PSID, STAFF)["threads"], 1)
        self.assertEqual(worker.purge()["threads"], 0)

    def test_a_deletion_that_cannot_reach_the_alarms_fails_instead_of_claiming_success(self):
        url = self.create_database("live_noprivilege_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
            connection.execute("REVOKE DELETE ON garage.live_alerts FROM garage_site")
        site = MetaStore(RoleDatabase(url, self.roles["site"]))
        MetaStore(RoleDatabase(url, self.roles["worker"])).add_message(
            PSID, "m_1", "customer", at(1), page_id=PAGE)
        with self.assertRaises(AlertsNotDeletable):
            site.delete_customer(PSID, STAFF)
        with psycopg.connect(url) as connection:  # nothing was deleted: it rolled back whole
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM garage.messenger_threads").fetchone()[0], 1)

    def test_ready_is_true_once_the_migration_is_applied_and_remembered(self):
        self.assertTrue(self.site.ready())
        self.assertTrue(self.site.usable())
        self.admin_sql("ALTER TABLE garage.live_web_messages RENAME TO live_web_messages_gone")
        try:
            self.assertTrue(self.site.ready())  # a yes is kept for a while
            fresh = LiveStore(RoleDatabase(self.url, self.roles["site"]))
            self.assertFalse(fresh.ready())
        finally:
            self.admin_sql("ALTER TABLE garage.live_web_messages_gone RENAME TO live_web_messages")

    # -- raising alarms --------------------------------------------------------------------------

    def alert_rows(self) -> list[dict]:
        return self.site.list_alerts("all")

    def test_an_alarm_is_created_once_and_a_repeat_merges_into_it(self):
        first = self.worker.raise_alert("messenger", PSID, draft(car="Honda Fit 2014"))
        self.assertTrue(first.created and first.changed)
        again = self.worker.raise_alert("messenger", PSID, draft(summary="  Front   BRAKE pads "))
        self.assertEqual((again.alert_id, again.created, again.changed),
                         (first.alert_id, False, False))
        rows = self.alert_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["occurrences"], rows[0]["version"], rows[0]["car_label"]),
                         (1, 1, "Honda Fit 2014"))

    def test_a_new_note_is_kept_beside_the_earlier_ones(self):
        self.worker.raise_alert("messenger", PSID, draft(summary="brake pads"))
        merged = self.worker.raise_alert("messenger", PSID, draft(
            reason="partial", summary="oil filter", car="Toyota Prius"))
        self.assertTrue(merged.changed and not merged.created)
        row = self.alert_rows()[0]
        self.assertEqual((row["summary"], row["occurrences"], row["version"], row["reason"],
                          row["car_label"]), ("oil filter", 2, 2, "partial", "Toyota Prius"))
        self.assertEqual([item["note"] for item in row["items"]], ["brake pads"])

    def test_only_the_last_ten_earlier_notes_are_kept(self):
        for number in range(14):
            self.worker.raise_alert("messenger", PSID, draft(summary=f"part {number}"))
        row = self.alert_rows()[0]
        self.assertEqual(row["occurrences"], 14)
        self.assertEqual(len(row["items"]), 10)
        self.assertEqual(row["items"][-1]["note"], "part 12")
        self.assertEqual(row["summary"], "part 13")

    def test_kinds_and_chats_have_alarms_of_their_own(self):
        self.worker.raise_alert("messenger", PSID, draft())
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "wants to come"))
        self.worker.raise_alert("messenger", OTHER_PSID, draft())
        self.site.raise_alert("web", WEB, draft())
        self.assertEqual(len(self.alert_rows()), 4)

    def test_a_new_alarm_follows_a_done_one(self):
        first = self.worker.raise_alert("messenger", PSID, draft())
        self.site.set_status(first.alert_id, "done", STAFF)
        second = self.worker.raise_alert("messenger", PSID, draft())
        self.assertTrue(second.created)
        self.assertNotEqual(second.alert_id, first.alert_id)

    def test_a_phone_added_later_updates_the_alarm_once(self):
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "coming"))
        added = self.worker.raise_alert("messenger", PSID,
                                        draft("visit_request", "", "coming", phone="599123456"))
        self.assertTrue(added.changed and added.phone_added)
        same = self.worker.raise_alert("messenger", PSID,
                                       draft("visit_request", "", "coming", phone="599123456"))
        self.assertFalse(same.changed)
        row = self.alert_rows()[0]
        self.assertEqual((row["phone"], row["occurrences"], row["version"]), ("599123456", 1, 2))
        self.assertIsNotNone(row["phone_at"])

    def test_a_long_summary_is_cut_and_an_empty_one_filled(self):
        self.worker.raise_alert("messenger", PSID, draft(summary="x" * 900))
        self.worker.raise_alert("messenger", OTHER_PSID, draft(summary="   "))
        summaries = sorted(row["summary"] for row in self.alert_rows())
        self.assertEqual(summaries, ["-", "x" * 300])

    # -- phone numbers ---------------------------------------------------------------------------

    def test_a_phone_goes_onto_every_open_alarm_of_the_chat_and_no_other(self):
        self.worker.raise_alert("messenger", PSID, draft())
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "coming"))
        self.worker.raise_alert("messenger", OTHER_PSID, draft())
        raised = self.worker.attach_phone("messenger", PSID, "599123456", "my number")
        self.assertEqual(len(raised), 2)
        phones = {(row["key"], row["kind"]): row["phone"] for row in self.alert_rows()}
        self.assertEqual(phones[("m-" + PSID, "part_not_found")], "599123456")
        self.assertEqual(phones[("m-" + PSID, "visit_request")], "599123456")
        self.assertIsNone(phones[("m-" + OTHER_PSID, "part_not_found")])
        self.assertEqual(self.worker.attach_phone("messenger", PSID, "599123456", "again"), [])

    def test_a_corrected_number_replaces_the_old_one(self):
        self.worker.raise_alert("messenger", PSID, draft(phone="599123456"))
        raised = self.worker.attach_phone("messenger", PSID, "599123499", "sorry, 599123499")
        self.assertEqual(len(raised), 1)
        self.assertEqual(self.alert_rows()[0]["phone"], "599123499")

    def test_a_new_number_with_nothing_open_raises_an_alarm_of_its_own(self):
        first = self.worker.raise_alert("messenger", PSID, draft())
        self.site.set_status(first.alert_id, "done", STAFF)
        raised = self.worker.attach_phone("messenger", PSID, "599123456", "call me 599123456",
                                          "Honda Fit")
        self.assertEqual(len(raised), 1)
        self.assertTrue(raised[0].created)
        row = self.site.list_alerts("open")[0]
        self.assertEqual((row["kind"], row["reason"], row["phone"], row["summary"],
                          row["car_label"]),
                         ("needs_staff", "phone", "599123456", "call me 599123456", "Honda Fit"))

    def test_a_number_staff_already_saw_is_not_raised_again(self):
        first = self.worker.raise_alert("messenger", PSID, draft(phone="599123456"))
        self.site.set_status(first.alert_id, "done", STAFF)
        self.assertEqual(self.worker.attach_phone("messenger", PSID, "599123456", "x"), [])
        self.assertEqual(self.site.list_alerts("open"), [])

    def test_record_turn_raises_the_drafts_and_attaches_the_phone_once(self):
        turn = TurnAlerts((draft(phone="599123456"), draft("visit_request", "", "coming",
                                                           phone="599123456")),
                          "599123456", "x", "")
        raised = self.worker.record_turn("messenger", PSID, turn)
        self.assertEqual(len(raised), 2)  # the phone rode on the drafts: no third alarm
        self.assertEqual(len(self.alert_rows()), 2)
        only_phone = self.worker.record_turn("messenger", OTHER_PSID,
                                             TurnAlerts((), "599000111", "call me", ""))
        self.assertEqual(len(only_phone), 1)
        self.assertEqual(self.worker.record_turn("messenger", OTHER_PSID, TurnAlerts()), [])

    # -- reading and closing alarms --------------------------------------------------------------

    def test_marking_done_stores_who_and_when_and_is_repeatable(self):
        raised = self.worker.raise_alert("messenger", PSID, draft())
        done = self.site.set_status(raised.alert_id, "done", STAFF, version=1)
        self.assertEqual((done["status"], done["handled_by"], done["version"]),
                         ("done", "counter", 2))
        self.assertIsNotNone(done["handled_at"])
        again = self.site.set_status(raised.alert_id, "done", STAFF)
        self.assertEqual(again["version"], 2)  # nothing changed
        self.assertEqual(self.site.list_alerts("open"), [])
        self.assertEqual(len(self.site.recent_done()), 1)

    def test_a_stale_version_is_a_conflict_and_an_unknown_alarm_is_none(self):
        raised = self.worker.raise_alert("messenger", PSID, draft())
        with self.assertRaises(MetaConflict):
            self.site.set_status(raised.alert_id, "done", STAFF, version=9)
        self.assertIsNone(self.site.set_status(999, "done", STAFF))
        with self.assertRaises(ValueError):
            self.site.set_status(raised.alert_id, "closed", STAFF)

    def test_reopening_clears_who_and_when_and_cannot_duplicate_an_open_alarm(self):
        first = self.worker.raise_alert("messenger", PSID, draft())
        self.site.set_status(first.alert_id, "done", STAFF)
        reopened = self.site.set_status(first.alert_id, "open", STAFF)
        self.assertEqual((reopened["status"], reopened["handled_by"], reopened["handled_at"]),
                         ("open", "", None))
        self.site.set_status(first.alert_id, "done", STAFF)
        second = self.worker.raise_alert("messenger", PSID, draft())
        self.assertTrue(second.created)
        with self.assertRaises(AlreadyOpen):
            self.site.set_status(first.alert_id, "open", STAFF)

    def test_alarms_list_with_the_customer_and_newest_first(self):
        self.say(PSID, "m_1", "customer", 1)
        self.admin_sql("UPDATE garage.messenger_threads SET customer_name = 'Giorgi' "
                       "WHERE psid = %s", (PSID,))
        self.worker.raise_alert("messenger", PSID, draft(summary="older"))
        self.site.raise_alert("web", WEB, draft(summary="newer"))
        rows = self.site.list_alerts("open")
        self.assertEqual([row["summary"] for row in rows], ["newer", "older"])
        self.assertEqual(rows[1]["name"], "Giorgi")
        self.assertEqual(rows[0]["key"], "w-" + WEB)
        self.assertEqual(rows[0]["tail"], "aaaa")
        # The chat's own version rides on the alarm, for the bot switch on its card; 0 when the
        # chat is gone (an alarm outlives a deleted chat only for a moment).
        self.assertEqual(rows[1]["chat_version"], 1)
        self.worker.raise_alert("messenger", OTHER_PSID, draft(summary="chat gone"))
        orphan = [row for row in self.site.list_alerts("open") if row["summary"] == "chat gone"]
        self.assertEqual(orphan[0]["chat_version"], 0)
        with self.assertRaises(ValueError):
            self.site.list_alerts("everything")

    # -- the pulse -------------------------------------------------------------------------------

    def test_the_pulse_counts_what_needs_a_human(self):
        self.say(PSID, "m_1", "customer", 1)
        self.say(PSID, "m_2", "bot", 2)
        self.worker.raise_alert("messenger", PSID, draft())
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "x"))
        self.site.raise_alert("web", WEB, draft("needs_staff", "failed", "x"))
        pulse = self.site.pulse()
        self.assertEqual(pulse["open"], {"part_not_found": 1, "visit_request": 1,
                                         "service_booking": 0, "needs_staff": 1, "total": 3})
        self.assertEqual((pulse["waiting"], pulse["attention"]), (0, 0))
        self.assertEqual(pulse["messenger_bot"], "off")  # the global switch ships off
        self.assertIsNotNone(pulse["stamp"])
        self.assertEqual(pulse["cursor"], {"m": 2, "w": 0})

    def test_a_chat_waits_only_when_its_bot_is_off_and_nobody_answered(self):
        self.say(PSID, "m_1", "customer", 1)
        self.say(PSID, "m_2", "bot", 2)
        self.say(OTHER_PSID, "m_3", "customer", 3)
        self.site_chats.set_thread_switch(PSID, False, STAFF)
        self.assertEqual(self.site.pulse()["waiting"], 0)  # the bot answered before it was off
        self.say(PSID, "m_4", "customer", 4)
        self.assertEqual(self.fresh().pulse()["waiting"], 1)  # the other chat's bot is on
        self.say(PSID, "m_5", "shop", 5)
        self.assertEqual(self.fresh().pulse()["waiting"], 0)  # staff answered
        self.say(PSID, "m_6", "customer", 6)
        self.admin_sql("UPDATE garage.messenger_threads SET is_admin = true WHERE psid = %s",
                       (PSID,))
        self.assertEqual(self.fresh().pulse()["waiting"], 0)  # the owner's own chat never waits

    def fresh(self) -> LiveStore:
        return LiveStore(RoleDatabase(self.url, self.roles["site"]))

    def test_stuck_chats_are_counted_and_the_flag_can_be_cleared(self):
        self.say(PSID, "m_1", "customer", 1)
        self.worker_chats.flag_attention(PSID, "engine_failed")
        self.assertEqual(self.site.pulse()["attention"], 1)
        self.assertTrue(self.site.clear_attention("m-" + PSID, STAFF))
        self.assertEqual(self.site.pulse()["attention"], 0)
        self.assertEqual(self.audits()[-1][:3], ("counter", "messenger.attention_cleared", "…1234"))
        self.assertFalse(self.site.clear_attention("w-" + WEB, STAFF))
        self.assertFalse(self.site.clear_attention("nonsense", STAFF))

    def test_the_pulse_is_remembered_briefly_and_a_change_made_here_refreshes_it(self):
        first = self.site.pulse()
        self.assertEqual(first["open"]["total"], 0)
        self.worker.raise_alert("messenger", PSID, draft())  # another process: not seen yet
        self.assertEqual(self.site.pulse()["open"]["total"], 0)
        self.site.raise_alert("web", WEB, draft())  # a change made through this store
        self.assertEqual(self.site.pulse()["open"]["total"], 2)

    # -- the feed and the chat list --------------------------------------------------------------

    def test_the_feed_returns_new_messages_and_the_chats_they_belong_to(self):
        self.say(PSID, "m_1", "customer", 1, "front pads for a fit")
        self.say(PSID, "m_2", "bot", 2, "which year")
        self.site.web_customer(WEB, "oil filter")
        self.site.web_bot(WEB, "found it", offered=[{"name": "Filter", "price": 12}],
                          outcome="offered", car_label="Toyota Prius")
        feed = self.site.feed(0, 0)
        self.assertEqual([item["id"] for item in feed["messages"]], ["m1", "m2", "w1", "w2"])
        self.assertEqual(feed["messages"][2]["offered"], [])
        self.assertEqual(feed["messages"][3]["offered"], [{"name": "Filter", "price": 12}])
        self.assertEqual(sorted(feed["chats"]), ["m-" + PSID, "w-" + WEB])
        self.assertEqual(feed["cursor"], {"m": 2, "w": 2})
        self.assertEqual(feed["chats"]["w-" + WEB]["car_label"], "Toyota Prius")

    def test_a_channel_without_a_cursor_is_skipped(self):
        self.say(PSID, "m_1", "customer", 1)
        self.site.web_customer(WEB, "hello")
        only_web = self.site.feed(None, 0)
        self.assertEqual([item["id"] for item in only_web["messages"]], ["w1"])
        self.assertIsNone(only_web["cursor"]["m"])

    def test_the_feed_reads_the_last_ids_again_in_case_one_committed_late(self):
        for number in range(1, 41):
            self.say(PSID, f"m_{number}", "customer", number)
        again = self.site.feed(40, None)
        self.assertEqual(len(again["messages"]), 30)
        self.assertEqual(again["messages"][0]["id"], "m11")
        self.assertEqual(again["cursor"]["m"], 40)
        nothing_new = self.site.feed(80, None)
        self.assertEqual(nothing_new["messages"], [])
        self.assertEqual(nothing_new["cursor"]["m"], 80)

    def test_the_feed_never_shows_the_owners_own_chat(self):
        self.say(PSID, "m_1", "customer", 1, "#bot off")
        self.admin_sql("UPDATE garage.messenger_threads SET is_admin = true")
        self.assertEqual(self.site.feed(0, None)["messages"], [])
        self.assertEqual(self.site.chats(), [])

    def test_a_bots_reply_is_shown_at_the_time_it_was_stored(self):
        self.say(PSID, "m_1", "customer", 1)
        self.say(PSID, "m_2", "bot", 1.001)
        stored = datetime(2030, 1, 1, tzinfo=UTC)
        self.admin_sql("UPDATE garage.messenger_messages SET stored_at = %s WHERE mid = 'm_2'",
                       (stored,))
        reply = [item for item in self.site.feed(0, None)["messages"] if item["id"] == "m2"][0]
        self.assertEqual(reply["at"], stored)

    def test_conversations_of_both_channels_most_recent_first_with_filters(self):
        self.say(PSID, "m_1", "customer", 1, "x" * 300)
        self.site.web_customer(WEB, "website question")
        # The website rows are stamped now; put them between the two Messenger chats.
        self.admin_sql("UPDATE garage.live_web_messages SET stored_at = %s", (at(5),))
        self.admin_sql("UPDATE garage.live_web_chats SET last_message_at = %s", (at(5),))
        self.say(OTHER_PSID, "m_2", "customer", 9, "newest")
        self.site_chats.set_thread_switch(OTHER_PSID, False, STAFF)
        self.worker.raise_alert("messenger", PSID, draft())
        self.worker_chats.flag_attention(OTHER_PSID, "send_failed")
        keys = [chat["key"] for chat in self.site.chats()]
        self.assertEqual(keys[0], "m-" + OTHER_PSID)
        self.assertEqual(set(keys), {"m-" + PSID, "m-" + OTHER_PSID, "w-" + WEB})
        first = {chat["key"]: chat for chat in self.site.chats()}["m-" + PSID]
        self.assertEqual(len(first["last_body"]), 120)
        self.assertEqual(first["alerts"], ["part_not_found"])
        self.assertEqual({chat["key"] for chat in self.site.chats("alerts")},
                         {"m-" + PSID, "m-" + OTHER_PSID})  # an open alarm, or stuck
        self.assertEqual([chat["key"] for chat in self.site.chats("off")], ["m-" + OTHER_PSID])
        self.assertEqual([chat["key"] for chat in self.site.chats("waiting")], ["m-" + OTHER_PSID])
        self.assertEqual([chat["key"] for chat in self.site.chats("stuck")], ["m-" + OTHER_PSID])
        self.assertEqual(len(self.site.chats(limit=2)), 2)
        with self.assertRaises(ValueError):
            self.site.chats("mystery")

    def test_one_messenger_conversation_pages_backwards(self):
        for number in range(1, 8):
            self.say(PSID, f"m_{number}", "customer" if number % 2 else "bot", number,
                     f"message {number}")
        newest = self.site.chat("m-" + PSID, limit=3)
        self.assertEqual([item["body"] for item in newest["messages"]],
                         ["message 5", "message 6", "message 7"])
        self.assertTrue(newest["has_older"])
        older = self.site.chat("m-" + PSID, limit=3, before=newest["messages"][0]["id"])
        self.assertEqual([item["body"] for item in older["messages"]],
                         ["message 2", "message 3", "message 4"])
        oldest = self.site.chat("m-" + PSID, limit=3, before=older["messages"][0]["id"])
        self.assertEqual([item["body"] for item in oldest["messages"]], ["message 1"])
        self.assertFalse(oldest["has_older"])
        self.assertEqual(newest["chat"]["key"], "m-" + PSID)

    def test_one_website_conversation_with_what_the_visitor_saw(self):
        self.site.web_customer(WEB, "do you have pads")
        self.site.web_bot(WEB, "yes", offered=[{"name": "Pads"}], outcome="offered")
        self.site.web_customer(WEB, "thanks")
        shown = self.site.chat("w-" + WEB, limit=2)
        self.assertEqual([item["body"] for item in shown["messages"]], ["yes", "thanks"])
        self.assertTrue(shown["has_older"])
        self.assertEqual(shown["messages"][0]["offered"], [{"name": "Pads"}])
        older = self.site.chat("w-" + WEB, limit=2, before=shown["messages"][0]["id"])
        self.assertEqual([item["body"] for item in older["messages"]], ["do you have pads"])
        # A cursor from the other channel is ignored, not trusted.
        self.assertEqual(len(self.site.chat("w-" + WEB, limit=5, before="m1")["messages"]), 3)

    def test_a_chat_shows_its_open_and_recently_done_alarms(self):
        self.say(PSID, "m_1", "customer", 1)
        done = self.worker.raise_alert("messenger", PSID, draft())
        self.site.set_status(done.alert_id, "done", STAFF)
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "coming"))
        self.admin_sql("UPDATE garage.live_alerts SET handled_at = now() - interval '2 days' "
                       "WHERE status = 'done'")
        self.assertEqual([item["kind"] for item in self.site.chat("m-" + PSID)["alerts"]],
                         ["visit_request"])  # a day-old done alarm is no longer shown here

    def test_no_answer_carries_costs_tokens_or_the_admin_flag(self):
        """An employee reads these answers: nothing the Facebook section keeps for admins."""
        self.say(PSID, "m_1", "customer", 1)
        self.say(PSID, "m_2", "bot", 2)
        self.worker_chats.flag_attention(PSID, "engine_failed")
        self.worker.raise_alert("messenger", PSID, draft(phone="599123456"))
        self.site.web_customer(WEB, "hello")
        self.site.web_bot(WEB, "hi", offered=[{"name": "Pads", "price": 12}])
        seen: set[str] = set()

        def walk(value):
            if isinstance(value, dict):
                seen.update(value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for answer in (self.site.pulse(), self.site.feed(0, 0), self.site.chats(),
                       self.site.chat("m-" + PSID), self.site.chat("w-" + WEB),
                       self.site.list_alerts("all"), self.site.recent_done()):
            walk(answer)
        forbidden = {"cost_usd", "usage", "is_admin", "engine_state", "token", "token_enc",
                     "page_id", "mid", "app_id", "referral_ad_id", "conversation_id", "psid"}
        self.assertEqual(seen & forbidden, set())

    def test_unknown_chats_and_keys_are_none(self):
        self.assertIsNone(self.site.chat("m-" + PSID))
        self.assertIsNone(self.site.chat("w-" + WEB))
        self.assertIsNone(self.site.chat("garbage"))

    # -- the bot's switch for one customer -------------------------------------------------------

    def test_the_bot_switch_of_a_messenger_chat_is_audited_and_versioned(self):
        self.say(PSID, "m_1", "customer", 1)
        off = self.site.set_bot("m-" + PSID, False, STAFF, version=1, client_ip="10.0.0.5")
        self.assertEqual(off, {"key": "m-" + PSID, "bot_enabled": False, "version": 2})
        self.assertEqual(self.audits()[-1][:3], ("counter", "messenger.chat_off", "…1234"))
        with self.assertRaises(MetaConflict):
            self.site.set_bot("m-" + PSID, True, STAFF, version=1)
        on = self.site.set_bot("m-" + PSID, True, STAFF)
        self.assertEqual((on["bot_enabled"], on["version"]), (True, 3))
        self.assertIsNone(self.site.set_bot("m-" + OTHER_PSID, False, STAFF))
        self.assertIsNone(self.site.set_bot("nonsense", False, STAFF))

    def test_the_bot_switch_of_a_website_visitor(self):
        self.assertIsNone(self.site.web_bot_enabled(WEB))
        self.site.web_customer(WEB, "hello")
        self.assertTrue(self.site.web_bot_enabled(WEB))
        off = self.site.set_bot("w-" + WEB, False, STAFF, version=1)
        self.assertEqual((off["bot_enabled"], off["version"]), (False, 2))
        self.assertFalse(self.site.web_bot_enabled(WEB))
        row = self.admin_sql("SELECT muted_by, muted_at IS NOT NULL FROM garage.live_web_chats")[0]
        self.assertEqual(tuple(row), ("counter", True))
        self.assertEqual(self.audits()[-1][:3], ("counter", "live.web_chat_off", "w-" + "a" * 8))
        with self.assertRaises(MetaConflict):
            self.site.set_bot("w-" + WEB, True, STAFF, version=1)
        self.site.set_bot("w-" + WEB, True, STAFF)
        row = self.admin_sql("SELECT muted_by, muted_at IS NOT NULL FROM garage.live_web_chats")[0]
        self.assertEqual(tuple(row), ("", False))
        self.assertIsNone(self.site.set_bot("w-" + OTHER_WEB, False, STAFF))

    # -- the website chat ------------------------------------------------------------------------

    def test_the_website_chat_is_stored_as_it_happens(self):
        self.site.web_customer(WEB, "hello", "ka")
        self.site.web_bot(WEB, "hi there", outcome="chat", car_label="Honda Fit 2014",
                          language="en")
        row = self.admin_sql("SELECT language, car_label, last_customer_at IS NOT NULL, "
                             "last_bot_at IS NOT NULL FROM garage.live_web_chats")[0]
        self.assertEqual(tuple(row), ("en", "Honda Fit 2014", True, True))
        self.assertEqual(self.admin_sql("SELECT count(*) FROM garage.live_web_messages")[0][0], 2)

    def test_a_website_message_is_cleaned_and_bounded(self):
        self.site.web_customer(WEB, "a\x00b" + "x" * 5000, "xx")
        self.site.web_bot(WEB, "ok", offered=[{"n": number} for number in range(20)])
        body = self.admin_sql("SELECT body, (SELECT language FROM garage.live_web_chats) "
                              "FROM garage.live_web_messages WHERE sender = 'customer'")[0]
        self.assertEqual((len(body[0]), "\x00" in body[0], body[1]), (4000, False, ""))
        offered = self.admin_sql("SELECT jsonb_array_length(offered) FROM "
                                 "garage.live_web_messages WHERE sender = 'bot'")[0][0]
        self.assertEqual(offered, 8)

    def test_a_new_alarm_only_merges_when_creating_is_not_allowed(self):
        refused = self.site.raise_alert("web", WEB, draft(), allow_create=False)
        self.assertIsNone(refused)
        self.assertEqual(self.alert_rows(), [])
        self.site.raise_alert("web", WEB, draft(summary="pads"))
        merged = self.site.raise_alert("web", WEB, draft(summary="discs"), allow_create=False)
        self.assertEqual((merged.created, merged.changed), (False, True))
        self.assertEqual(self.alert_rows()[0]["occurrences"], 2)

    def test_a_new_alarm_starts_with_the_phone_the_chat_already_gave(self):
        first = self.site.raise_alert("web", WEB, draft("visit_request", "", "coming",
                                                          phone="599123456"))
        self.site.set_status(first.alert_id, "done", STAFF)
        later = self.site.raise_alert("web", WEB, draft(summary="discs"))  # no phone this turn
        self.assertTrue(later.created and later.phone_added)
        self.assertEqual(self.site.list_alerts("open")[0]["phone"], "599123456")

    def test_a_note_the_column_could_not_hold_never_loses_the_repeat(self):
        for number in range(14):
            self.site.raise_alert("web", WEB, draft(summary=f"{number}" + "ა" * 299))
        row = self.alert_rows()[0]
        self.assertEqual(row["occurrences"], 14)
        self.assertLessEqual(len(row["items"]), 10)
        self.assertTrue(all(len(item["note"]) <= 160 for item in row["items"]))
        self.assertEqual(self.admin_sql(
            "SELECT octet_length(items::text) <= 8192 FROM garage.live_alerts")[0][0], True)

    def test_open_website_alarms_are_counted_in_two_groups(self):
        self.site.raise_alert("web", WEB, draft())  # part not found: noisy
        self.site.raise_alert("web", WEB, draft("needs_staff", "failed", "x"))  # noisy
        self.site.raise_alert("web", OTHER_WEB, draft("visit_request", "", "coming"))
        self.site.raise_alert("web", OTHER_WEB, draft("needs_staff", "handoff", "angry"))
        self.worker.raise_alert("messenger", PSID, draft())  # another channel: not counted
        self.assertEqual(self.site.open_web_alert_counts(), {"noisy": 2, "valuable": 2})

    def test_a_visitor_whose_bot_answered_again_has_their_failed_alarm_closed(self):
        self.site.raise_alert("web", WEB, draft("needs_staff", "failed", "x"))
        self.site.raise_alert("web", WEB, draft("visit_request", "", "coming"))
        self.site.raise_alert("web", OTHER_WEB, draft("needs_staff", "failed", "y"))
        self.assertEqual(self.site.close_failed("web", WEB), 1)
        rows = {(row["key"], row["kind"]): row for row in self.alert_rows()}
        closed = rows[("w-" + WEB, "needs_staff")]
        self.assertEqual((closed["status"], closed["handled_by"]), ("done", "system"))
        self.assertEqual(rows[("w-" + WEB, "visit_request")]["status"], "open")
        self.assertEqual(rows[("w-" + OTHER_WEB, "needs_staff")]["status"], "open")
        self.assertEqual(self.site.close_failed("web", WEB), 0)

    def test_the_muted_visitors_are_listed_for_the_recorder(self):
        self.site.web_customer(WEB, "a")
        self.site.web_customer(OTHER_WEB, "b")
        self.site.set_bot("w-" + WEB, False, STAFF)
        self.assertEqual(self.site.web_muted_keys(), {WEB})

    def test_marking_an_alarm_done_or_open_is_audited_without_text(self):
        raised = self.worker.raise_alert("messenger", PSID, draft(phone="599123456"))
        self.site.set_status(raised.alert_id, "done", STAFF, client_ip="10.0.0.5")
        self.site.set_status(raised.alert_id, "open", STAFF)
        self.site.set_status(raised.alert_id, "open", STAFF)  # nothing changed: no new row
        audits = self.audits()
        self.assertEqual([row[:3] for row in audits],
                         [("counter", "live.alert_done", f"alert-{raised.alert_id}"),
                          ("counter", "live.alert_open", f"alert-{raised.alert_id}")])
        self.assertNotIn("599123456", str(audits))
        self.assertEqual(audits[0][3]["kind"], "part_not_found")

    def test_the_owners_own_chat_raises_no_alarm_nobody_could_open(self):
        self.say(PSID, "m_1", "customer", 1)
        self.admin_sql("UPDATE garage.messenger_threads SET is_admin = true")
        turn = TurnAlerts((draft(),), "599123456", "x", "")
        self.assertEqual(self.worker.record_turn("messenger", PSID, turn), [])
        self.assertEqual(self.alert_rows(), [])

    def test_a_chat_lists_its_open_alarms_before_the_handled_ones(self):
        self.say(PSID, "m_1", "customer", 1)
        old = self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "coming"))
        self.site.set_status(old.alert_id, "done", STAFF)
        self.worker.raise_alert("messenger", PSID, draft())
        shown = self.site.chat("m-" + PSID)["alerts"]
        self.assertEqual([item["status"] for item in shown], ["open", "done"])

    def test_the_owners_own_chat_cannot_be_switched_or_cleared_from_this_page(self):
        self.say(PSID, "m_1", "customer", 1)
        self.admin_sql("UPDATE garage.messenger_threads SET is_admin = true")
        self.assertIsNone(self.site.set_bot("m-" + PSID, False, STAFF))
        self.assertFalse(self.site.clear_attention("m-" + PSID, STAFF))
        self.assertTrue(self.admin_sql("SELECT bot_enabled FROM garage.messenger_threads")[0][0])

    def test_a_page_that_fell_far_behind_reads_only_the_recent_window(self):
        for number in range(1, 451):
            self.say(PSID, f"m_{number}", "customer", number / 10)
        first = self.site.feed(0, None)
        self.assertEqual(first["messages"][0]["id"], "m51")  # not m1: 450 - 400 = 50 and up
        self.assertEqual(len(first["messages"]), 200)

    def test_open_web_alarms_are_counted(self):
        self.site.raise_alert("web", WEB, draft())
        done = self.site.raise_alert("web", OTHER_WEB, draft())
        self.site.set_status(done.alert_id, "done", STAFF)
        self.site.raise_alert("messenger", PSID, draft())
        self.assertEqual(self.site.open_web_alerts(), 1)

    def test_pruning_removes_old_website_alarms_and_keeps_every_chat(self):
        self.site.web_customer(WEB, "old")
        self.site.web_customer(OTHER_WEB, "new")
        self.site.raise_alert("web", WEB, draft())
        self.site.raise_alert("web", OTHER_WEB, draft())
        self.site.raise_alert("messenger", PSID, draft())
        self.admin_sql("UPDATE garage.live_web_messages SET stored_at = now() - interval '40 days' "
                       "WHERE chat_key = %s", (WEB,))
        self.admin_sql("UPDATE garage.live_web_chats SET last_message_at = now() - interval "
                       "'40 days' WHERE chat_key = %s", (WEB,))
        self.admin_sql("UPDATE garage.live_alerts SET created_at = now() - interval '40 days'")
        counts = self.site.web_prune(30)
        self.assertEqual(counts, {"alerts": 2})
        # The website chat is kept for good: the 40-day-old chat and its message stay.
        self.assertEqual(sorted(row[0] for row in self.admin_sql(
            "SELECT chat_key FROM garage.live_web_chats")), sorted([WEB, OTHER_WEB]))
        self.assertEqual(sorted(row[0] for row in self.admin_sql(
            "SELECT body FROM garage.live_web_messages")), ["new", "old"])
        self.assertEqual(self.admin_sql("SELECT channel FROM garage.live_alerts")[0][0],
                         "messenger")

    def test_the_recorder_writes_the_website_chat_and_honours_the_staff_switch(self):
        """The website process's side, on the site login: what is recorded, and the mute."""
        clock = [0.0]
        recorder = LiveRecorder(self.site, start=False, clock=lambda: clock[0],
                                shop_phone=lambda: "596 33 33 74")
        recorder.seen("conversation-1", "10.0.0.1")
        recorder.customer("conversation-1", "do you have front pads", "en")
        recorder.turn("conversation-1", "do you have front pads",
                      {"engine_status": "OK", "outcome": "nothing_found",
                       "answer": "I could not find them.", "cards": [],
                       "car_label": "Honda Fit 2014"}, "en")
        self.assertEqual(recorder.drain(), 2)
        key = "w-" + web_key("conversation-1")
        shown = self.site.chat(key)
        self.assertEqual([item["body"] for item in shown["messages"]],
                         ["do you have front pads", "I could not find them."])
        self.assertEqual([(item["kind"], item["reason"], item["summary"], item["car_label"])
                          for item in shown["alerts"]],
                         [("part_not_found", "nothing_found", "do you have front pads",
                           "Honda Fit 2014")])
        self.assertNotIn("conversation-1", str(self.admin_sql(
            "SELECT * FROM garage.live_web_chats")))
        self.assertIsNone(recorder.muted_reply("conversation-1", "en"))
        self.site.set_bot(key, False, STAFF)
        recorder.refresh()  # the writer thread does this every few seconds
        muted = recorder.muted_reply("conversation-1", "en")
        self.assertIn("switched off", muted)
        self.assertIn("596 33 33 74", muted)
        self.site.set_bot(key, True, STAFF)
        recorder.refresh()
        self.assertIsNone(recorder.muted_reply("conversation-1", "en"))
        recorder.canned("conversation-1", muted, "en")
        recorder.drain()
        self.assertEqual(self.site.chat(key)["messages"][-1]["body"], muted)

    # -- erasure and retention -------------------------------------------------------------------

    def test_deleting_a_customers_data_removes_their_alarms_and_no_one_elses(self):
        self.say(PSID, "m_1", "customer", 1)
        self.worker.raise_alert("messenger", PSID, draft(phone="599123456"))
        self.worker.raise_alert("messenger", OTHER_PSID, draft())
        self.site.raise_alert("web", WEB, draft())
        removed = self.site_chats.delete_customer(PSID, STAFF)
        self.assertEqual(removed["threads"], 1)
        keys = {(row["channel"], row["key"]) for row in self.site.list_alerts("all")}
        self.assertEqual(keys, {("messenger", "m-" + OTHER_PSID), ("web", "w-" + WEB)})

    def test_the_daily_purge_removes_alarms_past_retention_counted_from_when_they_began(self):
        first = self.worker.raise_alert("messenger", PSID, draft())
        self.worker.raise_alert("messenger", OTHER_PSID, draft())
        self.admin_sql("UPDATE garage.live_alerts SET created_at = now() - interval '400 days' "
                       "WHERE chat_key = %s", (PSID,))
        # Closing or merging into an old alarm today must not give its phone another year.
        self.site.set_status(first.alert_id, "done", STAFF)
        self.worker_chats.purge()
        self.assertEqual([row["key"] for row in self.site.list_alerts("all")],
                         ["m-" + OTHER_PSID])


if __name__ == "__main__":
    unittest.main()
