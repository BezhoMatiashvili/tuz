"""Graph API client for the Garage Facebook integration.

Every request goes to the fixed ``https://graph.facebook.com/<version>/`` prefix: callers pass a
relative object/edge path (``me/accounts``, ``{page_id}/messages``), never a URL. Redirects are
refused, and paging follows the ``after`` cursor on the same path instead of the ``paging.next``
URL Meta hands back, so no answer can steer a token to another host.

Each call carries the access token and, for tokens minted through our own app, ``appsecret_proof``
(HMAC-SHA256 of the token keyed with the app secret): a token copied out of this process is then
useless without the secret. A token minted through another app (the optional send-only page token,
a separate ads app) goes without the proof, because our secret would make Meta reject it.

Retries are limited to calls that cannot duplicate an effect. Reads retry on Meta's throttling
codes, 5xx answers and broken connections; writes run once unless the caller declares them
idempotent (subscriptions); messages to customers never retry, because a send repeated after it
left the machine can reach the customer twice.

No token, secret or proof appears in an exception, a log line or a repr. Errors are built from
Meta's JSON error object, its message scrubbed of every secret the call used, and are raised
outside the ``except`` block, so the ``requests`` exception (whose text embeds the full URL with
its query string) is never chained onto them.
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import NewConnectionError

from automotive.meta_config import GRAPH_HOST, MetaConfigError, MetaSettings

LOGGER = logging.getLogger(__name__)

GRAPH_HOSTNAME = urlsplit(GRAPH_HOST).hostname
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 20.0
MAX_RETRIES = 2
MAX_BODY_BYTES = 8 * 1024 * 1024
# Meta's throttling codes: app, user and page rate limits, the custom/ads limit and the ads
# management limit. GraphError.throttled also counts the rest of the business use-case range
# (80000-80099, e.g. 80006 for Messenger), which only lets a read retry.
THROTTLE_CODES = frozenset({4, 17, 32, 613, 80004})
# Send API subcodes (Meta's Messenger error codes page). The customer's messaging window is
# closed: the call itself was allowed. (2018109 is an attachment over the size limit.)
OUTSIDE_WINDOW_SUBCODES = frozenset({2018278, 2018065, 2534022})
# Meta's gate for an app whose messaging permission is not reviewed and live yet: "cannot
# message users who are not admins, developers or testers of the app". TNLU met it as a bare 10.
GATE_SUBCODES = frozenset({2018028, 2018027})
SENDER_ACTIONS = frozenset({"mark_seen", "typing_on", "typing_off"})
MESSAGING_TYPES = frozenset({"RESPONSE", "UPDATE"})
# Parameters whose values are secrets and are scrubbed from Meta's error messages.
SECRET_PARAMS = ("input_token", "fb_exchange_token", "client_secret")

_PATH = re.compile(r"^[A-Za-z0-9_-]{1,120}(?:/[A-Za-z0-9_-]{1,120}){0,3}$")
_VERSION = re.compile(r"^v[0-9]{1,2}\.[0-9]$")
_DIGITS = re.compile(r"^[0-9]{1,30}$")
_OBJECT_ID = re.compile(r"^[0-9_]{1,80}$")
_FIELD = re.compile(r"^[a-z_]{1,40}$")
_BARE_TOKEN = re.compile(r"EAA[A-Za-z0-9]{12,}")


@dataclass(frozen=True)
class TokenChoice:
    """A stored access token and whether it was minted through our app.

    Unpacks as ``token, proof = choice``, and every GraphClient method accepts it in place of a
    plain token: the client then adds ``appsecret_proof`` only when ``proof`` is true. The token
    stays out of ``repr``.
    """

    token: str = field(repr=False)
    proof: bool = True
    purpose: str = ""

    def __iter__(self) -> Iterator[Any]:
        return iter((self.token, self.proof))


class GraphError(RuntimeError):
    """Meta refused a call, answered with something that is not Graph JSON, or did not answer.

    ``http_status`` is 0 when no answer arrived. ``maybe_applied`` is then true unless the
    connection provably failed before the request left (refused, no DNS, connect timeout): a write
    that timed out while waiting for the answer may already have been applied and must not be
    repeated. ``message`` is Meta's text with the call's secrets removed, at most 300 characters.
    ``str()`` and ``repr()`` carry codes only, so logging the exception logs no free text.
    """

    def __init__(self, http_status: int, code: int | None, subcode: int | None, type: str = "",
                 message: str = "", fbtrace_id: str = "", *, maybe_applied: bool = False) -> None:
        super().__init__(http_status, code, subcode)
        self.http_status = int(http_status)
        self.code = code
        self.subcode = subcode
        self.type = type
        self.message = message
        self.fbtrace_id = fbtrace_id
        self.maybe_applied = maybe_applied

    def __str__(self) -> str:
        return (f"graph_error http={self.http_status} code={self.code} "
                f"subcode={self.subcode} type={self.type}")

    def __repr__(self) -> str:
        return (f"GraphError(http_status={self.http_status}, code={self.code}, "
                f"subcode={self.subcode}, type={self.type!r})")

    @property
    def token_dead(self) -> bool:
        return self.code == 190

    @property
    def gated(self) -> bool:
        """The app may not message this person at all: code 10 without a subcode, or the
        documented gate (200 or 10 with GATE_SUBCODES). Another code 10 is not the gate."""
        return ((self.code == 10 and not self.subcode)
                or (self.code in (10, 200) and self.subcode in GATE_SUBCODES))

    @property
    def outside_window(self) -> bool:
        """The customer's messaging window is closed; the call itself was allowed."""
        return self.code in (10, 100) and self.subcode in OUTSIDE_WINDOW_SUBCODES

    @property
    def unavailable(self) -> bool:
        """This person cannot get messages from the page now (blocked it, deactivated...):
        551, subcode 1545041 (under 551 or 200), or 10/2018108."""
        return (self.code == 551 or self.subcode == 1545041
                or (self.code == 10 and self.subcode == 2018108))

    @property
    def throttled(self) -> bool:
        return self.code is not None and (self.code in THROTTLE_CODES
                                          or 80000 <= self.code <= 80099)

    @property
    def invalid(self) -> bool:
        return self.code == 100

    @property
    def network(self) -> bool:
        return self.http_status == 0

    @property
    def retryable(self) -> bool:
        """Worth another attempt for a call that is safe to repeat."""
        return (self.throttled or self.network or self.http_status >= 500
                or self.type == "truncated")

    def summary(self) -> dict:
        """Codes for an API answer, an audit row or a reply record; never the message."""
        return {"http_status": self.http_status, "code": self.code, "subcode": self.subcode,
                "type": self.type, "fbtrace_id": self.fbtrace_id}


