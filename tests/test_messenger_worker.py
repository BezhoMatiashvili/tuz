"""The Messenger worker: inbox rows, bursts, the admin's switches, shop answers, caps, #bot
commands, failures, the parts pager, recovery, jobs and the threads.

A fake store (in memory, with meta_store's own rules: a mid and a reply trigger are stored once,
"unanswered" means after the chat's newest bot or shop message, turns are counted like
turns_today, an engine state goes through JSON), a Graph fake that records every call and a
scripted engine stand in for PostgreSQL, Meta and the AI provider: nothing leaves the process and
nothing is billed. ``RealEngineTests`` runs the real assistant engine with the scripted model of
``tests.test_assistant``. ``WorkerDatabaseTests`` runs the main paths through the real MetaStore
as a member of garage_messenger on a disposable PostgreSQL database, and skips without
WAREHOUSE_TEST_ADMIN_DATABASE_URL.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import itertools
import json
import os
import sys
import threading
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from automotive.messenger_engine import EngineResult, MessengerConversations, MessengerEngine
from automotive.messenger_render import (
    TEXTS,
    bot_failed_text,
    bot_help_text,
    bot_on_refused_text,
    bot_switched_text,
    conflicting_vins_text,
    late_apology,
    media_only_text,
)
from automotive.messenger_worker import (
    CHANNEL_KEY,
    INBOX_CHANNEL,
    JOB_NAMES,
    NOT_CONNECTED_RETRY_SECONDS,
    READY_RETRY_SECONDS,
    MessengerWorker,
    command_of,
    estimated_cost,
    main,
    usage_cost,
)
from automotive.meta_config import MetaSettings
from automotive.meta_connect import ConnectError
from automotive.meta_context import MetaContext, NotConnected
from automotive.meta_crypto import TokenCipher
from automotive.meta_graph import GraphError, TokenChoice
from automotive.meta_store import (
    ACTION,
    CHANNELS,
    REPLY_FIELDS,
    REPLY_STATUSES,
    SENDERS,
    SOURCES,
    THREAD_COLUMNS,
    UNFINISHED,
    AutoReplyNotConfirmed,
    MetaStore,
)
from tests.test_assistant import (
    REAR,
    Script,
    Search,
    context_of,
    done,
    handles,
    option,
    product,
    says,
    search,
    service,
    use,
)
from tests.test_meta_store_database import RoleDatabase
from warehouse_sync.migrations import apply_migrations_on_connection

APP, OTHER_APP, SEND_APP, AUTO_APP = "1234567890", "9990001112", "5550001113", "263902037430900"
PAGE, OTHER_PAGE = "61595318832800", "61500000000009"
PSID, OTHER, ADMIN_PSID = "7000000000001234", "7000000000005678", "7000000000009999"
PAGE_TOKEN, SEND_TOKEN = "EAApageFixtureToken000003", "EAAsendOnlyFixtureToken0005"
PHONE = "596 33 33 74"
SETTINGS = MetaSettings(app_id=APP, app_secret="s" * 32, verify_token="v" * 40,
                        token_key=b"k" * 32, public_url="https://hook.example")
START = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)  # 14:00 in Tbilisi
USAGE = {"model_requests": 2, "input_tokens": 1200, "output_tokens": 150}
ANSWER = "გვაქვს, ქვემოთაა."
MIDS = itertools.count(1)
TBILISI = ZoneInfo("Asia/Tbilisi")


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def actor_name(actor) -> str:
    if isinstance(actor, dict):
        return str(actor.get("username") or "")[:32]
    return str(actor or "").strip()[:32]


class Clock:
    """Wall time and the monotonic clock, moved together by the test."""

    def __init__(self, start: datetime = START) -> None:
        self.moment, self.ticks = start, 1000.0

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)
        self.ticks += seconds


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeStore:
    """meta_store.MetaStore in memory, with its rules."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.ids = itertools.count(1)
        self.lock = threading.RLock()
        now = clock.now()
        self.settings_row = {
            "settings_id": 1, "bot_enabled": False, "bot_enabled_at": None,
            "bot_changed_at": None, "bot_changed_by": "", "bot_paused_reason": "",
            "bot_paused_at": None, "auto_reply_confirmed_off": False, "comments_enabled": False,
            "comments_enabled_at": None, "max_reply_age_minutes": 1440,
            "daily_turn_limit": 150, "customer_daily_turn_limit": 30,
            "comment_public_reply": "", "fallback_ka": "", "fallback_en": "",
            "retention_days": 365, "version": 1, "updated_at": now}
        self.page_row: dict | None = {
            "page_id": PAGE, "page_name": "Garage • ფარეხი 1886", "app_id": APP,
            "subscribed_fields": [], "subscribed_at": None, "connected_at": now,
            "connected_by": "boss", "version": 1}
        self.tokens = {"page": {"purpose": "page", "app_id": APP, "subject_id": PAGE,
                                "status": "ok", "status_detail": ""}}
        self.values = {"page": PAGE_TOKEN}
        self.threads: dict[str, dict] = {}
        self.message_rows: list[dict] = []
        self.reply_rows: list[dict] = []
        self.audits: list[tuple] = []
        self.flags: list[tuple] = []
        self.inbox: list[dict] = []
        self.runs: list[dict] = []
        self.requests: list[dict] = []
        self.purged: list = []
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.claimed: list[int] = []

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        error = self.fail.pop(name, None)
        if error is not None:
            raise error

    # -- settings ------------------------------------------------------------------------------

    def settings(self) -> dict:
        self._hit("settings")
        return dict(self.settings_row)

    def set_bot(self, enabled, actor, auto_reply_confirmed=None, *, client_ip=None) -> dict:
        self._hit("set_bot")
        row = self.settings_row
        confirmed = (row["auto_reply_confirmed_off"] if auto_reply_confirmed is None
                     else bool(auto_reply_confirmed))
        if enabled and not confirmed:
            raise AutoReplyNotConfirmed("Confirm that Meta's Auto reply is off first")
        was, now = row["bot_enabled"], self.clock.now()
        if enabled and not was:
            row["bot_enabled_at"] = now
        row.update(bot_enabled=bool(enabled), bot_changed_at=now, bot_changed_by=actor_name(actor),
                   bot_paused_reason="", bot_paused_at=None, auto_reply_confirmed_off=confirmed,
                   version=row["version"] + 1)
        self.audit(actor, "messenger.bot_on" if enabled else "messenger.bot_off", "settings",
                   {"changed": bool(enabled) != was})
        return dict(row)

    def pause_for_provider(self, reason="ai_provider") -> bool:
        self._hit("pause_for_provider")
        if self.settings_row["bot_paused_reason"]:
            return False
        self.settings_row.update(bot_paused_reason=reason, bot_paused_at=self.clock.now())
        self.audit("worker", "messenger.bot_paused", "settings", {"reason": reason})
        return True

    def audit(self, actor, action, target="", detail=None, *, client_ip=None,
              connection=None) -> None:
        if not ACTION.match(action):
            raise ValueError("Audit actions look like 'messenger.bot_on'")
        self.audits.append((actor_name(actor), action, target, dict(detail or {})))

    # -- connection ----------------------------------------------------------------------------

    def page(self) -> dict | None:
        self._hit("page")
        return dict(self.page_row) if self.page_row else None

    def token_rows(self) -> list[dict]:
        self._hit("token_rows")
        return [dict(row) for _, row in sorted(self.tokens.items())]

    def token(self, purpose):
        self._hit("token")
        return self.values.get(purpose)

    def mark_token(self, purpose, status, detail="", *, expiry=None) -> bool:
        self._hit("mark_token")
        row = self.tokens.get(purpose)
        if row is None:
            return False
        row.update(status=status, status_detail=detail)
        return True

    # -- threads ------------------------------------------------------------------------------

    def _thread(self, psid, page_id) -> dict:
        thread = self.threads.get(psid)
        if thread is None:
            now = self.clock.now()
            thread = {"psid": psid, "page_id": page_id, "customer_name": "",
                      "conversation_id": "", "bot_enabled": True, "is_admin": False,
                      "source": "", "source_ad_id": "", "first_seen_at": now,
                      "last_customer_at": None, "last_shop_at": None, "last_bot_at": None,
                      "needs_attention": False, "attention_reason": "", "engine_state": None,
                      "engine_state_at": None, "updated_at": now, "version": 1}
            self.threads[psid] = thread
        return thread

    @staticmethod
    def _row(thread: dict, with_state: bool = False) -> dict:
        row = {key: thread[key] for key in THREAD_COLUMNS}
        if with_state:
            row["engine_state"] = copy.deepcopy(thread["engine_state"])
        return row

    def upsert_thread(self, psid, page_id, *, customer_name=None, conversation_id=None,
                      source=None, source_ad_id=None) -> dict:
        self._hit("upsert_thread")
        if source is not None and source not in SOURCES:
            raise ValueError("Unknown thread source")
        with self.lock:
            thread = self._thread(psid, page_id)
            thread["page_id"] = page_id
            for key, value in (("customer_name", customer_name),
                               ("conversation_id", conversation_id), ("source", source),
                               ("source_ad_id", source_ad_id)):
                if value is not None:
                    thread[key] = value
            return self._row(thread)

    def get_thread(self, psid, *, with_state=False) -> dict | None:
        self._hit("get_thread")
        thread = self.threads.get(psid)
        return self._row(thread, with_state) if thread else None

    def list_threads(self, filter="all", limit=50, offset=0) -> list[dict]:
        self._hit("list_threads")
        rows = sorted(self.threads.values(), reverse=True,
                      key=lambda thread: thread["last_customer_at"] or thread["first_seen_at"])
        return [self._row(thread) for thread in rows][offset:offset + limit]

    def save_engine_state(self, psid, state) -> bool:
        self._hit("save_engine_state")
        if state is not None and len(json.dumps(state, ensure_ascii=False).encode()) > 262144:
            raise ValueError("The engine state is larger than 256 KiB")
        thread = self.threads.get(psid)
        if thread is None:
            return False
        thread["engine_state"] = (None if state is None
                                  else json.loads(json.dumps(state, ensure_ascii=False)))
        return True

    def flag_attention(self, psid, reason, *, needs=True, actor=None, client_ip=None) -> bool:
        self._hit("flag_attention")
        thread = self.threads.get(psid)
        if thread is None:
            return False
        thread.update(needs_attention=needs, attention_reason=reason if needs else "")
        self.flags.append((psid, reason))
        return True

    # -- messages ------------------------------------------------------------------------------

    def add_message(self, psid, mid, sender, sent_at, *, page_id, body="", attachments=None,
                    quick_reply="", referral_ad_id="", app_id="") -> bool:
        self._hit("add_message")
        if sender not in SENDERS or not 1 <= len(mid) <= 200:
            raise ValueError("A message needs a sender and a 1-200 character mid")
        when = (sent_at if isinstance(sent_at, datetime)
                else datetime.fromtimestamp(sent_at / 1000, UTC))
        with self.lock:
            thread = self._thread(psid, page_id)
            if any(row["mid"] == mid for row in self.message_rows):
                return False
            self.message_rows.append({
                "message_id": next(self.ids), "mid": mid, "psid": psid, "sender": sender,
                "body": str(body or "")[:4000],
                "attachments": [{"type": item["type"]} for item in attachments or ()
                                if isinstance(item, dict)],
                "quick_reply": str(quick_reply or ""), "referral_ad_id": referral_ad_id or "",
                "app_id": app_id or "", "sent_at": when, "stored_at": self.clock.now()})
            column = {"customer": "last_customer_at", "shop": "last_shop_at",
                      "bot": "last_bot_at"}[sender]
            thread[column] = when if thread[column] is None else max(thread[column], when)
            return True

    def _ordered(self, psid) -> list[dict]:
        return sorted((row for row in self.message_rows if row["psid"] == psid),
                      key=lambda row: (row["sent_at"], row["message_id"]))

    def messages(self, psid, limit=50, *, before_id=None) -> list[dict]:
        self._hit("messages")
        return [dict(row) for row in self._ordered(psid)[-limit:]]

    def unanswered_customer_messages(self, psid, *, ignore_app_ids=()) -> list[dict]:
        self._hit("unanswered_customer_messages")
        rows = self._ordered(psid)
        answers = [row for row in rows if row["sender"] != "customer"
                   and not (row["sender"] == "shop" and row["app_id"] in ignore_app_ids)]
        last = (answers[-1]["sent_at"], answers[-1]["message_id"]) if answers else None
        return [dict(row) for row in rows if row["sender"] == "customer"
                and (last is None or (row["sent_at"], row["message_id"]) > last)]

    # -- replies -------------------------------------------------------------------------------

    def start_reply(self, channel, psid, trigger_key, trigger_keys=()) -> int | None:
        self._hit("start_reply")
        if channel not in CHANNELS or not 1 <= len(trigger_key) <= 200:
            raise ValueError("Unknown reply channel or trigger")
        with self.lock:
            if any(row["channel"] == channel and row["trigger_key"] == trigger_key
                   for row in self.reply_rows):
                return None
            reply_id = next(self.ids)
            self.reply_rows.append({
                "reply_id": reply_id, "channel": channel, "psid": psid,
                "trigger_key": trigger_key, "trigger_keys": list(trigger_keys),
                "status": "pending", "error_code": None, "error_subcode": None,
                "error_text": "", "engine_status": "", "sent_mids": [], "usage": {},
                "cost_usd": None, "started_at": self.clock.now(), "finished_at": None})
            return reply_id

    def reply(self, reply_id) -> dict:
        return next(row for row in self.reply_rows if row["reply_id"] == reply_id)

    def add_sent_mid(self, reply_id, mid) -> bool:
        self._hit("add_sent_mid")
        self.reply(reply_id)["sent_mids"].append(mid)
        return True

    def finish_reply(self, reply_id, status, **fields) -> bool:
        self._hit("finish_reply")
        if status not in REPLY_STATUSES or status == "pending":
            raise ValueError("Unknown final reply status")
        if set(fields) - REPLY_FIELDS:
            raise ValueError("Unknown reply fields")
        json.dumps(fields.get("usage") or {})
        self.reply(reply_id).update({**fields, "status": status,
                                     "finished_at": self.clock.now()})
        return True

    def abandon_stale_replies(self, older_than_seconds=900) -> int:
        self._hit("abandon_stale_replies")
        cutoff = self.clock.now() - timedelta(seconds=older_than_seconds)
        stale = [row for row in self.reply_rows
                 if row["status"] == "pending" and row["started_at"] < cutoff]
        for row in stale:
            row["status"] = "abandoned"
        return len(stale)

    def turns_today(self, psid=None) -> int:
        self._hit("turns_today")
        return sum(1 for row in self.reply_rows if row["channel"] in ("messenger", "comment")
                   and (row["engine_status"] or row["status"] in UNFINISHED)
                   and (psid is None or row["psid"] == psid))

    def cost_today(self) -> float:
        self._hit("cost_today")
        return float(sum(row["cost_usd"] or 0 for row in self.reply_rows))

    # -- inbox ---------------------------------------------------------------------------------

    def deliver(self, field, payload, *, page_id=PAGE, event_key=None) -> int:
        inbox_id = next(self.ids)
        self.inbox.append({
            "inbox_id": inbox_id,
            "event_key": event_key or "msg:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            "field": field, "page_id": page_id, "payload": copy.deepcopy(payload),
            "received_at": self.clock.now(), "processed_at": None, "attempts": 0,
            "outcome": "", "history": []})
        return inbox_id

    def row(self, inbox_id) -> dict:
        return next(row for row in self.inbox if row["inbox_id"] == inbox_id)

    def claim_inbox(self, limit=20, *, lease_seconds=300, max_attempts=5) -> list[dict]:
        self._hit("claim_inbox")
        with self.lock:
            picked = [row for row in self.inbox if row["processed_at"] is None][:limit]
            for row in picked:
                row.update(processed_at=self.clock.now(), outcome="claimed",
                           attempts=row["attempts"] + 1)
            self.claimed = [row["inbox_id"] for row in picked]
            return [{key: copy.deepcopy(row[key]) for key in (
                "inbox_id", "event_key", "field", "page_id", "payload", "received_at",
                "attempts")} for row in picked]

    def finish_inbox(self, inbox_id, outcome="done") -> bool:
        self._hit("finish_inbox")
        row = self.row(inbox_id)
        row.update(processed_at=self.clock.now(), outcome=outcome)
        row["history"].append(outcome)
        return True

    def fail_inbox(self, inbox_id, error, max_attempts=5) -> bool:
        self._hit("fail_inbox")
        row = self.row(inbox_id)
        final = row["attempts"] >= max_attempts
        row.update(processed_at=self.clock.now() if final else None,
                   outcome=("failed: " if final else "retry: ") + error)
        row["history"].append(row["outcome"])
        return not final

    # -- jobs ----------------------------------------------------------------------------------

    def start_run(self, job) -> int:
        self._hit("start_run")
        run_id = next(self.ids)
        self.runs.append({"run_id": run_id, "job": job, "started_at": self.clock.now(),
                          "finished_at": None, "status": "running", "records": 0,
                          "detail": ""})
        return run_id

    def finish_run(self, run_id, status, records=0, detail="") -> bool:
        self._hit("finish_run")
        next(row for row in self.runs if row["run_id"] == run_id).update(
            status=status, records=records, detail=detail, finished_at=self.clock.now())
        return True

    def latest_runs(self) -> list[dict]:
        self._hit("latest_runs")
        latest: dict[str, dict] = {}
        for row in self.runs:
            latest[row["job"]] = dict(row)
        return list(latest.values())

    def take_job_requests(self, limit=20) -> list[dict]:
        self._hit("take_job_requests")
        taken = [row for row in self.requests if row["taken_at"] is None][:limit]
        for row in taken:
            row["taken_at"] = self.clock.now()
        return [dict(row) for row in taken]

    def request(self, job) -> None:
        self.requests.append({"request_id": next(self.ids), "job": job,
                              "requested_at": self.clock.now(), "requested_by": "boss",
                              "taken_at": None})

    def purge(self, retention_days=None) -> dict:
        self._hit("purge")
        self.purged.append(retention_days)
        return {"inbox": 2, "messages": 1, "replies": 0, "threads": 0, "runs": 0,
                "job_requests": 0}


