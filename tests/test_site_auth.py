"""Password hashing, session tokens, the sign-in throttle and SiteAuth (no database)."""

from __future__ import annotations

import base64
import hashlib
import unittest
from unittest.mock import patch

import psycopg

from automotive import site_auth
from automotive.site_auth import (
    PASSWORD_MAX,
    SESSION_IDLE,
    SESSION_LIFETIME,
    TOKEN,
    AuthError,
    LoginThrottle,
    SiteAuth,
    cookie_name,
    csrf_token,
    hash_password,
    ip_key,
    new_token,
    password_problem,
    public_user,
    read_cookie,
    session_cookie,
    token_digest,
    verify_password,
)
from automotive.site_repository import SiteDatabaseNotConfigured

PASSWORD = "test-password-1"
NEW_PASSWORD = "new-test-password-2"
ADDRESS = "203.0.113.7"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def stored(password: str = PASSWORD, *, scheme: str = "scrypt", n_log2: int = 14, r: int = 8,
           p: int = 1, salt: bytes = b"0123456789abcdef", dklen: int = 32) -> str:
    """A stored value whose digest really is the scrypt of ``password`` under these costs."""
    digest = hashlib.scrypt(password.encode(), salt=salt, n=1 << n_log2, r=r, p=p,
                            maxmem=64 * 1024 * 1024, dklen=dklen)
    return "$".join((scheme, str(n_log2), str(r), str(p), _b64(salt), _b64(digest)))


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class PasswordHashTests(unittest.TestCase):
    def test_hash_verifies_only_the_same_password_and_is_salted(self):
        # Four scrypt runs at the production cost; cheaper parameters cover the rest below.
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password(PASSWORD, first))
        self.assertFalse(verify_password(PASSWORD + "x", first))
        cheap = stored()
        for wrong in ("", PASSWORD.upper(), PASSWORD[:-1], PASSWORD + " "):
            self.assertFalse(verify_password(wrong, cheap))

    def test_stored_format_states_its_cost_and_fits_the_database_column(self):
        encoded = hash_password(PASSWORD)
        scheme, n_log2, r, p, salt, digest = encoded.split("$")
        self.assertEqual((scheme, n_log2, r, p), ("scrypt", "15", "8", "3"))
        self.assertGreaterEqual(len(_unb64(salt)), 16)
        self.assertEqual(len(_unb64(digest)), 32)
        # garage.staff_users.password_hash must start with `scrypt$` and fit 200 characters.
        self.assertLessEqual(len(encoded), 200)
        self.assertNotIn(PASSWORD, encoded)

    def test_a_lower_cost_inside_the_accepted_range_still_verifies(self):
        # A hash written before a cost increase has to keep working after it.
        encoded = stored(n_log2=14, p=1)
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password("wrong-password-1", encoded))

    def test_a_genuine_digest_under_out_of_range_parameters_never_verifies(self):
        # Every value below is the real scrypt of the password, so only the range check
        # stands between a downgraded or truncated row and a successful sign-in.
        cases = {
            "work factor below the floor": stored(n_log2=13),
            "block size other than 8": stored(r=4),
            "parallelism above the ceiling": stored(p=6),
            "salt shorter than 8 bytes": stored(salt=b"short"),
            "digest shorter than 32 bytes": stored(dklen=16),
            "another scheme name": stored(scheme="scrypt2"),
        }
        for name, encoded in cases.items():
            with self.subTest(name):
                self.assertFalse(verify_password(PASSWORD, encoded))

    def test_malformed_stored_values_never_verify_and_never_raise(self):
        good = stored()
        scheme, n_log2, r, p, salt, digest = good.split("$")
        malformed = [
            None, 5, b"scrypt", "", "scrypt", PASSWORD, good + "$extra",
            "$".join((scheme, n_log2, r, p, salt)),  # no digest
            "$".join((scheme, "x", r, p, salt, digest)),  # cost is not a number
            "$".join((scheme, "-14", r, p, salt, digest)),
            "$".join((scheme, "18", r, p, salt, digest)),  # above the ceiling
            "$".join((scheme, n_log2, r, "0", salt, digest)),
            "$".join((scheme, n_log2, r, p, "é" * 16, digest)),  # not base64
            "$".join((scheme, n_log2, r, p, salt, digest[:-4])),  # truncated digest
            "$".join((scheme, n_log2, r, p, salt,  # one flipped leading character
                      ("A" if digest[0] != "A" else "B") + digest[1:])),
        ]
        for encoded in malformed:
            with self.subTest(encoded=repr(encoded)[:40]):
                self.assertFalse(verify_password(PASSWORD, encoded))

    def test_a_cost_the_memory_cap_cannot_serve_is_refused_not_raised(self):
        # 16 and 17 pass the range check, but 64 MiB of scrypt memory stops at N=2^15.
        scheme, _, r, p, salt, digest = stored().split("$")
        for n_log2 in ("16", "17"):
            with self.subTest(n_log2=n_log2):
                self.assertFalse(verify_password(
                    PASSWORD, "$".join((scheme, n_log2, r, p, salt, digest))))

    def test_passwords_that_cannot_be_hashed_never_verify(self):
        encoded = stored()
        unusable = (None, 12345, PASSWORD.encode(), "x" * (4 * PASSWORD_MAX + 1),
                    "\ud800" * 12)  # a lone surrogate, as JSON "\ud800" delivers it
        for password in unusable:
            with self.subTest(password=repr(password)[:24]):
                self.assertFalse(verify_password(password, encoded))

    def test_password_problem_codes(self):
        cases = [
            (None, "", "TOO_SHORT"), (12345678901, "", "TOO_SHORT"), ("", "", "TOO_SHORT"),
            ("abcdefgh9", "", "TOO_SHORT"),  # nine characters
            ("abcdefgh90", "", None),  # ten is enough
            ("abcd" * 32, "", None),  # 128 is still allowed
            ("abcd" * 32 + "e", "", "TOO_LONG"),
            ("Administrator", "administrator", "SAME_AS_USERNAME"),
            ("administrator", "ADMINISTRATOR", "SAME_AS_USERNAME"),
            ("administrator", "", None),  # no username to compare with
            ("aaaaaaaaaaaa", "", "TOO_SIMPLE"), ("abcabcabcabc", "", "TOO_SIMPLE"),
            ("abcdabcdabcd", "", None),  # four different characters
            (PASSWORD, "boss", None),
            ("გამარჯობა-პაროლი", "boss", None),
        ]
        for password, username, expected in cases:
            with self.subTest(password=repr(password)[:20], username=username):
                self.assertEqual(password_problem(password, username), expected)