class _RedactQuerySecrets(logging.Filter):
    """urllib3 logs each request line, query string included, at DEBUG level; a read's query
    carries the token, so a debugging session must not copy it into the log."""

    pattern = re.compile(r"((?:access_token|appsecret_proof|input_token|client_secret|"
                         r"fb_exchange_token)=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.pattern.sub(r"\1[redacted]", value)
                                if isinstance(value, str) else value for value in record.args)
        if isinstance(record.msg, str):
            record.msg = self.pattern.sub(r"\1[redacted]", record.msg)
        return True


_URLLIB3_LOG = logging.getLogger("urllib3.connectionpool")
if not any(isinstance(item, _RedactQuerySecrets) for item in _URLLIB3_LOG.filters):
    _URLLIB3_LOG.addFilter(_RedactQuerySecrets())


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    """Graph needs no cookies; keeping none stops one call's state reaching the next."""

    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False


def _new_session() -> requests.Session:
    session = requests.Session()
    # No proxies or .netrc credentials from the environment: tokens go straight to Meta.
    session.trust_env = False
    session.cookies.set_policy(_NoCookies())
    session.headers.update({"User-Agent": "garage-1886-meta/1.0", "Accept": "application/json"})
    return session


def appsecret_proof(token: str, app_secret: str) -> str:
    """HMAC-SHA256 of the access token keyed with the app secret, as Meta defines it."""
    return hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _plain(token: str | TokenChoice) -> tuple[str, bool]:
    value, ours = (token.token, token.proof) if isinstance(token, TokenChoice) else (token, True)
    if (not isinstance(value, str) or not value or len(value) > 4096 or not value.isprintable()
            or any(character.isspace() for character in value)):
        raise ValueError("An access token must be a non-empty string without spaces")
    return value, ours


