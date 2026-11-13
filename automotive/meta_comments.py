"""Comments on the Garage page's posts and ads: kept for the dashboard, answered in private.

A customer who comments under a post is usually asking the shop something. While the comment
switch is on, the bot answers such a comment the way TNLU does: one engine turn on the comment,
with a short excerpt of the post it was written under for context, sent as a Messenger *private
reply* (``recipient.comment_id``: Meta finds the commenter, and Meta's messaging gate does not
apply). Meta allows one private reply per comment, within 7 days of it. The Send API answers
with the commenter's page-scoped id, and the chat is stored then with the turn's engine state, so
when the customer writes back on Messenger the bot already knows what they asked. An optional
public reply (the dashboard's ``comment_public_reply``) goes under the comment once the private
reply is out.

The engine reads the words; this module only moves data and enforces the rules:
- only a new comment ('add') that is not the page's own (staff replies, our public replies), at
  most 7 days old, written after the comment switch went on (no backlog) and with text;
- the switch is read again right before the private reply leaves; off by then means nothing is
  sent (``skipped_off``); a provider pause of the bot stops comment replies too;
- a commenter whose Messenger chat the admin switched off is left to the admin
  (``skipped_chat_off``), checked before the turn and again before the reply leaves. The chat is
  known only once an earlier private reply linked it: Meta names it in its answer to one;
- one attempt per comment: ``messenger_replies`` is unique on (channel 'comment', comment id),
  so a redelivered or replayed event is never answered twice;
- comment turns count against the shop's daily turn cap;
- when the AI provider's account fails (billing, key, access) the bot pauses itself and nothing
  is sent; any other failure sends the bot's short fallback with the shop phone privately, posts
  nothing public, and flags the chat;
- comments read by the 30-day backfill (``sync_comments``) are for the dashboard only and are
  never answered.

Edits and hides update the stored row; a removed comment's row is deleted (its author took it
down). A customer's data deletion (``MetaStore.delete_customer``) deletes their comments and
keeps their ids, hashed, in ``garage.meta_forgotten``: what they wrote before it is never stored
again, by a webhook or the backfill. The dashboard helpers at the end (the list, hide/unhide, a
public reply the admin types) run with the site login, act only on comments stored here, and are
audited. Logs carry event names and codes, never a comment's text or its author.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from automotive.chat import _redact
from automotive.messenger_engine import TURN_SECONDS, compact, record_shop_reply
from automotive.messenger_events import CommentEvent
from automotive.messenger_render import (
    TEXT_LIMIT,
    TEXTS,
    card_line,
    conflicting_vins_text,
    fallback_text,
    late_apology,
    split_text,
    text_language,
)
from automotive.meta_context import NotConnected
from automotive.meta_crypto import TokenCipherError
from automotive.meta_graph import GraphError
from automotive.meta_store import forget_comments, forgotten_since
from automotive.meta_sync import (
    DIGITS,
    SyncCount,
    clip,
    count,
    database_of,
    json_ready,
    meta_time,
    object_id,
    safe_link,
)

LOGGER = logging.getLogger(__name__)

# Meta's limit for a private reply, and so the age at which a comment is no longer answered.
REPLY_WINDOW = timedelta(days=7)
BACKFILL_DAYS = 30
LATE_AFTER = timedelta(hours=1)  # older comments get the apology for the delay
CARD_LINES = 3
COMMENT_TEXT_LIMIT = 1400  # the engine reads 2,000 characters: the comment, then the post
POST_EXCERPT_LIMIT = 400
PUBLIC_REPLY_LIMIT = 2000
READY_WAIT_SECONDS = 45.0
BACKFILL_POSTS = 100
BACKFILL_PAGES = 3
POST_LABEL = ("Context: this message is a comment under the shop's own Facebook post. The "
              "post's text, written by the shop and not by the customer:")
COMMENT_FIELDS = "id,message,created_time,from{id,name},parent{id},is_hidden,permalink_url"
# The AI provider's account refuses every request: the bot pauses instead of answering.
ACCOUNT_CODES = frozenset({"PROVIDER_BILLING", "AUTHENTICATION_ERROR", "ACCESS_DENIED"})
# A reply attempt was made (or is running): the comment is never tried again.
HANDLED = frozenset({"pending", "sent", "fallback_sent", "failed", "blocked_by_meta",
                     "paused_provider", "skipped_cap", "abandoned"})
EDIT_VERBS = frozenset({"edited", "edit", "update"})
REMOVE_VERBS = frozenset({"remove", "delete"})
ACTION_CODES = frozenset({"NOT_CONNECTED", "NOT_FOUND", "INVALID", "META_REFUSED",
                          "TOKEN_DEAD", "NETWORK"})


class CommentRetry(RuntimeError):
    """Nothing was claimed or sent, and the comment can be handled later (the engine's
    catalogue is still loading): the worker puts the inbox item back (``fail_inbox``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CommentActionError(RuntimeError):
    """A dashboard action on a comment failed. ``code`` is one of ACTION_CODES; ``detail``
    holds Meta's codes and scrubbed message (``{"meta": {...}}``), never a token."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        if code not in ACTION_CODES:
            raise ValueError("Unknown comment action error code")
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})


# -- storage -------------------------------------------------------------------------------------


class CommentStore:
    """This module's SQL on one login's database. [W] needs the worker login
    (``garage_messenger``), [S] the dashboard's (``garage_site``), [B] works with both."""

    def __init__(self, db: Any) -> None:
        self.db = database_of(db)

    _SAVE = (
        "INSERT INTO garage.meta_comments (comment_id, post_id, parent_id, author_id, "
        "author_name, body, created_time, is_hidden, permalink, source) VALUES "
        "(%(comment_id)s, %(post_id)s, %(parent_id)s, %(author_id)s, %(author_name)s, %(body)s, "
        "%(created_time)s, COALESCE(%(hidden)s::boolean, FALSE), %(permalink)s, %(source)s) "
        "ON CONFLICT (comment_id) DO UPDATE SET "
        "parent_id = CASE WHEN EXCLUDED.parent_id <> '' THEN EXCLUDED.parent_id "
        "ELSE garage.meta_comments.parent_id END, "
        "author_id = CASE WHEN EXCLUDED.author_id <> '' THEN EXCLUDED.author_id "
        "ELSE garage.meta_comments.author_id END, "
        "author_name = CASE WHEN EXCLUDED.author_name <> '' THEN EXCLUDED.author_name "
        "ELSE garage.meta_comments.author_name END, body = EXCLUDED.body, "
        "is_hidden = COALESCE(%(hidden)s::boolean, garage.meta_comments.is_hidden), "
        "permalink = CASE WHEN EXCLUDED.permalink <> '' THEN EXCLUDED.permalink "
        "ELSE garage.meta_comments.permalink END, "
        "source = CASE WHEN EXCLUDED.source = 'webhook' THEN 'webhook' "
        "ELSE garage.meta_comments.source END, updated_at = now() "
        "RETURNING comment_id, reply_status, replied_psid, public_reply_id, source")

    def save(self, comment: Mapping[str, Any], source: str) -> dict | None:
        """[W] Insert or refresh one comment; its reply fields come back, or None when it is
        not stored because its author or the comment itself was forgotten (a customer's data
        deletion) after it was written. A webhook copy marks the row 'webhook'; a backfill copy
        never changes the source or the reply fields."""
        return self.save_many([comment], source)[0]

    def save_many(self, comments: Iterable[Mapping[str, Any]],
                  source: str) -> list[dict | None]:
        """[W] ``save`` for several comments in one transaction (None for each forgotten)."""
        if source not in ("webhook", "backfill"):
            raise ValueError("A comment's source is webhook or backfill")
        batch = list(comments)
        rows: list[dict | None] = []
        with self.db.transaction() as conn:
            forgotten = forgotten_since(conn, {subject for comment in batch
                                               for subject in _subjects(comment)})
            for comment in batch:
                if any(forgotten.get(subject) is not None
                       and comment["created_time"] <= forgotten[subject]
                       for subject in _subjects(comment)):
                    rows.append(None)
                    continue
                rows.append(conn.execute(self._SAVE, {**comment, "source": source}).fetchone())
        return rows

    def get(self, comment_id: str) -> dict | None:
        """[B]"""
        with self.db.transaction() as conn:
            return conn.execute(
                "SELECT comment_id, post_id, parent_id, author_name, body, created_time, "
                "from_page, is_hidden, permalink, source, reply_status, replied_psid, "
                "public_reply_id FROM garage.meta_comments WHERE comment_id = %s",
                (comment_id,)).fetchone()

    def is_our_public_reply(self, comment_id: str) -> bool:
        """[B] Whether ``comment_id`` is a public reply the page posted under a stored comment
        (its own webhook says 'add', and it must never be answered)."""
        with self.db.transaction() as conn:
            return conn.execute("SELECT 1 FROM garage.meta_comments WHERE public_reply_id = %s "
                                "LIMIT 1", (comment_id,)).fetchone() is not None

    def link_page_reply(self, parent_id: str, comment_id: str) -> bool:
        """[B] Record the page's own public reply under a stored comment (typed by staff on
        Facebook), unless one is already recorded."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments SET public_reply_id = %s, updated_at = now() "
                "WHERE comment_id = %s AND public_reply_id = '' RETURNING comment_id",
                (comment_id[:80], parent_id)).fetchone() is not None

    def set_status(self, comment_id: str, status: str, *, force: bool = False,
                   replied_psid: str | None = None) -> bool:
        """[W] The bot's reply outcome. Without ``force`` only an empty or skipped status is
        replaced, so a late skip never hides a reply that was made."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments SET reply_status = %(status)s, "
                "replied_psid = COALESCE(%(psid)s::text, replied_psid), updated_at = now() "
                "WHERE comment_id = %(id)s AND (%(force)s OR reply_status = '' "
                "OR starts_with(reply_status, 'skipped_')) RETURNING comment_id",
                {"status": status[:40], "psid": replied_psid, "id": comment_id,
                 "force": bool(force)}).fetchone() is not None

    def edit(self, comment_id: str, body: str, hidden: bool | None) -> bool:
        """[W]"""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments SET body = %s, "
                "is_hidden = COALESCE(%s::boolean, is_hidden), updated_at = now() "
                "WHERE comment_id = %s RETURNING comment_id",
                (clip(body, 4000), hidden, comment_id)).fetchone() is not None

    def set_hidden(self, comment_id: str, hidden: bool) -> bool:
        """[B]"""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments SET is_hidden = %s, updated_at = now() "
                "WHERE comment_id = %s RETURNING comment_id",
                (bool(hidden), comment_id)).fetchone() is not None

    def set_public_reply(self, comment_id: str, reply_id: str) -> bool:
        """[B]"""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments SET public_reply_id = %s, updated_at = now() "
                "WHERE comment_id = %s RETURNING comment_id",
                (reply_id[:80], comment_id)).fetchone() is not None

    def remove(self, comment_id: str) -> bool:
        """[W] Delete a comment its author removed, and forget it as anyone's public reply."""
        with self.db.transaction() as conn:
            gone = conn.execute("DELETE FROM garage.meta_comments WHERE comment_id = %s",
                                (comment_id,)).rowcount
            cleared = conn.execute(
                "UPDATE garage.meta_comments SET public_reply_id = '', updated_at = now() "
                "WHERE public_reply_id = %s", (comment_id,)).rowcount
        return bool(gone or cleared)

    def post_text(self, post_id: str) -> str | None:
        """[B] A stored post's text; None when the post is not stored."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT message FROM garage.meta_posts WHERE post_id = %s",
                               (post_id,)).fetchone()
        return None if row is None else row["message"]

    def save_post(self, post_id: str, created_time: datetime, message: str,
                  permalink: str) -> None:
        """[W] Keep a post read for a comment, so the next comment under it reads no Graph."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO garage.meta_posts (post_id, created_time, message, permalink) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (post_id) DO NOTHING",
                (post_id, created_time, clip(message, 2000), permalink))

    def chat_switched_off(self, author_id: str) -> bool:
        """[B] Whether this commenter's Messenger chat is known (an earlier private reply
        linked it) and its switch is off: the admin took that customer over. Unknown chats
        are False; Meta names the chat only in the answer to a private reply."""
        if not author_id:
            return False
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT t.bot_enabled FROM garage.meta_comments c "
                "JOIN garage.messenger_threads t ON t.psid = c.replied_psid "
                "WHERE c.author_id = %s AND c.replied_psid <> '' "
                "ORDER BY c.updated_at DESC LIMIT 1", (author_id,)).fetchone()
        return row is not None and not row["bot_enabled"]

    def thread(self, psid: str) -> dict | None:
        """[W] A chat's name, source and engine state."""
        with self.db.transaction() as conn:
            return conn.execute(
                "SELECT customer_name, source, engine_state, engine_state_at "
                "FROM garage.messenger_threads WHERE psid = %s", (psid,)).fetchone()

    def claim_source(self, psid: str, source: str) -> bool:
        """[W] Name where a chat came from, only when nothing else has."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.messenger_threads SET source = %s, updated_at = now() "
                "WHERE psid = %s AND source = '' RETURNING psid", (source, psid)).fetchone() \
                is not None

    def save_state(self, psid: str, state: Mapping[str, Any],
                   expected_at: datetime | None) -> bool:
        """[W] Store a chat's engine state unless another turn stored one since it was read
        (``expected_at`` is the ``engine_state_at`` read then): a Messenger turn that finished
        meanwhile wins over the comment's."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.messenger_threads SET engine_state = %s, engine_state_at = now(), "
                "updated_at = now() WHERE psid = %s "
                "AND engine_state_at IS NOT DISTINCT FROM %s RETURNING psid",
                (Jsonb(compact(dict(state))), psid, expected_at)).fetchone() is not None

    def link_reply(self, reply_id: int, psid: str) -> bool:
        """[W] Name the customer of a comment's reply attempt once Meta told us who it was, so
        the per-customer counts and the customer's deletion include it."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.messenger_replies SET psid = %s WHERE reply_id = %s "
                "AND psid IS NULL RETURNING reply_id", (psid, int(reply_id))).fetchone() \
                is not None

    def close_pending(self, reply_id: int, comment_id: str, code: str) -> None:
        """[W] An attempt that raised midway ends as failed, never as a 'pending' forever."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE garage.messenger_replies SET status = 'failed', finished_at = now(), "
                "error_text = %s WHERE reply_id = %s AND status = 'pending'",
                (str(code)[:300], int(reply_id)))
            conn.execute(
                "UPDATE garage.meta_comments SET reply_status = 'failed', updated_at = now() "
                "WHERE comment_id = %s AND reply_status = 'pending'", (comment_id,))

    def ad_story_ids(self, page_id: str, limit: int = 50) -> list[str]:
        """[B] The posts behind the page's synced ads: their comments are leads too."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT story_id FROM garage.meta_ads "
                "WHERE starts_with(story_id, %s) ORDER BY story_id DESC LIMIT %s",
                (f"{page_id}_", int(limit))).fetchall()
        return [row["story_id"] for row in rows]

    def reconcile(self) -> int:
        """[W] Comments left 'pending' by a worker that stopped mid-reply take the final
        status of their attempt (``abandon_stale_replies`` closes those as 'abandoned')."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.meta_comments c SET reply_status = r.status, updated_at = now() "
                "FROM garage.messenger_replies r WHERE c.reply_status = 'pending' "
                "AND r.channel = 'comment' AND r.trigger_key = c.comment_id "
                "AND r.status <> 'pending'").rowcount

    def listing(self, limit: int, offset: int, only_unreplied: bool) -> dict:
        """[S] See ``list_comments``."""
        try:
            size = max(1, min(int(limit), 200))
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            size, skip = 50, 0
        values = {"unreplied": bool(only_unreplied), "limit": size, "offset": skip}
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT c.comment_id, c.post_id, c.parent_id, c.author_name, c.body, "
                "c.created_time, c.is_hidden, c.permalink, c.source, c.reply_status, "
                "c.replied_psid, c.public_reply_id, c.updated_at, "
                "left(p.message, 160) AS post_excerpt, p.permalink AS post_permalink "
                "FROM garage.meta_comments c LEFT JOIN garage.meta_posts p "
                "ON p.post_id = c.post_id WHERE NOT c.from_page AND (NOT %(unreplied)s OR "
                "(c.reply_status <> 'sent' AND c.public_reply_id = '')) "
                "ORDER BY c.created_time DESC, c.comment_id DESC "
                "LIMIT %(limit)s OFFSET %(offset)s", values).fetchall()
            totals = conn.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE c.reply_status <> 'sent' "
                "AND c.public_reply_id = '') AS unreplied "
                "FROM garage.meta_comments c WHERE NOT c.from_page").fetchone()
        items = []
        for row in rows:
            item = dict(row)
            # Only an answer counts: a fallback ("call us") leaves the comment to the admin.
            item["replied"] = item["reply_status"] == "sent"
            item["link"] = comment_link(item["comment_id"], item["post_id"], item["permalink"])
            items.append(item)
        return json_ready({"comments": items, "total": int(totals["total"]),
                           "unreplied": int(totals["unreplied"]), "limit": size,
                           "offset": skip})


