"""Site setup scope, credential isolation and account commands (no real database)."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from automotive import site_setup
from automotive.local_setup import LocalSetupError
from automotive.site_auth import password_problem, verify_password
from automotive.site_content import DEFAULT_SECTIONS, DEFAULT_SERVICES
from automotive.site_repository import Conflict, SiteRepository
from automotive.site_setup import ENV_KEY, add_packaged_photos, configure_site, seed_defaults
from tests.test_site_guard import fast_scrypt
from warehouse_sync.migrations import MIGRATIONS_DIRECTORY

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "postgresql://admin:fixture-admin-password@127.0.0.1:55432/test_catalog"
EXISTING = "postgresql://garage_local_site_fixture:fixture-site-password@127.0.0.1:55432/x"
SITE_SQL = (MIGRATIONS_DIRECTORY / "0012_garage_site.up.sql").read_text(encoding="utf-8")
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text()
SEEDED = {"sections_seeded": 11, "services_seeded": 7, "photos_seeded": 2}
EARLIER = sorted(path.name.removesuffix(".up.sql")
                 for path in MIGRATIONS_DIRECTORY.glob("*.up.sql"))


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    """Answers the reads setup makes and records every statement it sends."""

    def __init__(self, *, site_applied: bool = False, catalog: bool = True,
                 applied: list[str] | None = None) -> None:
        self.site_applied, self.catalog = site_applied, catalog
        self.applied = EARLIER[:7] if applied is None else applied
        self.statements: list[tuple[str, tuple | None]] = []
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

    def execute(self, statement, parameters=None) -> Result:
        text = statement if isinstance(statement, str) else statement.as_string()
        if self.fail_on and self.fail_on in text:
            raise psycopg.errors.InsufficientPrivilege("role detail: password=private-fixture")
        self.statements.append((text, parameters))
        if "to_regclass('warehouse.warehouse_products')" in text:
            return Result([(self.catalog,)])
        if text.startswith("SELECT version FROM public.warehouse_schema_migrations"):
            return Result([(version,) for version in self.applied])
        if "warehouse_schema_migrations WHERE version" in text:
            return Result([(1,)] if self.site_applied else [])
        return Result([])

    def texts(self) -> list[str]:
        return [text for text, _ in self.statements]

    def matching(self, word: str) -> list[str]:
        """Statements setup composed itself (not the two SQL files) that contain ``word``."""
        return [text for text in self.texts() if word in text and text not in (SITE_SQL,
                                                                             ROLES_SQL)]


class ConfigureSiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_path = self.directory / ".env"
        self.env_path.write_text(
            f"DATABASE_URL={ADMIN}\nWAREHOUSE_COOKIE=preserved-fixture\n"
            "AUTOMOTIVE_DATABASE_URL=preserved-runtime-fixture\n", encoding="utf-8")
        os.chmod(self.env_path, 0o644)
        self.connection = FakeConnection()
        self.connect = self.enterContext(
            patch.object(site_setup.psycopg, "connect", return_value=self.connection))
        self.seed = self.enterContext(
            patch.object(site_setup, "seed_defaults", return_value=dict(SEEDED)))
        self.output = io.StringIO()

    def configure(self) -> dict:
        with (contextlib.redirect_stdout(self.output),
              contextlib.redirect_stderr(self.output)):
            return configure_site(self.env_path)

    def test_a_remote_or_ambiguous_database_url_is_refused_before_connecting(self):
        for url in ("", "postgresql://admin:fixture@db.example.ge/catalog", "service=production",
                    "host=127.0.0.1 hostaddr=203.0.113.1 dbname=catalog user=admin",
                    "host=/var/run/postgresql dbname=catalog user=admin",
                    "postgresql://admin:fixture@127.0.0.1/"):
            with self.subTest(url=url):
                self.env_path.write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
                before = self.env_path.read_bytes()
                with self.assertRaises(LocalSetupError):
                    self.configure()
                self.assertEqual(self.env_path.read_bytes(), before)
        self.connect.assert_not_called()
        self.seed.assert_not_called()

    def test_a_missing_env_file_is_refused(self):
        with self.assertRaisesRegex(LocalSetupError, r"\.env"):
            configure_site(self.directory / "missing.env")
        self.connect.assert_not_called()

    def test_first_run_applies_only_the_site_migration(self):
        with (patch("warehouse_sync.migrations.apply_migrations_on_connection") as runner,
              patch("automotive.local_setup.apply_migrations_on_connection") as local_runner):
            result = self.configure()
        runner.assert_not_called()
        local_runner.assert_not_called()
        self.assertEqual(result, {"migration_applied": True, "login_created": True, **SEEDED})
        texts = self.connection.texts()
        self.assertEqual(texts.count(SITE_SQL), 1)
        for path in MIGRATIONS_DIRECTORY.glob("*.sql"):
            if path.name != "0012_garage_site.up.sql":
                self.assertNotIn(path.read_text(encoding="utf-8"), texts, path.name)
        recorded = [parameters for text, parameters in self.connection.statements
                    if text.startswith("INSERT INTO public.warehouse_schema_migrations")]
        self.assertEqual(recorded, [("0012_garage_site",)])
        # Lock first, then the preflight, the roles, the migration and its grants.
        order = [texts.index(next(text for text in texts if marker in text)) for marker in (
            "pg_advisory_xact_lock", "to_regclass('warehouse.warehouse_products')",
            "CREATE ROLE garage_site", "CREATE SCHEMA garage", "SELECT garage.provision()",
            "CREATE ROLE \"garage_local_site_")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(texts.count(ROLES_SQL), 1)

    def test_the_new_login_is_a_member_of_garage_site_only(self):
        self.configure()
        created = self.connection.matching("CREATE ROLE")
        self.assertEqual(len(created), 1)
        login = conninfo_to_dict(dotenv_values(self.env_path)[ENV_KEY])
        self.assertRegex(login["user"], r"^garage_local_site_[0-9a-f]{12}$")
        for attribute in ("LOGIN", "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOBYPASSRLS"):
            self.assertIn(f" {attribute} ", created[0])
        self.assertIn(f'CREATE ROLE "{login["user"]}"', created[0])
        self.assertEqual(self.connection.matching("GRANT"),
                         [f'GRANT garage_site TO "{login["user"]}"'])
        self.assertEqual(self.connection.matching("ALTER ROLE"), [])
        # The server is sent a SCRAM verifier, never the password itself.
        self.assertEqual(self.connection.cleartext, [login["password"]])
        self.assertIn("SCRAM-SHA-256$", created[0])
        for text in self.connection.texts():
            self.assertNotIn(login["password"], text)

    def test_the_login_is_saved_privately_and_nothing_secret_is_printed(self):
        self.configure()
        values = dotenv_values(self.env_path)
        login = conninfo_to_dict(values[ENV_KEY])
        self.assertEqual((login["host"], login["port"], login["dbname"]),
                         ("127.0.0.1", "55432", "test_catalog"))
        self.assertGreaterEqual(len(login["password"]), 48)
        self.assertNotIn("fixture-admin-password", values[ENV_KEY])
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        # Everything that was already configured is still there.
        self.assertEqual((values["DATABASE_URL"], values["WAREHOUSE_COOKIE"],
                          values["AUTOMOTIVE_DATABASE_URL"]),
                         (ADMIN, "preserved-fixture", "preserved-runtime-fixture"))
        self.assertEqual(self.output.getvalue(), "")
        # Default content is stored through the new site login, not the admin connection.
        (repository,), _ = self.seed.call_args
        self.assertIsInstance(repository, SiteRepository)
        self.assertEqual(repository.database_url, values[ENV_KEY])

    def test_a_rerun_keeps_the_existing_login_and_migration(self):
        with self.env_path.open("a", encoding="utf-8") as target:
            target.write(f"{ENV_KEY}={EXISTING}\n")
        before = self.env_path.read_bytes()
        self.connection.site_applied = True
        result = self.configure()
        self.assertEqual(result, {"migration_applied": False, "login_created": False, **SEEDED})
        self.assertNotIn(SITE_SQL, self.connection.texts())
        for word in ("CREATE ROLE", "GRANT", "INSERT INTO"):
            self.assertEqual(self.connection.matching(word), [], word)
        self.assertEqual(self.connection.cleartext, [])
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertIn("SELECT garage.provision()", self.connection.texts())  # safe to repeat
        (repository,), _ = self.seed.call_args
        self.assertEqual(repository.database_url, EXISTING)
        self.assertEqual(self.output.getvalue(), "")

    def test_an_unmigrated_or_half_migrated_catalog_stops_before_any_change(self):
        cases = ((FakeConnection(catalog=False), "not migrated"),
                 (FakeConnection(applied=EARLIER[:3]), "Earlier warehouse migrations"))
        for connection, message in cases:
            with self.subTest(message):
                self.connect.return_value = connection
                before = self.env_path.read_bytes()
                with self.assertRaisesRegex(LocalSetupError, message):
                    self.configure()
                self.assertEqual(self.env_path.read_bytes(), before)
                self.assertEqual([text for text in connection.texts()
                                  if not text.startswith("SELECT")], [])
        self.seed.assert_not_called()

    def test_database_errors_are_withheld_and_leave_the_env_file_alone(self):
        before = self.env_path.read_bytes()
        self.connect.side_effect = psycopg.OperationalError("password=private-fixture")
        with self.assertRaises(LocalSetupError) as refused:
            self.configure()
        self.assertNotIn("private-fixture", str(refused.exception))
        self.assertIsNone(refused.exception.__cause__)
        self.connect.side_effect = None
        for statement in ("CREATE SCHEMA garage", "CREATE ROLE \"garage_local_site_",
                          "GRANT garage_site"):
            with self.subTest(statement=statement):
                self.connection = FakeConnection()
                self.connection.fail_on = statement
                self.connect.return_value = self.connection
                with self.assertRaises(LocalSetupError) as refused:
                    self.configure()
                self.assertNotIn("private-fixture", str(refused.exception))
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o644)
        self.seed.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")


class Store:
    """The repository methods the seeding and account commands use."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.media: list[dict] = []
        self.audits: list[tuple] = []
        self.seeded: list[tuple] = []
        self.passwords: list[tuple] = []
        self.error: Exception | None = None

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def seed(self, sections, services):
        self._check()
        self.seeded.append((sections, services))
        return {"sections": len(sections), "services": len(services)}

    def load_content(self):
        self._check()
        return {"sections": {}, "services": [], "media": list(self.media)}

    def add_media(self, **photo):
        self.media.append(photo)

    def create_user(self, username, display_name, role, password_hash):
        self._check()
        if username in self.users:
            raise Conflict("username")
        self.users[username] = {"user_id": len(self.users) + 1, "username": username,
                                "display_name": display_name, "role": role,
                                "is_active": True, "password_hash": password_hash}
        return self.users[username]

    def find_user(self, username):
        self._check()
        return self.users.get(username)

    def set_password(self, user_id, password_hash):
        self.passwords.append((user_id, password_hash))
        return True

    def list_users(self):
        self._check()
        return sorted(self.users.values(), key=lambda row: row["username"])

    def audit(self, actor, action, target="", detail=None):
        self.audits.append((actor, action, target, detail))