def _encode(values: Mapping[str, Any] | None) -> dict[str, str]:
    """Graph's form and query encoding: JSON for objects and lists, true/false for booleans."""
    encoded: dict[str, str] = {}
    for key, value in (values or {}).items():
        if key in ("access_token", "appsecret_proof"):
            raise ValueError("Pass the access token as the token argument")
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        elif isinstance(value, int | float):
            encoded[key] = str(value)
        elif isinstance(value, str):
            encoded[key] = value
        elif isinstance(value, dict | list | tuple):
            encoded[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            raise TypeError(f"Unsupported Graph parameter type for {key}")
    return encoded


def _scrubber(values: Sequence[str]) -> Callable[[str], str]:
    hidden = sorted({value for value in values if value and len(value) >= 6}, key=len,
                    reverse=True)

    def scrub(text: str) -> str:
        for value in hidden:
            text = text.replace(value, "[redacted]")
        return _BARE_TOKEN.sub("[redacted]", text)

    return scrub


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _never_sent(error: requests.exceptions.ConnectionError) -> bool:
    """Whether the connection failed before any byte of the request left (refused, no DNS)."""
    reason = error.args[0] if error.args else None
    reason = getattr(reason, "reason", reason)
    return isinstance(reason, NewConnectionError)


class _TooLarge(Exception):
    pass


def _read(response: Any) -> bytes:
    body = bytearray()
    for chunk in response.iter_content(65536):
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _TooLarge
    return bytes(body)


def _parse(status: int, raw: bytes, scrub: Callable[[str], str]) -> dict | GraphError:
    if 300 <= status < 400:
        return GraphError(status, None, None, "redirect", "redirect_refused")
    try:
        document = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("error"), dict):
        error = document["error"]
        return GraphError(status, _number(error.get("code")), _number(error.get("error_subcode")),
                          _text(error.get("type"), 80),
                          scrub(_text(error.get("message"), 2000))[:300],
                          _text(error.get("fbtrace_id"), 80))
    if 200 <= status < 300:
        if isinstance(document, dict):
            return document
        if isinstance(document, bool):
            return {"success": document}
        # A 2xx without a JSON object is an answer cut short; a write behind it may have landed.
        return GraphError(status, None, None, "truncated", "unexpected_response",
                          maybe_applied=True)
    return GraphError(status, None, None, "http", "non_graph_response")


def _fields(fields: Sequence[str]) -> str:
    names = list(fields)
    if not names or any(not isinstance(name, str) or not _FIELD.match(name) for name in names):
        raise ValueError("Webhook fields must be lower-case names")
    return ",".join(names)


def _require(pattern: re.Pattern, value: Any, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"{what} is malformed")
    return value


def _default_jitter() -> float:
    return secrets.randbelow(1000) / 1000


