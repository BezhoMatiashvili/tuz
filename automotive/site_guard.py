"""Outermost middleware for the garage site.

It runs before the legacy ``protections`` middleware in ``automotive.api`` and decides,
per path, who may pass: the public, a signed-in member of staff, or only a direct
loopback caller. It owns rate limits and body caps for the new ``/api/site``,
``/api/auth`` and ``/api/admin`` namespaces, so anonymous traffic never reaches the
legacy limiter, and it applies the cache policy on the way out.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import quote

import anyio
from starlette.datastructures import Headers, MutableHeaders

from automotive.site_auth import csrf_token, ip_key, read_cookie

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
       "object-src 'none'")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CACHE = {"immutable": "public, max-age=31536000, immutable", "revalidate": "no-cache",
         "short": "public, max-age=300", "hour": "public, max-age=3600"}
# category: (burst, refill per minute), keyed by client address (IPv6 by /64).
LIMITS = {
    "page": (60, 120), "asset": (600, 2400), "site_api": (30, 60), "vin": (4, 6),
    "chat_create": (4, 6), "chat_message": (4, 6), "chat_poll": (120, 300),
    "login": (5, 5), "auth": (20, 30), "admin": (120, 300),
    # Signed-in staff tools poll chat turns; the legacy limiter still applies inside.
    "staff": (300, 1200),
    # Session cookies that turned out to belong to nobody, per address and for the whole
    # site. A cookie that resolves to an account costs nothing here.
    "session": (30, 120),
    "session_all": (200, 600),
}
# Look-ups of cookies never seen valid that may run at once. They share the worker threads
# with every page, so a slow database must not let them take those.
LOOKUPS = 4


class _ClientGone(Exception):
    """The client went away before its request body had arrived."""


class TokenBuckets:
    """Bounded per-key token buckets. When full, the oldest key is evicted: a flood of new
    addresses can never turn the limiter into a site-wide denial."""

    def __init__(self, maximum: int = 20_000, clock: Any = time.monotonic) -> None:
        self.maximum, self.clock = maximum, clock
        self._buckets: OrderedDict[tuple, tuple[float, float]] = OrderedDict()
        self._lock = Lock()

    def has(self, key: tuple, burst: float, per_minute: float) -> bool:
        """Whether ``allow`` would pass, without taking the token."""
        now = self.clock()
        with self._lock:
            tokens, stamp = self._buckets.get(key, (burst, now))
            return min(burst, tokens + (now - stamp) * per_minute / 60.0) >= 1.0

    def allow(self, key: tuple, burst: float, per_minute: float) -> bool:
        now = self.clock()
        with self._lock:
            tokens, stamp = self._buckets.get(key, (burst, now))
            tokens = min(burst, tokens + (now - stamp) * per_minute / 60.0)
            allowed = tokens >= 1.0
            self._buckets[key] = (tokens - 1.0 if allowed else tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.maximum:
                self._buckets.popitem(last=False)
            return allowed


class DailyBudget:
    """A whole-site allowance per UTC day, for calls that cost money or a provider's goodwill."""

    def __init__(self, limit: int, clock: Any = time.time) -> None:
        self.limit, self.clock = max(0, int(limit)), clock
        self._day, self._used = -1, 0
        self._lock = Lock()

    def take(self, amount: int = 1) -> bool:
        with self._lock:
            day = int(self.clock() // 86400)
            if day != self._day:
                self._day, self._used = day, 0
            if self._used + amount > self.limit:
                return False
            self._used += amount
            return True

    def refund(self, amount: int = 1) -> None:
        """Give back what ``take`` charged for something that then did not happen."""
        with self._lock:
            if int(self.clock() // 86400) == self._day:
                self._used = max(0, self._used - amount)

    @property
    def used(self) -> int:
        return self._used if int(self.clock() // 86400) == self._day else 0


@dataclass(frozen=True, slots=True)
class Rule:
    category: str = "page"
    session: str = "none"  # none | optional | required
    guarded: bool = False  # limits and body cap applied here; legacy limiter skips it
    csrf: bool = False  # unsafe methods need the X-CSRF-Token header
    origin: bool = False  # unsafe methods need an allow-listed Origin
    body_cap: int = 0
    login: str = ""  # required session missing: redirect to /login?next=<this>
    loopback: bool = False  # direct local callers only
    blocked: bool = False
    noindex: bool = True


def rule_for(path: str, method: str, staff_auth: bool) -> Rule:
    """The access rule for a request. Order matters: most specific prefixes first."""
    if path.startswith("/api/site/"):
        category = "site_api"
        if path.startswith("/api/site/chat/"):
            if method == "GET" and "/turns/" in path:
                category = "chat_poll"
            elif method == "POST":
                category = "chat_message" if path.endswith("/messages") else "chat_create"
        elif path == "/api/site/vin":
            category = "vin"
        return Rule(category, guarded=True, body_cap=16_384)
    if path == "/api/auth/login":
        return Rule("login", "optional", guarded=True, origin=True, body_cap=4_096)
    if path.startswith("/api/auth/"):
        return Rule("auth", "optional", guarded=True, csrf=True, origin=True, body_cap=4_096)
    if path.startswith("/api/admin/"):
        cap = 1_700_000 if path.startswith("/api/admin/media") else 262_144
        return Rule("admin", "required", guarded=True, csrf=True, origin=True, body_cap=cap)
    if path == "/admin" or path.startswith("/admin/"):
        return Rule("page", "required", login="admin")
    if path == "/staff":
        return Rule("page", "required" if staff_auth else "none", login="staff")
    if path.startswith("/api/v1/operator/"):
        return Rule("page", loopback=True)
    if path == "/api/v1/health":
        # Open to everyone, so with the staff login on it is limited here and kept away
        # from the legacy limiter, which only signed-in staff may then reach.
        return Rule("page", guarded=staff_auth)
    if path.startswith("/api/v1/") or path in ("/openapi.json", "/docs", "/redoc"):
        return Rule("staff", "required" if staff_auth else "none", origin=staff_auth)
    if path.startswith(("/static/site/", "/static/admin/")):
        return Rule(blocked=True)  # Served only under /assets/<build>/, one URL per file.
    if path.startswith("/static/"):
        return Rule("asset", "required" if staff_auth else "none")
    if path.startswith(("/assets/", "/media/")):
        return Rule("asset", noindex=False)
    if path == "/api" or path.startswith("/api/"):
        # No such namespace. Answered here: the legacy limiter behind this guard denies
        # everyone once it is full, so anonymous traffic is never allowed to reach it.
        return Rule(blocked=True)
    return Rule("page", noindex=path == "/login")


def _aliased(path: str) -> bool:
    """True for ``//``, ``/./``, ``/../`` and a backslash. No address this site hands out
    contains them, and a static mount could normalise them into a second name for a file,
    one that the prefix rules above have not judged."""
    return ("//" in path or "\\" in path
            or any(part in (".", "..") for part in path.split("/")))


def _trusted(address: str, proxies: tuple[str, ...]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for proxy in proxies:
        try:
            if parsed in ipaddress.ip_network(proxy, strict=False):
                return True
        except ValueError:
            continue
    return False


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


class SiteGuard:
    """Pure ASGI middleware; see the module docstring."""

    def __init__(self, app: Any, *, auth: Any, settings: Any,
                 buckets: TokenBuckets | None = None) -> None:
        self.app, self.auth, self.settings = app, auth, settings
        self.buckets = buckets or TokenBuckets()
        self._looking = 0  # look-ups of unknown cookies in flight; event-loop only
        self.proxies = tuple(getattr(settings, "trusted_proxies", ()) or ())
        self.staff_auth = bool(getattr(settings, "staff_auth", False))
        self.origins = set(getattr(settings, "allowed_origins", ()) or ())

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        peer = scope["client"][0] if scope.get("client") else ""
        proxied = _trusted(peer, self.proxies)
        # Every X-Forwarded-For line counts, in order: a proxy that appends its own line
        # leaves whatever the client sent as the first one.
        forwarded = ",".join(headers.getlist("x-forwarded-for"))
        if proxied and forwarded:
            # The right-most hop that is not one of our proxies is the client; anything
            # further left was supplied by that client and is ignored.
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            client = next((hop for hop in reversed(hops)
                           if not _trusted(hop, self.proxies)), peer)
            scope["client"] = (client, 0)
            if headers.get("x-forwarded-proto") in ("http", "https"):
                scope["scheme"] = headers["x-forwarded-proto"]
        client = scope["client"][0] if scope.get("client") else ""
        path, method = scope["path"], scope["method"]
        rule = rule_for(path, method, self.staff_auth)
        secure = scope.get("scheme") == "https"

        async def reply(status: int, code: str, extra: dict | None = None) -> None:
            body = json.dumps({"detail": {"code": code}}).encode()
            await self._early(send, status, body, "application/json", secure, extra)

        if (rule.blocked or _aliased(path)
                or (rule.loopback and (proxied or forwarded or not _loopback(peer)))):
            await reply(404, "NOT_FOUND")
            return
        # Throttle by address before any session lookup, so guessed cookies cannot be used to
        # make the database work.
        burst, refill = LIMITS[rule.category]
        if not self.buckets.allow((rule.category, ip_key(client)), burst, refill):
            await reply(429, "RATE_LIMITED", {"retry-after": "10"})
            return
        state = scope.setdefault("state", {})
        token = user = None
        if rule.session != "none":
            token = read_cookie(headers.get("cookie"), self.auth.cookie)
            seen, user = self.auth.cached(token)
            if seen == "known":
                # Valid a moment ago: re-checked freely, whatever else this address sends.
                user = await anyio.to_thread.run_sync(self.auth.resolve, token)
            elif seen == "unknown":
                # Never seen valid, so it costs a database query. Wrong ones are rationed
                # per address and in all, and only a few look-ups run at once: invented
                # cookies can load neither the database nor the worker threads.
                mine, everyone = ("session", ip_key(client)), ("session_all",)
                if (self._looking >= LOOKUPS
                        or not self.buckets.has(mine, *LIMITS["session"])
                        or not self.buckets.has(everyone, *LIMITS["session_all"])):
                    if rule.session == "required" and not (rule.login
                                                           and method in ("GET", "HEAD")):
                        await reply(429, "RATE_LIMITED", {"retry-after": "10"})
                        return
                    # A page goes to the sign-in form below, which always works; a route
                    # where the session is optional carries on without one.
                    token = None
                else:
                    self._looking += 1
                    try:
                        user = await anyio.to_thread.run_sync(self.auth.resolve, token)
                    finally:
                        self._looking -= 1
                    if user is None:
                        self.buckets.allow(mine, *LIMITS["session"])
                        self.buckets.allow(everyone, *LIMITS["session_all"])
            state["site_user"], state["site_token"] = user, token if user else None
            if rule.session == "required" and not user:
                if rule.login and method in ("GET", "HEAD"):
                    await self._early(send, 303, b"", "text/plain", secure,
                                      {"location": "/login?next=" + quote(rule.login)})
                else:
                    await reply(401, "NOT_SIGNED_IN")
                return
        if method in UNSAFE and rule.origin:
            # A cookie-authenticated write must name its origin; "no Origin header" is how a
            # cross-site form or a stripped request looks, so it is refused, not waved through.
            if headers.get("origin") not in self.origins:
                await reply(403, "ORIGIN_REQUIRED")
                return
        if method in UNSAFE and rule.csrf and (user or rule.session == "required"):
            # Compared as bytes: compare_digest raises on non-ASCII text, and a header may
            # carry any byte.
            supplied = (headers.get("x-csrf-token") or "").encode()
            if not token or not hmac.compare_digest(supplied, csrf_token(token).encode()):
                await reply(403, "CSRF_REQUIRED")
                return
        if rule.guarded:
            state["site_guarded"] = True
            if method in UNSAFE:
                try:
                    body = await self._body(receive, headers, rule.body_cap)
                except _ClientGone:
                    return  # Nobody is left to answer, and half a body is not a request.
                if body is None:
                    await reply(413, "REQUEST_TOO_LARGE")
                    return
                receive = self._replay(body)

        async def finish(message: dict) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                policy = CACHE.get(out.get("x-site-cache", ""))
                if "x-site-cache" in out:
                    del out["x-site-cache"]
                if policy and message["status"] == 200 and method in ("GET", "HEAD"):
                    out["cache-control"] = policy
                self._harden(out, secure, rule.noindex)
            await send(message)

        await self.app(scope, receive, finish)

    @staticmethod
    def _harden(out: MutableHeaders, secure: bool, noindex: bool) -> None:
        out.setdefault("content-security-policy", CSP)
        out.setdefault("x-content-type-options", "nosniff")
        out.setdefault("referrer-policy", "no-referrer")
        out.setdefault("cache-control", "no-store")
        out["permissions-policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        out["cross-origin-opener-policy"] = "same-origin"
        if secure:
            out["strict-transport-security"] = "max-age=31536000"
        if noindex:
            out["x-robots-tag"] = "noindex, nofollow"

    async def _early(self, send: Any, status: int, body: bytes, content_type: str,
                     secure: bool, extra: dict | None = None) -> None:
        """A response produced here never passes ``protections``, so it sets every header."""
        out = MutableHeaders()
        out["content-type"] = content_type
        out["content-length"] = str(len(body))
        for name, value in (extra or {}).items():
            out[name] = value
        self._harden(out, secure, True)
        await send({"type": "http.response.start", "status": status, "headers": out.raw})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _body(receive: Any, headers: Headers, cap: int) -> bytes | None:
        declared = headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > cap):
            return None
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                raise _ClientGone
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > cap:
                return None
            chunks.append(chunk)
            if not message.get("more_body"):
                return b"".join(chunks)

    @staticmethod
    def _replay(body: bytes) -> Any:
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if sent:
                # Mirrors a finished request stream; a disconnect is not simulated.
                await anyio.sleep_forever()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive
