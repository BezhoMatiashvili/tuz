"""Page statistics and posts of the Garage page, for the dashboard's Page tab.

``sync_page`` (daily) files the page's follower count under the Tbilisi day and the page's daily
insights under the days Meta reports them for. ``sync_posts`` (hourly) keeps the latest 25 posts
with their reaction, comment and share counts, their lifetime views and a copy of each picture.

Meta replaced impressions and reach with "views" in 2025-26 (Graph API v25: ``page_impressions``
became ``page_media_view``, ``page_impressions_unique`` became ``page_total_media_view_unique``,
``page_fans`` became ``page_follows``, the same for posts), and a retired metric now answers with
an invalid-metric error that fails the whole request. So a metric Meta no longer knows costs only
itself: the request is repeated metric by metric, what works is kept, and the rest is recorded as
unavailable. When ``read_insights`` itself is refused (TNLU met this as a two-factor requirement)
the follower count is still kept and the refusal is recorded, so the dashboard can say what is
missing and why. Status lives under two reserved keys of the ``metrics`` JSON: ``_unavailable``
(metric names) and ``_refused`` (Meta's codes).

Pictures are copied into ``garage.meta_images`` because the dashboard's CSP allows same-origin
images only. ``ImageFetcher`` takes a picture only from Meta's own hosts over HTTPS, follows no
redirect to another host, reads at most 1 MB and keeps it only when its first bytes are the image
type its header claims. The access token never goes to the picture's host.

Every write runs with the worker login (``garage_messenger``); the read helpers at the end run
with the dashboard's (``garage_site``) and return JSON-ready values.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from psycopg.types.json import Jsonb

from automotive.meta_graph import GraphError
from automotive.meta_sync import (
    SyncCount,
    clip,
    count,
    database_of,
    json_ready,
    meta_time,
    object_id,
    safe_link,
    tbilisi_today,
    window_days,
)

LOGGER = logging.getLogger(__name__)

# The current names (v25+). A name Meta drops later is recorded as unavailable, not fatal.
PAGE_METRICS = ("page_media_view", "page_total_media_view_unique", "page_post_engagements",
                "page_follows", "page_daily_follows_unique")
POST_METRICS = ("post_media_view", "post_total_media_view_unique")
POST_FIELDS = ("id,message,created_time,permalink_url,full_picture,shares,"
               "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)")
POSTS_LIMIT = 25
# Each daily run reads the last week again: Meta fills a day in late, and a missed run (the
# laptop slept) is caught up.
INSIGHT_DAYS = 7
MESSAGE_LIMIT = 2000  # meta_posts.message
STATUS_KEYS = ("_unavailable", "_refused")
METRIC_NAME = re.compile(r"^[a-z_]{1,60}$")
IMAGE_KEY = re.compile(r"^[0-9a-f]{64}$")
IMAGE_BYTES = 1_048_576  # meta_images.data
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
IMAGE_HOSTS = ("fbcdn.net", "facebook.com")
IMAGE_REFRESH_DAYS = 7
IMAGE_TIMEOUT = (5.0, 15.0)
MAX_REDIRECTS = 3
URL_LIMIT = 4096
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


# -- what Meta answers -----------------------------------------------------------------------


def refused(error: GraphError) -> bool:
    """Meta refused a permission (read_insights, ads_read) rather than the request itself: code
    10 (no permission, or a security check such as two-factor) or a 200-299 permission code."""
    code = error.code
    return code == 10 or (code is not None and 200 <= code <= 299)


def refusal(error: GraphError) -> dict:
    """What is recorded about a refusal: Meta's codes, never its message."""
    return {"code": error.code, "subcode": error.subcode, "type": str(error.type or "")[:40]}


