"""The public receiver for Meta's webhooks: the only Garage process reachable from the internet.

ngrok forwards Meta's deliveries to this process on loopback. It checks each delivery's
signature, appends every item to ``garage.meta_inbox`` with an INSERT-only login (role
``garage_webhook``), wakes the worker with ``NOTIFY garage_meta_inbox`` and answers at once.
Everything that costs money or talks to Meta happens in the worker, so this process holds the
app secret and the verify token but never a page token, an AI key or a login that can read.

What faces the internet is small and fixed: the webhook (``GET`` handshake, signed ``POST``), the
two static pages Meta needs to publish the app, in Georgian and English, and a health check.
Every other method or path is a 404 answered before the router sees it, so the framework's 405s
and trailing-slash redirects never describe the routes.

Two rules keep Meta's retries useful:

* 503 means "try again" and is returned only when the inbox could not be written. A signed item
  the table could never hold (too large, text with NUL, a non-finite number, a field name outside
  the table's pattern) is made storable here instead of becoming an endless retry that also takes
  the other items of its delivery down with it.
* Every row's ``event_key`` is derived from the item alone, never from the delivery around it, so
  a redelivery in another batch is a silent duplicate (``ON CONFLICT DO NOTHING``).

Logs carry event names and counts only: no message text, sender ids, tokens, addresses, or
database error text (a CHECK violation's detail prints the failing row).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from psycopg.types.json import Jsonb
from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.requests import ClientDisconnect

from automotive.meta_config import WEBHOOK_PATH, MetaConfigError, MetaSettings
from automotive.meta_db import MetaDatabase
from automotive.site_auth import ip_key
from automotive.site_guard import TokenBuckets

LOGGER = logging.getLogger(__name__)
TEMPLATES = Path(__file__).parent / "templates" / "meta"

# The worker LISTENs on this channel. The notification carries no payload.
INBOX_CHANNEL = "garage_meta_inbox"
# No conflict target: garage_webhook may not name one (that needs SELECT on the table), and no
# RETURNING for the same reason; the cursor's rowcount says how many rows were new.
INSERT_SQL = ("INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
              "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING")
NOTIFY_SQL = "NOTIFY garage_meta_inbox"

BODY_CAP = 1_048_576  # bytes in one delivery
# To receive them. Meta sends a delivery at once; a trickle is someone else, holding one of the
# server's 64 connection slots while it lasts.
BODY_SECONDS = 3.0
# Bodies one address may be sending at once before its signature can be checked (it needs the
# whole body). Meta's own deliveries arrive in milliseconds, so this never refuses one; without
# it, slow uploads with a made-up signature could fill every slot and starve Meta's.
UNVERIFIED_READS = 8
# One stored item, measured as PostgreSQL prints the jsonb (the CHECK allows 65,536 bytes).
PAYLOAD_CAP = 61_440
MAX_DEPTH = 48  # nesting inside one item; Meta's deepest is about eight
DB_SLOTS = 4  # inbox writes at once, so a burst never opens more database connections

# Reserved payload keys. Meta's own keys never start with an underscore.
ENTRY_TIME_KEY = "_entry_time"  # entry.time of the delivery that carried the item
REPAIRED_KEY = "_repaired"  # text had NUL or lone surrogates, or a number was not finite
OVERSIZED_KEY = "_oversized"  # the row is a marker, see ``_marker``
UNKNOWN_FIELD = "unknown"  # a change whose field name the table could not hold

# (burst, refill per minute) per client address. A signed delivery is never refused because of
# another request's failures: when X-Forwarded-For is missing every caller shares one key.
LIMITS = {
    "signed": (600, 600),  # deliveries that verified
    "failed": (30, 30),  # bad signatures, refused handshakes, oversized bodies, unknown paths
    "page": (60, 120),  # the static pages and the health check
}
RETRY_AFTER = "60"

PAGES = {
    "/privacy": ("privacy.html", "ka"),
    "/privacy/en": ("privacy.html", "en"),
    "/data-deletion": ("data_deletion.html", "ka"),
    "/data-deletion/en": ("data_deletion.html", "en"),
}
# The shop as its public Facebook page shows it (2026-11-11). The owner confirms these before the
# app is published. The receiver cannot read the dashboard's brand settings (its login can only
# append to the inbox), so a change there has to be repeated here.
SHOP = {
    "name_ka": "ფარეხი 1886",
    "name_en": "GARAGE 1886",
    "page_name": "Garage • ფარეხი 1886",
    "address_ka": "ქავთარაძის ქ. N 17, თბილისი",
    "address_en": "17 Kavtaradze Street, Tbilisi, Georgia",
    "phone": "+995 596 33 33 74",
    "phone_link": "+995596333374",
    "updated_ka": "2026 წლის 11 ნოემბერი",
    "updated_en": "11 November 2026",
    # meta_settings.retention_days: 365 by default, and the most the dashboard accepts
    # (meta_store.SETTINGS_FIELDS, a test keeps them equal), so no setting outlasts the pages.
    "retention_days": 365,
    # The worker purges meta_inbox rows older than meta_store.INBOX_RETENTION_DAYS (6) once a
    # day, so none is older than this.
    "inbox_days": 7,
    "answer_days": 10,  # promised time to act on an access, correction or deletion request
}

_ROUTES: dict[tuple[str, str], str] = {
    ("GET", WEBHOOK_PATH): "handshake",
    ("POST", WEBHOOK_PATH): "webhook",
    ("GET", "/healthz"): "health",
    ("HEAD", "/healthz"): "health",
    **{(method, path): "page" for path in PAGES for method in ("GET", "HEAD")},
}
_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
             "form-action 'none'; frame-ancestors 'none'")
# fullmatch everywhere: "$" would also accept a trailing newline.
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})")
_FIELD = re.compile(r"[a-z_]{1,40}")  # garage.meta_inbox.field
_PAGE_ID = re.compile(r"[0-9]{1,30}")  # garage.meta_inbox.page_id
# PostgreSQL text cannot hold NUL, and UTF-8 cannot encode a lone surrogate.
_UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")
_RECEIPTS = ("read", "delivery")
_ACTIONS = ("message", "postback", "referral")

Row = tuple[str, str, str, dict[str, Any]]


class Inbox(Protocol):
    """Where verified items go. ``append`` stores one delivery's rows together or raises.

    Each row is ``(event_key, field, page_id, payload)``. The return value is optional: the
    number of rows that were new, used only for the log line."""

    def append(self, items: list[Row]) -> int | None: ...


class DatabaseInbox:
    """Appends with the receiver's login, which may INSERT four inbox columns and nothing else."""

    def __init__(self, database: MetaDatabase) -> None:
        self.database = database

    def append(self, items: list[Row]) -> int:
        """One transaction per delivery; a repeated event_key is skipped, never an error. The
        worker is woken only when something new arrived (NOTIFY is delivered at commit)."""
        if not items:
            return 0
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(INSERT_SQL, [(key, field_name, page_id, Jsonb(payload))
                                                for key, field_name, page_id, payload in items])
                stored = max(0, cursor.rowcount)
            if stored:
                connection.execute(NOTIFY_SQL)
        return stored


