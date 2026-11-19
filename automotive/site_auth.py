"""Staff sign-in for the garage site: scrypt password hashes and opaque browser sessions.

Nothing here trusts the browser: the cookie carries a random token, the database stores
only its SHA-256, and the CSRF header value is derived from the token, so there is no
server-side signing secret to manage or leak.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections import OrderedDict, deque
from threading import BoundedSemaphore, Lock
from typing import Any

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from automotive.site_repository import SiteDatabaseNotConfigured

LOGGER = logging.getLogger(__name__)
ROLES = ("admin", "employee")
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
SESSION_LIFETIME = 7 * 24 * 3600  # absolute, seconds
SESSION_IDLE = 12 * 3600  # sliding, seconds
PASSWORD_MIN, PASSWORD_MAX = 10, 128
# OWASP's scrypt profile N=2^15, r=8, p=3: about 32 MiB and 0.2 s per hash on this host.
SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P, SCRYPT_MAXMEM = 15, 8, 3, 64 * 1024 * 1024
# Two concurrent hashes bound login memory to roughly 64 MiB however many requests arrive.
_HASHING = BoundedSemaphore(2)
# Sign-in attempts that may hold or wait for a hashing slot. A flood beyond this is turned
# away at once instead of parking the worker threads the public pages also run on.
_SIGNING_IN = BoundedSemaphore(6)
_dummy_hash: str | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    with _HASHING:
        # maxmem is mandatory: OpenSSL's 32 MiB default rejects this profile outright.
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << n_log2, r=r, p=p,
                              maxmem=SCRYPT_MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P)
    return "$".join(("scrypt", str(SCRYPT_N_LOG2), str(SCRYPT_R), str(SCRYPT_P),
                     _b64(salt), _b64(digest)))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check; a malformed or out-of-range stored value never verifies."""
    try:
        scheme, n_log2, r, p, salt, expected = encoded.split("$")
        cost = (int(n_log2), int(r), int(p))
        salt_bytes, expected_bytes = _unb64(salt), _unb64(expected)
    except (ValueError, TypeError, AttributeError):
        return False
    if (scheme != "scrypt" or not 14 <= cost[0] <= 17 or cost[1] != 8 or not 1 <= cost[2] <= 5
            or len(salt_bytes) < 8 or len(expected_bytes) != 32
            or not isinstance(password, str) or len(password) > 4 * PASSWORD_MAX):
        return False
    try:
        digest = _scrypt(password, salt_bytes, *cost)
    except ValueError:  # A cost SCRYPT_MAXMEM cannot serve (N=2^16 up), or unencodable text.
        return False
    return hmac.compare_digest(digest, expected_bytes)