@dataclass(frozen=True)
class Insights:
    """Meta's insights entries, the requested metrics it does not know, and its refusal."""

    data: list[dict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    refused: dict | None = None


def _entries(answer: Any) -> list[dict]:
    data = answer.get("data") if isinstance(answer, Mapping) else None
    return [entry for entry in data or () if isinstance(entry, dict)
            and isinstance(entry.get("name"), str) and METRIC_NAME.match(entry["name"])]


def read_insights(graph: Any, token: Any, object_id_: str, metrics: Sequence[str],
                  params: Mapping[str, Any] | None = None) -> Insights:
    """``GET {object}/insights`` for ``metrics``, degrading one metric at a time.

    One request asks for every metric. When Meta calls one of them invalid (code 100) the
    request is repeated metric by metric: those that answer are kept, those it still calls
    invalid are listed as unavailable. A permission refusal stops at once and is returned as
    ``refused`` (nothing more is asked). Any other error is raised for the caller."""
    wanted = [name for name in metrics if isinstance(name, str) and METRIC_NAME.match(name)]
    if not wanted:
        return Insights()
    base = dict(params or {})
    path = f"{object_id_}/insights"
    try:
        return Insights(_entries(graph.get(path, token, {**base, "metric": ",".join(wanted)})))
    except GraphError as error:
        if refused(error):
            return Insights(refused=refusal(error))
        if not error.invalid or len(wanted) == 1:
            if error.invalid:
                return Insights(unavailable=wanted)
            raise
    data: list[dict] = []
    unavailable: list[str] = []
    for name in wanted:
        try:
            data.extend(_entries(graph.get(path, token, {**base, "metric": name})))
        except GraphError as error:
            if refused(error):
                return Insights(data, unavailable, refusal(error))
            if error.invalid:
                unavailable.append(name)
                continue
            raise
    return Insights(data, unavailable)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else value
    return value


def page_days(data: Sequence[Mapping[str, Any]]) -> dict[date, dict[str, int | float]]:
    """Daily insights by the day each value covers.

    A day value's ``end_time`` is the end of the day it counts (midnight Pacific time, sent as
    07:00 or 08:00 UTC of the next day), so the day is the date one day before it. Values that
    are not plain numbers (a breakdown object) are left out."""
    days: dict[date, dict[str, int | float]] = {}
    for entry in data:
        name = entry.get("name")
        if not isinstance(name, str) or not METRIC_NAME.match(name):
            continue
        for point in entry.get("values") or ():
            if not isinstance(point, Mapping):
                continue
            value, end = _number(point.get("value")), meta_time(point.get("end_time"))
            if value is not None and end is not None:
                days.setdefault((end - timedelta(days=1)).date(), {})[name] = value
    return days


def lifetime_values(data: Sequence[Mapping[str, Any]]) -> dict[str, int | float]:
    """A post's lifetime insights: each metric's latest plain number."""
    values: dict[str, int | float] = {}
    for entry in data:
        name = entry.get("name")
        points = [point for point in entry.get("values") or () if isinstance(point, Mapping)]
        numbers = [number for point in points
                   if (number := _number(point.get("value"))) is not None]
        if isinstance(name, str) and METRIC_NAME.match(name) and numbers:
            values[name] = numbers[-1]
    return values


def _status(insights: Insights) -> dict:
    status: dict[str, Any] = {}
    if insights.unavailable:
        status["_unavailable"] = sorted(set(insights.unavailable))
    if insights.refused:
        status["_refused"] = insights.refused
    return status


def _summary_count(value: Any) -> int:
    summary = value.get("summary") if isinstance(value, Mapping) else None
    found = count(summary.get("total_count")) if isinstance(summary, Mapping) else None
    return found or 0


def parse_post(item: Any) -> dict | None:
    """One ``{page}/posts`` item as a ``meta_posts`` row (plus ``picture_url``); None when it
    has no usable id or time."""
    if not isinstance(item, Mapping):
        return None
    post_id, created = object_id(item.get("id")), meta_time(item.get("created_time"))
    if not post_id or created is None:
        return None
    shares = item.get("shares")
    picture = item.get("full_picture")
    return {"post_id": post_id, "created_time": created,
            "message": clip(item.get("message"), MESSAGE_LIMIT),
            "permalink": safe_link(item.get("permalink_url")),
            "picture_url": picture if isinstance(picture, str) and len(picture) <= URL_LIMIT
            else "",
            "reactions": _summary_count(item.get("reactions")),
            "comments": _summary_count(item.get("comments")),
            "shares": (count(shares.get("count")) if isinstance(shares, Mapping) else None) or 0}


# -- pictures ---------------------------------------------------------------------------------


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False


def _image_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxy or .netrc from the environment
    session.cookies.set_policy(_NoCookies())
    session.headers.update({"User-Agent": "garage-1886-meta/1.0",
                            "Accept": ", ".join(IMAGE_TYPES), "Accept-Encoding": "identity"})
    return session


def refuse_url(url: Any) -> str:
    """Why a picture address is not fetched ('' when it may be): only https on Meta's own
    hosts (``*.fbcdn.net``, ``*.facebook.com``), the standard port, no credentials."""
    if not isinstance(url, str) or not url or len(url) > URL_LIMIT:
        return "no_url"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed"
    if parts.scheme != "https":
        return "not_https"
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == name or host.endswith("." + name) for name in IMAGE_HOSTS):
        return "host"
    if port not in (None, 443) or parts.username or parts.password:
        return "address"
    return ""