# -- parsing a delivery --------------------------------------------------------------------


class BadDelivery(ValueError):
    """A signed body that is not a page delivery. ``reason`` is a fixed code for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _TooDeep(ValueError):
    pass


@dataclass
class Delivery:
    """The rows of one delivery, and counts of what was not turned into a row."""

    rows: list[Row] = field(default_factory=list)
    items: int = 0  # every item seen, rows or not
    duplicates: int = 0  # the same event twice in this delivery
    receipts: int = 0  # read and delivery receipts, which nothing acts on
    unusable: int = 0  # entries without a valid page id, items that are not objects
    oversized: int = 0  # stored as a marker
    repaired: int = 0
    ignored: bool = False  # signed, but about another object than a page


def parse_delivery(body: bytes) -> Delivery:
    """Turn a verified body into inbox rows.

    ``entry[].messaging[]`` items become field ``messages``, ``entry[].standby[]`` items field
    ``standby`` and ``entry[].changes[]`` items their own field (``feed``). Read and delivery
    receipts are skipped. Raises ``BadDelivery`` when the body is not JSON, not an object with
    a string ``object``, has no ``entry`` list, or nests deeper than ``MAX_DEPTH``."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError included
        raise BadDelivery("json") from None
    if not isinstance(document, dict) or not isinstance(document.get("object"), str):
        raise BadDelivery("shape")
    if document["object"] != "page":
        return Delivery(ignored=True)
    entries = document.get("entry")
    if not isinstance(entries, list):
        raise BadDelivery("shape")
    delivery, seen = Delivery(), set()
    for entry in entries:
        page_id = _page_id(entry.get("id")) if isinstance(entry, dict) else None
        if page_id is None:
            delivery.unusable += 1
            continue
        entry_time = _entry_time(entry.get("time"))
        for source in ("messaging", "standby", "changes"):
            items = entry.get(source)
            if items is None:
                continue
            if not isinstance(items, list):
                delivery.unusable += 1
                continue
            for item in items:
                delivery.items += 1
                _add(delivery, seen, source, item, page_id, entry_time)
    return delivery


