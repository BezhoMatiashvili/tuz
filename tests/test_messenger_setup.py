"""messenger_setup: scope, credential isolation, idempotence and what the commands print.

The database tests run against a disposable PostgreSQL (WAREHOUSE_TEST_ADMIN_DATABASE_URL) in
databases and roles they create and drop; the rest use fakes. Nothing reaches Meta, and every
.env file is a temporary one.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import requests
from dotenv import dotenv_values, set_key
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive import messenger_setup
from automotive.local_setup import LocalSetupError
from automotive.messenger_setup import (
    RECEIVER_ENV_KEY,
    WORKER_ENV_KEY,
    check_public,
    configure_messenger,
)
from automotive.meta_config import MetaSettings
from automotive.meta_connect import ConnectError
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphError
from automotive.meta_store import MetaStore
from warehouse_sync.migrations import MIGRATIONS_DIRECTORY, apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
MESSENGER_SQL = (MIGRATIONS_DIRECTORY / "0013_meta_messenger.up.sql").read_text(encoding="utf-8")
FORGET_SQL = (MIGRATIONS_DIRECTORY / "0014_meta_forget.up.sql").read_text(encoding="utf-8")
MESSENGER_FILES = ("0013_meta_messenger.up.sql", "0014_meta_forget.up.sql")
FAKE_ADMIN = "postgresql://admin:fixture-admin-password@127.0.0.1:55432/test_catalog"
GENERATED = ("META_WEBHOOK_VERIFY_TOKEN", "META_TOKEN_ENCRYPTION_KEY", "MESSENGER_PUBLIC_URL",
             "META_GRAPH_API_VERSION")
ALL_EARLIER = sorted(path.name.removesuffix(".up.sql")
                     for path in MIGRATIONS_DIRECTORY.glob("*.up.sql")
                     if path.name < "0013")


def secrets_in(values: dict) -> list[str]:
    """Every secret value setup writes: login passwords, the verify token and the key."""
    found = [conninfo_to_dict(values[key])["password"] for key in (RECEIVER_ENV_KEY,
                                                                   WORKER_ENV_KEY)
             if values.get(key)]
    return found + [values[key] for key in ("META_WEBHOOK_VERIFY_TOKEN",
                                            "META_TOKEN_ENCRYPTION_KEY") if values.get(key)]


# -- against a real, disposable database -----------------------------------------------------------

@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class MessengerSetupDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if conninfo_to_dict(ADMIN_URL).get("host") not in {"127.0.0.1", "localhost", "::1"}:
            raise unittest.SkipTest("setup only accepts a loopback administrator URL")
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop_everything)

    @classmethod
    def _drop_everything(cls) -> None:
        for name in reversed(cls.databases):
            cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        for name in reversed(cls.cluster_roles):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin.close()

    def database(self, label: str, through: str) -> str:
        name = f"msetup_{label}_{self.suffix}"
        self.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.databases.append(name)
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = name
        url = make_conninfo(**info)
        with psycopg.connect(url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection, through=through)
        return url

    def env_file(self, url: str) -> Path:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / ".env"
        path.touch(mode=0o644)
        set_key(str(path), "DATABASE_URL", url)
        set_key(str(path), "WAREHOUSE_COOKIE", "preserved-fixture")
        return path

    def remember_logins(self, values: dict) -> None:
        for key in (RECEIVER_ENV_KEY, WORKER_ENV_KEY):
            if values.get(key):
                name = conninfo_to_dict(values[key])["user"]
                if name not in self.cluster_roles:
                    self.cluster_roles.append(name)

    def recorded(self, url: str) -> list[str]:
        with psycopg.connect(url) as connection:
            return [row[0] for row in connection.execute(
                "SELECT version FROM public.warehouse_schema_migrations ORDER BY version")]

    def garage_logins(self) -> set[str]:
        return {row[0] for row in self.admin.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE 'garage\\_local\\_%'")}

    def test_setup_applies_the_messenger_migrations_alone_and_creates_two_logins_once(self):
        url = self.database("main", "0012_garage_site")
        env = self.env_file(url)
        before = self.recorded(url)
        result = configure_messenger(env)
        values = dotenv_values(env)
        self.remember_logins(values)
        self.assertEqual(result, {"migration_applied": True,
                                  "logins_created": [RECEIVER_ENV_KEY, WORKER_ENV_KEY],
                                  "keys_added": [RECEIVER_ENV_KEY, WORKER_ENV_KEY, *GENERATED],
                                  "still_needed": ["META_APP_ID", "META_APP_SECRET"]})
        self.assertEqual(sorted(set(self.recorded(url)) - set(before)),
                         ["0013_meta_messenger", "0014_meta_forget"])
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(values["WAREHOUSE_COOKIE"], "preserved-fixture")
        settings = MetaSettings.from_env(values)  # every generated value passes validation
        self.assertEqual((len(settings.token_key), settings.graph_version, settings.public_url),
                         (32, "v26.0", "https://glorified-stratus-budding.ngrok-free.dev"))
        self.assertGreaterEqual(len(settings.verify_token), 64)
        for key, role in ((RECEIVER_ENV_KEY, "garage_webhook"),
                          (WORKER_ENV_KEY, "garage_messenger")):
            login = conninfo_to_dict(values[key])["user"]
            memberships = [row[0] for row in self.admin.execute(
                "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s", (login,))]
            attributes = self.admin.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s", (login,)).fetchone()
            self.assertEqual((memberships, attributes), ([role], (True, False, False, False,
                                                                  False)))
        # The real logins, no SET ROLE: the receiver only appends, the worker consumes.
        key = "msg:" + "a" * 64
        with psycopg.connect(values[RECEIVER_ENV_KEY], autocommit=True) as receiver:
            for _ in range(2):
                receiver.execute("INSERT INTO garage.meta_inbox (event_key, field, page_id, "
                                 "payload) VALUES (%s, 'messages', '1', '{}') "
                                 "ON CONFLICT DO NOTHING", (key,))
            for statement in ("SELECT inbox_id FROM garage.meta_inbox",
                              "SELECT * FROM garage.meta_tokens",
                              "SELECT * FROM garage.staff_users"):
                with self.assertRaises(errors.InsufficientPrivilege, msg=statement):
                    receiver.execute(statement)
        worker = MetaStore(MetaDatabase(values[WORKER_ENV_KEY]))
        self.assertEqual([row["event_key"] for row in worker.claim_inbox()], [key])
        self.assertFalse(worker.settings()["bot_enabled"])
        with psycopg.connect(values[WORKER_ENV_KEY], autocommit=True) as connection:
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM garage.staff_users")
        # A rerun changes nothing.
        snapshot = env.read_bytes()
        logins = self.garage_logins()
        self.assertEqual(configure_messenger(env), {"migration_applied": False,
                                                    "logins_created": [], "keys_added": [],
                                                    "still_needed": ["META_APP_ID",
                                                                     "META_APP_SECRET"]})
        self.assertEqual((env.read_bytes(), self.garage_logins()), (snapshot, logins))
        # A login that is missing from .env is created; the existing one is kept.
        kept = values[RECEIVER_ENV_KEY]
        lines = [line for line in env.read_text().splitlines()
                 if not line.startswith(WORKER_ENV_KEY + "=")]
        env.write_text("\n".join(lines) + "\n")
        again = configure_messenger(env)
        values = dotenv_values(env)
        self.remember_logins(values)
        self.assertEqual((again["logins_created"], again["keys_added"]),
                         ([WORKER_ENV_KEY], [WORKER_ENV_KEY]))
        self.assertEqual(values[RECEIVER_ENV_KEY], kept)
        self.assertEqual(len(self.garage_logins() - logins), 1)

    def test_a_rerun_on_a_database_set_up_before_0014_applies_it(self):
        url = self.database("upgrade", "0013_meta_messenger")
        env = self.env_file(url)
        result = configure_messenger(env)
        self.remember_logins(dotenv_values(env))
        self.assertTrue(result["migration_applied"])
        self.assertEqual(self.recorded(url)[-2:], ["0013_meta_messenger", "0014_meta_forget"])
        with psycopg.connect(url) as connection:
            self.assertEqual(connection.execute(
                "SELECT has_table_privilege('garage_site', 'garage.meta_comments', 'DELETE'), "
                "to_regclass('garage.meta_forgotten') IS NOT NULL").fetchone(), (True, True))
        self.assertFalse(configure_messenger(env)["migration_applied"])

    def test_setup_refuses_a_database_without_the_garage_site_and_changes_nothing(self):
        url = self.database("nosite", "0011_vehicle_detail")
        env = self.env_file(url)
        before, logins = env.read_bytes(), self.garage_logins()
        with self.assertRaisesRegex(LocalSetupError, "site_setup --setup-site"):
            configure_messenger(env)
        self.assertEqual((env.read_bytes(), self.garage_logins()), (before, logins))
        self.assertNotIn("0013_meta_messenger", self.recorded(url))
        with psycopg.connect(url) as connection:
            self.assertIsNone(connection.execute(
                "SELECT to_regclass('garage.meta_inbox')").fetchone()[0])

    def test_the_command_prints_key_names_and_never_a_value(self):
        url = self.database("cli", "0012_garage_site")
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "ops/sql").mkdir(parents=True)
        shutil.copy(ROOT / "ops/sql/provision_automotive_roles.sql", root / "ops/sql")
        env = root / ".env"
        env.touch(mode=0o644)
        set_key(str(env), "DATABASE_URL", url)
        out, err = io.StringIO(), io.StringIO()
        with (patch.object(messenger_setup, "ROOT", root), contextlib.redirect_stdout(out),
              contextlib.redirect_stderr(err)):
            messenger_setup.main(["--setup"])
            messenger_setup.main(["--setup"])
        values = dotenv_values(env)
        self.remember_logins(values)
        text = out.getvalue() + err.getvalue()
        for secret in [*secrets_in(values), conninfo_to_dict(url)["password"],
                       conninfo_to_dict(values[WORKER_ENV_KEY])["user"], "SCRAM"]:
            self.assertNotIn(secret, text)
        self.assertIn("migrations 0013_meta_messenger, 0014_meta_forget applied now.", text)
        self.assertIn("migrations 0013_meta_messenger, 0014_meta_forget were already applied.",
                      text)
        self.assertIn("Added to the private .env: " + RECEIVER_ENV_KEY, text)
        self.assertIn("Added to the private .env: nothing", text)
        self.assertIn("Still needed in .env (from the GARAGE 1886 app): META_APP_ID, "
                      "META_APP_SECRET", text)


# -- with a fake connection -----------------------------------------------------------------------

class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    """Answers setup's reads and records every statement it sends."""

    def __init__(self, *, site: bool = True, messenger: bool = False) -> None:
        self.site, self.messenger = site, messenger
        self.forget: bool | None = None  # 0014 applied; None: as 0013
        self.statements: list[tuple[str, object]] = []
        self.cleartext: list[str] = []
        self.fail_on = ""
        self.pgconn = SimpleNamespace(encrypt_password=self._encrypt)

    def _encrypt(self, password: bytes, user: bytes, algorithm: bytes) -> bytes:
        self.cleartext.append(password.decode())
        return b"SCRAM-SHA-256$4096:fixture-salt$fixture-stored:fixture-server"

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def transaction(self):
        return contextlib.nullcontext()

    def execute(self, statement, parameters=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        if self.fail_on and self.fail_on in text:
            raise psycopg.errors.InsufficientPrivilege("password=private-fixture")
        self.statements.append((text, parameters))
        rows: list[tuple] = []
        if "to_regclass('warehouse.warehouse_products')" in text:
            rows = [(True,)]
        elif text.startswith("SELECT version FROM public.warehouse_schema_migrations"):
            rows = [(version,) for version in ALL_EARLIER]
        elif "warehouse_schema_migrations WHERE version" in text:
            applied = {"0012_garage_site": self.site, "0013_meta_messenger": self.messenger,
                       "0014_meta_forget": self.messenger if self.forget is None
                       else self.forget}
            rows = [(1,)] if applied.get(parameters[0]) else []
        return Result(rows)

    def texts(self) -> list[str]:
        return [text for text, _ in self.statements]


class ConfigureWithFakesTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = self.directory / ".env"
        self.env.write_text(f"DATABASE_URL={FAKE_ADMIN}\n", encoding="utf-8")
        os.chmod(self.env, 0o644)
        self.connection = FakeConnection()
        self.connect = self.enterContext(
            patch.object(messenger_setup.psycopg, "connect", return_value=self.connection))

    def test_remote_ambiguous_or_missing_configuration_is_refused_before_connecting(self):
        for url in ("postgresql://admin:x@db.example.ge/catalog", "service=production",
                    "host=127.0.0.1 hostaddr=203.0.113.1 dbname=c user=a", ""):
            self.env.write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
            before = self.env.read_bytes()
            with self.assertRaises(LocalSetupError):
                configure_messenger(self.env)
            self.assertEqual(self.env.read_bytes(), before)
        with self.assertRaisesRegex(LocalSetupError, r"\.env"):
            configure_messenger(self.directory / "missing.env")
        self.connect.assert_not_called()

    def test_first_run_applies_only_the_messenger_migrations_and_one_role_per_login(self):
        result = configure_messenger(self.env)
        texts = self.connection.texts()
        self.assertEqual((texts.count(MESSENGER_SQL), texts.count(FORGET_SQL)), (1, 1))
        for path in MIGRATIONS_DIRECTORY.glob("*.sql"):
            if path.name not in MESSENGER_FILES:
                self.assertNotIn(path.read_text(encoding="utf-8"), texts, path.name)
        order = [texts.index(next(text for text in texts if marker in text)) for marker in (
            "pg_advisory_xact_lock", "to_regclass('warehouse.warehouse_products')",
            "CREATE ROLE garage_webhook", "CREATE TABLE garage.meta_inbox",
            "CREATE TABLE garage.meta_forgotten", 'CREATE ROLE "garage_local_webhook_',
            'CREATE ROLE "garage_local_messenger_')]
        self.assertEqual(order, sorted(order))
        self.assertLess(texts.index(FORGET_SQL), texts.index("SELECT garage.meta_provision()"))
        recorded = [parameters for text, parameters in self.connection.statements
                    if text.startswith("INSERT INTO public.warehouse_schema_migrations")]
        self.assertEqual(recorded, [("0013_meta_messenger",), ("0014_meta_forget",)])
        values = dotenv_values(self.env)
        grants = [text for text in texts if text.startswith("GRANT ")]
        receiver = conninfo_to_dict(values[RECEIVER_ENV_KEY])["user"]
        worker = conninfo_to_dict(values[WORKER_ENV_KEY])["user"]
        self.assertRegex(receiver, r"^garage_local_webhook_[0-9a-f]{12}$")
        self.assertRegex(worker, r"^garage_local_messenger_[0-9a-f]{12}$")
        self.assertEqual(grants, [f'GRANT "garage_webhook" TO "{receiver}"',
                                  f'GRANT "garage_messenger" TO "{worker}"'])
        # The server receives SCRAM verifiers, never a password.
        self.assertEqual(self.connection.cleartext, [
            conninfo_to_dict(values[key])["password"] for key in (RECEIVER_ENV_KEY,
                                                                  WORKER_ENV_KEY)])
        for password in self.connection.cleartext:
            self.assertNotIn(password, " ".join(texts))
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["keys_added"], [RECEIVER_ENV_KEY, WORKER_ENV_KEY, *GENERATED])
        self.assertEqual((conninfo_to_dict(values[WORKER_ENV_KEY])["host"],
                          conninfo_to_dict(values[WORKER_ENV_KEY])["dbname"]),
                         ("127.0.0.1", "test_catalog"))
        self.assertNotIn("fixture-admin-password", values[WORKER_ENV_KEY])

    def test_a_complete_rerun_writes_nothing(self):
        with self.env.open("a", encoding="utf-8") as target:
            for key in (RECEIVER_ENV_KEY, WORKER_ENV_KEY, *GENERATED):
                target.write(f"{key}=existing-{key.lower()}\n")
            target.write("META_APP_ID=1234567890\nMETA_APP_SECRET=existing-secret\n")
        before = self.env.read_bytes()
        self.connection.messenger = True
        self.assertEqual(configure_messenger(self.env), {
            "migration_applied": False, "logins_created": [], "keys_added": [],
            "still_needed": []})
        self.assertNotIn(MESSENGER_SQL, self.connection.texts())
        self.assertFalse([text for text in self.connection.texts()
                          if text.startswith(("CREATE ROLE \"", "GRANT", "INSERT"))])
        self.assertEqual(self.env.read_bytes(), before)

    def test_a_database_set_up_before_0014_gets_0014_alone(self):
        self.connection.messenger, self.connection.forget = True, False
        self.assertTrue(configure_messenger(self.env)["migration_applied"])
        texts = self.connection.texts()
        self.assertEqual((texts.count(MESSENGER_SQL), texts.count(FORGET_SQL)), (0, 1))
        recorded = [parameters for text, parameters in self.connection.statements
                    if text.startswith("INSERT INTO public.warehouse_schema_migrations")]
        self.assertEqual(recorded, [("0014_meta_forget",)])

    def test_without_the_garage_site_or_on_a_database_error_nothing_changes(self):
        before = self.env.read_bytes()
        self.connection.site = False
        with self.assertRaisesRegex(LocalSetupError, "site_setup --setup-site"):
            configure_messenger(self.env)
        self.assertFalse([text for text in self.connection.texts() if "CREATE" in text])
        for marker in ("CREATE TABLE garage.meta_inbox", 'CREATE ROLE "garage_local_messenger_',
                       "SELECT garage.meta_provision()"):
            self.connection = FakeConnection()
            self.connection.fail_on = marker
            self.connect.return_value = self.connection
            with self.assertRaises(LocalSetupError) as refused:
                configure_messenger(self.env)
            self.assertNotIn("private-fixture", str(refused.exception))
            self.assertIsNone(refused.exception.__cause__)
        self.assertEqual(self.env.read_bytes(), before)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o644)