class SeedTests(unittest.TestCase):
    PHOTOS = [{"sha256": "a" * 64, "content_type": "image/webp", "data": b"x", "width": 1,
               "height": 1, "alt_ka": "", "alt_en": "One", "published": True},
              {"sha256": "b" * 64, "content_type": "image/webp", "data": b"y", "width": 1,
               "height": 1, "alt_ka": "", "alt_en": "Two", "published": False}]

    def setUp(self):
        self.store = Store()
        self.enterContext(patch("automotive.site_media.packaged_gallery",
                                return_value=[dict(photo) for photo in self.PHOTOS]))

    def test_packaged_texts_services_and_photos_are_offered_to_the_repository(self):
        self.assertEqual(seed_defaults(self.store), {
            "sections_seeded": len(DEFAULT_SECTIONS), "services_seeded": len(DEFAULT_SERVICES),
            "photos_seeded": 2})
        self.assertEqual(self.store.seeded, [(DEFAULT_SECTIONS, DEFAULT_SERVICES)])
        self.assertEqual(self.store.media, self.PHOTOS)

    def test_photos_are_not_added_to_a_gallery_that_already_has_some(self):
        self.store.media = [{"media_id": 1}]
        self.assertEqual(seed_defaults(self.store)["photos_seeded"], 0)
        self.assertEqual(self.store.media, [{"media_id": 1}])

    def test_a_database_error_is_reported_without_details(self):
        self.store.error = psycopg.OperationalError("password=private-fixture")
        with self.assertRaises(LocalSetupError) as refused:
            seed_defaults(self.store)
        self.assertNotIn("private-fixture", str(refused.exception))
        self.assertIsNone(refused.exception.__cause__)


