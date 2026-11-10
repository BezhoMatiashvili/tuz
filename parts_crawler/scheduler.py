"""One worker thread per source: claim, fetch politely, store raw page, parse, persist.

Every state change is committed before the next request, so a crash or Ctrl+C loses
at most the page in flight; its lease expires and the next run picks it up again.
"""

from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace

import psycopg

from parts_crawler.config import ROBOTS_AGENT, Settings
from parts_crawler.http import (
    ChallengeDetected,
    CircuitOpen,
    Disallowed,
    Interrupted,
    PermanentFetchError,
    PoliteFetcher,
    RetryableFetchError,
    SourcePaused,
)
from parts_crawler.rawstore import RawStore
from parts_crawler.records import Link
from parts_crawler.sources.base import CrawledPage, SourceAdapter
from parts_crawler.store import Store

log = logging.getLogger("parts_crawler.scheduler")


def backoff(attempts: int) -> float:
    """2 s, 8 s, 32 s, ~2 min, ~8.5 min, then 30 min, each with ±20% jitter."""
    base = min(1800.0, 2.0 * 4 ** max(0, attempts - 1))
    return base * random.uniform(0.8, 1.2)  # nosec B311


def auto_part_links(adapter: SourceAdapter, store: Store, *, pattern: str | None = None,
                    parts: str | None = None) -> tuple[list[Link], int]:
    """One parts request per vehicle family, newest vehicles first.

    Crawling every stored variant would take years at a polite pace, and variants of
    one family (same model, frame and engine) share their part lists. The newest
    vehicle of each family stands in for it. Returns the links and how many vehicles
    were looked at. Queueing them twice is harmless: a known URL is left alone.
    """
    best: dict[str, tuple[tuple, dict]] = {}
    scanned = 0
    for row in store.iter_vehicles(adapter.name):
        scanned += 1
        family = adapter.family(row)
        if not family:
            continue
        rank = (row["year_to"] is None, row["year_to"] or 0, row["year_from"] or 0)
        if family not in best or rank > best[family][0]:
            best[family] = (rank, row)
    year = time.gmtime().tm_year
    links: list[Link] = []
    for _rank, row in best.values():
        # Still-produced and recent vehicles first; all of them below `fair_split`
        # and above the 4-11 used by a vehicle's own pages, so one vehicle is
        # finished before the next begins.
        age = max(0, year - (row["year_to"] or year))
        links += [replace(link, priority=20 + min(55, age), refresh=False)
                  for link in adapter.vehicle_seeds(
                      [row], adapter.auto_groups if pattern is None else pattern,
                      adapter.auto_part_names if parts is None else parts)]
    links.sort(key=lambda link: link.priority)
    return links, scanned


@dataclass
class SourceStats:
    name: str
    state: str = "starting"
    fetched: int = 0
    failed: int = 0
    retried: int = 0
    skipped: int = 0
    parse_errors: int = 0
    added: Counter = field(default_factory=Counter)
    last_error: str = ""
    last_error_at: float = 0.0
    recent: deque = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def hit(self) -> None:
        now = time.monotonic()
        with self.lock:
            self.recent.append(now)
            while self.recent and self.recent[0] < now - 300:
                self.recent.popleft()

    def per_minute(self) -> float:
        now = time.monotonic()
        with self.lock:
            recent = [moment for moment in self.recent if moment >= now - 300]
        if not recent:
            return 0.0
        window = max(60.0, now - recent[0])
        return len(recent) * 60.0 / window

    def error(self, code: str) -> None:
        self.last_error = code[:80]
        self.last_error_at = time.monotonic()

    def recent_error(self, within: float = 900.0) -> str:
        """The last error with its age, or '' once it is older than `within` seconds."""
        if not self.last_error:
            return ""
        age = time.monotonic() - self.last_error_at
        if age > within:
            return ""
        return f"{self.last_error} ({int(age // 60)}m ago)" if age >= 60 else self.last_error