class FakeGraph:
    """The Send API, recorded. ``errors`` queues an exception (or None, a success) per method;
    ``after_send`` runs after each message Meta accepted. ``reads`` keeps the store's last two
    calls before each Graph call: the switch re-read."""

    def __init__(self, store: FakeStore | None = None) -> None:
        self.store = store
        self.calls: list[tuple] = []
        self.reads: list[tuple] = []
        self.errors: dict[str, list] = {"send_message": [], "sender_action": []}
        self.after_send = None
        self.count = 0

    def _record(self, call: tuple) -> None:
        self.calls.append(call)
        if self.store is not None:
            self.reads.append(tuple(self.store.calls[-2:]))

    def _error(self, method: str) -> None:
        queue = self.errors[method]
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def send_message(self, page_id, token, psid, message, messaging_type="RESPONSE",
                     proof=True) -> dict:
        assert isinstance(token, TokenChoice) and page_id == PAGE
        self._record(("send", psid, copy.deepcopy(message), token.purpose, token.proof))
        self._error("send_message")
        self.count += 1
        if self.after_send is not None:
            self.after_send(self.count)
        return {"recipient_id": psid, "message_id": f"m_bot_{self.count}"}

    def sender_action(self, page_id, token, psid, action, proof=True) -> dict:
        assert isinstance(token, TokenChoice) and page_id == PAGE
        self._record(("action", psid, action))
        self._error("sender_action")
        return {"recipient_id": psid, "message_id": ""}

    def sends(self, psid: str | None = None) -> list[tuple]:
        return [call for call in self.calls
                if call[0] == "send" and (psid is None or call[1] == psid)]

    def texts(self, psid: str | None = None) -> list[str]:
        return [call[2]["text"] for call in self.sends(psid)]

    def actions(self) -> list[str]:
        return [call[2] for call in self.calls if call[0] == "action"]


class FakeEngine:
    """MessengerEngine's surface; each turn's result comes from ``results`` (callables that get
    the call), else an ok answer."""

    def __init__(self) -> None:
        self.results: list = []
        self.calls: list[dict] = []
        self.ready = True
        self.phone = PHONE
        self.prepared = 0

    def respond(self, state, text, *, choice=None, language="auto", timeout=60, cancel=None):
        call = {"state": copy.deepcopy(state), "text": text, "choice": choice, "cancel": cancel}
        self.calls.append(call)
        result = self.results.pop(0) if self.results else ok()
        return result(call)

    def prepare(self) -> bool:
        self.prepared += 1
        return True

    def shop_phone(self) -> str:
        return self.phone


def view(answer: str, *, language="ka", options=(), cards=0, suggestions=()) -> dict:
    """site_chat.public_turn's shape."""
    parts = [{"name": f"Part {number}", "brand": "SATO", "price": 40.0 + number,
              "availability": "in_stock", "group": None} for number in range(1, cards + 1)]
    return {"status": "OK", "answer": answer, "note": "", "language": language, "car": "",
            "question": ({"text": "რომელი?", "options": [{"label": label} for label in options]}
                         if options else None),
            "suggestions": [{"label": label} for label in suggestions], "quick_replies": [],
            "groups": [], "cards": parts, "total": cards, "offset": 0, "next_offset": None,
            "checked": 0, "checking": 0}


def unanswered(call: dict, language: str = "ka") -> dict:
    history = list((call["state"] or {}).get("history") or [])
    return {"language": language,
            "history": [*history, {"question": call["text"], "answer": "", "shown": []}]}


def ok(answer: str = ANSWER, *, language: str = "ka", **shape):
    def build(call: dict) -> EngineResult:
        return EngineResult(
            status="ok", public=view(answer, language=language, **shape),
            state={"language": language,
                   "history": [{"question": call["text"], "answer": answer, "shown": []}],
                   "memory": {"transcript": [{"user": call["text"], "assistant": answer}]}},
            engine_status="OK", language=language, usage=dict(USAGE), error_code="",
            unsent_state=unanswered(call, language))
    return build


def failure(code: str, *, status: str = "failed", engine_status: str = ""):
    def build(call: dict) -> EngineResult:
        kept = unanswered(call)
        return EngineResult(status=status, public={}, state=kept, engine_status=engine_status,
                            language="ka", usage={"model_requests": 1}, error_code=code,
                            unsent_state=kept)
    return build