# -- the commands that act like the dashboard -----------------------------------------------------

class StatusStore:
    def page(self):
        return {"page_id": "61595318832800", "page_name": "Garage • ფარეხი 1886",
                "connected_at": datetime(2026, 11, 11, 9, 0, tzinfo=UTC),
                "connected_by": "cli", "subscribed_fields": ["messages", "feed"],
                "subscribed_at": datetime(2026, 11, 11, 9, 1, tzinfo=UTC)}

    def token_rows(self):
        return [{"purpose": "page", "status": "ok", "status_detail": "", "app_id": "123",
                 "expires_at": None, "data_access_expires_at": datetime(2027, 2, 9, tzinfo=UTC)},
                {"purpose": "user", "status": "broken", "status_detail": "meta_code_190",
                 "app_id": "123", "expires_at": datetime(2027, 1, 10, tzinfo=UTC),
                 "data_access_expires_at": None}]

    def settings(self):
        return {"bot_enabled": True, "bot_paused_reason": "ai_provider",
                "bot_enabled_at": datetime(2026, 11, 11, 10, 0, tzinfo=UTC),
                "auto_reply_confirmed_off": True, "comments_enabled": False}

    def last_event_at(self):
        return {"received_at": datetime(2026, 11, 11, 10, 5, tzinfo=UTC), "field": "messages"}

    def counts_today(self):
        return {"webhooks": 4, "customer_messages": 3, "replies_sent": 2, "fallbacks": 0,
                "failed": 1, "turns": 2, "cost_usd": 0.061, "attention": 1}

    def recent_events(self, limit):
        self.limit = limit
        return [{"inbox_id": 9, "field": "message_echoes", "attempts": 1, "outcome": "stored",
                 "received_at": datetime(2026, 11, 11, 10, 5, tzinfo=UTC),
                 "processed_at": None}]