class Scheduler:
    def __init__(self, settings: Settings, adapters: list[SourceAdapter], *,
                 stop: threading.Event, max_pages: int | None = None,
                 seed_codes: int = 0, auto_parts: bool = False) -> None:
        self.settings = settings
        self.adapters = adapters
        self.stop = stop
        self.max_pages = max_pages
        self.seed_codes = seed_codes
        self.auto_parts = auto_parts
        self.raw = RawStore(settings.pages_dir)
        self.fetcher = PoliteFetcher(
            user_agent=settings.user_agent, robots_token=ROBOTS_AGENT,
            min_delay=settings.min_delay, jitter=settings.jitter, max_bytes=settings.max_bytes,
            breaker_threshold=settings.breaker_threshold,
            breaker_seconds=settings.breaker_seconds,
            breaker_trips_to_pause=settings.breaker_trips_to_pause,
            sleeper=lambda seconds: not self.stop.wait(seconds))
        self.stats = {adapter.name: SourceStats(adapter.name) for adapter in adapters}
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for adapter in self.adapters:
            thread = threading.Thread(target=self._guarded, args=(adapter,),
                                      name=f"crawl-{adapter.name}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def alive(self) -> bool:
        return any(thread.is_alive() for thread in self.threads)

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self.threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)

    def exempt_paths(self, adapter: SourceAdapter) -> tuple[str, ...]:
        """Path prefixes robots.txt rules are lifted for: needs both the adapter's
        declaration and the operator's PARTS_CRAWLER_ROBOTS_EXEMPT entry."""
        if adapter.needs_robots_exemption and adapter.name in self.settings.robots_exempt:
            return adapter.robots_exempt_paths
        return ()

    def _guarded(self, adapter: SourceAdapter) -> None:
        stats = self.stats[adapter.name]
        log.info("%s: worker started", adapter.name)
        try:
            self._supervise(adapter, stats)
        finally:
            log.info("%s: worker finished (%s): fetched %s, failed %s, retried %s this run",
                     adapter.name, stats.state, stats.fetched, stats.failed, stats.retried)

    def _supervise(self, adapter: SourceAdapter, stats: SourceStats) -> None:
        reconnects = 0
        while not self.stop.is_set():
            try:
                self._work(adapter, stats)
                return
            except psycopg.OperationalError:
                # The item in flight keeps its lease; it expires and is retried, so a
                # database restart costs at most one page.
                reconnects += 1
                stats.error("DATABASE_UNAVAILABLE")
                log.warning("%s: database unavailable (attempt %s)", adapter.name, reconnects)
                if reconnects > 5:
                    stats.state = "stopped: database unavailable"
                    return
                stats.state = "database retry"
                self.stop.wait(30.0 * reconnects)
            except Exception:
                log.exception("%s: worker crashed", adapter.name)
                stats.state = "stopped: unexpected error (see crawl.log)"
                return

    def _work(self, adapter: SourceAdapter, stats: SourceStats) -> None:
        name = adapter.name
        store = Store(self.settings.database_url)
        try:
            store.ensure_source(name, adapter.daily_budget)
            row = store.source_row(name)
            if row["state"] != "active":
                stats.state = f"{row['state']}: {row['state_reason'] or ''}".strip(": ")
                return
            exempt = self.exempt_paths(adapter)
            if adapter.needs_robots_exemption and not exempt:
                # Starting anyway would mark every queued URL "skipped"; leave the queue
                # untouched so the source can run later once the operator allows it.
                stats.state = "not started: set PARTS_CRAWLER_ROBOTS_EXEMPT=" + name
                log.warning("%s: robots.txt disallows its URLs and no exemption is set", name)
                return
            if exempt:
                log.warning("%s: robots.txt rules for %s are lifted by operator setting",
                            name, ", ".join(exempt))
            if not store.lock_source(name):
                stats.state = "already running in another process"
                log.warning("%s: another crawler process holds this source; not started", name)
                return
            released = store.release_unfinished(name)
            if released:
                log.info("%s: %s pages left unfinished by an earlier run are queued again",
                         name, released)
            if not store.has_queue(name):
                added = store.enqueue(name, adapter.seeds())
                if self.seed_codes:
                    added += store.enqueue(
                        name, adapter.code_seeds(store.warehouse_codes(self.seed_codes)))
                log.info("%s: seeded %s urls", name, added)
            token = secrets.token_hex(8)
            pages = 0
            turn = 0
            seeded = -3600.0
            checked = time.monotonic()
            while not self.stop.is_set():
                if self.max_pages is not None and pages >= self.max_pages:
                    stats.state = "page limit reached"
                    return
                if time.monotonic() - checked > 30:
                    checked = time.monotonic()
                    row = store.source_row(name)
                    if row["state"] != "active":
                        stats.state = f"{row['state']}: {row['state_reason'] or ''}".strip(": ")
                        return
                if (self.auto_parts and adapter.auto_groups
                        and time.monotonic() - seeded > 1800):
                    # Vehicles found since the last pass get their parts queued too.
                    seeded = time.monotonic()
                    links, scanned = auto_part_links(adapter, store)
                    added = store.enqueue(name, links)
                    log.info("%s: %s new vehicle families queued for parts "
                             "(%s families among %s stored vehicles)", name, added,
                             len(links), scanned)
                item = None
                if adapter.fair_split is not None:
                    # Take turns: one part request for a seeded vehicle, one page that
                    # discovers vehicles. Whichever kind is empty yields to the other.
                    item = store.claim(name, token, self.settings.lease_seconds,
                                       below=turn % 2 == 0, split=adapter.fair_split)
                    turn += 1
                if item is None:
                    item = store.claim(name, token, self.settings.lease_seconds)
                if item is None:
                    due = store.next_due_seconds(name)
                    if due is None:
                        stats.state = "done"
                        return
                    stats.state = f"waiting {int(due)}s"
                    self.stop.wait(min(max(due, 1.0), 30.0))
                    continue
                if item.attempts > self.settings.max_attempts:
                    store.mark(item, "failed", error="RETRY_EXHAUSTED:LEASE_EXPIRED")
                    stats.failed += 1
                    continue
                if not store.consume_budget(name):
                    store.release(item)
                    stats.state = "daily budget used"
                    return
                stats.state = "fetching"
                if self._fetch_one(adapter, store, item, stats):
                    pages += 1
                if stats.state in {"blocked", "paused", "interrupted"}:
                    return
            stats.state = "stopped"
        finally:
            store.close()

    def _fetch_one(self, adapter: SourceAdapter, store: Store, item, stats: SourceStats) -> bool:
        name = adapter.name
        try:
            page = self.fetcher.fetch(
                item.url, allowed_hosts=adapter.hosts, min_delay=adapter.min_delay,
                robots_exempt=self.exempt_paths(adapter), cookies=adapter.cookies,
                headers=adapter.request_headers,
                captcha_widget=adapter.embeds_captcha_widget)
        except Interrupted:
            store.release(item)
            stats.state = "interrupted"
            return False
        except Disallowed as error:
            store.mark(item, "skipped", error=error.code)
            stats.skipped += 1
            return False
        except PermanentFetchError as error:
            store.mark(item, "failed", error=error.code, http_status=error.status)
            stats.failed += 1
            stats.error(f"{error.code} {error.status or ''}")
            return False
        except CircuitOpen as error:
            store.release(item)
            waiting = ("robots.txt unavailable" if error.code == "ROBOTS_UNAVAILABLE"
                       else "circuit open")
            stats.state = f"{waiting} {int(error.retry_after or 0)}s"
            stats.error(error.code)
            store.record_error(name, error.code, trip=error.code == "CIRCUIT_OPEN")
            self.stop.wait(min(error.retry_after or 60.0, 1800.0))
            return False
        except SourcePaused as error:
            store.release(item)
            store.set_state(name, "paused", f"{error.code}: repeated host failures")
            stats.state = "paused"
            stats.error(error.code)
            return False
        except ChallengeDetected as error:
            store.release(item)
            store.set_state(name, "blocked",
                            f"{error.code}: bot protection; record a HAR and use import-har")
            stats.state = "blocked"
            stats.error(f"{error.code} {error.status or ''}")
            return False
        except RetryableFetchError as error:
            delay = max(error.retry_after or 0.0, backoff(item.attempts))
            outcome = store.retry_later(item, error.code, delay, self.settings.max_attempts,
                                        error.status)
            stats.retried += outcome == "pending"
            stats.failed += outcome == "failed"
            stats.error(f"{error.code} {error.status or ''}")
            return False
        stats.fetched += 1
        stats.hit()
        sha, path = self.raw.save(name, page.body)
        crawled = CrawledPage(page.url, item.kind, item.context, page.status, page.body,
                              page.content_type)
        try:
            result, problem = adapter.parse(crawled), None
        except Exception as error:
            log.exception("%s: parse failed for %s", name, page.url)
            result, problem = None, f"PARSE_ERROR:{type(error).__name__}"
            stats.parse_errors += 1
            stats.error(problem)
        counts = store.save_page(
            item, url=page.url, http_status=page.status, content_type=page.content_type,
            sha=sha, storage_path=path, size=len(page.body),
            parser_version=adapter.parser_version if result is not None else None,
            result=result, parse_error=problem)
        if counts.pop("store_errors", 0):
            # The database rejected a record; that fails this one page, not the source.
            log.error("%s: the database rejected records parsed from %s", name, page.url)
            stats.failed += 1
            stats.error("STORE_ERROR")
        with stats.lock:
            stats.added.update(counts)
        return True