def cancelled(call: dict) -> EngineResult:
    kept = unanswered(call)
    return EngineResult(status="cancelled", public={}, state=kept, engine_status="",
                        language="ka", usage={}, error_code="", unsent_state=kept)


class WorkerCase(unittest.TestCase):
    typing_seconds = 15.0

    def setUp(self) -> None:
        self.clock = Clock()
        self.store = FakeStore(self.clock)
        self.graph = FakeGraph(self.store)
        self.engine = FakeEngine()
        self.ctx = MetaContext(SETTINGS, self.store, self.graph, None, clock=self.clock.monotonic,
                               now=self.clock.now)
        self.worker = self.make_worker()
        self.store.settings_row["auto_reply_confirmed_off"] = True
        self.store.set_bot(True, "boss")
        self.clock.advance(1)

    def make_worker(self, **options) -> MessengerWorker:
        return MessengerWorker(self.ctx, self.engine, typing_seconds=self.typing_seconds,
                               **options)

    def say(self, text: str = "", *, psid: str = PSID, mid: str | None = None, ago: float = 0,
            attachments=(), quick_reply: str | None = None, referral: dict | None = None,
            field: str = "messages", sticker: bool = False) -> str:
        mid = mid or f"m_c{next(MIDS)}"
        message: dict = {"mid": mid}
        if text:
            message["text"] = text
        if attachments:
            message["attachments"] = [{"type": kind, "payload": {"url": "https://cdn.example/a"}}
                                      for kind in attachments]
        if sticker:
            message["sticker_id"] = 369239263222822
        if quick_reply:
            message["quick_reply"] = {"payload": quick_reply}
        item = {"sender": {"id": psid}, "recipient": {"id": PAGE},
                "timestamp": ms(self.clock.now() - timedelta(seconds=ago)), "message": message}
        if referral:
            item["referral"] = referral
        self.store.deliver(field, item)
        return mid

    def shop(self, text: str, *, psid: str = PSID, app_id: str | None = OTHER_APP,
             mid: str | None = None) -> str:
        mid = mid or f"m_s{next(MIDS)}"
        message: dict = {"mid": mid, "is_echo": True, "text": text}
        if app_id:
            message["app_id"] = int(app_id)
        self.store.deliver("messages", {"sender": {"id": PAGE}, "recipient": {"id": psid},
                                        "timestamp": ms(self.clock.now()), "message": message})
        return mid

    def pump(self) -> list[str]:
        self.worker.poll_inbox()
        return [self.store.row(inbox_id)["outcome"] for inbox_id in self.store.claimed]

    def later(self, seconds: float = 5) -> dict:
        self.clock.advance(seconds)
        return self.worker.run_due_bursts()

    def turn(self, text: str = "ხუნდები გაქვთ?", **options) -> tuple[str, dict]:
        mid = self.say(text, **options)
        self.pump()
        return mid, self.later(5)

    def reply(self, trigger: str, channel: str | None = None) -> dict:
        rows = [row for row in self.store.reply_rows if row["trigger_key"] == trigger
                and (channel is None or row["channel"] == channel)]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def state(self, psid: str = PSID) -> dict:
        return self.store.threads[psid]["engine_state"]


class BurstTests(WorkerCase):
    def test_a_burst_is_answered_after_the_quiet_window(self):
        mid = self.say("Camry 2015 წინა ხუნდები")
        self.assertEqual(self.pump(), ["stored"])
        self.assertEqual(self.engine.prepared, 1)  # the warm-up starts with the window
        self.assertEqual(self.later(4), {})
        with self.assertLogs("automotive.messenger_worker", "INFO") as logs:
            self.assertEqual(self.later(1), {PSID: "sent"})
        self.assertEqual(self.graph.actions(), ["mark_seen", "typing_on"])
        self.assertEqual(self.graph.texts(), [ANSWER])
        # Every Graph call came right after the switches and the chat were read again.
        self.assertEqual(set(self.graph.reads), {("settings", "get_thread")})
        call = self.engine.calls[0]
        self.assertEqual((call["state"], call["text"], call["choice"]),
                         (None, "Camry 2015 წინა ხუნდები", None))
        row = self.reply(mid)
        self.assertEqual((row["channel"], row["status"], row["engine_status"], row["sent_mids"],
                          row["usage"]), ("messenger", "sent", "OK", ["m_bot_1"], USAGE))
        self.assertEqual([(row["mid"], row["sender"], row["app_id"])
                          for row in self.store.message_rows],
                         [(mid, "customer", ""), ("m_bot_1", "bot", APP)])
        saved = self.state()
        self.assertEqual(saved[CHANNEL_KEY], {"options": [], "numbered": False})
        self.assertEqual(saved["history"][-1]["answer"], ANSWER)
        self.assertEqual(self.store.turns_today(), 1)
        output = "\n".join(logs.output)
        self.assertIn("…1234", output)
        self.assertNotIn(PSID, output)
        self.assertNotIn("ხუნდები", output)
        self.assertEqual(self.worker.run_burst(PSID), "nothing")  # nothing left to answer

    def test_messages_join_one_burst_until_quiet_or_for_at_most_45_seconds(self):
        self.say("გამარჯობა")
        self.pump()
        for _ in range(11):
            self.clock.advance(4)
            self.say("კიდევ ერთი")
            self.pump()
            self.assertEqual(self.worker.run_due_bursts(), {})
        self.assertEqual(self.later(1), {PSID: "sent"})  # 45 s after the first message
        self.assertEqual(self.engine.calls[0]["text"].split("\n"),
                         ["გამარჯობა"] + ["კიდევ ერთი"] * 11)
        self.assertEqual(len(self.graph.sends()), 1)

    def test_a_message_written_while_the_reply_was_made_is_answered_next(self):
        first, results = self.turn("ხუნდები")
        self.assertEqual(results, {PSID: "sent"})
        sent_at = next(row["sent_at"] for row in self.store.message_rows if row["mid"] == first)
        bot = next(row for row in self.store.message_rows if row["sender"] == "bot")
        # Stamped just after the message it answers, on Meta's clock.
        self.assertEqual(bot["sent_at"], sent_at + timedelta(milliseconds=1))
        # Written during the turn, its webhook arrived after the reply went out.
        self.say("უკანაც", ago=1)
        self.pump()
        self.assertEqual(self.later(5), {PSID: "sent"})
        self.assertEqual(self.engine.calls[1]["text"], "უკანაც")

    def test_a_long_backlog_keeps_the_newest_message(self):
        for number in range(5):
            self.say(str(number) + "ა" * 999)
            self.pump()
            self.clock.advance(1)
        self.say("NEWEST")
        self.pump()
        self.later(5)
        text = self.engine.calls[0]["text"]
        self.assertTrue(text.endswith("\nNEWEST"))
        self.assertLessEqual(len(text), 4000)
        self.assertEqual([line[0] for line in text.split("\n")[:-1]], ["2", "3", "4"])

    def test_a_reply_in_three_parts_is_never_stopped_by_its_own_parts(self):
        answer = "\n\n".join(letter * 1500 for letter in "აბგ")
        self.engine.results = [ok(answer)]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "sent"})
        self.assertEqual([len(text) for text in self.graph.texts()], [1500, 1500, 1500])
        self.assertEqual(self.reply(mid)["sent_mids"], ["m_bot_1", "m_bot_2", "m_bot_3"])
        self.assertEqual(set(self.graph.reads), {("settings", "get_thread")})

    def test_a_late_reply_opens_with_an_apology(self):
        self.store.settings_row["bot_enabled_at"] = START - timedelta(hours=5)
        self.turn("ხუნდები", ago=2 * 3600)
        self.assertEqual(self.graph.texts()[0], f"{late_apology('ka')}\n\n{ANSWER}")
        self.turn("და ზეთი?")
        self.assertEqual(self.graph.texts()[1], ANSWER)

    def test_photos_and_stickers_alone_get_the_text_request_without_the_engine(self):
        mid = self.say(attachments=("image",))
        self.pump()
        self.assertEqual(self.engine.prepared, 0)
        self.assertEqual(self.later(5), {PSID: "sent"})
        self.assertEqual(self.graph.texts(), [media_only_text("ka")])
        self.assertEqual((self.engine.calls, self.reply(mid)["channel"]), ([], "notice"))
        self.assertEqual(self.store.turns_today(), 0)  # no turn spent
        self.turn("", sticker=True, attachments=("image",))
        self.assertEqual(self.graph.texts()[-1], media_only_text("ka"))
        self.turn("ეს ნაწილი გაქვთ?", attachments=("image",))  # words with a photo: a turn
        self.assertEqual(self.engine.calls[0]["text"], "ეს ნაწილი გაქვთ?")

    def test_a_typed_number_or_a_tapped_answer_becomes_the_choice(self):
        labels = [f"ვარიანტი {number}" for number in range(1, 16)]
        sides = ["წინა", "უკანა"]
        self.engine.results = [ok("რომელი გჭირდებათ?", options=labels),
                               ok("რომელი მხარე?", options=sides),
                               ok("რომელი მხარე?", options=sides), ok("კარგი.")]
        self.turn("ხუნდები")
        self.assertIn("1. ვარიანტი 1\n2. ვარიანტი 2", self.graph.texts()[0])
        self.assertEqual(self.state()[CHANNEL_KEY], {"options": labels, "numbered": True})
        self.turn("2")  # under a numbered list
        self.assertEqual(self.engine.calls[1]["choice"], 2)
        sent = self.graph.sends()[-1][2]
        self.assertEqual([(entry["title"], entry["payload"]) for entry in sent["quick_replies"]],
                         [("წინა", "CHOICE:1"), ("უკანა", "CHOICE:2")])
        self.turn("2")  # a bare number under buttons is only words
        self.assertIsNone(self.engine.calls[2]["choice"])
        self.turn("უკანა", quick_reply="CHOICE:2")
        self.assertEqual(self.engine.calls[3]["choice"], 2)
        self.assertNotIn(CHANNEL_KEY, self.engine.calls[3]["state"])  # the engine never sees it

    def test_more_pages_through_the_kept_parts_without_a_turn(self):
        self.engine.results = [ok("ეს ნაწილები მოიძებნა.", cards=12)]
        self.turn("ფილტრები")
        first = self.graph.sends()[-1][2]
        self.assertIn("Part 5", first["text"])
        self.assertNotIn("Part 6", first["text"])
        self.assertEqual(first["quick_replies"][-1]["payload"], "MORE")
        self.assertEqual(self.state()[CHANNEL_KEY]["offset"], 5)
        mid, results = self.turn("მეტის ნახვა", quick_reply="MORE")
        self.assertEqual(results, {PSID: "sent"})
        self.assertEqual((len(self.engine.calls), self.engine.prepared), (1, 1))
        page = self.graph.sends()[-1][2]
        self.assertIn("Part 6", page["text"])
        self.assertIn("Part 10", page["text"])
        self.assertNotIn("Part 11", page["text"])
        self.assertEqual(self.reply(mid)["channel"], "notice")
        self.assertEqual(self.state()[CHANNEL_KEY]["offset"], 10)
        self.turn("მეტის ნახვა", quick_reply="MORE")
        last = self.graph.sends()[-1][2]
        self.assertIn("Part 12", last["text"])
        self.assertNotIn("quick_replies", last)
        self.assertNotIn("view", self.state()[CHANNEL_KEY])
        self.turn("მეტის ნახვა", quick_reply="MORE")  # nothing kept: words for the engine
        self.assertEqual(len(self.engine.calls), 2)

    def test_bursts_wait_for_the_catalogue_but_not_forever(self):
        self.engine.ready = False
        self.say("ხუნდები")
        self.pump()
        self.assertEqual(self.later(5), {PSID: "waiting"})
        self.assertEqual((self.store.reply_rows, self.graph.calls), ([], []))
        self.assertEqual(self.later(4), {})
        self.assertEqual(self.later(1), {PSID: "waiting"})
        self.engine.ready = True
        self.assertEqual(self.later(5), {PSID: "sent"})
        self.engine.ready = False
        self.say("ზეთი")
        self.pump()
        results, waited = {}, 0
        while results.get(PSID) != "sent" and waited < 400:
            results = self.later(5)
            waited += 5
        self.assertEqual(results, {PSID: "sent"})
        self.assertGreaterEqual(waited, 300)

    def test_after_a_restart_unanswered_chats_are_looked_at_again(self):
        self.store.settings_row["bot_enabled_at"] = START - timedelta(hours=1)
        now = self.clock.now()
        self.store.add_message(PSID, "m_wait", "customer", now - timedelta(minutes=1),
                               page_id=PAGE, body="ხუნდები?")
        self.store.add_message(OTHER, "m_done", "customer", now - timedelta(minutes=3),
                               page_id=PAGE, body="ზეთი?")
        self.store.add_message(OTHER, "m_reply", "bot", now - timedelta(minutes=2),
                               page_id=PAGE, body=ANSWER)
        self.store.add_message("7000000000003333", "m_old", "customer", now - timedelta(days=2),
                               page_id=PAGE, body="ძველი")
        stale = self.store.start_reply("messenger", OTHER, "m_lost")
        self.store.reply(stale)["started_at"] = now - timedelta(hours=1)
        restarted = self.make_worker()
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(self.store.reply(stale)["status"], "abandoned")
        self.clock.advance(5)
        self.assertEqual(restarted.run_due_bursts(), {PSID: "sent"})
        self.assertEqual(self.engine.calls[0]["text"], "ხუნდები?")