class TokenAndCookieTests(unittest.TestCase):
    def test_tokens_are_random_and_only_their_digest_is_stored(self):
        tokens = {new_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertTrue(TOKEN.fullmatch(token))
        token = tokens.pop()
        digest = token_digest(token)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")  # the CHECK on staff_sessions.token_hash
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, digest)

    def test_csrf_value_is_deterministic_per_token_and_differs_between_tokens(self):
        first, second = new_token(), new_token()
        self.assertEqual(csrf_token(first), csrf_token(first))
        self.assertNotEqual(csrf_token(first), csrf_token(second))
        self.assertRegex(csrf_token(first), r"^[0-9a-f]{64}$")
        # Neither the cookie value nor the digest the database holds works as the header.
        self.assertNotEqual(csrf_token(first), first)
        self.assertNotEqual(csrf_token(first), token_digest(first))
        self.assertNotEqual(csrf_token(first), csrf_token(token_digest(first)))

    def test_cookie_is_http_only_strict_and_host_bound_when_secure(self):
        token = new_token()
        plain = session_cookie(token, secure=False)
        self.assertTrue(plain.startswith(f"garage_session={token}; "))
        attributes = {part.strip() for part in plain.split(";")[1:]}
        self.assertEqual(attributes, {"Path=/", "HttpOnly", "SameSite=Strict",
                                      f"Max-Age={SESSION_LIFETIME}"})
        secure = session_cookie(token, secure=True)
        self.assertTrue(secure.startswith(f"__Host-garage_session={token}; "))
        attributes = {part.strip() for part in secure.split(";")[1:]}
        # A __Host- cookie needs Secure, Path=/ and no Domain attribute.
        self.assertEqual(attributes, {"Path=/", "HttpOnly", "SameSite=Strict", "Secure",
                                      f"Max-Age={SESSION_LIFETIME}"})
        self.assertEqual(cookie_name(False), "garage_session")
        self.assertEqual(cookie_name(True), "__Host-garage_session")
        self.assertIn("Max-Age=0", session_cookie("", secure=False, max_age=0))

    def test_read_cookie_returns_only_a_well_formed_token_under_the_exact_name(self):
        token = new_token()
        self.assertEqual(read_cookie(f"garage_session={token}", "garage_session"), token)
        self.assertEqual(
            read_cookie(f"theme=dark;  garage_session={token} ; other=1", "garage_session"),
            token)
        self.assertEqual(
            read_cookie(f"__Host-garage_session={token}", "__Host-garage_session"), token)
        ignored = [
            None, "", "garage_session", "garage_session=",
            f"garage_session={token[:-1]}",  # 42 characters
            f"garage_session={token}x",  # 44 characters
            f"garage_session={token[:-1]}+", f"garage_session={token[:-1]}/",
            f'garage_session="{token}"', f"garage_session={token[:20]} {token[20:]}",
            f"xgarage_session={token}", f"garage_session2={token}",
            f"GARAGE_SESSION={token}",
            f"__Host-garage_session={token}",  # the secure name is not the plain one
        ]
        for header in ignored:
            with self.subTest(header=repr(header)[:40]):
                self.assertIsNone(read_cookie(header, "garage_session"))
        self.assertIsNone(read_cookie(f"garage_session={token}", "__Host-garage_session"))

    def test_ip_key_groups_ipv6_by_prefix_and_unwraps_mapped_ipv4(self):
        self.assertEqual(ip_key("192.0.2.1"), "192.0.2.1")
        self.assertEqual(ip_key(" 192.0.2.1 "), "192.0.2.1")
        self.assertEqual(ip_key("::ffff:192.0.2.7"), "192.0.2.7")
        # One customer line is a /64: every address in it shares a key.
        self.assertEqual(ip_key("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(ip_key("2001:db8:1:2::9"), ip_key("2001:db8:1:2:ffff:ffff:ffff:ffff"))
        self.assertNotEqual(ip_key("2001:db8:1:2::9"), ip_key("2001:db8:1:3::9"))
        self.assertNotEqual(ip_key("192.0.2.1"), ip_key("192.0.2.2"))
        self.assertEqual(ip_key("fe80::1%eth0"), "fe80::/64")
        for unusable in (None, "", "garbage", "192.0.2.1, 198.51.100.2", "300.1.1.1"):
            self.assertEqual(ip_key(unusable), "unknown")

    def test_public_user_drops_everything_but_the_four_shown_fields(self):
        row = {"user_id": 3, "username": "boss", "display_name": None, "role": "admin",
               "is_active": True, "password_hash": "scrypt$fixture", "last_login_at": None}
        self.assertEqual(public_user(row), {"user_id": 3, "username": "boss",
                                            "display_name": "", "role": "admin"})


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.throttle = LoginThrottle(self.clock)

    def fail(self, username: str = "boss", address: str = ADDRESS, times: int = 1) -> None:
        for _ in range(times):
            self.throttle.failed(username, address)

    def test_five_failures_are_free_then_one_attempt_per_spacing(self):
        for _ in range(5):
            self.assertFalse(self.throttle.blocked("boss", ADDRESS))
            self.fail()
        self.assertTrue(self.throttle.blocked("boss", ADDRESS))
        self.clock.advance(29.9)
        self.assertTrue(self.throttle.blocked("boss", ADDRESS))
        self.clock.advance(0.2)
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))  # one more try is due
        self.fail()
        self.assertTrue(self.throttle.blocked("boss", ADDRESS))  # and then the wait again
        self.clock.advance(30.1)
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))

    def test_one_slowed_pair_does_not_slow_the_account_or_the_address(self):
        self.fail(times=5)
        self.assertTrue(self.throttle.blocked("boss", ADDRESS))
        self.assertFalse(self.throttle.blocked("boss", "198.51.100.9"))
        self.assertFalse(self.throttle.blocked("worker", ADDRESS))

    def test_failures_age_out_of_the_window(self):
        self.fail(times=5)
        self.clock.advance(900.1)
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))
        self.fail(times=4)  # a fresh allowance, not strikes six to nine
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))
        self.fail()
        self.assertTrue(self.throttle.blocked("boss", ADDRESS))

    def test_failures_from_other_addresses_never_lock_an_account(self):
        # Anyone can fail a sign-in for a name they know. If that slowed the account
        # itself, a few addresses taking turns could keep its owner out for good.
        for index in range(60):
            self.fail(address=f"198.51.100.{index}")
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))
        self.assertFalse(self.throttle.blocked("boss", "192.0.2.200"))  # never seen before
        self.assertEqual({key[0] for key in self.throttle._failures}, {"pair"})

    def test_wrong_tries_at_one_address_never_keep_its_other_people_out(self):
        # An office or a phone network shares one address. If failures there slowed the
        # address itself, one person could keep everyone else from signing in with two
        # wrong tries a minute. The rate limit, not this throttle, bounds an address.
        for _ in range(3):
            for index in range(40):
                self.fail(username=f"user{index}")
            self.clock.advance(29)
        self.fail(username="user7", times=5)
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))
        self.assertFalse(self.throttle.blocked("never-tried", ADDRESS))
        self.assertTrue(self.throttle.blocked("user7", ADDRESS))  # each pair is still slowed

    def test_success_clears_that_pair_and_no_other(self):
        self.fail(times=5)
        self.fail(username="worker", times=5)
        self.throttle.succeeded("boss", ADDRESS)
        self.assertFalse(self.throttle.blocked("boss", ADDRESS))
        self.assertTrue(self.throttle.blocked("worker", ADDRESS))

    def test_eviction_bounds_the_table_and_never_locks_newcomers_out(self):
        throttle = LoginThrottle(self.clock, maximum=30)
        for index in range(600):
            throttle.failed(f"user{index}", f"198.51.{index // 250}.{index % 250}")
        self.assertLessEqual(len(throttle._failures), 30)
        self.assertFalse(throttle.blocked("boss", ADDRESS))
        self.assertFalse(throttle.blocked("user0", "198.51.0.0"))  # evicted, so forgiven


