"""Garage site settings: what may be public, who is trusted, and what refuses to start."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from automotive.config import Settings, _site_settings
from automotive.site import check_serving

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://garage.example.ge"
PROXY = "10.0.0.5"
RUNTIME = "postgresql://runtime-fixture@127.0.0.1/catalog"
SITE = "postgresql://site-fixture@127.0.0.1/catalog"


def site_settings(**environment: str) -> dict:
    with patch.dict(os.environ, environment, clear=True):
        return _site_settings()


def from_env(**environment: str) -> Settings:
    """Settings.from_env with exactly this environment; the real .env is never read."""
    with patch.dict(os.environ, environment, clear=True), patch("automotive.config.load_dotenv"):
        return Settings.from_env()


class SiteSettingsTests(unittest.TestCase):
    def test_defaults_keep_the_site_local_with_the_staff_login_on(self):
        self.assertEqual(site_settings(), {
            "public_origins": (), "trusted_proxies": (), "staff_auth": True,
            "allowed_hosts": ("localhost", "127.0.0.1"),
            "allowed_origins": ("http://localhost:8767", "http://127.0.0.1:8767"),
            "site_database_url": "",
            "site_chat_log_path": "data/chat_logs/site_chat_log.sqlite3",
            "site_chat_daily_turns": 300, "site_vin_daily_decodes": 200,
            "site_chat_log_retention_days": 30})

    def test_public_origins_become_allowed_origins_and_hosts(self):
        settings = site_settings(AUTOMOTIVE_TRUSTED_PROXIES=PROXY, AUTOMOTIVE_PUBLIC_ORIGINS=(
            f" {ORIGIN}/ , https://www.garage.example.ge,{ORIGIN},, "
            "https://shop.example.ge:8443"))
        self.assertEqual(settings["public_origins"], (
            ORIGIN, "https://www.garage.example.ge", "https://shop.example.ge:8443"))
        self.assertEqual(settings["allowed_origins"], (
            "http://localhost:8767", "http://127.0.0.1:8767", ORIGIN,
            "https://www.garage.example.ge", "https://shop.example.ge:8443"))
        # A host is a name without scheme or port.
        self.assertEqual(settings["allowed_hosts"], (
            "localhost", "127.0.0.1", "garage.example.ge", "www.garage.example.ge",
            "shop.example.ge"))
        self.assertIs(settings["staff_auth"], True)

    def test_a_public_origin_must_be_a_bare_https_origin(self):
        refused = ("http://garage.example.ge", "garage.example.ge", "//garage.example.ge",
                   "ftp://garage.example.ge", "https://*.example.ge", "*", "https://",
                   f"{ORIGIN}/shop", f"{ORIGIN}?next=1", "https://user@garage.example.ge",
                   "https://user:secret-fixture@garage.example.ge",
                   f"{ORIGIN},http://other.example.ge")
        for value in refused:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_PUBLIC_ORIGINS") as error:
                    site_settings(AUTOMOTIVE_PUBLIC_ORIGINS=value)
                self.assertNotIn("secret-fixture", str(error.exception))

    def test_plain_http_is_accepted_for_loopback_only(self):
        for value in ("http://localhost:8767", "http://127.0.0.1:9000", "http://[::1]:8767"):
            with self.subTest(value=value):
                self.assertEqual(site_settings(AUTOMOTIVE_PUBLIC_ORIGINS=value)[
                    "public_origins"], (value,))
        for value in ("http://localhost.example.ge", "http://127.0.0.1.example.ge",
                      "http://192.168.1.10:8767", "http://0.0.0.0:8767"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    site_settings(AUTOMOTIVE_PUBLIC_ORIGINS=value)

    def test_the_staff_login_cannot_be_switched_off_with_a_public_origin(self):
        for off in ("false", "0", "no", "False"):
            with self.subTest(off=off):
                self.assertIs(site_settings(AUTOMOTIVE_STAFF_AUTH=off)["staff_auth"], False)
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_STAFF_AUTH"):
                    site_settings(AUTOMOTIVE_STAFF_AUTH=off, AUTOMOTIVE_PUBLIC_ORIGINS=ORIGIN,
                                  AUTOMOTIVE_TRUSTED_PROXIES=PROXY)
        for on in ("true", "1", "yes", " TRUE "):
            settings = site_settings(AUTOMOTIVE_STAFF_AUTH=on, AUTOMOTIVE_PUBLIC_ORIGINS=ORIGIN,
                                     AUTOMOTIVE_TRUSTED_PROXIES=PROXY)
            self.assertIs(settings["staff_auth"], True)

    def test_a_trusted_proxy_is_a_proxy_not_a_slice_of_the_internet(self):
        # Whoever is trusted may state any client address in X-Forwarded-For.
        for value in ("0.0.0.0/0", "::/0", "128.0.0.0/1", "10.0.0.0/7", "2000::/3",
                      "2001:db8::/31", "10.0.0.5,0.0.0.0/0"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_TRUSTED_PROXIES"):
                    site_settings(AUTOMOTIVE_TRUSTED_PROXIES=value)
        for value in ("10.0.0.0/8", "127.0.0.2", "173.245.48.0/20", "2400:cb00::/32", "::1"):
            self.assertEqual(site_settings(AUTOMOTIVE_TRUSTED_PROXIES=value)["trusted_proxies"],
                             (value,))

    def test_a_public_origin_needs_the_proxy_to_be_named(self):
        # Without it every visitor would look like the proxy and share one rate limit.
        for origins in (ORIGIN, f"http://localhost:9000,{ORIGIN}"):
            with self.subTest(origins=origins):
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_TRUSTED_PROXIES"):
                    site_settings(AUTOMOTIVE_PUBLIC_ORIGINS=origins)
        self.assertEqual(site_settings(AUTOMOTIVE_PUBLIC_ORIGINS=ORIGIN,
                                       AUTOMOTIVE_TRUSTED_PROXIES=PROXY)["trusted_proxies"],
                         (PROXY,))
        # A loopback origin has no proxy in front of it.
        local = site_settings(AUTOMOTIVE_PUBLIC_ORIGINS="http://localhost:9000")
        self.assertEqual((local["public_origins"], local["trusted_proxies"]),
                         (("http://localhost:9000",), ()))

    def test_trusted_proxies_must_be_addresses_or_networks(self):
        settings = site_settings(
            AUTOMOTIVE_TRUSTED_PROXIES=" 127.0.0.1, 10.0.0.0/8 ,2001:db8::/32,, ::1")
        self.assertEqual(settings["trusted_proxies"],
                         ("127.0.0.1", "10.0.0.0/8", "2001:db8::/32", "::1"))
        for value in ("proxy.internal", "10.0.0.0/33", "300.1.1.1", "127.0.0.1;10.0.0.1",
                      "127.0.0.1,nginx", "*"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_TRUSTED_PROXIES"):
                    site_settings(AUTOMOTIVE_TRUSTED_PROXIES=value)

    def test_daily_limits_and_retention_are_bounded_whole_numbers(self):
        settings = site_settings(AUTOMOTIVE_SITE_CHAT_DAILY_TURNS="0",
                                 AUTOMOTIVE_SITE_VIN_DAILY_DECODES="100000",
                                 AUTOMOTIVE_SITE_CHAT_LOG_RETENTION_DAYS=" 3650 ")
        self.assertEqual((settings["site_chat_daily_turns"], settings["site_vin_daily_decodes"],
                          settings["site_chat_log_retention_days"]), (0, 100000, 3650))
        refused = {"AUTOMOTIVE_SITE_CHAT_DAILY_TURNS": ("-1", "many", "1.5", "100001", ""),
                   "AUTOMOTIVE_SITE_VIN_DAILY_DECODES": ("100001", "1e3", "ten"),
                   "AUTOMOTIVE_SITE_CHAT_LOG_RETENTION_DAYS": ("3651", "-30", "forever")}
        for name, values in refused.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        site_settings(**{name: value})

    def test_the_example_environment_is_valid_local_and_keeps_the_staff_login_on(self):
        example = dotenv_values(ROOT / ".env.example")
        site_keys = {key: value or "" for key, value in example.items()
                     if "_SITE_" in key or key in ("AUTOMOTIVE_STAFF_AUTH",
                                                   "AUTOMOTIVE_PUBLIC_ORIGINS",
                                                   "AUTOMOTIVE_TRUSTED_PROXIES")}
        self.assertEqual(len(site_keys), 8)  # every site setting is listed in the example
        settings = site_settings(**site_keys)
        self.assertIs(settings["staff_auth"], True)
        self.assertEqual((settings["public_origins"], settings["trusted_proxies"],
                          settings["site_database_url"]), ((), (), ""))
        self.assertEqual(settings["allowed_hosts"], ("localhost", "127.0.0.1"))

    def test_paths_and_the_site_login_are_read_as_given(self):
        settings = site_settings(AUTOMOTIVE_SITE_DATABASE_URL=SITE,
                                 AUTOMOTIVE_SITE_CHAT_LOG_PATH="")
        self.assertEqual((settings["site_database_url"], settings["site_chat_log_path"]),
                         (SITE, ""))


class FromEnvTests(unittest.TestCase):
    def test_from_env_turns_the_staff_login_on_and_direct_construction_leaves_it_off(self):
        self.assertIs(Settings().staff_auth, False)  # injected-settings tests keep working
        settings = from_env()
        self.assertIs(settings.staff_auth, True)
        self.assertEqual((settings.public_origins, settings.trusted_proxies,
                          settings.site_database_url), ((), (), ""))
        self.assertEqual(settings.allowed_hosts, ("localhost", "127.0.0.1"))

    def test_site_settings_reach_the_settings_object(self):
        settings = from_env(
            AUTOMOTIVE_PUBLIC_ORIGINS=ORIGIN, AUTOMOTIVE_TRUSTED_PROXIES="127.0.0.1",
            AUTOMOTIVE_SITE_DATABASE_URL=SITE, AUTOMOTIVE_DATABASE_URL=RUNTIME,
            AUTOMOTIVE_SITE_CHAT_DAILY_TURNS="50", AUTOMOTIVE_SITE_VIN_DAILY_DECODES="20",
            AUTOMOTIVE_SITE_CHAT_LOG_PATH="/var/lib/garage/site_chat.sqlite3",
            AUTOMOTIVE_SITE_CHAT_LOG_RETENTION_DAYS="14")
        self.assertEqual((settings.public_origins, settings.trusted_proxies), ((ORIGIN,),
                                                                              ("127.0.0.1",)))
        self.assertEqual(settings.allowed_hosts, ("localhost", "127.0.0.1", "garage.example.ge"))
        self.assertIn(ORIGIN, settings.allowed_origins)
        self.assertEqual((settings.site_database_url, settings.site_chat_daily_turns,
                          settings.site_vin_daily_decodes, settings.site_chat_log_path,
                          settings.site_chat_log_retention_days),
                         (SITE, 50, 20, "/var/lib/garage/site_chat.sqlite3", 14))
        self.assertNotIn("site-fixture", repr(settings))
        check_serving(settings)  # public host with the staff login on: allowed to start

    def test_the_site_login_must_differ_from_every_other_credential(self):
        shared = {"AUTOMOTIVE_DATABASE_URL": RUNTIME,
                  "AUTOMOTIVE_ENRICHMENT_DATABASE_URL": "postgresql://enrich-fixture@127.0.0.1/c",
                  "AUTOMOTIVE_VALIDATOR_DATABASE_URL": "postgresql://valid-fixture@127.0.0.1/c"}
        for name, value in shared.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "separate garage site") as error:
                    from_env(**shared, AUTOMOTIVE_SITE_DATABASE_URL=value)
                self.assertNotIn("fixture", str(error.exception))
        self.assertEqual(from_env(**shared, AUTOMOTIVE_SITE_DATABASE_URL=SITE).site_database_url,
                         SITE)

    def test_invalid_site_settings_stop_from_env(self):
        for environment in ({"AUTOMOTIVE_PUBLIC_ORIGINS": "http://garage.example.ge"},
                            {"AUTOMOTIVE_PUBLIC_ORIGINS": ORIGIN,
                             "AUTOMOTIVE_STAFF_AUTH": "false"},
                            {"AUTOMOTIVE_PUBLIC_ORIGINS": ORIGIN},  # no proxy named
                            {"AUTOMOTIVE_TRUSTED_PROXIES": "nginx"},
                            {"AUTOMOTIVE_SITE_VIN_DAILY_DECODES": "lots"}):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    from_env(**environment)


class CheckServingTests(unittest.TestCase):
    def test_a_public_host_needs_the_staff_login(self):
        public = replace(Settings(), allowed_hosts=("localhost", "127.0.0.1",
                                                    "garage.example.ge"))
        with self.assertRaisesRegex(ValueError, "staff login"):
            check_serving(public)
        with self.assertRaisesRegex(ValueError, "TRUSTED_PROXIES"):
            check_serving(replace(public, staff_auth=True))
        check_serving(replace(public, staff_auth=True, trusted_proxies=(PROXY,)))

    def test_a_public_host_serves_visitors_only_on_the_engine_that_keeps_audiences_apart(self):
        # Only the assistant engine gives a visitor the public tools; the other two engines
        # ignore the audience.
        public = replace(Settings(), allowed_hosts=("localhost", "127.0.0.1",
                                                    "garage.example.ge"),
                         staff_auth=True, trusted_proxies=(PROXY,))
        for engine in ("orchestrated", "scripted"):
            with self.subTest(engine=engine):
                with self.assertRaisesRegex(ValueError, "AUTOMOTIVE_CHAT_ENGINE"):
                    check_serving(replace(public, chat_engine=engine))
        check_serving(replace(public, chat_engine="assistant"))
        check_serving(replace(public, chat_engine=""))
        check_serving(replace(Settings(), chat_engine="scripted"))  # loopback only

    def test_loopback_only_may_run_without_it(self):
        check_serving(Settings())
        check_serving(replace(Settings(), allowed_hosts=("localhost", "127.0.0.1", "::1")))
        check_serving(replace(Settings(), allowed_hosts=()))
        for host in ("0.0.0.0", "192.168.1.10", "localhost.example.ge", "*"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    check_serving(replace(Settings(), allowed_hosts=("localhost", host)))

    def run_cli(self, settings: Settings, *arguments: str):
        from automotive.__main__ import main

        with (patch("sys.argv", ["automotive", *arguments]),
              patch("automotive.config.Settings.from_env", return_value=settings),
              patch("automotive.api.create_app") as factory,
              patch("uvicorn.run") as runner):
            try:
                main()
            except ValueError as error:
                return error, factory, runner
        return None, factory, runner

    def test_the_server_refuses_to_start_in_that_shape(self):
        public = replace(Settings(), allowed_hosts=("localhost", "127.0.0.1",
                                                    "garage.example.ge"))
        error, factory, runner = self.run_cli(public)
        self.assertIsInstance(error, ValueError)
        factory.assert_not_called()
        runner.assert_not_called()

    def test_a_custom_port_keeps_the_public_origins_allowed(self):
        settings = replace(Settings(), staff_auth=True, public_origins=(ORIGIN,),
                           trusted_proxies=(PROXY,),
                           allowed_hosts=("localhost", "127.0.0.1", "garage.example.ge"),
                           allowed_origins=("http://localhost:8767", "http://127.0.0.1:8767",
                                            ORIGIN))
        error, factory, runner = self.run_cli(settings, "--port", "8877")
        self.assertIsNone(error)
        served = factory.call_args.kwargs["settings"]
        self.assertEqual(served.allowed_origins, (
            "http://localhost:8877", "http://127.0.0.1:8877", ORIGIN))
        self.assertIs(served.staff_auth, True)
        # The process itself listens on loopback only; the proxy is what faces the internet.
        self.assertEqual(runner.call_args.kwargs["host"], "127.0.0.1")
        self.assertFalse(runner.call_args.kwargs["proxy_headers"])


if __name__ == "__main__":
    unittest.main()