class RerunTests(WorkerCase):
    """A burst that ends before it reads the chat is never handed straight back: the scheduler
    would run it again at once, forever, at database speed."""

    def pending(self) -> None:
        self.say("Camry 2015 ხუნდები")
        self.assertEqual(self.pump(), ["stored"])

    def test_a_page_disconnected_in_the_quiet_window_is_looked_at_again_later(self):
        self.pending()
        page, self.store.page_row = self.store.page_row, None
        self.assertEqual(self.later(5), {PSID: "not_connected"})
        reads = self.store.calls.count("page")
        self.assertEqual([self.worker.run_due_bursts() for _ in range(3)], [{}, {}, {}])
        self.assertEqual(self.later(NOT_CONNECTED_RETRY_SECONDS - 1), {})
        self.assertEqual(self.store.calls.count("page"), reads)
        self.store.page_row = page  # reconnected
        self.assertEqual(self.later(1), {PSID: "sent"})

    def test_a_chat_deleted_in_the_quiet_window_leaves_the_schedule(self):
        self.pending()
        # The dashboard's Delete data (MetaStore.delete_customer).
        self.store.threads.pop(PSID)
        self.store.message_rows = [row for row in self.store.message_rows
                                   if row["psid"] != PSID]
        self.assertEqual(self.later(5), {PSID: "no_chat"})
        self.assertNotIn(PSID, self.worker._chats)
        self.assertEqual(self.later(3600), {})

    def test_a_database_outage_is_retried_after_a_pause(self):
        self.pending()
        down = {"now": True}
        real = self.store.settings

        def settings() -> dict:
            if down["now"]:
                raise OSError("connection refused")
            return real()

        self.store.settings = settings
        with self.assertLogs("automotive.messenger_worker", "WARNING") as logs:
            self.assertEqual(self.later(5), {PSID: "error"})
            self.assertEqual([self.worker.run_due_bursts() for _ in range(3)], [{}, {}, {}])
            self.assertEqual(self.later(READY_RETRY_SECONDS), {PSID: "error"})
        self.assertEqual(sum("messenger_burst_failed" in line for line in logs.output), 2)
        down["now"] = False
        self.assertEqual(self.later(READY_RETRY_SECONDS), {PSID: "sent"})


class SwitchTests(WorkerCase):
    def test_switching_the_bot_off_after_the_engine_answered_sends_nothing(self):
        def off(call):
            self.store.set_bot(False, "boss")
            return ok()(call)

        self.engine.results = [off]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "skipped_off"})
        self.assertEqual(self.graph.sends(), [])
        row = self.reply(mid)
        self.assertEqual((row["status"], row["engine_status"]), ("skipped_off", "OK"))
        self.assertEqual(self.store.turns_today(), 1)  # the engine ran: it counts
        saved = self.state()  # the message kept as unanswered for the next turn
        self.assertEqual(saved["history"][-1],
                         {"question": "ხუნდები გაქვთ?", "answer": "", "shown": []})
        self.assertNotIn(CHANNEL_KEY, saved)

    def test_a_switch_turned_off_between_parts_stops_the_rest(self):
        answer = "\n\n".join(letter * 1500 for letter in "აბგ")
        self.engine.results = [ok(answer)]
        self.graph.after_send = lambda count: count == 1 and self.store.set_bot(False, "boss")
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "skipped_off"})
        self.assertEqual(len(self.graph.sends()), 1)
        self.assertEqual(self.reply(mid)["sent_mids"], ["m_bot_1"])
        # Part of the answer went out, so the state that holds it is kept, without its buttons.
        self.assertEqual(self.state()["history"][-1]["answer"], answer)
        self.assertNotIn(CHANNEL_KEY, self.state())

    def test_turning_the_bot_on_never_answers_what_was_written_before(self):
        self.store.set_bot(False, "boss")
        self.say("ხუნდები")
        self.pump()
        self.assertEqual(self.later(5), {PSID: "skipped_off"})
        self.clock.advance(10)
        self.store.set_bot(True, "boss")
        self.assertEqual(self.worker.run_burst(PSID), "duplicate")
        # Written before the switch, delivered after it (the laptop slept): still not answered.
        self.say("ზეთი", ago=5)
        self.pump()
        self.assertEqual(self.later(5), {PSID: "skipped_off"})
        self.assertEqual((self.engine.calls, self.graph.calls), ([], []))
        self.turn("Camry 2015")  # a new message is answered, the earlier ones read before it
        self.assertEqual(self.engine.calls[0]["text"], "ხუნდები\nზეთი\nCamry 2015")

    def test_messages_older_than_the_maximum_age_are_never_answered(self):
        self.store.settings_row.update(bot_enabled_at=START - timedelta(days=3),
                                       max_reply_age_minutes=60)
        self.say("ხუნდები", ago=2 * 3600)
        self.pump()
        self.assertEqual(self.later(5), {PSID: "skipped_old"})
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.store.turns_today(), 0)
        self.turn("ზეთი", ago=30 * 60)
        self.assertEqual(len(self.graph.sends()), 1)

    def test_a_chat_switched_off_is_left_to_the_shop(self):
        mid = self.say("ხუნდები")
        self.pump()
        self.store.threads[PSID]["bot_enabled"] = False
        self.assertEqual(self.later(5), {PSID: "skipped_chat_off"})
        self.assertEqual((self.engine.calls, self.graph.calls), ([], []))
        self.assertEqual(self.reply(mid)["status"], "skipped_chat_off")
        self.store.threads[PSID]["bot_enabled"] = True
        self.assertEqual(self.worker.run_burst(PSID), "duplicate")

        def chat_off(call):
            self.store.threads[PSID]["bot_enabled"] = False
            return ok()(call)

        self.engine.results = [chat_off]
        self.assertEqual(self.turn("ზეთიც")[1], {PSID: "skipped_chat_off"})
        self.assertEqual(self.graph.sends(), [])

    def test_the_shop_answering_during_the_turn_stops_the_reply(self):
        shop_text = "დიახ, გვაქვს. რომელი მანქანისთვის?"

        def shop_answers(call):
            self.shop(shop_text)
            self.assertEqual(self.pump(), ["echo_shop"])
            self.assertTrue(call["cancel"].is_set())  # the engine is stopped at once
            return ok("ავტომატური პასუხი.")(call)

        self.engine.results = [shop_answers]
        mid, results = self.turn("ხუნდები გაქვთ?")
        self.assertEqual(results, {PSID: "skipped_shop_answered"})
        self.assertEqual(self.graph.sends(), [])
        self.assertEqual(self.reply(mid)["engine_status"], "OK")
        saved = self.state()  # the shop's words answer the message in the engine's view
        self.assertEqual(saved["history"][-1]["answer"], shop_text)
        self.assertEqual(saved["memory"]["transcript"][-1],
                         {"user": "ხუნდები გაქვთ?", "assistant": shop_text})

    def test_a_message_the_shop_answered_first_is_left_and_its_words_are_kept(self):
        self.say("ხუნდები გაქვთ?")
        self.pump()
        self.clock.advance(2)
        self.shop("დიახ. რომელი მანქანისთვის?")
        self.assertEqual(self.pump(), ["echo_shop"])
        self.assertEqual(self.later(5), {PSID: "nothing"})
        self.assertEqual((self.engine.calls, self.store.reply_rows), ([], []))
        self.turn("Camry 2015")
        call = self.engine.calls[0]
        self.assertEqual(call["text"], "Camry 2015")
        self.assertEqual(call["state"]["memory"]["transcript"],
                         [{"user": "ხუნდები გაქვთ?", "assistant": "დიახ. რომელი მანქანისთვის?"}])

    def test_echoes_of_an_ignored_app_never_count_as_the_shops_answer(self):
        self.worker = self.make_worker(ignore_app_ids=(AUTO_APP,))

        def greeting(call):
            self.shop("გამარჯობა! მალე გიპასუხებთ.", app_id=AUTO_APP)
            self.assertEqual(self.pump(), ["echo_ignored_app"])
            self.assertFalse(call["cancel"].is_set())
            return ok()(call)

        self.engine.results = [greeting]
        self.assertEqual(self.turn()[1], {PSID: "sent"})

    def test_a_newer_message_supersedes_the_turn_and_the_next_one_reads_both(self):
        def newer(call):
            self.clock.advance(2)
            self.say("უკანაც მინდა")
            self.assertEqual(self.pump(), ["stored"])
            self.assertTrue(call["cancel"].is_set())
            return ok("წინა ხუნდები ქვემოთაა.")(call)

        self.engine.results = [newer]
        first, results = self.turn("წინა ხუნდები")
        self.assertEqual(results, {PSID: "skipped_superseded"})
        self.assertEqual(self.graph.sends(), [])
        self.assertEqual(self.reply(first)["engine_status"], "OK")
        self.assertEqual(self.later(4), {})
        self.assertEqual(self.later(1), {PSID: "sent"})
        self.assertEqual(self.engine.calls[1]["text"], "წინა ხუნდები\nუკანაც მინდა")
        self.assertEqual(len(self.graph.sends()), 1)

    def test_a_superseded_turn_that_honours_the_cancel_sends_nothing(self):
        def honour(call):
            self.say("სხვა ნაწილიც")
            self.pump()
            return cancelled(call) if call["cancel"].is_set() else ok()(call)

        self.engine.results = [honour]
        first, results = self.turn("ფილტრი")
        self.assertEqual(results, {PSID: "skipped_superseded"})
        self.assertEqual(self.reply(first)["engine_status"], "CANCELLED")
        self.assertEqual(self.later(5), {PSID: "sent"})