def sniff(head: bytes) -> str:
    """The image type the first bytes of a file prove, or ''."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


class ImageFetcher:
    """Fetches one picture under the rules above; ``fetch`` returns (content type, bytes) or
    None, and logs a reason code (never the address, whose query is a signed link)."""

    def __init__(self, session: Any = None, *, max_bytes: int = IMAGE_BYTES,
                 timeout: tuple[float, float] = IMAGE_TIMEOUT) -> None:
        self._session = session if session is not None else _image_session()
        self.max_bytes = int(max_bytes)
        self.timeout = timeout

    def fetch(self, url: Any) -> tuple[str, bytes] | None:
        target = url
        for _ in range(MAX_REDIRECTS + 1):
            reason = refuse_url(target)
            if reason:
                return self._refused(reason)
            try:
                response = self._session.get(target, timeout=self.timeout,
                                             allow_redirects=False, stream=True)
            except requests.RequestException as error:
                return self._refused(f"network_{type(error).__name__}")
            try:
                status = int(response.status_code)
                if status in _REDIRECTS:
                    location = response.headers.get("Location") or ""
                    target = urljoin(target, location) if isinstance(location, str) else ""
                    continue  # the next round checks the new address like the first
                return self._read(response, status)
            except requests.RequestException as error:
                return self._refused(f"network_{type(error).__name__}")
            finally:
                response.close()
        return self._refused("redirects")

    def _read(self, response: Any, status: int) -> tuple[str, bytes] | None:
        if status != 200:
            return self._refused(f"http_{status}")
        declared = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if declared not in IMAGE_TYPES:
            return self._refused("content_type")
        length = str(response.headers.get("Content-Length") or "").strip()
        if length.isdigit() and int(length) > self.max_bytes:
            return self._refused("too_large")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > self.max_bytes:
                return self._refused("too_large")
        if not body:
            return self._refused("empty")
        if sniff(bytes(body[:16])) != declared:
            return self._refused("not_the_declared_type")
        return declared, bytes(body)

    @staticmethod
    def _refused(reason: str) -> None:
        LOGGER.info("meta_image_skipped reason=%s", reason)
        return None

    def close(self) -> None:
        """Release the session's connections (a job closes the fetcher it made itself)."""
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


def image_key(identity: str) -> str:
    """The key of an object's picture: SHA-256 of what the picture belongs to (``post:<id>``,
    ``ad_creative:<id>``), so a post keeps one cached picture however Meta's link changes."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def cache_image(db: Any, identity: str, url: Any, fetcher: ImageFetcher) -> str | None:
    """The cached picture's key for ``identity``, fetching ``url`` when the copy is missing or
    older than a week (worker login). A failed fetch keeps an older copy; None when there is
    none. Meta's picture links expire, so the link is used at once and never stored."""
    database = database_of(db)
    key = image_key(identity)
    with database.transaction() as conn:
        row = conn.execute(
            "SELECT fetched_at > now() - make_interval(days => %s) AS fresh "
            "FROM garage.meta_images WHERE image_key = %s",
            (IMAGE_REFRESH_DAYS, key)).fetchone()
    if row is not None and row["fresh"]:
        return key
    got = fetcher.fetch(url) if url else None
    if got is None:
        return key if row is not None else None
    content_type, data = got
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO garage.meta_images (image_key, content_type, data) VALUES (%s, %s, %s) "
            "ON CONFLICT (image_key) DO UPDATE SET content_type = EXCLUDED.content_type, "
            "data = EXCLUDED.data, fetched_at = now()", (key, content_type, data))
    return key


