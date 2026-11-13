"""Meta settings validation and the AES-GCM token cipher. No network, no database."""

from __future__ import annotations

import re
import unittest

from automotive.meta_config import (
    GRAPH_HOST,
    REQUIRED_SCOPES,
    SUBSCRIBED_FIELDS,
    WEBHOOK_PATH,
    MetaConfigError,
    MetaSettings,
)
from automotive.meta_crypto import PURPOSES, TokenCipher, TokenCipherError, new_key_hex

SECRET = "fixtureappsecret0123456789abcdef"  # gitleaks:allow
VERIFY = "fixture-verify-token-0123456789_abcdefghij"
KEY = "ab" * 32
RECEIVER_URL = "postgresql://hook:receiver-password-fixture@127.0.0.1:5432/x"
WORKER_URL = "postgresql://worker:worker-password-fixture@127.0.0.1:5432/x"
TOKEN = "EAAfixtureAccessToken0123456789"
FULL = {"META_APP_ID": "1234567890", "META_APP_SECRET": SECRET,
        "META_WEBHOOK_VERIFY_TOKEN": VERIFY, "META_TOKEN_ENCRYPTION_KEY": KEY,
        "META_GRAPH_API_VERSION": "v26.0", "META_PAGE_ID": "61595318832800",
        "MESSENGER_PUBLIC_URL": "https://hook.example.ngrok-free.dev/",
        "MESSENGER_RECEIVER_PORT": "8771", "MESSENGER_RECEIVER_DATABASE_URL": RECEIVER_URL,
        "MESSENGER_DATABASE_URL": WORKER_URL, "MESSENGER_QUIET_SECONDS": "5",
        "MESSENGER_MAX_WAIT_SECONDS": "45", "MESSENGER_CONCURRENT_TURNS": "3",
        "MESSENGER_SEND_TIMEOUT_SECONDS": "20"}


class MetaSettingsTests(unittest.TestCase):
    def test_an_empty_environment_gives_defaults_and_names_what_each_process_lacks(self):
        settings = MetaSettings.from_env({})
        self.assertEqual((settings.graph_version, settings.receiver_host, settings.receiver_port,
                          settings.data_dir, settings.webhook_url),
                         ("v26.0", "127.0.0.1", 8771, "data/messenger", ""))
        self.assertEqual(settings.missing_for_receiver(), [
            "META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN", "MESSENGER_RECEIVER_DATABASE_URL"])
        self.assertEqual(settings.missing_for_worker(), [
            "META_APP_ID", "META_APP_SECRET", "META_TOKEN_ENCRYPTION_KEY",
            "MESSENGER_DATABASE_URL"])
        self.assertEqual(len(settings.missing_for_dashboard()), 5)

    def test_a_complete_environment_is_parsed(self):
        settings = MetaSettings.from_env(FULL)
        self.assertEqual(settings.token_key, bytes.fromhex(KEY))
        self.assertEqual(settings.public_url, "https://hook.example.ngrok-free.dev")
        self.assertEqual(settings.webhook_url,
                         "https://hook.example.ngrok-free.dev" + WEBHOOK_PATH)
        self.assertEqual(settings.graph_base, f"{GRAPH_HOST}/v26.0/")
        self.assertEqual((settings.page_id, settings.quiet_seconds, settings.concurrent_turns),
                         ("61595318832800", 5.0, 3))
        self.assertEqual((settings.missing_for_receiver(), settings.missing_for_worker(),
                          settings.missing_for_dashboard()), ([], [], []))
        self.assertIn("pages_messaging", REQUIRED_SCOPES)
        self.assertIn("message_echoes", SUBSCRIBED_FIELDS)

    def test_bad_values_are_refused_and_the_error_names_the_variable_not_the_value(self):
        bad = {"META_APP_ID": "12ab34", "META_APP_SECRET": "short-secret!",
               "META_WEBHOOK_VERIFY_TOKEN": "has spaces in it but is long enough okay",
               "META_TOKEN_ENCRYPTION_KEY": "ab" * 31, "META_GRAPH_API_VERSION": "version-27",
               "META_PAGE_ID": "page-61595318832800",
               "MESSENGER_PUBLIC_URL": "http://hook.example", "MESSENGER_RECEIVER_PORT": "80",
               "MESSENGER_QUIET_SECONDS": "0.5", "MESSENGER_MAX_WAIT_SECONDS": "lots",
               "MESSENGER_CONCURRENT_TURNS": "9", "MESSENGER_SEND_TIMEOUT_SECONDS": "61"}
        for name, value in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(MetaConfigError) as refused:
                    MetaSettings.from_env({**FULL, name: value})
                self.assertIn(name, str(refused.exception))
                self.assertNotIn(value, str(refused.exception))
        for url in ("https://user:pass@hook.example", "https://hook.example/?q=1",
                    "https://hook.example/#top", "ftp://hook.example", "https://"):
            with self.subTest(url=url), self.assertRaises(MetaConfigError):
                MetaSettings.from_env({**FULL, "MESSENGER_PUBLIC_URL": url})

    def test_repr_never_shows_a_secret(self):
        text = repr(MetaSettings.from_env(FULL)) + str(MetaSettings.from_env(FULL))
        for secret in (SECRET, VERIFY, KEY, "receiver-password-fixture",
                       "worker-password-fixture", repr(bytes.fromhex(KEY))):
            self.assertNotIn(secret, text)
        self.assertIn("1234567890", text)  # the app id is not a secret


