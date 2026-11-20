"""The Messenger bot worker: ``python -m automotive.messenger_worker`` (local only, no port).

The public receiver only appends signature-checked webhook items to ``garage.meta_inbox`` and
sends ``NOTIFY garage_meta_inbox``. Everything else happens here, with the worker's own login
(``MESSENGER_DATABASE_URL``, role ``garage_messenger``):

- **Inbox.** One thread LISTENs on that channel, polls every 2 s in case a notification is lost,
  and handles the leased rows in order: customer messages, echoes of what the page sent, button
  taps, referrals, comments. Each row ends with ``finish_inbox`` and a short outcome code, or goes
  back through ``fail_inbox`` after a backoff. Nothing slow runs on that thread: turns, comment
  replies and command replies run on their own threads, so one slow call never holds the batch's
  lease.
- **Bursts.** A customer's messages are answered together: a burst is due 5 s after the newest
  message and at most 45 s after the first (the owner's TNLU choice), counted on this process's
  clock from when each message was stored. A message that arrives during the chat's turn
  supersedes the turn; the next one reads both.
- **Turns.** Up to ``MESSENGER_CONCURRENT_TURNS`` chats are answered at once, one turn per chat.
- **Jobs.** Names, comments, page stats, posts, ads, the subscription self-heal, token health and
  the retention purge run on their intervals and whenever the dashboard asks.

The admin's switches decide everything a customer receives. They are read fresh when a burst is
claimed and again immediately before every Send API call (typing indicators included), so a switch
turned off mid-turn stops the reply before it leaves. Only customer messages written after the bot
was switched on, and younger than the maximum age, are answered: a backlog is never bulk-answered.
A message the shop already answered is never answered by the bot.

Nothing here reads what a customer means: the assistant engine does. This module moves data and
enforces the switches, the limits, privacy and Meta's channel rules. The only parts of a message
it looks at are the channel's own mechanics: a quick-reply payload, a number typed under a
numbered list, and the exact ``#bot`` commands from a chat the admin marked as their own.

Logs carry event names, codes, counts and the last four digits of a customer's id; never a
token, a message, a VIN or a phone number.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import logging
import re
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import sql

from automotive.live_store import LiveStore
from automotive.messenger_events import (
    CommentEvent,
    CustomerMessage,
    Ignored,
    Oversized,
    PageEcho,
    Postback,
    Referral,
    ReferralEvent,
    parse,
)
from automotive.messenger_render import (
    MORE_PAYLOAD,
    RenderedReply,
    bot_failed_text,
    bot_help_text,
    bot_on_refused_text,
    bot_status_text,
    bot_switched_text,
    cap_notice,
    conflicting_vins_text,
    fallback_text,
    late_apology,
    media_only_text,
    parse_choice,
    render,
    split_text,
    text_language,
)
from automotive.meta_config import MetaConfigError, MetaSettings
from automotive.meta_connect import ConnectError, check, ensure_subscribed, sync_names
from automotive.meta_context import MetaContext, NotConnected
from automotive.meta_crypto import TokenCipherError
from automotive.meta_graph import GraphError
from automotive.meta_store import AutoReplyNotConfirmed, mask_psid

LOGGER = logging.getLogger(__name__)

TBILISI = ZoneInfo("Asia/Tbilisi")
# meta_receiver.INBOX_CHANNEL. Not imported, so the worker does not load the web stack; a test
# keeps the two equal.
INBOX_CHANNEL = "garage_meta_inbox"
POLL_SECONDS = 2.0
INBOX_BATCH = 20
LEASE_SECONDS = 300
MAX_ATTEMPTS = 5
RETRY_CAP_SECONDS = 60.0
TYPING_SECONDS = 15.0
LATE_SECONDS = 3600
# The catalogue loads in the background at start-up; until it has, every turn would end in
# DATA_UNAVAILABLE and each customer would get the fallback. Bursts wait for it, up to a limit.
READY_RETRY_SECONDS = 5.0
READY_WAIT_SECONDS = 300.0
JOB_POLL_SECONDS = 10.0
WAKE_GAP_SECONDS = 60.0  # more wall-clock time than monotonic time: the laptop slept
WAKE_SETTLE_SECONDS = 20.0  # Wi-Fi comes back a few seconds after a wake-up
ERROR_RETRY_SECONDS = 1800
NOT_CONNECTED_RETRY_SECONDS = 60
# A turn is at most 60 s plus its sends; systemd waits 90 s before it kills a service.
DRAIN_SECONDS = 70.0
STALE_REPLY_SECONDS = 900
RECOVERY_LIMIT = 200
CACHE_SECONDS = 10.0
TEXT_LIMIT = 4000  # what the engine reads of one turn's text (messenger_engine.MESSAGE_LIMIT)
# The worker's own part of a chat's engine_state: the options and the parts view of its last
# reply. ``messenger_engine.clean_state`` drops unknown keys, so the engine never sees it.
CHANNEL_KEY = "_messenger"
CHANNEL_BYTES = 40_000
VIEW_CARDS = 60
SENT_MEMORY = 2000
# The AI account itself refuses every request (no credit, a revoked key, no access), or the
# engine has no key at all: pause instead of sending every customer the fallback.
ACCOUNT_CODES = frozenset({"PROVIDER_BILLING", "AUTHENTICATION_ERROR", "ACCESS_DENIED",
                           "NOT_CONFIGURED"})
COMMANDS = {"#bot off": "off", "#bot on": "on", "#bot status": "status"}
_ANY_COMMAND = re.compile(r"#bot(?:\s.*)?", re.DOTALL)
_OUTCOME = re.compile(r"[^a-z0-9_.:-]")
# Internal stop reason: a Graph error the typing thread met; mapped to a status where recorded.
GRAPH_STOP = "graph_error"

# The status lines bot_status_text does not have: when the switch last changed, and today's
# estimated cost.
STATUS_TEXTS = {
    "ka": {"since": "ბოლო ცვლილება: {time}", "cost": "დღევანდელი ხარჯი: ≈${cost}"},
    "en": {"since": "Last change: {time}", "cost": "Cost today: ≈${cost}"},
}


# -- jobs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """A background job: its ``meta_sync_runs.job`` name (also what the dashboard's
    ``request_job`` passes), how often it runs, whether it needs a connected page, and for a
    feature module the (module, function) imported when it runs."""

    name: str
    every: float
    needs_page: bool = True
    feature: tuple[str, str] | None = None
    at_startup: bool = False


JOBS = (
    Job("subscription", 3600, at_startup=True),
    # Messages the webhook missed while the laptop slept (meta_catchup), at start, every ten
    # minutes and right after a wake-up (``_jobs_loop``).
    Job("catchup", 600, at_startup=True),
    Job("names", 600),
    Job("comments", 600, feature=("automotive.meta_comments", "sync_comments")),
    Job("posts", 3600, feature=("automotive.meta_page_stats", "sync_posts")),
    Job("page_stats", 86400, feature=("automotive.meta_page_stats", "sync_page")),
    Job("ads", 10800, feature=("automotive.meta_ads", "sync_ads")),
    Job("token_health", 21600),
    Job("purge", 86400, needs_page=False),
)
JOB_NAMES = tuple(job.name for job in JOBS)
JOBS_BY_NAME = {job.name: job for job in JOBS}


def _purge(ctx: MetaContext) -> dict:
    """The daily retention purge at the dashboard's ``retention_days``: the store's own tables
    (``MetaStore.purge``) and the stored comments (``meta_comments.purge_comments``), which the
    store does not cover. Only the worker's login may delete comments."""
    days = int(ctx.store.settings()["retention_days"])
    counts = dict(ctx.store.purge(days))
    database = getattr(ctx.store, "db", None)
    if database is not None:  # a store without a database (a test's fake) keeps no comments
        from automotive.meta_comments import purge_comments
        counts["comments"] = purge_comments(database, days)
    return counts


def _catchup(ctx: MetaContext) -> dict:
    from automotive.meta_catchup import sync_missed_messages

    return sync_missed_messages(ctx)


BUILT_IN_JOBS: dict[str, Callable[[MetaContext], Any]] = {
    "subscription": ensure_subscribed, "catchup": _catchup, "names": sync_names,
    "token_health": check, "purge": _purge}


# -- small pieces ------------------------------------------------------------------------------


class _Later(Exception):
    """The burst cannot run yet (the engine is still loading, the page is not connected); try
    again in ``seconds``. ``status`` is what the burst reports meanwhile."""

    def __init__(self, seconds: float, status: str = "waiting") -> None:
        super().__init__(seconds)
        self.seconds = seconds
        self.status = status


@dataclass
class _Chat:
    """What the scheduler knows of one chat. ``pending`` maps the mids stored since the last
    burst read the chat to when they were stored (this process's clock)."""

    pending: dict[str, float] = field(default_factory=dict)
    first: float | None = None
    last: float | None = None
    recheck_at: float | None = None
    not_before: float | None = None
    waiting_since: float | None = None
    running: bool = False
    snapshot: frozenset[str] = frozenset()
    newest_sent_at: datetime | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    stop_reason: str = ""
    stop_error: GraphError | None = None

    def due(self, quiet: float, max_wait: float) -> float | None:
        times = []
        if self.pending and self.first is not None and self.last is not None:
            times.append(min(self.last + quiet, self.first + max_wait))
        if self.recheck_at is not None:
            times.append(self.recheck_at)
        if not times:
            return None
        when = min(times)
        return max(when, self.not_before) if self.not_before is not None else when


@dataclass(frozen=True)
class _Burst:
    """One burst being answered. ``rows`` are its customer messages, oldest first. ``checked``
    is False only for a command reply, which the switches never stop."""

    psid: str
    page_id: str
    rows: tuple[dict, ...]
    newest_mid: str
    newest_sent_at: datetime
    kind: str  # turn, media, more, command
    checked: bool = True


@dataclass
class _Reply:
    """What ``finish_reply`` records for one attempt, and the chat flag it calls for."""

    status: str = "failed"
    engine_status: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    error_code: int | None = None
    error_subcode: int | None = None
    error_text: str = ""
    sent_mids: list[str] = field(default_factory=list)
    attention: str = ""

    def fields(self) -> dict:
        values: dict[str, Any] = {"engine_status": self.engine_status,
                                  "sent_mids": list(self.sent_mids)}
        if self.usage:
            values["usage"] = self.usage
        if self.cost_usd is not None:
            values["cost_usd"] = self.cost_usd
        if self.error_code is not None:
            values["error_code"] = self.error_code
        if self.error_subcode is not None:
            values["error_subcode"] = self.error_subcode
        if self.error_text:
            values["error_text"] = self.error_text
        return values

    def graph(self, error: GraphError) -> None:
        self.error_code, self.error_subcode = error.code, error.subcode
        self.error_text = error.message


