"""Results of the Garage page's own ads, read-only, for the dashboard's Ads tab.

The owner wants to see which Garage ads bring chats, not to run ads from here. So this module
only ever reads (``GET`` through ``GraphClient.get``/``paged``): it never pauses, edits, creates
or spends, and a test keeps every other Graph call out of this file.

``sync_ads`` (every 3 hours) scans every ad account the ads token can read (the owner's login,
or a separate ads app's) for ads that promote the Garage page: the creative's page, the page of
the post it boosts, or the ad set's promoted page (``meta_connect.promotes_page``). Ads of other
businesses in the same accounts (TNLU's) are counted out and never stored; only the account rows
are kept, with how many Garage ads each holds, so the dashboard can show where Garage ads run.
For the Garage ads it stores names, status, text and a cached thumbnail, then the daily results:
spend, impressions, reach, clicks and Meta's count of Messenger conversations the ad started
(``onsite_conversion.messaging_conversation_started_7d``). The first run reads 90 days, later
runs the last 30 again, because Meta restates results for up to about four weeks. Days are the
ad account's own (Meta buckets them in its time zone).

The dashboard's reads put Meta's chats next to the chats our bot handled from each ad
(``messenger_threads.source_ad_id``, from the referral of the customer's first message).
Spend is in each account's currency; sums never mix currencies.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

from automotive.meta_connect import promotes_page
from automotive.meta_context import NotConnected
from automotive.meta_crypto import TokenCipherError
from automotive.meta_graph import GraphError
from automotive.meta_page_stats import ImageFetcher, cache_image, refused
from automotive.meta_sync import (
    DIGITS,
    SyncCount,
    clip,
    count,
    database_of,
    json_ready,
    meta_time,
    object_id,
    window_days,
    zone,
)

LOGGER = logging.getLogger(__name__)

ACCOUNT_FIELDS = "account_id,name,currency,timezone_name,account_status"
AD_FIELDS = ("id,name,status,effective_status,created_time,campaign{id,name,objective},"
             "adset{id,name,promoted_object},creative{id,title,body,thumbnail_url,"
             "effective_object_story_id,object_story_spec}")
INSIGHT_FIELDS = "ad_id,date_start,spend,impressions,reach,clicks,actions"
CHAT_ACTION = "onsite_conversion.messaging_conversation_started_7d"
FIRST_DAYS = 90
ROLLING_DAYS = 30
IDS_PER_REQUEST = 50
ACCOUNT_PAGES = 5
AD_PAGES = 10
INSIGHT_PAGES = 20
INSIGHT_LIMIT = 200
ACTION_LIMIT = 60
RESULT_LIMIT = 200
CENT = Decimal("0.01")
MAX_SPEND = Decimal("999999999999.99")  # meta_ad_daily.spend NUMERIC(14, 2)
MAX_INT = 2_147_483_647
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ACTION_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
# Text of a creative made in Ads Manager, by the kind of story it builds.
_SPEC_TEXTS = (("link_data", "name", "message"), ("video_data", "title", "message"),
               ("photo_data", "", "caption"), ("template_data", "name", "message"))


# -- what Meta answers -----------------------------------------------------------------------


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def parse_account(item: Any) -> dict | None:
    """One ``me/adaccounts`` item as a ``meta_ad_accounts`` row; None without a numeric id."""
    item = _mapping(item)
    account_id = object_id(item.get("account_id"), DIGITS)
    if not account_id:
        return None
    currency = item.get("currency")
    status = item.get("account_status")
    return {"account_id": account_id, "name": clip(item.get("name"), 200),
            "currency": currency if isinstance(currency, str) and _CURRENCY.match(currency)
            else "",
            "timezone_name": clip(item.get("timezone_name"), 80),
            "account_status": status if isinstance(status, int)
            and not isinstance(status, bool) else None}


def parse_ad(item: Any, account_id: str) -> dict | None:
    """One ``act_<id>/ads`` item (fields ``AD_FIELDS``) as a ``meta_ads`` row, plus
    ``thumbnail_url`` for the picture cache; None without a numeric ad id."""
    item = _mapping(item)
    ad_id = object_id(item.get("id"), DIGITS)
    if not ad_id:
        return None
    campaign, adset = _mapping(item.get("campaign")), _mapping(item.get("adset"))
    creative = _mapping(item.get("creative"))
    spec = _mapping(creative.get("object_story_spec"))
    title, body = clip(creative.get("title"), 400), clip(creative.get("body"), 4000)
    for key, title_key, body_key in _SPEC_TEXTS:
        data = _mapping(spec.get(key))
        title = title or (clip(data.get(title_key), 400) if title_key else "")
        body = body or clip(data.get(body_key), 4000)
    thumbnail = creative.get("thumbnail_url")
    return {"ad_id": ad_id, "account_id": account_id, "name": clip(item.get("name"), 400),
            "status": clip(item.get("status"), 40),
            "effective_status": clip(item.get("effective_status"), 40),
            "campaign_id": object_id(campaign.get("id"), DIGITS),
            "campaign_name": clip(campaign.get("name"), 400),
            "objective": clip(campaign.get("objective"), 60),
            "adset_id": object_id(adset.get("id"), DIGITS),
            "adset_name": clip(adset.get("name"), 400),
            "creative_id": object_id(creative.get("id"), DIGITS), "title": title, "body": body,
            "story_id": object_id(creative.get("effective_object_story_id")),
            "created_time": meta_time(item.get("created_time")),
            "thumbnail_url": thumbnail if isinstance(thumbnail, str) and len(thumbnail) <= 4096
            else ""}


def garage_ads(ads: Sequence[Any], page_id: str, account_id: str) -> list[dict]:
    """The ads among ``ads`` that promote ``page_id``, parsed; every other ad is dropped."""
    return [parsed for ad in ads if isinstance(ad, Mapping) and promotes_page(ad, page_id)
            and (parsed := parse_ad(ad, account_id)) is not None]


def _money(value: Any) -> Decimal:
    """Meta's spend (a decimal string) as cents; 0 for anything that is not a sane amount."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return Decimal(0)
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)
    if not amount.is_finite() or amount < 0 or amount > MAX_SPEND:
        return Decimal(0)
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _whole(value: Any) -> int:
    """An action's count: Meta sends it as a string, sometimes with decimals."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return 0
    return int(amount) if amount.is_finite() and amount > 0 else 0


def _actions(value: Any) -> tuple[list[dict], int]:
    """Meta's ``actions`` kept compact (type and value), and the chats the ad started."""
    kept: list[dict] = []
    chats = 0
    for entry in value if isinstance(value, list) else ():
        if not isinstance(entry, Mapping):
            continue
        kind, amount = entry.get("action_type"), entry.get("value")
        if not isinstance(kind, str) or not _ACTION_TYPE.match(kind):
            continue
        if kind == CHAT_ACTION:
            chats += _whole(amount)
        if len(kept) < ACTION_LIMIT:
            kept.append({"action_type": kind, "value": str(amount)[:24]})
    return kept, min(chats, MAX_INT)


