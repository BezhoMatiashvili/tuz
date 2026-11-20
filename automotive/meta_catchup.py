"""Messenger messages the webhook missed, read back from the page's conversations and stored.

Meta delivers a webhook to this laptop only while it is awake and retries for about two hours.
Measured on 2026-11-20 against a full export of the inbox: every customer message sent while the
laptop was awake was stored, and none of the 44 sent more than two hours before it woke up. This
job reads the page's recently updated conversations through the Graph API and stores each message
the database does not hold yet, so the chat history is complete (the owner's decision: messages are
kept for good).

It never answers anybody by itself. Every customer message a chat gets this way that nobody
answered is claimed as handled (``MetaStore.add_caught_up``), so neither a burst nor the
worker's start-up recovery turns any of them into a late bot reply; the Live page lists the
chat as waiting for staff instead (``live_store.WAITING``). When the customer writes again, the
bot's answer to the new message reads the earlier unanswered ones too, as it does for any
messages nobody answered. A chat whose data
staff deleted is never read back (``garage.meta_forgotten``, key ``chat:<psid>``).
It runs with the worker's login, like the names job: reads Meta, writes the Messenger tables.
Nothing here logs a message, a name or an id beyond counts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any

from automotive.meta_connect import _meta_time, _participants
from automotive.meta_context import MetaContext

LOGGER = logging.getLogger(__name__)

JOB = "catchup"
# The first run reads back to when the page was connected (the webhook's first day: what came
# before is the page's older history, which the webhook never saw and this job does not import),
# and at most this far.
FIRST_DAYS = 30
OVERLAP = timedelta(hours=1)  # each run reads again from an hour before the last good one
# A message this young is the webhook's: stored and claimed here first, its delivery would land
# as a duplicate and the bot would never answer it. A later run (reading an hour back) takes it
# if the webhook never brings it.
FRESH = timedelta(minutes=5)
FOLDERS = ("inbox", "other")  # Meta files some chats under "other"; both are read
CONVERSATIONS_PAGE = 50
MAX_CONVERSATION_PAGES = 10
MESSAGES_PAGE = 50
MAX_MESSAGE_PAGES = 6
MESSAGE_FIELDS = "id,created_time,from,message,attachments{mime_type}"


def _kind(mime: Any) -> str:
    """What the webhook calls an attachment, from Graph's mime type (types only are stored)."""
    family = str(mime or "").split("/", 1)[0].lower()
    return {"image": "image", "video": "video", "audio": "audio"}.get(family, "file")


def _row(item: Mapping[str, Any], page_id: str, psid: str) -> dict | None:
    """A Graph message as ``MetaStore.add_caught_up`` takes it, or None when it is not one
    of this chat's two sides or carries no id or time."""
    sender_block = item.get("from")
    sender_id = str(sender_block.get("id") or "") if isinstance(sender_block, Mapping) else ""
    sender = "shop" if sender_id == page_id else "customer" if sender_id == psid else None
    when = _meta_time(item.get("created_time"))
    mid = item.get("id")
    if sender is None or when is None or not isinstance(mid, str) or not mid:
        return None
    block = item.get("attachments")
    attachments = block.get("data") if isinstance(block, Mapping) else None
    return {"mid": mid, "sender": sender, "sent_at": when,
            "body": item.get("message") if isinstance(item.get("message"), str) else "",
            "attachments": [{"type": _kind(entry.get("mime_type"))}
                            for entry in attachments or () if isinstance(entry, Mapping)]}


class Truncated(RuntimeError):
    """A run hit its page limits before the start of its window: what it read is stored, and the
    run is recorded as failed so the window is never marked done. The next run reads newest
    first again and stops at the same place: raising the limits is what reaches the rest."""


def _pages(ctx: MetaContext, path: str, token: str, params: Mapping[str, Any],
           max_pages: int) -> Iterator[list[dict]]:
    """Graph's pages of ``data`` items, newest first, following the ``after`` cursor. Raises
    ``Truncated`` (after the last page) when more pages remain past ``max_pages``."""
    query = dict(params)
    for number in range(max_pages + 1):
        if number == max_pages:
            raise Truncated(path.split("/")[-1])
        page = ctx.graph.get(path, token, query)
        data = page.get("data")
        if not isinstance(data, list) or not data:
            return
        yield [item for item in data if isinstance(item, dict)]
        paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
        cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
        after = cursors.get("after")
        if not paging.get("next") or not isinstance(after, str) or not after:
            return
        query = {**query, "after": after}


