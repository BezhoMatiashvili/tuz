"""Dashboard API for the Live chats page: alarms, the conversations of both channels, the bot's
switch for one customer.

``site.install_site`` mounts this router under ``/api/admin/live``. The site guard has already
required a staff session, an allow-listed Origin and the CSRF header for every route (its
``/api/admin/`` rule). Unlike the Facebook section, **both roles** may use every route here
(``site_admin._user``): an employee at the counter is who the alarms are for. What an employee
still cannot reach is everything else of the Facebook section (the connection, the switches for
everyone, the admin-account flag, deleting a customer's data, costs); this router returns none of
that and offers no write beyond: marking an alarm done or open, one customer's bot switch, and
clearing a chat's "needs attention" flag.

The page polls ``GET /pulse`` every few seconds on every dashboard page (the alarm badge and
sound) and, while the Live page is open, with ``detail=1`` for the new messages. Answers carry
codes only; the browser words them. Until migration 0015 is applied every route answers 503
``LIVE_NOT_SET_UP`` and the page says so, instead of failing every few seconds.
"""

# No ``from __future__ import annotations`` here: the routes depend on closures over this
# router's store, and FastAPI resolves string annotations against module globals only.

import contextlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NamedTuple

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from automotive.live_store import AlreadyOpen
from automotive.meta_store import MetaConflict
from automotive.site_admin import _address, _fail, _user
from automotive.site_meta_admin import Payload, _parse, _plain, _Strict

LOGGER = logging.getLogger(__name__)

PREFIX = "/api/admin/live"
MAX_ID = 9_223_372_036_854_775_807  # bigint
MAX_VERSION = 2_147_483_647
KeyPath = Annotated[str, Path(pattern=r"^(?:m-[0-9]{1,30}|w-[0-9a-f]{32})$")]
AlertPath = Annotated[int, Path(ge=1, le=MAX_ID)]
User = Annotated[dict, Depends(_user)]


class StatusChange(_Strict):
    """PUT /alerts/{id}: done, or open again."""

    status: Literal["open", "done"]
    version: int | None = Field(default=None, ge=1, le=MAX_VERSION)


class BotChange(_Strict):
    """PUT /chats/{key}/bot: the bot answers this customer (true) or stays silent (false)."""

    enabled: bool
    version: int | None = Field(default=None, ge=1, le=MAX_VERSION)


class Dismiss(_Strict):
    """POST /chats/{key}/waiting/dismiss: the newest stored message the screen showed (the
    chat's ``last_id``); without it, the newest one stored."""

    seen_id: int | None = Field(default=None, ge=1, le=MAX_ID)


class Ready(NamedTuple):
    """The signed-in user (admin or employee) and the store a route works with."""

    user: dict
    live: Any


@contextlib.contextmanager
def _database():
    """A database that cannot be used is a 503 the page shows as "offline", never a 500."""
    try:
        yield
    except (psycopg.Error, OSError) as error:
        LOGGER.warning("live_database_error %s", type(error).__name__)
        raise _fail(503, "LIVE_UNAVAILABLE") from None


