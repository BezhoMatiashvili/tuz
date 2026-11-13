"""Dashboard API for what happens on the Garage Facebook page: comments, statistics, posts, ads.

``site.install_site`` mounts this router under ``/api/admin/facebook``, next to
``site_meta_admin``'s (switches, connection, chats). The site guard has already required a staff
session for every route, and an allow-listed Origin plus the CSRF header for the two writes (its
``/api/admin/`` rule; a GET needs only the session, which is how an ``<img>`` loads a picture).
Every route then requires the admin role (``site_admin._admin``) before it looks at the
configuration or the database, so an employee is refused with 403 even while the section is off.

Reads use the feature modules' dashboard helpers (``meta_comments``, ``meta_page_stats``,
``meta_ads``) on the site login (``AUTOMOTIVE_SITE_DATABASE_URL``, a member of ``garage_site``).
Migration 0013 lets that login read comments, page statistics, posts, cached pictures, ads and
the webhook inbox's bookkeeping columns, and change only a comment's hidden flag and public-reply
id. The reads of one route share one connection and one transaction, so a page of figures costs
one login and its numbers agree with each other.

The two writes act on Facebook first (``meta_comments.set_hidden`` and ``public_reply``), and
those functions audit themselves, refusals included. A public reply is never retried: when Meta's
answer was lost on the way (``maybe_applied``) the reply may already be under the comment, and the
dashboard says so instead of offering to post it twice.

Pictures come from the cache the worker fills (``meta_images``): only the four image types that
cache admits, with nosniff and the site's CSP, and never cached by the browser (the default for
every staff answer; the guard's cache policies are all public).

The Meta block is read from the process environment, as ``site_meta_admin`` reads it. While it is
incomplete every route answers 503 META_NOT_CONFIGURED; that router's ``GET /status`` names the
missing variables.
"""

# No ``from __future__ import annotations`` here: the routes depend on closures over this
# router's context, and FastAPI resolves string annotations against module globals only.

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from automotive import meta_ads, meta_comments, meta_page_stats
from automotive.meta_comments import CommentActionError
from automotive.meta_config import MetaConfigError, MetaSettings
from automotive.meta_context import MetaContext, utc_now
from automotive.meta_crypto import TokenCipher, TokenCipherError
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphClient
from automotive.meta_store import MetaStore
from automotive.meta_sync import json_ready, safe_link
from automotive.site_admin import _address, _admin, _fail

PREFIX = "/api/admin/facebook"
# The ranges the Page and Ads tabs offer, in Tbilisi days.
WINDOWS = (7, 30, 90)
EVENTS_SHOWN, EVENTS_MAX = 50, 200
COMMENTS_SHOWN, COMMENTS_MAX = 30, 100
POSTS_SHOWN, POSTS_MAX = 10, 50
# "Best ads": at least this much spent in the ad's own currency, and at least one chat.
MIN_SPEND = 10.0
BEST_SHOWN = 10
REPLY_LIMIT = meta_comments.PUBLIC_REPLY_LIMIT
IMAGE_TYPES = frozenset(meta_page_stats.IMAGE_TYPES)
IMAGE_KEY = re.compile(r"^[0-9a-f]{64}$")
# Text the shop posts under a comment must not carry control characters; tab and line breaks
# are fine.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Defence in depth for error details: the Graph client already removes the call's secrets.
BARE_TOKEN = re.compile(r"EAA[A-Za-z0-9]{12,}")
ERROR_CODES = {"extra_forbidden": "UNKNOWN", "missing": "REQUIRED",
               "string_too_short": "REQUIRED", "string_too_long": "TOO_LONG"}
# CommentActionError code -> HTTP status. Meta's refusals and lost answers are the upstream's
# (502); a dead page token, like a missing connection, needs the admin to reconnect (409).
ACTION_STATUS = {"NOT_FOUND": 404, "NOT_CONNECTED": 409, "TOKEN_DEAD": 409,
                 "META_REFUSED": 502, "NETWORK": 502}

CommentPath = Annotated[str, Path(pattern=r"^[0-9]{1,40}(?:_[0-9]{1,40}){0,3}$")]
ImagePath = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
Days = Annotated[int, Query(ge=1, le=400)]


# -- request bodies ----------------------------------------------------------------------------