def parse_insight(row: Any) -> dict | None:
    """One ``level=ad, time_increment=1`` insights row as a ``meta_ad_daily`` row."""
    row = _mapping(row)
    ad_id = object_id(row.get("ad_id"), DIGITS)
    try:
        day = date.fromisoformat(str(row.get("date_start") or ""))
    except ValueError:
        return None
    if not ad_id:
        return None
    actions, chats = _actions(row.get("actions"))
    return {"ad_id": ad_id, "day": day, "spend": _money(row.get("spend")),
            "impressions": count(row.get("impressions")) or 0,
            "reach": count(row.get("reach")) or 0, "clicks": count(row.get("clicks")) or 0,
            "messaging_started": chats, "actions": actions}


# -- the job -----------------------------------------------------------------------------------


def _skipped(reason: str) -> SyncCount:
    LOGGER.info("meta_ads_skipped reason=%s", reason.split(" ")[0])
    return SyncCount(0, "skipped", reason)


def _insight_window(conn: Any, account: Mapping[str, Any], now: Any) -> tuple[date, date]:
    """Since and until, as days of the account's time zone: 90 days when nothing of this
    account is stored yet, else 30."""
    stored = conn.execute(
        "SELECT max(d.day) AS last FROM garage.meta_ad_daily d JOIN garage.meta_ads a "
        "ON a.ad_id = d.ad_id WHERE a.account_id = %s", (account["account_id"],)).fetchone()
    days = ROLLING_DAYS if stored and stored["last"] is not None else FIRST_DAYS
    until = now.astimezone(zone(account.get("timezone_name"))).date()
    return until - timedelta(days=days - 1), until