def _subjects(comment: Mapping[str, Any]) -> tuple[str, ...]:
    """How a customer's data deletion names a comment in garage.meta_forgotten."""
    author = comment.get("author_id") or ""
    own = (f"comment:{comment['comment_id']}",)
    return (*own, f"author:{author}") if author else own


def comment_link(comment_id: str, post_id: str, permalink: str = "") -> str:
    """The comment on Facebook: its permalink, else built from the post and comment ids."""
    if permalink:
        return permalink
    page, _, post = (post_id or "").partition("_")
    _, _, comment = (comment_id or "").partition("_")
    if page.isdigit() and post.isdigit() and comment.isdigit():
        return f"https://www.facebook.com/{page}/posts/{post}?comment_id={comment}"
    return ""


# -- what the engine reads and what the customer gets --------------------------------------------


def post_excerpt(text: Any, limit: int = POST_EXCERPT_LIMIT) -> str:
    """A post's text as context for the engine: one line, VINs taken out (a VIN in a post must
    not become every commenter's car), at most ``limit`` characters, cut at a word."""
    flat = " ".join(_redact(clip(text, 4000)).split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def engine_text(comment: Any, excerpt: str = "") -> str:
    """What the engine reads for one comment: the customer's words first, then the post they
    were written under, marked as the shop's own words. The marker is English so that it does
    not change the language the engine detects from Georgian or Russian letters."""
    said = clip(comment, COMMENT_TEXT_LIMIT)
    return f"{said}\n\n[{POST_LABEL} «{excerpt}»]" if excerpt and said else said


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _block(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    lines = [line.rstrip() for line in clip(value, 4000).replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def reply_message(public: Any, language: Any, *, opening: str = "") -> str:
    """One private reply of at most 2,000 characters (UTF-16 units, as Messenger counts) from
    ``site_chat.public_turn``'s view: the opening (the late apology), the answer and its note,
    then up to three part lines and how many more were found. No buttons: a private reply is a
    single message. A long answer is cut at a sentence or line break; the note goes first."""
    view = public if isinstance(public, Mapping) else {}
    lang = text_language(language)
    cards = [line for line in (card_line(card, lang) for card in view.get("cards") or ()
                               if isinstance(card, dict)) if line]
    lines = cards[:CARD_LINES]
    total = view.get("total") if isinstance(view.get("total"), int) else 0
    more = max(total, len(cards)) - len(lines)
    if lines and more > 0:
        texts = TEXTS[lang]
        line = texts.get("more_line_one") if more == 1 else None
        lines.append(line or texts["more_line"].replace("{count}", str(more)))
    tail = "\n".join(lines)
    head = [_block(opening), _block(view.get("answer")), _block(view.get("note"))]
    room = TEXT_LIMIT - (_units(tail) + 2 if tail else 0)
    if room < 200:  # part lines that leave no room for the answer give way to it
        tail, room = "", TEXT_LIMIT
    text = "\n\n".join(block for block in head if block)
    if _units(text) > room:
        text = "\n\n".join(block for block in head[:2] if block)
    if _units(text) > room:
        pieces = split_text(text, room)
        text = pieces[0] if pieces else ""
    message = "\n\n".join(block for block in (text, tail) if block)
    pieces = split_text(message, TEXT_LIMIT)
    return pieces[0] if pieces else ""


def reply_skip(settings: Mapping[str, Any], *, created: datetime, now: datetime,
               text: Any) -> str:
    """Why a new comment is not answered ('' when it may be): the switch is off, it was
    written before the switch went on or more than 7 days ago, the bot is paused because the
    AI provider failed, or it has no text (a sticker or a photo under a post asks nothing)."""
    if not settings.get("comments_enabled"):
        return "skipped_off"
    enabled_at = settings.get("comments_enabled_at")
    if enabled_at is None or created < enabled_at or now - created > REPLY_WINDOW:
        return "skipped_old"
    if settings.get("bot_paused_reason"):
        return "skipped_paused"
    if not isinstance(text, str) or not text.strip():
        return "skipped_empty"
    return ""


# -- the webhook path -----------------------------------------------------------------------------


def _parent(event: CommentEvent) -> str:
    """The comment a reply was written under; '' for a comment on the post itself (Meta names
    the post as the parent of those)."""
    return event.parent_id if event.parent_id and event.parent_id != event.post_id else ""


def _wait_ready(engine: Any) -> None:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while not getattr(engine, "ready", True):
        if time.monotonic() >= deadline:
            raise CommentRetry("engine_not_ready")
        time.sleep(0.5)


def handle_comment_event(ctx: Any, event: Any, engine: Any, *,
                         cost: Callable[[Mapping[str, Any]], float | None] | None = None) -> str:
    """Store, update or answer one ``feed`` comment change (worker login); returns a short
    outcome code for the inbox row (``comment_replied``, ``comment_skipped_off``...).

    ``engine`` is the worker's ``MessengerEngine``. ``cost``, when given, prices a turn's
    usage for ``messenger_replies.cost_usd``. Raises ``CommentRetry`` when the engine is not
    ready yet (nothing was claimed), and lets database errors through for the worker's retry;
    a retried event never answers twice."""
    if not isinstance(event, CommentEvent):
        return "comment_ignored"
    try:
        page_id = ctx.page_id
    except NotConnected:
        return "comment_not_connected"
    if event.page_id and event.page_id != page_id:
        return "comment_other_page"
    comments = CommentStore(ctx.store.db)
    verb = event.verb
    if verb in REMOVE_VERBS:
        return "comment_removed" if comments.remove(event.comment_id) else "comment_unknown"
    if verb in ("hide", "unhide"):
        hidden = event.hidden if event.hidden is not None else verb == "hide"
        if not comments.set_hidden(event.comment_id, hidden):
            return "comment_unknown"
        return "comment_hidden" if hidden else "comment_unhidden"
    if verb in EDIT_VERBS:
        changed = comments.edit(event.comment_id, event.message, event.hidden)
        return "comment_edited" if changed else "comment_unknown"
    if verb != "add":
        return "comment_verb_ignored"
    if event.from_id == page_id or comments.is_our_public_reply(event.comment_id):
        if _parent(event):
            comments.link_page_reply(_parent(event), event.comment_id)
        return "comment_from_page"
    now = ctx.now()
    created = event.created_time or now
    if now - created > REPLY_WINDOW:
        return "comment_too_old"
    row = comments.save({
        "comment_id": event.comment_id, "post_id": event.post_id, "parent_id": _parent(event),
        "author_id": event.from_id, "author_name": event.from_name, "body": event.message,
        "created_time": created, "hidden": event.hidden, "permalink": ""}, "webhook")
    if row is None:  # written before its author's data was deleted: neither kept nor answered
        return "comment_forgotten"
    if row["reply_status"] in HANDLED:
        return "comment_already_handled"
    settings = ctx.store.settings()
    skip = reply_skip(settings, created=created, now=now, text=event.message)
    if not skip and comments.chat_switched_off(event.from_id):
        skip = "skipped_chat_off"  # the admin took this customer's chat over
    if skip:
        comments.set_status(event.comment_id, skip)
        return f"comment_{skip}"
    if engine is None:
        return "comment_no_engine"
    _wait_ready(engine)
    reply_id = ctx.store.start_reply("comment", None, event.comment_id)
    if reply_id is None:
        return "comment_already_handled"
    comments.set_status(event.comment_id, "pending", force=True)
    try:
        return _answer(ctx, comments, event, engine, reply_id, page_id, settings, created, cost)
    except Exception as error:
        try:
            comments.close_pending(reply_id, event.comment_id, type(error).__name__)
        except Exception as closing:  # the original error is the one the worker needs
            LOGGER.warning("meta_comment_close_failed error=%s", type(closing).__name__)
        raise


def _excerpt_for(ctx: Any, comments: CommentStore, post_id: str) -> str:
    """The post's text from the database, else read once from Graph and stored; '' when it
    cannot be read (the comment is answered without it)."""
    text = comments.post_text(post_id)
    if text is None:
        try:
            post = ctx.graph.get(post_id, ctx.page_token(),
                                 {"fields": "message,created_time,permalink_url"})
        except (GraphError, NotConnected, TokenCipherError) as error:
            LOGGER.info("meta_comment_post_unreadable error=%s code=%s", type(error).__name__,
                        getattr(error, "code", None))
            return ""
        text = clip(post.get("message"), 2000)
        created = meta_time(post.get("created_time"))
        if created is not None:
            comments.save_post(post_id, created, text, safe_link(post.get("permalink_url")))
    return post_excerpt(text)


def _ran(result: Any) -> str:
    """``engine_status`` for the attempt: never empty once the engine ran, so the turn counts
    against the caps whatever happens after."""
    return str(getattr(result, "engine_status", "") or getattr(result, "error_code", "")
               or str(getattr(result, "status", "")).upper() or "RAN")[:40]


def _answer(ctx: Any, comments: CommentStore, event: CommentEvent, engine: Any, reply_id: int,
            page_id: str, settings: Mapping[str, Any], created: datetime,
            cost: Callable[[Mapping[str, Any]], float | None] | None) -> str:
    store = ctx.store
    if store.turns_today() > int(settings.get("daily_turn_limit") or 0):
        store.finish_reply(reply_id, "skipped_cap")
        comments.set_status(event.comment_id, "skipped_cap", force=True)
        return "comment_skipped_cap"
    text = engine_text(event.message, _excerpt_for(ctx, comments, event.post_id))
    result = engine.respond(None, text, language="auto", timeout=TURN_SECONDS)
    record: dict[str, Any] = {"engine_status": _ran(result),
                              "usage": dict(getattr(result, "usage", None) or {})}
    if cost is not None:
        try:
            record["cost_usd"] = cost(record["usage"])
        except Exception as error:  # a price is bookkeeping; it never stops a reply
            LOGGER.info("meta_comment_cost_failed error=%s", type(error).__name__)
    code = getattr(result, "error_code", "") or ""
    language = getattr(result, "language", "ka")
    if code in ACCOUNT_CODES:
        newly = store.pause_for_provider()
        LOGGER.warning("meta_comment_provider_failed code=%s paused=%s", code, newly)
        store.finish_reply(reply_id, "paused_provider", error_text=code, **record)
        comments.set_status(event.comment_id, "paused_provider", force=True)
        return "comment_paused_provider"
    if code == "NOT_CONFIGURED":  # no model key: every comment would get the fallback
        store.finish_reply(reply_id, "failed", error_text=code, **record)
        comments.set_status(event.comment_id, "failed", force=True)
        return "comment_engine_unavailable"
    if getattr(result, "status", "") != "ok" and (
            code == "CHAT_CLOSED" or getattr(engine, "closing", False) is True):
        # The worker is stopping and closed the engine under the turn (CONVERSATION_EXPIRED
        # mid-turn, CHAT_CLOSED after): no fallback for a failure of our own making, as on
        # Messenger. Meta allows one private reply, so the comment is left to the admin.
        store.finish_reply(reply_id, "abandoned", error_text=code or "CHAT_CLOSED", **record)
        comments.set_status(event.comment_id, "abandoned", force=True)
        return "comment_abandoned"
    final, message = "sent", ""
    if getattr(result, "status", "") == "ok":
        late = ctx.now() - created > LATE_AFTER
        message = reply_message(result.public, language,
                                opening=late_apology(language) if late else "")
    elif code == "CONFLICTING_VINS":
        message = conflicting_vins_text(language)
    if not message:
        final = "fallback_sent"
        custom = settings.get("fallback_en" if text_language(language) == "en"
                              else "fallback_ka")
        phone = ""
        try:
            phone = engine.shop_phone()
        except Exception as error:
            LOGGER.info("meta_comment_phone_unavailable error=%s", type(error).__name__)
        message = fallback_text(language, phone, custom)
    fresh = store.settings()  # the switches, read again right before anything leaves
    blocked = ("skipped_off" if not fresh.get("comments_enabled")
               else "skipped_paused" if fresh.get("bot_paused_reason")
               else "skipped_chat_off" if comments.chat_switched_off(event.from_id) else "")
    if blocked:
        store.finish_reply(reply_id, blocked, **record)
        comments.set_status(event.comment_id, blocked, force=True)
        return f"comment_{blocked}"
    try:
        token = ctx.page_token()
    except (NotConnected, TokenCipherError) as error:
        store.finish_reply(reply_id, "failed", error_text=type(error).__name__, **record)
        comments.set_status(event.comment_id, "failed", force=True)
        return "comment_failed"
    try:
        sent = ctx.graph.private_reply(page_id, token, event.comment_id, {"text": message})
    except GraphError as error:
        status = ("failed" if error.network or error.http_status >= 500
                  or error.type in ("truncated", "http", "redirect") else "blocked_by_meta")
        if error.token_dead:
            store.mark_token("page", "broken", f"meta_code_{error.code}")
        LOGGER.info("meta_comment_reply_refused code=%s subcode=%s http=%s", error.code,
                    error.subcode, error.http_status)
        store.finish_reply(reply_id, status, error_code=error.code, error_subcode=error.subcode,
                           error_text=error.message or error.type, **record)
        comments.set_status(event.comment_id, status, force=True)
        return f"comment_{status}"
    psid = str(sent.get("recipient_id") or "")
    psid = psid if DIGITS.match(psid) else ""
    mid = str(sent.get("message_id") or "")
    try:
        if psid:
            _adopt_chat(ctx, comments, psid, page_id, event, result, message, text)
            if mid:
                app_id = ctx.settings.app_id if DIGITS.match(ctx.settings.app_id or "") else ""
                store.add_message(psid, mid, "bot", ctx.now(), page_id=page_id, body=message,
                                  app_id=app_id)
            comments.link_reply(reply_id, psid)
            if final != "sent":
                store.flag_attention(psid, "comment_reply_failed")
    except Exception as error:  # the reply is out; its bookkeeping must not undo that
        LOGGER.warning("meta_comment_chat_not_stored error=%s", type(error).__name__)
    store.finish_reply(reply_id, final, sent_mids=[mid] if mid else [],
                       error_text="" if final == "sent" else code or "EMPTY_ANSWER", **record)
    comments.set_status(event.comment_id, final, force=True, replied_psid=psid or None)
    outcome = "comment_replied" if final == "sent" else "comment_fallback_sent"
    public = clip(fresh.get("comment_public_reply"), 300)
    if final == "sent" and public:
        try:
            posted = ctx.graph.comment_reply(token, event.comment_id, public)
        except GraphError as error:  # the private reply stands; the public one is optional
            LOGGER.info("meta_comment_public_reply_failed code=%s http=%s", error.code,
                        error.http_status)
        else:
            reply = str(posted.get("id") or "")[:80]
            if reply:
                comments.set_public_reply(event.comment_id, reply)
                outcome = "comment_replied_public"
    LOGGER.info("meta_comment_answered outcome=%s engine=%s", outcome, record["engine_status"])
    return outcome


def _adopt_chat(ctx: Any, comments: CommentStore, psid: str, page_id: str,
                event: CommentEvent, result: Any, sent_text: str, asked: str) -> None:
    """The commenter's Messenger chat, with the comment turn in its engine state.

    A new chat starts from the turn's state and is marked as coming from a comment. A chat that
    exists keeps its source (an ad, a link) and its state; the comment and the reply are added
    to that state as one more exchange, so neither conversation is lost."""
    current = comments.thread(psid)
    state = getattr(result, "state", None) or {}
    if current is None:
        ctx.store.upsert_thread(psid, page_id, customer_name=event.from_name or None,
                                source="comment")
        expected = None
    else:
        comments.claim_source(psid, "comment")
        if event.from_name and not current["customer_name"]:
            ctx.store.upsert_thread(psid, page_id, customer_name=event.from_name)
        expected = current["engine_state_at"]
        if current["engine_state"] is not None:
            state = record_shop_reply(current["engine_state"], sent_text, customer_text=asked)
    if not comments.save_state(psid, state, expected):
        LOGGER.info("meta_comment_state_kept reason=changed_meanwhile")


# -- the backfill -------------------------------------------------------------------------------


def _total(value: Any) -> int:
    summary = value.get("summary") if isinstance(value, Mapping) else None
    return (count(summary.get("total_count")) if isinstance(summary, Mapping) else None) or 0


def parse_comment(item: Any, post_id: str, page_id: str) -> dict | None:
    """One ``{post}/comments`` item (fields ``COMMENT_FIELDS``) as a stored comment, with
    ``from_page`` set for the page's own; None without a usable id or time."""
    if not isinstance(item, Mapping):
        return None
    comment_id, created = object_id(item.get("id")), meta_time(item.get("created_time"))
    if not comment_id or created is None:
        return None
    author = item.get("from") if isinstance(item.get("from"), Mapping) else {}
    parent = item.get("parent") if isinstance(item.get("parent"), Mapping) else {}
    parent_id = object_id(parent.get("id"))
    author_id = object_id(author.get("id"), DIGITS)
    hidden = item.get("is_hidden")
    return {"comment_id": comment_id, "post_id": post_id,
            "parent_id": parent_id if parent_id != post_id else "",
            "author_id": author_id, "author_name": clip(author.get("name"), 200),
            "body": clip(item.get("message"), 4000), "created_time": created,
            "hidden": hidden if isinstance(hidden, bool) else None,
            "permalink": safe_link(item.get("permalink_url")),
            "from_page": bool(author_id) and author_id == page_id}


def sync_comments(ctx: Any) -> SyncCount:
    """The last 30 days of comments on the page's recent posts and on its ads' posts, for the
    dashboard list (worker login, every 10 minutes). It never answers anything: no engine, no
    send. The page's own replies are not stored; each marks the comment it answers as
    publicly replied. Also settles comments a stopped worker left 'pending'."""
    page_id, token = ctx.page_id, ctx.page_token()
    comments = CommentStore(ctx.store.db)
    since = ctx.now() - timedelta(days=BACKFILL_DAYS)
    try:
        posts = ctx.graph.paged(f"{page_id}/posts", token, {
            "fields": "id,created_time,comments.limit(0).summary(true)", "limit": 50},
            max_pages=2)
    except GraphError as error:
        if error.token_dead:
            ctx.store.mark_token("page", "broken", f"meta_code_{error.code}")
        raise
    post_ids = [post_id for post in posts if _total(post.get("comments")) > 0
                and (post_id := object_id(post.get("id")))]
    post_ids += [story for story in comments.ad_story_ids(page_id) if story not in post_ids]
    stored = linked = unreadable = 0
    for post_id in post_ids[:BACKFILL_POSTS]:
        try:
            # Newest first, so the pages read hold the 30 days; older ones are dropped below.
            rows = ctx.graph.paged(f"{post_id}/comments", token, {
                "fields": COMMENT_FIELDS, "filter": "stream", "order": "reverse_chronological",
                "limit": 100}, max_pages=BACKFILL_PAGES)
        except GraphError as error:
            if error.token_dead:
                ctx.store.mark_token("page", "broken", f"meta_code_{error.code}")
                raise
            LOGGER.info("meta_comments_post_unreadable code=%s http=%s", error.code,
                        error.http_status)
            unreadable += 1
            continue
        parsed = [comment for comment in (parse_comment(item, post_id, page_id) for item in rows)
                  if comment is not None and comment["created_time"] >= since]
        customers = [comment for comment in parsed if not comment["from_page"]]
        if customers:
            # A comment of a customer whose data was deleted is not stored again (None).
            stored += sum(row is not None
                          for row in comments.save_many(customers, "backfill"))
        # Replies come before what they answer in this order, so they are linked last.
        for comment in parsed:
            if comment["from_page"] and comment["parent_id"]:
                linked += comments.link_page_reply(comment["parent_id"], comment["comment_id"])
    settled = comments.reconcile()
    return SyncCount(stored, "ok", f"posts={len(post_ids)} comments={stored} "
                                   f"page_replies={linked} unreadable={unreadable} "
                                   f"settled={settled}")


# -- retention and deletion (worker login) ---------------------------------------------------


def purge_comments(db: Any, retention_days: int | None = None) -> int:
    """Delete stored comments older than the retention period (default: the settings'
    ``retention_days``), like ``MetaStore.purge`` does for messages. For the worker's daily
    purge; the backfill only ever reads 30 days back, so nothing purged comes back."""
    with database_of(db).transaction() as conn:
        days = retention_days
        if days is None:
            days = conn.execute("SELECT retention_days FROM garage.meta_settings "
                                "WHERE settings_id = 1").fetchone()["retention_days"]
        days = int(days)
        if not 1 <= days <= 3650:
            raise ValueError("retention_days must be 1-3650")
        return conn.execute("DELETE FROM garage.meta_comments "
                            "WHERE created_time < now() - make_interval(days => %s)",
                            (days,)).rowcount


def forget_customer(db: Any, psid: str) -> int | None:
    """Delete the stored comments of the customer whose chat is ``psid``: those answered into
    that chat, and every other comment of the same author (``meta_store.forget_comments``,
    which ``MetaStore.delete_customer`` runs in its own transaction). Either login. Lasting:
    their ids are kept, hashed, so the backfill (``sync_comments``) and late webhooks never
    store those comments again. Returns the comments deleted, None before migration 0014;
    audit the deletion with the caller's."""
    if not isinstance(psid, str) or not DIGITS.match(psid):
        raise ValueError("psid must be a numeric id")
    with database_of(db).transaction() as conn:
        return forget_comments(conn, psid)


# -- the dashboard (site login) ---------------------------------------------------------------


def list_comments(db: Any, limit: int = 50, offset: int = 0,
                  only_unreplied: bool = False) -> dict:
    """Customers' comments, newest first: ``{comments, total, unreplied, limit, offset}``.

    Each comment: comment_id, post_id, parent_id, author_name, body, created_time, is_hidden,
    permalink, link (Facebook), source ('webhook' or 'backfill'), reply_status (the bot's:
    '', 'pending', 'sent', 'fallback_sent', 'failed', 'blocked_by_meta', 'paused_provider',
    'abandoned' or a 'skipped_*' reason), replied (a private reply went out), replied_psid (the
    chat it opened), public_reply_id, updated_at, post_excerpt and post_permalink.
    ``replied`` and ``only_unreplied`` count only a real answer ('sent') or a public reply:
    a comment that got only the fallback ("call us") still waits for the admin."""
    return CommentStore(db).listing(limit, offset, only_unreplied)


def _action(ctx: Any, comment_id: Any) -> tuple[CommentStore, dict, Any]:
    if not isinstance(comment_id, str) or not object_id(comment_id):
        raise CommentActionError("INVALID", {"field": "comment_id"})
    comments = CommentStore(ctx.store.db)
    row = comments.get(comment_id)
    if row is None or row["from_page"]:
        raise CommentActionError("NOT_FOUND")
    try:
        token = ctx.page_token()
    except (NotConnected, TokenCipherError) as error:
        raise CommentActionError("NOT_CONNECTED", {"reason": type(error).__name__}) from None
    return comments, row, token


def _refused(ctx: Any, error: GraphError, actor: Any, action: str, comment_id: str,
             client_ip: str | None) -> CommentActionError:
    if error.token_dead:
        code = "TOKEN_DEAD"
        try:
            ctx.store.mark_token("page", "broken", f"meta_code_{error.code}")
        except Exception as marking:
            LOGGER.warning("meta_token_mark_failed error=%s", type(marking).__name__)
    elif error.network or error.http_status >= 500 or error.type in ("truncated", "http"):
        code = "NETWORK"
    else:
        code = "META_REFUSED"
    try:
        ctx.store.audit(actor, "messenger.comment_action_failed", comment_id,
                        {"action": action, "code": error.code, "subcode": error.subcode},
                        client_ip=client_ip)
    except Exception as auditing:  # the refusal is what the admin must see
        LOGGER.warning("meta_audit_failed error=%s", type(auditing).__name__)
    return CommentActionError(code, {"meta": {**error.summary(), "message": error.message},
                                     "maybe_applied": error.maybe_applied})


def set_hidden(ctx: Any, comment_id: str, hidden: bool, actor: Any, *,
               client_ip: str | None = None) -> dict:
    """Hide or unhide a stored comment on Facebook, then here; audited. Returns
    ``{comment_id, hidden}``; raises CommentActionError."""
    comments, _, token = _action(ctx, comment_id)
    try:
        ctx.graph.set_comment_hidden(token, comment_id, bool(hidden))
    except GraphError as error:
        raise _refused(ctx, error, actor, "hide" if hidden else "unhide", comment_id,
                       client_ip) from None
    comments.set_hidden(comment_id, bool(hidden))
    ctx.store.audit(actor, "messenger.comment_hidden" if hidden
                    else "messenger.comment_unhidden", comment_id, {}, client_ip=client_ip)
    return {"comment_id": comment_id, "hidden": bool(hidden)}


def public_reply(ctx: Any, comment_id: str, text: str, actor: Any, *,
                 client_ip: str | None = None) -> dict:
    """Post the admin's public reply under a stored comment; audited (its length, never its
    text). Returns ``{comment_id, public_reply_id}``; raises CommentActionError. Never
    retried: a reply that may have been posted is reported, not repeated."""
    words = text.strip() if isinstance(text, str) else ""
    if not words or len(words) > PUBLIC_REPLY_LIMIT or "\x00" in words:
        raise CommentActionError("INVALID", {"field": "text"})
    comments, _, token = _action(ctx, comment_id)
    try:
        posted = ctx.graph.comment_reply(token, comment_id, words)
    except GraphError as error:
        raise _refused(ctx, error, actor, "public_reply", comment_id, client_ip) from None
    reply = str(posted.get("id") or "")[:80]
    if reply:
        comments.set_public_reply(comment_id, reply)
    ctx.store.audit(actor, "messenger.comment_public_reply", comment_id,
                    {"chars": len(words), "stored": bool(reply)}, client_ip=client_ip)
    return {"comment_id": comment_id, "public_reply_id": reply}