async def _json_object(request: Request) -> dict[str, Any]:
    """The JSON object a write reads. Parsed here, so no error ever repeats what was sent."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _fail(422, "INVALID_REQUEST") from None
    if not isinstance(data, dict):
        raise _fail(422, "INVALID_REQUEST")
    return data


Payload = Annotated[dict[str, Any], Depends(_json_object)]
AdminUser = Annotated[dict, Depends(_admin)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class HideBody(_Strict):
    hidden: bool


class ReplyBody(_Strict):
    """The admin's public reply under a comment, as typed (meta_comments.PUBLIC_REPLY_LIMIT)."""

    text: str = Field(min_length=1, max_length=REPLY_LIMIT)

    @field_validator("text")
    @classmethod
    def _printable(cls, value: str) -> str:
        if CONTROL.search(value):
            raise ValueError("control characters")
        return value


def _parse(model: type[BaseModel], payload: Mapping[str, Any]) -> Any:
    """The validated body, or 422 INVALID_FIELDS naming each field by a code, never a value."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        codes: dict[str, str] = {}
        for item in error.errors(include_url=False, include_context=False,
                                 include_input=False):
            location = item.get("loc") or ("",)
            codes.setdefault(str(location[0])[:60],
                             ERROR_CODES.get(str(item.get("type") or ""), "INVALID"))
        raise _fail(422, "INVALID_FIELDS", codes) from None


def _window(days: int) -> int:
    if days not in WINDOWS:
        raise _fail(422, "INVALID_FIELDS", {"days": "INVALID"})
    return days


# -- answers -----------------------------------------------------------------------------------

def _image_url(key: Any) -> str | None:
    """Where the dashboard loads a cached picture: this router's same-origin image route."""
    return f"{PREFIX}/images/{key}" if isinstance(key, str) and IMAGE_KEY.match(key) else None


def _post_view(row: Mapping[str, Any]) -> dict:
    return {**row, "permalink": safe_link(row.get("permalink")),
            "image_url": _image_url(row.get("image_key"))}


def _ad_view(row: Mapping[str, Any]) -> dict:
    return {**row, "ads_manager_url": safe_link(row.get("ads_manager_url")),
            "image_url": _image_url(row.get("image_key"))}


def _comment_view(row: Mapping[str, Any]) -> dict:
    # Rendered as links in the dashboard: only Facebook's own https addresses get through.
    return {**row, "link": safe_link(row.get("link")),
            "permalink": safe_link(row.get("permalink")),
            "post_permalink": safe_link(row.get("post_permalink"))}


def _event_state(row: Mapping[str, Any]) -> str:
    """What became of a webhook item (meta_store's inbox lease): waiting for the worker, being
    handled now, put back for another try, done, or given up."""
    outcome = str(row.get("outcome") or "")
    if outcome == "claimed":
        return "working"
    if outcome.startswith("failed:"):
        return "failed"
    if row.get("processed_at") is None:
        return "retrying" if outcome.startswith("retry:") else "waiting"
    return "done"


def _numbers(series: list[Mapping[str, Any]], key: str) -> list[float]:
    return [float(item[key]) for item in series
            if isinstance(item.get(key), int | float) and not isinstance(item.get(key), bool)]


def _whole(value: float) -> int | float:
    return int(value) if value == int(value) else value


def _page_summary(followers: list[dict], metrics: Mapping[str, list[dict]]) -> dict:
    """The Page tab's headline figures: followers now and their change over the range (from
    the first day with a count), and per metric its total over the range and its latest day.
    Which one means something is the metric's: views add up, a running count does not."""
    counts = _numbers(followers, "followers")
    totals, latest = {}, {}
    for name, series in metrics.items():
        values = _numbers(series, "value")
        totals[name] = _whole(sum(values)) if values else None
        latest[name] = _whole(values[-1]) if values else None
    return {"followers": int(counts[-1]) if counts else None,
            "followers_change": int(counts[-1] - counts[0]) if len(counts) > 1 else None,
            "totals": totals, "latest": latest}