class AddPackagedPhotosTests(unittest.TestCase):
    """A photo packaged after the site was set up reaches a gallery that is not empty."""

    PHOTOS = [*SeedTests.PHOTOS,
              {"sha256": "c" * 64, "content_type": "image/webp", "data": b"z", "width": 1,
               "height": 1, "alt_ka": "", "alt_en": "Three", "published": True}]

    def setUp(self):
        self.store = Store()
        self.enterContext(patch("automotive.site_media.packaged_gallery",
                                return_value=[dict(photo) for photo in self.PHOTOS]))

    def test_only_photos_the_gallery_lacks_are_stored_and_the_rest_are_left_alone(self):
        # The owner renamed this one and took it off the site; neither is undone.
        kept = {"media_id": 7, "sha256": "a" * 64, "alt_en": "Renamed in the dashboard",
                "is_published": False}
        uploaded = {"media_id": 8, "sha256": "d" * 64, "alt_en": "Uploaded in the dashboard",
                    "is_published": True}
        self.store.media = [kept, uploaded]
        self.assertEqual(add_packaged_photos(self.store),
                         {"photos_added": 2, "photos_published": 1})
        self.assertEqual(self.store.media, [kept, uploaded, *self.PHOTOS[1:]])
        self.assertEqual(self.store.audits, [(None, "media.packaged_cli", "",
                                              {"count": 2, "published": 1})])

    def test_an_empty_gallery_receives_every_packaged_photo_in_order(self):
        self.assertEqual(add_packaged_photos(self.store),
                         {"photos_added": 3, "photos_published": 2})
        self.assertEqual(self.store.media, self.PHOTOS)

    def test_a_second_run_adds_nothing_and_is_not_written_to_the_audit_log(self):
        add_packaged_photos(self.store)
        self.assertEqual(add_packaged_photos(self.store),
                         {"photos_added": 0, "photos_published": 0})
        self.assertEqual(len(self.store.media), 3)
        self.assertEqual(len(self.store.audits), 1)

    def test_a_database_error_is_reported_without_details(self):
        self.store.error = psycopg.OperationalError("password=private-fixture")
        with self.assertRaises(LocalSetupError) as refused:
            add_packaged_photos(self.store)
        self.assertNotIn("private-fixture", str(refused.exception))
        self.assertIsNone(refused.exception.__cause__)
        self.assertEqual((self.store.media, self.store.audits), ([], []))


class AccountCommandTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.opened = self.enterContext(
            patch.object(site_setup, "_repository", return_value=self.store))
        self.enterContext(patch("automotive.site_auth._scrypt", fast_scrypt))
        self.asked = self.enterContext(patch.object(site_setup.getpass, "getpass"))

    def test_a_generated_password_is_returned_once_and_only_its_hash_is_stored(self):
        password = site_setup.create_user("  NewAdmin ", "admin", "  The Owner ", generate=True)
        self.assertIsNone(password_problem(password, "newadmin"))
        row = self.store.users["newadmin"]
        self.assertEqual((row["role"], row["display_name"]), ("admin", "The Owner"))
        self.assertTrue(verify_password(password, row["password_hash"]))
        self.assertNotIn(password, row["password_hash"])
        self.assertEqual(self.store.audits, [(None, "user.created_cli", "newadmin",
                                              {"role": "admin"})])
        self.assertNotIn(password, str(self.store.audits))
        self.asked.assert_not_called()
        other = site_setup.create_user("second", "employee", generate=True)
        self.assertNotEqual(other, password)

    def test_a_typed_password_is_asked_twice_and_never_returned(self):
        self.asked.side_effect = ["typed-test-password-1", "typed-test-password-1"]
        self.assertIsNone(site_setup.create_user("worker", "employee"))
        self.assertEqual(self.asked.call_count, 2)
        self.assertTrue(verify_password("typed-test-password-1",
                                        self.store.users["worker"]["password_hash"]))

    def test_mismatched_or_weak_passwords_change_nothing(self):
        self.asked.side_effect = ["typed-test-password-1", "typed-test-password-2"]
        with self.assertRaisesRegex(LocalSetupError, "did not match"):
            site_setup.create_user("worker", "employee")
        for typed, code in (("tiny-pw", "TOO_SHORT"), ("zzzzzzzzzzzz", "TOO_SIMPLE"),
                            ("Administrator", "SAME_AS_USERNAME")):
            self.asked.side_effect = [typed, typed]
            with self.assertRaisesRegex(LocalSetupError, code) as refused:
                site_setup.create_user("administrator", "admin")
            self.assertNotIn(typed, str(refused.exception))  # the password is not echoed
        self.assertEqual((self.store.users, self.store.audits), ({}, []))

    def test_invalid_names_and_roles_are_refused_before_the_database_is_opened(self):
        for username, role in (("ab", "admin"), ("Bad Name", "admin"), ("-dash", "admin"),
                               ("u" * 33, "admin"), ("valid-name", "owner"),
                               ("valid-name", "")):
            with self.assertRaises(LocalSetupError):
                site_setup.create_user(username, role, generate=True)
        self.opened.assert_not_called()

    def test_a_taken_name_or_a_database_error_is_a_safe_message(self):
        site_setup.create_user("worker", "employee", generate=True)
        with self.assertRaisesRegex(LocalSetupError, "already exists"):
            site_setup.create_user("WORKER", "admin", generate=True)
        self.assertEqual(self.store.users["worker"]["role"], "employee")
        self.store.error = psycopg.OperationalError("password=private-fixture")
        for command in (lambda: site_setup.create_user("other", "admin", generate=True),
                        lambda: site_setup.reset_password("worker", generate=True)):
            with self.assertRaises(LocalSetupError) as refused:
                command()
            self.assertNotIn("private-fixture", str(refused.exception))

    def test_reset_password_replaces_the_hash_and_is_audited(self):
        site_setup.create_user("worker", "employee", generate=True)
        password = site_setup.reset_password(" Worker ", generate=True)
        self.assertIsNone(password_problem(password, "worker"))
        (user_id, stored), = self.store.passwords
        self.assertEqual(user_id, self.store.users["worker"]["user_id"])
        self.assertTrue(verify_password(password, stored))
        self.assertEqual(self.store.audits[-1][1:3], ("user.password_reset_cli", "worker"))
        with self.assertRaisesRegex(LocalSetupError, "No account named nobody"):
            site_setup.reset_password("nobody", generate=True)
        self.asked.side_effect = ["short", "short"]
        with self.assertRaisesRegex(LocalSetupError, "TOO_SHORT"):
            site_setup.reset_password("worker")
        self.assertEqual(len(self.store.passwords), 1)

