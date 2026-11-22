"""SQL for the dashboard's Live chats page: alarms, the website chat for staff, and the feed.

Tables are migration 0015 (``garage.live_alerts``, ``live_web_chats``, ``live_web_messages``)
and the Messenger tables of 0013. Two logins use this class and every method names the one it
needs; the privileges are the fence, this module only stays inside them:

- the Messenger worker (``garage_messenger``) raises alarms from the bot's turns (``record_turn``);
- the dashboard and the website process (``garage_site``) do everything else: they read chats and
  alarms, mark alarms done, switch a bot off for one customer, and record the website chat.

An alarm never carries more than the customer's own words (cut to 200 characters), a short note the
model wrote for the staff, a phone number the customer typed, and why the bot found nothing
(``live_alerts.turn_detail``, migration 0017: the words it searched with and the catalogue's own
counts, bounded in size). Audit rows name a customer by the last four characters of their key.
Nothing here logs a message or a number.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from psycopg import errors
from psycopg.types.json import Jsonb

from automotive import shop_hours
from automotive.live_alerts import AlertDraft, TurnAlerts, text, visible
from automotive.meta_db import MetaDatabase
from automotive.meta_store import MetaConflict, MetaStore, bot_state, mask_psid

LOGGER = logging.getLogger(__name__)

KEY = re.compile(r"^(?:m-([0-9]{1,30})|w-([0-9a-f]{32}))$")
# What the Messenger adapter records for a turn that ended in an error (messenger_engine._cause).
CAUSE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,59}@[a-z]{1,20}")
STATUSES = ("open", "done")
FILTERS = ("all", "alerts", "waiting", "off", "stuck")
ITEMS_KEPT = 10  # earlier notes an alarm keeps when a repeat merges into it
ITEM_NOTE = 160  # characters of each earlier note
ITEMS_BYTES = 7000  # under the column's 8192-byte CHECK, which counts jsonb's own spacing
FEED_WINDOW = 400  # the page never reads further back than this many message ids
MESSAGE_OVERLAP = 30  # ids re-read on every poll: identity ids can commit out of order
FEED_LIMIT = 200
BODY_LIMIT = 4000
OFFERED_LIMIT = 8
PULSE_SECONDS = 1.5  # the counts every open dashboard asks for are computed once per this
READY_SECONDS = 300.0
NOT_READY_SECONDS = 30.0

# A Messenger customer waits for staff when their newest message is later than the newest answer
# (the bot's or the shop's), is younger than the reply window (the dashboard's
# max_reply_age_minutes, 24 hours by default: Meta's standard messaging window, after which the
# page cannot answer anyway), and nothing is going to answer it by itself: the bot is switched off
# for this chat, switched off or paused for everyone, in test mode and this is not a tester's chat,
# or it has said nothing for BOT_GRACE_SECONDS
# (a reply that failed or was cut off, a message too old for the bot, one the catch-up job found
# after the laptop slept). Constants only: no value ever reaches this text.
BOT_GRACE_SECONDS = 180
# Meta's window for a reply from the page: past it staff can no longer answer, so the chat stops
# waiting. Not the bot's max_reply_age_minutes, which only limits what the bot itself answers.
REPLY_WINDOW_HOURS = 24
_SETTING = "(SELECT {} FROM garage.meta_settings WHERE settings_id = 1)"
WAITING = (  # constants only (see above): no value ever reaches this text
    "(NOT t.is_admin AND t.last_customer_at IS NOT NULL "  # nosec B608
    "AND t.last_customer_at > GREATEST(COALESCE(t.last_shop_at, '-infinity'::timestamptz), "
    "COALESCE(t.last_bot_at, '-infinity'::timestamptz)) "
    f"AND t.last_customer_at > now() - make_interval(hours => {REPLY_WINDOW_HOURS}) "
    # Staff marked it handled (a closing "thanks" needs no answer), until a customer message is
    # stored after the newest one their screen showed: storage order, so also one sent earlier
    # but stored late (migration 0016; LiveStore.dismiss_waiting).
    "AND (t.waiting_dismissed_id IS NULL OR EXISTS (SELECT 1 FROM garage.messenger_messages d "
    "WHERE d.psid = t.psid AND d.sender = 'customer' AND d.message_id > t.waiting_dismissed_id)) "
    # The bot answers nobody, or (test mode, migration 0018) not this customer: staff at once.
    "AND (NOT t.bot_enabled OR NOT (COALESCE(" + _SETTING.format("bot_enabled") + ", FALSE) "
    "OR (COALESCE(" + _SETTING.format("bot_test_mode") + ", FALSE) AND t.bot_tester)) "
    "OR COALESCE(" + _SETTING.format("bot_paused_reason") + ", '') <> '' "
    f"OR t.last_customer_at < now() - make_interval(secs => {BOT_GRACE_SECONDS})))")
OPEN_ALERT = ("EXISTS (SELECT 1 FROM garage.live_alerts a WHERE a.channel = '{channel}' "
              "AND a.chat_key = {key} AND a.status = 'open')")
MESSENGER_CHATS = (
    # Constants only (the WAITING fragment above): no value ever reaches this text; every value
    # of a query that uses it is a bound parameter.
    "SELECT t.psid, t.customer_name, t.bot_enabled, t.version, t.needs_attention, "  # nosec B608
    "t.attention_reason, t.first_seen_at, t.source, last.sender AS last_sender, "
    "left(last.body, 120) AS last_body, "
    "COALESCE(last.sent_at, t.first_seen_at) AS last_at, "  # nosec B608
    # The newest stored message (storage order): what "No reply needed" says the screen showed.
    "(SELECT max(s.message_id) FROM garage.messenger_messages s WHERE s.psid = t.psid) "
    "AS last_id, "
    + WAITING + " AS waiting, "
    "COALESCE((SELECT array_agg(a.kind ORDER BY a.kind) FROM garage.live_alerts a "
    "WHERE a.channel = 'messenger' AND a.chat_key = t.psid AND a.status = 'open'), "
    "ARRAY[]::text[]) AS alerts, "
    # Why the bot could not answer a stuck chat: the newest reply that failed, as the worker
    # recorded it (the engine's status; Meta's or the engine's error text).
    "COALESCE(failed.engine_status, '') AS failed_engine, "
    "COALESCE(failed.error_text, '') AS failed_error "
    "FROM garage.messenger_threads t LEFT JOIN LATERAL (SELECT sender, body, sent_at "
    "FROM garage.messenger_messages m WHERE m.psid = t.psid "
    "ORDER BY m.sent_at DESC, m.message_id DESC LIMIT 1) last ON TRUE "
    "LEFT JOIN LATERAL (SELECT r.engine_status, r.error_text FROM garage.messenger_replies r "
    "WHERE t.needs_attention AND r.psid = t.psid AND r.channel = 'messenger' AND r.status IN "
    "('fallback_sent', 'failed', 'paused_provider', 'abandoned', 'blocked_by_meta') "
    "ORDER BY r.started_at DESC, r.reply_id DESC LIMIT 1) failed ON TRUE WHERE NOT t.is_admin")
MESSENGER_FILTERS = {
    "all": "",
    "alerts": " AND (t.needs_attention OR " + OPEN_ALERT.format(channel="messenger",
                                                                key="t.psid") + ")",
    "waiting": " AND " + WAITING,
    "off": " AND NOT t.bot_enabled",
    "stuck": " AND t.needs_attention",  # the bot could not answer properly (a worker flag)
}
WEB_CHATS = (
    "SELECT c.chat_key, c.car_label, c.language, c.bot_enabled, c.version, c.started_at, "
    "last.sender AS last_sender, left(last.body, 120) AS last_body, "
    "COALESCE(last.stored_at, c.last_message_at) AS last_at, "
    "COALESCE((SELECT array_agg(a.kind ORDER BY a.kind) FROM garage.live_alerts a "
    "WHERE a.channel = 'web' AND a.chat_key = c.chat_key AND a.status = 'open'), "
    "ARRAY[]::text[]) AS alerts "
    "FROM garage.live_web_chats c LEFT JOIN LATERAL (SELECT sender, body, stored_at "
    "FROM garage.live_web_messages m WHERE m.chat_key = c.chat_key "
    "ORDER BY m.message_id DESC LIMIT 1) last ON TRUE WHERE TRUE")
WEB_FILTERS = {
    "all": "",
    "alerts": " AND " + OPEN_ALERT.format(channel="web", key="c.chat_key"),
    "waiting": " AND FALSE",  # nobody can answer a website visitor from here
    "off": " AND NOT c.bot_enabled",
    "stuck": " AND FALSE",  # a website chat has no such flag
}
ALERT_COLUMNS = (
    "a.alert_id, a.channel, a.chat_key, a.kind, a.reason, a.status, a.summary, a.items, "
    "a.occurrences, a.car_label, a.phone, a.phone_at, a.created_at, a.updated_at, a.handled_by, "
    "a.handled_at, a.version, "
    # Why the bot found nothing (migration 0017); an empty object before it, or for an old alarm.
    "COALESCE(to_jsonb(a) -> 'detail', '{}'::jsonb) AS detail, "
    "COALESCE(t.customer_name, '') AS customer_name, "
    "COALESCE(t.bot_enabled, w.bot_enabled, TRUE) AS bot_enabled, "
    "COALESCE(t.version, w.version, 0) AS chat_version")
ALERT_JOINS = (
    "FROM garage.live_alerts a "
    "LEFT JOIN garage.messenger_threads t ON a.channel = 'messenger' AND t.psid = a.chat_key "
    "LEFT JOIN garage.live_web_chats w ON a.channel = 'web' AND w.chat_key = a.chat_key")


class LiveNotSetUp(RuntimeError):
    """Migration 0015 is not applied in this database."""


class AlreadyOpen(RuntimeError):
    """Reopening an alarm would make two open alarms of one chat and kind."""


class RaisedAlert(NamedTuple):
    alert_id: int
    created: bool
    changed: bool  # a new alarm, a new note or a new phone: staff should hear it again
    phone_added: bool
    version: int


class Levels(NamedTuple):
    """Which of the Live chats migrations this database has (``LiveStore._levels``)."""

    tables: bool  # 0015: alarms, website chats, the bot switches
    dismiss: bool  # 0016: "No reply needed" (the pages read it)
    detail: bool  # 0017: why the bot found nothing, kept with each alarm
    testers: bool  # 0018: the bot's test mode and tester chats (the pages read it, WAITING)


def web_key(conversation_id: str) -> str:
    """How the website chat's own log names a conversation (``ChatLogger.session_key``): the raw
    id is a bearer secret and is never stored."""
    return hashlib.sha256(conversation_id.encode()).hexdigest()[:32]


def parse_key(key: Any) -> tuple[str, str] | None:
    """('messenger', psid) or ('web', chat_key) for a key the dashboard sent, else None."""
    found = KEY.match(key) if isinstance(key, str) else None
    if found is None:
        return None
    return ("messenger", found.group(1)) if found.group(1) else ("web", found.group(2))


def _username(actor: Any) -> str:
    name = actor.get("username") if isinstance(actor, Mapping) else actor
    return str(name or "")[:32]


def _same(first: str, second: str) -> bool:
    return " ".join(first.split()).casefold() == " ".join(second.split()).casefold()


def _clean_body(value: Any) -> str:
    clean = str(value or "").replace("\x00", "")
    return "".join(char for char in clean if visible(char))[:BODY_LIMIT]


def _keep_detail(conn: Any, alert_id: int, detail: Jsonb | None) -> bool:
    """Store why the bot found nothing on an alarm (migration 0017), in a savepoint: a detail the
    database refuses is left out and never takes the alarm, or the rest of its turn, with it.
    False when nothing was stored."""
    if detail is None:
        return False
    try:
        with conn.transaction():
            conn.execute("UPDATE garage.live_alerts SET detail = %s WHERE alert_id = %s",
                         (detail, alert_id))
    except errors.Error as error:
        LOGGER.warning("live_alert_detail_skipped %s", type(error).__name__)
        return False
    return True


def _messenger_chat(row: Mapping[str, Any]) -> dict:
    psid = row["psid"]
    return {"key": f"m-{psid}", "channel": "messenger", "name": row["customer_name"] or "",
            "tail": psid[-4:], "car_label": "", "language": "", "bot_enabled": row["bot_enabled"],
            "version": row["version"], "needs_attention": row["needs_attention"],
            "attention_reason": row["attention_reason"] or "", "waiting": bool(row["waiting"]),
            "alerts": list(row["alerts"] or []), "last_sender": row["last_sender"] or "",
            "last_body": row["last_body"] or "", "last_at": row["last_at"],
            "last_id": row["last_id"],  # what "No reply needed" sends back (``seen_id``)
            "first_seen_at": row["first_seen_at"], "source": row["source"] or "",
            # What the stuck chat's newest failed reply recorded: why the bot could not answer.
            # Of its error text only the engine's own cause ("ValueError@model"): Meta's words
            # for a refused send stay in the admin's Facebook section.
            "failed_engine": row.get("failed_engine") or "",
            "failed_error": cause if CAUSE.fullmatch(cause := str(row.get("failed_error") or ""))
            else ""}


def _web_chat(row: Mapping[str, Any]) -> dict:
    key = row["chat_key"]
    return {"key": f"w-{key}", "channel": "web", "name": "", "tail": key[:4],
            "car_label": row["car_label"] or "", "language": row["language"] or "",
            "bot_enabled": row["bot_enabled"], "version": row["version"],
            "needs_attention": False, "attention_reason": "", "waiting": False,
            "alerts": list(row["alerts"] or []), "last_sender": row["last_sender"] or "",
            "last_body": row["last_body"] or "", "last_at": row["last_at"], "last_id": None,
            "first_seen_at": row["started_at"], "source": "", "failed_engine": "",
            "failed_error": ""}


def _messenger_message(row: Mapping[str, Any]) -> dict:
    # A bot reply is stamped just after the message it answers; stored_at is when it was sent.
    when = row["stored_at"] if row["sender"] == "bot" and row.get("stored_at") else row["sent_at"]
    return {"id": f"m{row['message_id']}", "key": f"m-{row['psid']}", "channel": "messenger",
            "sender": row["sender"], "body": row["body"] or "",
            "attachments": [item.get("type", "") for item in row.get("attachments") or []
                            if isinstance(item, Mapping)], "offered": [], "at": when}


def _web_message(row: Mapping[str, Any]) -> dict:
    return {"id": f"w{row['message_id']}", "key": f"w-{row['chat_key']}", "channel": "web",
            "sender": row["sender"], "body": row["body"] or "", "attachments": [],
            "offered": list(row["offered"] or []), "at": row["stored_at"]}


def _alert(row: Mapping[str, Any]) -> dict:
    channel, key = row["channel"], row["chat_key"]
    return {"alert_id": row["alert_id"], "channel": channel,
            "key": f"m-{key}" if channel == "messenger" else f"w-{key}",
            "tail": key[-4:] if channel == "messenger" else key[:4],
            "name": row["customer_name"] or "", "kind": row["kind"], "reason": row["reason"],
            "status": row["status"], "summary": row["summary"],
            "items": list(row["items"] or []), "occurrences": row["occurrences"],
            "car_label": row["car_label"], "phone": row["phone"], "phone_at": row["phone_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "handled_by": row["handled_by"], "handled_at": row["handled_at"],
            "version": row["version"], "bot_enabled": bool(row["bot_enabled"]),
            # The chat's own version (0 when the chat is gone), for the bot switch on a card.
            "chat_version": int(row["chat_version"] or 0),
            "quiet": quiet(row["kind"], row["updated_at"]),
            # Why the bot found nothing (``live_alerts.turn_detail``): {} for an alarm without it.
            "detail": row["detail"] if isinstance(row.get("detail"), dict) else {}}


def quiet(kind: str, updated_at: Any) -> bool:
    """A part the bot could not find, last raised or repeated outside the staff's hours: the
    page shows it without a sound, for the morning. Judged by the newest change, so a customer
    who asks again during the day makes it sound."""
    return kind == "part_not_found" and updated_at is not None and not shop_hours.is_open(
        updated_at)


class LiveStore:
    """Short transactions, one connection per call (``MetaDatabase``)."""

    def __init__(self, db: MetaDatabase, meta: MetaStore | None = None) -> None:
        self.db = db
        self.meta = meta or MetaStore(db)
        self._lock = threading.Lock()
        self._ready: tuple[Levels, float] | None = None
        self._pulse: tuple[float, dict] | None = None
        self._reachable = True  # what usable() last saw, so an outage is logged once

    # -- is the migration in place ---------------------------------------------------------------

    def ready(self) -> bool:
        """Whether migration 0015's tables exist: all that recording alarms, website chats and
        the bot switches needs. A yes is kept five minutes and a no thirty seconds, so a page
        asking every few seconds costs one query a minute. Raises when the database cannot be
        reached."""
        return self._levels().tables

    def pages_ready(self) -> bool:
        """``ready`` and the columns of migrations 0016 and 0018, which the Live pages read
        (``WAITING``): without them the pages say "not set up" instead of every query failing,
        while alarms and website chats are still recorded."""
        levels = self._levels()
        return levels.tables and levels.dismiss and levels.testers

    def details_ready(self) -> bool:
        """``ready`` and migration 0017's column: an alarm then keeps why the bot found nothing
        (``AlertDraft.detail``). Without it alarms are raised and read exactly as before, with
        no detail: the column is written only when it exists, so no alarm is lost to it."""
        levels = self._levels()
        return levels.tables and levels.detail

    def _levels(self) -> Levels:
        now = time.monotonic()
        with self._lock:
            if self._ready is not None and now < self._ready[1]:
                return self._ready[0]
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT to_regclass('garage.live_alerts') IS NOT NULL "
                "AND to_regclass('garage.live_web_chats') IS NOT NULL "
                "AND to_regclass('garage.live_web_messages') IS NOT NULL AS tables, "
                "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
                "'garage' AND table_name = 'messenger_threads' "
                "AND column_name = 'waiting_dismissed_id') AS dismiss, "
                "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
                "'garage' AND table_name = 'live_alerts' "
                "AND column_name = 'detail') AS detail, "
                "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
                "'garage' AND table_name = 'messenger_threads' "
                "AND column_name = 'bot_tester') AS testers").fetchone()
        levels = Levels(bool(row["tables"]), bool(row["dismiss"]), bool(row["detail"]),
                        bool(row["testers"]))
        with self._lock:
            self._ready = (levels, now + (READY_SECONDS if all(levels) else NOT_READY_SECONDS))
        return levels

    def usable(self) -> bool:
        """``ready`` for code that must never fail a customer's turn: False on any error. The
        log says when the database stops and starts answering, not on every call (the website
        recorder asks every few seconds)."""
        try:
            there = self.ready()
        except Exception as error:
            if self._reachable:
                LOGGER.warning("live_store_unavailable %s", type(error).__name__)
            self._reachable = False
            return False
        if not self._reachable:
            LOGGER.info("live_store_available")
        self._reachable = True
        return there

    @property
    def reachable(self) -> bool:
        """False while the last ``usable`` could not reach the database (not merely a database
        without migration 0015): the website recorder then waits and tries again."""
        return self._reachable

    def _changed(self) -> None:
        with self._lock:
            self._pulse = None

    # -- raising alarms (worker and website login) -----------------------------------------------

    def raise_alert(self, channel: str, chat_key: str, draft: AlertDraft, *,
                    allow_create: bool = True) -> RaisedAlert | None:
        """Record one alarm. With one already open for this chat and kind it merges: a new note
        is appended to the earlier ones (at most ``ITEMS_KEPT``), a new phone is stored, and
        ``changed`` says staff should hear it again. Saying the same thing twice changes nothing.
        A new alarm starts with the phone number the chat already gave. ``allow_create=False``
        only merges: None when nothing is open (the website's limits on new alarms)."""
        for attempt in (1, 2):
            try:
                return self._raise(channel, chat_key, draft, allow_create)
            except errors.UniqueViolation:
                if attempt == 2:  # a second writer won twice: the row exists, give up quietly
                    raise
        raise AssertionError("unreachable")

    def _raise(self, channel: str, chat_key: str, draft: AlertDraft,
               allow_create: bool) -> RaisedAlert | None:
        summary = text(draft.summary, 300) or "-"
        phone = draft.phone or None
        # Why the bot found nothing (``live_alerts.turn_detail``), kept once migration 0017 is
        # there; before it the alarm is raised exactly as it always was.
        detail = Jsonb(dict(draft.detail)) if draft.detail and self.details_ready() else None
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT alert_id, version, reason, summary, items, occurrences, phone, updated_at, "
                "to_jsonb(a) -> 'detail' AS detail "  # NULL before migration 0017
                "FROM garage.live_alerts a WHERE channel = %s AND chat_key = %s AND kind = %s "
                "AND status = 'open' FOR UPDATE", (channel, chat_key, draft.kind)).fetchone()
            if row is None:
                if not allow_create:
                    return None
                if phone is None:  # the number this chat already gave, on any earlier alarm
                    known = conn.execute(
                        "SELECT phone FROM garage.live_alerts WHERE channel = %s AND "
                        "chat_key = %s AND phone IS NOT NULL ORDER BY phone_at DESC NULLS LAST, "
                        "alert_id DESC LIMIT 1", (channel, chat_key)).fetchone()
                    phone = known["phone"] if known else None
                made = conn.execute(
                    "INSERT INTO garage.live_alerts (channel, chat_key, kind, reason, summary, "
                    "car_label, phone, phone_at) VALUES (%s, %s, %s, %s, %s, %s, %s::text, "
                    "CASE WHEN %s::text IS NULL THEN NULL ELSE now() END) "
                    "RETURNING alert_id, version",
                    (channel, chat_key, draft.kind, draft.reason, summary,
                     text(draft.car_label, 80), phone, phone)).fetchone()
                _keep_detail(conn, made["alert_id"], detail)
                self._changed()
                return RaisedAlert(made["alert_id"], True, True, bool(phone), made["version"])
            repeated = _same(row["summary"], summary)
            phone_new = bool(phone) and phone != row["phone"]
            # A failed turn never takes the place of what a person asked for: a handoff or a
            # phone number merged with a later failure stays a handoff (``close_failed`` closes
            # only alarms that are nothing but failures). A failure that turns into a handoff
            # does change the reason, even in the same words.
            reason = (row["reason"] if draft.reason == "failed" and row["reason"] not in
                      ("", "failed") else draft.reason)
            # A part not found that was saved quiet (outside the staff's hours) and is asked
            # for again while they work, even in the same words: its newest change moves into
            # the day, so the page sounds it.
            woke = quiet(draft.kind, row["updated_at"]) and shop_hours.is_open()
            # The newest turn's reason replaces the stored one, but a failure's never takes the
            # place of a handoff's (the alarm stays a handoff, see above).
            if detail is not None and draft.reason == "failed" and reason != "failed":
                detail = None
            if repeated and not phone_new and reason == row["reason"] and not woke:
                if detail is None or detail.obj == row["detail"]:
                    return RaisedAlert(row["alert_id"], False, False, False, row["version"])
                # The same request found nothing again, for other reasons: the card shows the new
                # ones and nothing sounds. Its newest change moves, so the page reads it, unless
                # that would make a loud part-not-found quiet (``quiet`` judges by the newest
                # change): one refreshed after hours keeps its time, and so its morning siren.
                if not _keep_detail(conn, row["alert_id"], detail):
                    return RaisedAlert(row["alert_id"], False, False, False, row["version"])
                keep_time = (draft.kind == "part_not_found" and not shop_hours.is_open()
                             and not quiet(draft.kind, row["updated_at"]))
                refreshed = conn.execute(
                    "UPDATE garage.live_alerts SET updated_at = CASE WHEN %s THEN updated_at "
                    "ELSE now() END, version = version + 1 WHERE alert_id = %s "
                    "RETURNING version", (keep_time, row["alert_id"])).fetchone()
                self._changed()
                return RaisedAlert(row["alert_id"], False, False, False, refreshed["version"])
            items = list(row["items"] or [])
            occurrences = row["occurrences"]
            kept = row["summary"]
            if not repeated:
                items.append({"at": row["updated_at"].isoformat(),
                              "note": text(row["summary"], ITEM_NOTE)})
                items = items[-ITEMS_KEPT:]
                # Whatever the notes' length, the column's size CHECK must never refuse a repeat
                # (it would take the same turn's visit request and phone number down with it).
                while len(items) > 1 and len(json.dumps(
                        items, ensure_ascii=False).encode()) > ITEMS_BYTES:
                    items.pop(0)
                occurrences += 1
                kept = summary
            made = conn.execute(
                "UPDATE garage.live_alerts SET reason = %s, summary = %s, items = %s, "
                "occurrences = %s, car_label = COALESCE(NULLIF(%s, ''), car_label), "
                "phone = COALESCE(%s::text, phone), "
                "phone_at = CASE WHEN %s THEN now() ELSE phone_at END, updated_at = now(), "
                "version = version + 1 WHERE alert_id = %s RETURNING version",
                (reason, kept, Jsonb(items), occurrences, text(draft.car_label, 80),
                 phone, phone_new, row["alert_id"])).fetchone()
            _keep_detail(conn, row["alert_id"], detail)
        self._changed()
        return RaisedAlert(row["alert_id"], False, True, phone_new, made["version"])

    def attach_phone(self, channel: str, chat_key: str, phone: str, words: str,
                     car_label: str = "", *, allow_create: bool = True) -> list[RaisedAlert]:
        """A phone number the customer typed goes onto the chat's open alarms (see ``_attach``).
        Tried once more when another writer raised the same alarm in the meantime."""
        for attempt in (1, 2):
            try:
                return self._attach(channel, chat_key, phone, words, car_label, allow_create)
            except errors.UniqueViolation:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def _attach(self, channel: str, chat_key: str, phone: str, words: str, car_label: str,
                allow_create: bool) -> list[RaisedAlert]:
        """A phone number the customer typed goes onto every open alarm of the chat that does not
        have it. When the chat has no open alarm and the number is new to it, the number gets an
        alarm of its own (needs_staff, reason phone): a customer who answers after staff marked
        everything done is not lost. A number an alarm already carried, done or not, is not
        raised again."""
        if not phone:
            return []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "UPDATE garage.live_alerts SET phone = %s, phone_at = now(), updated_at = now(), "
                "version = version + 1 WHERE channel = %s AND chat_key = %s AND status = 'open' "
                "AND phone IS DISTINCT FROM %s RETURNING alert_id, version",
                (phone, channel, chat_key, phone)).fetchall()
            raised = [RaisedAlert(row["alert_id"], False, True, True, row["version"])
                      for row in rows]
            if not raised:
                known = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM garage.live_alerts WHERE channel = %(c)s "
                    "AND chat_key = %(k)s AND (status = 'open' OR phone = %(p)s)) AS known",
                    {"c": channel, "k": chat_key, "p": phone}).fetchone()["known"]
                if not known and allow_create:
                    made = conn.execute(
                        "INSERT INTO garage.live_alerts (channel, chat_key, kind, reason, "
                        "summary, car_label, phone, phone_at) VALUES (%s, %s, 'needs_staff', "
                        "'phone', %s, %s, %s, now()) RETURNING alert_id, version",
                        (channel, chat_key, text(words, 300) or "-", text(car_label, 80),
                         phone)).fetchone()
                    raised.append(RaisedAlert(made["alert_id"], True, True, True,
                                              made["version"]))
        if raised:
            self._changed()
        return raised

    def record_turn(self, channel: str, chat_key: str, turn: TurnAlerts) -> list[RaisedAlert]:
        """Everything a finished turn tells the staff (``live_alerts.turn_alerts``). Nothing for
        the owner's own chat (testing the bot from their account): nobody could open its alarm."""
        if channel == "messenger" and self._admin_thread(chat_key):
            return []
        # Each alarm and the phone number are written on their own: one that fails must not
        # take the visit request and the number of the same turn down with it.
        raised: list[RaisedAlert] = []
        for draft in turn.drafts:
            try:
                made = self.raise_alert(channel, chat_key, draft)
            except Exception as error:
                LOGGER.warning("live_alert_failed kind=%s error=%s", draft.kind,
                               type(error).__name__)
                continue
            if made is not None:
                raised.append(made)
        if turn.phone:
            try:
                raised.extend(self.attach_phone(channel, chat_key, turn.phone, turn.message,
                                                turn.car_label))
            except Exception as error:
                LOGGER.warning("live_alert_failed kind=phone error=%s", type(error).__name__)
        return raised

    # -- reading alarms --------------------------------------------------------------------------

    def list_alerts(self, status: str = "open", limit: int = 100) -> list[dict]:
        """Alarms newest first: open ones by their last change, done ones by when they were done."""
        if status not in (*STATUSES, "all"):
            raise ValueError("Unknown alert status")
        where = "" if status == "all" else "WHERE a.status = %(status)s "
        order = ("a.handled_at DESC NULLS LAST, a.alert_id DESC" if status == "done"
                 else "a.updated_at DESC, a.alert_id DESC")
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {ALERT_COLUMNS} {ALERT_JOINS} {where}ORDER BY {order} LIMIT %(limit)s",
                {"status": status, "limit": max(1, min(int(limit), 300))}).fetchall()
        return [_alert(row) for row in rows]

    def recent_done(self, hours: int = 24, limit: int = 50) -> list[dict]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {ALERT_COLUMNS} {ALERT_JOINS} WHERE a.status = 'done' "
                "AND a.handled_at > now() - make_interval(hours => %s) "
                "ORDER BY a.handled_at DESC, a.alert_id DESC LIMIT %s",
                (int(hours), max(1, min(int(limit), 200)))).fetchall()
        return [_alert(row) for row in rows]

    def set_status(self, alert_id: int, status: str, actor: Any, *, version: int | None = None,
                   client_ip: str | None = None) -> dict | None:
        """Mark an alarm done (who and when are stored) or reopen it. None when it does not exist;
        ``MetaConflict`` when ``version`` is stale; ``AlreadyOpen`` when a reopened alarm would
        sit beside another open one of the same chat and kind. Doing what is already done is not
        an error."""
        if status not in STATUSES:
            raise ValueError("Unknown alert status")
        name = _username(actor)
        try:
            with self.db.transaction() as conn:
                current = conn.execute(
                    "SELECT alert_id, kind, status, version FROM garage.live_alerts "
                    "WHERE alert_id = %s FOR UPDATE", (alert_id,)).fetchone()
                if current is None:
                    return None
                # Two people marking the same alarm done (or a repeat merging into it while one
                # clicks) is not a conflict: the result is what both wanted.
                if (version is not None and current["version"] != version
                        and current["status"] != status):
                    raise MetaConflict("alert")
                if current["status"] != status:
                    if status == "done":
                        conn.execute(
                            "UPDATE garage.live_alerts SET status = 'done', handled_by = %s, "
                            "handled_at = now(), updated_at = now(), version = version + 1 "
                            "WHERE alert_id = %s", (name, alert_id))
                    else:
                        conn.execute(
                            "UPDATE garage.live_alerts SET status = 'open', handled_by = '', "
                            "handled_at = NULL, updated_at = now(), version = version + 1 "
                            "WHERE alert_id = %s", (alert_id,))
                    # Who closed or reopened it, and when: reopening clears handled_by.
                    self.meta.audit(actor, "live.alert_done" if status == "done"
                                    else "live.alert_open", f"alert-{alert_id}",
                                    {"kind": current["kind"], "version": current["version"] + 1},
                                    client_ip=client_ip, connection=conn)
                row = conn.execute(f"SELECT {ALERT_COLUMNS} {ALERT_JOINS} "
                                   "WHERE a.alert_id = %s", (alert_id,)).fetchone()
        except errors.UniqueViolation:
            raise AlreadyOpen("alert") from None
        self._changed()
        return _alert(row)

    # -- the pulse and the feed (dashboard login) ------------------------------------------------

    def pulse(self) -> dict:
        """What every open dashboard page asks for: open alarms per kind, the Messenger chats
        waiting for staff or stuck, the global bot state, the newest alarm change and the newest
        message ids. Computed once per ``PULSE_SECONDS`` however many pages ask."""
        now = time.monotonic()
        with self._lock:
            if self._pulse is not None and now - self._pulse[0] < PULSE_SECONDS:
                return dict(self._pulse[1])
        with self.db.transaction() as conn:
            kinds = conn.execute("SELECT kind, count(*) AS n FROM garage.live_alerts "
                                 "WHERE status = 'open' GROUP BY kind").fetchall()
            stamp = conn.execute("SELECT max(updated_at) AS stamp "
                                 "FROM garage.live_alerts").fetchone()["stamp"]
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM garage.messenger_threads t "  # nosec B608
                "WHERE t.needs_attention AND NOT t.is_admin) AS attention, "
                "(SELECT count(*) FROM garage.messenger_threads t WHERE " + WAITING +
                ") AS waiting, "  # nosec B608
                # The newest waiting message: a second message in a chat that was already
                # waiting moves it, so the page sounds again although the count did not move.
                "(SELECT max(t.last_customer_at) FROM garage.messenger_threads t WHERE "
                + WAITING + ") AS waiting_at, "  # nosec B608
                "(SELECT COALESCE(max(message_id), 0) FROM garage.messenger_messages) AS m, "
                "(SELECT COALESCE(max(message_id), 0) FROM garage.live_web_messages) AS w"
            ).fetchone()
            settings = conn.execute("SELECT bot_enabled, bot_test_mode, bot_paused_reason "
                                    "FROM garage.meta_settings WHERE settings_id = 1").fetchone()
        open_by_kind = {"part_not_found": 0, "visit_request": 0, "service_booking": 0,
                        "needs_staff": 0}
        open_by_kind.update({row["kind"]: int(row["n"]) for row in kinds})
        result = {"open": {**open_by_kind, "total": sum(open_by_kind.values())},
                  "waiting": int(counts["waiting"]), "waiting_at": counts["waiting_at"],
                  "attention": int(counts["attention"]),
                  # The staff's hours, by the server's clock: the siren stops at closing time.
                  "open_now": shop_hours.is_open(),
                  "messenger_bot": bot_state(settings) if settings else "unknown",
                  "stamp": stamp, "cursor": {"m": int(counts["m"]), "w": int(counts["w"])}}
        with self._lock:
            self._pulse = (now, result)
        return dict(result)

    def feed(self, after_m: int | None, after_w: int | None) -> dict:
        """The messages stored after the given ids, and the chats they belong to. A channel
        whose id is None is skipped (the page has no cursor for it yet). The last
        ``MESSAGE_OVERLAP`` ids are read again, because identity ids can commit out of order; the
        browser drops what it already shows. ``cursor`` is the newest id returned."""
        messages: list[dict] = []
        cursor = {"m": after_m, "w": after_w}
        messenger: set[str] = set()
        web: set[str] = set()
        with self.db.transaction() as conn:
            if after_m is not None:
                rows = conn.execute(
                    "SELECT m.message_id, m.psid, m.sender, m.body, m.attachments, m.sent_at, "
                    "m.stored_at FROM garage.messenger_messages m "
                    "JOIN garage.messenger_threads t ON t.psid = m.psid "
                    "WHERE m.message_id > GREATEST(%s, (SELECT COALESCE(max(message_id), 0) "
                    "FROM garage.messenger_messages) - %s) AND NOT t.is_admin "
                    "ORDER BY m.message_id LIMIT %s",
                    (max(after_m - MESSAGE_OVERLAP, 0), FEED_WINDOW, FEED_LIMIT)).fetchall()
                messages += [_messenger_message(row) for row in rows]
                messenger = {row["psid"] for row in rows}
                if rows:
                    cursor["m"] = max(after_m, rows[-1]["message_id"])
            if after_w is not None:
                rows = conn.execute(
                    "SELECT message_id, chat_key, sender, body, offered, stored_at "
                    "FROM garage.live_web_messages WHERE message_id > GREATEST(%s, "
                    "(SELECT COALESCE(max(message_id), 0) FROM garage.live_web_messages) - %s) "
                    "ORDER BY message_id LIMIT %s",
                    (max(after_w - MESSAGE_OVERLAP, 0), FEED_WINDOW, FEED_LIMIT)).fetchall()
                messages += [_web_message(row) for row in rows]
                web = {row["chat_key"] for row in rows}
                if rows:
                    cursor["w"] = max(after_w, rows[-1]["message_id"])
            chats = self._summaries(conn, messenger, web)
        messages.sort(key=lambda item: (item["at"], item["id"]))
        return {"messages": messages, "chats": chats, "cursor": cursor}

    def _summaries(self, conn: Any, messenger: set[str], web: set[str]) -> dict[str, dict]:
        chats: dict[str, dict] = {}
        if messenger:
            for row in conn.execute(MESSENGER_CHATS + " AND t.psid = ANY(%s)",
                                    (sorted(messenger),)).fetchall():
                chats[f"m-{row['psid']}"] = _messenger_chat(row)
        if web:
            for row in conn.execute(WEB_CHATS + " AND c.chat_key = ANY(%s)",
                                    (sorted(web),)).fetchall():
                chats[f"w-{row['chat_key']}"] = _web_chat(row)
        return chats

    def chats(self, filter: str = "all", limit: int = 40) -> list[dict]:
        """Both channels' conversations, the most recently active first."""
        if filter not in FILTERS:
            raise ValueError("Unknown chat filter")
        size = max(1, min(int(limit), 150))
        with self.db.transaction() as conn:
            rows = [_messenger_chat(row) for row in conn.execute(
                MESSENGER_CHATS + MESSENGER_FILTERS[filter] +
                " ORDER BY COALESCE(last.sent_at, t.first_seen_at) DESC, t.psid LIMIT %s",
                (size,)).fetchall()]
            rows += [_web_chat(row) for row in conn.execute(
                WEB_CHATS + WEB_FILTERS[filter] +
                " ORDER BY COALESCE(last.stored_at, c.last_message_at) DESC, c.chat_key LIMIT %s",
                (size,)).fetchall()]
        rows.sort(key=lambda chat: chat["last_at"], reverse=True)
        return rows[:size]

    def chat(self, key: str, limit: int = 50, before: str | None = None) -> dict | None:
        """One conversation: its summary, its alarms, and its newest messages oldest first.
        ``before`` is the id of the oldest message the page shows (``m123`` or ``w45``)."""
        parsed = parse_key(key)
        if parsed is None:
            return None
        channel, chat_key = parsed
        size = max(1, min(int(limit), 200))
        anchor = None
        if before and re.fullmatch(r"[mw][0-9]{1,18}", before) and before[0] == key[0]:
            anchor = int(before[1:])
        with self.db.transaction() as conn:
            if channel == "messenger":
                row = conn.execute(MESSENGER_CHATS + " AND t.psid = %s", (chat_key,)).fetchone()
                if row is None:
                    return None
                summary = _messenger_chat(row)
            else:
                row = conn.execute(WEB_CHATS + " AND c.chat_key = %s", (chat_key,)).fetchone()
                if row is None:
                    return None
                summary = _web_chat(row)
            alerts = conn.execute(
                f"SELECT {ALERT_COLUMNS} {ALERT_JOINS} WHERE a.channel = %s AND a.chat_key = %s "
                "AND (a.status = 'open' OR a.handled_at > now() - interval '24 hours') "
                "ORDER BY (a.status = 'open') DESC, a.updated_at DESC, a.alert_id DESC LIMIT 20",
                (channel, chat_key)).fetchall()
        if channel == "messenger":
            rows = self.meta.messages(chat_key, size + 1, before_id=anchor)
            messages = [_messenger_message(item) for item in rows[-size:]]
            older = len(rows) > size
        else:
            with self.db.transaction() as conn:
                rows = conn.execute(
                    "SELECT message_id, chat_key, sender, body, offered, stored_at "
                    "FROM garage.live_web_messages WHERE chat_key = %s "
                    "AND (%s::bigint IS NULL OR message_id < %s::bigint) "
                    "ORDER BY message_id DESC LIMIT %s",
                    (chat_key, anchor, anchor, size + 1)).fetchall()
            rows = rows[::-1]
            messages = [_web_message(item) for item in rows[-size:]]
            older = len(rows) > size
        return {"chat": summary, "alerts": [_alert(item) for item in alerts],
                "messages": messages, "has_older": older}

    # -- switches (dashboard login) --------------------------------------------------------------

    def set_bot(self, key: str, enabled: bool, actor: Any, *, version: int | None = None,
                client_ip: str | None = None) -> dict | None:
        """The bot's switch for one customer. Messenger: the chat's own switch the worker already
        honours before every send. Website: the visitor's flag, read before each of their turns.
        None when the chat does not exist; ``MetaConflict`` for a stale ``version``."""
        parsed = parse_key(key)
        if parsed is None:
            return None
        channel, chat_key = parsed
        if channel == "messenger":
            if not self._plain_thread(chat_key):  # the owner's own chat is not listed either
                return None
            row = self.meta.set_thread_switch(chat_key, bool(enabled), actor,
                                              expected_version=version, client_ip=client_ip)
            self._changed()
            return None if row is None else {"key": key, "bot_enabled": row["bot_enabled"],
                                             "version": row["version"]}
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.live_web_chats SET bot_enabled = %s, muted_by = %s, "
                "muted_at = CASE WHEN %s THEN NULL ELSE now() END, version = version + 1, "
                "updated_at = now() WHERE chat_key = %s "
                "AND (%s::int IS NULL OR version = %s::int) RETURNING bot_enabled, version",
                (bool(enabled), "" if enabled else _username(actor), bool(enabled), chat_key,
                 version, version)).fetchone()
            if row is None:
                if version is not None and conn.execute(
                        "SELECT 1 FROM garage.live_web_chats WHERE chat_key = %s",
                        (chat_key,)).fetchone():
                    raise MetaConflict("chat")
                return None
            self.meta.audit(actor, "live.web_chat_on" if enabled else "live.web_chat_off",
                            "w-" + chat_key[:8], {"version": row["version"]},
                            client_ip=client_ip, connection=conn)
        self._changed()
        return {"key": key, "bot_enabled": row["bot_enabled"], "version": row["version"]}

    def dismiss_waiting(self, key: str, actor: Any, *, seen_id: int | None = None,
                        client_ip: str | None = None) -> dict | None:
        """Staff mark a waiting Messenger chat as handled without writing to the customer (a
        closing "thanks" needs no answer). The mark is ``seen_id``, the newest stored message
        their screen showed (a chat's ``last_id``), or else the newest stored now: storage
        order, not send time. Any customer message stored after it keeps or makes the chat
        waiting, whatever its send time: one that came in before the screen caught up (then the
        answer is {"waiting": True}) or one stored late by the catch-up job or Meta's retries.
        Never the owner's own chat; audited. None when there is no such chat (or a website key:
        a visitor never waits)."""
        parsed = parse_key(key)
        if parsed is None or parsed[0] != "messenger":
            return None
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.messenger_threads t SET waiting_dismissed_id = LEAST("
                "COALESCE((SELECT max(m.message_id) FROM garage.messenger_messages m "
                "WHERE m.psid = t.psid), 0), COALESCE(%s::bigint, 9223372036854775807)), "
                "updated_at = now() WHERE t.psid = %s AND NOT t.is_admin "
                "RETURNING EXISTS (SELECT 1 FROM garage.messenger_messages d "
                "WHERE d.psid = t.psid AND d.sender = 'customer' "
                "AND d.message_id > t.waiting_dismissed_id) AS newer",
                (seen_id, parsed[1])).fetchone()
            if row is None:
                return None
            self.meta.audit(actor, "live.waiting_dismissed", mask_psid(parsed[1]),
                            {"newer": True} if row["newer"] else {},
                            client_ip=client_ip, connection=conn)
        self._changed()
        return {"waiting": bool(row["newer"])}

    def clear_attention(self, key: str, actor: Any, *, client_ip: str | None = None) -> bool:
        """Clear a Messenger chat's "needs attention" flag (there is no automatic clearing)."""
        parsed = parse_key(key)
        if parsed is None or parsed[0] != "messenger":
            return False
        if not self._plain_thread(parsed[1]):
            return False
        done = self.meta.flag_attention(parsed[1], "", needs=False, actor=actor,
                                        client_ip=client_ip)
        self._changed()
        return done

    def _plain_thread(self, psid: str) -> bool:
        """Whether this is a customer's chat, as the page lists chats (never the owner's own)."""
        with self.db.transaction() as conn:
            return conn.execute("SELECT 1 FROM garage.messenger_threads WHERE psid = %s "
                                "AND NOT is_admin", (psid,)).fetchone() is not None

    def _admin_thread(self, psid: str) -> bool:
        """Whether this chat is marked as the owner's own (``is_admin``)."""
        with self.db.transaction() as conn:
            return conn.execute("SELECT 1 FROM garage.messenger_threads WHERE psid = %s "
                                "AND is_admin", (psid,)).fetchone() is not None

    # -- the website chat (website login) --------------------------------------------------------

    def web_customer(self, chat_key: str, body: str, language: str = "") -> int:
        """Store what a website visitor wrote, the moment they wrote it."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO garage.live_web_chats (chat_key, language, last_customer_at, "
                "last_message_at) VALUES (%s, %s, now(), now()) ON CONFLICT (chat_key) DO UPDATE "
                "SET last_customer_at = now(), last_message_at = now(), "
                "language = COALESCE(NULLIF(EXCLUDED.language, ''), live_web_chats.language), "
                "updated_at = now()", (chat_key, language if language in ("ka", "en") else ""))
            row = conn.execute(
                "INSERT INTO garage.live_web_messages (chat_key, sender, body) "
                "VALUES (%s, 'customer', %s) RETURNING message_id",
                (chat_key, _clean_body(body))).fetchone()
        return row["message_id"]

    def web_bot(self, chat_key: str, body: str, *, offered: Sequence[Mapping[str, Any]] = (),
                outcome: str = "", car_label: str = "", language: str = "") -> int:
        """Store what the bot answered a website visitor, with what the visitor saw offered."""
        shown = [dict(item) for item in offered][:OFFERED_LIMIT]
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO garage.live_web_chats (chat_key, language, car_label, "
                "last_bot_at, last_message_at) VALUES (%s, %s, %s, now(), now()) "
                "ON CONFLICT (chat_key) DO UPDATE SET last_bot_at = now(), "
                "last_message_at = now(), "
                "language = COALESCE(NULLIF(EXCLUDED.language, ''), live_web_chats.language), "
                "car_label = COALESCE(NULLIF(EXCLUDED.car_label, ''), live_web_chats.car_label), "
                "updated_at = now()",
                (chat_key, language if language in ("ka", "en") else "", text(car_label, 80)))
            row = conn.execute(
                "INSERT INTO garage.live_web_messages (chat_key, sender, body, offered, outcome) "
                "VALUES (%s, 'bot', %s, %s, %s) RETURNING message_id",
                (chat_key, _clean_body(body), Jsonb(shown), text(outcome, 20))).fetchone()
        return row["message_id"]

    def web_bot_enabled(self, chat_key: str) -> bool | None:
        """The visitor's switch; None for a visitor staff have not seen yet (the bot is on)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT bot_enabled FROM garage.live_web_chats "
                               "WHERE chat_key = %s", (chat_key,)).fetchone()
        return None if row is None else bool(row["bot_enabled"])

    def open_web_alerts(self) -> int:
        with self.db.transaction() as conn:
            return int(conn.execute("SELECT count(*) AS n FROM garage.live_alerts "
                                    "WHERE channel = 'web' AND status = 'open'").fetchone()["n"])

    def open_web_alert_counts(self) -> dict:
        """Open website alarms in two groups: ``noisy`` (a part not found, a failed turn: what a
        visitor or an outage can produce in numbers) and ``valuable`` (a visit or service request,
        a handoff, a phone number). Each has its own room on the board."""
        noisy = "(kind = 'part_not_found' OR (kind = 'needs_staff' AND reason = 'failed'))"
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT count(*) FILTER (WHERE {noisy}) AS noisy, "  # nosec B608
                f"count(*) FILTER (WHERE NOT {noisy}) AS valuable "
                "FROM garage.live_alerts WHERE channel = 'web' AND status = 'open'").fetchone()
        return {"noisy": int(row["noisy"]), "valuable": int(row["valuable"])}

    def close_failed(self, channel: str, chat_key: str) -> int:
        """A visitor whose bot answered again no longer has a failed turn to look at: their open
        "failed" alarm is closed by the system. Without it an outage's alarms would stay open
        until staff closed each one by hand."""
        with self.db.transaction() as conn:
            closed = conn.execute(
                "UPDATE garage.live_alerts SET status = 'done', handled_by = 'system', "
                "handled_at = now(), updated_at = now(), version = version + 1 "
                "WHERE channel = %s AND chat_key = %s AND kind = 'needs_staff' "
                "AND reason = 'failed' AND status = 'open'", (channel, chat_key)).rowcount
        if closed:
            self._changed()
        return closed

    def close_stale_failed(self, hours: int = 24) -> int:
        """Website "failed" alarms of visitors who never came back are closed by the system once
        nothing happened on them for ``hours``: a visitor's conversation is forgotten after ten
        idle minutes, so nobody can answer them, and left open they would fill the board's room
        for new alarms after an outage. Alarms that are more than failures (a handoff, a phone
        number) keep their own reason and are never closed here."""
        with self.db.transaction() as conn:
            closed = conn.execute(
                "UPDATE garage.live_alerts SET status = 'done', handled_by = 'system', "
                "handled_at = now(), updated_at = now(), version = version + 1 "
                "WHERE channel = 'web' AND kind = 'needs_staff' AND reason = 'failed' "
                "AND status = 'open' AND updated_at < now() - make_interval(hours => %s)",
                (max(1, int(hours)),)).rowcount
        if closed:
            self._changed()
        return closed

    def web_muted_keys(self) -> set[str]:
        """The website chats whose bot staff switched off. The recorder reads them every few
        seconds on its own thread, so a visitor's request never waits for the database."""
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT chat_key FROM garage.live_web_chats "
                                "WHERE NOT bot_enabled").fetchall()
        return {row["chat_key"] for row in rows}

    def web_prune(self, days: int) -> dict:
        """Delete the website alarms older than ``days`` (the website chat log's retention).

        The website chats and their messages are kept for good (owner's decision, 2026-11-20).
        A chat is never deleted here either: its messages would go with it (ON DELETE CASCADE).
        """
        days = max(1, int(days))
        with self.db.transaction() as conn:
            return {
                "alerts": conn.execute(
                    "DELETE FROM garage.live_alerts WHERE channel = 'web' AND created_at < "
                    "now() - make_interval(days => %s)", (days,)).rowcount}
