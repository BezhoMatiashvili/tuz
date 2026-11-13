"""Webhook inbox rows read as the events the Messenger worker acts on.

The public receiver (``automotive.meta_receiver``) stores every signature-checked webhook item as
one row of ``garage.meta_inbox``: field ``messages`` for a messaging item (a customer's message,
the echo of a message the Page sent, a postback, a referral), ``standby`` for the same items
delivered while another app holds the conversation, and a change field such as ``feed`` with the
change object ``{"field", "value"}`` as its payload. It adds reserved keys of its own, which Meta
never uses because they start with an underscore: the delivery's ``entry.time`` and, for an item
too large for the table, a marker that replaces the item. ``parse`` reads one row into one frozen
event.

Meta signs these payloads, but their shape is Meta's to change. So no field is trusted: every
value is type-checked, ids are checked against the column patterns of migration 0013 (a value the
worker could not store becomes ``Ignored`` here, not a constraint error there), text is capped at
4,000 characters, and an attachment keeps its type and whether it carried a link, never the link
itself. ``parse`` never raises.

Nothing here reads what a message means. Understanding the customer is the assistant's job; this
module only moves data.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TEXT_LIMIT = 4000  # messenger_messages.body and meta_comments.body
MID_LIMIT = 200  # messenger_messages.mid
PAYLOAD_LIMIT = 1000  # messenger_messages.quick_reply; Meta caps a payload at 1,000 too
TITLE_LIMIT = 200
NAME_LIMIT = 200  # meta_comments.author_name
REF_LIMIT = 1000
ATTACHMENT_LIMIT = 10
# The receiver's reserved payload keys (``meta_receiver.ENTRY_TIME_KEY`` and ``OVERSIZED_KEY``;
# a test keeps the two modules in step).
ENTRY_TIME_KEY = "_entry_time"
OVERSIZED_KEY = "_oversized"
# Messaging items nothing here acts on. The receiver already drops read and delivery receipts;
# the rest are named so a log of skipped rows says what they were.
SKIPPED_MESSAGING = ("read", "delivery", "reaction", "optin", "account_linking",
                     "pass_thread_control", "take_thread_control", "request_thread_control",
                     "app_roles", "policy_enforcement", "message_edit", "game_play",
                     "response_feedback")
MESSAGING_FIELDS = ("messages", "standby")

_ID = re.compile(r"[0-9]{1,30}")  # psid, page id, app id, ad id
_OBJECT_ID = re.compile(r"[0-9_]{1,80}")  # comment and post ids
_WORD = re.compile(r"[a-z_]{1,20}")  # attachment types, feed items and verbs
_CODE = re.compile(r"[A-Z0-9_]{1,40}")  # referral source and type
_DIGITS = re.compile(r"[0-9]{1,16}")
# 2000-01-01 to 2200-01-01. A time outside these is not one Meta sent: fall back to another.
_EARLIEST, _LATEST = 946_684_800, 7_258_118_400
# Above this a number is epoch milliseconds (messaging ``timestamp``), below it epoch seconds
# (``created_time`` of a feed change). 1e11 seconds is the year 5138; 1e11 ms is 1973.
_MILLISECONDS = 100_000_000_000


@dataclass(frozen=True, slots=True)
class Referral:
    """Where a chat came from: an ad, an m.me link with ``ref``, the chat plugin.

    ``source`` and ``type`` are Meta's own codes in upper case (``ADS``, ``SHORTLINK``,
    ``OPEN_THREAD``); ``ads_title`` is the ad's title when Meta sends one. The ad's image and
    video links are never kept."""

    source: str
    type: str
    ad_id: str
    ref: str
    ads_title: str

    @property
    def channel(self) -> str:
        """The ``messenger_threads.source`` value this referral stands for."""
        if self.source == "ADS" or self.ad_id:
            return "ads"
        return "shortlink" if self.source == "SHORTLINK" else "other"


@dataclass(frozen=True, slots=True)
class CustomerMessage:
    """A message a customer sent to the page.

    ``attachments`` holds ``{"type": ..., "has_url": bool}`` per attachment, never the link.
    ``sticker`` is set when the message is a sticker (the thumbs-up "like" among them).
    ``standby`` is set when it arrived on the standby channel: another app held the
    conversation, so it is a record of what was said and the bot must not answer it."""

    psid: str
    page_id: str
    mid: str
    text: str
    attachments: tuple[dict, ...]
    sticker: bool
    quick_reply: str | None
    referral: Referral | None
    reply_to_mid: str
    sent_at: datetime
    standby: bool = False


@dataclass(frozen=True, slots=True)
class PageEcho:
    """A message the page sent, echoed back: ``psid`` is the customer it went to.

    ``app_id`` names the app that sent it ('' when Meta gives none, as for some messages typed
    in the page inbox); the worker compares it with its own app to tell the bot's messages from
    the shop's. A photo the shop sent is an echo with empty ``text`` and an attachment."""

    psid: str
    page_id: str
    mid: str
    text: str
    app_id: str
    metadata: str
    sent_at: datetime
    attachments: tuple[dict, ...] = ()
    standby: bool = False