def _add(delivery: Delivery, seen: set[str], source: str, raw: Any, page_id: str,
         entry_time: int | None) -> None:
    if not isinstance(raw, dict):
        delivery.unusable += 1
        return
    try:
        item, repaired = _clean(raw, 0)
    except _TooDeep:
        raise BadDelivery("depth") from None
    classified = classify(source, item)
    if classified is None:
        delivery.receipts += 1
        return
    kind, identity = classified
    key = event_key(kind, identity)
    if key in seen:
        delivery.duplicates += 1
        return
    seen.add(key)
    if source == "messaging":
        field_name = "messages"
    elif source == "standby":
        field_name = "standby"
    else:
        own = item.get("field")
        field_name = own if isinstance(own, str) and _FIELD.fullmatch(own) else UNKNOWN_FIELD
    payload = dict(item)
    if entry_time is not None:
        payload[ENTRY_TIME_KEY] = entry_time
    if repaired:
        payload[REPAIRED_KEY] = True
        delivery.repaired += 1
    size = jsonb_size(payload)
    if size > PAYLOAD_CAP:
        payload = _marker(kind, item, size, entry_time)
        delivery.oversized += 1
    delivery.rows.append((key, field_name, page_id, payload))


def classify(source: str, item: dict[str, Any]) -> tuple[str, Any] | None:
    """``(kind, identity)`` of one webhook item, or None for a receipt nobody acts on.

    ``source`` is the list the item came in: ``messaging``, ``standby`` or ``changes``.

    ===== =====================================================================================
    msg   ``[message.mid]``
    echo  ``[message.mid]`` of a message with ``is_echo: true`` (the page's own message)
    pb    ``[sender.id, timestamp, postback.payload, postback.mid]``
    ref   ``[sender.id, timestamp, referral.ref, referral.ad_id]``
    chg   ``[field, value.comment_id, value.post_id, value.item, value.verb, value.created_time]``
    sb    the standby item itself
    raw   the item itself (anything else)
    ===== =====================================================================================

    Missing values count as null. Two reactions to one post in the same second by different
    people share a ``chg`` identity and keep only the first; nothing acts on reactions."""
    if source == "standby":
        return None if _receipt(item) else ("sb", item)
    if source == "messaging":
        message = item.get("message")
        if isinstance(message, dict) and isinstance(message.get("mid"), str) and message["mid"]:
            return ("echo" if message.get("is_echo") is True else "msg"), [message["mid"]]
        postback = item.get("postback")
        if isinstance(postback, dict):
            return "pb", [_id_of(item.get("sender")), item.get("timestamp"),
                          postback.get("payload"), postback.get("mid")]
        referral = item.get("referral")
        if isinstance(referral, dict):
            return "ref", [_id_of(item.get("sender")), item.get("timestamp"),
                           referral.get("ref"), referral.get("ad_id")]
        return None if _receipt(item) else ("raw", item)
    value = item.get("value")
    if isinstance(value, dict):
        return "chg", [item.get("field"), value.get("comment_id"), value.get("post_id"),
                       value.get("item"), value.get("verb"), value.get("created_time")]
    return "raw", item