def sync_ads(ctx: Any, *, fetcher: ImageFetcher | None = None) -> SyncCount:
    """Garage's ads and their daily results, read-only (worker login, every 3 hours).

    Returns the ads plus daily rows written. Without a page, an ads token, a readable ad
    account or any Garage ad it returns 0 with status 'skipped' and the reason as detail. One
    account that cannot be read is skipped (the rest still sync); when none can be read, the
    error is raised. A dead token is marked broken."""
    try:
        page_id = ctx.page_id
    except NotConnected:
        return _skipped("not_connected")
    try:
        token = ctx.ads_token()
    except NotConnected:
        return _skipped("no_ads_token")
    except TokenCipherError:
        return _skipped("ads_token_unreadable")
    graph = ctx.graph

    def dead(error: GraphError) -> bool:
        if error.token_dead:
            ctx.store.mark_token(token.purpose, "broken", f"meta_code_{error.code}")
        return error.token_dead

    try:
        raw = graph.paged("me/adaccounts", token, {"fields": ACCOUNT_FIELDS, "limit": 100},
                          max_pages=ACCOUNT_PAGES)
    except GraphError as error:
        if dead(error):
            return _skipped(f"ads_token_dead code={error.code}")
        if refused(error):
            return _skipped(f"ads_read_refused code={error.code}")
        raise
    accounts = [account for account in map(parse_account, raw) if account]
    if not accounts:
        return _skipped("no_ad_accounts")
    found: dict[str, list[dict]] = {}
    failure: GraphError | None = None
    for account in accounts:
        account_id = account["account_id"]
        try:
            ads = graph.paged(f"act_{account_id}/ads", token,
                              {"fields": AD_FIELDS, "limit": 100}, max_pages=AD_PAGES)
        except GraphError as error:
            if dead(error):
                return _skipped(f"ads_token_dead code={error.code}")
            LOGGER.info("meta_ads_account_unreadable code=%s http=%s", error.code,
                        error.http_status)
            failure = error
            continue
        found[account_id] = garage_ads(ads, page_id, account_id)
    if not found and failure is not None:
        raise failure
    database = database_of(ctx)
    with database.transaction() as conn:
        for account in accounts:
            ads = found.get(account["account_id"])
            conn.execute(
                "INSERT INTO garage.meta_ad_accounts (account_id, name, currency, timezone_name, "
                "account_status, garage_ads) VALUES (%(account_id)s, %(name)s, %(currency)s, "
                "%(timezone_name)s, %(account_status)s, COALESCE(%(garage)s, 0)) "
                "ON CONFLICT (account_id) DO UPDATE SET name = EXCLUDED.name, "
                "currency = EXCLUDED.currency, timezone_name = EXCLUDED.timezone_name, "
                "account_status = EXCLUDED.account_status, garage_ads = "
                "COALESCE(%(garage)s, garage.meta_ad_accounts.garage_ads), synced_at = now()",
                {**account, "garage": None if ads is None else len(ads)})
    total = sum(len(ads) for ads in found.values())
    unread = len(accounts) - len(found)
    if not total:
        return _skipped(f"no_garage_ads accounts={len(accounts)} unreadable={unread}")
    own = ImageFetcher() if fetcher is None else None
    try:
        for ad in (ad for ads in found.values() for ad in ads):
            _store_ad(database, ad, fetcher or own)
    finally:
        if own is not None:
            own.close()
    days_written, broken = 0, 0
    by_id = {account["account_id"]: account for account in accounts}
    for account_id, ads in found.items():
        if not ads:
            continue
        with database.transaction() as conn:
            since, until = _insight_window(conn, by_id[account_id], ctx.now())
        ids = [ad["ad_id"] for ad in ads]
        for start in range(0, len(ids), IDS_PER_REQUEST):
            chunk = ids[start:start + IDS_PER_REQUEST]
            try:
                rows = graph.paged(f"act_{account_id}/insights", token, {
                    "level": "ad", "time_increment": 1, "fields": INSIGHT_FIELDS,
                    "time_range": {"since": since.isoformat(), "until": until.isoformat()},
                    "filtering": [{"field": "ad.id", "operator": "IN", "value": chunk}],
                    "limit": INSIGHT_LIMIT}, max_pages=INSIGHT_PAGES)
            except GraphError as error:
                if dead(error):
                    raise
                LOGGER.info("meta_ads_insights_unreadable code=%s http=%s", error.code,
                            error.http_status)
                broken += 1
                continue
            wanted = set(chunk)
            parsed = [row for row in map(parse_insight, rows) if row and row["ad_id"] in wanted]
            _store_days(database, parsed)
            days_written += len(parsed)
    detail = (f"accounts={len(accounts)} unreadable={unread} garage_ads={total} "
              f"days={days_written} insight_errors={broken}")
    return SyncCount(total + days_written, "ok", detail)


