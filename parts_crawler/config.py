"""Crawler settings from the environment; no secrets are logged or echoed."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROBOTS_AGENT = "TuzPartsCrawler"
SOURCE_NAME = re.compile(r"^[a-z0-9_]{2,32}$")


def _names(name: str) -> frozenset[str]:
    return frozenset(part.strip() for part in os.getenv(name, "").split(",")
                     if SOURCE_NAME.match(part.strip()))


def _float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, value)


def _int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    user_agent: str
    min_delay: float
    jitter: float
    max_attempts: int
    lease_seconds: int
    breaker_threshold: int
    breaker_seconds: float
    breaker_trips_to_pause: int
    max_bytes: int
    # Sources the operator allows to request paths their robots.txt disallows.
    robots_exempt: frozenset[str] = frozenset()

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "crawl.log"


def load_settings(*, require_database: bool = True) -> Settings:
    database_url = os.getenv("PARTS_CRAWLER_DATABASE_URL", "")
    if require_database and not database_url:
        raise SystemExit(
            "PARTS_CRAWLER_DATABASE_URL is required. Run "
            "`uv run python -m parts_crawler setup-local` first."
        )
    contact = os.getenv("PARTS_CRAWLER_CONTACT", "").strip()
    agent = f"Mozilla/5.0 (compatible; {ROBOTS_AGENT}/0.1; learning project"
    agent += f"; contact: {contact})" if contact else ")"
    return Settings(
        database_url=database_url,
        data_dir=Path(os.getenv("PARTS_CRAWLER_DATA_DIR", "") or ROOT / "data" / "crawl"),
        user_agent=os.getenv("PARTS_CRAWLER_USER_AGENT", "") or agent,
        # Lower bounds keep a misconfiguration from turning the crawler aggressive.
        min_delay=_float("PARTS_CRAWLER_MIN_DELAY_SECONDS", 5.0, 2.0),
        jitter=_float("PARTS_CRAWLER_JITTER_SECONDS", 1.5, 0.0),
        max_attempts=_int("PARTS_CRAWLER_MAX_ATTEMPTS", 6, 1),
        lease_seconds=_int("PARTS_CRAWLER_LEASE_SECONDS", 300, 60),
        breaker_threshold=_int("PARTS_CRAWLER_BREAKER_THRESHOLD", 10, 3),
        breaker_seconds=_float("PARTS_CRAWLER_BREAKER_SECONDS", 1800.0, 60.0),
        breaker_trips_to_pause=_int("PARTS_CRAWLER_BREAKER_TRIPS", 3, 1),
        max_bytes=_int("PARTS_CRAWLER_MAX_BYTES", 8_000_000, 100_000),
        robots_exempt=_names("PARTS_CRAWLER_ROBOTS_EXEMPT"),
    )