# -- the jobs ----------------------------------------------------------------------------------


def _mark_dead(ctx: Any, purpose: str, error: GraphError) -> None:
    if error.token_dead:
        ctx.store.mark_token(purpose, "broken", f"meta_code_{error.code}")


def sync_page(ctx: Any) -> SyncCount:
    """Followers into today's row; the last week's daily insights into their days (worker).

    Reads with the page token. Returns the rows written, with a detail naming a refusal or
    the unavailable metrics. A dead token is marked broken and the error raised."""
    page_id, token = ctx.page_id, ctx.page_token()
    now = ctx.now()
    failed = ""
    try:
        info = ctx.graph.get(page_id, token, {"fields": "followers_count,fan_count,name"})
    except GraphError as error:
        _mark_dead(ctx, "page", error)
        raise
    try:
        insights = read_insights(ctx.graph, token, page_id, PAGE_METRICS, {
            "period": "day", "since": int((now - timedelta(days=INSIGHT_DAYS)).timestamp()),
            "until": int(now.timestamp())})
    except GraphError as error:
        _mark_dead(ctx, "page", error)
        if error.token_dead:
            raise
        # Throttled or down after the client's retries: the followers are kept anyway, and
        # tomorrow's run reads the week again.
        LOGGER.info("meta_page_insights_failed code=%s http=%s", error.code, error.http_status)
        insights, failed = Insights(), f"insights_error code={error.code}"
    fans = count(info.get("fan_count"))
    followers = count(info.get("followers_count"))
    followers = followers if followers is not None else fans
    today = tbilisi_today(now)
    days = page_days(insights.data)
    today_metrics = {**days.pop(today, {}), **_status(insights)}
    if fans is not None:
        today_metrics["fan_count"] = fans
    statement = (
        "INSERT INTO garage.meta_page_daily (day, followers, metrics) "
        "VALUES (%(day)s, %(followers)s, %(metrics)s) ON CONFLICT (day) DO UPDATE SET "
        "followers = COALESCE(EXCLUDED.followers, garage.meta_page_daily.followers), "
        "metrics = (CASE WHEN %(status)s THEN garage.meta_page_daily.metrics - '_unavailable' "
        "- '_refused' ELSE garage.meta_page_daily.metrics END) || EXCLUDED.metrics, "
        "fetched_at = now()")
    with database_of(ctx).transaction() as conn:
        for day, metrics in sorted(days.items()):
            conn.execute(statement, {"day": day, "followers": None, "metrics": Jsonb(metrics),
                                     "status": False})
        # After a failed read the stored status stays: a failure is no news about permissions.
        conn.execute(statement, {"day": today, "followers": followers,
                                 "metrics": Jsonb(today_metrics), "status": not failed})
    detail = [f"followers={'none' if followers is None else followers}",
              f"days={len(days)}"]
    if insights.refused:
        detail.append(f"insights_refused code={insights.refused['code']}")
        LOGGER.info("meta_page_insights_refused code=%s subcode=%s", insights.refused["code"],
                    insights.refused["subcode"])
    if insights.unavailable:
        detail.append("unavailable=" + ",".join(sorted(set(insights.unavailable))))
    if failed:
        detail.append(failed)
    return SyncCount(len(days) + 1, "ok", " ".join(detail))


