"""Why the bot found nothing, kept with each alarm (migration 0017), on a disposable PostgreSQL
database under the real capability roles.

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and roles this
file creates and drops. The worker's store runs as a member of garage_messenger and the dashboard's
as a member of garage_site, so a write outside migration 0017's grants fails here as it would in
production.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.live_alerts import AlertDraft
from automotive.live_store import LiveStore
from automotive.meta_store import MetaStore
from tests.test_meta_store_database import ADMIN_URL, ROLES_SQL, RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "migrations"
PAGE = "110000000000001"
PSID, OTHER_PSID = "7000000000001234", "7000000000005678"
WEB = "a" * 32
TABLES = ("live_alerts", "live_web_chats", "live_web_messages", "messenger_threads",
          "messenger_messages", "messenger_replies", "audit_log")
SEARCHED = {"v": 1, "outcome": "nothing_found", "searched": 1, "searches": [
    {"tool": "search_catalog", "part": ["სანთელი"], "car": ["camry"], "total": 31,
     "available": 12, "car_rows": 0}]}
AGAIN = {**SEARCHED, "searches": [{**SEARCHED["searches"][0], "car": ["camry 2020"]}]}
COLUMN = ("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'garage' "
          "AND table_name = 'live_alerts' AND column_name = 'detail'")


def draft(kind="part_not_found", reason="nothing_found", summary="სანთლები", detail=None):
    return AlertDraft(kind, reason, summary, "Toyota Camry 2020", "", detail or {})


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class AlertDetailDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("live_detail_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"))}

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
    def create_role(cls, key: str, capability: str) -> str:
        name = f"detail_{key}_{cls.suffix}"
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name)))
        cls.cluster_roles.append(name)
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
        self.worker = self.store("worker")
        self.site = self.store("site")
        self.chats = MetaStore(RoleDatabase(self.url, self.roles["worker"]))

    def store(self, role: str, url: str | None = None) -> LiveStore:
        return LiveStore(RoleDatabase(url or self.url, self.roles[role]))

    def stored(self, url: str | None = None) -> list[tuple]:
        with psycopg.connect(url or self.url) as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT alert_id, version, occurrences, updated_at, "
                "to_jsonb(a) -> 'detail' FROM garage.live_alerts a ORDER BY alert_id")]

    # -- kept, read back, refreshed --------------------------------------------------------------

    def test_both_logins_keep_the_reason_with_the_alarm_and_the_page_reads_it(self):
        self.assertTrue(self.worker.details_ready())
        made = self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        self.site.raise_alert("web", WEB, draft(detail=AGAIN))
        self.assertTrue(made.created)
        details = {alert["channel"]: alert["detail"] for alert in self.site.list_alerts()}
        self.assertEqual(details, {"messenger": SEARCHED, "web": AGAIN})
        # An alarm raised without one (a visit request, a phone number) reads as {}.
        self.worker.raise_alert("messenger", PSID, draft("visit_request", "", "coming"))
        visit = [alert for alert in self.site.list_alerts() if alert["kind"] == "visit_request"]
        self.assertEqual(visit[0]["detail"], {})

    def test_the_same_request_found_nothing_again_for_new_reasons_refreshes_them_quietly(self):
        first = self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        (before,) = self.stored()
        same = self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        self.assertEqual((same.created, same.changed, same.version), (False, False,
                                                                     first.version))
        self.assertEqual(self.stored(), [before])  # saying the same thing twice changes nothing
        refreshed = self.worker.raise_alert("messenger", PSID, draft(detail=AGAIN))
        self.assertEqual((refreshed.created, refreshed.changed, refreshed.phone_added),
                         (False, False, False))  # nothing sounds
        (after,) = self.stored()
        self.assertEqual((after[1], after[2], after[4]), (before[1] + 1, before[2], AGAIN))
        self.assertGreater(after[3], before[3])  # the page reads the alarm again

    def test_a_refresh_after_hours_never_makes_a_loud_alarm_quiet(self):
        with patch("automotive.shop_hours.is_open", lambda at=None: True):
            self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        (before,) = self.stored()
        # Now the shop is closed, and the alarm's own time is inside the hours: it is loud.
        with patch("automotive.shop_hours.is_open", lambda at=None: at is not None):
            again = self.worker.raise_alert("messenger", PSID, draft(detail=AGAIN))
            (alert,) = self.site.list_alerts()
        (after,) = self.stored()
        self.assertEqual((again.changed, after[1], after[3], after[4]),
                         (False, before[1] + 1, before[3], AGAIN))  # its time kept
        self.assertIs(alert["quiet"], False)  # so the siren still calls for it at opening
        # While the shop is open the same refresh moves the newest change: the page reads it.
        with patch("automotive.shop_hours.is_open", lambda at=None: True):
            self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        (day,) = self.stored()
        self.assertGreater(day[3], after[3])

    def test_a_new_request_takes_its_own_reasons_but_a_failure_never_replaces_a_handoffs(self):
        self.worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        self.worker.raise_alert("messenger", PSID, draft(summary="ზეთის ფილტრი", detail=AGAIN))
        (merged,) = self.site.list_alerts()
        self.assertEqual((merged["occurrences"], merged["detail"]), (2, AGAIN))
        handoff = {"v": 1, "outcome": "handoff", "note": "wants a person", "searched": 0}
        failed = {"v": 1, "searched": 0, "status": "TIMEOUT"}
        self.site.raise_alert("web", WEB, draft("needs_staff", "handoff", "help", handoff))
        self.site.raise_alert("web", WEB, draft("needs_staff", "failed", "help again", failed))
        (staff,) = [alert for alert in self.site.list_alerts() if alert["kind"] == "needs_staff"]
        self.assertEqual((staff["reason"], staff["detail"]), ("handoff", handoff))
        # A failure that follows a failure does carry its own cause.
        self.site.raise_alert("web", WEB, draft("needs_staff", "failed", "x", failed))
        self.site.raise_alert("web", "b" * 32, draft("needs_staff", "failed", "x", failed))
        self.site.raise_alert("web", "b" * 32, draft("needs_staff", "failed", "x2", {
            **failed, "status": "PROVIDER_BILLING"}))
        other = [alert for alert in self.site.list_alerts() if alert["key"] == "w-" + "b" * 32]
        self.assertEqual(other[0]["detail"]["status"], "PROVIDER_BILLING")

    def test_a_reason_the_database_refuses_never_takes_the_alarm_with_it(self):
        huge = {"v": 1, "searches": [{"tool": "search_catalog", "part": ["x" * 9000]}]}
        with self.assertLogs("automotive.live_store", "WARNING") as logged:
            made = self.worker.raise_alert("messenger", PSID, draft(detail=huge))
        self.assertTrue(made.created)
        (alert,) = self.site.list_alerts()
        self.assertEqual((alert["summary"], alert["detail"]), ("სანთლები", {}))
        self.assertIn("live_alert_detail_skipped CheckViolation", logged.output[0])
        # A repeat whose reason is refused changes nothing, and says so.
        with self.assertLogs("automotive.live_store", "WARNING"):
            again = self.worker.raise_alert("messenger", PSID, draft(detail=huge))
        self.assertEqual((again.changed, again.version), (False, made.version))

    # -- the stuck chat's cause -----------------------------------------------------------------

    def test_a_stuck_chat_carries_the_cause_its_newest_failed_reply_recorded(self):
        sent = datetime.now(UTC) - timedelta(minutes=5)
        for psid in (PSID, OTHER_PSID):
            self.chats.add_message(psid, f"m_{psid}", "customer", sent, page_id=PAGE, body="x")
        failed = self.chats.start_reply("messenger", PSID, "t1")
        self.chats.finish_reply(failed, "fallback_sent", engine_status="DATA_UNAVAILABLE",
                                error_text="ValueError@model")
        later = self.chats.start_reply("messenger", PSID, "t2")
        self.chats.finish_reply(later, "sent", engine_status="OK")  # answered fine since
        other = self.chats.start_reply("messenger", OTHER_PSID, "t3")
        self.chats.finish_reply(other, "fallback_sent", engine_status="TIMEOUT")
        self.chats.flag_attention(PSID, "engine_failed")
        # A send Meta refused: the chat's reason says so; Meta's own words are not passed on.
        refused = "7000000000009999"
        self.chats.add_message(refused, "m_refused", "customer", sent, page_id=PAGE, body="x")
        send = self.chats.start_reply("messenger", refused, "t4")
        self.chats.finish_reply(send, "failed", engine_status="OK",
                                error_text="(#551) This person isn't available right now.")
        self.chats.flag_attention(refused, "send_failed")
        chats = {chat["key"]: chat for chat in self.site.chats()}
        self.assertEqual((chats["m-" + refused]["failed_engine"],
                          chats["m-" + refused]["failed_error"]), ("OK", ""))
        stuck = chats["m-" + PSID]
        self.assertEqual((stuck["failed_engine"], stuck["failed_error"]),
                         ("DATA_UNAVAILABLE", "ValueError@model"))
        # A chat that is not stuck carries no cause, whatever its replies were.
        self.assertEqual((chats["m-" + OTHER_PSID]["failed_engine"],
                          chats["m-" + OTHER_PSID]["failed_error"]), ("", ""))
        self.assertEqual(self.site.chat("m-" + PSID)["chat"]["failed_engine"],
                         "DATA_UNAVAILABLE")

    # -- before 0017, and its down and up scripts --------------------------------------------------

    def test_before_0017_alarms_are_raised_exactly_as_before_without_a_reason(self):
        url = self.create_database("live_before_detail_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0016_live_waiting_dismiss")
        worker, site = self.store("worker", url), self.store("site", url)
        self.assertEqual((worker.ready(), worker.details_ready()), (True, False))
        made = worker.raise_alert("messenger", PSID, draft(detail=SEARCHED))
        self.assertTrue(made.created)
        again = worker.raise_alert("messenger", PSID, draft(detail=AGAIN))
        self.assertFalse(again.changed)
        (alert,) = site.list_alerts()
        self.assertEqual((alert["summary"], alert["detail"]), ("სანთლები", {}))

    def test_the_0017_down_script_drops_the_reasons_and_up_brings_the_column_back(self):
        url = self.create_database("live_detail_down_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0017_live_alert_detail")
        self.store("worker", url).raise_alert("messenger", PSID, draft(detail=SEARCHED))
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute((FOLDER / "0017_live_alert_detail.down.sql").read_text())
            self.assertEqual(connection.execute(COLUMN).fetchone()[0], 0)
        # The alarm itself stays, and new ones are raised without a reason.
        down = self.store("site", url)
        self.assertEqual([alert["detail"] for alert in down.list_alerts()], [{}])
        self.assertTrue(down.raise_alert("web", WEB, draft(detail=SEARCHED)).created)
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute((FOLDER / "0017_live_alert_detail.up.sql").read_text())
            self.assertEqual(connection.execute(COLUMN).fetchone()[0], 1)
        # Both logins may write the column again (0017's grants are back).
        self.store("worker", url).raise_alert("messenger", OTHER_PSID, draft(detail=AGAIN))
        self.store("site", url).raise_alert("web", "c" * 32, draft(detail=AGAIN))
        kept = [alert["detail"] for alert in self.store("site", url).list_alerts()]
        self.assertEqual(kept.count(AGAIN), 2)


if __name__ == "__main__":
    unittest.main()