def _scrubbed(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return json.loads(BARE_TOKEN.sub("[redacted]", text)) if BARE_TOKEN.search(text) else value


def _refused(error: CommentActionError) -> HTTPException:
    """A comment action's failure as the dashboard's error: Meta's codes and scrubbed message
    under ``fields.meta``, and ``fields.maybe_applied`` when the change may have happened."""
    detail = dict(error.detail)
    if error.code == "INVALID":
        return _fail(422, "INVALID_FIELDS", {str(detail.get("field") or "")[:40]: "INVALID"})
    if error.code == "NOT_CONNECTED" and detail.get("reason") == "TokenCipherError":
        return _fail(409, "TOKEN_UNREADABLE")
    fields: dict[str, Any] = {}
    if isinstance(detail.get("meta"), Mapping):
        fields["meta"] = _scrubbed(json_ready(dict(detail["meta"])))
    if "maybe_applied" in detail:
        fields["maybe_applied"] = bool(detail["maybe_applied"])
    return _fail(ACTION_STATUS.get(error.code, 502), error.code, fields or None)


# -- configuration ---------------------------------------------------------------------------

class _Connection:
    """One open connection standing in for a MetaDatabase, for the feature modules' read
    helpers: each helper's ``transaction()`` becomes a savepoint in the route's transaction."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._connection.transaction():
            yield self._connection


@contextmanager
def _reading(ctx: MetaContext) -> Iterator[_Connection]:
    """The route's reads on one connection of the site login, in one transaction."""
    with ctx.store.db.transaction() as connection:
        yield _Connection(connection)


def _context(site_settings: Any, store: Any, graph: Any, meta_settings: MetaSettings | None,
             clock: Callable[[], datetime] | None) -> MetaContext | None:
    """The integration this router works with, or None while the Meta block or the site login
    is incomplete. Never raises: an incomplete block switches the routes off."""
    meta = meta_settings
    if meta is None:
        try:
            # The environment only: Settings.from_env has loaded .env already, and a process
            # that never did (a test) must not have the real file loaded behind its back.
            meta = MetaSettings.from_env(os.environ)
        except MetaConfigError:
            return None
    url = str(getattr(site_settings, "site_database_url", "") or "")
    if meta.missing_for_dashboard() or (store is None and not url):
        return None
    try:
        cipher = TokenCipher(meta.token_key)
    except TokenCipherError:
        return None
    if store is None:
        store = MetaStore(MetaDatabase(url, application_name="garage-site-facebook"), cipher)
    if graph is None:
        try:
            graph = GraphClient(meta)
        except MetaConfigError:
            return None
    return MetaContext(meta, store, graph, cipher, now=clock or utc_now)


class Ready(NamedTuple):
    """The signed-in admin and the integration a route works with."""

    user: dict
    ctx: MetaContext


# -- routes ------------------------------------------------------------------------------------

def build_meta_activity_router(site_settings: Any, *, store: Any = None, graph: Any = None,
                               meta_settings: MetaSettings | None = None,
                               clock: Callable[[], datetime] | None = None) -> APIRouter:
    """The comments, page-statistics, posts, ads, picture and webhook-event routes, admin
    only. ``store``, ``graph``, ``meta_settings`` and ``clock`` (an aware "now") replace the real
    ones in tests; by default the Meta block comes from the environment and the store uses the
    site login. Never raises."""
    router = APIRouter(prefix=PREFIX, include_in_schema=False)
    ctx = _context(site_settings, store, graph, meta_settings, clock)

    def ready(user: AdminUser) -> Ready:
        if ctx is None:
            raise _fail(503, "META_NOT_CONFIGURED")
        return Ready(user, ctx)

    Admin = Annotated[Ready, Depends(ready)]

    # -- webhook events ------------------------------------------------------------------------

    @router.get("/events")
    def events(admin: Admin,
               limit: Annotated[int, Query(ge=1, le=EVENTS_MAX)] = EVENTS_SHOWN) -> dict:
        """The newest webhook items, bookkeeping only (the site login cannot read a payload):
        field, times, attempts, the worker's outcome code and what became of each."""
        rows = admin.ctx.store.recent_events(limit)
        return json_ready({"events": [{**row, "state": _event_state(row)} for row in rows],
                           "checked_at": admin.ctx.now()})

    # -- comments ------------------------------------------------------------------------------

    @router.get("/comments")
    def comments(admin: Admin,
                 limit: Annotated[int, Query(ge=1, le=COMMENTS_MAX)] = COMMENTS_SHOWN,
                 offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
                 unreplied: bool = False) -> dict:
        """Customers' comments, newest first, with the bot's private-reply status and the
        comment switch, so the tab can say why nothing was answered."""
        with _reading(admin.ctx) as db:
            listing = meta_comments.list_comments(db, limit, offset, unreplied)
        settings = admin.ctx.store.settings()
        shown = [_comment_view(row) for row in listing["comments"]]
        # The Unreplied list holds only the unreplied comments, so it ends at their count.
        listed = listing["unreplied"] if unreplied else listing["total"]
        return json_ready({
            **listing, "comments": shown,
            "has_more": listing["offset"] + len(shown) < listed,
            "switch": {key: settings.get(key) for key in ("comments_enabled",
                                                          "comments_enabled_at",
                                                          "bot_paused_reason")}})

    @router.post("/comments/{comment_id}/hide")
    def hide_comment(comment_id: CommentPath, request: Request, admin: Admin,
                     payload: Payload) -> dict:
        """Hide or unhide a comment on Facebook, then here (audited by meta_comments)."""
        body = _parse(HideBody, payload)
        try:
            return meta_comments.set_hidden(admin.ctx, comment_id, body.hidden, admin.user,
                                            client_ip=_address(request))
        except CommentActionError as error:
            raise _refused(error) from None

    @router.post("/comments/{comment_id}/reply")
    def reply_to_comment(comment_id: CommentPath, request: Request, admin: Admin,
                         payload: Payload) -> dict:
        """Post the admin's public reply under a comment, once (audited by its length)."""
        body = _parse(ReplyBody, payload)
        try:
            return meta_comments.public_reply(admin.ctx, comment_id, body.text, admin.user,
                                              client_ip=_address(request))
        except CommentActionError as error:
            raise _refused(error) from None

    # -- the page --------------------------------------------------------------------------------

    @router.get("/page-stats")
    def page_stats(admin: Admin, days: Days = 30) -> dict:
        """Followers per day, the daily page metrics Meta gave, what the last syncs could read
        (``status``: a refusal or unavailable metrics), and the headline figures."""
        window = _window(days)
        with _reading(admin.ctx) as db:
            followers = meta_page_stats.followers_series(db, window)
            metrics = {name: meta_page_stats.metric_series(db, name, window)
                       for name in meta_page_stats.PAGE_METRICS}
            status = meta_page_stats.insights_status(db)
        return json_ready({"days": window, "followers": followers, "metrics": metrics,
                           "status": status, "summary": _page_summary(followers, metrics)})

    @router.get("/posts")
    def posts(admin: Admin, days: Days = 30,
              limit: Annotated[int, Query(ge=1, le=POSTS_MAX)] = POSTS_SHOWN) -> dict:
        """The range's posts, most engaging first, each with its cached picture's address."""
        window = _window(days)
        with _reading(admin.ctx) as db:
            rows = meta_page_stats.top_posts(db, window, limit)
        return json_ready({"days": window, "posts": [_post_view(row) for row in rows]})

    @router.get("/images/{key}")
    def image(key: ImagePath, admin: Admin) -> Response:
        """A cached post picture or ad thumbnail. Only the cache's own image types are served
        (never SVG or anything a browser could run), with nosniff and no cross-origin use."""
        found = meta_page_stats.image(admin.ctx, key)
        if found is None or found[0] not in IMAGE_TYPES:
            raise _fail(404, "NOT_FOUND")
        content_type, data = found
        return Response(bytes(data), media_type=content_type, headers={
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin"})

    # -- ads -------------------------------------------------------------------------------------

    @router.get("/ads")
    def ads(admin: Admin, days: Days = 30) -> dict:
        """Garage's ads over the range (read-only), the cheapest chats first under ``best``,
        and the ad accounts the last sync could read. Money stays in each ad's own currency."""
        window = _window(days)
        with _reading(admin.ctx) as db:
            results = meta_ads.ad_results(db, window)
            best = meta_ads.best_ads(db, window, MIN_SPEND, BEST_SHOWN)
            accounts = meta_ads.ad_accounts(db)
        return json_ready({
            "days": window, "ads": [_ad_view(row) for row in results],
            "best": [_ad_view(row) for row in best], "accounts": accounts,
            "min_spend": MIN_SPEND,
            "currencies": sorted({row.get("currency") or "" for row in results} - {""})})

    @router.get("/ads/daily")
    def ads_daily(admin: Admin, days: Days = 30) -> dict:
        """Spend, impressions, clicks and chats started per day and currency."""
        window = _window(days)
        with _reading(admin.ctx) as db:
            rows = meta_ads.daily_spend(db, window)
        return json_ready({"days": window, "daily": rows})

    return router
