"""Garage schema on a disposable PostgreSQL database: migrations, privileges, repository.

Skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL. Everything happens in databases and
roles this file creates and drops; the database named in that URL is only connected to.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.site_content import DEFAULT_SECTIONS, DEFAULT_SERVICES
from automotive.site_repository import Conflict, LastAdmin, SiteRepository
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted(path.name.removesuffix(".up.sql")
                    for path in (ROOT / "migrations").glob("*.up.sql"))
SITE_UP = (ROOT / "migrations/0012_garage_site.up.sql").read_text(encoding="utf-8")
SITE_DOWN = (ROOT / "migrations/0012_garage_site.down.sql").read_text(encoding="utf-8")
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
OWNER = "warehouse_migration_owner"
TABLES = ("staff_users", "staff_sessions", "site_sections", "services", "media", "audit_log")
# Migration 0013 (Garage on Facebook) adds these to schema garage; tests/test_meta_* cover them.
META_TABLES = ("meta_inbox", "meta_settings", "meta_page", "meta_tokens", "messenger_threads",
               "messenger_messages", "messenger_replies", "meta_comments", "meta_page_daily",
               "meta_images", "meta_posts", "meta_ad_accounts", "meta_ads", "meta_ad_daily",
               "meta_sync_runs", "meta_job_requests", "meta_forgotten",
               # Migration 0015 (Live chats); tests/test_live_store_database.py covers them.
               "live_alerts", "live_web_chats", "live_web_messages",
               # Migration 0019 (Prompts); tests/test_llm_prompts_database.py covers them.
               "llm_prompts", "llm_prompt_history")
# Shaped like a stored hash; these tests never verify a password.
HASH = "scrypt$15$8$3$Zml4dHVyZS1zYWx0LTE2Yg$Zml4dHVyZS1kaWdlc3QtMzItYnl0ZXMtbG9uZy0wMDAw"
HERO = {"title": {"ka": "სათაური", "en": "Headline"}, "subline": {"ka": "ქვე", "en": "Sub"}}
PIXEL = b"\x89PNG\r\n\x1a\n" + b"fixture-image-bytes"


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class GarageSiteDatabaseTests(unittest.TestCase):
    """One temporary database with every migration; garage tables are emptied per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)
        cls.url = cls.create_database("site_test_" + cls.suffix)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            cls.applied = apply_migrations_on_connection(connection)
        cls.roles = {key: cls.create_role(key, capability) for key, capability in (
            ("member", "garage_site"), ("runtime", "automotive_runtime"),
            ("enrichment", "automotive_enrichment"), ("crawler", "parts_crawler"),
            ("outsider", None))}
        cls.site = cls.repository(cls.roles["member"])

    @classmethod
    def _drop_everything(cls) -> None:
        # Databases first: a role with privileges inside one cannot be dropped before it.
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
        """A NOLOGIN role, optionally a member of one capability role, that we may SET."""
        name = f"site_{key}_{cls.suffix}"
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name)))
        cls.cluster_roles.append(name)
        if capability:
            cls.admin.execute(sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(capability), sql.Identifier(name)))
        cls.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(name)))
        return name

    @classmethod
    def repository(cls, role: str, url: str | None = None) -> SiteRepository:
        """The real SiteRepository, with every connection switched to ``role``."""
        repository = SiteRepository(url or cls.url)
        connect = repository._connect

        def as_role() -> psycopg.Connection:
            connection = connect()
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            return connection

        repository._connect = as_role
        return repository

    @contextlib.contextmanager
    def acting_as(self, role: str, url: str | None = None):
        with psycopg.connect(url or self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            yield connection

    def scalar(self, statement: str, parameters: tuple = (), url: str | None = None):
        with psycopg.connect(url or self.url) as connection:
            return connection.execute(statement, parameters).fetchone()[0]

    def setUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(
                "TRUNCATE garage.staff_users, garage.staff_sessions, garage.site_sections, "
                "garage.services, garage.media, garage.audit_log RESTART IDENTITY CASCADE")

    def assert_denied(self, role: str, statement: str, url: str | None = None) -> None:
        with self.acting_as(role, url) as connection:
            with self.assertRaises(errors.InsufficientPrivilege, msg=f"{role}: {statement}"):
                connection.execute(statement)

    # -- migrations --------------------------------------------------------------------

    def test_all_migrations_apply_as_the_database_administrator(self):
        self.assertEqual(self.applied, MIGRATIONS)
        self.assertIn("0012_garage_site", self.applied)
        with psycopg.connect(self.url) as connection:
            recorded = [row[0] for row in connection.execute(
                "SELECT version FROM public.warehouse_schema_migrations ORDER BY version")]
            tables = {row[0] for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'garage'")}
            unprotected = [row[0] for row in connection.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'garage' AND c.relkind = 'r' AND NOT c.relrowsecurity")]
            public_usage = connection.execute(
                "SELECT has_schema_privilege('public', 'garage', 'USAGE')").fetchone()[0]
        self.assertEqual(recorded, MIGRATIONS)
        self.assertEqual(tables, set(TABLES) | set(META_TABLES))
        self.assertEqual(unprotected, [])  # row-level security on every table
        self.assertFalse(public_usage)

    def test_all_migrations_apply_under_the_migration_owner_role(self):
        if not self.scalar("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                           (OWNER,)):
            self.skipTest(f"{OWNER} does not exist in this cluster")
        name = "site_owner_" + self.suffix
        url = self.create_database(name)
        with psycopg.connect(url) as connection:
            # What ops/sql/provision_production_roles.sql gives the owner in a database.
            connection.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(name), sql.Identifier(OWNER)))
            connection.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                sql.Identifier(OWNER)))
            connection.execute(ROLES_SQL)
            connection.commit()
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
            self.assertEqual(apply_migrations_on_connection(connection), MIGRATIONS)
            owner = connection.execute(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'garage'"
            ).fetchone()[0]
            self.assertEqual(owner, OWNER)
            connection.execute("SELECT garage.provision()")  # rerunnable by its owner
            # A rollback and a second upgrade, as the owner would run them.
            connection.execute(SITE_DOWN)
            self.assertIsNone(connection.execute(
                "SELECT to_regnamespace('garage')").fetchone()[0])
            connection.execute(SITE_UP)
            connection.commit()
        site = self.repository(self.roles["member"], url)
        created = site.create_user("owner-path", "", "admin", HASH)
        self.assertEqual(site.find_user("owner-path")["user_id"], created["user_id"])
        self.assertEqual(site.sync_runs(), [])
        self.assert_denied(self.roles["member"], "DELETE FROM garage.staff_users", url)
        self.assert_denied(self.roles["runtime"], "SELECT * FROM garage.staff_users", url)

    def test_provision_is_rerunnable_and_not_callable_by_the_site_role(self):
        with psycopg.connect(self.url) as connection:
            for _ in range(3):
                connection.execute("SELECT garage.provision()")
            connection.execute(ROLES_SQL)  # the role script calls it once the schema exists
            public_execute = connection.execute(
                "SELECT has_function_privilege('public', 'garage.provision()', 'EXECUTE')"
            ).fetchone()[0]
        self.assertFalse(public_execute)
        self.assertEqual(self.site.list_users(), [])
        self.assert_denied(self.roles["member"], "SELECT garage.provision()")

    def test_down_then_up_leaves_a_working_empty_schema(self):
        self.site.create_user("before-down", "", "admin", HASH)
        with psycopg.connect(self.url) as connection:
            try:
                connection.execute(SITE_DOWN)
                gone = connection.execute("SELECT to_regnamespace('garage')").fetchone()[0]
                kept = connection.execute(
                    "SELECT to_regclass('warehouse.warehouse_products') IS NOT NULL AND "
                    "to_regclass('automotive.evidence') IS NOT NULL").fetchone()[0]
                reads_sync = connection.execute(
                    "SELECT has_table_privilege('garage_site', "
                    "'warehouse.warehouse_sync_runs', 'SELECT')").fetchone()[0]
                connection.execute(SITE_DOWN)  # nothing left to remove: still succeeds
            finally:
                connection.execute(SITE_UP)
            reads_sync_again = connection.execute(
                "SELECT has_table_privilege('garage_site', "
                "'warehouse.warehouse_sync_runs', 'SELECT')").fetchone()[0]
        self.assertIsNone(gone)
        self.assertTrue(kept)
        self.assertFalse(reads_sync)
        self.assertTrue(reads_sync_again)
        self.assertEqual(self.site.list_users(), [])  # the data went with the schema
        self.assertEqual(self.site.create_user("after-up", "", "admin", HASH)["username"],
                         "after-up")

    # -- the site role through the real repository -------------------------------------

    def test_site_role_manages_accounts(self):
        site = self.site
        boss = site.create_user("boss", "The Boss", "admin", HASH)
        self.assertEqual(set(boss), {"user_id", "username", "display_name", "role",
                                     "is_active", "created_at", "updated_at",
                                     "password_changed_at", "last_login_at"})
        self.assertEqual((boss["username"], boss["display_name"], boss["role"],
                          boss["is_active"], boss["last_login_at"]),
                         ("boss", "The Boss", "admin", True, None))
        with self.assertRaises(Conflict):
            site.create_user("boss", "", "employee", HASH)
        site.create_user("worker", "", "employee", HASH)
        self.assertEqual(site.find_user("boss")["password_hash"], HASH)
        self.assertEqual(site.user_with_hash(boss["user_id"])["username"], "boss")
        self.assertIsNone(site.find_user("nobody"))
        self.assertIsNone(site.user_with_hash(999_999))
        listed = site.list_users()
        self.assertEqual([row["username"] for row in listed], ["boss", "worker"])
        self.assertTrue(all("password_hash" not in row for row in listed))
        # The table itself refuses what the application should never send.
        for username, role, stored in (("Bad Name", "admin", HASH), ("ok-name", "owner", HASH),
                                       ("ok-name", "admin", "plain-text-password"),
                                       ("ok-name", "admin", "scrypt$" + "x" * 200)):
            with self.assertRaises(errors.CheckViolation):
                site.create_user(username, "", role, stored)
        self.assertEqual(len(site.list_users()), 2)

    def test_site_role_manages_sessions(self):
        site = self.site
        boss = site.create_user("boss", "The Boss", "admin", HASH)
        worker = site.create_user("worker", "", "employee", HASH)
        first, second, other = "a" * 64, "b" * 64, "c" * 64
        site.create_session(first, boss["user_id"], 3600)
        site.create_session(second, boss["user_id"], 3600)
        site.create_session(other, worker["user_id"], 3600)
        self.assertEqual(site.resolve_session(first, 600), {
            "user_id": boss["user_id"], "username": "boss", "display_name": "The Boss",
            "role": "admin"})
        self.assertIsNotNone(site.find_user("boss")["last_login_at"])
        self.assertIsNone(site.resolve_session("d" * 64, 600))
        self.assertIsNone(site.resolve_session(first, 0))  # idle for longer than allowed
        with self.assertRaises(errors.CheckViolation):
            site.create_session("not-a-digest", boss["user_id"], 3600)
        # A new password ends the account's other sessions and nobody else's.
        self.assertTrue(site.set_password(boss["user_id"], HASH + "new", keep_session=first))
        self.assertIsNotNone(site.resolve_session(first, 600))
        self.assertIsNone(site.resolve_session(second, 600))
        self.assertIsNotNone(site.resolve_session(other, 600))
        self.assertEqual(site.find_user("boss")["password_hash"], HASH + "new")
        self.assertGreater(site.find_user("boss")["password_changed_at"],
                           boss["password_changed_at"])
        self.assertFalse(site.set_password(999_999, HASH))
        self.assertTrue(site.set_password(boss["user_id"], HASH))
        self.assertIsNone(site.resolve_session(first, 600))
        # Disabling an account ends its sessions; so does a role change.
        site.update_user(worker["user_id"], {"is_active": False})
        self.assertIsNone(site.resolve_session(other, 600))
        site.update_user(worker["user_id"], {"is_active": True})
        site.create_session(other, worker["user_id"], 3600)
        site.update_user(worker["user_id"], {"display_name": "Worker"})
        self.assertIsNotNone(site.resolve_session(other, 600))
        site.update_user(worker["user_id"], {"role": "admin"})
        self.assertIsNone(site.resolve_session(other, 600))
        site.create_session(first, boss["user_id"], 3600)
        site.create_session(second, boss["user_id"], 3600)
        site.delete_session(first)
        self.assertIsNone(site.resolve_session(first, 600))
        self.assertEqual(site.delete_user_sessions(boss["user_id"]), 1)
        self.assertEqual(site.delete_user_sessions(boss["user_id"]), 0)

    def test_expired_sessions_stop_resolving_and_are_swept(self):
        boss = self.site.create_user("boss", "", "admin", HASH)
        self.site.create_session("a" * 64, boss["user_id"], 3600)
        with psycopg.connect(self.url) as connection:
            connection.execute(
                "UPDATE garage.staff_sessions SET created_at = now() - interval '8 days', "
                "expires_at = now() - interval '1 day'")
        self.assertIsNone(self.site.resolve_session("a" * 64, 86_400 * 30))
        self.site.create_session("b" * 64, boss["user_id"], 3600)
        self.assertEqual(self.scalar("SELECT count(*) FROM garage.staff_sessions"), 1)

    def test_the_last_active_admin_cannot_be_demoted_or_disabled(self):
        site = self.site
        boss = site.create_user("boss", "", "admin", HASH)
        worker = site.create_user("worker", "", "employee", HASH)
        retired = site.create_user("retired", "", "admin", HASH)
        site.update_user(retired["user_id"], {"is_active": False})  # allowed: boss remains
        for fields in ({"role": "employee"}, {"is_active": False},
                       {"role": "employee", "is_active": False}):
            with self.assertRaises(LastAdmin):  # a disabled admin does not count
                site.update_user(boss["user_id"], fields)
        self.assertEqual((site.find_user("boss")["role"], site.find_user("boss")["is_active"]),
                         ("admin", True))
        self.assertEqual(site.update_user(boss["user_id"], {"display_name": "Renamed"})[
            "display_name"], "Renamed")
        # Changes to other accounts are not blocked by the guard.
        self.assertEqual(site.update_user(worker["user_id"], {"is_active": False})["is_active"],
                         False)
        self.assertEqual(site.update_user(worker["user_id"], {"is_active": True,
                                                              "role": "admin"})["role"], "admin")
        self.assertEqual(site.update_user(boss["user_id"], {"role": "employee"})["role"],
                         "employee")
        with self.assertRaises(LastAdmin):
            site.update_user(worker["user_id"], {"is_active": False})
        self.assertIsNone(site.update_user(999_999, {"display_name": "nobody"}))
        with self.assertRaises(ValueError):
            site.update_user(boss["user_id"], {"username": "renamed", "password_hash": HASH})

    def test_save_section_uses_optimistic_versions(self):
        site = self.site
        actor = site.create_user("boss", "", "admin", HASH)["user_id"]
        self.assertEqual(site.save_section("hero", HERO, 0, actor), 1)
        changed = {**HERO, "title": {"ka": "ახალი", "en": "New"}}
        self.assertEqual(site.save_section("hero", changed, 1, actor), 2)
        for stale in (0, 1, 3):
            with self.assertRaises(Conflict):
                site.save_section("hero", HERO, stale, actor)
        self.assertEqual(site.save_section("seo", {"title": {"ka": "ა", "en": "a"}}, 0, None), 1)
        sections = site.load_content()["sections"]
        self.assertEqual(set(sections), {"hero", "seo"})
        self.assertEqual((sections["hero"]["document"], sections["hero"]["version"]),
                         (changed, 2))
        self.assertEqual(self.scalar(
            "SELECT updated_by FROM garage.site_sections WHERE section_key = 'hero'"), actor)
        for key, document in (("Bad Key", HERO), ("hero2", ["not", "an", "object"])):
            with self.assertRaises(errors.CheckViolation):
                site.save_section(key, document, 0, actor)

    def test_seed_fills_empty_tables_once(self):
        site = self.site
        self.assertEqual(site.seed(DEFAULT_SECTIONS, DEFAULT_SERVICES),
                         {"sections": len(DEFAULT_SECTIONS), "services": len(DEFAULT_SERVICES)})
        self.assertEqual(site.seed(DEFAULT_SECTIONS, DEFAULT_SERVICES),
                         {"sections": 0, "services": 0})
        content = site.load_content()
        self.assertEqual({key: row["document"] for key, row in content["sections"].items()},
                         DEFAULT_SECTIONS)
        self.assertEqual({row["version"] for row in content["sections"].values()}, {1})
        self.assertEqual([row["title_en"] for row in content["services"]],
                         [row["title_en"] for row in DEFAULT_SERVICES])
        self.assertEqual([row["position"] for row in content["services"]],
                         list(range(1, len(DEFAULT_SERVICES) + 1)))
        self.assertEqual(content["media"], [])

    def test_site_role_manages_services(self):
        site = self.site
        fields = {"icon": "gauge", "title_ka": "დიაგნოსტიკა", "title_en": "Diagnostics",
                  "summary_ka": "", "summary_en": "Checks", "price_from": 45.5,
                  "is_published": True}
        first = site.create_service(fields)
        second = site.create_service({**fields, "title_en": "Brakes", "price_from": None})
        self.assertEqual((first["position"], second["position"], first["version"]), (1, 2, 1))
        self.assertEqual(float(first["price_from"]), 45.5)
        self.assertIsNone(second["price_from"])
        updated = site.update_service(first["service_id"], {**fields, "title_en": "Checks",
                                                            "is_published": False}, 1)
        self.assertEqual((updated["title_en"], updated["is_published"], updated["version"]),
                         ("Checks", False, 2))
        with self.assertRaises(Conflict):
            site.update_service(first["service_id"], fields, 1)
        self.assertIsNone(site.update_service(999_999, fields, 1))
        site.reorder("services", [second["service_id"], first["service_id"]])
        self.assertEqual([row["title_en"] for row in site.load_content()["services"]],
                         ["Brakes", "Checks"])
        self.assertTrue(site.delete_service(first["service_id"]))
        self.assertFalse(site.delete_service(first["service_id"]))
        with self.assertRaises(errors.CheckViolation):
            site.create_service({**fields, "title_en": ""})
        with self.assertRaises(KeyError):
            site.reorder("staff_users", [1])  # only the two orderable tables

    def test_site_role_manages_gallery_images(self):
        site = self.site
        actor = site.create_user("boss", "", "admin", HASH)["user_id"]
        image = {"sha256": "a" * 64, "content_type": "image/png", "data": PIXEL, "width": 4,
                 "height": 3}
        row = site.add_media(**image, alt_en="Shop", actor_id=actor)
        self.assertEqual({key: row[key] for key in ("sha256", "content_type", "width", "height",
                                                    "alt_ka", "alt_en", "position",
                                                    "is_published", "byte_size")},
                         {"sha256": "a" * 64, "content_type": "image/png", "width": 4,
                          "height": 3, "alt_ka": "", "alt_en": "Shop", "position": 1,
                          "is_published": False, "byte_size": len(PIXEL)})
        self.assertNotIn("data", row)
        again = site.add_media(**image, alt_en="Uploaded twice")
        self.assertEqual((again["media_id"], again["alt_en"]), (row["media_id"], "Shop"))
        self.assertEqual(self.scalar("SELECT count(*) FROM garage.media"), 1)
        self.assertIsNone(site.media_bytes("a" * 64))  # unpublished: not for the public
        hidden = site.media_bytes("a" * 64, published_only=False)
        self.assertEqual((bytes(hidden["data"]), hidden["content_type"]), (PIXEL, "image/png"))
        published = site.update_media(row["media_id"], {"is_published": True, "alt_ka": "ფოტო"})
        self.assertEqual((published["is_published"], published["alt_ka"]), (True, "ფოტო"))
        self.assertEqual(bytes(site.media_bytes("a" * 64)["data"]), PIXEL)
        listed = site.load_content()["media"]
        self.assertEqual([entry["media_id"] for entry in listed], [row["media_id"]])
        self.assertNotIn("data", listed[0])
        second = site.add_media(**{**image, "sha256": "b" * 64}, published=True)
        site.reorder("media", [second["media_id"], row["media_id"]])
        self.assertEqual([entry["sha256"][0] for entry in site.load_content()["media"]],
                         ["b", "a"])
        self.assertIsNone(site.update_media(999_999, {"alt_en": "x"}))
        self.assertTrue(site.delete_media(row["media_id"]))
        self.assertFalse(site.delete_media(row["media_id"]))
        refused = ({"content_type": "image/svg+xml"}, {"width": 4097}, {"height": 0},
                   {"data": b""}, {"data": b"x" * 1_572_865}, {"sha256": "not-a-digest"})
        for change in refused:
            with self.assertRaises(errors.CheckViolation, msg=str(list(change))):
                site.add_media(**{**image, "sha256": "c" * 64, **change})

    def test_audit_rows_are_appended_and_read_newest_first(self):
        site = self.site
        boss = site.create_user("boss", "", "admin", HASH)
        site.audit(boss, "content.saved", "hero", {"version": 2}, "203.0.113.7")
        site.audit(None, "auth.login_failed", "x" * 200, None, None)
        site.audit(boss, "user.created", "worker", {"role": "employee"}, "2001:db8:1:2::/64")
        rows = site.audit_rows()
        self.assertEqual([row["action"] for row in rows],
                         ["user.created", "auth.login_failed", "content.saved"])
        self.assertEqual(set(rows[0]), {"audit_id", "occurred_at", "actor_username", "action",
                                        "target", "detail"})
        self.assertEqual((rows[2]["actor_username"], rows[2]["target"], rows[2]["detail"]),
                         ("boss", "hero", {"version": 2}))
        self.assertEqual((rows[1]["actor_username"], len(rows[1]["target"]), rows[1]["detail"]),
                         ("", 120, {}))
        self.assertEqual([row["action"] for row in site.audit_rows(before=rows[0]["audit_id"])],
                         ["auth.login_failed", "content.saved"])
        self.assertEqual(len(site.audit_rows(limit=0)), 1)
        self.assertEqual(len(site.audit_rows(limit=2)), 2)
        self.assertEqual(self.scalar("SELECT host(client_ip) FROM garage.audit_log "
                                     "WHERE action = 'content.saved'"), "203.0.113.7")
        with self.assertRaises(errors.CheckViolation):
            site.audit(boss, "Not An Action", "x")
        # Audit rows outlive the account they name: accounts are disabled, never deleted.
        self.assertEqual(self.scalar("SELECT count(*) FROM garage.audit_log"), 3)

    def test_site_role_reads_sync_runs_and_inferred_trade_for_the_reports(self):
        with psycopg.connect(self.url) as connection:
            connection.execute(
                "INSERT INTO warehouse.warehouse_sync_runs (status) VALUES ('running')")
        runs = self.site.sync_runs()
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(set(runs[0]), {"run_id", "status", "pages_fetched", "rows_seen",
                                        "rows_added", "rows_changed", "started_at",
                                        "finished_at"})
        # None would mean the trade queries no longer match the trade schema.
        self.assertEqual(self.site.trade_daily(30), {"sales": [], "purchases": [],
                                                     "since": None})
        self.assertEqual(self.site.best_sellers(30), [])

    # -- least privilege ---------------------------------------------------------------

    def test_site_role_cannot_rewrite_history_delete_accounts_or_leave_its_schema(self):
        boss = self.site.create_user("boss", "", "admin", HASH)
        self.site.audit(boss, "user.created", "boss")
        self.site.save_section("hero", HERO, 0, boss["user_id"])
        denied = (
            "UPDATE garage.audit_log SET action = 'forged.entry'",
            "DELETE FROM garage.audit_log",
            "TRUNCATE garage.audit_log",
            "DELETE FROM garage.staff_users",
            "TRUNCATE garage.staff_users CASCADE",
            "DELETE FROM garage.site_sections",
            "DROP TABLE garage.media",
            "CREATE TABLE garage.backdoor (id integer)",
            "ALTER TABLE garage.staff_users DISABLE ROW LEVEL SECURITY",
            "SELECT * FROM automotive.evidence",
            "SELECT * FROM warehouse.warehouse_products",
            "UPDATE warehouse.warehouse_sync_runs SET status = 'forged'",
            "SELECT * FROM public.warehouse_schema_migrations",
        )
        for statement in denied:
            self.assert_denied(self.roles["member"], statement)
        for table in ("audit_log", "staff_users", "site_sections"):
            self.assertEqual(self.scalar(sql.SQL("SELECT count(*) FROM garage.{}").format(
                sql.Identifier(table))), 1, table)
        self.assertEqual(self.scalar("SELECT action FROM garage.audit_log"), "user.created")

    def test_other_capability_roles_have_no_access_to_the_site_schema(self):
        self.site.create_user("boss", "", "admin", HASH)
        statements = ("SELECT * FROM garage.staff_users", "SELECT * FROM garage.staff_sessions",
                      "SELECT * FROM garage.audit_log", "SELECT * FROM garage.site_sections",
                      "INSERT INTO garage.audit_log (action) VALUES ('forged.entry')",
                      "UPDATE garage.staff_users SET role = 'admin'")
        for key in ("runtime", "enrichment", "crawler", "outsider"):
            for statement in statements:
                self.assert_denied(self.roles[key], statement)

    def test_a_grant_without_membership_still_sees_and_writes_nothing(self):
        site = self.site
        boss = site.create_user("boss", "", "admin", HASH)
        site.create_session("a" * 64, boss["user_id"], 3600)
        site.save_section("hero", HERO, 0, boss["user_id"])
        site.create_service({"title_ka": "სერვისი", "title_en": "Service"})
        site.add_media(sha256="a" * 64, content_type="image/png", data=PIXEL, width=1, height=1)
        site.audit(boss, "user.created", "boss")
        outsider = sql.Identifier(self.roles["outsider"])
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA garage TO {}").format(outsider))
            connection.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA garage TO {}"
            ).format(outsider))
        for table in TABLES:
            count = sql.SQL("SELECT count(*) FROM garage.{}").format(sql.Identifier(table))
            with self.acting_as(self.roles["outsider"]) as connection:
                self.assertEqual(connection.execute(count).fetchone()[0], 0, table)
                self.assertEqual(connection.execute(sql.SQL("DELETE FROM garage.{}").format(
                    sql.Identifier(table))).rowcount, 0, table)
            with self.acting_as(self.roles["member"]) as connection:
                self.assertEqual(connection.execute(count).fetchone()[0], 1, table)
        # Row-level security also refuses a row this role would write.
        self.assert_denied(self.roles["outsider"],
                           "INSERT INTO garage.audit_log (action) VALUES ('forged.entry')")
        self.assert_denied(
            self.roles["outsider"],
            "INSERT INTO garage.site_sections (section_key, document) VALUES ('forged', '{}')")

    def test_setup_site_creates_a_working_login_that_is_only_a_garage_site_member(self):
        from dotenv import dotenv_values, set_key

        from automotive.site_media import packaged_gallery
        from automotive.site_setup import ENV_KEY, configure_site

        if conninfo_to_dict(ADMIN_URL).get("host") not in {"127.0.0.1", "localhost", "::1"}:
            self.skipTest("setup only accepts a loopback administrator URL")
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.touch(mode=0o644)
            set_key(str(env_path), "DATABASE_URL", self.url)
            result = configure_site(env_path)
            values = dotenv_values(env_path)
            login = conninfo_to_dict(values[ENV_KEY])["user"]
            self.cluster_roles.append(login)
            self.assertEqual(result, {
                "migration_applied": False, "login_created": True,
                "sections_seeded": len(DEFAULT_SECTIONS),
                "services_seeded": len(DEFAULT_SERVICES),
                "photos_seeded": len(packaged_gallery())})
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            before = env_path.read_bytes()
            again = configure_site(env_path)  # keeps the login, seeds nothing twice
            self.assertEqual(again, {"migration_applied": False, "login_created": False,
                                     "sections_seeded": 0, "services_seeded": 0,
                                     "photos_seeded": 0})
            self.assertEqual(env_path.read_bytes(), before)
        with psycopg.connect(self.url) as connection:
            memberships = [row[0] for row in connection.execute(
                "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s", (login,))]
            attributes = connection.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s", (login,)).fetchone()
        self.assertEqual(memberships, ["garage_site"])
        self.assertEqual(attributes, (True, False, False, False, False))
        # The real class over the real login: no SET ROLE involved.
        site = SiteRepository(values[ENV_KEY])
        boss = site.create_user("boss", "The Boss", "admin", HASH)
        site.create_session("a" * 64, boss["user_id"], 3600)
        self.assertEqual(site.resolve_session("a" * 64, 600)["username"], "boss")
        self.assertEqual(site.save_section("hero", HERO, 1, boss["user_id"]), 2)
        site.audit(boss, "content.saved", "hero", None, "203.0.113.7")
        self.assertEqual(set(site.load_content()["sections"]), set(DEFAULT_SECTIONS))
        self.assertEqual(len(site.load_content()["media"]), result["photos_seeded"])
        for statement in ("UPDATE garage.audit_log SET action = 'forged.entry'",
                          "DELETE FROM garage.audit_log", "DELETE FROM garage.staff_users",
                          "SELECT * FROM automotive.evidence",
                          "SELECT * FROM warehouse.warehouse_products"):
            with psycopg.connect(values[ENV_KEY], autocommit=True) as connection:
                with self.assertRaises(errors.InsufficientPrivilege, msg=statement):
                    connection.execute(statement)


if __name__ == "__main__":
    unittest.main()