class CapTests(WorkerCase):
    def test_the_daily_cap_sends_one_notice_a_day_and_flags_the_chat(self):
        self.store.settings_row["daily_turn_limit"] = 1
        notice = TEXTS["ka"]["cap_phone"].replace("{phone}", PHONE)
        self.turn("ხუნდები")
        second, results = self.turn("ზეთი")
        self.assertEqual(results, {PSID: "skipped_cap"})
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.graph.texts(), [ANSWER, notice])
        self.assertEqual(self.reply(second)["status"], "skipped_cap")
        self.assertEqual(self.reply(f"cap:{PSID}:2026-11-11")["status"], "sent")
        self.assertIn((PSID, "daily_cap"), self.store.flags)
        self.assertEqual(self.store.turns_today(), 1)  # capped attempts used no turn
        self.turn("ფილტრი")
        self.assertEqual(len(self.graph.sends()), 2)  # one notice a day
        self.turn("სანთლები", psid=OTHER)
        self.assertEqual(self.graph.texts(OTHER), [notice])
        self.clock.advance(11 * 3600)  # past midnight in Tbilisi
        self.turn("ზეთი ხვალ")
        self.assertEqual(self.reply(f"cap:{PSID}:2026-11-12")["status"], "sent")

    def test_one_customers_cap_leaves_the_others_answered(self):
        self.store.settings_row["customer_daily_turn_limit"] = 1
        self.turn("ხუნდები")
        self.turn("ზეთი")
        self.assertEqual(len(self.engine.calls), 1)
        self.turn("ფილტრი", psid=OTHER)
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.graph.texts(OTHER), [ANSWER])

    def test_a_cap_of_zero_allows_no_turn(self):
        self.store.settings_row["daily_turn_limit"] = 0
        self.assertEqual(self.turn()[1], {PSID: "skipped_cap"})
        self.assertEqual(self.engine.calls, [])

    def test_past_a_cap_the_engine_is_not_warmed_up_for_nothing(self):
        # engine.prepare() asks the provider to read the prompt into its cache, a billed
        # request; a burst that ends as skipped_cap needs none.
        self.store.settings_row["customer_daily_turn_limit"] = 1
        self.turn("ხუნდები")
        self.assertEqual(self.engine.prepared, 1)
        self.assertEqual(self.turn("ზეთი")[1], {PSID: "skipped_cap"})
        self.assertEqual(self.engine.prepared, 1)
        self.turn("ფილტრი", psid=OTHER)  # another customer still gets the warm engine
        self.assertEqual(self.engine.prepared, 2)
        self.store.settings_row["daily_turn_limit"] = 2
        self.assertEqual(self.turn("სანთლები", psid=ADMIN_PSID)[1], {ADMIN_PSID: "skipped_cap"})
        self.assertEqual(self.engine.prepared, 2)


class CommandTests(WorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_thread(ADMIN_PSID, PAGE)
        self.store.threads[ADMIN_PSID]["is_admin"] = True

    def command(self, text: str, mid: str | None = None) -> tuple[str, str]:
        self.say(text, psid=ADMIN_PSID, mid=mid)
        outcome = self.pump()[0]
        texts = self.graph.texts(ADMIN_PSID)
        return outcome, texts[-1] if texts else ""

    def test_off_status_and_on_from_the_admins_own_chat(self):
        mid = f"m_cmd{next(MIDS)}"
        self.assertEqual(self.command("#bot off", mid), ("command", bot_switched_text("ka", False)))
        self.assertFalse(self.store.settings_row["bot_enabled"])
        self.assertEqual(self.store.audits[-1][:3], ("messenger:9999", "messenger.bot_off",
                                                     "settings"))
        audits = len(self.store.audits)
        self.assertEqual(self.command("#bot off", mid)[0], "command_duplicate")  # replayed
        self.assertEqual(len(self.store.audits), audits)
        outcome, text = self.command("  #BOT Status ")  # answered while the bot is off
        self.assertEqual(outcome, "command")
        self.assertEqual(text.split("\n"), [
            TEXTS["ka"]["status_off"], "ბოლო ცვლილება: 11.11.2026 14:00",
            "დღევანდელი პასუხები: 0 / 150", TEXTS["ka"]["status_comments_off"],
            "დღევანდელი ხარჯი: ≈$0.00"])
        self.assertEqual(self.store.audits[-1][:2], ("messenger:9999", "messenger.bot_status"))
        self.store.settings_row["auto_reply_confirmed_off"] = False
        self.assertEqual(self.command("#bot on")[1], bot_on_refused_text("ka"))
        self.assertFalse(self.store.settings_row["bot_enabled"])
        self.assertEqual(self.store.audits[-1][:2], ("messenger:9999", "messenger.bot_on_refused"))
        self.store.settings_row["auto_reply_confirmed_off"] = True
        self.assertEqual(self.command("#bot on")[1], bot_switched_text("ka", True))
        self.assertTrue(self.store.settings_row["bot_enabled"])
        self.assertEqual(self.command("#bot on")[1], bot_switched_text("ka", True, changed=False))
        self.assertEqual(self.command("#bot please stop")[1], bot_help_text("ka"))
        self.assertEqual(self.store.audits[-1][1], "messenger.bot_help")
        self.assertEqual(self.engine.calls, [])
        self.assertEqual([row["status"] for row in self.store.reply_rows
                          if row["channel"] == "command"], ["sent"] * 6)
        # Commands never become a burst.
        self.assertEqual(self.later(5), {})
        self.assertEqual(self.engine.calls, [])

    def test_a_command_whose_first_try_broke_is_applied_when_the_row_comes_back(self):
        self.store.fail["start_reply"] = OSError("connection reset")  # after the message was kept
        self.say("#bot off", psid=ADMIN_PSID)
        with self.assertLogs("automotive.messenger_worker", "WARNING"):
            self.assertEqual(self.pump(), ["claimed"])  # put back after a backoff
        self.assertTrue(self.store.settings_row["bot_enabled"])
        self.clock.advance(2)
        self.assertEqual(self.pump(), ["command"])
        self.assertFalse(self.store.settings_row["bot_enabled"])
        self.assertEqual(self.graph.texts(ADMIN_PSID), [bot_switched_text("ka", False)])

    def test_a_command_that_could_not_be_applied_tells_the_admin(self):
        self.store.fail["set_bot"] = OSError("connection reset")
        with self.assertLogs("automotive.messenger_worker", "WARNING"):
            self.assertEqual(self.command("#bot off"), ("command_failed", bot_failed_text("ka")))
        self.assertTrue(self.store.settings_row["bot_enabled"])
        (row,) = [row for row in self.store.reply_rows if row["channel"] == "command"]
        self.assertEqual((row["status"], row["error_text"]), ("failed", "command_failed"))
        self.assertEqual(self.command("#bot off"), ("command", bot_switched_text("ka", False)))

    def test_on_also_resumes_a_bot_the_ai_provider_paused(self):
        self.store.pause_for_provider()
        self.assertEqual(self.command("#bot on")[1], bot_switched_text("ka", True))
        self.assertEqual(self.store.settings_row["bot_paused_reason"], "")

    def test_a_command_whose_reply_failed_is_still_kept_from_the_engine(self):
        self.graph.errors["send_message"] = [GraphError(500, 2, None, "OAuthException", "down")]
        self.command("#bot status")
        self.assertEqual([row["status"] for row in self.store.reply_rows
                          if row["channel"] == "command"], ["failed"])
        self.clock.advance(1)
        self.say("გამარჯობა", psid=ADMIN_PSID)
        self.pump()
        self.assertEqual(self.later(5), {ADMIN_PSID: "sent"})
        self.assertEqual(self.engine.calls[0]["text"], "გამარჯობა")

    def test_bot_commands_from_a_customer_are_only_words(self):
        self.assertEqual(self.turn("#bot off")[1], {PSID: "sent"})
        self.assertEqual(self.engine.calls[0]["text"], "#bot off")
        self.assertTrue(self.store.settings_row["bot_enabled"])


class FailureTests(WorkerCase):
    def test_a_refusing_ai_account_pauses_the_bot_and_messages_nobody(self):
        self.engine.results = [failure("PROVIDER_BILLING", engine_status="PROVIDER_BILLING")]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "paused_provider"})
        self.assertEqual(self.graph.sends(), [])
        self.assertEqual(self.store.settings_row["bot_paused_reason"], "ai_provider")
        self.assertIn((PSID, "ai_provider"), self.store.flags)
        self.assertEqual(self.reply(mid)["engine_status"], "PROVIDER_BILLING")
        other, results = self.turn("ზეთი", psid=OTHER)
        self.assertEqual(results, {OTHER: "skipped_paused"})
        self.assertEqual(len(self.engine.calls), 1)
        self.store.set_bot(True, "boss")  # resumed: what came in meanwhile stays unanswered
        self.assertEqual(self.worker.run_burst(OTHER), "duplicate")

    def test_an_engine_without_a_model_key_pauses_too(self):
        self.engine.results = [failure("NOT_CONFIGURED", engine_status="NOT_CONFIGURED")]
        self.assertEqual(self.turn()[1], {PSID: "paused_provider"})
        self.assertEqual(self.graph.sends(), [])

    def test_a_dead_token_is_marked_broken_and_nothing_more_is_sent(self):
        self.graph.errors["send_message"] = [
            GraphError(400, 190, 460, "OAuthException", "Error validating access token")]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "failed"})
        row = self.reply(mid)
        self.assertEqual((row["error_code"], row["error_subcode"]), (190, 460))
        self.assertEqual(self.store.tokens["page"]["status"], "broken")
        self.assertIn((PSID, "send_token_dead"), self.store.flags)
        calls = len(self.graph.calls)
        later, results = self.turn("ზეთი", psid=OTHER)
        self.assertEqual(results, {OTHER: "failed"})
        self.assertEqual(len(self.graph.calls), calls)  # no typing indicator, no send
        self.assertEqual(len(self.engine.calls), 1)  # and no turn spent
        self.assertEqual(self.store.turns_today(), 1)  # the first, whose engine ran
        self.assertEqual(self.reply(later)["error_text"], "send_token_broken")
        self.store.tokens["page"]["status"] = "ok"  # the dashboard reconnected
        self.turn("ფილტრი", psid=OTHER)
        self.assertEqual(len(self.graph.sends(OTHER)), 1)

    def test_an_engine_failure_sends_one_fallback_with_the_shop_phone(self):
        self.engine.results = [failure("PROVIDER_UNAVAILABLE",
                                       engine_status="PROVIDER_UNAVAILABLE"),
                               failure("TIMEOUT", status="timeout")]
        mid, results = self.turn()
        self.assertEqual(results, {PSID: "fallback_sent"})
        texts = TEXTS["ka"]
        call = texts["fallback_call"].replace("{phone}", PHONE)
        self.assertEqual(self.graph.texts(), [f"{texts['fallback']} {call}"])
        self.assertIn((PSID, "engine_failed"), self.store.flags)
        self.assertEqual(self.reply(mid)["engine_status"], "PROVIDER_UNAVAILABLE")
        self.store.settings_row["fallback_ka"] = "ახლა ვერ გიპასუხებთ."
        self.engine.phone = ""
        self.turn("ზეთი")
        self.assertEqual(self.graph.texts()[-1], "ახლა ვერ გიპასუხებთ.")
        self.assertIn((PSID, "engine_timeout"), self.store.flags)

    def test_a_turn_cut_short_by_the_worker_stopping_sends_no_fallback(self):
        def closing(call):
            self.worker.request_stop()
            return failure("CHAT_CLOSED")(call)

        self.engine.results = [closing]
        self.assertEqual(self.turn()[1], {PSID: "abandoned"})
        self.assertEqual(self.graph.sends(), [])
        self.assertIn((PSID, "worker_stopped"), self.store.flags)

    def test_two_vins_get_a_request_for_one(self):
        self.engine.results = [failure("CONFLICTING_VINS")]
        self.assertEqual(self.turn("1HGCM82633A004352 ან 4T1BF1FK5FU000001")[1], {PSID: "sent"})
        self.assertEqual(self.graph.texts(), [conflicting_vins_text("ka")])

    def test_meta_refusing_the_typing_indicator_spends_no_turn(self):
        cases = [(GraphError(400, 10, None, "OAuthException", "gated"), "blocked_by_meta",
                  [("7000000000000000", "blocked_by_meta")]),
                 (GraphError(400, 10, 2018278, "OAuthException", "window"), "skipped_old", []),
                 (GraphError(400, 551, None, "OAuthException", "unavailable"), "failed", [])]
        for index, (error, status, flags) in enumerate(cases):
            psid = f"700000000000000{index}"
            self.store.flags.clear()
            self.graph.errors["sender_action"] = [error]
            mid, results = self.turn("ხუნდები", psid=psid)
            self.assertEqual(results, {psid: status})
            self.assertEqual(self.reply(mid)["error_code"], error.code)
            self.assertEqual(self.store.flags, flags)
        self.assertEqual((self.engine.calls, self.graph.sends()), ([], []))
        # Closed before the engine ran ('blocked_by_meta', 'failed'): the caps count nothing.
        self.assertEqual(self.store.turns_today(), 0)
        self.graph.errors["sender_action"] = [
            GraphError(500, 2, None, "OAuthException", "temporary")]
        self.assertEqual(self.turn("ზეთი", psid=OTHER)[1], {OTHER: "sent"})