def _store_ad(database: Any, ad: Mapping[str, Any], fetcher: ImageFetcher) -> None:
    """One Garage ad with its cached thumbnail (worker login)."""
    identity = f"ad_creative:{ad['creative_id']}" if ad["creative_id"] else f"ad:{ad['ad_id']}"
    key = cache_image(database, identity, ad["thumbnail_url"], fetcher)
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO garage.meta_ads (ad_id, account_id, name, status, effective_status, "
            "campaign_id, campaign_name, objective, adset_id, adset_name, creative_id, title, "
            "body, story_id, image_key, created_time) VALUES (%(ad_id)s, %(account_id)s, "
            "%(name)s, %(status)s, %(effective_status)s, %(campaign_id)s, %(campaign_name)s, "
            "%(objective)s, %(adset_id)s, %(adset_name)s, %(creative_id)s, %(title)s, %(body)s, "
            "%(story_id)s, %(image_key)s, %(created_time)s) "
            "ON CONFLICT (ad_id) DO UPDATE SET account_id = EXCLUDED.account_id, "
            "name = EXCLUDED.name, status = EXCLUDED.status, "
            "effective_status = EXCLUDED.effective_status, campaign_id = EXCLUDED.campaign_id, "
            "campaign_name = EXCLUDED.campaign_name, objective = EXCLUDED.objective, "
            "adset_id = EXCLUDED.adset_id, adset_name = EXCLUDED.adset_name, "
            "creative_id = EXCLUDED.creative_id, title = EXCLUDED.title, body = EXCLUDED.body, "
            "story_id = EXCLUDED.story_id, "
            "image_key = COALESCE(EXCLUDED.image_key, garage.meta_ads.image_key), "
            "created_time = COALESCE(EXCLUDED.created_time, garage.meta_ads.created_time), "
            "synced_at = now()", {**ad, "image_key": key})