class SiteLoginLookupTests(unittest.TestCase):
    def test_commands_use_only_the_site_login_and_say_how_to_create_it(self):
        with (patch.dict(os.environ, {"DATABASE_URL": ADMIN}, clear=True),
              patch.object(site_setup, "load_dotenv") as load):
            # The admin URL is present, but it is never borrowed for account commands.
            with self.assertRaisesRegex(LocalSetupError, "--setup-site"):
                site_setup.create_user("worker", "employee", generate=True)
            os.environ[ENV_KEY] = EXISTING
            repository = site_setup._repository()
        self.assertIsInstance(repository, SiteRepository)
        self.assertEqual(repository.database_url, EXISTING)
        # Values already in the environment win over the .env file.
        self.assertEqual(load.call_args.kwargs, {"override": False})


class CommandLineTests(unittest.TestCase):
    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                site_setup.main(list(arguments))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_setup_site_end_to_end_prints_no_credential(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "ops/sql").mkdir(parents=True)
        shutil.copy(ROOT / "ops/sql/provision_automotive_roles.sql", root / "ops/sql")
        (root / ".env").write_text(f"DATABASE_URL={ADMIN}\n", encoding="utf-8")
        connection = FakeConnection()
        with (patch.object(site_setup, "ROOT", root),
              patch.object(site_setup.psycopg, "connect", return_value=connection),
              patch.object(site_setup, "seed_defaults", return_value=dict(SEEDED))):
            code, out, err = self.run_main("--setup-site")
            again = self.run_main("--setup-site")
        self.assertEqual((code, err), (0, ""))
        login = conninfo_to_dict(dotenv_values(root / ".env")[ENV_KEY])
        for secret in (login["password"], login["user"], "fixture-admin-password", "SCRAM"):
            self.assertNotIn(secret, out + again[1])
        self.assertIn("saved in the private .env", out)
        self.assertIn("Seeded 11 text sections, 7 services, 2 photos.", out)
        self.assertIn("--create-user NAME --role admin", out)
        self.assertIn("existing site login was kept", again[1])
        self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(connection.matching("CREATE ROLE")), 1)  # not a second login

    def test_account_commands_report_and_show_a_generated_password_once(self):
        with patch.object(site_setup, "create_user", return_value="generated-fixture-9") as make:
            code, out, _ = self.run_main("--create-user", " Boss ", "--role", "admin",
                                         "--display-name", "The Owner", "--generate")
        make.assert_called_once_with(" Boss ", "admin", "The Owner", True)
        self.assertEqual(code, 0)
        self.assertIn("Created admin account boss.", out)
        self.assertEqual(out.count("generated-fixture-9"), 1)
        with patch.object(site_setup, "create_user", return_value=None) as make:
            code, out, _ = self.run_main("--create-user", "worker")
        make.assert_called_once_with("worker", "employee", "", False)  # employee by default
        self.assertNotIn("password", out.lower())
        with patch.object(site_setup, "reset_password", return_value="generated-fixture-8"):
            code, out, _ = self.run_main("--reset-password", "Worker", "--generate")
        self.assertIn("Password changed for worker", out)
        self.assertEqual(out.count("generated-fixture-8"), 1)

    def test_add_photos_says_how_many_were_added_and_how_many_are_shown(self):
        store = Store()
        with (patch.object(site_setup, "_repository", return_value=store),
              patch.object(site_setup, "add_packaged_photos",
                           return_value={"photos_added": 5, "photos_published": 1}) as add):
            code, out, err = self.run_main("--add-photos")
        add.assert_called_once_with(store)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Added 5 packaged photos to the gallery: 1 shown on the site, "
                      "4 not shown until published in the dashboard.", out)
        with (patch.object(site_setup, "_repository", return_value=store),
              patch.object(site_setup, "add_packaged_photos",
                           return_value={"photos_added": 0, "photos_published": 0})):
            code, out, _ = self.run_main("--add-photos")
        self.assertEqual(code, 0)
        self.assertIn("nothing was added", out)
        # Without the site login there is nothing to add to, and the message says what to run.
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(site_setup, "load_dotenv")):
            code, out, err = self.run_main("--add-photos")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--setup-site", err)

    def test_list_users_shows_names_roles_and_state_only(self):
        store = Store()
        store.users = {
            "boss": {"username": "boss", "role": "admin", "is_active": True,
                     "password_hash": "scrypt$private-fixture"},
            "gone": {"username": "gone", "role": "employee", "is_active": False,
                     "password_hash": "scrypt$private-fixture"}}
        with patch.object(site_setup, "_repository", return_value=store):
            code, out, _ = self.run_main("--list-users")
        self.assertEqual(code, 0)
        self.assertEqual([line.split() for line in out.splitlines()],
                         [["boss", "admin", "active"], ["gone", "employee", "disabled"]])
        self.assertNotIn("scrypt", out)

    def test_failures_exit_with_a_safe_message(self):
        with patch.object(site_setup, "configure_site",
                          side_effect=LocalSetupError("Local setup requires a loopback URL.")):
            code, out, err = self.run_main("--setup-site")
        self.assertEqual((code, out, err), (2, "", "Local setup requires a loopback URL.\n"))
        store = Store()
        store.error = psycopg.OperationalError("password=private-fixture")
        with patch.object(site_setup, "_repository", return_value=store):
            code, out, err = self.run_main("--list-users")
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("private-fixture", err)
        self.assertIn("withheld", err)

    def test_exactly_one_action_and_a_known_role_are_required(self):
        for arguments in ((), ("--setup-site", "--list-users"),
                          ("--create-user", "boss", "--role", "owner"), ("--role", "admin")):
            code, out, _ = self.run_main(*arguments)
            self.assertEqual((code, out), (2, ""), arguments)


if __name__ == "__main__":
    unittest.main()
