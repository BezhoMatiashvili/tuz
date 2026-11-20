"""Orchestration for one full or intentionally partial source synchronisation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from warehouse_sync.client import WarehouseClient
from warehouse_sync.config import Settings
from warehouse_sync.errors import (
    SourceChangedError,
    SourceProtocolError,
    SyncAlreadyRunning,
    failure_code,
)
from warehouse_sync.models import ParsedPage, SyncCounters
from warehouse_sync.parser import parse_products_page
from warehouse_sync.repository import WarehouseRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Safe summary returned to the command-line entry point."""

    pages_planned: int
    complete: bool
    counters: SyncCounters


def _fetch_and_parse(client: WarehouseClient, page: int, attempts: int) -> ParsedPage:
    for attempt in range(1, attempts + 1):
        html = client.fetch_products_page(page)
        try:
            parsed = parse_products_page(html)
            if not parsed.products:
                raise SourceProtocolError("No product rows in source response.")
            return parsed
        except SourceProtocolError as exc:
            if attempt == attempts:
                raise SourceProtocolError(
                    f"Page {page} returned invalid or empty product data after {attempts} attempts."
                ) from exc
            delay = min(30, 2 ** (attempt - 1))
            LOGGER.warning(
                "Invalid or empty source page %s; retrying attempt %s/%s in %ss.",
                page, attempt + 1, attempts, delay,
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def sync(settings: Settings, *, max_pages: int | None = None, dry_run: bool = False) -> SyncResult:
    """Retry a whole idempotent scan after a lost PostgreSQL connection.

    A fresh scan is required because skipping pages after a restart could miss
    products when the source's offset-based pagination changes.
    """
    for attempt in range(1, settings.max_retries + 1):
        try:
            return _sync_once(settings, max_pages=max_pages, dry_run=dry_run)
        except SourceChangedError:
            if attempt == settings.max_retries:
                raise
            delay = min(30, 2 ** (attempt - 1))
            LOGGER.warning(
                "Source changed during scan; restarting from page 1 in %ss (attempt %s/%s).",
                delay, attempt + 1, settings.max_retries,
            )
            time.sleep(delay)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            sqlstate = exc.sqlstate
            transient = (
                sqlstate is None
                or sqlstate.startswith("08")
                or sqlstate in {"40001", "40P01", "53300", "57P01", "57P02", "57P03"}
            )
            if not transient or attempt == settings.max_retries:
                raise
            delay = min(30, 2 ** (attempt - 1))
            LOGGER.warning(
                "Database connection lost (%s); restarting scan from page 1 in %ss "
                "(attempt %s/%s). Previously committed products remain saved.",
                type(exc).__name__, delay, attempt + 1, settings.max_retries,
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def _sync_once(settings: Settings, *, max_pages: int | None, dry_run: bool) -> SyncResult:
    """Synchronise product pages without making any source-side change."""
    client = WarehouseClient(settings)
    try:
        LOGGER.info("Connecting to source and fetching first page...")
        first_page = _fetch_and_parse(client, 1, settings.max_retries)
        pages_available = first_page.total_pages
        if max_pages is not None and max_pages < 1:
            raise ValueError("--max-pages must be at least 1")
        pages_planned = min(pages_available, max_pages) if max_pages else pages_available
        complete = pages_planned == pages_available

        if dry_run:
            counters = SyncCounters(pages_fetched=1, rows_seen=len(first_page.products))
            return SyncResult(pages_planned=pages_planned, complete=complete, counters=counters)

        if settings.database_url is None:
            raise ValueError("DATABASE_URL is required unless --dry-run is used.")

        if settings.hmac_key is None:
            raise ValueError("A version-integrity key is required for synchronisation.")
        repository = WarehouseRepository(settings.database_url, settings.hmac_key)
        try:
            if not repository.acquire_single_runner_lock():
                raise SyncAlreadyRunning("Another warehouse synchronisation is already running.")
            run_id = repository.start_run(pages_planned)
            counters = SyncCounters()
            signatures: list[tuple[tuple[str, str], ...]] = []
            seen_ids: set[str] = set()
            expected_page_size = len(first_page.products)
            try:
                # This process holds the lock: any other run still marked 'running' died.
                repository.close_abandoned_runs(run_id)
                _check_page(first_page, 1, pages_available, expected_page_size, seen_ids)
                signatures.append(_page_signature(first_page))
                counters.add(
                    repository.record_page(run_id=run_id, page=1, products=first_page.products)
                )
                _log_progress(1, pages_planned, counters)
                for page in range(2, pages_planned + 1):
                    time.sleep(settings.request_delay_seconds)
                    parsed = _fetch_and_parse(client, page, settings.max_retries)
                    _check_page(parsed, page, pages_available, expected_page_size, seen_ids)
                    signatures.append(_page_signature(parsed))
                    counters.add(
                        repository.record_page(run_id=run_id, page=page, products=parsed.products)
                    )
                    _log_progress(page, pages_planned, counters)
                if complete:
                    LOGGER.info("Checking source pages a second time for changes...")
                    for page in range(1, pages_planned + 1):
                        time.sleep(settings.request_delay_seconds)
                        verified = _fetch_and_parse(client, page, settings.max_retries)
                        if page == 1 and verified.total_pages != pages_available:
                            raise SourceChangedError("Source page count changed during scan.")
                        if _page_signature(verified) != signatures[page - 1]:
                            raise SourceChangedError(
                                f"Source page {page} changed during scan; retry the scan."
                            )
                        if page % 100 == 0 or page == pages_planned:
                            LOGGER.info("Verified %s/%s source pages", page, pages_planned)
                    LOGGER.warning(
                        "Source checks passed, but no snapshot guarantee is available; "
                        "automatic product deactivation is disabled."
                    )
                # Offset pagination cannot prove that the source supplied a consistent
                # snapshot. Never deactivate products based solely on this scan.
                repository.finish_run(run_id, counters, complete=False)
            except Exception as exc:
                LOGGER.error(
                    "Sync stopped after %s/%s saved pages: %s (%s).",
                    counters.pages_fetched, pages_planned, failure_code(exc), type(exc).__name__,
                )
                try:
                    repository.fail_run(run_id, counters, failure_code(exc))
                except Exception as record_error:
                    LOGGER.error(
                        "Could not record failed-run status (%s).", type(record_error).__name__
                    )
                raise
        finally:
            repository.close()
    finally:
        client.close()

    return SyncResult(pages_planned=pages_planned, complete=complete, counters=counters)


def _page_signature(page: ParsedPage) -> tuple[tuple[str, str], ...]:
    return tuple((product.source_product_id, product.row_hash) for product in page.products)


def _check_page(
    parsed: ParsedPage, page: int, total: int, page_size: int, seen_ids: set[str]
) -> None:
    if page < total and len(parsed.products) != page_size:
        raise SourceProtocolError(f"Source page {page} is incomplete; scan stopped.")
    if len(parsed.products) > page_size:
        raise SourceProtocolError(f"Source page {page} has an unexpected size; scan stopped.")
    ids = {product.source_product_id for product in parsed.products}
    if ids & seen_ids:
        raise SourceChangedError(f"Duplicate product across pages at page {page}; scan stopped.")
    seen_ids.update(ids)


def _log_progress(page: int, pages_planned: int, counters: SyncCounters) -> None:
    """Report committed page progress without logging product data or credentials."""
    LOGGER.info(
        "Page %s/%s (%.1f%%) | seen=%s added=%s changed=%s unchanged=%s",
        page,
        pages_planned,
        100 * page / pages_planned,
        counters.rows_seen,
        counters.rows_added,
        counters.rows_changed,
        counters.rows_unchanged,
    )