class CommandTests(unittest.TestCase):
    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                messenger_setup.main(list(arguments))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.store = StatusStore()
        self.ctx = SimpleNamespace(store=self.store, settings=MetaSettings())
        self.enterContext(patch.object(messenger_setup, "_context", return_value=self.ctx))

    def test_status_shows_health_switches_and_counts_without_tokens(self):
        code, out, _ = self.run_main("--status")
        self.assertEqual(code, 0)
        for expected in ("Page: Garage • ფარეხი 1886 (id 61595318832800)",
                         "Webhook fields: messages, feed", "Token page", "never",
                         "(meta_code_190)", "Messenger bot: PAUSED",
                         "Meta auto reply confirmed off: yes", "Comment replies: OFF",
                         "(messages)", "Today: 4 webhooks", "$0.06", "1 chats need attention"):
            self.assertIn(expected, out)
        self.assertNotIn("EAA", out)

    def test_connect_reads_the_token_hidden_and_never_prints_it(self):
        token = "EAAtypedFixtureToken0123456789"
        result = {"page_id": "61595318832800", "page_name": "Garage", "missing_scopes": [
            "ads_read"], "user_expires_at": None, "data_access_expires_at": None,
            "page_expires_at": None, "subscription": {"ok": False, "app": False, "page": True,
                                                      "error": {"code": "SUBSCRIBE_FAILED",
                                                                "step": "app",
                                                                "meta": {"code": 2200,
                                                                         "message": "verify"}}}}
        with (patch.object(messenger_setup.getpass, "getpass", return_value=token) as asked,
              patch.object(messenger_setup, "connect", return_value=result) as connected):
            code, out, err = self.run_main("--connect", "--page-id", "61595318832800")
        asked.assert_called_once()
        connected.assert_called_once_with(self.ctx, token, actor="cli",
                                          page_id="61595318832800")
        self.assertEqual(code, 0)
        self.assertNotIn(token, out + err)
        self.assertIn("Connected Garage (id 61595318832800)", out)
        self.assertIn("Missing permissions", out)
        self.assertIn("failed at the app step (Meta code 2200: verify)", out)
        refusal = ConnectError("MULTIPLE_PAGES", {"choices": [
            {"id": "1", "name": "Garage"}, {"id": "2", "name": "TNLU"}]})
        with (patch.object(messenger_setup.getpass, "getpass", return_value=token),
              patch.object(messenger_setup, "connect", side_effect=refusal)):
            code, out, err = self.run_main("--connect")
        self.assertEqual(code, 2)
        self.assertIn("Connect refused: MULTIPLE_PAGES.", err)
        self.assertIn("Garage (id 1); TNLU (id 2)", err)
        self.assertNotIn(token, out + err)

    def test_probe_events_and_ads_print_codes_and_masked_ids_only(self):
        with patch.object(messenger_setup, "probe", return_value={
                "result": "passed", "reason": "outside_window", "code": 10,
                "subcode": 2018278, "psid": "…1234", "send_purpose": "page",
                "tried": 1}) as probed:
            code, out, _ = self.run_main("--probe", "--psid", "7000000000001234")
        probed.assert_called_once_with(self.ctx, "7000000000001234", actor="cli")
        self.assertEqual(code, 0)
        self.assertIn("Probe: passed (outside_window); Meta code 10/2018278; customer …1234", out)
        self.assertNotIn("7000000000001234", out)
        code, out, _ = self.run_main("--events", "5")
        self.assertEqual((code, self.store.limit), (0, 5))
        self.assertIn("#9 message_echoes", out)
        self.assertIn("processed pending", out)
        self.run_main("--events")
        self.assertEqual(self.store.limit, 20)
        with patch.object(messenger_setup, "discover_ads", return_value=[
                {"account_id": "1383535660550763", "name": "Personal", "currency": "USD",
                 "garage_ads": 0, "ads_scanned": 3, "error": None},
                {"account_id": "2", "name": "Shop", "currency": "", "garage_ads": 0,
                 "ads_scanned": 0, "error": {"code": 200}}]):
            code, out, _ = self.run_main("--discover-ads")
        self.assertIn("act_1383535660550763 Personal [USD]: 0 of 3 ads promote the page", out)
        self.assertIn("(unreadable: code 200)", out)

    def test_failures_exit_with_codes_only(self):
        with patch.object(messenger_setup, "probe",
                          side_effect=GraphError(400, 190, 460, "OAuthException",
                                                 "token EAAleak0000000000000000")):
            code, out, err = self.run_main("--probe")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("code=190", err)
        self.assertNotIn("EAAleak", err)
        with patch.object(messenger_setup, "_context",
                          side_effect=LocalSetupError("AUTOMOTIVE_SITE_DATABASE_URL is not set.")):
            code, _, err = self.run_main("--status")
        self.assertEqual((code, err.strip()), (2, "AUTOMOTIVE_SITE_DATABASE_URL is not set."))
        for arguments in ((), ("--status", "--probe"), ("--events", "x")):
            self.assertEqual(self.run_main(*arguments)[0], 2, arguments)