def build_live_router(store: Any = None) -> APIRouter:
    """The /api/admin/live routes. ``store`` is a ``LiveStore`` on the site's own login, or a
    double in tests; None (no site database configured) answers 503 ``LIVE_NOT_CONFIGURED``."""
    router = APIRouter(prefix=PREFIX, include_in_schema=False)

    def ready(user: User) -> Ready:
        if store is None:
            raise _fail(503, "LIVE_NOT_CONFIGURED")
        with _database():
            there = store.pages_ready()  # 0015's tables and 0016's column: WAITING reads it
        if not there:
            raise _fail(503, "LIVE_NOT_SET_UP")
        return Ready(user, store)

    Live = Annotated[Ready, Depends(ready)]

    # -- the pulse ---------------------------------------------------------------------------

    @router.get("/pulse")
    def pulse(live: Live,
              after_m: Annotated[int | None, Query(ge=0, le=MAX_ID)] = None,
              after_w: Annotated[int | None, Query(ge=0, le=MAX_ID)] = None,
              detail: Annotated[int, Query(ge=0, le=1)] = 0) -> dict:
        """What every dashboard page asks for: open alarms per kind, the Messenger chats that
        wait for staff or are stuck, the global bot state, the newest alarm change and the newest
        message ids (``cursor``: where to start reading). With ``detail=1`` also the messages
        after ``after_m`` (Messenger) and ``after_w`` (website) that the page does not show yet,
        the chats they belong to, and the open and recently done alarms. A channel without a
        cursor is skipped: the page takes the answer's ``cursor`` as its start."""
        with _database():
            counts = live.live.pulse()
            answer: dict = {"now": datetime.now(UTC), **counts}
            if detail:
                feed = live.live.feed(after_m, after_w)
                answer["cursor"] = {
                    channel: counts["cursor"][channel] if feed["cursor"][channel] is None
                    else feed["cursor"][channel] for channel in ("m", "w")}
                answer["messages"] = feed["messages"]
                answer["chats"] = feed["chats"]
                answer["alerts"] = {"open": live.live.list_alerts("open", 100),
                                    "done": live.live.recent_done(24, 50)}
        return _plain(answer)

    # -- alarms ------------------------------------------------------------------------------

    @router.get("/alerts")
    def alerts(live: Live,
               status: Annotated[str, Query(pattern="^(open|done|all)$")] = "open",
               limit: Annotated[int, Query(ge=1, le=300)] = 100) -> dict:
        with _database():
            rows = live.live.list_alerts(status, limit)
        return _plain({"alerts": rows, "status": status})

    @router.put("/alerts/{alert_id}")
    def change_alert(alert_id: AlertPath, request: Request, live: Live,
                     payload: Payload) -> dict:
        """Mark an alarm done (who and when are stored) or open it again. ``version`` is the one
        last read; a stale one is 409 VERSION_CONFLICT, and an alarm that cannot be reopened
        because another of its chat and kind is open is 409 ALREADY_OPEN."""
        body = _parse(StatusChange, payload)
        with _database():
            try:
                row = live.live.set_status(alert_id, body.status, live.user,
                                           version=body.version, client_ip=_address(request))
            except MetaConflict:
                raise _fail(409, "VERSION_CONFLICT") from None
            except AlreadyOpen:
                raise _fail(409, "ALREADY_OPEN") from None
        if row is None:
            raise _fail(404, "NOT_FOUND")
        return _plain({"alert": row})

    # -- conversations -----------------------------------------------------------------------

    @router.get("/chats")
    def chats(live: Live,
              kind: Annotated[str, Query(alias="filter",
                                         pattern="^(all|alerts|waiting|off|stuck)$")] = "all",
              limit: Annotated[int, Query(ge=1, le=150)] = 40) -> dict:
        with _database():
            rows = live.live.chats(kind, limit)
        return _plain({"chats": rows, "filter": kind})

    @router.get("/chats/{key}")
    def chat(key: KeyPath, live: Live, limit: Annotated[int, Query(ge=1, le=200)] = 50,
             before: Annotated[str | None, Query(pattern=r"^[mw][0-9]{1,18}$")] = None) -> dict:
        """One conversation: its summary, its alarms and its newest messages, oldest first.
        ``before`` is the id of the oldest message the page shows."""
        with _database():
            found = live.live.chat(key, limit, before)
        if found is None:
            raise _fail(404, "NOT_FOUND")
        return _plain(found)

    @router.put("/chats/{key}/bot")
    def bot(key: KeyPath, request: Request, live: Live, payload: Payload) -> dict:
        """The bot's switch for this one customer, for both roles. A Messenger chat's switch is
        the one the worker already honours before every send; a website visitor's is read before
        each of their turns. Audited with the user's name."""
        body = _parse(BotChange, payload)
        with _database():
            try:
                row = live.live.set_bot(key, body.enabled, live.user, version=body.version,
                                        client_ip=_address(request))
            except MetaConflict:
                raise _fail(409, "VERSION_CONFLICT") from None
        if row is None:
            raise _fail(404, "NOT_FOUND")
        return _plain({"chat": row})

    @router.post("/chats/{key}/attention/clear")
    def clear_attention(key: KeyPath, request: Request, live: Live) -> dict:
        """Clear a Messenger chat's "needs attention" flag; nothing clears it by itself."""
        with _database():
            cleared = live.live.clear_attention(key, live.user, client_ip=_address(request))
        if not cleared:
            raise _fail(404, "NOT_FOUND")
        return {"cleared": True}

    @router.post("/chats/{key}/waiting/dismiss")
    def dismiss_waiting(key: KeyPath, request: Request, live: Live, payload: Payload) -> dict:
        """Mark a waiting Messenger chat as handled without writing to the customer (a closing
        "thanks" needs no answer), up to the newest stored message the screen showed
        (``seen_id``): one stored after it keeps the chat waiting ({"waiting": true}). Audited."""
        seen_id = _parse(Dismiss, payload).seen_id
        with _database():
            result = live.live.dismiss_waiting(key, live.user, seen_id=seen_id,
                                               client_ip=_address(request))
        if result is None:
            raise _fail(404, "NOT_FOUND")
        return {"key": key, "waiting": bool(result["waiting"])}

    return router