@dataclass(frozen=True, slots=True)
class Postback:
    """A tap on a button the page sent (or "Get started"); ``referral`` when it opened from
    an ad or a link."""

    psid: str
    page_id: str
    mid: str
    title: str
    payload: str
    referral: Referral | None
    sent_at: datetime
    standby: bool = False


@dataclass(frozen=True, slots=True)
class ReferralEvent:
    """A customer who already had a chat opened it again from an ad or a link."""

    psid: str
    page_id: str
    referral: Referral
    sent_at: datetime
    standby: bool = False


@dataclass(frozen=True, slots=True)
class CommentEvent:
    """A change to a comment on one of the page's posts (``feed``).

    ``verb`` is Meta's (``add``, ``edited``, ``remove``, ``hide``, ``unhide``...).
    ``created_time`` is None when Meta gave none on an event that is not ``add``. ``hidden``
    is the comment's state when the change says it, else None."""

    page_id: str
    verb: str
    item: str
    comment_id: str
    post_id: str
    parent_id: str
    from_id: str
    from_name: str
    message: str
    created_time: datetime | None
    hidden: bool | None


@dataclass(frozen=True, slots=True)
class Oversized:
    """An item the receiver could not store whole (over its size cap), kept as ids only.

    Enough to flag the chat or comment and to fetch the original from the Graph API; there is
    no text to answer. ``kind`` is the receiver's: ``msg``, ``echo``, ``pb``, ``ref``, ``chg``,
    ``sb`` or ``raw``. ``psid`` is the customer for ``msg``, ``echo``, ``pb`` and ``ref``."""

    kind: str
    psid: str
    page_id: str
    mid: str
    comment_id: str
    post_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class Ignored:
    """A row nothing acts on. ``reason`` is a fixed code, safe to log."""

    reason: str


Event = (CustomerMessage | PageEcho | Postback | ReferralEvent | CommentEvent | Oversized
         | Ignored)


def parse(field: Any, payload: Any, *, page_id: str = "",
          received_at: datetime | None = None) -> Event:
    """One inbox row as an event; never raises.

    ``page_id`` is the row's own page id column (the webhook entry's id). A feed change carries
    none of its own, and a messaging item for another page is ``Ignored("other_page")``.
    ``received_at`` is the row's ``received_at``: the time of last resort when neither the
    item nor its entry gives one."""
    try:
        return _parse(field, payload, _id(page_id), _aware(received_at))
    except Exception:  # A shape nobody foresaw skips the row; it must not stop the worker.
        return Ignored("malformed")


def _parse(field: Any, payload: Any, page_hint: str, received_at: datetime | None) -> Event:
    if not isinstance(payload, dict) or not isinstance(field, str):
        return Ignored("malformed")
    fallback = _moment(payload.get(ENTRY_TIME_KEY)) or received_at or datetime.now(UTC)
    if payload.get(OVERSIZED_KEY) is True:
        return _oversized(payload, page_hint, fallback)
    if field in MESSAGING_FIELDS:
        return _messaging(payload, field == "standby", page_hint, fallback)
    return _change(field, payload, page_hint, fallback)


# -- messaging -------------------------------------------------------------------------------


