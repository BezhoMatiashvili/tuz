"""The public Meta webhook receiver: handshake, signatures, inbox rows, pages and limits.

$0 and offline: an in-memory inbox stands in for the database and nothing listens on a port.
The PostgreSQL cases at the end run only with WAREHOUSE_TEST_ADMIN_DATABASE_URL (a disposable
server); they create and drop their own database and login, and skip cleanly without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import unittest
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive import meta_receiver
from automotive.meta_config import MetaConfigError, MetaSettings
from automotive.meta_db import MetaDatabase
from automotive.meta_receiver import (
    BODY_CAP,
    ENTRY_TIME_KEY,
    LIMITS,
    OVERSIZED_KEY,
    PAYLOAD_CAP,
    REPAIRED_KEY,
    DatabaseInbox,
    client_key,
    create_receiver_app,
    event_key,
    jsonb_size,
    parse_delivery,
)
from automotive.site_guard import TokenBuckets
from warehouse_sync.migrations import apply_migrations_on_connection

ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
# The refusals these tests provoke are logged on purpose; assertLogs still sees them.
logging.getLogger("automotive.meta_receiver").addHandler(logging.NullHandler())
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")

SECRET = "0123456789abcdefABCDEF0123456789"  # gitleaks:allow
VERIFY = "verify_token-0123456789-abcdefghijklmnop"
SETTINGS = MetaSettings(app_secret=SECRET, verify_token=VERIFY)
PAGE = "1234567890"
TIME = 1762850000123  # entry.time
TS = 1762849999000  # item timestamp
KEY_FORMAT = re.compile(r"[a-z]{1,12}:[0-9a-f]{64}")  # garage.meta_inbox CHECK


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def digest(identity: Any) -> str:
    """The documented canonical form, written out independently of the module."""
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message(mid: str, text: str = "სალამი, BMW F30 B46-ის ზეთის ფილტრი გაქვთ?",
            psid: str = "111", **extra: Any) -> dict:
    return {"sender": {"id": psid}, "recipient": {"id": PAGE}, "timestamp": TS,
            "message": {"mid": mid, "text": text, **extra}}


def echo(mid: str) -> dict:
    return {"sender": {"id": PAGE}, "recipient": {"id": "111"}, "timestamp": TS,
            "message": {"mid": mid, "is_echo": True, "app_id": 263902037430900,
                        "text": "გამარჯობა"}}


def postback(payload: str = "CHOICE:2", mid: str = "m_pb") -> dict:
    return {"sender": {"id": "111"}, "recipient": {"id": PAGE}, "timestamp": TS,
            "postback": {"title": "Second", "payload": payload, "mid": mid}}


def referral(ref: str = "spring", ad_id: str = "6012345678901") -> dict:
    return {"sender": {"id": "111"}, "recipient": {"id": PAGE}, "timestamp": TS,
            "referral": {"ref": ref, "ad_id": ad_id, "source": "ADS", "type": "OPEN_THREAD"}}


def receipt(kind: str) -> dict:
    return {"sender": {"id": "111"}, "recipient": {"id": PAGE}, "timestamp": TS,
            kind: {"watermark": TS}}


def comment(comment_id: str = "999_555", verb: str = "add", text: str = "ფასი?") -> dict:
    return {"field": "feed", "value": {
        "from": {"id": "333", "name": "Customer"}, "item": "comment", "verb": verb,
        "comment_id": comment_id, "post_id": "999", "parent_id": "999",
        "created_time": 1762849990, "message": text}}


def entry(*, messaging: list | None = None, standby: list | None = None,
          changes: list | None = None, page: Any = PAGE, time: Any = TIME) -> dict:
    found: dict[str, Any] = {"id": page, "time": time}
    for name, items in (("messaging", messaging), ("standby", standby), ("changes", changes)):
        if items is not None:
            found[name] = items
    return found


def delivery(*entries: dict) -> dict:
    return {"object": "page", "entry": list(entries)}


class MemoryInbox:
    """Keeps every appended row; ``error`` makes the next append fail."""

    def __init__(self) -> None:
        self.calls: list[list[tuple]] = []
        self.keys: set[str] = set()
        self.error: Exception | None = None

    def append(self, items: list[tuple]) -> int:
        if self.error is not None:
            raise self.error
        self.calls.append(list(items))
        new = {item[0] for item in items} - self.keys
        self.keys.update(item[0] for item in items)
        return len(new)

    @property
    def rows(self) -> list[tuple]:
        return [row for call in self.calls for row in call]


class Shape(HTMLParser):
    """Enough of an HTML checker for two static pages: every opened element is closed in order,
    and nothing is loaded from anywhere."""

    VOID = {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.loaded: list[str] = []
        self.doctype = ""

    def handle_decl(self, decl: str) -> None:
        self.doctype = decl

    def handle_starttag(self, tag: str, attrs: list) -> None:
        for name, value in attrs:
            if name in ("src", "srcset", "data", "poster", "action", "formaction"):
                self.loaded.append(f"{tag}[{name}]={value}")
            if name == "href" and tag != "a":
                self.loaded.append(f"{tag}[href]={value}")
            if name.startswith("on"):
                self.problems.append(f"{tag}[{name}]")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unexpected </{tag}> with {self.stack[-3:]}")
        else:
            self.stack.pop()


async def _pulse() -> None:
    # As in tests/test_site_guard.py: periodic timers let thread hops finish without sockets.
    while True:
        await asyncio.sleep(0.01)


class ReceiverCase(unittest.IsolatedAsyncioTestCase):
    """The real application around an in-memory inbox."""

    async def asyncSetUp(self) -> None:
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 5.0
        loop.set_debug(False)
        self._heartbeat = asyncio.create_task(_pulse())
        self._clients: list[httpx.AsyncClient] = []
        self.inbox = MemoryInbox()
        # A stopped clock: no bucket refills mid-test, however slow the machine is.
        self.app = create_receiver_app(SETTINGS, inbox=self.inbox,
                                       buckets=TokenBuckets(clock=lambda: 0.0))

    async def asyncTearDown(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat

    def client(self, peer: tuple[str, int] = ("127.0.0.1", 40000), *,
               forwarded: str | None = None, app: Any = None) -> httpx.AsyncClient:
        """A caller as ngrok presents it: from loopback, with the address in X-Forwarded-For."""
        headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app or self.app, client=peer),
            base_url="http://127.0.0.1:8771", headers=headers)
        self._clients.append(client)
        return client

    async def deliver(self, document: Any, *, client: httpx.AsyncClient | None = None,
                      secret: str = SECRET, headers: dict | None = None) -> httpx.Response:
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        sent = {"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json",
                **(headers or {})}
        return await (client or self.client()).post("/meta/webhook", content=body,
                                                     headers=sent)

    async def handshake(self, params: Any, client: httpx.AsyncClient | None = None):
        return await (client or self.client()).get("/meta/webhook", params=params)


class HandshakeTests(ReceiverCase):

    async def test_the_verify_token_gets_the_challenge_back_as_plain_text(self):
        with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
            response = await self.handshake({"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                             "hub.challenge": "1158201444"})
        self.assertEqual((response.status_code, response.text), (200, "1158201444"))
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(logs.output, ["INFO:automotive.meta_receiver:meta_webhook_verified"])

    async def test_anything_but_an_exact_subscribe_with_the_token_is_403(self):
        good = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"}
        refused = [
            {**good, "hub.verify_token": VERIFY[:-1]},
            {**good, "hub.verify_token": VERIFY + "x"},
            {**good, "hub.verify_token": ""},
            {**good, "hub.verify_token": "ვერიფიკაცია"},  # non-ASCII must not become a 500
            {**good, "hub.mode": "unsubscribe"},
            {**good, "hub.mode": "Subscribe"},
            {**good, "hub.challenge": "<script>alert(1)</script>"},
            {**good, "hub.challenge": "42\n"},  # "$" would have let this through
            {**good, "hub.challenge": "x" * 129},
            {**good, "hub.challenge": ""},
            {key: value for key, value in good.items() if key != "hub.challenge"},
            {key: value for key, value in good.items() if key != "hub.verify_token"},
            {},
            [*good.items(), ("hub.verify_token", VERIFY)],  # repeated: ambiguous
            [*good.items(), ("hub.challenge", "43")],
        ]
        for params in refused:
            response = await self.handshake(params)
            self.assertEqual((response.status_code, response.content), (403, b""), params)
        accepted = await self.handshake({**good, "hub.challenge": "a-Z_9" * 25 + "abc"})
        self.assertEqual(accepted.status_code, 200)


class SignatureTests(ReceiverCase):

    async def test_a_signed_delivery_is_stored_and_acknowledged(self):
        document = delivery(entry(messaging=[message("m_1")]))
        with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
            response = await self.deliver(document)
        self.assertEqual((response.status_code, response.text), (200, "EVENT_RECEIVED"))
        self.assertEqual(len(self.inbox.calls), 1)
        key, field, page_id, payload = self.inbox.rows[0]
        self.assertEqual((key, field, page_id), ("msg:" + digest(["m_1"]), "messages", PAGE))
        self.assertEqual(payload, {**message("m_1"), ENTRY_TIME_KEY: TIME})
        self.assertEqual(logs.output, [
            "INFO:automotive.meta_receiver:meta_webhook_received items=1 rows=1 new=1 "
            "duplicates=0 receipts=0 unusable=0 oversized=0 repaired=0"])
        # No message text, ids or tokens in the log line.
        self.assertNotIn("სალამი", logs.output[0])
        self.assertNotIn("111", logs.output[0])

    async def test_missing_malformed_or_wrong_signatures_are_401_with_nothing_said(self):
        body = json.dumps(delivery(entry(messaging=[message("m_1")]))).encode()
        right = sign(body)
        cases = {
            "missing": {},
            "empty": {"X-Hub-Signature-256": ""},
            "sha1": {"X-Hub-Signature-256": "sha1=" + right[7:47]},
            "short": {"X-Hub-Signature-256": right[:-1]},
            "long": {"X-Hub-Signature-256": right + "0"},
            "newline": {"X-Hub-Signature-256": right + "\n"},
            "spaces": {"X-Hub-Signature-256": "sha256= " + right[7:]},
            "wrong secret": {"X-Hub-Signature-256": sign(body, SECRET[::-1])},
            "other body": {"X-Hub-Signature-256": sign(body + b" ")},
            "not ascii": {"X-Hub-Signature-256": "sha256=" + "é" * 64},
        }
        client = self.client()
        for name, headers in cases.items():
            try:
                response = await client.post("/meta/webhook", content=body, headers=headers)
            except (httpx.LocalProtocolError, UnicodeEncodeError):
                continue  # the client refused to send it; nothing to test
            self.assertEqual((response.status_code, response.content), (401, b""), name)
        two = httpx.Headers([("X-Hub-Signature-256", right), ("X-Hub-Signature-256", right)])
        response = await client.post("/meta/webhook", content=body, headers=two)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.inbox.calls, [])
        # Upper-case hex is the same signature.
        response = await client.post("/meta/webhook", content=body, headers={
            "X-Hub-Signature-256": "sha256=" + right[7:].upper()})
        self.assertEqual(response.status_code, 200)

    async def test_a_raw_byte_header_is_refused_not_a_crash(self):
        body = json.dumps(delivery()).encode()
        response = await self.client().post("/meta/webhook", content=body, headers=[
            (b"X-Hub-Signature-256", b"sha256=" + b"\xff" * 64)])
        self.assertEqual(response.status_code, 401)

    async def test_bodies_over_one_megabyte_are_413_and_never_stored(self):
        big = b"{" + b" " * BODY_CAP + b"}"
        declared = await self.deliver(big)
        self.assertEqual(declared.status_code, 413)

        async def chunks():
            for _ in range(3):
                yield b" " * 400_000

        streamed = await self.client().post("/meta/webhook", content=chunks(), headers={
            "X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(streamed.status_code, 413)
        self.assertEqual(self.inbox.calls, [])
        # Exactly the cap is read and handled.
        head = '{"object":"page","entry":[],"pad":"'
        exact = (head + "x" * (BODY_CAP - len(head) - 2) + '"}').encode()
        self.assertEqual(len(exact), BODY_CAP)
        self.assertEqual((await self.deliver(exact)).status_code, 200)

    async def test_a_body_that_never_finishes_is_408(self):
        async def trickle():
            yield b"{"
            await asyncio.sleep(1)

        with patch.object(meta_receiver, "BODY_SECONDS", 0.05):
            response = await self.client().post("/meta/webhook", content=trickle(), headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(response.status_code, 408)
        # Meta sends a delivery at once; a slow upload only holds one of the 64 slots.
        self.assertLessEqual(meta_receiver.BODY_SECONDS, 3.0)


class SlowUploadTests(ReceiverCase):
    """A client with a well-formed but made-up signature cannot fill the server's 64 slots by
    sending bodies slowly: few unverified uploads per address, and every refusal ends the
    connection, so a half-sent body cannot keep it."""

    FAKE = {"X-Hub-Signature-256": "sha256=" + "0" * 64}

    async def raw(self, messages: list[dict], *, forwarded: str,
                  headers: tuple = ()) -> tuple[int, dict]:
        """One POST straight to the ASGI app, its body as ``messages``."""
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/meta/webhook",
                 "raw_path": b"/meta/webhook", "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"127.0.0.1:8771"),
                             (b"x-forwarded-for", forwarded.encode()), *headers],
                 "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8771)}
        pending = iter(messages)
        sent: list[dict] = []

        async def receive() -> dict:
            return next(pending)

        async def send(message: dict) -> None:
            sent.append(message)

        await self.app(scope, receive, send)
        return sent[0]["status"], {name.decode(): value.decode()
                                   for name, value in sent[0]["headers"]}

    async def test_one_address_may_hold_only_a_few_unverified_uploads(self):
        receiver = self.app.state.meta_receiver
        release = asyncio.Event()

        async def slow():
            yield b"{"
            await release.wait()

        attacker = self.client(forwarded="203.0.113.60")
        held = [asyncio.create_task(attacker.post("/meta/webhook", content=slow(),
                                                  headers=self.FAKE))
                for _ in range(meta_receiver.UNVERIFIED_READS)]
        for _ in range(500):
            if sum(receiver._reading.values()) == meta_receiver.UNVERIFIED_READS:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(receiver._reading, {"203.0.113.60": meta_receiver.UNVERIFIED_READS})
        refused = await attacker.post("/meta/webhook", content=b"{}", headers=self.FAKE)
        self.assertEqual((refused.status_code, refused.headers["retry-after"],
                          refused.headers["connection"]), (429, "60", "close"))
        # Meta's own delivery, from its own address, is not held up.
        signed = await self.deliver(delivery(entry(messaging=[message("m_meta")])),
                                    client=self.client(forwarded="157.240.0.1"))
        self.assertEqual(signed.status_code, 200)
        release.set()
        codes = [response.status_code for response in await asyncio.gather(*held)]
        self.assertEqual(codes, [401] * meta_receiver.UNVERIFIED_READS)
        self.assertEqual(receiver._reading, {})

    async def test_a_client_that_drops_its_upload_is_charged_as_a_failure(self):
        dropped = [{"type": "http.request", "body": b"{", "more_body": True},
                   {"type": "http.disconnect"}]
        headers = ((b"x-hub-signature-256", b"sha256=" + b"0" * 64),
                   (b"content-length", b"100"))
        with patch.dict(LIMITS, {"failed": (2, 0.001)}):
            for _ in range(2):
                status, _ = await self.raw(dropped, forwarded="203.0.113.70", headers=headers)
                self.assertEqual(status, 400)
            status, sent = await self.raw([{"type": "http.request", "body": b"{}"}],
                                          forwarded="203.0.113.70")
        self.assertEqual((status, sent["retry-after"]), (429, "60"))
        self.assertEqual(self.app.state.meta_receiver._reading, {})

    async def test_every_refusal_closes_the_connection_and_answers_keep_it(self):
        client = self.client(forwarded="203.0.113.80")

        async def trickle():
            yield b"{"
            await asyncio.sleep(1)

        with patch.object(meta_receiver, "BODY_SECONDS", 0.05):
            timed_out = await client.post("/meta/webhook", content=trickle(), headers=self.FAKE)
        refusals = {
            "missing": await client.post("/meta/webhook", content=b"{}"),
            "bad": await client.post("/meta/webhook", content=b"{}", headers=self.FAKE),
            "large": await self.deliver(b"{" + b" " * BODY_CAP + b"}", client=client),
            "slow": timed_out,
            "unknown": await client.post("/wp-login.php", content=b"x" * 10),
            "handshake": await self.handshake({"hub.mode": "subscribe"}, client),
        }
        self.assertEqual({name: response.status_code for name, response in refusals.items()},
                         {"missing": 401, "bad": 401, "large": 413, "slow": 408,
                          "unknown": 404, "handshake": 403})
        for name, response in refusals.items():
            self.assertEqual(response.headers.get("connection"), "close", name)
        for answer in (await self.deliver(delivery(entry(messaging=[message("m_ok")]))),
                       await client.get("/privacy"), await client.get("/healthz")):
            self.assertEqual(answer.status_code, 200)
            self.assertNotIn("connection", answer.headers)


class PayloadTests(ReceiverCase):

    async def test_signed_bodies_that_are_not_page_deliveries_are_400(self):
        deep = {"mid": "m_deep"}
        for _ in range(60):
            deep = {"next": deep}
        bad = [
            b"", b"not json", b"\xff\xfe{}", b"[]", b'"page"', b"{}",
            b'{"object": 1, "entry": []}', b'{"object": "page"}',
            b'{"object": "page", "entry": {}}',
            b"[" * 100_000 + b"]" * 100_000,  # deeper than the parser goes
            json.dumps(delivery(entry(messaging=[message("m_deep", nested=deep)]))).encode(),
        ]
        for body in bad:
            response = await self.deliver(body)
            self.assertEqual((response.status_code, response.content), (400, b""), body[:40])
        self.assertEqual(self.inbox.calls, [])

    async def test_other_objects_are_acknowledged_and_ignored(self):
        for document in ({"object": "instagram", "entry": [entry(messaging=[message("m_1")])]},
                         {"object": "permissions", "entry": "anything"}):
            response = await self.deliver(document)
            self.assertEqual((response.status_code, response.text), (200, "EVENT_RECEIVED"))
        self.assertEqual(self.inbox.calls, [])

    async def test_every_kind_of_item_gets_its_own_key_and_field(self):
        reaction = {"sender": {"id": "111"}, "recipient": {"id": PAGE}, "timestamp": TS,
                    "reaction": {"mid": "m_1", "action": "react", "reaction": "love"}}
        standby_message = message("m_sb")
        document = delivery(entry(
            messaging=[message("m_1"), echo("m_echo"), postback(), referral(),
                       receipt("read"), receipt("delivery"), reaction],
            standby=[standby_message, receipt("read")],
            changes=[comment()]))
        response = await self.deliver(document)
        self.assertEqual(response.status_code, 200)
        rows = self.inbox.rows
        self.assertEqual([row[0].split(":")[0] for row in rows],
                         ["msg", "echo", "pb", "ref", "raw", "sb", "chg"])
        self.assertEqual([row[1] for row in rows],
                         ["messages"] * 5 + ["standby", "feed"])
        self.assertEqual({row[2] for row in rows}, {PAGE})
        for row in rows:
            self.assertRegex(row[0], KEY_FORMAT)
            self.assertEqual(row[3][ENTRY_TIME_KEY], TIME)
        keys = [row[0] for row in rows]
        self.assertEqual(event_key("msg", ["m_1"]), keys[0])
        self.assertEqual(keys, [
            "msg:" + digest(["m_1"]),
            "echo:" + digest(["m_echo"]),
            "pb:" + digest(["111", TS, "CHOICE:2", "m_pb"]),
            "ref:" + digest(["111", TS, "spring", "6012345678901"]),
            "raw:" + digest(reaction),
            "sb:" + digest(standby_message),
            "chg:" + digest(["feed", "999_555", "999", "comment", "add", 1762849990]),
        ])
        self.assertEqual(rows[6][3], {**comment(), ENTRY_TIME_KEY: TIME})

    async def test_a_redelivery_or_repeat_keeps_its_key(self):
        first = delivery(entry(messaging=[message("m_1"), message("m_1")]))
        later = delivery(entry(messaging=[postback(), message("m_1")], time=TIME + 60_000))
        with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
            self.assertEqual((await self.deliver(first)).status_code, 200)
            self.assertEqual((await self.deliver(later)).status_code, 200)
        self.assertEqual(len(self.inbox.calls[0]), 1)  # the repeat inside one delivery
        self.assertEqual(self.inbox.calls[0][0][0], self.inbox.calls[1][1][0])
        self.assertEqual(self.inbox.calls[1][1][3][ENTRY_TIME_KEY], TIME + 60_000)
        self.assertIn("rows=1 new=1 duplicates=1", logs.output[0])
        self.assertIn("rows=2 new=1 duplicates=0", logs.output[1])

    async def test_different_events_never_share_a_key(self):
        items = [postback("CHOICE:1"), postback("CHOICE:2"), postback("CHOICE:1", mid="m_pb2"),
                 referral("spring"), referral("autumn"), referral("spring", ad_id="1"),
                 message("m_a"), echo("m_a")]
        changes = [comment(), comment(verb="edited"), comment(comment_id="999_556")]
        await self.deliver(delivery(entry(messaging=items, changes=changes)))
        keys = [row[0] for row in self.inbox.rows]
        self.assertEqual(len(keys), len(items) + len(changes))
        self.assertEqual(len(set(keys)), len(keys))

    async def test_entries_and_items_the_table_cannot_hold_are_skipped_or_renamed(self):
        odd_change = {"field": "Feed-X", "value": {"item": "status", "verb": "add"}}
        document = delivery(
            entry(messaging=[message("m_ok")]),
            entry(messaging=[message("m_int")], page=int(PAGE)),
            entry(messaging=[message("m_bad")], page="12ab"),
            entry(messaging=[message("m_none")], page=None),
            entry(messaging=[message("m_bool")], page=True),
            entry(messaging=[message("m_time")], time="yesterday"),
            "not an entry",
            entry(messaging=["not an item", 7, message("m_after")], changes=[odd_change]),
            entry(messaging={"not": "a list"}),
        )
        with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
            response = await self.deliver(document)
        self.assertEqual(response.status_code, 200)
        rows = {row[3].get("message", {}).get("mid", "change"): row for row in self.inbox.rows}
        self.assertEqual(set(rows), {"m_ok", "m_int", "m_time", "m_after", "change"})
        self.assertEqual(rows["m_int"][2], PAGE)
        self.assertNotIn(ENTRY_TIME_KEY, rows["m_time"][3])
        self.assertEqual(rows["change"][1], "unknown")
        self.assertEqual(rows["change"][3]["field"], "Feed-X")
        self.assertIn("unusable=7", logs.output[0])

    async def test_an_oversized_item_becomes_a_marker_with_its_ids_and_no_text(self):
        long = message("m_long", text="ა" * 25_000, psid="222")
        big_comment = comment(text="ტ" * 25_000)
        response = await self.deliver(delivery(entry(messaging=[long], changes=[big_comment])))
        self.assertEqual(response.status_code, 200)
        (key, field, _, marker), (comment_key, _, _, comment_marker) = self.inbox.rows
        self.assertEqual((key, field), ("msg:" + digest(["m_long"]), "messages"))
        self.assertGreater(marker["_bytes"], PAYLOAD_CAP)
        self.assertEqual(marker, {
            OVERSIZED_KEY: True, "_bytes": marker["_bytes"], "_kind": "msg",
            ENTRY_TIME_KEY: TIME,
            "ids": {"sender": "222", "recipient": PAGE, "timestamp": TS, "mid": "m_long"}})
        self.assertEqual(comment_marker["ids"], {
            "sender": "333", "field": "feed", "comment_id": "999_555", "post_id": "999",
            "parent_id": "999", "item": "comment", "verb": "add", "created_time": 1762849990})
        self.assertEqual(comment_key, "chg:" + digest(
            ["feed", "999_555", "999", "comment", "add", 1762849990]))
        for stored in (marker, comment_marker):
            text = json.dumps(stored, ensure_ascii=False)
            self.assertNotIn("ა", text)
            self.assertNotIn("ტ", text)
            self.assertLess(jsonb_size(stored), 1_000)

    async def test_the_cap_is_measured_the_way_postgres_prints_jsonb(self):
        # Compact JSON is under the cap, but jsonb puts a space after every comma: a marker.
        spaced = message("m_list", attachments=[0] * 25_000)
        compact = len(json.dumps({**spaced, ENTRY_TIME_KEY: TIME}, separators=(",", ":")))
        self.assertLess(compact, PAYLOAD_CAP)
        # Escaped JSON is over the cap, but jsonb keeps Georgian as 3-byte UTF-8: kept whole.
        georgian = message("m_ka", text="ა" * 20_000)
        self.assertGreater(len(json.dumps(georgian)), PAYLOAD_CAP)
        await self.deliver(delivery(entry(messaging=[spaced, georgian])))
        first, second = (row[3] for row in self.inbox.rows)
        self.assertTrue(first[OVERSIZED_KEY])
        self.assertEqual(second["message"]["text"], "ა" * 20_000)
        self.assertLessEqual(jsonb_size(second), PAYLOAD_CAP)

    def test_jsonb_size_counts_like_postgres(self):
        samples = {
            '{"a": 1, "b": [1, 2]}': {"a": 1, "b": [1, 2]},
            '{}': {}, '[]': [], 'null': None, 'true': True, 'false': False,
            '"ა"': "ა", '"\\n"': "\n", '"\\u0001"': "\x01", '"😀"': "😀",
            '10000000000000000': 1e16, '0.00000015': 1.5e-07, '0.1': 0.1,
            '{"x": {}, "y": [[], {}]}': {"x": {}, "y": [[], {}]},
        }
        for text, value in samples.items():
            self.assertEqual(jsonb_size(value), len(text.encode("utf-8")), text)

    async def test_text_postgres_cannot_hold_is_repaired_not_refused(self):
        # Written by hand: json.dumps would never produce these.
        body = ('{"object":"page","entry":[{"id":"PAGE","time":1,"messaging":[{"sender":'
                '{"id":"1"},"recipient":{"id":"PAGE"},"timestamp":1,"message":{"mid":"m_nul",'
                '"text":"a\\u0000b\\ud800c","score":NaN,"big":1e999}}]}]}'
                ).replace("PAGE", PAGE).encode()
        for _ in range(2):  # the same key both times
            with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
                self.assertEqual((await self.deliver(body)).status_code, 200)
            self.assertIn("repaired=1", logs.output[0])
        first, second = self.inbox.calls
        self.assertEqual(first[0][0], second[0][0])
        payload = first[0][3]
        self.assertEqual(payload["message"], {"mid": "m_nul", "text": "a�b�c",
                                              "score": None, "big": None})
        self.assertIs(payload[REPAIRED_KEY], True)
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")  # storable

    async def test_an_inbox_failure_is_503_and_logs_no_message_text(self):
        vin = "WBA8E9C50GK123456"
        document = delivery(entry(messaging=[message("m_1", text=f"ჩემი VIN {vin}")]))
        failures = {
            "RuntimeError sqlstate=-": RuntimeError(f"could not store {vin}"),
            # A CHECK violation's detail prints the failing row, the message text included.
            "CheckViolation sqlstate=23514": errors.CheckViolation(f"Failing row ({vin})"),
            "OperationalError sqlstate=-": psycopg.OperationalError("connection failed"),
        }
        for logged, failure in failures.items():
            self.inbox.error = failure
            with self.assertLogs("automotive.meta_receiver", "INFO") as logs:
                response = await self.deliver(document)
            self.assertEqual((response.status_code, response.content), (503, b""))
            self.assertEqual(response.headers["retry-after"], "60")
            self.assertEqual(logs.output, [
                f"ERROR:automotive.meta_receiver:meta_inbox_unavailable error={logged}"])
        self.inbox.error = None
        self.assertEqual((await self.deliver(document)).status_code, 200)

    def test_parse_delivery_reports_what_it_skipped(self):
        body = json.dumps(delivery(
            entry(messaging=[message("m_1"), receipt("read")], standby=[receipt("delivery")]),
            entry(page="x"))).encode()
        parsed = parse_delivery(body)
        self.assertEqual((parsed.items, len(parsed.rows), parsed.receipts, parsed.unusable),
                         (3, 1, 2, 1))


class ResponseTests(ReceiverCase):

    async def collect(self) -> dict[str, httpx.Response]:
        client = self.client(forwarded="203.0.113.50")
        found = {
            "delivered": await self.deliver(delivery(), client=client),
            "unsigned": await client.post("/meta/webhook", content=b"{}"),
            "unknown": await client.get("/wp-login.php"),
            "refused": await self.handshake({"hub.mode": "subscribe"}, client),
            "health": await client.get("/healthz"),
            "privacy": await client.get("/privacy"),
            "deletion": await client.get("/data-deletion/en"),
        }
        with patch.dict(LIMITS, {"page": (1, 0.001)}):
            await self.client(forwarded="203.0.113.51").get("/healthz")
            found["limited"] = await self.client(forwarded="203.0.113.51").get("/privacy")
        return found

    async def test_every_response_carries_the_security_headers(self):
        responses = await self.collect()
        self.assertEqual(responses["limited"].status_code, 429)
        for name, response in responses.items():
            headers = response.headers
            self.assertEqual(headers["x-content-type-options"], "nosniff", name)
            self.assertEqual(headers["referrer-policy"], "no-referrer", name)
            self.assertEqual(headers["x-frame-options"], "DENY", name)
            self.assertIn("default-src 'none'", headers["content-security-policy"], name)
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"], name)
            self.assertNotIn("set-cookie", headers, name)
            self.assertNotIn("server", headers, name)
            self.assertFalse([key for key in headers if key.startswith("access-control-")])
            page = name in ("privacy", "deletion")
            self.assertEqual("style-src 'unsafe-inline'" in headers["content-security-policy"],
                             page, name)
            self.assertEqual(headers["cache-control"],
                             "public, max-age=300" if page else "no-store", name)

    def test_headers_from_lower_layers_cannot_add_cookies_cors_or_a_banner(self):
        raw = meta_receiver._harden([(b"server", b"uvicorn"), (b"Set-Cookie", b"a=b"),
                                     (b"access-control-allow-origin", b"*"),
                                     (b"content-type", b"text/plain")], None, 200)
        names = [name for name, _ in raw]
        self.assertIn(b"content-type", names)
        for dropped in (b"server", b"set-cookie", b"access-control-allow-origin"):
            self.assertNotIn(dropped, [name.lower() for name in names])

    async def test_an_unexpected_error_is_a_bare_500_with_the_headers(self):
        with (patch.object(meta_receiver, "parse_delivery", side_effect=KeyError("text")),
              self.assertLogs("automotive.meta_receiver", "ERROR") as logs):
            response = await self.deliver(delivery())
        self.assertEqual((response.status_code, response.content), (500, b""))
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(logs.output, ["ERROR:automotive.meta_receiver:meta_receiver_error "
                                       "error=KeyError"])

    async def test_pages_render_in_both_languages_without_external_assets(self):
        client = self.client()
        expected = {"/privacy": ("ka", "კონფიდენციალურობის პოლიტიკა", "/data-deletion"),
                    "/privacy/en": ("en", "Privacy policy", "/data-deletion/en"),
                    "/data-deletion": ("ka", "მონაცემების წაშლა", "/privacy"),
                    "/data-deletion/en": ("en", "Delete your data", "/privacy/en")}
        for path, (language, heading, other_page) in expected.items():
            response = await client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
            html = response.text
            self.assertTrue(html.startswith("<!doctype html>"), path)
            self.assertIn(f'<html lang="{language}">', html)
            self.assertIn(f"<h1>{heading}</h1>", html)
            self.assertIn(f'href="{other_page}"', html)
            switch = path.removesuffix("/en") if language == "en" else path + "/en"
            self.assertIn(f'href="{switch}"', html)
            self.assertIn("+995 596 33 33 74", html)
            self.assertIn('href="tel:+995596333374"', html)
            self.assertIn("ქავთარაძის ქ. N 17" if language == "ka" else "17 Kavtaradze", html)
            self.assertIn(str(meta_receiver.SHOP["answer_days"]), html)
            shape = Shape()
            shape.feed(html)
            shape.close()
            self.assertEqual(shape.doctype, "doctype html", path)
            self.assertEqual((shape.stack, shape.problems, shape.loaded), ([], [], []), path)
            for banned in ("<script", "<link", "<img", "<iframe", "url(", "@import", "{{",
                           "{%"):
                self.assertNotIn(banned, html, path)
            # http(s) appears only as the target of a link the reader may follow.
            self.assertEqual(len(re.findall(r"https?://", html)),
                             len(re.findall(r'<a href="https://[^"\s]+">', html)), path)
            head = await client.head(path)
            self.assertEqual((head.status_code, head.content), (200, b""), path)
        privacy = (await client.get("/privacy")).text
        for processor in ("Meta Platforms", "OpenRouter", "Anthropic",
                          "სანამ მის წაშლას არ მოგვთხოვთ"):
            self.assertIn(processor, privacy)
        self.assertIn("until you ask us to delete them", (await client.get("/privacy/en")).text)

    async def test_health_answers_ok(self):
        client = self.client()
        self.assertEqual((await client.get("/healthz")).text, "ok")
        self.assertEqual((await client.head("/healthz")).status_code, 200)

    async def test_anything_else_is_404(self):
        client = self.client()
        attempts = [("GET", path) for path in (
            "/", "/docs", "/redoc", "/openapi.json", "/privacy/", "/PRIVACY", "/privacy/en/",
            "/privacy/ka", "/meta/webhook/", "/meta", "/healthz/", "/.env", "/static/x.js")]
        attempts += [(method, "/meta/webhook") for method in
                     ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")]
        attempts += [("POST", "/privacy"), ("POST", "/healthz"), ("DELETE", "/privacy/en")]
        for method, path in attempts:
            response = await client.request(method, path, headers={
                "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
            self.assertEqual((response.status_code, response.content), (404, b""),
                             f"{method} {path}")
            self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertEqual(self.inbox.calls, [])


class RateLimitTests(ReceiverCase):

    async def test_failed_signatures_from_one_address_turn_into_429(self):
        body = json.dumps(delivery()).encode()
        bad = {"X-Hub-Signature-256": sign(body, "x" * 32)}
        attacker = self.client(forwarded="203.0.113.9")
        codes = [(await attacker.post("/meta/webhook", content=body, headers=bad)).status_code
                 for _ in range(LIMITS["failed"][0] + 1)]
        self.assertEqual(codes, [401] * LIMITS["failed"][0] + [429])
        limited = await attacker.post("/meta/webhook", content=body, headers=bad)
        self.assertEqual((limited.status_code, limited.headers["retry-after"]), (429, "60"))
        self.assertEqual((await attacker.get("/nothing-here")).status_code, 429)
        self.assertEqual((await self.handshake({"hub.mode": "x"}, attacker)).status_code, 429)
        # Another address is unaffected.
        other = self.client(forwarded="198.51.100.7")
        self.assertEqual((await other.post("/meta/webhook", content=body,
                                           headers=bad)).status_code, 401)
        # A delivery that verifies is never refused because of failures.
        self.assertEqual((await self.deliver(delivery(entry(messaging=[message("m_1")])),
                                             client=attacker)).status_code, 200)
        self.assertEqual(len(self.inbox.rows), 1)

    async def test_unknown_paths_are_limited_the_same_way(self):
        scanner = self.client(forwarded="203.0.113.10")
        codes = [(await scanner.get(f"/probe/{index}")).status_code
                 for index in range(LIMITS["failed"][0] + 1)]
        self.assertEqual(codes, [404] * LIMITS["failed"][0] + [429])
        self.assertEqual((await scanner.get("/privacy")).status_code, 200)  # pages have their own

    async def test_the_last_forwarded_hop_is_the_caller(self):
        body = json.dumps(delivery()).encode()
        bad = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
        codes = []
        for index in range(LIMITS["failed"][0] + 1):
            # Whatever the caller puts in front, ngrok's own hop comes last.
            client = self.client(forwarded=f"10.0.0.{index}, 203.0.113.11")
            response = await client.post("/meta/webhook", content=body, headers=bad)
            codes.append(response.status_code)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(client_key({"type": "http", "client": ("127.0.0.1", 1), "headers": [
            (b"x-forwarded-for", b"1.1.1.1, 2.2.2.2"),
            (b"x-forwarded-for", b"3.3.3.3")]}), "3.3.3.3")
        self.assertEqual(client_key({"type": "http", "client": ("127.0.0.1", 1), "headers": [
            (b"x-forwarded-for", b"2001:db8:1:2::5")]}), "2001:db8:1:2::/64")
        self.assertEqual(client_key({"type": "http", "client": ("127.0.0.1", 1), "headers": [
            (b"x-forwarded-for", b"garbage")]}), "127.0.0.1")

    async def test_forwarded_for_from_any_other_peer_is_ignored(self):
        body = json.dumps(delivery()).encode()
        bad = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
        codes = []
        for index in range(LIMITS["failed"][0] + 1):
            client = self.client(("198.51.100.20", 5000), forwarded=f"10.0.1.{index}")
            codes.append((await client.post("/meta/webhook", content=body,
                                            headers=bad)).status_code)
        self.assertEqual(codes[-1], 429)

    async def test_without_forwarded_for_everyone_shares_loopback_and_meta_still_gets_in(self):
        body = json.dumps(delivery()).encode()
        local = self.client()
        for _ in range(LIMITS["failed"][0] + 1):
            await local.post("/meta/webhook", content=body,
                             headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual((await local.get("/unknown")).status_code, 429)
        response = await self.deliver(delivery(entry(messaging=[message("m_meta")])),
                                      client=local)
        self.assertEqual(response.status_code, 200)
        accepted = await self.handshake({"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                         "hub.challenge": "7"}, local)
        self.assertEqual(accepted.status_code, 200)

    async def test_signed_deliveries_have_their_own_generous_limit(self):
        self.assertEqual(LIMITS["signed"], (600, 600))
        with patch.dict(LIMITS, {"signed": (2, 0.001)}):
            codes = [(await self.deliver(delivery(entry(messaging=[message(f"m_{index}")])),
                                         client=self.client(forwarded="157.240.0.1"))
                      ).status_code for index in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(len(self.inbox.rows), 2)

    async def test_pages_have_a_limit_of_their_own(self):
        with patch.dict(LIMITS, {"page": (2, 0.001)}):
            reader = self.client(forwarded="203.0.113.12")
            codes = [(await reader.get("/privacy")).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class StartupTests(unittest.TestCase):

    def test_the_app_refuses_to_build_without_its_secrets(self):
        inbox = MemoryInbox()
        with self.assertRaises(MetaConfigError) as raised:
            create_receiver_app(MetaSettings(), inbox=inbox)
        self.assertEqual(str(raised.exception),
                         "The Meta receiver needs META_APP_SECRET, META_WEBHOOK_VERIFY_TOKEN")
        with self.assertRaises(MetaConfigError) as raised:
            create_receiver_app(SETTINGS)
        self.assertEqual(str(raised.exception),
                         "The Meta receiver needs MESSENGER_RECEIVER_DATABASE_URL")
        app = create_receiver_app(MetaSettings(app_secret=SECRET, verify_token=VERIFY,
                                               receiver_database_url="postgresql://x@127.0.0.1:1/x"))
        self.assertIsInstance(app.state.meta_receiver.inbox, DatabaseInbox)
        self.assertIsNone(app.openapi_url)

    def run_main(self, settings: Any) -> tuple[int, str, Any]:
        loader = (patch.object(MetaSettings, "from_env", side_effect=settings)
                  if isinstance(settings, Exception)
                  else patch.object(MetaSettings, "from_env", return_value=settings))
        stderr = io.StringIO()
        with (loader, patch("uvicorn.run") as run, patch("logging.basicConfig"),
              contextlib.redirect_stderr(stderr)):
            code = meta_receiver.main()
        return code, stderr.getvalue(), run

    def test_main_names_what_is_missing_and_does_not_start(self):
        code, said, run = self.run_main(MetaSettings(app_secret=SECRET))
        self.assertEqual(code, 2)
        self.assertIn("META_WEBHOOK_VERIFY_TOKEN, MESSENGER_RECEIVER_DATABASE_URL", said)
        self.assertNotIn(SECRET, said)
        run.assert_not_called()
        code, said, run = self.run_main(MetaConfigError("META_APP_SECRET must be the App Secret"))
        self.assertEqual(code, 2)
        self.assertIn("META_APP_SECRET must be the App Secret", said)
        run.assert_not_called()

    def test_main_serves_one_loopback_process_without_an_access_log(self):
        settings = MetaSettings(app_secret=SECRET, verify_token=VERIFY, receiver_port=59871,
                                receiver_database_url="postgresql://x@127.0.0.1:1/x")
        code, said, run = self.run_main(settings)
        self.assertEqual((code, said), (0, ""))
        run.assert_called_once()
        options = run.call_args.kwargs
        self.assertEqual({key: options[key] for key in (
            "host", "port", "proxy_headers", "access_log", "server_header", "ws")},
            {"host": "127.0.0.1", "port": 59871, "proxy_headers": False, "access_log": False,
             "server_header": False, "ws": "none"})
        self.assertNotIn("workers", options)
        self.assertNotIn("reload", options)

    def test_the_dashboard_bounds_for_bot_records_agree_and_no_page_names_a_cut(self):
        """Conversations are kept until deletion, so the pages promise no number of days;
        the dashboard, the API and the store agree on the bot records' 30-365."""
        from automotive.meta_store import SETTINGS_FIELDS
        from automotive.site_meta_admin import SettingsChange

        self.assertNotIn("retention_days", meta_receiver.SHOP)
        page = (ROOT / "automotive/templates/meta/privacy.html").read_text(encoding="utf-8")
        self.assertNotIn("retention_days", page)
        days = 365
        self.assertEqual(SETTINGS_FIELDS["retention_days"][2], days)
        bounds = SettingsChange.model_fields["retention_days"].metadata
        self.assertIn(days, [getattr(item, "le", None) for item in bounds])
        script = (ROOT / "automotive/static/admin/js/views/facebook-status.js").read_text(
            encoding="utf-8")
        self.assertIn(f"{{ name: 'retention_days', min: 30, max: {days} }}", script)

    def test_the_tunnel_keeps_no_copy_of_the_requests(self):
        # ngrok's inspector (127.0.0.1:4040) would keep recent deliveries, customer text and
        # the handshake's verify token, readable by anything on the laptop.
        unit = (ROOT / "ops/systemd/garage-ngrok.service").read_text(encoding="utf-8")
        (command,) = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertIn(" --inspect=false", command)


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class ReceiverDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The real DatabaseInbox over a real login that is a member of garage_webhook only."""

    @classmethod
    def setUpClass(cls) -> None:
        suffix = uuid.uuid4().hex[:12]
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.database = "meta_receiver_" + suffix
        cls.login = "meta_hook_" + suffix
        cls.addClassCleanup(cls._drop)
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.database
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        password = secrets.token_urlsafe(24)
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(cls.login), sql.Literal(password)))
        cls.admin.execute(sql.SQL("GRANT garage_webhook TO {}").format(
            sql.Identifier(cls.login)))
        info.update(user=cls.login, password=password)
        cls.hook_url = make_conninfo(**info)

    @classmethod
    def _drop(cls) -> None:
        cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(cls.database)))
        cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(cls.login)))
        cls.admin.close()

    def setUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute("TRUNCATE garage.meta_inbox RESTART IDENTITY")
        self.inbox = DatabaseInbox(MetaDatabase(self.hook_url))

    def stored(self) -> list[tuple]:
        with psycopg.connect(self.url) as connection:
            return connection.execute(
                "SELECT event_key, field, page_id, payload, processed_at, attempts "
                "FROM garage.meta_inbox ORDER BY inbox_id").fetchall()

    def rows_of(self, document: dict) -> list[tuple]:
        return parse_delivery(json.dumps(document).encode()).rows

    def test_the_login_is_a_member_of_garage_webhook_only(self):
        memberships = [row[0] for row in self.admin.execute(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s", (self.login,))]
        self.assertEqual(memberships, ["garage_webhook"])

    def test_the_receiver_login_appends_and_wakes_the_worker(self):
        rows = self.rows_of(delivery(entry(messaging=[message("m_1"), postback()],
                                           changes=[comment()])))
        with psycopg.connect(self.url, autocommit=True) as listener:
            listener.execute("LISTEN garage_meta_inbox")
            self.assertEqual(self.inbox.append(rows), 3)
            woken = list(listener.notifies(timeout=5, stop_after=1))
        self.assertEqual([(note.channel, note.payload) for note in woken],
                         [("garage_meta_inbox", "")])
        stored = self.stored()
        self.assertEqual([row[:4] for row in stored], [tuple(row) for row in rows])
        self.assertEqual({(row[4], row[5]) for row in stored}, {(None, 0)})

    def test_duplicates_are_silent_across_and_within_calls(self):
        rows = self.rows_of(delivery(entry(messaging=[message("m_1"), message("m_2")])))
        fresh = self.rows_of(delivery(entry(messaging=[message("m_3")])))
        self.assertEqual(self.inbox.append(rows), 2)
        with psycopg.connect(self.url, autocommit=True) as listener:
            listener.execute("LISTEN garage_meta_inbox")
            self.assertEqual(self.inbox.append(rows), 0)
            self.assertEqual(list(listener.notifies(timeout=0.5, stop_after=1)), [])
            self.assertEqual(self.inbox.append([rows[0], *fresh, fresh[0]]), 1)
            self.assertEqual(len(list(listener.notifies(timeout=5, stop_after=1))), 1)
        self.assertEqual(self.inbox.append([]), 0)
        self.assertEqual(len(self.stored()), 3)

    def test_the_receiver_login_can_do_nothing_else(self):
        self.inbox.append(self.rows_of(delivery(entry(messaging=[message("m_1")]))))
        values = "('msg:' || repeat('a', 64), 'messages', '1', '{}')"
        denied = (
            "SELECT * FROM garage.meta_inbox",
            "SELECT count(*) FROM garage.meta_inbox",
            "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) VALUES "
            + values + " ON CONFLICT (event_key) DO NOTHING",
            "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) VALUES "
            + values + " RETURNING inbox_id",
            "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload, processed_at) "
            "VALUES ('msg:' || repeat('b', 64), 'messages', '1', '{}', now())",
            "UPDATE garage.meta_inbox SET outcome = 'forged'",
            "DELETE FROM garage.meta_inbox",
            "TRUNCATE garage.meta_inbox",
            # Inserting needs no sequence privilege (identity column); 0014 revoked it.
            "SELECT nextval('garage.meta_inbox_inbox_id_seq')",
            "SELECT * FROM garage.meta_forgotten",
            "SELECT * FROM garage.meta_settings",
            "UPDATE garage.meta_settings SET bot_enabled = TRUE",
            "SELECT * FROM garage.meta_tokens",
            "SELECT * FROM garage.messenger_messages",
            "INSERT INTO garage.messenger_threads (psid, page_id) VALUES ('1', '1')",
            "SELECT * FROM garage.staff_users",
            "INSERT INTO garage.audit_log (action) VALUES ('forged.entry')",
            "SELECT * FROM warehouse.warehouse_products",
        )
        for statement in denied:
            with psycopg.connect(self.hook_url, autocommit=True) as connection:
                with self.assertRaises(errors.InsufficientPrivilege, msg=statement):
                    connection.execute(statement)
        self.assertEqual(len(self.stored()), 1)

    def test_jsonb_size_matches_what_the_check_measures(self):
        samples = [
            {"text": "ქართული 😀 \"quoted\" \\ back\nline\ttab\x01 /slash", "n": [0, -1, 1.5],
             "f": [0.1, 1e16, 1.5e-07, 1e300, 2.5e-30, 12345678901234567890],
             "flags": [True, False, None], "empty": {}, "list": [], "nested": {"a": [{"b": []}]}},
            message("m_1"), comment(), {**postback(), ENTRY_TIME_KEY: TIME},
        ]
        with psycopg.connect(self.url) as connection:
            for sample in samples:
                measured = connection.execute("SELECT octet_length(%s::jsonb::text)",
                                              (json.dumps(sample),)).fetchone()[0]
                self.assertEqual(jsonb_size(sample), measured, sample)

    def test_the_largest_item_kept_whole_fits_the_table(self):
        # Many tiny elements make the separators count; Georgian makes the UTF-8 count.
        filler = message("m_big", text="ა" * 12_000, attachments=[1] * 8_000)
        base = jsonb_size({**filler, ENTRY_TIME_KEY: TIME})
        pad = PAYLOAD_CAP - base
        filler["message"]["pad"] = "x" * (pad - len(', "pad": ""'))
        rows = self.rows_of(delivery(entry(messaging=[filler])))
        payload = rows[0][3]
        self.assertNotIn(OVERSIZED_KEY, payload)
        self.assertEqual(jsonb_size(payload), PAYLOAD_CAP)
        over = message("m_over", text="ა" * 12_000, attachments=[1] * 8_000)
        over["message"]["pad"] = "x" * (pad - len(', "pad": ""') + 1)
        rows += self.rows_of(delivery(entry(messaging=[over])))
        self.assertTrue(rows[1][3][OVERSIZED_KEY])
        self.assertEqual(self.inbox.append(rows), 2)
        with psycopg.connect(self.url) as connection:
            sizes = [row[0] for row in connection.execute(
                "SELECT octet_length(payload::text) FROM garage.meta_inbox ORDER BY inbox_id")]
        self.assertEqual(sizes[0], PAYLOAD_CAP)
        self.assertLess(sizes[1], 1_000)

    async def test_a_signed_delivery_reaches_the_table_through_the_default_inbox(self):
        settings = MetaSettings(app_secret=SECRET, verify_token=VERIFY,
                                receiver_database_url=self.hook_url)
        app = create_receiver_app(settings)
        body = json.dumps(delivery(entry(messaging=[message("m_e2e")]))).encode()
        headers = {"X-Hub-Signature-256": sign(body)}
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            first = await client.post("/meta/webhook", content=body, headers=headers)
            again = await client.post("/meta/webhook", content=body, headers=headers)
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual([row[0] for row in self.stored()], ["msg:" + digest(["m_e2e"])])
        dead = MetaSettings(app_secret=SECRET, verify_token=VERIFY,
                            receiver_database_url=make_conninfo(self.hook_url, port=1))
        transport = httpx.ASGITransport(app=create_receiver_app(dead),
                                        client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            unavailable = await client.post("/meta/webhook", content=body, headers=headers)
        self.assertEqual(unavailable.status_code, 503)


class ReceiverEnvironmentTests(unittest.TestCase):
    """The public process may hold only its own settings, never the rest of .env."""

    def test_only_receiver_keys_are_read_and_nothing_reaches_os_environ(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                "META_APP_SECRET=" + "a" * 32 + "\n"
                "META_WEBHOOK_VERIFY_TOKEN=" + "v" * 40 + "\n"
                "MESSENGER_RECEIVER_DATABASE_URL=postgresql://hook@127.0.0.1/db\n"
                "ANTHROPIC_API_KEY=sk-fixture-not-real\n"  # gitleaks:allow
                "OPENROUTER_API_KEY=or-fixture-not-real\n"  # gitleaks:allow
                "META_TOKEN_ENCRYPTION_KEY=" + "b" * 64 + "\n"
                "MESSENGER_DATABASE_URL=postgresql://worker@127.0.0.1/db\n"
                "AUTOMOTIVE_SITE_DATABASE_URL=postgresql://site@127.0.0.1/db\n",
                encoding="utf-8")
            before = dict(os.environ)
            values = meta_receiver.receiver_environment(env_file, environ={})
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(set(values), {"META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN",
                                       "MESSENGER_RECEIVER_DATABASE_URL"})
        settings = meta_receiver.MetaSettings.from_env(values)
        self.assertEqual(settings.missing_for_receiver(), [])
        self.assertEqual((settings.token_key, settings.database_url), (b"", ""))

    def test_process_environment_wins_for_receiver_keys_only(self) -> None:
        values = meta_receiver.receiver_environment(
            Path("/nonexistent/.env"),
            environ={"MESSENGER_RECEIVER_PORT": "18771", "ANTHROPIC_API_KEY": "x"})
        self.assertEqual(values, {"MESSENGER_RECEIVER_PORT": "18771"})


if __name__ == "__main__":
    unittest.main()