def event_key(kind: str, identity: Any) -> str:
    """``kind:<sha256 hex>`` of the identity's canonical JSON (sorted keys, no spaces, UTF-8).
    A JSON array keeps free text such as a postback payload from running into the next value."""
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{kind}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def jsonb_size(value: Any) -> int:
    """Bytes of ``value`` as PostgreSQL prints it as jsonb text, which the inbox CHECK measures:
    ``", "`` and ``": "`` separators, non-ASCII as raw UTF-8, and numbers in positional notation
    (``1e+300`` prints as 301 digits). Exact for what ``_clean`` returns."""
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if value is None or value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        text = repr(value)
        return max(len(text), len(format(Decimal(text), "f")))
    if isinstance(value, dict):
        return (2 + 2 * max(0, len(value) - 1)
                + sum(jsonb_size(key) + 2 + jsonb_size(item) for key, item in value.items()))
    if isinstance(value, list):
        return 2 + 2 * max(0, len(value) - 1) + sum(jsonb_size(item) for item in value)
    raise TypeError(type(value).__name__)


def _clean(value: Any, depth: int) -> tuple[Any, bool]:
    """A copy the inbox can store, and whether anything had to change: NUL and lone surrogates
    become U+FFFD, a non-finite number (``NaN``, ``1e999``) becomes null. Meta never sends
    these, but a signed item the database refuses would be retried for a day and a half."""
    if depth > MAX_DEPTH:
        raise _TooDeep
    if isinstance(value, str):
        if _UNSTORABLE_TEXT.search(value):
            return _UNSTORABLE_TEXT.sub("�", value), True
        return value, False
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    if isinstance(value, dict):
        copy, changed = {}, False
        for key, item in value.items():
            clean_key, key_changed = _clean(key, depth + 1)
            clean_item, item_changed = _clean(item, depth + 1)
            copy[clean_key] = clean_item
            changed = changed or key_changed or item_changed
        return copy, changed
    if isinstance(value, list):
        copy_list, changed = [], False
        for item in value:
            clean_item, item_changed = _clean(item, depth + 1)
            copy_list.append(clean_item)
            changed = changed or item_changed
        return copy_list, changed
    return value, False


def _marker(kind: str, item: dict[str, Any], size: int, entry_time: int | None) -> dict:
    """Stored instead of an item over ``PAYLOAD_CAP``: enough to flag the chat or comment and to
    fetch the original from the Graph API by its mid or comment id, and none of its text."""
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    postback = item.get("postback") if isinstance(item.get("postback"), dict) else {}
    value = item.get("value") if isinstance(item.get("value"), dict) else {}
    found = {
        "sender": _id_of(item.get("sender")) or _id_of(value.get("from")),
        "recipient": _id_of(item.get("recipient")),
        "timestamp": item.get("timestamp"),
        "mid": message.get("mid") or postback.get("mid"),
        "field": item.get("field"),
        "comment_id": value.get("comment_id"),
        "post_id": value.get("post_id"),
        "parent_id": value.get("parent_id"),
        "item": value.get("item"),
        "verb": value.get("verb"),
        "created_time": value.get("created_time"),
    }
    marker: dict[str, Any] = {
        OVERSIZED_KEY: True, "_bytes": size, "_kind": kind,
        "ids": {name: found_value for name, found_value in found.items()
                if _short_scalar(found_value)},
    }
    if entry_time is not None:
        marker[ENTRY_TIME_KEY] = entry_time
    return marker


def _short_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return 0 < len(value) <= 200
    return isinstance(value, int) and not isinstance(value, bool)


