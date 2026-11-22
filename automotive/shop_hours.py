"""When the shop's staff answer chats: 10:00 to 19:00, every day, Tbilisi time.

One clock for the whole system. The bot reads it (after hours it tells a customer whose part it
could not find that the team will send the details during working hours), the Live page reads
it (a part_not_found alarm raised after hours is saved without a sound, and the repeating siren
stops at closing time). Georgia keeps UTC+4 all year; the fixed offset stands in when the
system has no time-zone data.

These are the hours the staff watch the Live page, set by the owner on 2026-11-21. They are
not parsed from the opening hours on the website (free text for people to read).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone, tzinfo

OPENS = time(10, 0)  # inclusive
CLOSES = time(19, 0)  # exclusive

try:
    from zoneinfo import ZoneInfo

    TBILISI: tzinfo = ZoneInfo("Asia/Tbilisi")
except Exception:  # noqa: BLE001 - no tz database: Georgia has no daylight saving time
    TBILISI = timezone(timedelta(hours=4), "Asia/Tbilisi")


def now() -> datetime:
    """The current time; tests replace it."""
    return datetime.now(UTC)


def is_open(at: datetime | None = None) -> bool:
    """Whether ``at`` (now when None) falls inside the staff's hours. A naive ``at`` is UTC."""
    moment = now() if at is None else at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(TBILISI).time()
    return OPENS <= local < CLOSES