def command_of(text: Any) -> str | None:
    """The ``#bot`` command an admin's message is: 'off', 'on' and 'status' match exactly after
    trimming and lower-casing; any other text starting with '#bot' asks for 'help'. None for
    everything else. The caller applies it only to chats the admin marked as their own."""
    if not isinstance(text, str):
        return None
    said = text.strip().lower()
    if said in COMMANDS:
        return COMMANDS[said]
    return "help" if _ANY_COMMAND.fullmatch(said) else None


def usage_cost(usage: Mapping[str, Any]) -> float | None:
    """The cost the engine's usage reports itself (``cost_usd``), else None."""
    value = usage.get("cost_usd") if isinstance(usage, Mapping) else None
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def estimated_cost(model: str) -> Callable[[Mapping[str, Any]], float | None]:
    """A cost function for the dashboard's "cost today": the usage's own figure when it has
    one, else an estimate from its token counts at the price table the assistant bench uses
    (``assistant_eval.cost``). An estimate: what the provider bills can differ."""

    def cost(usage: Mapping[str, Any]) -> float | None:
        own = usage_cost(usage)
        if own is not None:
            return own
        if not isinstance(usage, Mapping) or not any(
                usage.get(key) for key in ("input_tokens", "output_tokens",
                                           "cache_read_input_tokens",
                                           "cache_creation_input_tokens")):
            return None
        try:
            from automotive.assistant_eval import cost as priced
            return round(float(priced(model, dict(usage))), 5)
        except Exception:  # A missing estimate is no reason to fail a reply's record.
            return None

    return cost


def _error_code(error: BaseException) -> str:
    """A loggable code for an exception: Graph's codes, a database error's SQLSTATE, or the class
    name. Never the message: a database error's text can quote the row, a customer's words."""
    if isinstance(error, GraphError):
        return f"graph_code_{error.code}" if error.code is not None else f"graph_{error.type}"
    state = getattr(error, "sqlstate", None)
    if isinstance(state, str) and state:
        return f"database_{state}"
    return type(error).__name__


def _language(state: Mapping[str, Any] | None) -> str:
    value = (state or {}).get("language")
    return value if value in ("ka", "en") else "ka"


def _split_state(value: Any) -> tuple[dict, dict]:
    """A stored engine_state as (the engine's state, the worker's channel part)."""
    if not isinstance(value, Mapping):
        return {}, {}
    state = dict(value)
    channel = state.pop(CHANNEL_KEY, None)
    return state, dict(channel) if isinstance(channel, Mapping) else {}


def _body(row: Mapping[str, Any]) -> str:
    return str(row.get("body") or "").strip()


def _kind(rows: Sequence[Mapping[str, Any]], channel: Mapping[str, Any]) -> str:
    """'media' when no message of the burst has words (photos, voice notes, stickers), 'more'
    when it is only taps on our own "more" button and the parts view is still kept, else
    'turn'."""
    if all(not _body(row) and not row.get("quick_reply") for row in rows):
        return "media"
    view, offset = channel.get("view"), channel.get("offset")
    if (isinstance(view, Mapping) and isinstance(offset, int) and not isinstance(offset, bool)
            and offset > 0 and all(row.get("quick_reply") == MORE_PAYLOAD for row in rows)):
        return "more"
    return "turn"


def _joined(rows: Sequence[Mapping[str, Any]]) -> str:
    """The burst's words for the engine, one message per line, oldest first. The engine reads
    4,000 characters, so a long backlog loses its oldest messages, never the newest."""
    texts = [text for text in (_body(row) for row in rows) if text]
    kept: list[str] = []
    size = 0
    for text in reversed(texts):
        extra = len(text) + (1 if kept else 0)
        if size + extra > TEXT_LIMIT:
            if not kept:
                kept.append(text[:TEXT_LIMIT])
            break
        kept.append(text)
        size += extra
    return "\n".join(reversed(kept))


def _text_messages(text: str, opening: str = "") -> list[dict]:
    body = f"{opening}\n\n{text}" if opening else text
    return [{"text": piece} for piece in split_text(body)]


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _reply_channel(rendered: RenderedReply, public: Mapping[str, Any]) -> dict:
    """What the next message needs from this reply: the options (for a typed number or a
    tapped answer) and, when parts are left, the view the "more" button pages through."""
    channel: dict[str, Any] = {"options": list(rendered.options),
                               "numbered": bool(rendered.numbered)}
    if rendered.next_offset is not None:
        channel["view"] = {
            "cards": [card for card in public.get("cards") or [] if isinstance(card, dict)]
            [:VIEW_CARDS],
            "groups": list(public.get("groups") or []), "total": public.get("total") or 0,
            "question": public.get("question"),
            "suggestions": list(public.get("suggestions") or []),
            "language": public.get("language")}
        channel["offset"] = rendered.next_offset
    if _size(channel) > CHANNEL_BYTES:
        channel.pop("view", None)
        channel.pop("offset", None)
    if _size(channel) > CHANNEL_BYTES:
        channel = {"options": [], "numbered": False}
    return channel


def _comment_outcome(value: Any) -> str:
    """An inbox outcome from what the comments module returned: a short code, never text."""
    if isinstance(value, Mapping):
        value = value.get("status") or value.get("outcome")
    code = _OUTCOME.sub("", str(value or "").lower())[:60]
    return f"comment: {code}" if code else "comment"