def _id_of(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else None


def _receipt(item: dict[str, Any]) -> bool:
    return (any(name in item for name in _RECEIPTS)
            and not any(name in item for name in _ACTIONS))


def _page_id(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        value = str(value)
    return value if isinstance(value, str) and _PAGE_ID.fullmatch(value) else None


def _entry_time(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**15:
        return value
    return None


# -- the application ------------------------------------------------------------------------


class _TooLarge(Exception):
    pass


def render_pages() -> dict[str, bytes]:
    """Every static page, rendered once: they change only with a release."""
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True,
                              undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    rendered = {}
    for path, (template, language) in PAGES.items():
        suffix = "/en" if language == "en" else ""
        base = path.removesuffix("/en")
        html = environment.get_template(template).render(
            lang=language, shop=SHOP, suffix=suffix,
            switch=base if language == "en" else base + "/en")
        rendered[path] = html.encode("utf-8")
    return rendered


class _Receiver:
    """The secrets, the inbox, the limits and the pages behind one application."""

    def __init__(self, settings: MetaSettings, inbox: Inbox, buckets: TokenBuckets) -> None:
        self.secret = settings.app_secret.encode("utf-8")
        self.verify = hashlib.sha256(settings.verify_token.encode("utf-8")).digest()
        self.inbox, self.buckets = inbox, buckets
        self.pages = render_pages()
        self._slots: anyio.CapacityLimiter | None = None  # made inside the event loop
        # Bodies being read, per client key (only touched on the event loop's thread).
        self._reading: dict[str, int] = {}

    def failed(self, client: str) -> bool:
        """Charge a failure to ``client``. False once its allowance is spent: answer 429."""
        return self.buckets.allow(("failed", client), *LIMITS["failed"])

    def challenge_for(self, params: QueryParams) -> str | None:
        """Meta's subscription handshake: the challenge to echo, or None to refuse."""
        def single(name: str) -> str | None:
            values = params.getlist(name)
            return values[0] if len(values) == 1 else None

        mode, token, challenge = (single("hub.mode"), single("hub.verify_token"),
                                  single("hub.challenge"))
        if (mode != "subscribe" or token is None or challenge is None
                or not _CHALLENGE.fullmatch(challenge)):
            return None
        # Digests of equal length: compare_digest then reveals nothing, not even the length.
        supplied = hashlib.sha256(token.encode("utf-8", "replace")).digest()
        return challenge if hmac.compare_digest(supplied, self.verify) else None

    async def handshake(self, request: Request) -> Response:
        challenge = self.challenge_for(request.query_params)
        if challenge is not None:
            LOGGER.info("meta_webhook_verified")
            return PlainTextResponse(challenge)
        return self._refuse(_client_of(request), 403, "meta_webhook_handshake_refused")

    async def deliver(self, request: Request) -> Response:
        client = _client_of(request)
        headers = request.headers.getlist("x-hub-signature-256")
        supplied = _SIGNATURE.fullmatch(headers[0]) if len(headers) == 1 else None
        if supplied is None:  # cannot verify, so the body is not even read
            return self._refuse(client, 401, "meta_webhook_signature_missing")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > BODY_CAP):
            return self._refuse(client, 413, "meta_webhook_body_too_large")
        if self._reading.get(client, 0) >= UNVERIFIED_READS:
            return self._refuse(client, 429, "meta_webhook_uploads_busy")
        self._reading[client] = self._reading.get(client, 0) + 1
        try:
            with anyio.fail_after(BODY_SECONDS):
                body = await _read_capped(request, BODY_CAP)
        except _TooLarge:
            return self._refuse(client, 413, "meta_webhook_body_too_large")
        except TimeoutError:
            return self._refuse(client, 408, "meta_webhook_body_timeout")
        except ClientDisconnect:
            self.failed(client)  # an upload dropped midway counts like any other failure
            return _empty(400)  # nobody is left to read it
        finally:
            left = self._reading.get(client, 1) - 1
            if left > 0:
                self._reading[client] = left
            else:
                self._reading.pop(client, None)
        expected = hmac.new(self.secret, body, hashlib.sha256).hexdigest().encode("ascii")
        if not hmac.compare_digest(supplied.group(1).lower().encode("ascii"), expected):
            return self._refuse(client, 401, "meta_webhook_signature_refused")
        if not self.buckets.allow(("signed", client), *LIMITS["signed"]):
            LOGGER.warning("meta_webhook_throttled")
            return _empty(429, retry=True)
        try:
            delivery = parse_delivery(body)
        except BadDelivery as error:
            LOGGER.warning("meta_webhook_bad_payload reason=%s", error.reason)
            return _empty(400)
        if delivery.ignored:
            LOGGER.info("meta_webhook_ignored_object")
            return PlainTextResponse("EVENT_RECEIVED")
        stored: int | None = 0
        if delivery.rows:
            try:
                stored = await self._append(delivery.rows)
            except Exception as error:  # any failure to store is worth Meta's retry
                LOGGER.error("meta_inbox_unavailable error=%s sqlstate=%s",
                             type(error).__name__, getattr(error, "sqlstate", None) or "-")
                return _empty(503, retry=True)
        LOGGER.info("meta_webhook_received items=%d rows=%d new=%s duplicates=%d receipts=%d "
                    "unusable=%d oversized=%d repaired=%d", delivery.items,
                    len(delivery.rows), "-" if stored is None else stored, delivery.duplicates,
                    delivery.receipts, delivery.unusable, delivery.oversized, delivery.repaired)
        return PlainTextResponse("EVENT_RECEIVED")

    async def page(self, request: Request) -> Response:
        return Response(self.pages[request.scope["path"]], media_type="text/html")

    async def _append(self, rows: list[Row]) -> int | None:
        if self._slots is None:
            self._slots = anyio.CapacityLimiter(DB_SLOTS)
        # psycopg blocks; a worker thread keeps the event loop answering meanwhile.
        return await anyio.to_thread.run_sync(self.inbox.append, rows, limiter=self._slots)

    def _refuse(self, client: str, status: int, event: str) -> Response:
        """A failed request: its own status while the address has allowance left, then 429.
        Logged only while allowed, so a flood cannot flood the log too."""
        if self.failed(client):
            LOGGER.warning(event)
            return _empty(status, retry=status == 429)
        return _empty(429, retry=True)


async def _health(request: Request) -> Response:
    return PlainTextResponse("ok")


async def _read_capped(request: Request, cap: int) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _empty(status: int, *, retry: bool = False) -> Response:
    """Errors say nothing beyond their status."""
    return Response(status_code=status, media_type="text/plain",
                    headers={"retry-after": RETRY_AFTER} if retry else None)


def _client_of(request: Request) -> str:
    return request.scope.get("state", {}).get("meta_client") or client_key(request.scope)


def client_key(scope: dict) -> str:
    """Throttle key for the caller. ngrok connects from loopback and appends the address it
    accepted the connection from to X-Forwarded-For, so the last hop is the caller and anything
    left of it is whatever the caller chose to send. Another peer is taken as it is: nothing but
    ngrok should reach this port, and only loopback may speak for someone else."""
    peer = scope["client"][0] if scope.get("client") else ""
    if _loopback(peer):
        forwarded = ",".join(Headers(scope=scope).getlist("x-forwarded-for"))
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops and (key := ip_key(hops[-1])) != "unknown":
            return key
    return ip_key(peer)


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _harden(raw: list[tuple[bytes, bytes]], route: str | None,
            status: int) -> list[tuple[bytes, bytes]]:
    """Security headers for every response. Nothing here sets a cookie or allows another origin,
    and the server banner is dropped even if a layer below added one."""
    kept = [(name, value) for name, value in raw
            if name.lower() not in (b"server", b"set-cookie")
            and not name.lower().startswith(b"access-control-")]
    out = MutableHeaders(raw=kept)
    page = route == "page" and status == 200  # a refused page request has no styles to allow
    out["content-security-policy"] = _PAGE_CSP if page else _CSP
    out["x-content-type-options"] = "nosniff"
    out["referrer-policy"] = "no-referrer"
    out["x-frame-options"] = "DENY"
    out["cross-origin-opener-policy"] = "same-origin"
    out["cross-origin-resource-policy"] = "same-origin"
    out["permissions-policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    out["x-robots-tag"] = "noindex, nofollow"
    out["cache-control"] = "public, max-age=300" if page else "no-store"
    if status >= 400:
        # A refusal may leave part of a body unread. uvicorn then keeps the connection open,
        # and one more byte cancels its keep-alive timer, so a client could hold the slot for
        # as long as it likes; closing ends that.
        out["connection"] = "close"
    return out.raw


class _Guard:
    """Outermost layer: the route allow-list, the page limit, headers, and the last resort for
    an exception, so even a 500 carries the headers and no traceback text reaches a log."""

    def __init__(self, app: Any, *, receiver: _Receiver) -> None:
        self.app, self.receiver = app, receiver

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = client_key(scope)
        scope.setdefault("state", {})["meta_client"] = client
        route = _ROUTES.get((scope["method"], scope["path"]))
        if route is None:
            await _respond(send, 404 if self.receiver.failed(client) else 429, None)
            return
        if route in ("page", "health") and not self.receiver.buckets.allow(
                ("page", client), *LIMITS["page"]):
            await _respond(send, 429, route)
            return
        started = False

        async def hardened(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = _harden(list(message.get("headers", [])), route,
                                             message["status"])
            await send(message)

        try:
            await self.app(scope, receive, hardened)
        except Exception as error:
            LOGGER.error("meta_receiver_error error=%s", type(error).__name__)
            if not started:
                await _respond(send, 500, route)


async def _respond(send: Any, status: int, route: str | None) -> None:
    raw = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", b"0")]
    if status == 429:
        raw.append((b"retry-after", RETRY_AFTER.encode("ascii")))
    await send({"type": "http.response.start", "status": status,
                "headers": _harden(raw, route, status)})
    await send({"type": "http.response.body", "body": b""})


def create_receiver_app(settings: MetaSettings, *, inbox: Inbox | None = None,
                        buckets: TokenBuckets | None = None) -> FastAPI:
    """The receiver application. Without ``inbox`` it writes through
    ``MESSENGER_RECEIVER_DATABASE_URL``; ``buckets`` lets a test bring its own clock.

    Refuses to build without the app secret or the verify token: an HMAC keyed with an empty
    secret is forgeable by anyone, and an empty verify token would match an empty one."""
    missing = [name for name, value in (("META_APP_SECRET", settings.app_secret),
                                        ("META_WEBHOOK_VERIFY_TOKEN", settings.verify_token))
               if not value]
    if inbox is None:
        if settings.receiver_database_url:
            inbox = DatabaseInbox(MetaDatabase(settings.receiver_database_url,
                                               application_name="garage-meta-receiver"))
        else:
            missing.append("MESSENGER_RECEIVER_DATABASE_URL")
    if missing:
        raise MetaConfigError("The Meta receiver needs " + ", ".join(missing))
    receiver = _Receiver(settings, inbox, buckets or TokenBuckets())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_api_route(WEBHOOK_PATH, receiver.handshake, methods=["GET"],
                      include_in_schema=False)
    app.add_api_route(WEBHOOK_PATH, receiver.deliver, methods=["POST"], include_in_schema=False)
    for path in PAGES:
        app.add_api_route(path, receiver.page, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/healthz", _health, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_middleware(_Guard, receiver=receiver)
    app.state.meta_receiver = receiver
    return app


# The only settings the public process may hold. Everything else in .env (the AI keys, the other
# database logins, the token encryption key) stays out of this process, so a flaw here cannot
# reach them: ``MetaSettings.from_env()`` without an argument would load the whole file into
# os.environ.
RECEIVER_KEYS = ("META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN", "MESSENGER_RECEIVER_DATABASE_URL",
                 "MESSENGER_RECEIVER_PORT", "META_GRAPH_API_VERSION")


def receiver_environment(env_file: Path | None = None,
                         environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The receiver's settings from ``.env`` and the process environment, those keys only.

    Reads the file with ``dotenv_values`` (nothing is put into os.environ); a variable set in the
    process environment wins, as ``load_dotenv(override=False)`` would have it.
    """
    from dotenv import dotenv_values

    from automotive.meta_config import ROOT

    path = env_file if env_file is not None else ROOT / ".env"
    environ = os.environ if environ is None else environ
    values = ({key: value for key, value in dotenv_values(path).items()
               if key in RECEIVER_KEYS and value} if path.is_file() else {})
    values.update({key: environ[key] for key in RECEIVER_KEYS if environ.get(key)})
    return values


def main() -> int:
    """``python -m automotive.meta_receiver``: one process on loopback, behind ngrok. It takes
    no options; its settings come from the environment and ``.env``, limited to RECEIVER_KEYS."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = MetaSettings.from_env(receiver_environment())
    except MetaConfigError as error:  # names the variable, never its value
        print(f"Meta receiver not started: {error}", file=sys.stderr)
        return 2
    missing = settings.missing_for_receiver()
    if missing:
        print("Meta receiver not started. Set in .env: " + ", ".join(missing), file=sys.stderr)
        return 2
    import uvicorn

    app = create_receiver_app(settings)
    LOGGER.info("meta_receiver_starting host=%s port=%d", settings.receiver_host,
                settings.receiver_port)
    # No access log: the handshake's query string carries the verify token. proxy_headers off:
    # client_key reads X-Forwarded-For itself, and only from loopback.
    uvicorn.run(app, host=settings.receiver_host, port=settings.receiver_port,
                proxy_headers=False, access_log=False, server_header=False, ws="none",
                lifespan="off", limit_concurrency=64, timeout_keep_alive=5,
                timeout_graceful_shutdown=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