def password_problem(password: str, username: str = "") -> str | None:
    """A field code when the password is unacceptable, else None. Length over complexity."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        return "TOO_SHORT"
    if len(password) > PASSWORD_MAX:
        return "TOO_LONG"
    if username and password.casefold() == username.casefold():
        return "SAME_AS_USERNAME"
    if len(set(password)) < 4:
        return "TOO_SIMPLE"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_token(token: str) -> str:
    """Synchroniser value derived from the session token; never stored."""
    return hmac.new(token.encode("ascii"), b"garage-csrf-v1", hashlib.sha256).hexdigest()


def cookie_name(secure: bool) -> str:
    # The __Host- prefix makes browsers refuse a cookie planted by a sibling subdomain.
    return "__Host-garage_session" if secure else "garage_session"


def session_cookie(token: str, secure: bool, max_age: int = SESSION_LIFETIME) -> str:
    return (f"{cookie_name(secure)}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={max_age}" + ("; Secure" if secure else ""))


def read_cookie(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and TOKEN.fullmatch(value):
            return value
    return None


def ip_key(address: str | None) -> str:
    """Throttle key for a client: the address itself, or the /64 for IPv6."""
    try:
        parsed = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return "unknown"
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False).network_address) + "/64"
    return str(parsed)


def wide_key(address: str | None) -> str | None:
    """The /48 an IPv6 client lies in, from its address or its ``ip_key``; None for IPv4,
    IPv4-mapped and unreadable values. One end site is normally given a whole /48, so its
    /64s are one client changing address, not 65,536 clients."""
    try:
        network = ipaddress.ip_network((address or "").strip(), strict=False)
    except ValueError:
        return None
    if (network.version != 6 or network.prefixlen < 48
            or network.network_address.ipv4_mapped is not None):
        return None
    return str(ipaddress.ip_network(f"{network.network_address}/48", strict=False))


def public_user(row: dict) -> dict:
    return {"user_id": row["user_id"], "username": row["username"],
            "display_name": row.get("display_name") or "", "role": row["role"]}


class AuthError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class LoginThrottle:
    """Slows guessing at one account from one address.

    Each key gets ``free`` recent failures at full speed; after that it may try once every
    ``spacing`` seconds until the failures age out of ``window``.

    The only key is the account-and-address pair. A key for the account alone would let
    anyone who knows a username lock its owner out from anywhere, and a key for the address
    alone would let one person on a shared address (an office, a mobile network) keep
    everyone there from signing in with two wrong tries a minute. How many attempts an
    address may make at all is the rate limit's job (``site_guard.LIMITS["login"]``).
    """

    LIMITS = {"pair": (5, 30.0)}

    def __init__(self, clock: Any = time.monotonic, window: float = 900.0,
                 maximum: int = 5000) -> None:
        self.clock, self.window, self.maximum = clock, window, maximum
        self._failures: OrderedDict[tuple, deque[float]] = OrderedDict()
        self._lock = Lock()

    def _keys(self, username: str, address: str) -> list[tuple]:
        return [("pair", username, address)]

    def blocked(self, username: str, address: str) -> bool:
        now = self.clock()
        with self._lock:
            for key in self._keys(username, address):
                recent = self._failures.get(key)
                if not recent:
                    continue
                while recent and recent[0] < now - self.window:
                    recent.popleft()
                free, spacing = self.LIMITS[key[0]]
                if len(recent) >= free and now - recent[-1] < spacing:
                    return True
        return False

    def failed(self, username: str, address: str) -> None:
        now = self.clock()
        with self._lock:
            for key in self._keys(username, address):
                self._failures.setdefault(key, deque(maxlen=40)).append(now)
                self._failures.move_to_end(key)
            while len(self._failures) > self.maximum:
                self._failures.popitem(last=False)  # Evict the oldest; never deny everyone.

    def succeeded(self, username: str, address: str) -> None:
        with self._lock:
            self._failures.pop(("pair", username, address), None)


class SiteAuth:
    """Sessions over SiteRepository, with a short cache so most requests skip the database."""

    # How long an answer is used without asking the database. Also how long a session
    # ended from another process (the command line, a second worker) keeps working here.
    CACHE_SECONDS = 5.0
    # How long a session that was valid stays "known". A known session is re-checked
    # without rationing, and keeps working while the database cannot be reached.
    KNOWN_SECONDS = 600.0
    # Failed sign-ins written to the append-only audit log: one per address in this many
    # seconds and FAILURE_AUDITS a minute in all, plus one row a minute that counts the
    # rest, so guessing can neither grow the table freely nor go unrecorded.
    FAILURE_AUDIT_SECONDS = 60.0
    FAILURE_AUDITS = 10

    def __init__(self, repository: Any, *, secure_cookies: bool = False,
                 clock: Any = time.monotonic) -> None:
        self.repository = repository
        self.secure_cookies = secure_cookies
        self.clock = clock
        self.throttle = LoginThrottle(clock)
        # digest -> (fresh until, known until, account). Sessions that resolved to nobody
        # are kept apart, so a stream of invented cookies cannot push real ones out.
        self._cache: OrderedDict[str, tuple[float, float, dict]] = OrderedDict()
        self._absent: OrderedDict[str, float] = OrderedDict()  # digest -> absent until
        self._audited: OrderedDict[str, float] = OrderedDict()  # address -> last failure row
        self._audit_times: deque[float] = deque()
        self._unaudited, self._summary_at = 0, clock()
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(getattr(self.repository, "configured", False))

    @property
    def cookie(self) -> str:
        return cookie_name(self.secure_cookies)

    # -- cache -------------------------------------------------------------------------

    def _remember(self, digest: str, user: dict | None) -> None:
        now = self.clock()
        with self._lock:
            if user is None:
                self._cache.pop(digest, None)
                table, value = self._absent, now + self.CACHE_SECONDS
            else:
                self._absent.pop(digest, None)
                table = self._cache
                value = (now + self.CACHE_SECONDS, now + self.KNOWN_SECONDS, user)
            table[digest] = value
            table.move_to_end(digest)
            while len(table) > 2000:
                table.popitem(last=False)

    def forget(self, *, digest: str | None = None, user_id: int | None = None) -> None:
        """Drop cached sessions after logout, a password change or an account edit."""
        with self._lock:
            for key in [key for key, (_, _, user) in self._cache.items()
                        if key == digest or (user_id is not None
                                             and user.get("user_id") == user_id)]:
                del self._cache[key]

    def cached(self, token: str | None) -> tuple[str, dict | None]:
        """What is known about a cookie token without asking the database.

        ``("fresh", account or None)`` is an answer. ``("known", None)`` is a session that
        was valid a moment ago and is due a re-check. ``("unknown", None)`` has not been
        seen valid: looking it up costs a query, so the guard rations those.
        """
        if not token or not TOKEN.fullmatch(token) or not self.configured:
            return "fresh", None
        digest, now = token_digest(token), self.clock()
        with self._lock:
            entry = self._cache.get(digest)
            if entry and entry[0] > now:
                return "fresh", dict(entry[2])
            if entry and entry[1] > now:
                return "known", None
            if self._absent.get(digest, 0.0) > now:
                return "fresh", None
        return "unknown", None

    # -- operations --------------------------------------------------------------------

    def resolve(self, token: str | None) -> dict | None:
        """The signed-in account for a cookie token, or None. Never raises."""
        if not token or not TOKEN.fullmatch(token) or not self.configured:
            return None
        digest, now = token_digest(token), self.clock()
        with self._lock:
            entry = self._cache.get(digest)
            if entry and entry[0] > now:
                return dict(entry[2])
            if self._absent.get(digest, 0.0) > now:
                return None
        try:
            row = self.repository.resolve_session(digest, SESSION_IDLE)
        except (psycopg.Error, SiteDatabaseNotConfigured):
            # A database blip must not sign people out: a session that was valid a moment
            # ago keeps working, and is tried again after CACHE_SECONDS, until the database
            # answers or KNOWN_SECONDS have passed since it last did.
            with self._lock:
                entry = self._cache.get(digest)
                if entry and entry[1] > now:
                    self._cache[digest] = (now + self.CACHE_SECONDS, entry[1], entry[2])
                    return dict(entry[2])
            return None
        user = public_user(row) if row else None
        self._remember(digest, user)
        return dict(user) if user else None

    def login(self, username: str, password: str, address: str) -> tuple[str, dict]:
        global _dummy_hash
        if not self.configured:
            raise AuthError("SITE_AUTH_NOT_CONFIGURED", 503)
        name = (username or "").strip().lower()
        if self.throttle.blocked(name, address):
            raise AuthError("TOO_MANY_ATTEMPTS", 429)
        if not _SIGNING_IN.acquire(blocking=False):
            raise AuthError("SITE_AUTH_BUSY", 503)
        try:
            row = self.repository.find_user(name) if USERNAME.fullmatch(name) else None
            if _dummy_hash is None:
                _dummy_hash = hash_password(secrets.token_urlsafe(16))
            usable = bool(row and row["is_active"])
            # Unknown, disabled and wrong-password logins all pay for one hash and look alike.
            verified = verify_password(password, row["password_hash"] if usable
                                       else _dummy_hash)
            if not (usable and verified):
                self.throttle.failed(name, address)
                if self._audit_failure(address):
                    self._audit(None, "auth.login_failed", name[:32], address)
                raise AuthError("INVALID_CREDENTIALS", 401)
            token = new_token()
            self.repository.create_session(token_digest(token), row["user_id"],
                                           SESSION_LIFETIME)
        except (psycopg.Error, SiteDatabaseNotConfigured):
            raise AuthError("SITE_AUTH_UNAVAILABLE", 503) from None
        finally:
            _SIGNING_IN.release()
        self.throttle.succeeded(name, address)
        user = public_user(row)
        # Known from the first request on, so a new session never waits for a rationed
        # look-up.
        self._remember(token_digest(token), user)
        self._audit_summary()
        self._audit(user, "auth.login", "", address)
        return token, user

    def logout(self, token: str | None) -> None:
        if not token or not TOKEN.fullmatch(token):
            return
        digest = token_digest(token)
        self.forget(digest=digest)
        try:
            self.repository.delete_session(digest)
        except (psycopg.Error, SiteDatabaseNotConfigured):
            LOGGER.warning("site_logout_not_persisted")

    def change_password(self, user: dict, token: str, current: str, new: str,
                        address: str) -> str:
        """Verify the current password, store the new one and rotate the session."""
        problem = password_problem(new, user["username"])
        if problem:
            raise AuthError(problem, 422)
        if self.throttle.blocked(user["username"], address):
            raise AuthError("TOO_MANY_ATTEMPTS", 429)
        if not _SIGNING_IN.acquire(blocking=False):  # two hashes: the same queue as sign-in
            raise AuthError("SITE_AUTH_BUSY", 503)
        try:
            row = self.repository.user_with_hash(user["user_id"])
            if not row or not verify_password(current, row["password_hash"]):
                self.throttle.failed(user["username"], address)
                raise AuthError("INVALID_CREDENTIALS", 401)
            self.repository.set_password(user["user_id"], hash_password(new))
            fresh = new_token()
            self.repository.create_session(token_digest(fresh), user["user_id"],
                                           SESSION_LIFETIME)
        except (psycopg.Error, SiteDatabaseNotConfigured):
            raise AuthError("SITE_AUTH_UNAVAILABLE", 503) from None
        finally:
            _SIGNING_IN.release()
        self.forget(user_id=user["user_id"])
        self._remember(token_digest(fresh), public_user(user))
        self._audit(user, "auth.password_changed", "", address)
        return fresh

    def _audit_failure(self, address: str) -> bool:
        """Whether this failed sign-in gets an audit row of its own; see
        FAILURE_AUDIT_SECONDS. The ones that do not are counted, and the count is written
        as one row a minute, so the log shows how much guessing there was."""
        self._audit_summary()
        now = self.clock()
        with self._lock:
            last = self._audited.get(address)
            while self._audit_times and now - self._audit_times[0] >= 60.0:
                self._audit_times.popleft()
            own = ((last is None or now - last >= self.FAILURE_AUDIT_SECONDS)
                   and len(self._audit_times) < self.FAILURE_AUDITS)
            if own:
                self._audit_times.append(now)
                self._audited[address] = now
                self._audited.move_to_end(address)
                while len(self._audited) > 2000:
                    self._audited.popitem(last=False)
            else:
                if not self._unaudited:
                    self._summary_at = now  # the minute being counted starts here
                self._unaudited += 1
        return own

    def _audit_summary(self) -> None:
        """Write the count of failed sign-ins that got no row of their own, once the
        minute they were counted in is over."""
        now = self.clock()
        with self._lock:
            if not self._unaudited or now - self._summary_at < 60.0:
                return
            count, self._unaudited = self._unaudited, 0
        self._audit(None, "auth.login_failed_more", "", "unknown", {"count": count})

    def _audit(self, user: dict | None, action: str, target: str, address: str,
               detail: dict | None = None) -> None:
        try:
            self.repository.audit(user, action, target, detail,
                                  client_ip=address if address != "unknown" else None)
        except Exception:  # Audit is best-effort here; sign-in must not fail on it.
            LOGGER.warning("site_audit_skipped")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=4 * PASSWORD_MAX)
    next: str = Field(default="admin", max_length=16)


class PasswordBody(_Body):
    current: str = Field(min_length=1, max_length=4 * PASSWORD_MAX)
    new: str = Field(min_length=1, max_length=4 * PASSWORD_MAX)


# Where a successful sign-in may land. An enum, never a caller-supplied URL.
LANDINGS = {"admin": "/admin", "staff": "/staff"}


def _client(request: Request) -> str:
    return ip_key(request.client.host if request.client else None)


def _error(exc: AuthError) -> JSONResponse:
    return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)


def build_auth_router(auth: SiteAuth) -> APIRouter:
    """Routes under /api/auth. The guard has already resolved ``request.state.site_user``
    and enforced Origin and the CSRF header on everything except the login itself."""
    router = APIRouter(prefix="/api/auth", include_in_schema=False)

    @router.post("/login")
    def login(payload: LoginBody, request: Request, response: Response) -> Any:
        try:
            token, user = auth.login(payload.username, payload.password, _client(request))
        except AuthError as exc:
            return _error(exc)
        # Any session the browser presented is ended, so a planted cookie gains nothing.
        previous = getattr(request.state, "site_token", None)
        if previous:
            auth.logout(previous)
        response.headers.append("set-cookie", session_cookie(token, auth.secure_cookies))
        return {"user": user, "csrf": csrf_token(token),
                "next": LANDINGS.get(payload.next, LANDINGS["admin"])}

    @router.get("/session")
    def session(request: Request) -> Any:
        user = getattr(request.state, "site_user", None)
        token = getattr(request.state, "site_token", None)
        if not user or not token:
            return JSONResponse({"detail": {"code": "NOT_SIGNED_IN"}}, status_code=401)
        return {"user": user, "csrf": csrf_token(token)}

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        auth.logout(getattr(request.state, "site_token", None))
        response.headers.append("set-cookie", session_cookie("", auth.secure_cookies, 0))

    @router.post("/password")
    def password(payload: PasswordBody, request: Request, response: Response) -> Any:
        user = getattr(request.state, "site_user", None)
        token = getattr(request.state, "site_token", None)
        if not user or not token:
            return JSONResponse({"detail": {"code": "NOT_SIGNED_IN"}}, status_code=401)
        try:
            fresh = auth.change_password(user, token, payload.current, payload.new,
                                         _client(request))
        except AuthError as exc:
            return _error(exc)
        response.headers.append("set-cookie", session_cookie(fresh, auth.secure_cookies))
        return {"csrf": csrf_token(fresh)}

    return router