class FakeHttp:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, text = answer
        return SimpleNamespace(status_code=status, text=text)


class CheckPublicTests(unittest.TestCase):
    SETTINGS = MetaSettings(verify_token="real-verify-token-fixture-0123456789",
                            public_url="https://hook.example")

    def test_a_wrong_verify_token_must_be_refused_and_health_must_answer(self):
        http = FakeHttp((403, "Forbidden"), (200, "ok"))
        results = check_public(self.SETTINGS, session=http)
        self.assertEqual([(item["status"], item["ok"]) for item in results],
                         [(403, True), (200, True)])
        webhook, health = http.calls
        self.assertEqual((webhook["url"], health["url"]),
                         ("https://hook.example/meta/webhook", "https://hook.example/healthz"))
        self.assertEqual(webhook["params"]["hub.mode"], "subscribe")
        self.assertNotEqual(webhook["params"]["hub.verify_token"], self.SETTINGS.verify_token)
        self.assertFalse(webhook["allow_redirects"])
        self.assertEqual(webhook["headers"]["ngrok-skip-browser-warning"], "1")

    def test_an_echoed_challenge_a_wrong_status_or_no_answer_fails(self):
        http = FakeHttp((200, "x"), requests.ConnectionError("down"))
        results = check_public(self.SETTINGS, session=http)
        self.assertEqual([(item["status"], item["ok"]) for item in results],
                         [(200, False), (None, False)])

        class Echo(FakeHttp):
            def get(self, url, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status_code=403, text=(kwargs.get("params") or {}).get(
                    "hub.challenge", ""))

        self.assertFalse(check_public(self.SETTINGS, session=Echo())[0]["ok"])
        with self.assertRaises(LocalSetupError):
            check_public(MetaSettings(), session=FakeHttp())

    def test_the_command_exits_non_zero_when_a_check_fails(self):
        with (patch.object(messenger_setup.MetaSettings, "from_env",
                           return_value=self.SETTINGS),
              patch.object(messenger_setup, "check_public", return_value=[
                  {"check": "health check", "status": 502, "expected": 200, "ok": False}])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exited:
                messenger_setup.main(["--check-public"])
        self.assertEqual(exited.exception.code, 1)
        self.assertIn("FAIL health check: HTTP 502 (expected 200)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