class InboxTests(WorkerCase):
    def test_echoes_from_our_apps_are_the_bots_and_every_other_the_shops(self):
        self.store.tokens["send_page"] = {"purpose": "send_page", "app_id": SEND_APP,
                                          "subject_id": PAGE, "status": "ok",
                                          "status_detail": ""}
        self.store.values["send_page"] = SEND_TOKEN
        self.shop("bot words", app_id=APP)
        self.shop("sent through the send-only app", app_id=SEND_APP)
        self.shop("typed in Business Suite", app_id=OTHER_APP)
        self.shop("typed in the page inbox", app_id=None)
        self.assertEqual(self.pump(), ["echo_bot", "echo_bot", "echo_shop", "echo_shop"])
        self.assertEqual([row["sender"] for row in self.store.message_rows],
                         ["bot", "bot", "shop", "shop"])
        self.assertIsNotNone(self.store.threads[PSID]["last_shop_at"])
        # A message this process sent is the bot's even when its echo names no app.
        self.store.fail["add_sent_mid"] = RuntimeError("database blip")
        self.turn("ხუნდები", psid=OTHER)
        sent = self.graph.sends(OTHER)[0]
        self.assertEqual((sent[3], sent[4]), ("send_page", False))  # no proof for another app
        self.assertFalse(any(row["mid"] == "m_bot_1" for row in self.store.message_rows))
        self.shop("our reply's echo", psid=OTHER, app_id=None, mid="m_bot_1")
        self.assertEqual(self.pump(), ["echo_bot"])

    def test_a_row_delivered_twice_is_handled_once(self):
        mid = self.say("ხუნდები")
        self.say("ხუნდები", mid=mid)
        self.assertEqual(self.pump(), ["stored", "duplicate"])
        self.assertEqual(self.later(5), {PSID: "sent"})
        self.assertEqual(len(self.graph.sends()), 1)
        self.say("ხუნდები", mid=mid)
        self.assertEqual(self.pump(), ["duplicate"])
        self.assertEqual(self.later(0), {PSID: "nothing"})
        self.assertEqual(len(self.graph.sends()), 1)
        self.assertEqual(len([row for row in self.store.message_rows
                              if row["sender"] == "customer"]), 1)

    def test_a_message_on_the_standby_channel_is_kept_but_never_answered(self):
        mid = self.say("ხუნდები", field="standby")
        self.assertEqual(self.pump(), ["standby"])
        self.assertEqual(self.later(5), {})
        for channel in ("messenger", "notice"):  # closed for a turn and for a text request
            self.assertEqual(self.reply(mid, channel)["status"], "skipped_chat_off")
        self.assertEqual(self.worker.recover(), 1)
        self.assertEqual(self.later(5), {PSID: "duplicate"})
        self.assertEqual((self.engine.calls, self.graph.calls), ([], []))
        photo = self.say(attachments=("image",), psid=OTHER, field="standby")  # no words either
        self.assertEqual(self.pump(), ["standby"])
        self.assertEqual(self.reply(photo, "notice")["status"], "skipped_chat_off")
        self.assertEqual(self.worker.recover(), 2)
        self.assertEqual(self.later(5), {PSID: "duplicate", OTHER: "duplicate"})
        self.assertEqual(self.graph.calls, [])

    def test_a_button_tap_is_a_message_and_a_referral_marks_the_chats_source(self):
        key = "pb:" + "a" * 64
        self.store.deliver("messages", {
            "sender": {"id": PSID}, "recipient": {"id": PAGE}, "timestamp": ms(self.clock.now()),
            "postback": {"title": "დაწყება", "payload": "GET_STARTED",
                         "referral": {"source": "ADS", "type": "OPEN_THREAD",
                                      "ad_id": "120200000000001"}}}, event_key=key)
        self.assertEqual(self.pump(), ["stored"])
        row = self.store.message_rows[-1]
        self.assertEqual((row["mid"], row["body"], row["quick_reply"], row["referral_ad_id"]),
                         (key, "დაწყება", "GET_STARTED", "120200000000001"))
        thread = self.store.threads[PSID]
        self.assertEqual((thread["source"], thread["source_ad_id"]), ("ads", "120200000000001"))
        self.later(5)
        self.assertEqual(self.engine.calls[0]["text"], "დაწყება")
        self.store.deliver("messages", {
            "sender": {"id": PSID}, "recipient": {"id": PAGE}, "timestamp": ms(self.clock.now()),
            "referral": {"source": "SHORTLINK", "type": "OPEN_THREAD", "ref": "spring"}})
        self.assertEqual(self.pump(), ["referral"])
        thread = self.store.threads[PSID]
        self.assertEqual((thread["source"], thread["source_ad_id"]), ("shortlink", ""))
        self.assertEqual(self.later(5), {})

    def test_events_of_another_page_or_without_a_connection_are_skipped(self):
        self.store.deliver("messages", {
            "sender": {"id": PSID}, "recipient": {"id": OTHER_PAGE},
            "timestamp": ms(self.clock.now()), "message": {"mid": "m_x", "text": "x"}},
            page_id=OTHER_PAGE)
        self.assertEqual(self.pump(), ["other_page"])
        self.store.page_row = None
        self.clock.advance(11)  # past the brief cache of the connected page
        self.say("ხუნდები")
        self.assertEqual(self.pump(), ["not_connected"])
        self.assertEqual((self.store.message_rows, self.store.threads), ([], {}))
        self.store.deliver("messages", {"sender": {"id": PSID}, "recipient": {"id": PAGE},
                                        "timestamp": ms(self.clock.now()),
                                        "read": {"watermark": 1}})
        self.assertEqual(self.pump(), ["ignored: read"])

    def test_a_row_that_fails_is_retried_after_a_backoff(self):
        self.store.fail["upsert_thread"] = RuntimeError("connection reset")
        self.say("ხუნდები")
        with self.assertLogs("automotive.messenger_worker", "WARNING"):
            self.assertEqual(self.pump(), ["claimed"])
        self.assertEqual(self.pump(), [])  # held for its backoff
        self.clock.advance(2)
        self.assertEqual(self.pump(), ["stored"])
        row = self.store.inbox[-1]
        self.assertEqual((row["attempts"], row["history"]), (2, ["retry: RuntimeError", "stored"]))
        self.store.fail["upsert_thread"] = RuntimeError("again")
        self.say("ზეთი")
        self.store.inbox[-1]["attempts"] = 4
        with self.assertLogs("automotive.messenger_worker", "WARNING"):
            self.pump()
        self.assertEqual(self.store.inbox[-1]["outcome"], "failed: RuntimeError")

    def test_comments_go_to_the_comments_module(self):
        handled = []

        def handle(ctx, event, engine, *, cost=None):
            handled.append((ctx, event, engine, cost))
            return "replied"

        change = {"field": "feed", "value": {
            "item": "comment", "verb": "add", "comment_id": f"{PAGE}_1_2",
            "post_id": f"{PAGE}_1", "from": {"id": "7000000000004321", "name": "ნინო"},
            "message": "ფასი?", "created_time": int(self.clock.now().timestamp())}}
        module = SimpleNamespace(handle_comment_event=handle)
        with mock.patch.dict(sys.modules, {"automotive.meta_comments": module}):
            self.store.deliver("feed", change)
            self.assertEqual(self.pump(), ["comment: replied"])
        ctx, event, engine, cost = handled[0]
        self.assertIs(ctx, self.ctx)
        self.assertIs(engine, self.engine)
        self.assertIs(cost, self.worker.cost)  # comment turns are priced like the chats'
        self.assertEqual((event.comment_id, event.verb, event.message),
                         (f"{PAGE}_1_2", "add", "ფასი?"))
        with mock.patch("automotive.messenger_worker.importlib.import_module",
                        side_effect=ImportError("absent")):
            self.store.deliver("feed", change)
            self.assertEqual(self.pump(), ["comments_unavailable"])

    def test_an_item_too_large_to_store_flags_the_chat(self):
        self.store.deliver("messages", {"_oversized": True, "_bytes": 70000, "_kind": "msg",
                                        "ids": {"sender": PSID, "recipient": PAGE,
                                                "mid": "m_big", "timestamp": ms(START)}})
        self.assertEqual(self.pump(), ["oversized"])
        self.assertIn((PSID, "oversized_message"), self.store.flags)
        self.assertEqual(self.later(5), {})


