"""Pieces the Facebook sync jobs share: a record count that says how a run went, values the
dashboard can serialize, Meta's time strings and the Tbilisi day.

The worker runs each job (comment backfill, page stats, posts, ad results) and writes one
``meta_sync_runs`` row around it with the number the job returns. A job returns ``SyncCount``: an
``int`` (the records it wrote, all the worker needs), which also carries ``status`` ('ok' or
'skipped') and a short ``detail`` made of codes, so a run that wrote nothing can say why (no ads
token, no Garage ads, read_insights refused) when the worker passes them to ``finish_run``.

Nothing here talks to Meta or holds a token.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RUN_STATUSES = ("ok", "skipped")
DETAIL_LIMIT = 500  # meta_sync_runs.detail
DIGITS = re.compile(r"^[0-9]{1,30}$")  # page, user, ad, account and creative ids
OBJECT_ID = re.compile(r"^[0-9_]{1,80}$")  # post and comment ids ("<page>_<post>")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Georgia keeps UTC+4 all year: the fixed offset stands in without a tz database.
        return timezone(timedelta(hours=4), "Asia/Tbilisi")


TBILISI = _zone("Asia/Tbilisi")


class SyncCount(int):
    """The records a job wrote, with how the run went for its ``meta_sync_runs`` row.

    It is an ``int``, so a caller that only wants the count uses it as one. ``status`` is 'ok'
    or 'skipped' (the job had nothing it could do: no token, nothing to read); ``detail`` is a
    short line of codes and counts, never text from Meta or a customer."""

    status: str
    detail: str

    def __new__(cls, records: int = 0, status: str = "ok", detail: str = "") -> SyncCount:
        value = super().__new__(cls, max(0, int(records)))
        value.status = status if status in RUN_STATUSES else "ok"
        value.detail = str(detail or "")[:DETAIL_LIMIT]
        return value

    def __repr__(self) -> str:
        return f"SyncCount({int(self)}, status={self.status!r}, detail={self.detail!r})"


def database_of(source: Any) -> Any:
    """The ``MetaDatabase`` behind a ``MetaDatabase``, a ``MetaStore`` or a ``MetaContext``, so
    a dashboard helper takes whichever the caller holds for the site login."""
    store = getattr(source, "store", None)
    if store is not None and hasattr(store, "db"):
        return store.db
    if hasattr(source, "db") and not hasattr(source, "transaction"):
        return source.db
    return source


def zone(name: Any) -> tzinfo:
    """An ad account's IANA time zone (Meta buckets its days in it); Tbilisi when the name is
    missing or unknown."""
    if isinstance(name, str) and name.strip():
        found = _zone(name.strip())
        if isinstance(found, ZoneInfo):
            return found
    return TBILISI


def tbilisi_today(now: datetime) -> date:
    """The shop's calendar day at ``now``: page statistics are filed under it."""
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    return moment.astimezone(TBILISI).date()


def meta_time(value: Any) -> datetime | None:
    """Meta's time strings ("2026-11-10T12:34:56+0000", ISO 8601) and epoch seconds as aware
    UTC datetimes; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value) or not 946_684_800 <= value <= 7_258_118_400:
            return None
        return datetime.fromtimestamp(value, UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for parse in (lambda raw: datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z"),
                  datetime.fromisoformat):
        try:
            moment = parse(text)
        except ValueError:
            continue
        return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)
    return None


def clip(value: Any, limit: int) -> str:
    """Text as the tables hold it: no NUL, no lone surrogate, trimmed, at most ``limit``."""
    if not isinstance(value, str):
        return ""
    return _SURROGATE.sub("�", value.replace("\x00", "")).strip()[:limit]


def one_line(value: Any, limit: int) -> str:
    """``clip`` with every run of white space made one space."""
    return " ".join(clip(value, max(limit * 4, limit)).split())[:limit]


def count(value: Any) -> int | None:
    """A non-negative whole number Meta sent as a number or a digit string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def object_id(value: Any, pattern: re.Pattern = OBJECT_ID) -> str:
    """``value`` when it is an id the 0013 columns accept, else ''."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        value = str(value)
    return value if isinstance(value, str) and pattern.match(value) else ""


def safe_link(value: Any, limit: int = 500) -> str:
    """A Facebook link the dashboard may show (a post's or comment's permalink): https on
    facebook.com or fb.com, without credentials, at most ``limit`` characters; else ''. Meta
    sends these, but the dashboard renders them as links, so nothing else gets through."""
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        return ""
    text = value.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password
            or not any(host == name or host.endswith("." + name)
                       for name in ("facebook.com", "fb.com"))):
        return ""
    return text


def json_ready(value: Any) -> Any:
    """Rows for the dashboard's JSON: datetimes and dates as ISO 8601 text, decimals as
    floats, tuples as lists, bytes left out."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()
                if not isinstance(item, bytes | bytearray | memoryview)}
    if isinstance(value, list | tuple):
        return [json_ready(item) for item in value]
    return value


def window_days(days: Any, default: int = 30, ceiling: int = 400) -> int:
    """A dashboard range in days, 1 to ``ceiling``."""
    try:
        number = int(days)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))