def sync_posts(ctx: Any, *, fetcher: ImageFetcher | None = None) -> SyncCount:
    """The latest 25 posts with counts, lifetime views and pictures (worker, hourly).

    A metric found unavailable, or a refusal, on one post is not asked again for the next ones
    in the same run. One post whose insights fail otherwise keeps its earlier numbers."""
    page_id, token = ctx.page_id, ctx.page_token()
    try:
        answer = ctx.graph.get(f"{page_id}/posts", token,
                               {"fields": POST_FIELDS, "limit": POSTS_LIMIT})
    except GraphError as error:
        _mark_dead(ctx, "page", error)
        raise
    posts = [post for post in (parse_post(item) for item in (answer.get("data") or ())[
        :POSTS_LIMIT]) if post]
    if fetcher is not None:
        return _store_posts(ctx, token, posts, fetcher)
    own = ImageFetcher()
    try:
        return _store_posts(ctx, token, posts, own)
    finally:
        own.close()


def _store_posts(ctx: Any, token: Any, posts: list[dict], fetcher: ImageFetcher) -> SyncCount:
    database = database_of(ctx)
    wanted = list(POST_METRICS)
    unavailable: set[str] = set()
    blocked: dict | None = None
    pictures = 0
    for post in posts:
        metrics: dict[str, Any] = {}
        status = False
        if blocked is None and wanted:
            try:
                insights = read_insights(ctx.graph, token, post["post_id"], wanted,
                                         {"period": "lifetime"})
            except GraphError as error:
                _mark_dead(ctx, "page", error)
                if error.token_dead:
                    raise
                LOGGER.info("meta_post_insights_skipped code=%s http=%s", error.code,
                            error.http_status)
            else:
                unavailable.update(insights.unavailable)
                wanted = [name for name in wanted if name not in unavailable]
                blocked = insights.refused
                metrics = lifetime_values(insights.data)
                status = True
        if blocked is not None or unavailable:
            status = True
        if status:
            metrics.update(_status(Insights([], sorted(unavailable), blocked)))
        key = cache_image(database, f"post:{post['post_id']}", post["picture_url"], fetcher)
        pictures += key is not None
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO garage.meta_posts (post_id, created_time, message, permalink, "
                "image_key, reactions, comments, shares, metrics) VALUES (%(post_id)s, "
                "%(created_time)s, %(message)s, %(permalink)s, %(image_key)s, %(reactions)s, "
                "%(comments)s, %(shares)s, %(metrics)s) ON CONFLICT (post_id) DO UPDATE SET "
                "created_time = EXCLUDED.created_time, message = EXCLUDED.message, "
                "permalink = CASE WHEN EXCLUDED.permalink <> '' THEN EXCLUDED.permalink "
                "ELSE garage.meta_posts.permalink END, "
                "image_key = COALESCE(EXCLUDED.image_key, garage.meta_posts.image_key), "
                "reactions = EXCLUDED.reactions, comments = EXCLUDED.comments, "
                "shares = EXCLUDED.shares, metrics = (CASE WHEN %(status)s THEN "
                "garage.meta_posts.metrics - '_unavailable' - '_refused' "
                "ELSE garage.meta_posts.metrics END) || EXCLUDED.metrics, updated_at = now()",
                {**post, "image_key": key, "metrics": Jsonb(metrics), "status": status})
    detail = [f"posts={len(posts)}", f"pictures={pictures}"]
    if blocked:
        detail.append(f"insights_refused code={blocked['code']}")
    if unavailable:
        detail.append("unavailable=" + ",".join(sorted(unavailable)))
    return SyncCount(len(posts), "ok", " ".join(detail))


# -- the dashboard's reads (site login) --------------------------------------------------------

def followers_series(db: Any, days: int = 30) -> list[dict]:
    """``[{day, followers}]`` for the last ``days`` Tbilisi days that have a count, oldest
    first."""
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "SELECT day, followers FROM garage.meta_page_daily WHERE followers IS NOT NULL "
            "AND day >= (now() AT TIME ZONE 'Asia/Tbilisi')::date - %(days)s::int + 1 "
            "ORDER BY day", {"days": window_days(days)}).fetchall()
    return json_ready([dict(row) for row in rows])