def _messaging(item: dict, standby: bool, page_hint: str, fallback: datetime) -> Event:
    sender, recipient = _id_of(item.get("sender")), _id_of(item.get("recipient"))
    sent_at = _moment(item.get("timestamp")) or fallback
    message = item.get("message")
    if message is not None:
        if not isinstance(message, dict):
            return Ignored("malformed")
        if message.get("is_echo") is True:
            # The page is the sender of its own message; the customer is the recipient.
            return _echo(message, psid=recipient, page_id=sender, page_hint=page_hint,
                         sent_at=sent_at, standby=standby)
        return _customer(item, message, psid=sender, page_id=recipient, page_hint=page_hint,
                         sent_at=sent_at, standby=standby)
    postback = item.get("postback")
    if isinstance(postback, dict):
        problem = _parties(sender, recipient, page_hint)
        if problem:
            return Ignored(problem)
        return Postback(psid=sender, page_id=recipient, mid=_mid(postback.get("mid")),
                        title=_text(postback.get("title"), TITLE_LIMIT),
                        payload=_text(postback.get("payload"), PAYLOAD_LIMIT),
                        referral=_referral(postback.get("referral"))
                        or _referral(item.get("referral")),
                        sent_at=sent_at, standby=standby)
    referral = item.get("referral")
    if referral is not None:
        problem = _parties(sender, recipient, page_hint)
        if problem:
            return Ignored(problem)
        found = _referral(referral)
        if found is None:
            return Ignored("bad_referral")
        return ReferralEvent(psid=sender, page_id=recipient, referral=found, sent_at=sent_at,
                             standby=standby)
    for name in SKIPPED_MESSAGING:
        if name in item:
            return Ignored(name)
    return Ignored("unknown_messaging")


def _customer(item: dict, message: dict, *, psid: str, page_id: str, page_hint: str,
              sent_at: datetime, standby: bool) -> Event:
    problem = _parties(psid, page_id, page_hint)
    if problem:
        return Ignored(problem)
    mid = _mid(message.get("mid"))
    if not mid:
        return Ignored("missing_mid")
    if message.get("is_deleted") is True:
        return Ignored("deleted")
    text = _text(message.get("text"))
    attachments, sticker = _attachments(message)
    quick = message.get("quick_reply")
    quick_reply = _text(quick.get("payload"), PAYLOAD_LIMIT) if isinstance(quick, dict) else ""
    if not (text or attachments or sticker or quick_reply):
        return Ignored("empty_message")
    reply_to = message.get("reply_to")
    return CustomerMessage(
        psid=psid, page_id=page_id, mid=mid, text=text, attachments=attachments,
        sticker=sticker, quick_reply=quick_reply or None,
        referral=_referral(message.get("referral")) or _referral(item.get("referral")),
        reply_to_mid=_mid(reply_to.get("mid")) if isinstance(reply_to, dict) else "",
        sent_at=sent_at, standby=standby)


def _echo(message: dict, *, psid: str, page_id: str, page_hint: str, sent_at: datetime,
          standby: bool) -> Event:
    problem = _parties(psid, page_id, page_hint)
    if problem:
        return Ignored(problem)
    mid = _mid(message.get("mid"))
    if not mid:
        return Ignored("missing_mid")
    # An echo stays an echo with no text at all: a photo the shop sent still answers the chat.
    attachments, _ = _attachments(message)
    return PageEcho(psid=psid, page_id=page_id, mid=mid, text=_text(message.get("text")),
                    app_id=_id(message.get("app_id")),
                    metadata=_text(message.get("metadata"), PAYLOAD_LIMIT), sent_at=sent_at,
                    attachments=attachments, standby=standby)


def _parties(psid: str, page_id: str, page_hint: str) -> str:
    """Why a messaging item cannot be stored as a chat of this page, or ''."""
    if not psid or not page_id:
        return "missing_party"
    if page_hint and page_id != page_hint:
        return "other_page"
    if psid == page_id:
        return "page_to_itself"
    return ""


def _attachments(message: dict) -> tuple[tuple[dict, ...], bool]:
    """Each attachment's type and whether it had a link; and whether the message is a sticker.

    An "unsupported" message (a kind Messenger does not hand to apps) counts as one attachment:
    the customer sent something, and it cannot be read."""
    found: list[dict] = []
    sticker = _present(message.get("sticker_id"))
    items = message.get("attachments")
    for entry in items if isinstance(items, list) else ():
        if len(found) >= ATTACHMENT_LIMIT:
            break
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        content = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if _present(content.get("sticker_id")):
            sticker = True
        url = content.get("url")
        found.append({"type": kind if _WORD.fullmatch(kind) else "unknown",
                      "has_url": isinstance(url, str) and bool(url.strip())})
    if message.get("is_unsupported") is True and not found:
        found.append({"type": "unsupported", "has_url": False})
    return tuple(found), sticker