def _number(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _summary(name: str, result: Any) -> tuple[int, str]:
    """(records, detail) for a job run: counts and codes only."""
    if isinstance(result, bool):
        return int(result), ""
    if isinstance(result, int):
        # A feature job returns meta_sync.SyncCount: an int that also says, in codes, why a
        # run wrote nothing (no ads token, insights refused...), for the dashboard's job list.
        return max(0, int(result)), str(getattr(result, "detail", "") or "")[:500]
    if isinstance(result, Mapping):
        if name == "names":
            return _number(result.get("updated")), (
                f"conversations={_number(result.get('conversations'))} "
                f"updated={_number(result.get('updated'))}")
        if name == "subscription":
            page, app = bool(result.get("page_resubscribed")), bool(
                result.get("app_resubscribed"))
            return int(page) + int(app), f"page_resubscribed={page} app_resubscribed={app}"
        if name == "catchup":
            return _number(result.get("stored")), " ".join(
                f"{key}={_number(result.get(key))}"
                for key in ("conversations", "messages", "stored", "claimed"))
        counts = {str(key): _number(value) for key, value in result.items()
                  if isinstance(value, int) and not isinstance(value, bool)}
        return max(0, sum(counts.values())), " ".join(
            f"{key}={value}" for key, value in sorted(counts.items()))[:500]
    if isinstance(result, list | tuple):
        if name == "token_health":
            broken = sum(1 for row in result if isinstance(row, Mapping)
                         and row.get("status") != "ok")
            warned = sum(1 for row in result if isinstance(row, Mapping) and row.get("warn"))
            return len(result), f"tokens={len(result)} broken={broken} warn={warned}"
        return len(result), ""
    return 0, ""


def _run_status(result: Any) -> str:
    """'skipped' when a feature job's SyncCount says it had nothing it could do (no token,
    nothing to read), else 'ok'. Any other result of a job that returned is 'ok'."""
    if (isinstance(result, int) and not isinstance(result, bool)
            and getattr(result, "status", "ok") == "skipped"):
        return "skipped"
    return "ok"


def _connect_detail(error: ConnectError) -> str:
    meta = error.detail.get("meta") if isinstance(error.detail, Mapping) else None
    if isinstance(meta, Mapping):
        # The type says "network" when Meta was never reached (Wi-Fi still down after a
        # wake-up), which is not the same problem as Meta refusing the call.
        return (f"{error.code} code={meta.get('code')} subcode={meta.get('subcode')} "
                f"http={meta.get('http_status')} type={meta.get('type')}")
    return str(error.code)


def _local_time(value: datetime) -> str:
    return value.astimezone(TBILISI).strftime("%d.%m.%Y %H:%M")


def _model_name(engine: Any) -> str:
    settings = getattr(getattr(getattr(engine, "store", None), "service", None), "settings",
                       None)
    return str(getattr(settings, "assistant_model", "") or getattr(settings, "anthropic_model",
                                                                    "") or "")


# -- the worker --------------------------------------------------------------------------------


class MessengerWorker:
    """The bot: inbox, bursts, turns, commands and jobs for one connected page.

    Every step is a plain method (``poll_inbox``, ``handle_row``, ``run_due_bursts``,
    ``run_burst``, ``run_jobs``, ``run_job``, ``recover``) that does its work in the caller's
    thread; ``start`` adds the threads that call them, and ``stop`` drains them. ``ctx.clock``
    times the bursts and ``ctx.now`` is compared with Meta's message times, so tests drive both.

    ``engine`` is a ``messenger_engine.MessengerEngine`` or anything with its ``respond``,
    ``ready``, ``prepare`` and ``shop_phone``. ``cost`` turns a turn's usage into US dollars
    for the caps' dashboard (None: not known). ``jobs`` replaces job functions by name.
    ``ignore_app_ids`` names apps whose echoes never count as the shop's answer (empty: every
    other app's echo does; Meta's own "Auto reply" must therefore be off, which switching the
    bot on already requires).
    """

    def __init__(self, ctx: MetaContext, engine: Any, *, quiet_seconds: float | None = None,
                 max_wait_seconds: float | None = None, concurrent_turns: int | None = None,
                 typing_seconds: float = TYPING_SECONDS, poll_seconds: float = POLL_SECONDS,
                 ignore_app_ids: Sequence[str] = (),
                 cost: Callable[[Mapping[str, Any]], float | None] | None = None,
                 jobs: Mapping[str, Callable[[MetaContext], Any]] | None = None,
                 record_shop_reply: Callable[..., dict] | None = None,
                 live: Any = None) -> None:
        settings = ctx.settings
        self.ctx = ctx
        self.store = ctx.store
        self.graph = ctx.graph
        self.engine = engine
        # The Live chats alarms (migration 0015), on the worker's own login. A store without a
        # database (a test's double) records none.
        database = getattr(self.store, "db", None)
        self.live = live if live is not None else (
            LiveStore(database, self.store) if database is not None else None)
        self.quiet_seconds = float(settings.quiet_seconds if quiet_seconds is None
                                   else quiet_seconds)
        self.max_wait_seconds = float(settings.max_wait_seconds if max_wait_seconds is None
                                      else max_wait_seconds)
        self.concurrent_turns = max(1, int(settings.concurrent_turns if concurrent_turns is None
                                           else concurrent_turns))
        self.typing_seconds = float(typing_seconds)
        self.poll_seconds = float(poll_seconds)
        self.ignore_app_ids = tuple(str(app) for app in ignore_app_ids if str(app))
        self.cost = cost or usage_cost
        self.job_targets = dict(jobs or {})
        self._record_shop_reply = record_shop_reply
        # Guards the chat records; notified whenever one changes, so the scheduler wakes.
        self._wake = threading.Condition()
        self._chats: dict[str, _Chat] = {}
        # A chat's engine_state is read, changed and written under its lock. While a turn holds
        # the state, a shop echo queues its words in _active instead of writing.
        self._state_locks = tuple(threading.Lock() for _ in range(32))
        self._active: dict[str, list[tuple[str, str]]] = {}
        self._cap_lock = threading.Lock()
        self._retry_lock = threading.Lock()
        self._retries: list[tuple[float, int, str]] = []
        self._sent_lock = threading.Lock()
        self._sent: deque[str] = deque(maxlen=SENT_MEMORY)
        self._page_cache: tuple[float, str | None] | None = None
        self._apps_cache: tuple[float, frozenset[str]] | None = None
        self._job_hold: dict[str, datetime] = {}
        self._quiet_logged: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._turns: ThreadPoolExecutor | None = None
        self._commands: ThreadPoolExecutor | None = None
        self._comments: ThreadPoolExecutor | None = None
        self._futures: set[Future] = set()
        self._futures_lock = threading.Lock()
        self._listening: bool | None = None

    # -- inbox -----------------------------------------------------------------------------

    def poll_inbox(self, limit: int = INBOX_BATCH) -> int:
        """Lease up to ``limit`` webhook rows and handle them in order; how many were leased."""
        self._flush_retries()
        rows = self.store.claim_inbox(limit, lease_seconds=LEASE_SECONDS,
                                      max_attempts=MAX_ATTEMPTS)
        for row in rows:
            self.handle_row(row)
        return len(rows)

    def handle_row(self, row: Mapping[str, Any]) -> str | None:
        """Handle one leased inbox row and finish it with its outcome code. A row that fails is
        put back after a backoff ('error'); a comment handed to its own thread is finished
        there (None)."""
        try:
            outcome = self._handle(row)
        except Exception as error:
            self._failed(row, error)
            return "error"
        if outcome is not None:
            self._finish_row(int(row["inbox_id"]), outcome)
        return outcome

    def _handle(self, row: Mapping[str, Any]) -> str | None:
        event = parse(row.get("field"), row.get("payload"), page_id=str(row.get("page_id") or ""),
                      received_at=row.get("received_at"))
        if isinstance(event, Ignored):
            return f"ignored: {event.reason}"
        page_id = self._connected_page(event.page_id)
        if page_id is None:
            return "not_connected"
        if page_id != event.page_id:
            return "other_page"
        if isinstance(event, CustomerMessage):
            return self._customer(event.psid, event.page_id, event.mid, event.text,
                                  event.attachments, event.quick_reply or "", event.referral,
                                  event.sent_at, event.standby)
        if isinstance(event, Postback):
            # A tap on a button: its title is what the customer "said". Meta may leave out the
            # mid; the row's event key is unique per postback and fits the column.
            return self._customer(event.psid, event.page_id, event.mid or str(row["event_key"]),
                                  event.title, (), event.payload, event.referral, event.sent_at,
                                  event.standby)
        if isinstance(event, PageEcho):
            return self._echo(event)
        if isinstance(event, ReferralEvent):
            if self.store.deleted_after(event.psid, event.sent_at):
                return "ignored: deleted_chat"
            self.store.upsert_thread(event.psid, event.page_id, source=event.referral.channel,
                                     source_ad_id=event.referral.ad_id)
            return "referral"
        if isinstance(event, CommentEvent):
            return self._comment(row, event)
        if isinstance(event, Oversized):
            return self._oversized(event)
        return "ignored: unknown_event"

    def _customer(self, psid: str, page_id: str, mid: str, text: str, attachments: Sequence[Any],
                  quick_reply: str, referral: Referral | None, sent_at: datetime,
                  standby: bool) -> str:
        if self.store.deleted_after(psid, sent_at):
            # A late copy (the worker was stopped, or Meta retried) of a message sent before
            # staff deleted this customer's data: nothing is stored or answered, and the chat
            # is not created again (MetaStore.add_message refuses it as well).
            return "ignored: deleted_chat"
        thread = self.store.upsert_thread(
            psid, page_id, source=referral.channel if referral else None,
            source_ad_id=referral.ad_id if referral else None)
        stored = self.store.add_message(
            psid, mid, "customer", sent_at, page_id=page_id, body=text,
            attachments=list(attachments), quick_reply=quick_reply,
            referral_ad_id=referral.ad_id if referral else "")
        command = command_of(text) if thread.get("is_admin") and not standby else None
        if command is not None:
            # Before the duplicate check: a row that failed after its message was stored comes
            # back as a duplicate, and its command must still be applied. A command already
            # handled is "command_duplicate" (its reply attempt is keyed by the mid).
            return self._run_command(psid, page_id, mid, command)
        if not stored:
            # A row handed out again (a lease ran out, or Meta sent the mid twice). Everything it
            # caused is keyed by the mid already; only look at the chat again, which never
            # supersedes a turn and never answers the same burst twice.
            if not standby:
                self._recheck(psid)
            return "duplicate"
        if standby:
            # Another app holds this chat: keep the words, never answer them. Closing the mid as
            # a trigger stops a later look at the chat from answering it on its own.
            self._close_trigger(psid, mid, "skipped_chat_off")
            return "standby"
        opened = self._arrived(psid, mid)
        # A tap on "more" is paged from the kept view, so it needs no warm engine.
        if opened and quick_reply != MORE_PAYLOAD and self._turn_likely(thread, sent_at, text):
            prepare = getattr(self.engine, "prepare", None)
            if callable(prepare):
                with contextlib.suppress(Exception):
                    prepare()
        return "stored"

    def _echo(self, event: PageEcho) -> str:
        """A message the page sent: the bot's own (our app, or a mid this process sent) or the
        shop's (any other app, or none: typed in the page inbox or Business Suite)."""
        if self._ours(event):
            stored = self.store.add_message(
                event.psid, event.mid, "bot", event.sent_at, page_id=event.page_id,
                body=event.text, attachments=list(event.attachments), app_id=event.app_id)
            return "echo_bot" if stored else "duplicate"
        waiting = self.store.unanswered_customer_messages(event.psid,
                                                          ignore_app_ids=self.ignore_app_ids)
        stored = self.store.add_message(
            event.psid, event.mid, "shop", event.sent_at, page_id=event.page_id, body=event.text,
            attachments=list(event.attachments), app_id=event.app_id)
        if not stored:
            return "duplicate"
        if event.app_id and event.app_id in self.ignore_app_ids:
            return "echo_ignored_app"
        self._shop_wrote(event.psid, event.sent_at)
        self._remember_shop_reply(event, [row for row in waiting
                                          if row["sent_at"] <= event.sent_at])
        return "echo_shop"

    def _ours(self, event: PageEcho) -> bool:
        with self._sent_lock:
            if event.mid in self._sent:
                return True
        return bool(event.app_id) and event.app_id in self._own_apps()

    def _remember_shop_reply(self, event: PageEcho, before: list[dict]) -> None:
        """Put the shop's words in the chat's engine state as the answer to what the customer
        had written, so the bot answers the next message with them in view. A turn running for
        the chat holds the state; the words then wait for it to finish."""
        psid = event.psid
        with self._state_lock(psid):
            if psid in self._active:
                with self._wake:
                    chat = self._chats.get(psid)
                    covered = chat.snapshot if chat is not None else frozenset()
                # The running turn's own messages are in its state already, as unanswered.
                said = _joined([row for row in before if row["mid"] not in covered])
                self._active[psid].append((event.text, said))
                return
            thread = self.store.get_thread(psid, with_state=True)
            state, _ = _split_state(thread.get("engine_state") if thread else None)
            # The shop wrote last, so the options of the bot's last reply no longer apply.
            self._save_state(psid, self._shop_reply(state, event.text, _joined(before)), {})

    def _comment(self, row: Mapping[str, Any], event: CommentEvent) -> str | None:
        handler = self._comment_handler()
        if handler is None:
            return "comments_unavailable"
        if self._comments is None:
            return _comment_outcome(handler(self.ctx, event, self.engine, cost=self.cost))
        self._dispatch(self._comments, self._comment_task, dict(row), event, handler)
        return None

    def _comment_task(self, row: dict, event: CommentEvent, handler: Callable) -> None:
        # The comment turn's cost goes into "cost today" with the chats' (meta_comments).
        try:
            outcome = _comment_outcome(handler(self.ctx, event, self.engine, cost=self.cost))
        except Exception as error:
            self._failed(row, error)
            return
        self._finish_row(int(row["inbox_id"]), outcome)

    def _comment_handler(self) -> Callable | None:
        """``automotive.meta_comments.handle_comment_event``, imported when the first comment
        arrives; None (logged once) while that module is not there."""
        try:
            module = importlib.import_module("automotive.meta_comments")
        except ImportError as error:
            self._log_once("comments", "messenger_comments_unavailable error=%s",
                           type(error).__name__)
            return None
        handler = getattr(module, "handle_comment_event", None)
        return handler if callable(handler) else None

    def _oversized(self, event: Oversized) -> str:
        """An item too large for the inbox, kept as ids only: there are no words to answer, so
        the chat is flagged for the shop to read in the page inbox."""
        if event.psid and event.kind in ("msg", "pb", "echo"):
            if self.store.deleted_after(event.psid, event.sent_at):
                return "ignored: deleted_chat"  # a late copy from before "Delete data"
            self.store.upsert_thread(event.psid, event.page_id)
            self.store.flag_attention(event.psid, "oversized_message")
        return "oversized"

    def _finish_row(self, inbox_id: int, outcome: str) -> None:
        try:
            self.store.finish_inbox(inbox_id, outcome)
        except Exception as error:  # The lease runs out and the row comes back; mids dedupe.
            LOGGER.warning("messenger_inbox_finish_failed inbox_id=%s error=%s", inbox_id,
                           _error_code(error))

    def _failed(self, row: Mapping[str, Any], error: BaseException) -> None:
        """Put the row back after 2, 4, 8, 16 s... (its lease is held until then), or end it
        as failed after MAX_ATTEMPTS. The backoff lives here: the table has no retry time."""
        inbox_id, code = int(row["inbox_id"]), _error_code(error)
        attempts = max(1, _number(row.get("attempts")))
        LOGGER.warning("messenger_inbox_failed inbox_id=%s attempts=%d error=%s", inbox_id,
                       attempts, code)
        if attempts >= MAX_ATTEMPTS:
            self._fail_now(inbox_id, code)
            return
        delay = min(RETRY_CAP_SECONDS, 2.0 ** attempts)
        with self._retry_lock:
            self._retries.append((self.ctx.clock() + delay, inbox_id, code))

    def _flush_retries(self, *, force: bool = False) -> None:
        now = self.ctx.clock()
        with self._retry_lock:
            due = [entry for entry in self._retries if force or entry[0] <= now]
            self._retries = [entry for entry in self._retries if entry not in due]
        for _, inbox_id, code in due:
            self._fail_now(inbox_id, code)

    def _fail_now(self, inbox_id: int, code: str) -> None:
        try:
            self.store.fail_inbox(inbox_id, code, max_attempts=MAX_ATTEMPTS)
        except Exception as error:
            LOGGER.warning("messenger_inbox_fail_failed inbox_id=%s error=%s", inbox_id,
                           _error_code(error))

    # -- connection facts, cached briefly ------------------------------------------------------

    def _cached_page(self, *, fresh: bool = False) -> str | None:
        now = self.ctx.clock()
        cached = self._page_cache
        if fresh or cached is None or now - cached[0] > CACHE_SECONDS:
            page = self.store.page()
            cached = (now, str(page["page_id"]) if page else None)
            self._page_cache = cached
        return cached[1]

    def _connected_page(self, hint: str = "") -> str | None:
        """The connected page's id; read again when an event names another (a reconnect)."""
        page = self._cached_page()
        if page != hint:
            page = self._cached_page(fresh=True)
        return page

    def _own_apps(self) -> frozenset[str]:
        """Our app, and the app of a stored send-only token: echoes from these are the bot's."""
        now = self.ctx.clock()
        cached = self._apps_cache
        if cached is None or now - cached[0] > CACHE_SECONDS:
            apps = {self.ctx.settings.app_id}
            apps.update(str(row.get("app_id") or "") for row in self.store.token_rows()
                        if row.get("purpose") in ("page", "send_page"))
            cached = (now, frozenset(app for app in apps if app))
            self._apps_cache = cached
        return cached[1]

    def _send_token_row(self) -> Mapping[str, Any] | None:
        """The token row the Send API will use (as ``MetaContext.send_token`` picks it)."""
        rows = {row["purpose"]: row for row in self.store.token_rows()}
        return rows.get("send_page") or rows.get("page")

    def _token_app(self, purpose: str) -> str:
        for row in self.store.token_rows():
            if row.get("purpose") == purpose:
                return str(row.get("app_id") or "")
        return self.ctx.settings.app_id

    # -- the scheduler ------------------------------------------------------------------------

    def _arrived(self, psid: str, mid: str) -> bool:
        """A new customer message: open or extend the chat's quiet window, and supersede the
        turn running for it unless that turn already holds this message. True when the
        message opened a new window."""
        with self._wake:
            chat = self._chats.setdefault(psid, _Chat())
            if chat.running and mid in chat.snapshot:
                return False
            opened = not chat.running and not chat.pending
            now = self.ctx.clock()
            chat.pending[mid] = now
            chat.first = now if chat.first is None else chat.first
            chat.last = now
            if chat.running:
                self._stop_turn(chat, "skipped_superseded")
            self._wake.notify_all()
            return opened

    def _recheck(self, psid: str, delay: float = 0.0) -> None:
        """Look at the chat again after ``delay`` seconds, without a new message."""
        with self._wake:
            chat = self._chats.setdefault(psid, _Chat())
            at = self.ctx.clock() + max(0.0, delay)
            chat.recheck_at = at if chat.recheck_at is None else min(chat.recheck_at, at)
            self._wake.notify_all()

    def _stop_turn(self, chat: _Chat, reason: str, error: GraphError | None = None) -> None:
        if not chat.stop_reason:
            chat.stop_reason, chat.stop_error = reason, error
        chat.cancel.set()

    def _stop_running(self, psid: str, reason: str, error: GraphError | None = None) -> None:
        with self._wake:
            chat = self._chats.get(psid)
            if chat is not None and chat.running:
                self._stop_turn(chat, reason, error)

    def _shop_wrote(self, psid: str, sent_at: datetime) -> None:
        """The shop answered: a turn working on older messages stops before it sends."""
        with self._wake:
            chat = self._chats.get(psid)
            if (chat is not None and chat.running and chat.newest_sent_at is not None
                    and sent_at > chat.newest_sent_at):
                self._stop_turn(chat, "skipped_shop_answered")

    def _begin(self, chat: _Chat) -> None:
        chat.running = True
        chat.recheck_at = None
        chat.snapshot = frozenset()
        chat.newest_sent_at = None
        chat.cancel = threading.Event()
        chat.stop_reason, chat.stop_error = "", None

    def _take_due(self) -> list[str]:
        with self._wake:
            now = self.ctx.clock()
            taken = []
            for psid, chat in self._chats.items():
                when = None if chat.running else chat.due(self.quiet_seconds,
                                                          self.max_wait_seconds)
                if when is not None and when <= now:
                    self._begin(chat)
                    taken.append(psid)
            return taken

    def _idle_seconds(self) -> float:
        """How long the scheduler may sleep (called holding the lock)."""
        now = self.ctx.clock()
        times = [when for chat in self._chats.values() if not chat.running
                 for when in [chat.due(self.quiet_seconds, self.max_wait_seconds)]
                 if when is not None]
        if not times:
            return 1.0
        return max(0.05, min(1.0, min(times) - now))

    def _seen(self, psid: str) -> set[str]:
        with self._wake:
            chat = self._chats.get(psid)
            return set(chat.pending) if chat is not None else set()

    def _snapshot(self, psid: str, seen: set[str], mids: Sequence[str],
                  newest_sent_at: datetime | None) -> None:
        """Record what this burst read. Every message stored before the read is either in it or
        already answered, so only messages stored since still count as superseding it."""
        with self._wake:
            chat = self._chats.get(psid)
            if chat is None:
                return
            chat.snapshot = frozenset(mids)
            chat.newest_sent_at = newest_sent_at
            for mid in seen | set(mids):
                chat.pending.pop(mid, None)
            if chat.pending:
                chat.first, chat.last = min(chat.pending.values()), max(chat.pending.values())
            else:
                chat.first = chat.last = None
                if chat.stop_reason == "skipped_superseded":  # nothing has run yet
                    chat.stop_reason = ""
                    chat.cancel.clear()

    def _release(self, psid: str, retry_in: float | None = None) -> None:
        with self._wake:
            chat = self._chats.get(psid)
            if chat is None:
                return
            chat.running = False
            chat.snapshot = frozenset()
            chat.newest_sent_at = None
            chat.stop_reason, chat.stop_error = "", None
            if retry_in is not None:
                at = self.ctx.clock() + retry_in
                chat.not_before = at
                chat.recheck_at = at if chat.recheck_at is None else chat.recheck_at
            else:
                chat.not_before = chat.waiting_since = None
            if not chat.pending and chat.recheck_at is None:
                del self._chats[psid]
            self._wake.notify_all()

    def _cancel_event(self, psid: str) -> threading.Event:
        with self._wake:
            chat = self._chats.get(psid)
            return chat.cancel if chat is not None else threading.Event()

    def _stop_of(self, psid: str) -> tuple[str, GraphError | None]:
        with self._wake:
            chat = self._chats.get(psid)
            if chat is None or not chat.stop_reason:
                return "skipped_superseded", None
            return chat.stop_reason, chat.stop_error

    def _engine_ready(self, psid: str) -> bool:
        if getattr(self.engine, "ready", True) is not False:
            return True
        with self._wake:
            chat = self._chats.get(psid)
            now = self.ctx.clock()
            if chat is None:
                return True
            if chat.waiting_since is None:
                chat.waiting_since = now
                LOGGER.info("messenger_engine_not_ready psid=%s", mask_psid(psid))
            return now - chat.waiting_since >= READY_WAIT_SECONDS

    # -- bursts ---------------------------------------------------------------------------------

    def run_due_bursts(self) -> dict[str, str]:
        """Run, in this thread, every burst that is due; {psid: status}."""
        return {psid: self._run_taken(psid) for psid in self._take_due()}

    def run_burst(self, psid: str) -> str:
        """Answer the chat now, whether or not its window is due ('busy' when a turn runs)."""
        with self._wake:
            chat = self._chats.setdefault(psid, _Chat())
            if chat.running:
                return "busy"
            self._begin(chat)
        return self._run_taken(psid)

    def _run_taken(self, psid: str) -> str:
        retry_in = None
        try:
            return self._burst(psid)
        except _Later as later:
            retry_in = later.seconds
            return later.status
        except Exception as error:
            # Most often the database is away. The chat's messages are still pending: look
            # again after a pause, not at once (that would spin, logging this every time).
            LOGGER.warning("messenger_burst_failed psid=%s error=%s", mask_psid(psid),
                           _error_code(error))
            retry_in = READY_RETRY_SECONDS
            return "error"
        finally:
            self._release(psid, retry_in)

    def _burst(self, psid: str) -> str:
        if not self._engine_ready(psid):
            raise _Later(READY_RETRY_SECONDS)
        page_id = self._cached_page(fresh=True)
        if page_id is None:
            # Disconnected while the messages waited: they are answered once a page is
            # connected again, if still young enough.
            raise _Later(NOT_CONNECTED_RETRY_SECONDS, "not_connected")
        settings = self.store.settings()
        thread = self.store.get_thread(psid, with_state=True)
        if thread is None:
            # The chat was deleted (the dashboard's Delete data): its messages are gone too.
            self._snapshot(psid, self._seen(psid), [], None)
            return "no_chat"
        seen = self._seen(psid)
        rows = [dict(row) for row in self.store.unanswered_customer_messages(
            psid, ignore_app_ids=self.ignore_app_ids)]
        mids = [row["mid"] for row in rows]
        if thread.get("is_admin"):
            # The admin's commands were handled when they arrived; they are never the engine's.
            rows = [row for row in rows if command_of(row.get("body")) is None]
        self._snapshot(psid, seen, mids, rows[-1]["sent_at"] if rows else None)
        if not rows:
            return "nothing"
        state, channel = _split_state(thread.get("engine_state"))
        kind = _kind(rows, channel)
        newest = rows[-1]
        burst = _Burst(psid=psid, page_id=page_id, rows=tuple(rows), newest_mid=newest["mid"],
                       newest_sent_at=newest["sent_at"], kind=kind)
        name = "messenger" if kind == "turn" else "notice"
        skip = self._rules(settings, thread, burst.newest_sent_at, claim=True)
        if skip is not None:
            return self._close(name, burst, skip)
        token = self._send_token_row()
        if token is None:
            return "not_connected"
        if token.get("status") != "ok":
            # Meta said the token is dead; nothing is sent (and no turn spent) until the
            # dashboard reconnects, which stores the token as ok again.
            return self._close(name, burst, "failed", attention="send_token_broken",
                               error_text="send_token_broken")
        keys = [row["mid"] for row in rows]
        if kind == "turn":
            with self._cap_lock:
                reply_id = self.store.start_reply("messenger", psid, burst.newest_mid, keys)
                if reply_id is None:
                    return "duplicate"
                try:
                    capped = self._capped(settings, psid)
                except Exception:
                    self._finish_reply(reply_id, _Reply(status="failed"))
                    raise
            if capped:
                self._finish_reply(reply_id, _Reply(status="skipped_cap"))
                self._cap_notice(burst, _language(state))
                self._flag(psid, "daily_cap")
                LOGGER.info("messenger_burst psid=%s status=skipped_cap", mask_psid(psid))
                return "skipped_cap"
        else:
            reply_id = self.store.start_reply("notice", psid, burst.newest_mid, keys)
            if reply_id is None:
                return "duplicate"
        return self._answer(burst, reply_id, settings)

    def _rules(self, settings: Mapping[str, Any], thread: Mapping[str, Any] | None,
               newest_sent_at: datetime, *, claim: bool) -> str | None:
        """Why the bot may not answer this burst now, or None. At claim time the maximum age
        applies too; before a send it does not (the reply is already under way)."""
        if not settings.get("bot_enabled"):
            return "skipped_off"
        if settings.get("bot_paused_reason"):
            return "skipped_paused"
        if thread is None or not thread.get("bot_enabled"):
            return "skipped_chat_off"
        enabled_at = settings.get("bot_enabled_at")
        if enabled_at is None or newest_sent_at <= enabled_at:
            return "skipped_off"  # written before the bot was switched on: never a backlog reply
        if claim:
            limit = timedelta(minutes=int(settings.get("max_reply_age_minutes") or 1440))
            if self.ctx.now() - newest_sent_at > limit:
                return "skipped_old"
        return None

    def _capped(self, settings: Mapping[str, Any], psid: str) -> bool:
        """Whether today's turns, this one included (it is pending), pass a cap. A cap of 0
        allows no turn at all."""
        return (self.store.turns_today() > int(settings["daily_turn_limit"])
                or self.store.turns_today(psid) > int(settings["customer_daily_turn_limit"]))

    def _close(self, channel: str, burst: _Burst, status: str, *, attention: str = "",
               error_text: str = "") -> str:
        """Record a burst the bot will not answer, once: the same messages never come back."""
        reply_id = self.store.start_reply(channel, burst.psid, burst.newest_mid,
                                          [row["mid"] for row in burst.rows])
        if reply_id is None:
            return "duplicate"
        self._finish_reply(reply_id, _Reply(status=status, error_text=error_text))
        if attention:
            self._flag(burst.psid, attention)
        LOGGER.info("messenger_burst psid=%s status=%s", mask_psid(burst.psid), status)
        return status

    def _cap_notice(self, burst: _Burst, language: str) -> None:
        """One polite notice per chat per Tbilisi day, with the shop's phone."""
        day = self.ctx.now().astimezone(TBILISI).date().isoformat()
        notice_id = self.store.start_reply("notice", burst.psid, f"cap:{burst.psid}:{day}")
        if notice_id is None:
            return
        reply = _Reply()
        try:
            self._deliver(burst, _text_messages(cap_notice(language, self._phone())), notice_id,
                          reply, done="sent")
        finally:
            self._finish_reply(notice_id, reply)

    def _answer(self, burst: _Burst, reply_id: int, settings: Mapping[str, Any]) -> str:
        """Answer the burst (its reply row is open) and close the row, whatever happens."""
        psid = burst.psid
        reply = _Reply()
        saving: tuple[dict, dict] | None = None
        state: dict = {}
        channel: dict = {}
        try:
            with self._state_lock(psid):
                self._active[psid] = []
                thread = self.store.get_thread(psid, with_state=True)
            state, channel = _split_state(thread.get("engine_state") if thread else None)
            if burst.kind == "media":
                saving = self._answer_media(burst, reply_id, reply, state, channel)
            elif burst.kind == "more":
                saving = self._answer_more(burst, reply_id, reply, state, channel)
            else:
                saving = self._answer_turn(burst, reply_id, reply, state, channel, settings)
        except Exception as error:  # the status says what was delivered before it ('failed')
            LOGGER.warning("messenger_reply_failed psid=%s error=%s", mask_psid(psid),
                           _error_code(error))
        finally:
            self._settle(psid, saving, state, channel)
            self._finish_reply(reply_id, reply)
            if reply.attention:
                self._flag(psid, reply.attention)
        LOGGER.info("messenger_reply psid=%s kind=%s status=%s parts=%d engine=%s",
                    mask_psid(psid), burst.kind, reply.status, len(reply.sent_mids),
                    reply.engine_status or "-")
        return reply.status

    def _answer_media(self, burst: _Burst, reply_id: int, reply: _Reply, state: dict,
                      channel: dict) -> tuple[dict, dict] | None:
        """Photos, voice notes and stickers alone: ask for the car, VIN or part in words."""
        language = _language(state)
        messages = _text_messages(media_only_text(language), self._apology(burst, language))
        if self._deliver(burst, messages, reply_id, reply, done="sent"):
            return state, {}
        return None

    def _answer_more(self, burst: _Burst, reply_id: int, reply: _Reply, state: dict,
                     channel: dict) -> tuple[dict, dict] | None:
        """The next page of the last reply's parts, from the kept view: no turn is spent."""
        view, offset = channel.get("view"), channel.get("offset")
        if not isinstance(view, dict) or not isinstance(offset, int):
            reply.status = "skipped_shop_answered"  # a shop message cleared the view meanwhile
            return None
        language = view.get("language") if view.get("language") in ("ka", "en") \
            else _language(state)
        rendered = render(view, language=language, offset=offset,
                          opening=self._apology(burst, language))
        if not rendered.messages:
            reply.status = "failed"
            return state, {key: channel[key] for key in ("options", "numbered") if key in channel}
        sent = self._deliver(burst, rendered.messages, reply_id, reply, done="sent")
        if not sent:
            return None
        updated = dict(channel)
        if rendered.next_offset is None or sent < len(rendered.messages):
            updated.pop("view", None)
            updated.pop("offset", None)
        else:
            updated["offset"] = rendered.next_offset
        return state, updated

    def _answer_turn(self, burst: _Burst, reply_id: int, reply: _Reply, state: dict,
                     channel: dict, settings: Mapping[str, Any]) -> tuple[dict, dict] | None:
        """One engine turn and its reply. Returns the (state, channel) to save: the state that
        holds the reply when anything was sent, the state with the messages still unanswered
        when the engine ran and nothing was sent, or None when the engine never ran."""
        psid = burst.psid
        try:
            choice = self.ctx.send_token()
        except (NotConnected, TokenCipherError):
            reply.status, reply.error_text = "failed", "send_token_unavailable"
            reply.attention = "send_token_broken"
            return None
        if self._typing(burst, choice, reply):
            return None
        newest = burst.rows[-1]
        number = parse_choice(newest.get("body"), newest.get("quick_reply"),
                              channel.get("options") or [], bool(channel.get("numbered")))
        done = threading.Event()
        threading.Thread(target=self._keep_typing, args=(burst, choice, done),
                         name="messenger-typing", daemon=True).start()
        try:
            result = self.engine.respond(state or None, _joined(burst.rows), choice=number,
                                         cancel=self._cancel_event(psid))
        finally:
            done.set()
        try:
            return self._reply_turn(burst, reply_id, reply, channel, settings, choice, result)
        finally:
            # After the reply went out (or could not): the customer never waits for the
            # alarms' database writes, and a reply that cannot be delivered still alarms.
            self._raise_alerts(psid, result)

    def _reply_turn(self, burst: _Burst, reply_id: int, reply: _Reply, channel: dict,
                    settings: Mapping[str, Any], choice: Any,
                    result: Any) -> tuple[dict, dict] | None:
        """Send what the engine answered (the rest of ``_answer_turn``)."""
        psid = burst.psid
        # The engine ran: a non-empty status makes the reply count as a turn for the caps.
        reply.engine_status = str(result.engine_status or result.error_code
                                  or str(result.status).upper() or "RAN")[:40]
        reply.usage = dict(result.usage or {})
        reply.cost_usd = self._cost(reply.usage)
        language = result.language
        answered = result.state
        unsent = result.unsent_state or result.state
        apology = self._apology(burst, language)
        if result.status == "ok":
            rendered = render(result.public, language=language, opening=apology)
            if rendered.messages:
                sent = self._deliver(burst, rendered.messages, reply_id, reply, done="sent")
                if not sent:
                    return unsent, channel
                whole = sent == len(rendered.messages)
                return answered, (_reply_channel(rendered, result.public) if whole else {})
        elif result.error_code in ACCOUNT_CODES:
            newly = self.store.pause_for_provider()
            LOGGER.warning("messenger_bot_paused reason=%s newly=%s", result.error_code, newly)
            reply.status, reply.attention = "paused_provider", "ai_provider"
            return unsent, channel
        elif result.status == "cancelled":
            self._apply_stop(psid, self._stop_of(psid)[0], choice.purpose, reply)
            return unsent, channel
        elif result.error_code == "CONFLICTING_VINS":
            messages = _text_messages(conflicting_vins_text(language), apology)
            if self._deliver(burst, messages, reply_id, reply, done="sent"):
                return answered, {}
            return unsent, channel
        if result.error_code == "CHAT_CLOSED" or self._stop.is_set():
            # The worker is stopping and closed the engine under the turn: no fallback for a
            # failure of our own making; the shop sees the flag.
            reply.status, reply.attention = "abandoned", "worker_stopped"
            return unsent, channel
        # A failure, a timeout or an empty answer: one short message with the shop's phone.
        custom = settings.get("fallback_en" if text_language(language) == "en"
                              else "fallback_ka") or ""
        messages = _text_messages(fallback_text(language, self._phone(), custom), apology)
        if self._deliver(burst, messages, reply_id, reply, done="fallback_sent"):
            reply.attention = reply.attention or (
                "engine_timeout" if result.status == "timeout" else "engine_failed")
            return answered, {}
        return unsent, channel

    def _raise_alerts(self, psid: str, result: Any) -> None:
        """Record what the turn means for the staff (dashboard > Live chats): a part not found, a
        visit or service request, a handoff, a phone number. Called once the reply is sent or
        could not be, so a reply that cannot be delivered still alarms and the customer never
        waits for these writes. Never raises and never logs a message or a number."""
        turn = getattr(result, "alerts", None)
        if self.live is None or not turn:
            return
        try:
            if self.live.usable():
                self.live.record_turn("messenger", psid, turn)
        except Exception as error:
            LOGGER.warning("live_alert_failed psid=%s error=%s", mask_psid(psid),
                           _error_code(error))

    def _settle(self, psid: str, saving: tuple[dict, dict] | None, state: dict,
                channel: dict) -> None:
        """Save the chat's state once the reply is over, with any shop words that arrived
        meanwhile added on top, and let echoes write the state directly again."""
        with self._state_lock(psid):
            deferred = self._active.pop(psid, [])
            if saving is None and not deferred:
                return
            new_state, new_channel = saving if saving is not None else (state, channel)
            for shop_text, customer_text in deferred:
                new_state = self._shop_reply(new_state, shop_text, customer_text)
                new_channel = {}
            try:
                self._save_state(psid, new_state, new_channel)
            except Exception as error:
                LOGGER.warning("messenger_state_not_saved psid=%s error=%s", mask_psid(psid),
                               _error_code(error))

    def _apology(self, burst: _Burst, language: str) -> str:
        """The apology for a late reply, when the newest message is over an hour old."""
        if self.ctx.now() - burst.newest_sent_at > timedelta(seconds=LATE_SECONDS):
            return late_apology(language)
        return ""

    def _cost(self, usage: Mapping[str, Any]) -> float | None:
        try:
            return self.cost(usage)
        except Exception:
            return None

    def _phone(self) -> str:
        try:
            return str(self.engine.shop_phone() or "")
        except Exception:
            return ""

    # -- sending ------------------------------------------------------------------------------

    def _may_send(self, burst: _Burst) -> str | None:
        """Read the switches and the chat again, immediately before a Send API call. A status
        to stop with, or None to go ahead. Command replies are never stopped."""
        if not burst.checked:
            return None
        settings = self.store.settings()
        thread = self.store.get_thread(burst.psid)
        status = self._rules(settings, thread, burst.newest_sent_at, claim=False)
        if status is not None:
            return status
        if self._shop_answered(thread, burst):
            return "skipped_shop_answered"
        with self._wake:
            chat = self._chats.get(burst.psid)
            if chat is not None and chat.running:
                if chat.pending:
                    return "skipped_superseded"
                if chat.stop_reason:
                    return chat.stop_reason
        return None

    def _shop_answered(self, thread: Mapping[str, Any], burst: _Burst) -> bool:
        """Whether the shop wrote after the burst's newest message. The thread's last_shop_at is
        enough; the bot's own parts are stored as the bot's and never count."""
        last = thread.get("last_shop_at")
        if last is None or last <= burst.newest_sent_at:
            return False
        if not self.ignore_app_ids:
            return True
        return any(row["sender"] == "shop" and row["sent_at"] > burst.newest_sent_at
                   and row.get("app_id") not in self.ignore_app_ids
                   for row in self.store.messages(burst.psid, 50))

    def _typing(self, burst: _Burst, choice: Any, reply: _Reply) -> bool:
        """mark_seen, then typing_on, each after the switch check. True when the reply must
        stop: a switch is off, or Meta refused in a way the reply would meet too (a dead token,
        the gate, a closed window, an unavailable person), so no turn is spent on it."""
        for action in ("mark_seen", "typing_on"):
            try:
                stop = self._may_send(burst)
            except Exception as error:
                LOGGER.warning("messenger_switch_check_failed psid=%s error=%s",
                               mask_psid(burst.psid), _error_code(error))
                reply.status, reply.error_text = "failed", "switch_check_failed"
                return True
            if stop is not None:
                self._apply_stop(burst.psid, stop, choice.purpose, reply)
                return True
            try:
                self.graph.sender_action(burst.page_id, choice, burst.psid, action)
            except GraphError as error:
                if error.token_dead or error.gated or error.outside_window or error.unavailable:
                    self._graph_failure(error, choice.purpose, reply)
                    return True
                LOGGER.info("messenger_typing_failed psid=%s action=%s code=%s",
                            mask_psid(burst.psid), action, error.code)
        return False

    def _keep_typing(self, burst: _Burst, choice: Any, done: threading.Event) -> None:
        """typing_on again every 15 s while the engine works; a switch found off, or Meta
        refusing, stops the turn at once instead of at its send."""
        while not done.wait(self.typing_seconds):
            try:
                stop = self._may_send(burst)
            except Exception as error:  # the send itself checks again; the indicator can wait
                LOGGER.info("messenger_typing_check_failed psid=%s error=%s",
                            mask_psid(burst.psid), _error_code(error))
                continue
            if done.is_set():  # the turn ended during the check: stop nothing, type nothing
                return
            if stop is not None:
                self._stop_running(burst.psid, stop)
                return
            try:
                self.graph.sender_action(burst.page_id, choice, burst.psid, "typing_on")
            except GraphError as error:
                if error.token_dead or error.gated or error.outside_window or error.unavailable:
                    if not done.is_set():
                        self._stop_running(burst.psid, GRAPH_STOP, error)
                    return
                LOGGER.info("messenger_typing_failed psid=%s action=typing_on code=%s",
                            mask_psid(burst.psid), error.code)
            except Exception as error:  # a malformed call is the send's to report
                LOGGER.info("messenger_typing_failed psid=%s action=typing_on error=%s",
                            mask_psid(burst.psid), _error_code(error))

    def _apply_stop(self, psid: str, stop: str, purpose: str, reply: _Reply) -> None:
        if stop == GRAPH_STOP:
            _, error = self._stop_of(psid)
            if error is not None:
                self._graph_failure(error, purpose, reply)
                return
            reply.status = "failed"
            return
        reply.status = stop

    def _graph_failure(self, error: GraphError, purpose: str, reply: _Reply) -> None:
        """Map a Send API refusal to the reply's status and the chat's flag. A dead token is
        marked broken, which stops every send until the dashboard reconnects."""
        reply.graph(error)
        if error.token_dead:
            try:
                self.store.mark_token(purpose, "broken", f"meta_code_{error.code}")
            except Exception as failure:
                LOGGER.warning("messenger_token_mark_failed purpose=%s error=%s", purpose,
                               _error_code(failure))
            self._apps_cache = None
            reply.status, reply.attention = "failed", "send_token_dead"
        elif error.gated:
            reply.status, reply.attention = "blocked_by_meta", "blocked_by_meta"
        elif error.outside_window:
            reply.status = "skipped_old"
        elif error.unavailable:
            reply.status = "failed"
        else:
            reply.status, reply.attention = "failed", "send_failed"
        LOGGER.warning("messenger_send_refused status=%s code=%s subcode=%s http=%s",
                       reply.status, error.code, error.subcode, error.http_status)

    def _deliver(self, burst: _Burst, messages: Sequence[dict], reply_id: int, reply: _Reply,
                 *, done: str) -> int:
        """Send ``messages`` in order, each after the switch check; how many were sent. Sets
        ``reply.status`` to ``done`` when all went, else to the reason it stopped. A send is
        never repeated: Meta may have delivered one that timed out."""
        try:
            choice = self.ctx.send_token()
        except (NotConnected, TokenCipherError):
            reply.status, reply.error_text = "failed", "send_token_unavailable"
            reply.attention = "send_token_broken"
            return 0
        app_id = self._token_app(choice.purpose)
        sent = 0
        for message in messages:
            try:
                stop = self._may_send(burst)
            except Exception as error:  # The switches cannot be read: do not send blind.
                LOGGER.warning("messenger_switch_check_failed psid=%s error=%s",
                               mask_psid(burst.psid), _error_code(error))
                reply.status, reply.error_text = "failed", "switch_check_failed"
                return sent
            if stop is not None:
                self._apply_stop(burst.psid, stop, choice.purpose, reply)
                return sent
            try:
                answer = self.graph.send_message(burst.page_id, choice, burst.psid, message)
            except GraphError as error:
                self._graph_failure(error, choice.purpose, reply)
                return sent
            sent += 1
            mid = str(answer.get("message_id") or "")
            if not mid:
                continue  # Meta's echo of it stores it, as our app's message
            with self._sent_lock:
                self._sent.append(mid)
            reply.sent_mids.append(mid)
            try:
                self.store.add_sent_mid(reply_id, mid)
                self.store.add_message(burst.psid, mid, "bot", self._sent_at(burst),
                                       page_id=burst.page_id, body=str(message.get("text") or ""),
                                       app_id=app_id)
            except Exception as error:  # Sent all the same; the echo stores it.
                LOGGER.warning("messenger_sent_not_stored psid=%s error=%s",
                               mask_psid(burst.psid), _error_code(error))
        reply.status = done
        return sent

    def _sent_at(self, burst: _Burst) -> datetime:
        """The time the bot's message is stored with: just after the newest message it
        answers, on Meta's clock. "Unanswered" means after the newest bot or shop message, so a
        message the customer wrote while the turn ran (its webhook landing after the last
        check) stays unanswered and gets the next turn; stamped with the local time, it would
        sort before the reply and count as answered for good. ``stored_at`` keeps the real
        time."""
        return burst.newest_sent_at + timedelta(milliseconds=1)

    # -- commands ---------------------------------------------------------------------------

    def _run_command(self, psid: str, page_id: str, mid: str, command: str) -> str:
        """'#bot off|on|status' from the admin's own chat: the switch changes here, at once;
        the answer is sent on the command thread. Never reaches the engine."""
        reply_id = self.store.start_reply("command", psid, mid)
        if reply_id is None:
            return "command_duplicate"
        actor = f"messenger:{psid[-4:]}"
        language, outcome, done = "ka", "command", "sent"
        try:
            thread = self.store.get_thread(psid, with_state=True)
            language = _language(_split_state(thread.get("engine_state") if thread else None)[0])
            text = self._command_reply(command, actor, psid, language)
        except Exception as error:
            # The switch is as it was; the admin is told so instead of hearing nothing.
            LOGGER.warning("messenger_command_failed psid=%s command=%s error=%s",
                           mask_psid(psid), command, _error_code(error))
            text, outcome, done = bot_failed_text(language), "command_failed", "failed"
        else:
            LOGGER.info("messenger_command psid=%s command=%s", mask_psid(psid), command)
        burst = _Burst(psid=psid, page_id=page_id, rows=(), newest_mid=mid,
                       newest_sent_at=self.ctx.now(), kind="command", checked=False)
        self._dispatch(self._commands, self._command_send, burst, text, reply_id, done)
        return outcome

    def _command_reply(self, command: str, actor: str, psid: str, language: str) -> str:
        """Apply the command and word the answer. set_bot audits the switch itself; the rest
        are audited here, so every command leaves an audit row."""
        target = mask_psid(psid)
        if command == "off":
            before = self.store.settings()
            self.store.set_bot(False, actor)
            return bot_switched_text(language, False, changed=bool(before.get("bot_enabled")))
        if command == "on":
            before = self.store.settings()
            try:
                self.store.set_bot(True, actor)  # never confirms Auto reply on the admin's behalf
            except AutoReplyNotConfirmed:
                self.store.audit(actor, "messenger.bot_on_refused", target,
                                 {"reason": "auto_reply_not_confirmed"})
                return bot_on_refused_text(language)
            changed = not before.get("bot_enabled") or bool(before.get("bot_paused_reason"))
            return bot_switched_text(language, True, changed=changed)
        if command == "status":
            self.store.audit(actor, "messenger.bot_status", target, {})
            return self.status_text(language)
        self.store.audit(actor, "messenger.bot_help", target, {})
        return bot_help_text(language)

    def status_text(self, language: str = "ka") -> str:
        """The bot's state, since when, today's turns against the cap, the comment switch and
        today's estimated cost."""
        settings = self.store.settings()
        enabled, paused = bool(settings.get("bot_enabled")), settings.get("bot_paused_reason")
        lines = bot_status_text(language, enabled=enabled, paused=paused,
                                turns_today=self.store.turns_today(),
                                turn_limit=settings.get("daily_turn_limit"),
                                comments_enabled=settings.get("comments_enabled")).split("\n")
        texts = STATUS_TEXTS[text_language(language)]
        since = (settings.get("bot_paused_at") if enabled and paused
                 else settings.get("bot_enabled_at") if enabled
                 else settings.get("bot_changed_at"))
        if isinstance(since, datetime):
            lines.insert(1, texts["since"].replace("{time}", _local_time(since)))
        lines.append(texts["cost"].replace("{cost}", f"{self.store.cost_today():.2f}"))
        return "\n".join(lines)

    def _command_send(self, burst: _Burst, text: str, reply_id: int,
                      done: str = "sent") -> None:
        """Send a command's answer. ``done`` 'failed': the command was not carried out, and the
        attempt says so even when its notice went out."""
        reply = _Reply(error_text="command_failed" if done == "failed" else "")
        try:
            self._deliver(burst, _text_messages(text), reply_id, reply, done=done)
        except Exception as error:
            LOGGER.warning("messenger_command_send_failed psid=%s error=%s",
                           mask_psid(burst.psid), _error_code(error))
        finally:
            self._finish_reply(reply_id, reply)

    # -- small effects -------------------------------------------------------------------------

    def _close_trigger(self, psid: str, mid: str, status: str) -> None:
        """Mark a message as never to be answered on its own, on both channels a burst ending
        with it could use (a turn, or the text request for a photo)."""
        for channel in ("messenger", "notice"):
            reply_id = self.store.start_reply(channel, psid, mid)
            if reply_id is not None:
                self._finish_reply(reply_id, _Reply(status=status))

    def _finish_reply(self, reply_id: int, reply: _Reply) -> None:
        try:
            self.store.finish_reply(reply_id, reply.status, **reply.fields())
        except Exception as error:  # Left pending; abandon_stale_replies closes it later.
            LOGGER.warning("messenger_reply_not_recorded reply_id=%s error=%s", reply_id,
                           _error_code(error))

    def _flag(self, psid: str, reason: str) -> None:
        try:
            self.store.flag_attention(psid, reason)
        except Exception as error:
            LOGGER.warning("messenger_flag_failed psid=%s error=%s", mask_psid(psid),
                           _error_code(error))

    def _turn_likely(self, thread: Mapping[str, Any], sent_at: datetime, text: str) -> bool:
        """Whether this message will lead to a turn, so warming the engine is worth its one
        billed request."""
        if not str(text or "").strip():
            return False
        try:
            settings = self.store.settings()
            if self._rules(settings, thread, sent_at, claim=True) is not None:
                return False
            # At a cap the burst ends as skipped_cap. Unlike _capped, this counts before the
            # burst's own attempt is open, so reaching the limit is enough.
            return (self.store.turns_today() < int(settings["daily_turn_limit"])
                    and self.store.turns_today(thread["psid"])
                    < int(settings["customer_daily_turn_limit"]))
        except Exception:
            return False

    def _state_lock(self, psid: str) -> threading.Lock:
        return self._state_locks[hash(psid) % len(self._state_locks)]

    def _shop_reply(self, state: dict, shop_text: str, customer_text: str) -> dict:
        function = self._record_shop_reply
        if function is None:
            from automotive.messenger_engine import record_shop_reply as function
            self._record_shop_reply = function
        return function(state, shop_text, customer_text=customer_text)

    def _save_state(self, psid: str, state: Mapping[str, Any] | None,
                    channel: Mapping[str, Any] | None) -> None:
        data = dict(state or {})
        data.pop(CHANNEL_KEY, None)
        if channel:
            data[CHANNEL_KEY] = dict(channel)
        try:
            self.store.save_engine_state(psid, data)
        except ValueError:  # over the column's size: the engine's own part is what matters
            data.pop(CHANNEL_KEY, None)
            self.store.save_engine_state(psid, data)

    def _log_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._quiet_logged:
            self._quiet_logged.add(key)
            LOGGER.info(message, *args)

    # -- recovery -------------------------------------------------------------------------------

    def recover(self) -> int:
        """At start-up: close the reply attempts a dead worker left open, and look again at
        every chat whose newest message is a customer's, young enough to answer. Bursts live in
        memory, and their inbox rows are already finished. How many chats were rescheduled."""
        try:
            closed = self.store.abandon_stale_replies(STALE_REPLY_SECONDS)
            if closed:
                LOGGER.info("messenger_replies_abandoned count=%d", closed)
        except Exception as error:
            LOGGER.warning("messenger_abandon_failed error=%s", _error_code(error))
        settings = self.store.settings()
        oldest = self.ctx.now() - timedelta(minutes=int(settings.get("max_reply_age_minutes")
                                                        or 1440))
        count = 0
        for thread in self.store.list_threads("all", limit=RECOVERY_LIMIT):
            last = thread.get("last_customer_at")
            if last is None or last < oldest:
                continue
            answered = [when for when in (thread.get("last_bot_at"), thread.get("last_shop_at"))
                        if when is not None]
            if answered and max(answered) >= last:
                continue
            self._recheck(thread["psid"], self.quiet_seconds)
            count += 1
        if count:
            LOGGER.info("messenger_recovered chats=%d", count)
        return count

    # -- jobs -----------------------------------------------------------------------------------

    def run_jobs(self, *, startup: bool = False) -> dict[str, str]:
        """The dashboard's requests first, then every job that is due; {job: status}."""
        results: dict[str, str] = {}
        try:
            requested = self.store.take_job_requests()
        except Exception as error:
            LOGGER.warning("messenger_job_requests_failed error=%s", _error_code(error))
            requested = []
        names = [(str(row.get("job") or ""), True) for row in requested]
        names += [(job.name, False) for job in self._due_jobs(startup)]
        for name, asked in names:
            if self._stop.is_set():
                break
            if name in results:
                continue
            try:
                results[name] = self.run_job(name, requested=asked)
            except Exception as error:  # one job's fault never holds up the others
                LOGGER.warning("messenger_job_failed job=%s error=%s", name, _error_code(error))
                results[name] = "error"
        return results

    def _due_jobs(self, startup: bool) -> list[Job]:
        """Due from each job's last recorded run, so a restart does not run everything again;
        a failed run is retried within half an hour."""
        now = self.ctx.now()
        try:
            latest = {row["job"]: row for row in self.store.latest_runs()}
        except Exception as error:
            LOGGER.warning("messenger_job_runs_unreadable error=%s", _error_code(error))
            return []
        due = []
        for job in JOBS:
            hold = self._job_hold.get(job.name)
            if hold is not None and now < hold:
                continue
            last = latest.get(job.name)
            if (startup and job.at_startup) or last is None:
                due.append(job)
                continue
            wait = job.every if last.get("status") != "error" else min(job.every,
                                                                         ERROR_RETRY_SECONDS)
            if last["started_at"] + timedelta(seconds=wait) <= now:
                due.append(job)
        return due

    def run_job(self, name: str, *, requested: bool = False) -> str:
        """Run one job now, recorded in meta_sync_runs: 'ok', 'error' or 'skipped'. A job that
        needs the page skips while none is connected, and a feature module that is not there
        yet skips; both are recorded only when the dashboard asked."""
        job = JOBS_BY_NAME.get(name)
        if job is None:
            if requested:
                self._record_run(name, "skipped", "unknown_job")
            return "skipped"
        now = self.ctx.now()
        if job.needs_page:
            try:
                connected = self._cached_page(fresh=True) is not None
            except Exception as error:
                LOGGER.warning("messenger_job_failed job=%s error=%s", name, _error_code(error))
                return "error"
            if not connected:
                if requested:
                    self._record_run(name, "skipped", "not_connected")
                else:
                    self._job_hold[name] = now + timedelta(seconds=NOT_CONNECTED_RETRY_SECONDS)
                return "skipped"
        target = self._job_target(job)
        if target is None:
            if requested:
                self._record_run(name, "skipped", "not_available")
            else:
                self._job_hold[name] = now + timedelta(seconds=job.every)
                self._log_once(f"job:{name}", "messenger_job_unavailable job=%s", name)
            return "skipped"
        self._job_hold.pop(name, None)
        try:
            run_id = self.store.start_run(name)
        except Exception as error:
            LOGGER.warning("messenger_job_failed job=%s error=%s", name, _error_code(error))
            return "error"
        status, records, detail = "error", 0, ""
        try:
            result = target(self.ctx)
            records, detail = _summary(name, result)
            status = _run_status(result)
        except NotConnected:
            status, detail = "skipped", "not_connected"
        except ConnectError as error:
            status, detail = (("skipped", "not_configured") if error.code == "NOT_CONFIGURED"
                              else ("error", _connect_detail(error)))
        except GraphError as error:
            detail = str(error)
        except TokenCipherError:
            detail = "token_unreadable"
        except Exception as error:
            detail = _error_code(error)
        try:
            self.store.finish_run(run_id, status, records, detail)
        except Exception as error:
            LOGGER.warning("messenger_job_not_recorded job=%s error=%s", name, _error_code(error))
        LOGGER.info("messenger_job job=%s status=%s records=%d requested=%s", name, status,
                    records, requested)
        return status

    def _job_target(self, job: Job) -> Callable[[MetaContext], Any] | None:
        override = self.job_targets.get(job.name)
        if override is not None:
            return override
        if job.feature is None:
            return BUILT_IN_JOBS.get(job.name)
        module_name, function_name = job.feature
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # absent, or broken: either way it cannot run this pass
            self._log_once(f"import:{module_name}", "messenger_module_unavailable module=%s "
                           "error=%s", module_name, type(error).__name__)
            return None
        function = getattr(module, function_name, None)
        return function if callable(function) else None

    def _record_run(self, name: str, status: str, detail: str) -> None:
        try:
            self.store.finish_run(self.store.start_run(name), status, 0, detail)
        except Exception as error:
            LOGGER.warning("messenger_job_not_recorded job=%s error=%s", name, _error_code(error))

    # -- threads ----------------------------------------------------------------------------

    def start(self) -> None:
        """Recover, then run the inbox, the scheduler and the jobs on their own threads, with a
        pool of ``concurrent_turns`` turns and one thread each for commands and comments."""
        if self._threads:
            raise RuntimeError("The worker is already running")
        self._stop.clear()
        self._turns = ThreadPoolExecutor(self.concurrent_turns, thread_name_prefix="messenger-turn")
        self._commands = ThreadPoolExecutor(1, thread_name_prefix="messenger-command")
        self._comments = ThreadPoolExecutor(1, thread_name_prefix="messenger-comment")
        try:
            self.recover()
        except Exception as error:
            LOGGER.warning("messenger_recovery_failed error=%s", _error_code(error))
        for name, target in (("messenger-inbox", self._inbox_loop),
                             ("messenger-bursts", self._scheduler_loop),
                             ("messenger-jobs", self._jobs_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def request_stop(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()

    def stop(self, timeout: float = DRAIN_SECONDS) -> None:
        """Stop taking work and let running turns finish, up to ``timeout`` seconds. A burst not
        started yet is looked at again by ``recover`` at the next start."""
        self.request_stop()
        deadline = time.monotonic() + max(0.0, timeout)
        # The inbox and the scheduler stop within a poll; after that no new turn is handed out,
        # so the running ones are all in _futures. A long job may still be finishing a call.
        for thread in self._threads:
            if thread.name != "messenger-jobs":
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._futures_lock:
            running = list(self._futures)
        wait_for_futures(running, timeout=max(0.0, deadline - time.monotonic()))
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for executor in (self._turns, self._commands, self._comments):
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        self._flush_retries(force=True)
        self._threads = []

    def _inbox_loop(self) -> None:
        listener = None
        try:
            while not self._stop.is_set():
                if listener is None:
                    listener = self._listen()
                try:
                    claimed = self.poll_inbox()
                except Exception as error:
                    LOGGER.warning("messenger_inbox_poll_failed error=%s", _error_code(error))
                    claimed = 0
                if claimed >= INBOX_BATCH:
                    continue
                listener = self._wait_for_inbox(listener)
        finally:
            if listener is not None:
                with contextlib.suppress(Exception):
                    listener.close()
            self._flush_retries(force=True)

    def _listen(self) -> Any:
        """A dedicated autocommit connection LISTENing on the inbox channel; None (and the
        2-second poll alone) while it cannot be opened."""
        connect = getattr(getattr(self.store, "db", None), "connect", None)
        if not callable(connect):
            return None
        try:
            connection = connect(autocommit=True)
            try:
                connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(INBOX_CHANNEL)))
            except Exception:
                connection.close()
                raise
        except Exception as error:
            if self._listening is not False:
                LOGGER.info("messenger_inbox_listen_unavailable error=%s", _error_code(error))
            self._listening = False
            return None
        if self._listening is not True:
            LOGGER.info("messenger_inbox_listening channel=%s", INBOX_CHANNEL)
        self._listening = True
        return connection

    def _wait_for_inbox(self, listener: Any) -> Any:
        if listener is None:
            self._stop.wait(self.poll_seconds)
            return None
        try:
            for _ in listener.notifies(timeout=self.poll_seconds, stop_after=1):
                pass
            return listener
        except Exception as error:
            LOGGER.info("messenger_inbox_listen_lost error=%s", _error_code(error))
            self._listening = None
            with contextlib.suppress(Exception):
                listener.close()
            return None

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            for psid in self._take_due():
                self._dispatch(self._turns, self._turn_task, psid)
            with self._wake:
                if self._stop.is_set():
                    break
                self._wake.wait(self._idle_seconds())

    def _turn_task(self, psid: str) -> None:
        if self._stop.is_set():
            self._release(psid)  # not started: the next start's recovery looks at it again
            return
        self._run_taken(psid)

    def _jobs_loop(self) -> None:
        startup = True
        while not self._stop.is_set():
            try:
                self.run_jobs(startup=startup)
            except Exception as error:
                LOGGER.warning("messenger_jobs_failed error=%s", _error_code(error))
            startup = False
            wall, steady = time.time(), time.monotonic()
            self._stop.wait(JOB_POLL_SECONDS)
            # The monotonic clock stands still while the laptop sleeps and the wall clock does
            # not: a gap between them is a wake-up. Meta has given up on what it could not
            # deliver meanwhile, so the webhook subscription is checked and the missed messages
            # are read back at once, once the network had a moment to come back.
            slept = (time.time() - wall) - (time.monotonic() - steady)
            if slept > WAKE_GAP_SECONDS:
                LOGGER.info("messenger_woke slept_s=%d", int(slept))
                self._stop.wait(WAKE_SETTLE_SECONDS)
                startup = True

    def _dispatch(self, executor: ThreadPoolExecutor | None, function: Callable,
                  *args: Any) -> None:
        """Run ``function`` on ``executor``, or here when there is none (tests, shutdown)."""
        if executor is None:
            function(*args)
            return
        try:
            future = executor.submit(function, *args)
        except RuntimeError:  # shutting down: finish it here rather than drop it
            function(*args)
            return
        with self._futures_lock:
            self._futures.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        if not future.cancelled() and future.exception() is not None:
            LOGGER.warning("messenger_task_failed error=%s", _error_code(future.exception()))


# -- the process ---------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    """``python -m automotive.messenger_worker``: one process, no port. Everything comes from the
    environment and ``.env`` (``env`` replaces both, for tests). Refuses to start, naming the
    missing variables, when the worker's settings are incomplete; stops on SIGTERM or SIGINT
    after letting running turns finish."""
    parser = argparse.ArgumentParser(
        prog="python -m automotive.messenger_worker",
        description="Answer the Garage page's Messenger chats and run the Facebook syncs.")
    parser.parse_args(argv)
    try:
        settings = MetaSettings.from_env(env)
    except MetaConfigError as error:  # names the variable, never its value
        print(f"Messenger worker not started: {error}", file=sys.stderr)
        return 2
    missing = settings.missing_for_worker()
    if missing:
        print("Messenger worker not started. Set in .env: " + ", ".join(missing),
              file=sys.stderr)
        return 2
    from automotive.messenger_engine import build_engine, data_directory
    from automotive.meta_context import build_context

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = build_context(settings, settings.database_url, application_name="garage-messenger")
    try:
        ctx.store.settings()
    except Exception as error:
        print(f"Messenger worker not started: the database is not usable ({_error_code(error)})",
              file=sys.stderr)
        return 1
    folder = data_directory(settings)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        engine = build_engine(settings)
    except Exception as error:  # the class only: a settings error may quote a value
        print(f"Messenger worker not started: the engine could not be built "
              f"({_error_code(error)})", file=sys.stderr)
        return 1
    worker = MessengerWorker(ctx, engine, cost=estimated_cost(_model_name(engine)))
    LOGGER.info(
        "messenger_worker_starting app_id=%s graph=%s page_pin=%s public_url=%s data_dir=%s "
        "quiet_s=%s max_wait_s=%s turns=%d typing_s=%s send_timeout_s=%s inbox=listen+poll_%ss "
        "jobs=%s", settings.app_id, settings.graph_version, settings.page_id or "-",
        "set" if settings.public_url else "missing", folder, settings.quiet_seconds,
        settings.max_wait_seconds, worker.concurrent_turns, worker.typing_seconds,
        settings.send_timeout_seconds, worker.poll_seconds, ",".join(JOB_NAMES))
    stopping = threading.Event()

    def stop(signum: int, frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    try:
        while not stopping.wait(1.0):
            pass
    finally:
        LOGGER.info("messenger_worker_stopping")
        worker.stop()
        engine.close()
    LOGGER.info("messenger_worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