def _store_days(database: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    """Daily results, replacing what Meta restated (worker login)."""
    with database.transaction() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO garage.meta_ad_daily (ad_id, day, spend, impressions, reach, "
                "clicks, messaging_started, actions) VALUES (%(ad_id)s, %(day)s, %(spend)s, "
                "%(impressions)s, %(reach)s, %(clicks)s, %(messaging_started)s, %(actions)s) "
                "ON CONFLICT (ad_id, day) DO UPDATE SET spend = EXCLUDED.spend, "
                "impressions = EXCLUDED.impressions, reach = EXCLUDED.reach, "
                "clicks = EXCLUDED.clicks, messaging_started = EXCLUDED.messaging_started, "
                "actions = EXCLUDED.actions, synced_at = now()",
                {**row, "actions": Jsonb(row["actions"])})


# -- the dashboard's reads (site login) --------------------------------------------------------


def _int(value: Any) -> int:
    return int(value or 0)


def summarize(row: Mapping[str, Any]) -> dict:
    """One ad's totals with its rates: CTR in percent of impressions, cost per chat Meta
    counted (None without a chat), and a link to the ad in Ads Manager."""
    raw = row.get("spend")
    spend = raw if isinstance(raw, Decimal) else _money(str(raw or 0))
    impressions, clicks = _int(row.get("impressions")), _int(row.get("clicks"))
    started = _int(row.get("messaging_started"))
    item = dict(row)
    item.update(spend=float(spend), impressions=impressions, reach=_int(row.get("reach")),
                clicks=clicks, messaging_started=started, chats=_int(row.get("chats")),
                chats_handled=_int(row.get("chats_handled")),
                ctr=round(clicks * 100 / impressions, 2) if impressions else None,
                cost_per_chat=float((spend / started).quantize(CENT, rounding=ROUND_HALF_UP))
                if started else None)
    account_id, ad_id = str(row.get("account_id") or ""), str(row.get("ad_id") or "")
    if DIGITS.match(account_id) and DIGITS.match(ad_id):
        item["ads_manager_url"] = ("https://adsmanager.facebook.com/adsmanager/manage/ads?"
                                   f"act={account_id}&selected_ad_ids={ad_id}")
    return json_ready(item)


def ad_results(db: Any, days: int = 30) -> list[dict]:
    """Per Garage ad over the last ``days`` Tbilisi days, biggest spend first: ad_id,
    account_id, currency, name, status, effective_status, campaign and ad set ids and names,
    objective, title, body (the boosted post's text when the creative has none), story_id,
    image_key, created_time, spend, impressions, reach (a sum of daily reach, so people seen on
    two days count twice), clicks, ctr, messaging_started (Meta's chats), cost_per_chat, chats
    (our chats that came from the ad) and chats_handled (those the bot answered),
    ads_manager_url. An ad shows when it has results in the range, is active, or brought a
    chat."""
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "WITH bounds AS (SELECT (now() AT TIME ZONE 'Asia/Tbilisi')::date "
            "- %(days)s::int + 1 AS first_day), "
            "totals AS (SELECT d.ad_id, sum(d.spend) AS spend, sum(d.impressions) AS impressions, "
            "sum(d.reach) AS reach, sum(d.clicks) AS clicks, "
            "sum(d.messaging_started) AS messaging_started FROM garage.meta_ad_daily d, bounds b "
            "WHERE d.day >= b.first_day GROUP BY d.ad_id), "
            "chats AS (SELECT t.source_ad_id AS ad_id, count(*) AS chats, "
            "count(*) FILTER (WHERE t.last_bot_at IS NOT NULL) AS chats_handled "
            "FROM garage.messenger_threads t, bounds b WHERE t.source_ad_id <> '' "
            "AND t.first_seen_at >= (b.first_day::timestamp AT TIME ZONE 'Asia/Tbilisi') "
            "GROUP BY t.source_ad_id) "
            "SELECT a.ad_id, a.account_id, acc.currency, a.name, a.status, a.effective_status, "
            "a.campaign_id, a.campaign_name, a.objective, a.adset_id, a.adset_name, a.title, "
            "COALESCE(NULLIF(a.body, ''), left(p.message, 4000), '') AS body, a.story_id, "
            "a.image_key, a.created_time, COALESCE(t.spend, 0) AS spend, "
            "COALESCE(t.impressions, 0) AS impressions, COALESCE(t.reach, 0) AS reach, "
            "COALESCE(t.clicks, 0) AS clicks, "
            "COALESCE(t.messaging_started, 0) AS messaging_started, "
            "COALESCE(c.chats, 0) AS chats, COALESCE(c.chats_handled, 0) AS chats_handled "
            "FROM garage.meta_ads a JOIN garage.meta_ad_accounts acc "
            "ON acc.account_id = a.account_id LEFT JOIN totals t ON t.ad_id = a.ad_id "
            "LEFT JOIN chats c ON c.ad_id = a.ad_id "
            "LEFT JOIN garage.meta_posts p ON a.story_id <> '' AND p.post_id = a.story_id "
            "WHERE t.ad_id IS NOT NULL OR a.effective_status = 'ACTIVE' OR c.ad_id IS NOT NULL "
            "ORDER BY COALESCE(t.spend, 0) DESC, COALESCE(t.messaging_started, 0) DESC, a.ad_id "
            "LIMIT %(limit)s", {"days": window_days(days), "limit": RESULT_LIMIT}).fetchall()
    return [summarize(row) for row in rows]


