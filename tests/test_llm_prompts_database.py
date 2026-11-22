"""The owner's prompt edits on a disposable PostgreSQL database (migration 0019).

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and roles this
file creates and drops. Store calls run as NOLOGIN members of garage_site (the dashboard and the
website chat) and garage_messenger (the Messenger worker), so anything outside the migration's
grants and row-level security fails here as it would in production.
"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.prompt_library import PromptConflict, PromptStore, resolve
from tests.test_meta_store_database import ADMIN_URL, ROLES_SQL, RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

ROOT = Path(__file__).resolve().parents[1]
DOWN = (ROOT / "migrations/0019_llm_prompts.down.sql").read_text(encoding="utf-8")
UP = (ROOT / "migrations/0019_llm_prompts.up.sql").read_text(encoding="utf-8")
EDIT = "You are the Garage 1886 parts assistant. Edited by the owner."


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class PromptDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("prompts_" + cls.suffix)
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
        name = f"prompts_{key}_{cls.suffix}"
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
            connection.execute("TRUNCATE garage.llm_prompts, garage.llm_prompt_history "
                               "RESTART IDENTITY")

    def store(self, role: str, url: str | None = None) -> PromptStore:
        return PromptStore(RoleDatabase(url or self.url, self.roles[role]))

    def run_as(self, role: str, statement: str, parameters: tuple = ()):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles[role])))
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def test_the_dashboard_saves_resets_and_keeps_the_history(self):
        site = self.store("site")
        self.assertEqual(site.save("assistant.system", EDIT, 0, "boss"), 1)
        self.assertEqual(site.save("assistant.system", EDIT + " Twice.", 1, "boss"), 2)
        with self.assertRaises(PromptConflict):
            site.save("assistant.system", "stale", 1, "other")
        with self.assertRaises(PromptConflict):
            site.save("assistant.system", "stale", 0, "other")
        row = site.rows()["assistant.system"]
        self.assertEqual((row["body"], row["version"], row["updated_by"]),
                         (EDIT + " Twice.", 2, "boss"))
        with self.assertRaises(PromptConflict):
            site.reset("assistant.system", 1, "boss")
        self.assertTrue(site.reset("assistant.system", 2, "boss"))
        self.assertFalse(site.reset("assistant.system", 2, "boss"))
        self.assertEqual(site.rows(), {})
        self.assertEqual([(row["action"], row["version"], row["saved_by"])
                          for row in site.history("assistant.system")],
                         [("reset", 2, "boss"), ("saved", 2, "boss"), ("saved", 1, "boss")])

    def test_the_messenger_worker_reads_edits_and_cannot_change_them(self):
        self.store("site").save("assistant.system", EDIT, 0, "boss")
        worker = self.store("worker")
        self.assertEqual(worker.text("assistant.system"), EDIT)
        self.assertTrue(worker.available)
        for statement in (
                "INSERT INTO garage.llm_prompts (prompt_key, body) VALUES ('assistant.nudge', 'x')",
                "UPDATE garage.llm_prompts SET body = 'x'",
                "DELETE FROM garage.llm_prompts",
                "SELECT version FROM garage.llm_prompts",
                "SELECT * FROM garage.llm_prompt_history"):
            with self.subTest(statement), self.assertRaises(errors.InsufficientPrivilege):
                self.run_as("worker", statement)

    def test_another_login_sees_nothing_and_the_bot_keeps_the_packaged_text(self):
        self.store("site").save("assistant.system", EDIT, 0, "boss")
        outsider = self.store("outsider")
        self.assertIsNone(outsider.text("assistant.system"))
        self.assertEqual(resolve(outsider, "assistant.system", "packaged"), "packaged")
        self.assertFalse(outsider.available)

    def test_the_table_refuses_blank_texts_and_odd_keys(self):
        for key, body in (("assistant.system", "   "), ("Assistant System", "text"),
                          ("assistant.system", "x" * 60001)):
            with self.subTest(key=key, size=len(body)), \
                    self.assertRaises(errors.CheckViolation):
                self.run_as("site", "INSERT INTO garage.llm_prompts (prompt_key, body) "
                                    "VALUES (%s, %s)", (key, body))

    def test_before_0019_the_bot_sends_the_packaged_prompts(self):
        url = self.create_database("prompts_old_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through="0018_messenger_test_mode")
        for role in ("site", "worker"):
            store = self.store(role, url)
            self.assertIsNone(store.text("assistant.system"))
            self.assertFalse(store.available)
        with self.assertRaises(errors.UndefinedTable):
            self.store("site", url).rows()

    def test_the_0019_down_script_drops_the_edits_and_up_applies_again(self):
        url = self.create_database("prompts_down_" + self.suffix)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
            connection.execute("INSERT INTO garage.llm_prompts (prompt_key, body) "
                               "VALUES ('assistant.system', %s)", (EDIT,))
            connection.execute(DOWN)
            self.assertEqual(connection.execute(
                "SELECT to_regclass('garage.llm_prompts'), "
                "to_regclass('garage.llm_prompt_history')").fetchone(), (None, None))
            connection.execute("SELECT garage.meta_provision()")  # 0018's body still runs
            connection.execute(UP)
        self.store("site", url).save("assistant.system", EDIT, 0, "boss")
        self.assertEqual(self.store("worker", url).text("assistant.system"), EDIT)


if __name__ == "__main__":
    unittest.main()