def _conversations(ctx: MetaContext, page_id: str, token: str, folder: str,
                   since: datetime) -> Iterator[dict]:
    """The folder's conversations updated since ``since`` (Meta lists the newest first)."""
    params = {"fields": "id,updated_time,participants", "limit": CONVERSATIONS_PAGE,
              "folder": folder}
    for page in _pages(ctx, f"{page_id}/conversations", token, params, MAX_CONVERSATION_PAGES):
        for conversation in page:
            updated = _meta_time(conversation.get("updated_time"))
            if updated is not None and updated < since:
                return
            yield conversation


def _messages(ctx: MetaContext, token: str, conversation_id: str,
              since: datetime) -> Iterator[dict]:
    """The conversation's messages sent since ``since``, newest first."""
    params = {"fields": MESSAGE_FIELDS, "limit": MESSAGES_PAGE}
    for page in _pages(ctx, f"{conversation_id}/messages", token, params, MAX_MESSAGE_PAGES):
        for item in page:
            when = _meta_time(item.get("created_time"))
            if when is not None and when < since:
                return
            yield item


def start_time(ctx: MetaContext) -> datetime:
    """From when this run reads: an hour before the last good run (at most FIRST_DAYS back);
    on the very first run, from when the page was connected. A reconnect later (a token that
    died) moves connected_at, so it never limits a later run: the gap before it is read too."""
    floor = ctx.now() - timedelta(days=FIRST_DAYS)
    last = ctx.store.last_ok_run(JOB)
    if last is not None:
        return max(last - OVERLAP, floor)
    connected = (ctx.store.page() or {}).get("connected_at")
    return max(connected, floor) if isinstance(connected, datetime) else floor


def sync_missed_messages(ctx: MetaContext, *, since: datetime | None = None,
                         dry_run: bool = False) -> dict:
    """Store what the webhook missed; counts only. ``dry_run`` reads and compares, writes
    nothing: ``known`` says how many of the messages read are stored already."""
    page_id = ctx.page_id
    token = ctx.page_token()
    start = since if since is not None else start_time(ctx)
    counts = {"conversations": 0, "messages": 0, "stored": 0, "claimed": 0, "known": 0}
    fresh = ctx.now() - FRESH
    seen: set[str] = set()
    truncated = False
    for folder in FOLDERS:
        conversations = _conversations(ctx, page_id, token, folder, start)
        while True:
            try:
                conversation = next(conversations)
            except StopIteration:
                break
            except Truncated:
                truncated = True
                break
            cid = conversation.get("id")
            people = _participants(conversation, page_id)
            if not isinstance(cid, str) or not cid or cid in seen or len(people) != 1:
                continue
            seen.add(cid)
            psid = str(people[0]["id"])
            items: list[dict] = []
            try:  # newest first: what was read before a cut-off is the newest, kept
                for item in _messages(ctx, token, cid, start):
                    items.append(item)
            except Truncated:
                truncated = True
            rows = [row for row in (_row(item, page_id, psid) for item in items)
                    if row and row["sent_at"] <= fresh]
            counts["conversations"] += 1
            counts["messages"] += len(rows)
            if not rows:
                continue
            if dry_run:
                counts["known"] += len(ctx.store.known_mids([row["mid"] for row in rows]))
                continue
            # Oldest first, as they happened: Graph lists the newest first and its times have
            # whole seconds, so reversed before the (stable) sort, two in one second keep
            # their true order, which is also the storage order the worker reads.
            rows.reverse()
            rows.sort(key=lambda row: row["sent_at"])
            result = ctx.store.add_caught_up(psid, page_id, rows)
            counts["stored"] += int(result.get("stored", 0))
            counts["claimed"] += int(result.get("claimed", 0))
    if counts["stored"]:
        LOGGER.info("meta_catchup_stored conversations=%d messages=%d stored=%d claimed=%d",
                    counts["conversations"], counts["messages"], counts["stored"],
                    counts["claimed"])
    if truncated and not dry_run:
        raise Truncated(f"stored={counts['stored']}")
    return counts
