"""Polite HTTPS fetcher: robots.txt, per-host pacing, failure classification, breaker.

One worker thread owns one host, so pacing is serial per host. Challenge and captcha
pages stop a source; nothing here tries to get past a site's bot protection.
"""

from __future__ import annotations

import logging
import random
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

import requests

from automotive.public_http import retry_delay
from parts_crawler.records import ascii_url
from parts_crawler.robots import ALLOW_ALL, DISALLOW_ALL, RobotsRules, parse_robots

log = logging.getLogger("parts_crawler.http")

CHALLENGE_MARKERS = (
    b"challenge-platform", b"cf-chl-", b"<title>just a moment", b"attention required! | cloudflare",
    b"g-recaptcha", b"h-captcha", b"cf-turnstile", b"/captcha/", b"captcha-delivery",
)
ROBOTS_TTL_SECONDS = 86400.0


class FetchError(Exception):
    def __init__(self, code: str, *, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


class Disallowed(FetchError):
    """robots.txt or the adapter's host list forbids the URL; never retried."""


class PermanentFetchError(FetchError):
    """404/410 and other client errors that a retry cannot fix."""


class RetryableFetchError(FetchError):
    """Rate limits, server errors and network faults; the queue retries later."""


class ChallengeDetected(FetchError):
    """A bot-protection page; the whole source is blocked until an operator acts."""


class CircuitOpen(FetchError):
    """Too many consecutive host failures; wait `retry_after` seconds."""


class SourcePaused(FetchError):
    """The breaker tripped repeatedly; the source is paused for an operator."""


class Interrupted(FetchError):
    """Shutdown was requested while waiting for the host's next slot."""


@dataclass
class Page:
    url: str
    status: int
    body: bytes
    content_type: str = ""

    @property
    def text(self) -> str:
        charset = "utf-8"
        lowered = self.content_type.lower()
        if "charset=" in lowered:
            charset = lowered.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


@dataclass
class HostState:
    next_at: float = 0.0
    failures: int = 0
    breaker_until: float = 0.0
    trips: int = 0
    robots: RobotsRules | None = None
    robots_at: float = 0.0
    session: requests.Session | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def is_challenge(status: int, headers: dict, body: bytes, *, widget_ok: bool = False) -> bool:
    """True for a bot-protection page.

    `widget_ok` is for sites whose ordinary pages embed a captcha widget (a contact
    form): their error pages would otherwise look like challenges.
    """
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    head = body[:20000].lower()
    if (not widget_ok and status in {403, 429, 503}
            and any(marker in head for marker in CHALLENGE_MARKERS)):
        return True
    return b"<title>just a moment" in head or b"attention required! | cloudflare" in head


def redirect_target(base: str, location: str) -> str:
    """Absolute URL of a redirect, with non-ASCII bytes percent-encoded as sent."""
    if not location.isascii():
        # http.client decodes header bytes as Latin-1; encoding back recovers them.
        location = quote(location.encode("latin-1", "replace"), safe=string.punctuation)
    return ascii_url(urljoin(base, location))


class PoliteFetcher:
    def __init__(self, *, user_agent: str, robots_token: str, min_delay: float = 5.0,
                 jitter: float = 1.5, max_bytes: int = 8_000_000,
                 breaker_threshold: int = 10, breaker_seconds: float = 1800.0,
                 breaker_trips_to_pause: int = 3,
                 session_factory: Callable[[], requests.Session] = requests.Session,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], bool] | None = None) -> None:
        self.user_agent = user_agent
        self.robots_token = robots_token
        self.min_delay = min_delay
        self.jitter = jitter
        self.max_bytes = max_bytes
        self.breaker_threshold = breaker_threshold
        self.breaker_seconds = breaker_seconds
        self.breaker_trips_to_pause = breaker_trips_to_pause
        self.session_factory = session_factory
        self.clock = clock
        self.sleeper = sleeper or (lambda seconds: (time.sleep(seconds), True)[1])
        self._hosts: dict[str, HostState] = {}
        self._hosts_lock = threading.Lock()

    def host(self, host: str) -> HostState:
        with self._hosts_lock:
            return self._hosts.setdefault(host, HostState())

    def _session(self, state: HostState) -> requests.Session:
        if state.session is None:
            state.session = self.session_factory()
            state.session.headers.update({
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "application/json;q=0.8,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            })
        return state.session

    def robots(self, host: str) -> RobotsRules:
        state = self.host(host)
        now = self.clock()
        if state.robots is not None and now - state.robots_at < ROBOTS_TTL_SECONDS:
            if state.robots is DISALLOW_ALL:
                # robots.txt could not be read a moment ago. That is a reason to wait,
                # not to mark every queued URL as disallowed.
                raise CircuitOpen("ROBOTS_UNAVAILABLE",
                                  retry_after=ROBOTS_TTL_SECONDS - (now - state.robots_at))
            return state.robots
        try:
            response = self._session(state).get(
                f"https://{host}/robots.txt", timeout=(5, 20), allow_redirects=True)
            body = response.content[:500_000]
        except (requests.RequestException, UnicodeError, ValueError):
            state.robots, state.robots_at = DISALLOW_ALL, now - ROBOTS_TTL_SECONDS + 300
            raise CircuitOpen("ROBOTS_UNAVAILABLE", retry_after=300.0) from None
        status = response.status_code
        if is_challenge(status, response.headers, body):
            raise ChallengeDetected("CHALLENGE", status=status)
        if status >= 500 or status in {401, 403, 429}:
            # RFC 9309: an unreachable robots.txt means complete disallow for now.
            state.robots, state.robots_at = DISALLOW_ALL, now - ROBOTS_TTL_SECONDS + 600
            raise CircuitOpen("ROBOTS_UNAVAILABLE", status=status, retry_after=600.0)
        if 400 <= status < 500:
            rules = ALLOW_ALL
        else:
            rules = parse_robots(body.decode("utf-8", errors="replace"), self.robots_token)
        state.robots, state.robots_at = rules, now
        return rules

    def _check(self, url: str, allowed_hosts: frozenset[str],
               robots_exempt: tuple[str, ...] = ()) -> tuple[str, str]:
        try:
            parts = urlsplit(url)
        except ValueError:
            raise PermanentFetchError("BAD_URL") from None
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.hostname not in allowed_hosts):
            raise Disallowed("HOST_NOT_ALLOWED")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        # robots.txt is always read and its Crawl-delay always applies. An operator-set
        # exemption lifts the path rules for the listed path prefixes only.
        rules = self.robots(parts.hostname)
        exempt = bool(robots_exempt) and (parts.path or "/").startswith(robots_exempt)
        if not exempt and not rules.allowed(path):
            raise Disallowed("ROBOTS_DISALLOWED")
        return parts.hostname, path

    def _wait_turn(self, state: HostState, delay: float) -> None:
        with state.lock:
            now = self.clock()
            if state.breaker_until > now:
                raise CircuitOpen("CIRCUIT_OPEN", retry_after=state.breaker_until - now)
            wait = state.next_at - now
            if wait > 0 and not self.sleeper(wait):
                raise Interrupted("INTERRUPTED")
            state.next_at = self.clock() + delay + random.uniform(0, self.jitter)  # nosec B311

    def _failure(self, state: HostState, error: FetchError) -> FetchError:
        state.failures += 1
        if state.failures >= self.breaker_threshold:
            state.failures = 0
            state.trips += 1
            state.breaker_until = self.clock() + self.breaker_seconds
            log.warning("circuit opened after repeated failures (trip %s)", state.trips)
            if state.trips >= self.breaker_trips_to_pause:
                return SourcePaused("BREAKER_TRIPPED", status=error.status)
        return error

    def fetch(self, url: str, *, allowed_hosts: frozenset[str], method: str = "GET",
              data: dict | None = None, min_delay: float | None = None,
              max_redirects: int = 3, robots_exempt: tuple[str, ...] = (),
              cookies: dict[str, str] | None = None,
              headers: dict[str, str] | None = None, captcha_widget: bool = False) -> Page:
        if method not in {"GET", "POST"}:
            raise Disallowed("METHOD_NOT_ALLOWED")
        url = ascii_url(url)
        request_headers = headers
        host, _ = self._check(url, allowed_hosts, robots_exempt)
        state = self.host(host)
        rules = state.robots or ALLOW_ALL
        delay = max(self.min_delay, min_delay or 0.0, rules.crawl_delay or 0.0)
        self._wait_turn(state, delay)
        started = self.clock()
        response = None
        current = url
        body = bytearray()
        try:
            for _ in range(max_redirects + 1):
                response = self._session(state).request(
                    method, current, data=data, cookies=cookies, headers=request_headers,
                    timeout=(10, 45), allow_redirects=False, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = redirect_target(current, response.headers.get("Location", ""))
                    response.close()
                    response = None
                    self._check(target, allowed_hosts, robots_exempt)
                    current, method, data = target, "GET", None
                    continue
                break
            else:
                raise PermanentFetchError("TOO_MANY_REDIRECTS")
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    raise PermanentFetchError("TOO_LARGE", status=response.status_code)
            status = response.status_code
            headers = {key.lower(): value for key, value in response.headers.items()}
        except FetchError:
            raise
        except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                requests.exceptions.MissingSchema, requests.exceptions.InvalidHeader):
            raise PermanentFetchError("BAD_URL") from None
        except requests.RequestException as error:
            kind = "TIMEOUT" if isinstance(error, requests.Timeout) else "NETWORK"
            raise self._failure(state, RetryableFetchError(kind, retry_after=60.0)) from None
        except (UnicodeError, ValueError) as error:
            # requests decodes a redirect's Location header even with redirects off; a
            # server that puts raw non-UTF-8 bytes there makes the response unusable.
            log.warning("unusable response from %s: %s", current, type(error).__name__)
            raise PermanentFetchError("BAD_RESPONSE") from None
        except Exception as error:
            # Whatever else a transport library raises must not end the source's worker.
            log.exception("unexpected fetch error for %s", current)
            raise self._failure(state, RetryableFetchError(
                f"FETCH_ERROR:{type(error).__name__}", retry_after=60.0)) from None
        finally:
            if response is not None:
                response.close()
        log.info("%s %s %s %.1fs %sB", method, status, current,
                 self.clock() - started, len(body))
        if is_challenge(status, headers, bytes(body), widget_ok=captcha_widget):
            raise ChallengeDetected("CHALLENGE", status=status)
        if 200 <= status < 300:
            state.failures = 0
            return Page(current, status, bytes(body), headers.get("content-type", ""))
        if status == 429:
            wait = retry_delay(headers)
            with state.lock:
                state.next_at = max(state.next_at, self.clock() + wait)
            raise self._failure(state, RetryableFetchError("RATE_LIMITED", status=status,
                                                           retry_after=wait))
        if status >= 500 or status == 408:
            wait = retry_delay(headers) if "retry-after" in headers else None
            raise self._failure(state, RetryableFetchError("SERVER_ERROR", status=status,
                                                           retry_after=wait))
        if status in {401, 403}:
            raise self._failure(state, RetryableFetchError("ACCESS_DENIED", status=status,
                                                           retry_after=600.0))
        raise PermanentFetchError("NOT_FOUND" if status in {404, 410} else "CLIENT_ERROR",
                                  status=status)
