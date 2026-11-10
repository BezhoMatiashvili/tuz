"""Parts catalog crawler CLI: uv run python -m parts_crawler <command> --help

Every command logs to data/crawl/crawl.log and can be stopped with Ctrl+C or `kill`;
running the same command again continues where it stopped.
"""

from __future__ import annotations

import argparse
import logging
import re
import threading
from pathlib import Path

from dotenv import load_dotenv
from rich.progress_bar import ProgressBar
from rich.table import Table

from automotive.local_setup import LocalSetupError
from parts_crawler.config import Settings, load_settings
from parts_crawler.progress import duration, number, percent, queue_progress, watch
from parts_crawler.rawstore import RawStore
from parts_crawler.scheduler import Scheduler
from parts_crawler.sources import ADAPTERS, get_adapters
from parts_crawler.sources.base import CrawledPage
from parts_crawler.store import SCHEMA_HINT, Store
from parts_crawler.term import Tracker, configure_logging, console, stop_on_sigterm

log = logging.getLogger("parts_crawler.cli")


def open_store(settings: Settings) -> Store:
    """A store on a database that has every crawler migration, or a clear way to get one."""
    store = Store(settings.database_url)
    if not store.schema_ready():
        store.close()
        raise SystemExit(SCHEMA_HINT)
    return store


def cmd_setup(_args: argparse.Namespace) -> None:
    from parts_crawler.setup import CRAWLER_MIGRATION, configure_local

    try:
        created = configure_local()
    except LocalSetupError as error:
        raise SystemExit(str(error)) from None
    console.print(f"Migrations through {CRAWLER_MIGRATION} applied. " + (
        "PARTS_CRAWLER_DATABASE_URL was added to .env." if created
        else "Existing PARTS_CRAWLER_DATABASE_URL was kept."))


def cmd_run(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    adapters = get_adapters(args.source)
    stop = threading.Event()
    scheduler = Scheduler(settings, adapters, stop=stop, max_pages=args.max_pages,
                          seed_codes=args.seed_codes, auto_parts=args.auto_parts)
    names = [adapter.name for adapter in adapters]
    exemptions = {adapter.name: list(scheduler.exempt_paths(adapter)) for adapter in adapters
                  if scheduler.exempt_paths(adapter)}
    store = open_store(settings)
    run_id = store.start_run(names)
    store.close()
    console.print(f"Crawling {', '.join(names)} · min delay {settings.min_delay:.0f}s per host "
                  f"· log: {settings.log_path}")
    for name, paths in exemptions.items():
        console.print(f"[yellow]{name}: robots.txt rules for {', '.join(paths)} are lifted "
                      "(PARTS_CRAWLER_ROBOTS_EXEMPT).[/]")
    log.info("run %s started: sources=%s max_pages=%s exemptions=%s", run_id, names,
             args.max_pages, exemptions)
    scheduler.start()
    reason = watch(scheduler, settings.database_url, plain=args.plain)
    scheduler.join(timeout=60)
    stats = {name: {"state": item.state, "fetched": item.fetched, "failed": item.failed,
                    "retried": item.retried, "skipped": item.skipped,
                    "parse_errors": item.parse_errors, **dict(item.added),
                    "robots_exempt_paths": exemptions.get(name, [])}
             for name, item in scheduler.stats.items()}
    fetched = sum(item.fetched for item in scheduler.stats.values())
    store = Store(settings.database_url)
    try:
        store.finish_run(run_id, fetched, sum(item.failed for item in scheduler.stats.values()),
                         stats, reason)
    finally:
        store.close()
    log.info("run %s finished (%s): %s pages fetched", run_id, reason, fetched)


def cmd_status(_args: argparse.Namespace) -> None:
    settings = load_settings()
    store = Store(settings.database_url)
    try:
        queue, catalog, sources = store.queue_counts(), store.catalog_counts(), store.sources()
        checkpoints = store.conn.execute(
            "SELECT name, state, updated_at FROM parts_catalog.import_checkpoints "
            "ORDER BY name").fetchall() if store.schema_ready() else []
    finally:
        store.close()
    table = Table(title="parts_crawler status", title_justify="left")
    for column in ("source", "state", "progress", "%", "today", "pending", "done", "failed",
                   "skipped", "parts", "vehicles", "fitments", "x-refs", "ETA"):
        table.add_column(column, no_wrap=True,
                         justify="left" if column in {"source", "state", "progress"}
                         else "right")
    reasons = []
    for row in sources:
        name = row["source"]
        counts, stored = queue.get(name, {}), catalog.get(name, {})
        handled, total = queue_progress(counts)
        budget = row["daily_budget"] or 0
        eta = (total - handled) / budget * 86400 if budget and total > handled else None
        if row["state_reason"]:
            reasons.append(f"{name}: {row['state']} - {row['state_reason']}")
        today = row["budget_used"] if row["budget_day"] else 0
        table.add_row(name, row["state"],
                      ProgressBar(total=max(total, 1), completed=handled, width=12),
                      percent(handled, total), f"{today}/{budget}" if budget else "import",
                      number(total - handled), number(counts.get("done")),
                      number(counts.get("failed")), number(counts.get("skipped")),
                      number(stored.get("parts")), number(stored.get("vehicles")),
                      number(stored.get("fitments")), number(stored.get("xrefs")),
                      duration(eta))
    console.print(table)
    for reason in reasons:
        console.print(f"[yellow]{reason}[/]")
    for row in checkpoints:
        state = row["state"]
        position = ", ".join(f"{key}={state[key]}" for key in
                             ("rows", "items", "total", "complete", "file") if key in state)
        console.print(f"import {row['name']}: {position} "
                      f"(saved {row['updated_at'].astimezone():%Y-%m-%d %H:%M})", style="dim")
    exempt = sorted(name for name in settings.robots_exempt if name in ADAPTERS
                    and ADAPTERS[name].needs_robots_exemption)
    if exempt:
        console.print(f"[yellow]robots.txt exemption set for: {', '.join(exempt)}[/]")
    console.print("ETA assumes the daily budget is used every day. "
                  "Sources: " + "; ".join(f"{name}: {cls.note}" for name, cls in ADAPTERS.items()),
                  style="dim")


def cmd_seed(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    store = open_store(settings)
    try:
        for adapter in get_adapters(args.source):
            store.ensure_source(adapter.name, adapter.daily_budget)
            added = store.enqueue(adapter.name, adapter.seeds())
            if args.codes:
                added += store.enqueue(adapter.name,
                                       adapter.code_seeds(store.warehouse_codes(args.codes)))
            log.info("%s: %s new queue entries from seeds", adapter.name, added)
            console.print(f"{adapter.name}: {added} new queue entries")
    finally:
        store.close()


def cmd_seed_vehicles(args: argparse.Namespace) -> None:
    from parts_crawler.scheduler import auto_part_links
    from parts_crawler.sources.catcar import MAINTENANCE, WEAR_PARTS

    settings = load_settings()
    configure_logging(settings)
    [adapter] = get_adapters(args.source)
    pattern = None if args.all_groups else (
        MAINTENANCE if args.groups == "maintenance" else args.groups)
    if pattern:
        try:
            re.compile(pattern)
        except re.error as error:
            raise SystemExit(f"--groups is not a valid regular expression: {error}") from None
    parts = None if args.parts in {None, "all"} else (
        WEAR_PARTS if args.parts == "wear" else args.parts)
    if parts:
        try:
            re.compile(parts)
        except re.error as error:
            raise SystemExit(f"--parts is not a valid regular expression: {error}") from None
    store = open_store(settings)
    try:
        if args.all:
            part_names = WEAR_PARTS if args.parts is None else (parts or "")
            links, scanned = auto_part_links(adapter, store, pattern=pattern or "",
                                             parts=part_names)
            budget = store.source_row(adapter.name)["daily_budget"] or 1
            console.print(
                f"{adapter.name}: {scanned:,} stored vehicles form {len(links):,} families "
                "(same model, frame and engine). One vehicle per family is queued, the "
                "newest first.")
            console.print(
                f"Rough size: about {len(links) * 80:,} requests, {len(links) * 80 / budget:.0f} "
                f"days at {budget:,} requests a day, more while vehicles are still being "
                "discovered. Queueing again later only adds new families.", style="dim")
            if args.dry_run:
                console.print("Dry run: nothing was queued.")
                return
            added = sum(store.enqueue(adapter.name, links[start:start + 2000])
                        for start in range(0, len(links), 2000))
            log.info("%s: %s vehicle families queued for parts (groups: %s; parts: %s)",
                     adapter.name, added, pattern or "all", part_names or "all")
            console.print(f"{added:,} vehicles queued for parts. A crawl that is already "
                          f"running picks them up; otherwise start `run --source "
                          f"{adapter.name} --auto-parts`.")
            return
        rows = store.vehicles_for_seed(
            adapter.name, make=args.make or "", model=f"%{args.model}%" if args.model else "",
            code=f"%{args.code}%" if args.code else "",
            engine=f"%{args.engine}%" if args.engine else "", year=args.year, limit=args.limit)
        links = adapter.vehicle_seeds(rows, pattern, parts)
        table = Table(title=f"{adapter.name}: {len(links)} of {len(rows)} matching vehicles "
                            "can be seeded", title_justify="left")
        for column in ("make", "model", "code", "engine", "years"):
            table.add_column(column)
        for row in rows:
            table.add_row(row["make"], row["model"], row["model_code"], row["engine_code"],
                          f"{row['year_from'] or ''}-{row['year_to'] or ''}")
        console.print(table)
        if not links:
            console.print("Nothing to queue. Vehicles appear here after the source's "
                          "taxonomy crawl has stored them (`run --source "
                          f"{adapter.name}`).")
            return
        if args.dry_run:
            console.print("Dry run: nothing was queued.")
            return
        added = store.enqueue(adapter.name, links, refresh=True)
        log.info("%s: %s vehicles queued for parts (groups: %s)", adapter.name, added,
                 pattern or "all")
        console.print(f"{added} vehicles queued for parts "
                      f"({'groups matching ' + pattern if pattern else 'all groups'}). "
                      f"Start them with `run --source {adapter.name}`.")
    finally:
        store.close()


def cmd_requeue(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    store = Store(settings.database_url)
    try:
        for adapter in get_adapters(args.source):
            count = store.requeue_failed(adapter.name, include_skipped=args.include_skipped)
            log.info("%s: %s queue entries requeued", adapter.name, count)
            console.print(f"{adapter.name}: {count} requeued")
    finally:
        store.close()


def cmd_reset_queue(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    [adapter] = get_adapters(args.source)
    store = Store(settings.database_url)
    try:
        counts = store.queue_counts().get(adapter.name, {})
        console.print(f"{adapter.name} queue: " + (", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())) or "empty"))
        if not args.yes:
            console.print("Nothing deleted. Add --yes to forget this queue and its page "
                          "index; stored parts, vehicles and raw page files are kept.")
            return
        removed = store.reset_queue(adapter.name)
        log.warning("%s: queue reset, %s entries removed", adapter.name, removed)
        console.print(f"{adapter.name}: {removed} queue entries removed. "
                      "The next run queues the start pages again.")
    finally:
        store.close()


def cmd_purge_source(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    if not re.fullmatch(r"[a-z0-9_]{2,32}", args.source):
        raise SystemExit("--source must be one source name.")
    store = open_store(settings)
    try:
        counts = store.purge_source(args.source, apply=args.yes)
    finally:
        store.close()
    summary = ", ".join(f"{count:,} {name}" for name, count in counts.items())
    if args.yes:
        log.warning("%s: purged %s", args.source, summary)
        console.print(f"{args.source}: deleted {summary}.")
    else:
        console.print(f"{args.source}: would delete {summary}. Add --yes to delete.")


def cmd_state(args: argparse.Namespace, state: str) -> None:
    settings = load_settings()
    configure_logging(settings)
    store = Store(settings.database_url)
    try:
        for adapter in get_adapters(args.source):
            store.ensure_source(adapter.name, adapter.daily_budget)
            store.set_state(adapter.name, state, args.reason if state != "active" else None)
            log.info("%s: state set to %s (%s)", adapter.name, state, args.reason)
            console.print(f"{adapter.name}: {state}")
    finally:
        store.close()


def cmd_reparse(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings)
    raw = RawStore(settings.pages_dir)
    store = open_store(settings)
    try:
        for adapter in get_adapters(args.source):
            after, totals, errors = 0, {}, 0
            with Tracker(f"reparse {adapter.name}", None, unit="pages") as tracker:
                while rows := store.pages_for_reparse(adapter.name, adapter.parser_version,
                                                      after):
                    for row in rows:
                        after = row["id"]
                        page = CrawledPage(row["url"], row["kind"], row["context"] or {},
                                           row["http_status"], raw.load(row["storage_path"]),
                                           row["content_type"])
                        try:
                            result = adapter.parse(page)
                            counts = store.mark_reparsed(row, adapter.parser_version, result)
                        except Exception:
                            log.exception("reparse failed for %s", row["url"])
                            errors += 1
                            continue
                        for key, value in counts.items():
                            totals[key] = totals.get(key, 0) + value
                        tracker.advance()
            console.print(f"{adapter.name}: {totals} errors={errors}")
    finally:
        store.close()


def cmd_import_har(args: argparse.Namespace) -> None:
    from parts_crawler.har import import_har

    settings = load_settings()
    configure_logging(settings)
    [adapter] = get_adapters(args.source)
    console.print(import_har(settings, adapter, Path(args.har)))
    console.print("Delete the HAR file now; it may contain cookies or tokens.")


def cmd_import_vpic(args: argparse.Namespace) -> None:
    from parts_crawler.importers.vpic import import_vpic
    from parts_crawler.setup import admin_url

    settings = load_settings(require_database=False)
    configure_logging(settings)
    try:
        url = admin_url()
    except LocalSetupError as error:
        raise SystemExit(str(error)) from None
    console.print(import_vpic(settings, url, Path(args.archive) if args.archive else None,
                              lookups_only=args.lookups_only))


def cmd_import_ymm(args: argparse.Namespace) -> None:
    from parts_crawler.importers.ymm_csv import import_ymm

    settings = load_settings()
    configure_logging(settings)
    console.print(import_ymm(settings, Path(args.csv) if args.csv else None))


def cmd_import_fapi(args: argparse.Namespace) -> None:
    from parts_crawler.importers.fapi import import_fapi

    settings = load_settings()
    configure_logging(settings)
    console.print(import_fapi(settings, Path(args.dir) if args.dir else None))


def cmd_import_fueleconomy(args: argparse.Namespace) -> None:
    from parts_crawler.importers.fueleconomy import import_fueleconomy

    settings = load_settings()
    configure_logging(settings)
    console.print(import_fueleconomy(settings, Path(args.file) if args.file else None,
                                     restart=args.restart, refresh=args.refresh))


def cmd_import_wikidata(args: argparse.Namespace) -> None:
    from parts_crawler.importers.wikidata import import_wikidata

    settings = load_settings()
    configure_logging(settings)
    languages = tuple(code.strip().lower() for code in args.languages.split(",")
                      if code.strip())
    console.print(import_wikidata(settings, restart=args.restart, languages=languages))


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m parts_crawler", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup-local", help="apply crawler migrations, create the crawler login"
                        ).set_defaults(func=cmd_setup)
    run = commands.add_parser("run", help="crawl with live progress (Ctrl+C or kill stops safely)")
    run.add_argument("--source", default="all", help="all, or e.g. mann_api,ngk,catcar")
    run.add_argument("--max-pages", type=int, default=None, help="pages per source this run")
    run.add_argument("--seed-codes", type=int, default=0,
                     help="also queue code searches for this many warehouse codes")
    run.add_argument("--plain", action="store_true", help="line output instead of a table")
    run.add_argument("--auto-parts", action="store_true",
                     help="also queue wear parts for one vehicle per model, frame and engine "
                          "as vehicles are discovered (checked every 30 minutes)")
    run.set_defaults(func=cmd_run)
    commands.add_parser("status", help="queue progress, catalog totals and import checkpoints"
                        ).set_defaults(func=cmd_status)
    seed = commands.add_parser("seed", help="queue start pages again (duplicates ignored)")
    seed.add_argument("--source", default="all")
    seed.add_argument("--codes", type=int, default=0)
    seed.set_defaults(func=cmd_seed)
    deep = commands.add_parser(
        "seed-vehicles", help="queue part groups and part numbers for stored vehicles")
    deep.add_argument("--source", default="catcar")
    deep.add_argument("--make", help="e.g. TOYOTA")
    deep.add_argument("--model", help="part of the model name, e.g. COROLLA")
    deep.add_argument("--code", help="part of the model or frame code, e.g. NDE180")
    deep.add_argument("--engine", help="part of the engine code, e.g. 1ND")
    deep.add_argument("--year", type=int, help="a year the vehicle was produced in")
    deep.add_argument("--limit", type=int, default=10, help="most vehicles to queue (10)")
    deep.add_argument("--groups", default="maintenance",
                      help="regular expression on group names; 'maintenance' (default) "
                           "selects filters, brakes, plugs, belts and similar wear parts")
    deep.add_argument("--all-groups", action="store_true",
                      help="every group (thousands of requests per vehicle)")
    deep.add_argument("--all", action="store_true",
                      help="every stored vehicle family: one vehicle per model, frame and "
                           "engine, newest first (ignores the vehicle filters and --limit)")
    deep.add_argument("--parts",
                      help="regular expression on part names that get their own request; "
                           "'wear' selects filters, pads, discs, plugs, belts and similar "
                           "(the default with --all), 'all' takes every row")
    deep.add_argument("--dry-run", action="store_true", help="list the vehicles, queue nothing")
    deep.set_defaults(func=cmd_seed_vehicles)
    requeue = commands.add_parser("requeue-failed", help="retry failed URLs from scratch")
    requeue.add_argument("--source", default="all")
    requeue.add_argument("--include-skipped", action="store_true",
                         help="also retry URLs skipped as disallowed")
    requeue.set_defaults(func=cmd_requeue)
    reset = commands.add_parser("reset-queue", help="forget one source's queue (keeps data)")
    reset.add_argument("--source", required=True)
    reset.add_argument("--yes", action="store_true", help="delete; without it only counts")
    reset.set_defaults(func=cmd_reset_queue)
    purge = commands.add_parser("purge-source", help="delete everything one source stored")
    purge.add_argument("--source", required=True)
    purge.add_argument("--yes", action="store_true", help="delete; without it only counts")
    purge.set_defaults(func=cmd_purge_source)
    reparse = commands.add_parser("reparse", help="rerun parsers on stored pages, offline")
    reparse.add_argument("--source", required=True)
    reparse.set_defaults(func=cmd_reparse)
    for name, state in (("pause", "paused"), ("resume", "active")):
        command = commands.add_parser(name, help=f"mark sources {state}")
        command.add_argument("--source", required=True)
        command.add_argument("--reason", default="operator")
        command.set_defaults(func=lambda args, state=state: cmd_state(args, state))
    har = commands.add_parser("import-har", help="parse pages from a recorded HAR file")
    har.add_argument("--source", required=True)
    har.add_argument("--har", required=True)
    har.set_defaults(func=cmd_import_har)
    vpic = commands.add_parser("import-vpic", help="restore NHTSA vPIC and copy its lookups")
    vpic.add_argument("--archive", help="local vPICList_lite_*.plain.zip")
    vpic.add_argument("--lookups-only", action="store_true",
                      help="only copy WMI and make/model lookups from an existing restore")
    vpic.set_defaults(func=cmd_import_vpic)
    ymm = commands.add_parser("import-ymm", help="load the free year/make/model CSV")
    ymm.add_argument("--csv")
    ymm.set_defaults(func=cmd_import_ymm)
    fapi = commands.add_parser("import-fapi", help="load FAPI cross-reference files")
    fapi.add_argument("--dir", help="directory with the three FAPI .txt files")
    fapi.set_defaults(func=cmd_import_fapi)
    fuel = commands.add_parser("import-fueleconomy",
                               help="load EPA vehicle configurations (fueleconomy.gov)")
    fuel.add_argument("--file", help="local vehicles.csv or vehicles.csv.zip")
    fuel.add_argument("--restart", action="store_true", help="ignore the saved checkpoint")
    fuel.add_argument("--refresh", action="store_true", help="download the file again")
    fuel.set_defaults(func=cmd_import_fueleconomy)
    wikidata = commands.add_parser("import-wikidata",
                                   help="load car makes and models with multilingual names")
    wikidata.add_argument("--restart", action="store_true", help="start from the first page")
    wikidata.add_argument("--languages", default="en,ka,ru")
    wikidata.set_defaults(func=cmd_import_wikidata)
    args = parser.parse_args()
    stop_on_sigterm()
    try:
        args.func(args)
    except KeyboardInterrupt:
        log.warning("%s stopped by the operator", args.command)
        console.print("[yellow]Stopped. Run the same command again to continue.[/]")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