class JobTests(WorkerCase):
    def jobs(self, **results) -> list[str]:
        ran: list[str] = []

        def make(name, value):
            def run(ctx):
                ran.append(name)
                if isinstance(value, Exception):
                    raise value
                return value
            return run

        values = {"names": {"conversations": 4, "updated": 2}, "comments": 3, "posts": 5,
                  "page_stats": 1, "ads": [{"id": 1}, {"id": 2}],
                  "subscription": {"page_id": PAGE, "page_resubscribed": True,
                                   "app_resubscribed": False},
                  "token_health": [{"status": "ok", "warn": False},
                                   {"status": "broken", "warn": True}], **results}
        self.worker = self.make_worker(jobs={name: make(name, value)
                                             for name, value in values.items()})
        return ran

    def runs(self) -> dict:
        return {row["job"]: (row["status"], row["records"], row["detail"])
                for row in self.store.runs}

    def test_jobs_run_when_due_and_on_request_and_record_counts_only(self):
        self.jobs()
        self.assertEqual(self.worker.run_jobs(startup=True), {name: "ok" for name in JOB_NAMES})
        runs = self.runs()
        self.assertEqual(runs["names"], ("ok", 2, "conversations=4 updated=2"))
        self.assertEqual(runs["subscription"],
                         ("ok", 1, "page_resubscribed=True app_resubscribed=False"))
        self.assertEqual(runs["token_health"], ("ok", 2, "tokens=2 broken=1 warn=1"))
        self.assertEqual(runs["ads"], ("ok", 2, ""))
        self.assertEqual(runs["purge"], (
            "ok", 3, "inbox=2 job_requests=0 messages=1 replies=0 runs=0 threads=0"))
        self.assertEqual(self.store.purged, [365])
        self.assertEqual(self.worker.run_jobs(), {})
        self.clock.advance(601)
        self.assertEqual(self.worker.run_jobs(), {"names": "ok", "comments": "ok"})
        self.assertEqual(self.worker.run_jobs(startup=True), {"subscription": "ok"})
        for job in ("ads", "ads", "nonsense"):
            self.store.request(job)
        self.assertEqual(self.worker.run_jobs(), {"ads": "ok", "nonsense": "skipped"})
        self.assertEqual(self.runs()["nonsense"], ("skipped", 0, "unknown_job"))

    def test_a_feature_jobs_own_status_and_codes_reach_its_run(self):
        from automotive.meta_sync import SyncCount

        self.jobs(ads=SyncCount(0, "skipped", "no_ads_token"),
                  posts=SyncCount(4, "ok", "posts=4 insights_refused code=10"))
        results = self.worker.run_jobs(startup=True)
        self.assertEqual((results["ads"], results["posts"]), ("skipped", "ok"))
        runs = self.runs()
        self.assertEqual(runs["ads"], ("skipped", 0, "no_ads_token"))
        self.assertEqual(runs["posts"], ("ok", 4, "posts=4 insights_refused code=10"))

    def test_failures_record_codes_and_a_missing_page_waits(self):
        self.jobs(names=GraphError(400, 190, None, "OAuthException", "expired"),
                  comments=NotConnected("no page"), page_stats=RuntimeError("private detail"),
                  subscription=ConnectError("NOT_CONFIGURED",
                                            {"missing": ["MESSENGER_PUBLIC_URL"]}))
        results = self.worker.run_jobs(startup=True)
        self.assertEqual([results[name] for name in ("names", "comments", "subscription")],
                         ["error", "skipped", "skipped"])
        runs = self.runs()
        self.assertEqual(runs["names"], (
            "error", 0, "graph_error http=400 code=190 subcode=None type=OAuthException"))
        self.assertEqual((runs["comments"][2], runs["subscription"][2], runs["page_stats"][2]),
                         ("not_connected", "not_configured", "RuntimeError"))
        self.clock.advance(1800)  # a failed daily job is tried again within half an hour
        self.assertIn("page_stats", self.worker.run_jobs())
        self.store.page_row = None
        count = len(self.store.runs)
        self.clock.advance(601)
        self.assertEqual(self.worker.run_jobs().get("names"), "skipped")
        self.assertEqual(len(self.store.runs), count)  # interval runs wait without a record
        self.store.request("names")
        self.worker.run_jobs()
        self.assertEqual(self.store.runs[-1]["detail"], "not_connected")

    def test_feature_modules_not_there_yet_are_skipped(self):
        self.worker = self.make_worker(jobs={"subscription": lambda ctx: 0,
                                             "names": lambda ctx: 0,
                                             "token_health": lambda ctx: [],
                                             "purge": lambda ctx: {}})
        with mock.patch("automotive.messenger_worker.importlib.import_module",
                        side_effect=ImportError("absent")):
            self.assertEqual(self.worker.run_job("ads"), "skipped")
            self.assertEqual(self.store.runs, [])
            self.store.request("ads")
            self.assertEqual(self.worker.run_jobs()["ads"], "skipped")
        self.assertEqual(self.runs()["ads"], ("skipped", 0, "not_available"))
        # A module that is there but broken is skipped the same way; the other jobs still run.
        self.store.runs.clear()
        self.clock.advance(86400)
        with mock.patch("automotive.messenger_worker.importlib.import_module",
                        side_effect=SyntaxError("broken module")):
            results = self.worker.run_jobs()
        self.assertEqual({name: results[name] for name in ("comments", "posts", "ads")},
                         {"comments": "skipped", "posts": "skipped", "ads": "skipped"})
        self.assertEqual({name: results[name] for name in ("names", "token_health", "purge")},
                         {"names": "ok", "token_health": "ok", "purge": "ok"})


class ThreadTests(WorkerCase):
    typing_seconds = 0.02

    def test_typing_is_refreshed_while_the_engine_works(self):
        def slow(call):
            time.sleep(0.25)
            return ok()(call)

        self.engine.results = [slow]
        self.assertEqual(self.turn()[1], {PSID: "sent"})
        actions = self.graph.actions()
        self.assertEqual(actions[0], "mark_seen")
        self.assertGreaterEqual(actions.count("typing_on"), 3)

    def test_a_switch_found_off_while_typing_stops_the_engine(self):
        def slow(call):
            self.store.set_bot(False, "boss")
            stopped = call["cancel"].wait(2)
            return cancelled(call) if stopped else ok()(call)

        self.engine.results = [slow]
        self.assertEqual(self.turn()[1], {PSID: "skipped_off"})
        self.assertEqual(self.graph.sends(), [])


class RuntimeTests(unittest.TestCase):
    def test_the_threads_answer_a_message_and_stop_cleanly(self):
        store, graph, engine = FakeStore(RealClock()), FakeGraph(), FakeEngine()
        store.settings_row.update(bot_enabled=True, auto_reply_confirmed_off=True,
                                  bot_enabled_at=datetime.now(UTC) - timedelta(minutes=1))
        worker = MessengerWorker(MetaContext(SETTINGS, store, graph, None), engine,
                                 quiet_seconds=0.05, max_wait_seconds=1, poll_seconds=0.05,
                                 jobs={name: (lambda ctx: 0) for name in JOB_NAMES})
        worker.start()
        try:
            store.deliver("messages", {"sender": {"id": PSID}, "recipient": {"id": PAGE},
                                       "timestamp": ms(datetime.now(UTC)),
                                       "message": {"mid": "m_live", "text": "ხუნდები"}})
            deadline = time.monotonic() + 5
            while not graph.sends() and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            worker.stop(timeout=5)
        self.assertEqual(len(graph.sends()), 1)
        self.assertEqual(store.inbox[0]["outcome"], "stored")
        self.assertEqual([row["status"] for row in store.reply_rows], ["sent"])
        self.assertEqual({row["job"] for row in store.runs}, set(JOB_NAMES))
        alive = [thread.name for thread in threading.enumerate()
                 if thread.name in ("messenger-inbox", "messenger-bursts", "messenger-jobs")]
        self.assertEqual(alive, [])