def metric_series(db: Any, metric: str, days: int = 30) -> list[dict]:
    """``[{day, value}]`` of one daily page metric (e.g. ``page_media_view``), oldest first.
    Empty for an unknown or unavailable metric (``insights_status`` says which)."""
    if not isinstance(metric, str) or not METRIC_NAME.match(metric) or metric.startswith("_"):
        raise ValueError("Unknown metric name")
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "SELECT day, metrics -> %(metric)s AS value FROM garage.meta_page_daily "
            "WHERE jsonb_typeof(metrics -> %(metric)s) = 'number' "
            "AND day >= (now() AT TIME ZONE 'Asia/Tbilisi')::date - %(days)s::int + 1 "
            "ORDER BY day",
            {"metric": metric, "days": window_days(days)}).fetchall()
    return json_ready([dict(row) for row in rows])


def insights_status(db: Any) -> dict:
    """What the latest syncs could read, for the Page card: ``page`` (day, followers,
    fetched_at, refused, unavailable) and ``posts`` (updated_at, refused, unavailable); each
    None before its first run. ``refused`` holds Meta's codes, e.g. ``{"code": 10, ...}``."""
    with database_of(db).transaction() as conn:
        page = conn.execute(
            "SELECT day, followers, fetched_at, metrics -> '_refused' AS refused, "
            "metrics -> '_unavailable' AS unavailable FROM garage.meta_page_daily "
            "WHERE metrics ? 'fan_count' OR followers IS NOT NULL OR metrics ? '_refused' "
            "ORDER BY fetched_at DESC, day DESC LIMIT 1").fetchone()
        posts = conn.execute(
            "SELECT updated_at, metrics -> '_refused' AS refused, "
            "metrics -> '_unavailable' AS unavailable FROM garage.meta_posts "
            "ORDER BY updated_at DESC LIMIT 1").fetchone()

    def shaped(row: Any) -> dict | None:
        if row is None:
            return None
        found = dict(row)
        found["unavailable"] = [name for name in found.get("unavailable") or ()
                                if isinstance(name, str)]
        found["refused"] = found.get("refused") if isinstance(found.get("refused"), dict) \
            else None
        return found

    return json_ready({"page": shaped(page), "posts": shaped(posts)})


def top_posts(db: Any, days: int = 30, limit: int = 10) -> list[dict]:
    """The page's posts of the last ``days`` days, most engaging first (reactions, comments
    and shares together, then views): post_id, created_time, message (300 characters),
    permalink, image_key, reactions, comments, shares, engagement, views, viewers (views and
    viewers are None where Meta gave none)."""
    try:
        size = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        size = 10
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "SELECT post_id, created_time, left(message, 300) AS message, permalink, "
            "image_key, reactions, comments, shares, reactions + comments + shares "
            "AS engagement, CASE WHEN jsonb_typeof(metrics -> 'post_media_view') = 'number' "
            "THEN (metrics ->> 'post_media_view')::numeric END AS views, "
            "CASE WHEN jsonb_typeof(metrics -> 'post_total_media_view_unique') = 'number' "
            "THEN (metrics ->> 'post_total_media_view_unique')::numeric END AS viewers "
            "FROM garage.meta_posts WHERE created_time >= now() - make_interval(days => %s) "
            "ORDER BY engagement DESC, views DESC NULLS LAST, created_time DESC LIMIT %s",
            (window_days(days), size)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for name in ("views", "viewers"):
            if item[name] is not None:
                item[name] = int(item[name]) if item[name] == int(item[name]) \
                    else float(item[name])
        result.append(item)
    return json_ready(result)


def image(db: Any, key: Any) -> tuple[str, bytes] | None:
    """A cached picture for the dashboard's same-origin image route: (content type, bytes),
    or None for a key that is malformed or unknown."""
    if not isinstance(key, str) or not IMAGE_KEY.match(key):
        return None
    with database_of(db).transaction() as conn:
        row = conn.execute("SELECT content_type, data FROM garage.meta_images "
                           "WHERE image_key = %s", (key,)).fetchone()
    return (row["content_type"], bytes(row["data"])) if row else None