class GraphClient:
    """Requests to graph.facebook.com for one app, with a fixed base, proofs and bounded retries."""

    def __init__(self, settings: MetaSettings, *, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = _default_jitter) -> None:
        if not _VERSION.match(settings.graph_version or ""):
            raise MetaConfigError("META_GRAPH_API_VERSION must be like v26.0")
        self.settings = settings
        self._base = settings.graph_base
        self._session = session if session is not None else _new_session()
        self._sleep = sleep
        self._jitter = jitter

    def __repr__(self) -> str:
        return f"GraphClient(base={self._base!r})"

    # -- plumbing ----------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        clean = path.lstrip("/") if isinstance(path, str) else ""
        if not _PATH.match(clean):
            raise ValueError("A Graph path is a relative object/edge name")
        url = self._base + clean
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != GRAPH_HOSTNAME or parts.port is not None
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("Only https://graph.facebook.com is reachable")
        return url

    def _proof(self, token: str) -> str:
        if not self.settings.app_secret:
            raise MetaConfigError("META_APP_SECRET must be set to sign Graph calls")
        return appsecret_proof(token, self.settings.app_secret)

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 2.0 ** attempt) + self._jitter() * 0.5

    def _request(self, method: str, path: str, token: str | TokenChoice, *,
                 params: Mapping[str, Any] | None = None, data: Mapping[str, Any] | None = None,
                 proof: bool = True, retry: bool = False,
                 read_timeout: float = READ_TIMEOUT) -> dict:
        url = self._url(path)
        value, ours = _plain(token)
        auth = {"access_token": value}
        hidden = [value, self.settings.app_secret]
        if proof and ours:
            auth["appsecret_proof"] = self._proof(value)
            hidden.append(auth["appsecret_proof"])
        for source in (params or {}, data or {}):
            hidden.extend(source[key] for key in SECRET_PARAMS if isinstance(source.get(key), str))
        query, body = _encode(params), _encode(data)
        (body if method == "POST" else query).update(auth)
        scrub = _scrubber(hidden)
        attempts = 1 + MAX_RETRIES if method == "GET" or retry else 1
        for attempt in range(attempts):
            outcome = self._once(method, url, query, body, read_timeout, scrub)
            if isinstance(outcome, dict):
                return outcome
            if attempt + 1 < attempts and outcome.retryable:
                LOGGER.info("meta_graph_retry method=%s http=%s code=%s attempt=%s", method,
                            outcome.http_status, outcome.code, attempt + 1)
                self._sleep(self._backoff(attempt))
                continue
            # Raised here, outside every except block, so no requests exception is chained.
            raise outcome
        raise AssertionError("unreachable")

    def _once(self, method: str, url: str, query: dict, body: dict, read_timeout: float,
              scrub: Callable[[str], str]) -> dict | GraphError:
        failure: GraphError | None = None
        response = None
        status, raw = 0, b""
        try:
            response = self._session.request(
                method, url, params=query or None, data=body or None,
                timeout=(CONNECT_TIMEOUT, read_timeout), allow_redirects=False, stream=True)
            status = int(response.status_code)
            raw = _read(response)
        except _TooLarge:
            failure = GraphError(status, None, None, "http", "response_too_large",
                                 maybe_applied=True)
        except requests.exceptions.ConnectTimeout:
            failure = GraphError(0, None, None, "network", "connect_timeout")
        except requests.exceptions.ReadTimeout:
            failure = GraphError(0, None, None, "network", "read_timeout", maybe_applied=True)
        except requests.exceptions.ConnectionError as error:
            failure = GraphError(0, None, None, "network", "connection_error",
                                 maybe_applied=not _never_sent(error))
        except requests.exceptions.RequestException:
            failure = GraphError(0, None, None, "network", "request_error", maybe_applied=True)
        finally:
            if response is not None:
                response.close()
        if failure is not None:
            return failure
        return _parse(status, raw, scrub)

    # -- generic calls -----------------------------------------------------------------------

    def get(self, path: str, token: str | TokenChoice, params: Mapping[str, Any] | None = None,
            proof: bool = True) -> dict:
        return self._request("GET", path, token, params=params, proof=proof)

    def post(self, path: str, token: str | TokenChoice, data: Mapping[str, Any] | None = None,
             proof: bool = True, retry: bool = False) -> dict:
        """One attempt unless ``retry`` declares the write idempotent."""
        return self._request("POST", path, token, data=data, proof=proof, retry=retry)

    def delete(self, path: str, token: str | TokenChoice,
               params: Mapping[str, Any] | None = None, proof: bool = True,
               retry: bool = False) -> dict:
        return self._request("DELETE", path, token, params=params, proof=proof, retry=retry)

    def paged(self, path: str, token: str | TokenChoice, params: Mapping[str, Any] | None = None,
              max_pages: int = 10, *, proof: bool = True) -> list[dict]:
        """Every ``data`` item over at most ``max_pages`` pages, following the ``after`` cursor.

        Meta's ``paging.next`` URL is never fetched; it only says another page exists. Edges that
        page by time (``since``/``until``) end after one page; ask for explicit windows there.
        """
        query = {key: value for key, value in (params or {}).items() if key != "before"}
        rows: list[dict] = []
        for _ in range(max(1, int(max_pages))):
            page = self.get(path, token, query, proof=proof)
            data = page.get("data")
            if not isinstance(data, list):
                break
            rows.extend(item for item in data if isinstance(item, dict))
            paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            after = cursors.get("after")
            if not data or not paging.get("next") or not isinstance(after, str) or not after:
                break
            query = {**query, "after": after}
        return rows

    # -- app -----------------------------------------------------------------------------------

    def _require_app(self) -> None:
        missing = [name for name, value in (("META_APP_ID", self.settings.app_id),
                                            ("META_APP_SECRET", self.settings.app_secret))
                   if not value]
        if missing:
            raise MetaConfigError(f"{', '.join(missing)} must be set")

    def app_token(self) -> str:
        """``app_id|app_secret``: the app's own token, for debug_token and app subscriptions."""
        self._require_app()
        return f"{self.settings.app_id}|{self.settings.app_secret}"

    def debug_token(self, input_token: str | TokenChoice) -> dict:
        """Meta's ``data`` object for a token (is_valid, type, app_id, scopes, expiry, ...)."""
        value, _ = _plain(input_token)
        result = self.get("debug_token", self.app_token(), {"input_token": value})
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def exchange_user_token(self, short_token: str | TokenChoice) -> dict:
        """A long-lived user token (about 60 days) for a short-lived one from Graph API Explorer."""
        value, _ = _plain(short_token)
        result = self.get("oauth/access_token", self.app_token(), {
            "grant_type": "fb_exchange_token", "client_id": self.settings.app_id,
            "client_secret": self.settings.app_secret, "fb_exchange_token": value})
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise GraphError(200, None, None, "truncated", "no_access_token")
        expires_in = _number(result.get("expires_in"))
        return {"access_token": token, "expires_in": expires_in}

    def subscribe_app(self, callback_url: str, verify_token: str,
                      fields: Sequence[str]) -> dict:
        """Point the app's page webhooks at ``callback_url``. Meta verifies the URL during the call,
        so the receiver must be reachable through the tunnel."""
        parts = urlsplit(callback_url or "")
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("The webhook callback must be an https:// address")
        if not verify_token:
            raise MetaConfigError("META_WEBHOOK_VERIFY_TOKEN must be set")
        return self.post(f"{self.settings.app_id}/subscriptions", self.app_token(), {
            "object": "page", "callback_url": callback_url, "verify_token": verify_token,
            "fields": _fields(fields), "include_values": True}, retry=True)

    def app_subscriptions(self) -> dict:
        return self.get(f"{self.settings.app_id}/subscriptions", self.app_token())

    # -- page ----------------------------------------------------------------------------------

    def subscribe_page(self, page_id: str, page_token: str | TokenChoice,
                       fields: Sequence[str], proof: bool = True) -> dict:
        _require(_DIGITS, page_id, "page id")
        return self.post(f"{page_id}/subscribed_apps", page_token,
                         {"subscribed_fields": _fields(fields)}, proof=proof, retry=True)

    def page_subscriptions(self, page_id: str, page_token: str | TokenChoice,
                           proof: bool = True) -> dict:
        _require(_DIGITS, page_id, "page id")
        return self.get(f"{page_id}/subscribed_apps", page_token, proof=proof)

    def unsubscribe_page(self, page_id: str, page_token: str | TokenChoice,
                         proof: bool = True) -> dict:
        """Remove the token's app from the page's webhooks."""
        _require(_DIGITS, page_id, "page id")
        return self.delete(f"{page_id}/subscribed_apps", page_token, proof=proof, retry=True)

    # -- messages and comments (never retried) -------------------------------------------------

    def _send(self, page_id: str, page_token: str | TokenChoice, data: dict,
              proof: bool) -> dict:
        _require(_DIGITS, page_id, "page id")
        result = self._request("POST", f"{page_id}/messages", page_token, data=data,
                               proof=proof, retry=False,
                               read_timeout=self.settings.send_timeout_seconds)
        return {"recipient_id": _text(result.get("recipient_id"), 40),
                "message_id": _text(result.get("message_id"), 200)}

    def send_message(self, page_id: str, page_token: str | TokenChoice, psid: str,
                     message: dict, messaging_type: str = "RESPONSE",
                     proof: bool = True) -> dict:
        """One Send API call; returns recipient_id and message_id ('' if Meta omitted it)."""
        _require(_DIGITS, psid, "psid")
        if messaging_type not in MESSAGING_TYPES:
            raise ValueError("messaging_type must be RESPONSE or UPDATE")
        if not isinstance(message, dict) or not message:
            raise ValueError("message must be a Send API message object")
        return self._send(page_id, page_token, {"recipient": {"id": psid}, "message": message,
                                                "messaging_type": messaging_type}, proof)

    def sender_action(self, page_id: str, page_token: str | TokenChoice, psid: str,
                      action: str, proof: bool = True) -> dict:
        _require(_DIGITS, psid, "psid")
        if action not in SENDER_ACTIONS:
            raise ValueError("Unknown sender action")
        return self._send(page_id, page_token, {"recipient": {"id": psid},
                                                "sender_action": action}, proof)

    def private_reply(self, page_id: str, page_token: str | TokenChoice, comment_id: str,
                      message: dict, proof: bool = True) -> dict:
        """A private Messenger reply to a comment; Meta resolves the commenter."""
        _require(_OBJECT_ID, comment_id, "comment id")
        if not isinstance(message, dict) or not message:
            raise ValueError("message must be a Send API message object")
        return self._send(page_id, page_token, {"recipient": {"comment_id": comment_id},
                                                "message": message}, proof)

    def comment_reply(self, page_token: str | TokenChoice, comment_id: str, text: str,
                      proof: bool = True) -> dict:
        """A public reply under a comment; returns Meta's ``{"id": ...}``."""
        _require(_OBJECT_ID, comment_id, "comment id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A comment reply needs text")
        return self._request("POST", f"{comment_id}/comments", page_token,
                             data={"message": text}, proof=proof, retry=False,
                             read_timeout=self.settings.send_timeout_seconds)

    def set_comment_hidden(self, page_token: str | TokenChoice, comment_id: str, hidden: bool,
                           proof: bool = True) -> dict:
        _require(_OBJECT_ID, comment_id, "comment id")
        return self.post(comment_id, page_token, {"is_hidden": bool(hidden)}, proof=proof)