def _referral(value: Any) -> Referral | None:
    if not isinstance(value, dict):
        return None
    context = value.get("ads_context_data")
    found = Referral(source=_code(value.get("source")), type=_code(value.get("type")),
                     ad_id=_id(value.get("ad_id")), ref=_text(value.get("ref"), REF_LIMIT),
                     ads_title=_text(context.get("ad_title"), TITLE_LIMIT)
                     if isinstance(context, dict) else "")
    return found if (found.source or found.ad_id or found.ref) else None


# -- changes ---------------------------------------------------------------------------------


def _change(field: str, payload: dict, page_hint: str, fallback: datetime) -> Event:
    own = payload.get("field")
    if (own if isinstance(own, str) and own else field) != "feed":
        return Ignored("unsupported_field")
    value = payload.get("value")
    if not isinstance(value, dict):
        return Ignored("malformed")
    item = _word(value.get("item"))
    if item != "comment":
        return Ignored("not_a_comment")
    verb = _word(value.get("verb"))
    comment_id, post_id = _object_id(value.get("comment_id")), _object_id(value.get("post_id"))
    if not verb or not comment_id or not post_id:
        return Ignored("bad_comment")
    author = value.get("from") if isinstance(value.get("from"), dict) else {}
    # A post id is "<page id>_<post id>": the page a feed change belongs to when the row's own
    # page id is not given.
    prefix = post_id.split("_", 1)[0] if "_" in post_id else ""
    hidden = value.get("is_hidden")
    if not isinstance(hidden, bool):
        hidden = True if verb == "hide" else False if verb == "unhide" else None
    created = _moment(value.get("created_time"))
    if created is None and verb == "add":
        created = fallback  # A new comment is as old as the delivery that announced it.
    return CommentEvent(
        page_id=page_hint or _id(prefix), verb=verb, item=item, comment_id=comment_id,
        post_id=post_id, parent_id=_object_id(value.get("parent_id")),
        from_id=_id(author.get("id")), from_name=_text(author.get("name"), NAME_LIMIT),
        message=_text(value.get("message")), created_time=created, hidden=hidden)


def _oversized(payload: dict, page_hint: str, fallback: datetime) -> Event:
    ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else {}
    kind = _word(payload.get("_kind")) or "raw"
    sender, recipient = _id(ids.get("sender")), _id(ids.get("recipient"))
    psid, page_id = (recipient, sender) if kind == "echo" else (sender, recipient)
    if kind == "chg":
        psid, page_id = "", page_hint
    return Oversized(kind=kind, psid=psid, page_id=page_hint or page_id,
                     mid=_mid(ids.get("mid")), comment_id=_object_id(ids.get("comment_id")),
                     post_id=_object_id(ids.get("post_id")),
                     sent_at=_moment(ids.get("timestamp")) or fallback)


# -- values ----------------------------------------------------------------------------------


def _present(value: Any) -> bool:
    return value is not None and value is not False and value != "" and value != 0


def _id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        value = str(value)
    return value if isinstance(value, str) and _ID.fullmatch(value) else ""


def _id_of(value: Any) -> str:
    return _id(value.get("id")) if isinstance(value, dict) else ""


def _object_id(value: Any) -> str:
    return value if isinstance(value, str) and _OBJECT_ID.fullmatch(value) else ""


def _word(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if _WORD.fullmatch(value) else ""


def _code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().upper()
    return value if _CODE.fullmatch(value) else ""


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    """Text as the tables can hold it: no NUL, no lone surrogate, trimmed and capped."""
    if not isinstance(value, str):
        return ""
    value = value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    return value.strip()[:limit]


def _mid(value: Any) -> str:
    """A message id, or '' when it is not one the table could hold. Never cut short: a
    shortened mid would name another message."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not 1 <= len(value) <= MID_LIMIT or "\x00" in value:
        return ""
    return value


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _moment(value: Any) -> datetime | None:
    """A time Meta sends: epoch milliseconds, epoch seconds or ISO 8601; else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if _DIGITS.fullmatch(text):
            value = int(text)
        else:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return None
            moment = _aware(moment)
            return moment if moment and _EARLIEST <= moment.timestamp() <= _LATEST else None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        seconds = value / 1000 if value >= _MILLISECONDS else value
        if not _EARLIEST <= seconds <= _LATEST:
            return None
        return datetime.fromtimestamp(seconds, UTC)
    return None