class ProcessTests(unittest.TestCase):
    def test_the_worker_refuses_to_start_and_names_what_is_missing(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main([], env={}), 2)
        for name in ("META_APP_ID", "META_APP_SECRET", "META_TOKEN_ENCRYPTION_KEY",
                     "MESSENGER_DATABASE_URL"):
            self.assertIn(name, output.getvalue())
        secret = "S3cretValue" + "x" * 20
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main([], env={"META_APP_ID": "12ab", "META_APP_SECRET": secret}), 2)
        self.assertIn("META_APP_ID", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_the_inbox_channel_is_the_receivers(self):
        from automotive.meta_receiver import INBOX_CHANNEL as RECEIVER_CHANNEL
        self.assertEqual(INBOX_CHANNEL, RECEIVER_CHANNEL)

    def test_only_exact_bot_commands_are_commands(self):
        cases = {"#bot off": "off", " #BOT ON ": "on", "#bot status": "status", "#bot": "help",
                 "#bot what now": "help", "#botany": None, "bot off": None,
                 "please #bot off": None, "": None, None: None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(command_of(text), expected)

    def test_costs_come_from_the_usage_or_an_estimate(self):
        self.assertEqual(usage_cost({"cost_usd": 0.031}), 0.031)
        self.assertIsNone(usage_cost({"input_tokens": 5}))
        estimate = estimated_cost("claude-sonnet-5-5")
        self.assertEqual(estimate({"cost_usd": 0.02}), 0.02)
        self.assertGreater(estimate({"input_tokens": 1000, "output_tokens": 100}), 0)
        self.assertIsNone(estimate({"model_requests": 1}))


class RealEngineTests(WorkerCase):
    """The real assistant engine (ChatService) with the scripted model: the worker's state and
    the renderer's buttons round-trip through the store as JSON."""

    def test_a_question_its_tapped_answer_and_the_saved_state(self):
        asked = {"text": "წინა თუ უკანა?", "options": [option("წინა", "front pads: r1, r2"),
                                                       option("უკანა", "rear pads: r3")]}

        def first(request):
            found = handles(request)
            return done(reply="Camry 2015-ისთვის ორივე გვაქვს.", question=asked, suggestions=[],
                        outcome="asked", car_label="Toyota Camry 2015",
                        notes=f"Camry 2015 pads; rear is {found['04466-33180']}")

        def second(request):
            sent = context_of(request)
            self.assertEqual(sent["tapped_option"],
                             {"index": 2, "label": "უკანა", "meaning": "rear pads: r3"})
            rear = [row[0] for row in sent["rows"]["rows"] if row[1] == REAR]
            return done(reply="უკანა ხუნდები ქვემოთაა.", outcome="offered",
                        car_label=sent["car_label"], products=[product(rear[0])])

        chat = service(Script(says(use("search_catalog",
                                       search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                              first, second))
        engine = MessengerEngine(MessengerConversations(chat, Search()), chat.catalog_store,
                                 poll_seconds=0.005)
        self.addCleanup(engine.close)
        self.worker = MessengerWorker(self.ctx, engine)
        self.assertEqual(self.turn("Camry 2015 ხუნდები")[1], {PSID: "sent"})
        message = self.graph.sends()[-1][2]
        self.assertEqual([(entry["title"], entry["payload"])
                          for entry in message["quick_replies"]],
                         [("წინა", "CHOICE:1"), ("უკანა", "CHOICE:2")])
        saved = self.state()
        self.assertEqual(saved[CHANNEL_KEY], {"options": ["წინა", "უკანა"], "numbered": False})
        self.assertEqual(saved["pending"]["options"][1]["meaning"], "rear pads: r3")
        mid, results = self.turn("უკანა", quick_reply="CHOICE:2")
        self.assertEqual(results, {PSID: "sent"})
        self.assertIn(REAR, self.graph.texts()[-1])
        self.assertEqual(self.reply(mid)["engine_status"], "OK")
        self.assertIsNone(self.state()["pending"])


ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = (ROOT / "ops/sql/provision_automotive_roles.sql").read_text(encoding="utf-8")
TABLES = ("meta_inbox", "meta_page", "meta_tokens", "messenger_threads", "messenger_messages",
          "messenger_replies", "meta_sync_runs", "meta_job_requests", "meta_comments",
          "meta_forgotten", "audit_log")


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class WorkerDatabaseTests(unittest.TestCase):
    """The worker on the real MetaStore, every call as a member of garage_messenger only; the
    dashboard's part as garage_site, the receiver's appends as garage_webhook."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.suffix = uuid.uuid4().hex[:12]
        cls.databases: list[str] = []
        cls.cluster_roles: list[str] = []
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.addClassCleanup(cls._drop)
        name = "meta_worker_" + cls.suffix
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cls.databases.append(name)
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = name
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as connection:
            connection.execute(ROLES_SQL)
            apply_migrations_on_connection(connection)
        cls.roles = {key: cls._role(key, capability) for key, capability in (
            ("worker", "garage_messenger"), ("site", "garage_site"), ("hook", "garage_webhook"))}
        cls.cipher = TokenCipher(os.urandom(32))

    @classmethod
    def _drop(cls) -> None:
        for name in reversed(cls.databases):
            cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        for name in reversed(cls.cluster_roles):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        cls.admin.close()

    @classmethod
    def _role(cls, key: str, capability: str) -> str:
        name = f"worker_{key}_{cls.suffix}"
        cls.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        ).format(sql.Identifier(name)))
        cls.cluster_roles.append(name)
        cls.admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability),
                                                           sql.Identifier(name)))
        cls.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(name)))
        return name

    def setUp(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("garage", table) for table in TABLES)))
            connection.execute("DELETE FROM garage.meta_settings")
            connection.execute("INSERT INTO garage.meta_settings (settings_id) VALUES (1)")
        self.site = MetaStore(RoleDatabase(self.url, self.roles["site"]), self.cipher)
        self.store = MetaStore(RoleDatabase(self.url, self.roles["worker"]), self.cipher)
        self.site.save_connection(PAGE, "Garage", APP, {"page": {
            "token": PAGE_TOKEN, "subject_id": PAGE, "scopes": ["pages_messaging"]}}, "cli")
        self.site.set_bot(True, "cli", auto_reply_confirmed=True)
        self.sql("UPDATE garage.meta_settings SET bot_enabled_at = now() - interval '1 minute'")
        self.ticks = [1000.0]
        self.graph, self.engine = FakeGraph(), FakeEngine()
        self.ctx = MetaContext(SETTINGS, self.store, self.graph, self.cipher,
                               clock=lambda: self.ticks[0], now=lambda: datetime.now(UTC))
        self.worker = MessengerWorker(self.ctx, self.engine)

    def sql(self, statement: str, parameters: tuple | None = None):
        with psycopg.connect(self.url) as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def deliver(self, payload: dict, field: str = "messages") -> None:
        """Append one webhook item exactly as the receiver's role can, and notify."""
        key = "msg:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.roles["hook"])))
            connection.execute(
                "INSERT INTO garage.meta_inbox (event_key, field, page_id, payload) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (key, field, PAGE, Jsonb(payload)))
            connection.execute("NOTIFY garage_meta_inbox")

    def say(self, text: str, *, psid: str = PSID, mid: str | None = None) -> str:
        mid = mid or f"m_c{next(MIDS)}"
        self.deliver({"sender": {"id": psid}, "recipient": {"id": PAGE},
                      "timestamp": ms(datetime.now(UTC)), "message": {"mid": mid, "text": text}})
        return mid

    def echo(self, text: str, mid: str, app_id: str) -> None:
        self.deliver({"sender": {"id": PAGE}, "recipient": {"id": PSID},
                      "timestamp": ms(datetime.now(UTC) + timedelta(seconds=1)),
                      "message": {"mid": mid, "is_echo": True, "app_id": int(app_id),
                                  "text": text}})

    def burst(self, worker: MessengerWorker | None = None) -> dict:
        self.ticks[0] += 5
        return (worker or self.worker).run_due_bursts()

    def test_a_message_is_answered_end_to_end_with_the_worker_login(self):
        mid = self.say("Camry 2015 ხუნდები")
        self.assertEqual(self.worker.poll_inbox(), 1)
        self.assertEqual(self.burst(), {PSID: "sent"})
        self.assertEqual([tuple(row) for row in self.sql(
            "SELECT mid, sender, body, app_id FROM garage.messenger_messages ORDER BY message_id")],
            [(mid, "customer", "Camry 2015 ხუნდები", ""), ("m_bot_1", "bot", ANSWER, APP)])
        self.assertEqual(tuple(self.sql(
            "SELECT channel, status, engine_status, sent_mids, usage, trigger_key "
            "FROM garage.messenger_replies")[0]),
            ("messenger", "sent", "OK", ["m_bot_1"], USAGE, mid))
        state = self.sql("SELECT engine_state FROM garage.messenger_threads")[0][0]
        self.assertEqual(state[CHANNEL_KEY], {"options": [], "numbered": False})
        self.assertEqual(self.sql("SELECT outcome FROM garage.meta_inbox")[0][0], "stored")
        self.assertEqual(self.site.counts_today()["turns"], 1)

    def test_switches_and_commands_with_the_worker_login(self):
        def off(call):
            self.site.set_bot(False, "cli")
            return ok()(call)

        self.engine.results = [off]
        self.say("ხუნდები")
        self.worker.poll_inbox()
        self.assertEqual(self.burst(), {PSID: "skipped_off"})
        self.assertEqual(self.graph.sends(), [])
        self.say("გამარჯობა", psid=ADMIN_PSID)
        self.worker.poll_inbox()
        self.site.set_admin(ADMIN_PSID, True, "cli")
        self.sql("UPDATE garage.meta_settings SET auto_reply_confirmed_off = false")
        for text in ("#bot on", "#bot status", "#bot off"):
            self.say(text, psid=ADMIN_PSID)
            self.assertEqual(self.worker.poll_inbox(), 1)
        self.sql("UPDATE garage.meta_settings SET auto_reply_confirmed_off = true")
        self.say("#bot on", psid=ADMIN_PSID)
        self.worker.poll_inbox()
        self.assertTrue(self.site.settings()["bot_enabled"])
        self.assertEqual([tuple(row) for row in self.sql(
            "SELECT actor_username, action, target FROM garage.audit_log "
            "WHERE actor_username LIKE 'messenger:%' ORDER BY audit_id")], [
            ("messenger:9999", "messenger.bot_on_refused", "…9999"),
            ("messenger:9999", "messenger.bot_status", "…9999"),
            ("messenger:9999", "messenger.bot_off", "settings"),
            ("messenger:9999", "messenger.bot_on", "settings")])
        self.assertEqual([row[0] for row in self.sql(
            "SELECT status FROM garage.messenger_replies WHERE channel = 'command' "
            "ORDER BY reply_id")], ["sent"] * 4)
        texts = self.graph.texts(ADMIN_PSID)
        self.assertEqual(texts[0], bot_on_refused_text("ka"))
        self.assertTrue(texts[1].startswith(TEXTS["ka"]["status_off"]))
        self.assertEqual(texts[3], bot_switched_text("ka", True))

    def test_the_daily_cap_and_its_one_notice(self):
        self.site.update_settings({"daily_turn_limit": 1}, "cli")
        for text in ("ხუნდები", "ზეთი", "ფილტრი"):
            self.say(text)
            self.worker.poll_inbox()
            self.burst()
        rows = self.sql("SELECT channel, status, trigger_key FROM garage.messenger_replies "
                        "ORDER BY reply_id")
        self.assertEqual([(row[0], row[1]) for row in rows], [
            ("messenger", "sent"), ("messenger", "skipped_cap"), ("notice", "sent"),
            ("messenger", "skipped_cap")])
        self.assertEqual(rows[2][2], f"cap:{PSID}:{datetime.now(TBILISI).date().isoformat()}")
        self.assertEqual(len(self.graph.sends()), 2)
        self.assertEqual(self.site.counts_today()["turns"], 1)
        self.assertEqual(self.site.get_thread(PSID)["attention_reason"], "daily_cap")

    def test_the_shops_reply_is_stored_and_kept_in_the_engine_state(self):
        self.say("ხუნდები გაქვთ?")
        self.worker.poll_inbox()
        self.echo("დიახ, გვაქვს.", "m_shop", OTHER_APP)
        self.echo("bot words", "m_ours", APP)
        self.assertEqual(self.worker.poll_inbox(), 2)
        self.assertEqual([tuple(row) for row in self.sql(
            "SELECT mid, sender FROM garage.messenger_messages ORDER BY message_id")][1:],
            [("m_shop", "shop"), ("m_ours", "bot")])
        state = self.sql("SELECT engine_state FROM garage.messenger_threads")[0][0]
        self.assertEqual(state["memory"]["transcript"],
                         [{"user": "ხუნდები გაქვთ?", "assistant": "დიახ, გვაქვს."}])
        self.assertEqual(self.burst(), {PSID: "nothing"})

    def test_a_row_twice_and_a_restart_answer_once(self):
        mid = self.say("ხუნდები")
        self.say("ხუნდები", mid=mid)
        self.assertEqual(self.worker.poll_inbox(), 2)
        self.assertEqual([row[0] for row in self.sql(
            "SELECT outcome FROM garage.meta_inbox ORDER BY inbox_id")], ["stored", "duplicate"])
        restarted = MessengerWorker(self.ctx, self.engine)  # the first one stopped in the window
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(self.burst(restarted), {PSID: "sent"})
        self.assertEqual(self.worker.run_burst(PSID), "nothing")
        self.assertEqual(len(self.graph.sends()), 1)

    def test_jobs_record_their_runs_with_the_worker_login(self):
        # Comments past the retention period go with the daily purge too (meta_comments).
        self.sql("INSERT INTO garage.meta_comments (comment_id, post_id, created_time, source) "
                 "VALUES ('1_2_3', '1_2', now() - interval '400 days', 'backfill'), "
                 "('1_2_4', '1_2', now() - interval '1 day', 'backfill')")
        worker = MessengerWorker(self.ctx, self.engine, jobs={
            name: (lambda ctx: 1) for name in JOB_NAMES if name != "purge"})
        self.assertEqual(worker.run_jobs(startup=True), {name: "ok" for name in JOB_NAMES})
        self.site.request_job("names", "cli")
        self.assertEqual(worker.run_jobs(), {"names": "ok"})
        self.assertEqual(len(self.sql("SELECT run_id FROM garage.meta_sync_runs "
                                      "WHERE status = 'ok'")), len(JOB_NAMES) + 1)
        purge = self.sql("SELECT records, detail FROM garage.meta_sync_runs WHERE job = 'purge'")
        self.assertEqual(tuple(purge[0]), (1, "comments=1 forgotten=0 inbox=0 job_requests=0 "
                                              "replies=0 runs=0 threads=0"))
        self.assertEqual([row[0] for row in self.sql(
            "SELECT comment_id FROM garage.meta_comments")], ["1_2_4"])

    def test_a_notification_wakes_the_inbox_at_once(self):
        worker = MessengerWorker(self.ctx, self.engine, poll_seconds=30)
        thread = threading.Thread(target=worker._inbox_loop, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while worker._listening is not True and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(worker._listening)
            started = time.monotonic()
            self.say("ხუნდები")
            outcome = ""
            while outcome != "stored" and time.monotonic() < started + 5:
                time.sleep(0.05)
                outcome = self.sql("SELECT outcome FROM garage.meta_inbox")[0][0]
            self.assertEqual(outcome, "stored")  # long before the 30 s poll
        finally:
            worker.request_stop()
            self.sql("NOTIFY garage_meta_inbox")
            thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