class TokenCipherTests(unittest.TestCase):
    def setUp(self):
        self.cipher = TokenCipher(bytes.fromhex(KEY))

    def test_round_trip_for_every_purpose_with_a_fresh_nonce_each_time(self):
        for purpose in sorted(PURPOSES):
            sealed = self.cipher.encrypt(TOKEN, purpose=purpose)
            self.assertRegex(sealed, r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
            self.assertNotIn(TOKEN, sealed)
            self.assertEqual(self.cipher.decrypt(sealed, purpose=purpose), TOKEN)
        self.assertNotEqual(self.cipher.encrypt(TOKEN, purpose="page"),
                            self.cipher.encrypt(TOKEN, purpose="page"))
        self.assertEqual(self.cipher.decrypt(self.cipher.encrypt("EAAä€", purpose="user"),
                                             purpose="user"), "EAAä€")

    def test_a_token_sealed_for_one_purpose_cannot_be_opened_as_another(self):
        sealed = self.cipher.encrypt(TOKEN, purpose="page")
        for purpose in ("user", "send_page", "ads_user"):
            with self.assertRaises(TokenCipherError):
                self.cipher.decrypt(sealed, purpose=purpose)
        with self.assertRaises(TokenCipherError):
            self.cipher.decrypt(sealed, purpose="admin")

    def test_tampered_or_malformed_values_are_refused_without_echoing_them(self):
        sealed = self.cipher.encrypt(TOKEN, purpose="page")
        prefix, nonce, body = sealed.split(".")
        flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
        for stored in (f"{prefix}.{nonce}.{flipped}", f"v2.{nonce}.{body}",
                       f"{prefix}.{nonce[:-2]}.{body}", f"{prefix}.{nonce}", "", "v1..",
                       f"{prefix}.{nonce}.{body}.extra", None):
            with self.subTest(stored=stored):
                with self.assertRaises(TokenCipherError) as refused:
                    self.cipher.decrypt(stored, purpose="page")
                self.assertNotIn(body, str(refused.exception))
                self.assertIsNone(refused.exception.__cause__)

    def test_another_key_cannot_open_it(self):
        sealed = self.cipher.encrypt(TOKEN, purpose="user")
        with self.assertRaises(TokenCipherError):
            TokenCipher(bytes.fromhex(new_key_hex())).decrypt(sealed, purpose="user")

    def test_keys_and_tokens_are_checked(self):
        for key in (b"", b"x" * 31, b"x" * 33, KEY, None):
            with self.subTest(key=key), self.assertRaises(TokenCipherError):
                TokenCipher(key)
        for token in ("", "x" * 2049, None, b"EAA"):
            with self.subTest(token=token), self.assertRaises(TokenCipherError):
                self.cipher.encrypt(token, purpose="page")
        with self.assertRaises(TokenCipherError):
            self.cipher.encrypt(TOKEN, purpose="everything")
        first, second = new_key_hex(), new_key_hex()
        self.assertRegex(first, re.compile(r"^[0-9a-f]{64}$"))
        self.assertNotEqual(first, second)
        self.assertEqual(MetaSettings.from_env({"META_TOKEN_ENCRYPTION_KEY": first}).token_key,
                         bytes.fromhex(first))


if __name__ == "__main__":
    unittest.main()
