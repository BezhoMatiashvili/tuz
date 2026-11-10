"""Live terminal table for a crawl run, plus a plain fallback for logs and pipes."""

from __future__ import annotations

import logging
import time

from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table

from parts_crawler.scheduler import Scheduler
from parts_crawler.store import Store
from parts_crawler.term import console

log = logging.getLogger("parts_crawler.progress")


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def number(value: int | float | None) -> str:
    return "-" if value is None else f"{int(value):,}"


def queue_progress(counts: dict) -> tuple[int, int]:
    """(pages dealt with, pages known) for one source's queue."""
    pending = counts.get("pending", 0) + counts.get("running", 0)
    handled = counts.get("done", 0) + counts.get("failed", 0) + counts.get("skipped", 0)
    return handled, handled + pending


def percent(handled: int, total: int) -> str:
    return f"{handled * 100 // total}%" if total else "-"


STATE_STYLE = {"fetching": "green", "done": "cyan", "blocked": "red", "paused": "yellow",
               "daily budget used": "yellow"}


def build_table(scheduler: Scheduler, queue: dict, catalog: dict, started: float,
                stopping: bool = False) -> Table:
    title = f"parts_crawler · {duration(time.monotonic() - started)} elapsed"
    if stopping:
        title += " · stopping after the current page (Ctrl+C again to force)"
    table = Table(title=title, title_justify="left", expand=True,
                  caption="Progress is measured against the pages found so far; "
                          "catalog trees grow while they are explored.",
                  caption_justify="left")
    for column, justify in (("source", "left"), ("state", "left"), ("progress", "left"),
                            ("%", "right"), ("pending", "right"), ("done", "right"),
                            ("failed", "right"), ("pages/min", "right"),
                            ("this run", "right"), ("parts", "right"), ("vehicles", "right"),
                            ("fitments", "right"), ("x-refs", "right"), ("ETA", "right"),
                            ("last error", "left")):
        table.add_column(column, justify=justify, no_wrap=column != "last error")
    for name, stats in scheduler.stats.items():
        counts = queue.get(name, {})
        stored = catalog.get(name, {})
        handled, total = queue_progress(counts)
        rate = stats.per_minute()
        eta = (total - handled) / rate * 60 if rate > 0 else None
        style = STATE_STYLE.get(stats.state.split(":")[0], "")
        table.add_row(
            name, f"[{style}]{stats.state}[/]" if style else stats.state,
            ProgressBar(total=max(total, 1), completed=handled, width=14),
            percent(handled, total), number(total - handled), number(counts.get("done")),
            number(counts.get("failed")), f"{rate:.1f}", number(stats.fetched),
            number(stored.get("parts")), number(stored.get("vehicles")),
            number(stored.get("fitments")), number(stored.get("xrefs")), duration(eta),
            stats.recent_error())
    return table


def _counts(database_url: str) -> tuple[dict, dict]:
    store = Store(database_url)
    try:
        return store.queue_counts(), store.catalog_counts()
    finally:
        store.close()


def watch(scheduler: Scheduler, database_url: str, *, plain: bool,
          db_refresh_seconds: float = 15.0) -> str:
    """Render until every worker stops; returns the stop reason for crawl_runs."""
    plain = plain or not console.is_terminal
    started = time.monotonic()
    queue: dict = {}
    catalog: dict = {}
    refreshed = 0.0
    reason = "finished"

    def refresh() -> None:
        nonlocal queue, catalog, refreshed
        if time.monotonic() - refreshed < db_refresh_seconds:
            return
        refreshed = time.monotonic()
        try:
            queue, catalog = _counts(database_url)
        except Exception:
            log.warning("progress counts unavailable")

    def plain_line() -> None:
        for name, stats in scheduler.stats.items():
            counts = queue.get(name, {})
            handled, total = queue_progress(counts)
            line = (f"{name:9} {stats.state:22} {percent(handled, total):>4} "
                    f"pending={total - handled} done={counts.get('done', 0)} "
                    f"failed={counts.get('failed', 0)} run={stats.fetched} "
                    f"rate={stats.per_minute():.1f}/min {stats.recent_error()}")
            print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
            log.info("progress %s", " ".join(line.split()))

    try:
        if plain:
            printed = 0.0
            while scheduler.alive():
                refresh()
                if time.monotonic() - printed >= 30:
                    printed = time.monotonic()
                    plain_line()
                time.sleep(0.5)
            refreshed = 0.0
            refresh()
            plain_line()
        else:
            logged = time.monotonic()
            with Live(build_table(scheduler, queue, catalog, started), console=console,
                      refresh_per_second=2) as live:
                while scheduler.alive():
                    refresh()
                    live.update(build_table(scheduler, queue, catalog, started))
                    if time.monotonic() - logged >= 60:
                        logged = time.monotonic()
                        for name, stats in scheduler.stats.items():
                            handled, total = queue_progress(queue.get(name, {}))
                            log.info("progress %s %s %s pending=%s run=%s rate=%.1f/min",
                                     name, stats.state, percent(handled, total),
                                     total - handled, stats.fetched, stats.per_minute())
                    time.sleep(0.5)
                refreshed = 0.0
                refresh()
                live.update(build_table(scheduler, queue, catalog, started))
    except KeyboardInterrupt:
        reason = "interrupted"
        scheduler.stop.set()
        log.info("stop requested; finishing the pages in flight")
        console.print("[yellow]Stopping: finishing the current page and releasing leases. "
                      "Press Ctrl+C again to force.[/]")
        try:
            while scheduler.alive():
                scheduler.join(0.5)
        except KeyboardInterrupt:
            log.warning("forced stop; unfinished leases are queued again on the next run")
            return "forced"
        refreshed = 0.0
        refresh()
        console.print(build_table(scheduler, queue, catalog, started))
        console.print("Stopped cleanly. Run the same command again to continue.")
    return reason