def best_ads(db: Any, days: int = 30, min_spend: float = 10.0, limit: int = 10) -> list[dict]:
    """The ads that bought chats cheapest: spend of at least ``min_spend`` (in the ad's own
    currency) and at least one chat, by cost per chat, then by more chats."""
    floor = float(min_spend) if isinstance(min_spend, int | float) else 10.0
    eligible = [row for row in ad_results(db, days)
                if row["spend"] >= floor and row["messaging_started"] > 0]
    eligible.sort(key=lambda row: (row["cost_per_chat"], -row["messaging_started"],
                                   -row["spend"], row["ad_id"]))
    try:
        size = max(1, min(int(limit), RESULT_LIMIT))
    except (TypeError, ValueError):
        size = 10
    return eligible[:size]


def daily_spend(db: Any, days: int = 30) -> list[dict]:
    """``[{day, currency, spend, impressions, clicks, messaging_started}]`` of the Garage ads,
    per account day and currency, oldest first."""
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "SELECT d.day, acc.currency, sum(d.spend) AS spend, sum(d.impressions) "
            "AS impressions, sum(d.clicks) AS clicks, sum(d.messaging_started) "
            "AS messaging_started FROM garage.meta_ad_daily d "
            "JOIN garage.meta_ads a ON a.ad_id = d.ad_id "
            "JOIN garage.meta_ad_accounts acc ON acc.account_id = a.account_id "
            "WHERE d.day >= (now() AT TIME ZONE 'Asia/Tbilisi')::date - %(days)s::int + 1 "
            "GROUP BY d.day, acc.currency ORDER BY d.day, acc.currency",
            {"days": window_days(days)}).fetchall()
    return json_ready([{**row, "impressions": _int(row["impressions"]),
                        "clicks": _int(row["clicks"]),
                        "messaging_started": _int(row["messaging_started"])} for row in rows])


def ad_accounts(db: Any) -> list[dict]:
    """Every ad account the last sync could read, with how many Garage ads it holds (most
    first): where Garage's ads run, and an empty list or zeros when there are none yet."""
    with database_of(db).transaction() as conn:
        rows = conn.execute(
            "SELECT account_id, name, currency, timezone_name, account_status, garage_ads, "
            "synced_at FROM garage.meta_ad_accounts ORDER BY garage_ads DESC, name, account_id"
        ).fetchall()
    return json_ready([dict(row) for row in rows])
