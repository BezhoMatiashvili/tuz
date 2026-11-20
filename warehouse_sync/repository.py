"""Least-privilege PostgreSQL persistence through security-definer functions."""

from __future__ import annotations

from collections.abc import Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from warehouse_sync.integrity import version_integrity_tag
from warehouse_sync.models import SyncCounters, WarehouseProduct

ADVISORY_LOCK_KEY = 8_295_141_741


class WarehouseRepository:
    """Own one writer-role connection for one synchronisation process.

    The writer role has no table privileges. Every database mutation is made by
    narrowly scoped `warehouse.*` functions defined in the migration owner role.
    """

    def __init__(self, database_url: str, hmac_key: bytes) -> None:
        self._connection = psycopg.connect(database_url, row_factory=dict_row)
        self._hmac_key = hmac_key
        self._lock_acquired = False

    def close(self) -> None:
        try:
            if self._lock_acquired and not self._connection.closed:
                self._connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        except psycopg.Error:
            # Closing a broken connection releases its session advisory lock.
            pass
        finally:
            self._connection.close()

    def acquire_single_runner_lock(self) -> bool:
        with self._connection.transaction():
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (ADVISORY_LOCK_KEY,)
            ).fetchone()
        self._lock_acquired = bool(row["acquired"])
        return self._lock_acquired

    def start_run(self, expected_pages: int) -> int:
        with self._connection.transaction():
            row = self._connection.execute(
                "SELECT warehouse.start_sync_run(%s) AS run_id", (expected_pages,)
            ).fetchone()
        return int(row["run_id"])

    def record_page(
        self,
        *,
        run_id: int,
        page: int,
        products: Iterable[WarehouseProduct],
    ) -> SyncCounters:
        """Upsert a page and append a signed version for every changed row."""
        product_list = list(products)
        counters = SyncCounters(pages_fetched=1, rows_seen=len(product_list))

        with self._connection.transaction():
            self._connection.execute("SELECT warehouse.record_page_seen(%s, %s)", (run_id, page))
            for product in product_list:
                integrity_tag = version_integrity_tag(
                    self._hmac_key,
                    source_product_id=product.source_product_id,
                    observed_run_id=run_id,
                    source_page=page,
                    source_columns=product.source_columns,
                    row_hash=product.row_hash,
                )
                row = self._connection.execute(
                    """
                    SELECT warehouse.record_product(
                        %(run_id)s, %(source_page)s, %(source_product_id)s,
                        %(product_code)s, %(product_name)s, %(product_model)s,
                        %(original_code)s, %(duplicate_code)s, %(brand)s,
                        %(source_columns)s, %(row_hash)s, %(integrity_tag)s
                    ) AS outcome
                    """,
                    {
                        "run_id": run_id,
                        "source_page": page,
                        "source_product_id": product.source_product_id,
                        "product_code": product.product_code,
                        "product_name": product.product_name,
                        "product_model": product.product_model,
                        "original_code": product.original_code,
                        "duplicate_code": product.duplicate_code,
                        "brand": product.brand,
                        "source_columns": Jsonb(product.source_columns),
                        "row_hash": product.row_hash,
                        "integrity_tag": integrity_tag,
                    },
                ).fetchone()
                outcome = row["outcome"]
                if outcome == "added":
                    counters.rows_added += 1
                elif outcome == "changed":
                    counters.rows_changed += 1
                elif outcome == "unchanged":
                    counters.rows_unchanged += 1
                else:  # A database function changing this contract must fail closed.
                    raise RuntimeError("Unexpected warehouse persistence outcome.")
        return counters

    def finish_run(self, run_id: int, counters: SyncCounters, *, complete: bool) -> None:
        with self._connection.transaction():
            row = self._connection.execute(
                """
                SELECT warehouse.finish_sync_run(%s, %s, %s, %s, %s, %s, %s, %s) AS deactivated
                """,
                (
                    run_id,
                    complete,
                    counters.pages_fetched,
                    counters.rows_seen,
                    counters.rows_added,
                    counters.rows_changed,
                    counters.rows_unchanged,
                    counters.rows_deactivated,
                ),
            ).fetchone()
        counters.rows_deactivated = int(row["deactivated"])

    def close_abandoned_runs(self, current_run_id: int, window: int = 50) -> None:
        """Runs a process that died without a word left 'running' (SIGKILL, power loss, a
        lost connection) are closed as failed 'abandoned'. Called while this process holds
        the single-runner lock, so none of them is still alive. The writer may not read the
        runs table, so the ``window`` ids before this run are tried in turn: for a run that
        finished, fail_sync_run changes nothing."""
        with self._connection.transaction():
            for run_id in range(max(1, current_run_id - window), current_run_id):
                self._connection.execute(
                    "SELECT warehouse.fail_sync_run(%s, 0, 0, 0, 0, 0, 'abandoned')", (run_id,))

    def fail_run(self, run_id: int, counters: SyncCounters, failure: str) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "SELECT warehouse.fail_sync_run(%s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    counters.pages_fetched,
                    counters.rows_seen,
                    counters.rows_added,
                    counters.rows_changed,
                    counters.rows_unchanged,
                    failure,
                ),
            )