class CountingScrypt:
    """A fast stand-in for the 150 ms scrypt call that counts how often it runs."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, password: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
        self.calls += 1
        return hashlib.sha256(password.encode() + salt + bytes((n_log2, r, p))).digest()


class Accounts:
    """The SiteRepository methods SiteAuth uses, in memory, recording every call."""

    configured = True

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, int] = {}
        self.audits: list[dict] = []
        self.calls: list[tuple] = []
        self.error: Exception | None = None
        self.audit_error: Exception | None = None

    def _call(self, *call) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def add(self, username: str, role: str = "admin", password: str = PASSWORD,
            active: bool = True) -> dict:
        row = {"user_id": len(self.users) + 1, "username": username, "display_name": "",
               "role": role, "is_active": active, "password_hash": hash_password(password)}
        self.users[username] = row
        return row

    def _by_id(self, user_id: int) -> dict | None:
        return next((row for row in self.users.values() if row["user_id"] == user_id), None)

    def find_user(self, username):
        self._call("find_user", username)
        row = self.users.get(username)
        return dict(row) if row else None

    def user_with_hash(self, user_id):
        self._call("user_with_hash", user_id)
        row = self._by_id(user_id)
        return dict(row) if row else None

    def set_password(self, user_id, password_hash, keep_session=None):
        self._call("set_password", user_id)
        self._by_id(user_id)["password_hash"] = password_hash
        self.sessions = {digest: owner for digest, owner in self.sessions.items()
                         if owner != user_id or digest == keep_session}
        return True

    def create_session(self, token_hash, user_id, lifetime_seconds):
        self._call("create_session", user_id, lifetime_seconds)
        self.sessions[token_hash] = user_id

    def resolve_session(self, token_hash, idle_seconds):
        self._call("resolve_session", idle_seconds)
        row = self._by_id(self.sessions.get(token_hash, 0))
        return dict(row) if row and row["is_active"] else None

    def delete_session(self, token_hash):
        self._call("delete_session")
        self.sessions.pop(token_hash, None)

    def audit(self, actor, action, target="", detail=None, client_ip=None):
        if self.audit_error is not None:
            raise self.audit_error
        row = {"actor": (actor or {}).get("username"), "action": action,
               "target": target, "client_ip": client_ip}
        if detail:
            row["detail"] = detail
        self.audits.append(row)


class SiteAuthTests(unittest.TestCase):
    def setUp(self):
        self.hashes = CountingScrypt()
        self.clock = Clock()
        for name, value in (("_scrypt", self.hashes), ("_dummy_hash", None)):
            patcher = patch.object(site_auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        site_auth._dummy_hash = hash_password("decoy-password-0")
        self.accounts = Accounts()
        self.boss = self.accounts.add("boss")
        self.worker = self.accounts.add("worker", role="employee")
        self.accounts.add("gone", active=False)
        self.auth = SiteAuth(self.accounts, clock=self.clock)
        self.hashes.calls = 0

    def refused(self, code: str, status: int, call, *args) -> AuthError:
        with self.assertRaises(AuthError) as caught:
            call(*args)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        return caught.exception

    def test_unknown_disabled_and_wrong_password_fail_alike_and_each_pays_one_hash(self):
        attempts = (("nobody", PASSWORD), ("gone", PASSWORD), ("boss", "wrong-password-1"),
                    ("not a name!", PASSWORD))
        messages = set()
        for username, password in attempts:
            with self.subTest(username=username):
                before = self.hashes.calls
                error = self.refused("INVALID_CREDENTIALS", 401, self.auth.login, username,
                                     password, ADDRESS)
                messages.add(str(error))
                self.assertEqual(self.hashes.calls - before, 1)
        self.assertEqual(messages, {"INVALID_CREDENTIALS"})
        self.assertEqual(self.accounts.sessions, {})
        # One audit row for the address, however many attempts it made in the minute.
        self.assertEqual([row["action"] for row in self.accounts.audits],
                         ["auth.login_failed"])
        # A name that cannot be an account is not even looked up.
        self.assertEqual(self.accounts.names().count("find_user"), 3)

    def test_the_first_failed_sign_in_creates_the_decoy_hash(self):
        site_auth._dummy_hash = None
        self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "nobody", PASSWORD, ADDRESS)
        self.assertTrue(site_auth._dummy_hash.startswith("scrypt$"))
        self.assertEqual(self.hashes.calls, 2)  # the decoy itself, then the comparison

    def test_login_issues_a_token_and_stores_only_its_digest(self):
        token, user = self.auth.login("  Boss ", PASSWORD, ADDRESS)
        self.assertTrue(TOKEN.fullmatch(token))
        self.assertEqual(user, {"user_id": self.boss["user_id"], "username": "boss",
                                "display_name": "", "role": "admin"})
        self.assertEqual(self.accounts.sessions, {token_digest(token): self.boss["user_id"]})
        self.assertIn(("create_session", self.boss["user_id"], SESSION_LIFETIME),
                      self.accounts.calls)
        self.assertEqual(self.accounts.audits[-1], {"actor": "boss", "action": "auth.login",
                                                    "target": "", "client_ip": ADDRESS})
        self.assertEqual(self.auth.resolve(token), user)
        other, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        self.assertNotEqual(other, token)  # every sign-in gets its own session

    def test_failed_attempts_are_audited_with_a_bounded_name_and_a_usable_address(self):
        self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "N" * 64, PASSWORD, "unknown")
        self.assertEqual(self.accounts.audits[-1], {
            "actor": None, "action": "auth.login_failed", "target": "n" * 32,
            "client_ip": None})  # "unknown" is not an inet value

    def test_failed_sign_ins_reach_the_audit_log_at_a_bounded_rate(self):
        def fail(address: str) -> None:
            self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "nobody", PASSWORD,
                         address)

        def rows(action: str) -> list[dict]:
            return [row for row in self.accounts.audits if row["action"] == action]

        fail(ADDRESS)
        fail(ADDRESS)
        fail("198.51.100.9")  # another address has its own row
        self.assertEqual([row["client_ip"] for row in self.accounts.audits],
                         [ADDRESS, "198.51.100.9"])
        self.clock.advance(SiteAuth.FAILURE_AUDIT_SECONDS + 0.1)
        fail(ADDRESS)
        # The second try from the first address got no row of its own; it was counted,
        # and the count is written once its minute is over.
        self.assertEqual([row["detail"] for row in rows("auth.login_failed_more")],
                         [{"count": 1}])
        self.assertEqual(len(rows("auth.login_failed")), 3)
        # Guessing from many addresses cannot fill the append-only table either...
        self.clock.advance(120)
        for index in range(200):
            fail(f"198.18.{index // 250}.{index % 250}")
        self.assertEqual(len(rows("auth.login_failed")), 3 + SiteAuth.FAILURE_AUDITS)
        # ...and none of it goes unrecorded: the rest is one row with the number.
        self.clock.advance(60.1)
        self.auth.login("boss", PASSWORD, "192.0.2.77")  # any sign-in writes what is due
        self.assertEqual([row["detail"] for row in rows("auth.login_failed_more")],
                         [{"count": 1}, {"count": 200 - SiteAuth.FAILURE_AUDITS}])
        self.assertIsNone(rows("auth.login_failed_more")[-1]["client_ip"])
        self.assertLessEqual(SiteAuth.FAILURE_AUDITS, 10)

    def test_a_flood_of_sign_ins_is_turned_away_instead_of_queueing(self):
        def free_slots() -> int:
            taken = 0
            while site_auth._SIGNING_IN.acquire(blocking=False):
                taken += 1
            for _ in range(taken):
                site_auth._SIGNING_IN.release()
            return taken

        slots = free_slots()
        self.assertEqual(slots, 6)
        for _ in range(slots):
            site_auth._SIGNING_IN.acquire()
        try:
            calls, hashes = len(self.accounts.calls), self.hashes.calls
            # No look-up, no hash and no thread left waiting: the answer is immediate.
            self.refused("SITE_AUTH_BUSY", 503, self.auth.login, "boss", PASSWORD, ADDRESS)
            self.assertEqual((len(self.accounts.calls), self.hashes.calls), (calls, hashes))
        finally:
            for _ in range(slots):
                site_auth._SIGNING_IN.release()
        # A failed, a broken and a successful attempt each hand their slot back.
        self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "boss", "wrong-password-1",
                     ADDRESS)
        self.accounts.error = psycopg.OperationalError("connection lost")
        self.refused("SITE_AUTH_UNAVAILABLE", 503, self.auth.login, "boss", PASSWORD, ADDRESS)
        self.accounts.error = None
        self.assertTrue(self.auth.login("boss", PASSWORD, ADDRESS)[0])
        self.assertEqual(free_slots(), 6)

    def test_repeated_failures_are_refused_before_any_lookup_or_hash(self):
        for _ in range(5):
            self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "boss",
                         "wrong-password-1", ADDRESS)
        calls, hashes = len(self.accounts.calls), self.hashes.calls
        # Even the right password waits; nothing is looked up or hashed meanwhile.
        self.refused("TOO_MANY_ATTEMPTS", 429, self.auth.login, "boss", PASSWORD, ADDRESS)
        self.assertEqual((len(self.accounts.calls), self.hashes.calls), (calls, hashes))
        # Never a hard lockout: another address is served, and so is this one after a wait.
        self.assertTrue(self.auth.login("boss", PASSWORD, "198.51.100.9")[0])
        self.clock.advance(30.1)
        self.assertTrue(self.auth.login("boss", PASSWORD, ADDRESS)[0])

    def test_a_successful_sign_in_restores_the_pair_allowance(self):
        for _ in range(4):
            self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "boss",
                         "wrong-password-1", ADDRESS)
        self.auth.login("boss", PASSWORD, ADDRESS)
        for _ in range(5):  # Without the reset the second of these would be throttled.
            self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "boss",
                         "wrong-password-1", ADDRESS)
        self.refused("TOO_MANY_ATTEMPTS", 429, self.auth.login, "boss", PASSWORD, ADDRESS)

    def test_database_failure_and_missing_configuration_are_distinct_and_safe(self):
        for error in (psycopg.OperationalError("password=private-fixture"),
                      SiteDatabaseNotConfigured("no url")):
            self.accounts.error = error
            refused = self.refused("SITE_AUTH_UNAVAILABLE", 503, self.auth.login, "boss",
                                   PASSWORD, ADDRESS)
            self.assertNotIn("private-fixture", str(refused))
            self.assertIsNone(refused.__cause__)
        self.accounts.error = None
        self.accounts.configured = False
        self.accounts.calls.clear()
        self.refused("SITE_AUTH_NOT_CONFIGURED", 503, self.auth.login, "boss", PASSWORD,
                     ADDRESS)
        self.assertEqual(self.accounts.calls, [])
        self.assertIsNone(self.auth.resolve(new_token()))
        self.assertEqual(self.accounts.calls, [])

    def test_an_audit_failure_does_not_block_sign_in(self):
        self.accounts.audit_error = RuntimeError("audit table unavailable")
        with self.assertLogs("automotive.site_auth", "WARNING"):
            token, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        self.assertIn(token_digest(token), self.accounts.sessions)

    def test_resolve_is_served_from_the_cache_until_it_expires(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.accounts.calls.clear()
        # Signing in already made the session known, so the first requests ask nothing.
        self.assertEqual(self.auth.resolve(token), user)
        self.assertEqual(self.auth.resolve(token), user)
        self.assertEqual(self.accounts.calls, [])
        self.clock.advance(SiteAuth.CACHE_SECONDS + 0.1)
        self.assertEqual(self.auth.resolve(token), user)
        self.assertEqual(self.auth.resolve(token), user)
        self.assertEqual(self.accounts.calls, [("resolve_session", SESSION_IDLE)])

    def test_resolve_hands_out_copies_so_a_caller_cannot_poison_the_cache(self):
        token, _ = self.auth.login("worker", PASSWORD, ADDRESS)
        self.auth.resolve(token)["role"] = "admin"
        self.auth.resolve(token)["role"] = "admin"
        self.assertEqual(self.auth.resolve(token)["role"], "employee")

    def test_malformed_tokens_never_reach_the_repository(self):
        good = new_token()
        for token in (None, "", "short", good + "x", good[:-1], good[:-1] + "+",
                      good[:-1] + "\n", token_digest(good)):
            self.assertIsNone(self.auth.resolve(token))
            self.auth.logout(token)
        self.assertEqual(self.accounts.calls, [])

    def test_an_unknown_token_is_remembered_as_absent(self):
        token = new_token()
        self.assertIsNone(self.auth.resolve(token))
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(self.accounts.names(), ["resolve_session"])

    def test_a_database_blip_does_not_sign_a_known_session_out(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.clock.advance(SiteAuth.CACHE_SECONDS + 0.1)
        self.accounts.error = psycopg.OperationalError("connection lost")
        self.accounts.calls.clear()
        self.assertEqual(self.auth.resolve(token), user)  # valid a moment ago: still served
        self.assertEqual(self.auth.resolve(token), user)
        # The database is not asked again on every request while it is down.
        self.assertEqual(self.accounts.names(), ["resolve_session"])
        self.assertEqual(self.auth.cached(token), ("fresh", user))
        self.clock.advance(SiteAuth.CACHE_SECONDS + 0.1)
        self.assertEqual(self.auth.cached(token), ("known", None))
        # Back up, and the session has been ended meanwhile: that is found at once.
        self.accounts.error = None
        self.accounts.sessions.clear()
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(self.auth.cached(token), ("fresh", None))

    def test_an_outage_carries_a_session_only_so_far(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.accounts.error = psycopg.OperationalError("connection lost")
        self.clock.advance(SiteAuth.KNOWN_SECONDS - 1)
        self.assertEqual(self.auth.resolve(token), user)
        self.clock.advance(SiteAuth.CACHE_SECONDS + 1)  # past the last time it was confirmed
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(self.auth.cached(token), ("unknown", None))
        self.assertLessEqual(SiteAuth.KNOWN_SECONDS, 900)

    def test_a_database_error_for_a_token_never_seen_valid_is_nobody_and_not_cached(self):
        token = new_token()
        self.accounts.sessions[token_digest(token)] = self.boss["user_id"]
        self.accounts.error = psycopg.OperationalError("connection lost")
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(self.auth.cached(token), ("unknown", None))
        self.accounts.error = None
        self.assertEqual(self.auth.resolve(token)["username"], "boss")  # at once

    def test_cached_tells_an_answer_from_a_known_and_an_unknown_token(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        elsewhere, unknown = new_token(), new_token()
        self.accounts.sessions[token_digest(elsewhere)] = self.worker["user_id"]
        self.accounts.calls.clear()
        self.assertEqual(self.auth.cached(token), ("fresh", user))  # known since sign-in
        # A session this process has not seen (made before a restart, say) is unknown
        # until it has been looked up once, like one that does not exist.
        self.assertEqual(self.auth.cached(elsewhere), ("unknown", None))
        self.assertEqual(self.auth.cached(unknown), ("unknown", None))
        self.assertEqual(self.accounts.calls, [])  # asking costs nothing
        self.auth.resolve(elsewhere)
        self.auth.resolve(unknown)
        self.assertEqual(self.auth.cached(elsewhere)[0], "fresh")
        self.assertEqual(self.auth.cached(unknown), ("fresh", None))  # remembered as absent
        self.auth.cached(token)[1]["role"] = "nobody"  # a copy, as resolve gives
        self.assertEqual(self.auth.cached(token), ("fresh", user))
        for nothing_to_look_up in (None, "", "short", unknown + "x"):
            self.assertEqual(self.auth.cached(nothing_to_look_up), ("fresh", None))
        self.clock.advance(SiteAuth.CACHE_SECONDS + 0.1)
        self.assertEqual(self.auth.cached(token), ("known", None))  # due a re-check
        self.assertEqual(self.auth.cached(elsewhere), ("known", None))
        self.assertEqual(self.auth.cached(unknown), ("unknown", None))
        self.clock.advance(SiteAuth.KNOWN_SECONDS)
        self.assertEqual(self.auth.cached(token), ("unknown", None))
        self.accounts.configured = False
        self.assertEqual(self.auth.cached(token), ("fresh", None))

    def test_invented_tokens_cannot_push_real_sessions_out_of_the_cache(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        for _ in range(2500):
            self.auth.resolve(new_token())
        self.assertEqual(self.auth.cached(token), ("fresh", user))
        self.assertLessEqual(len(self.auth._absent), 2000)

    def test_a_changed_password_makes_the_new_session_known_at_once(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        fresh = self.auth.change_password(user, token, PASSWORD, NEW_PASSWORD, ADDRESS)
        self.assertEqual(self.auth.cached(fresh), ("fresh", user))
        self.assertEqual(self.auth.cached(token)[0], "unknown")  # the old one is forgotten

    def test_a_session_ended_by_another_process_stops_working_within_seconds(self):
        # The command line and a second worker cannot clear this process's cache, so the
        # cache lifetime is how long a session they ended can still be used here.
        self.assertLessEqual(SiteAuth.CACHE_SECONDS, 5.0)
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.assertEqual(self.auth.resolve(token), user)
        self.accounts.sessions.clear()
        self.assertEqual(self.auth.resolve(token), user)  # still cached
        self.clock.advance(SiteAuth.CACHE_SECONDS + 0.1)
        self.assertIsNone(self.auth.resolve(token))

    def test_a_disabled_account_stops_resolving_once_the_cache_lets_go(self):
        token, user = self.auth.login("worker", PASSWORD, ADDRESS)
        self.assertEqual(self.auth.resolve(token), user)
        self.accounts.users["worker"]["is_active"] = False
        self.auth.forget(user_id=user["user_id"])
        self.assertIsNone(self.auth.resolve(token))

    def test_forget_drops_cached_sessions_by_digest_and_by_account(self):
        first, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        second, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        other, _ = self.auth.login("worker", PASSWORD, ADDRESS)
        for token in (first, second, other):
            self.assertIsNotNone(self.auth.resolve(token))
        self.accounts.sessions.clear()  # The database no longer knows any of them.
        self.assertIsNotNone(self.auth.resolve(first))  # still cached
        self.auth.forget(digest=token_digest(first))
        self.assertIsNone(self.auth.resolve(first))
        self.assertIsNotNone(self.auth.resolve(second))  # the account's other session
        self.auth.forget(user_id=self.boss["user_id"])
        self.assertIsNone(self.auth.resolve(second))
        self.assertIsNotNone(self.auth.resolve(other))  # another account is untouched

    def test_logout_ends_the_session_at_once(self):
        token, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        kept, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        self.assertIsNotNone(self.auth.resolve(token))
        self.auth.logout(token)
        self.assertEqual(list(self.accounts.sessions), [token_digest(kept)])
        self.assertIsNone(self.auth.resolve(token))
        self.assertIsNotNone(self.auth.resolve(kept))

    def test_logout_survives_a_database_error(self):
        token, _ = self.auth.login("boss", PASSWORD, ADDRESS)
        self.accounts.error = psycopg.OperationalError("connection lost")
        with self.assertLogs("automotive.site_auth", "WARNING"):
            self.auth.logout(token)

    def test_change_password_rotates_the_session_and_ends_the_others(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        elsewhere, _ = self.auth.login("boss", PASSWORD, "198.51.100.9")
        bystander, _ = self.auth.login("worker", PASSWORD, ADDRESS)
        for session in (token, elsewhere, bystander):
            self.assertIsNotNone(self.auth.resolve(session))  # all three are cached now
        fresh = self.auth.change_password(user, token, PASSWORD, NEW_PASSWORD, ADDRESS)
        self.assertTrue(TOKEN.fullmatch(fresh))
        self.assertNotIn(fresh, (token, elsewhere))
        self.assertEqual(self.accounts.sessions, {
            token_digest(fresh): user["user_id"],
            token_digest(bystander): self.worker["user_id"]})
        self.assertIsNone(self.auth.resolve(token))
        self.assertIsNone(self.auth.resolve(elsewhere))
        self.assertEqual(self.auth.resolve(fresh), user)
        self.assertIsNotNone(self.auth.resolve(bystander))
        saved = self.accounts.users["boss"]["password_hash"]
        self.assertTrue(verify_password(NEW_PASSWORD, saved))
        self.assertFalse(verify_password(PASSWORD, saved))
        self.assertNotIn(NEW_PASSWORD, saved)
        self.assertEqual(self.accounts.audits[-1]["action"], "auth.password_changed")
        self.refused("INVALID_CREDENTIALS", 401, self.auth.login, "boss", PASSWORD, ADDRESS)
        self.assertTrue(self.auth.login("boss", NEW_PASSWORD, ADDRESS)[0])

    def test_change_password_needs_the_current_password_and_is_throttled(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        before = self.accounts.users["boss"]["password_hash"]
        for _ in range(5):
            self.refused("INVALID_CREDENTIALS", 401, self.auth.change_password, user, token,
                         "wrong-password-1", NEW_PASSWORD, ADDRESS)
        self.assertEqual(self.accounts.users["boss"]["password_hash"], before)
        self.assertEqual(list(self.accounts.sessions), [token_digest(token)])
        self.refused("TOO_MANY_ATTEMPTS", 429, self.auth.change_password, user, token,
                     PASSWORD, NEW_PASSWORD, ADDRESS)
        self.assertEqual(self.accounts.users["boss"]["password_hash"], before)

    def test_change_password_checks_the_new_password_before_touching_anything(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.accounts.calls.clear()
        hashes = self.hashes.calls
        for new, code in (("short", "TOO_SHORT"), ("abcd" * 32 + "e", "TOO_LONG"),
                          ("aaaaaaaaaaaa", "TOO_SIMPLE")):
            self.refused(code, 422, self.auth.change_password, user, token, PASSWORD, new,
                         ADDRESS)
        named = {**user, "username": "administrator"}
        self.refused("SAME_AS_USERNAME", 422, self.auth.change_password, named, token,
                     PASSWORD, "Administrator", ADDRESS)
        self.assertEqual((self.accounts.calls, self.hashes.calls), ([], hashes))

    def test_change_password_reports_a_database_failure_without_details(self):
        token, user = self.auth.login("boss", PASSWORD, ADDRESS)
        self.accounts.error = psycopg.OperationalError("password=private-fixture")
        refused = self.refused("SITE_AUTH_UNAVAILABLE", 503, self.auth.change_password, user,
                               token, PASSWORD, NEW_PASSWORD, ADDRESS)
        self.assertNotIn("private-fixture", str(refused))

    def test_cookie_name_follows_the_secure_setting(self):
        self.assertEqual(self.auth.cookie, "garage_session")
        self.assertEqual(SiteAuth(self.accounts, secure_cookies=True).cookie,
                         "__Host-garage_session")


if __name__ == "__main__":
    unittest.main()
