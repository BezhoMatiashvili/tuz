"""parts_catalog persistence: durable queue leases, source state and catalog upserts."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from parts_crawler.records import (
    Link,
    ParseResult,
    PartRef,
    VehicleName,
    ascii_url,
    bounded,
    clean,
    normalize_brand,
    normalize_name,
)

SCHEMA_HINT = ("The database is missing migration 0011. Run "
               "`uv run python -m parts_crawler setup-local` once, then repeat the command.")
log = logging.getLogger("parts_crawler.store")
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")
SOURCE_LOCK = 1728036742  # advisory-lock class for "this source is being crawled"


@dataclass
class QueueItem:
    id: int
    source: str
    url: str
    kind: str
    priority: int
    depth: int
    context: dict
    attempts: int
    lease_token: str | None

    @classmethod
    def from_row(cls, row: dict) -> QueueItem:
        return cls(row["id"], row["source"], row["url"], row["kind"], row["priority"],
                   row["depth"], row["context"] or {}, row["attempts"], row["lease_token"])


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(
        database_url, row_factory=dict_row, autocommit=True, connect_timeout=10,
        options="-c application_name=parts-crawler -c statement_timeout=120000")


class Store:
    def __init__(self, database_url: str) -> None:
        self.conn = connect(database_url)

    def close(self) -> None:
        self.conn.close()

    def schema_ready(self) -> bool:
        """True once migration 0011 (vehicle detail, names, checkpoints) is applied."""
        return bool(self.conn.execute(
            "SELECT to_regclass('parts_catalog.import_checkpoints') IS NOT NULL AS ready"
        ).fetchone()["ready"])

    # Sources -------------------------------------------------------------------
    def ensure_source(self, source: str, daily_budget: int) -> None:
        self.conn.execute(
            "INSERT INTO parts_catalog.sources (source, daily_budget) VALUES (%s, %s) "
            "ON CONFLICT (source) DO NOTHING", (source, daily_budget))

    def source_row(self, source: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM parts_catalog.sources WHERE source = %s", (source,)).fetchone()
        if row is None:
            raise KeyError(source)
        return row

    def set_state(self, source: str, state: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE parts_catalog.sources SET state = %s, state_reason = %s, "
            "breaker_trips = CASE WHEN %s = 'active' THEN 0 ELSE breaker_trips END, "
            "updated_at = now() WHERE source = %s", (state, reason, state, source))

    def consume_budget(self, source: str) -> bool:
        row = self.conn.execute(
            """UPDATE parts_catalog.sources SET
                   budget_used = CASE WHEN budget_day = current_date THEN budget_used + 1
                                      ELSE 1 END,
                   budget_day = current_date, updated_at = now()
               WHERE source = %s AND daily_budget > 0
                 AND (budget_day IS DISTINCT FROM current_date OR budget_used < daily_budget)
               RETURNING budget_used""", (source,)).fetchone()
        return row is not None

    def lock_source(self, source: str) -> bool:
        """Hold a session lock so two processes never crawl one source at once.

        Two runs on one source would double the request rate to its host. The lock is
        released when this connection closes, including after a crash.
        """
        return bool(self.conn.execute(
            "SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS locked",
            (SOURCE_LOCK, source)).fetchone()["locked"])

    def release_unfinished(self, source: str) -> int:
        """Queue again the pages a dead run left leased; call only with the source lock.

        The attempt stays counted: a page that crashes the worker every time must run
        out of attempts instead of being retried on every start.
        """
        cursor = self.conn.execute(
            "UPDATE parts_catalog.crawl_queue SET status = 'pending', lease_token = NULL, "
            "lease_until = NULL, updated_at = now() "
            "WHERE source = %s AND status = 'running'", (source,))
        return cursor.rowcount

    def record_error(self, source: str, code: str, *, trip: bool = False) -> None:
        self.conn.execute(
            "UPDATE parts_catalog.sources SET last_error = %s, updated_at = now(), "
            "breaker_trips = breaker_trips + %s WHERE source = %s",
            (code[:200], 1 if trip else 0, source))

    # Queue ---------------------------------------------------------------------
    def enqueue(self, source: str, links: list[Link], depth: int = 0, *,
                refresh: bool = False) -> int:
        """Queue links; a URL already queued is left alone unless `refresh` is set.

        `refresh` puts an existing URL back to pending with the new context, which is
        how a vehicle is queued again with a different group filter.
        """
        usable = [(link, url) for link in links for url in (ascii_url(link.url),)
                  if url.startswith("https://") and len(url) <= 2048]
        if len(usable) < len(links):
            log.warning("%s: %s links dropped (not https, or longer than 2048 characters)",
                        source, len(links) - len(usable))
        # Refreshing only on a changed context keeps pages that link to each other
        # (catalog tabs) from re-queueing one another for ever.
        conflicts = {
            True: ("DO UPDATE SET status = 'pending', kind = EXCLUDED.kind, "
                   "priority = EXCLUDED.priority, context = EXCLUDED.context, attempts = 0, "
                   "next_attempt_at = now(), last_error = NULL, updated_at = now() "
                   "WHERE parts_catalog.crawl_queue.status <> 'running' "
                   "AND parts_catalog.crawl_queue.context IS DISTINCT FROM EXCLUDED.context"),
            False: "DO NOTHING"}
        added = 0
        for flag, conflict in conflicts.items():
            rows = [(source, url, link.kind, link.priority, depth, Jsonb(link.context))
                    for link, url in usable if (refresh or link.refresh) is flag]
            if not rows:
                continue
            with self.conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO parts_catalog.crawl_queue "
                    "(source, url, kind, priority, depth, context) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (source, url) " + conflict, rows)
                added += max(cursor.rowcount, 0)
        return added

    def has_queue(self, source: str) -> bool:
        return self.conn.execute(
            "SELECT EXISTS (SELECT 1 FROM parts_catalog.crawl_queue WHERE source = %s)",
            (source,)).fetchone()["exists"]

    def claim(self, source: str, token: str, lease_seconds: int, *,
              below: bool | None = None, split: int = 80) -> QueueItem | None:
        """Lease the next due URL. `below` restricts it to priorities under (True) or
        from (False) `split`, which lets a worker alternate between two kinds of work."""
        band = "" if below is None else (
            "AND priority < %(split)s" if below else "AND priority >= %(split)s")
        row = self.conn.execute(
            f"""WITH next AS (
                   SELECT id FROM parts_catalog.crawl_queue
                   WHERE source = %(source)s AND status IN ('pending', 'running')
                     AND (status = 'pending' OR lease_until < now())
                     AND next_attempt_at <= now() {band}
                   ORDER BY priority, next_attempt_at, id
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
               UPDATE parts_catalog.crawl_queue q SET status = 'running',
                   attempts = q.attempts + 1, lease_token = %(token)s,
                   lease_until = now() + make_interval(secs => %(lease)s), updated_at = now()
               FROM next WHERE q.id = next.id RETURNING q.*""",  # nosec B608
            {"source": source, "token": token, "lease": lease_seconds,
             "split": split}).fetchone()
        return QueueItem.from_row(row) if row else None

    def iter_vehicles(self, source: str, batch: int = 5000):
        """Every stored vehicle of a source, read in pages so memory stays flat."""
        after = 0
        while True:
            rows = self.conn.execute(
                "SELECT id, source_vehicle_id, make, model, model_code, engine_code, "
                "year_from, year_to, raw FROM parts_catalog.vehicles "
                "WHERE source = %s AND id > %s ORDER BY id LIMIT %s",
                (source, after, batch)).fetchall()
            if not rows:
                return
            yield from rows
            after = rows[-1]["id"]

    def release(self, item: QueueItem) -> None:
        self.conn.execute(
            "UPDATE parts_catalog.crawl_queue SET status = 'pending', "
            "attempts = GREATEST(attempts - 1, 0), lease_token = NULL, lease_until = NULL, "
            "updated_at = now() WHERE id = %s AND lease_token = %s",
            (item.id, item.lease_token))

    def mark(self, item: QueueItem, status: str, *, error: str | None = None,
             http_status: int | None = None, sha: str | None = None) -> None:
        self.conn.execute(
            "UPDATE parts_catalog.crawl_queue SET status = %s, last_error = %s, "
            "http_status = COALESCE(%s, http_status), content_sha = COALESCE(%s, content_sha), "
            "lease_token = NULL, lease_until = NULL, updated_at = now() "
            "WHERE id = %s AND lease_token IS NOT DISTINCT FROM %s",
            (status, error, http_status, sha, item.id, item.lease_token))

    def retry_later(self, item: QueueItem, error: str, delay: float, max_attempts: int,
                    http_status: int | None = None) -> str:
        if item.attempts >= max_attempts:
            self.mark(item, "failed", error=f"RETRY_EXHAUSTED:{error}", http_status=http_status)
            return "failed"
        self.conn.execute(
            "UPDATE parts_catalog.crawl_queue SET status = 'pending', last_error = %s, "
            "http_status = COALESCE(%s, http_status), "
            "next_attempt_at = now() + make_interval(secs => %s), lease_token = NULL, "
            "lease_until = NULL, updated_at = now() WHERE id = %s AND lease_token = %s",
            (error, http_status, max(1.0, delay), item.id, item.lease_token))
        return "pending"

    def next_due_seconds(self, source: str) -> float | None:
        row = self.conn.execute(
            """SELECT EXTRACT(EPOCH FROM (min(CASE WHEN status = 'pending'
                                                   THEN next_attempt_at ELSE lease_until END)
                                          - now()))::float AS seconds,
                      count(*) AS open
               FROM parts_catalog.crawl_queue
               WHERE source = %s AND status IN ('pending', 'running')""", (source,)).fetchone()
        return None if not row["open"] else max(0.0, row["seconds"] or 0.0)

    def requeue_failed(self, source: str, *, include_skipped: bool = False) -> int:
        statuses = ["failed", "skipped"] if include_skipped else ["failed"]
        cursor = self.conn.execute(
            "UPDATE parts_catalog.crawl_queue SET status = 'pending', attempts = 0, "
            "next_attempt_at = now(), last_error = NULL, updated_at = now() "
            "WHERE source = %s AND status = ANY(%s)", (source, statuses))
        return cursor.rowcount

    def reset_queue(self, source: str) -> int:
        """Forget a source's queue and page index so its seeds are queued afresh.

        Stored catalog rows and the raw page files on disk are kept.
        """
        cursor = self.conn.execute(
            "DELETE FROM parts_catalog.crawl_queue WHERE source = %s", (source,))
        return cursor.rowcount

    def upsert_done_item(self, source: str, url: str, kind: str, context: dict) -> QueueItem:
        row = self.conn.execute(
            "INSERT INTO parts_catalog.crawl_queue (source, url, kind, context, status) "
            "VALUES (%s, %s, %s, %s, 'running') ON CONFLICT (source, url) DO UPDATE SET "
            "status = 'running', lease_token = NULL, updated_at = now() RETURNING *",
            (source, url, kind, Jsonb(context))).fetchone()
        return QueueItem.from_row(row)

    # Pages and catalog -----------------------------------------------------------
    def save_page(self, item: QueueItem, *, url: str, http_status: int, content_type: str,
                  sha: str, storage_path: str, size: int, parser_version: str | None,
                  result: ParseResult | None, parse_error: str | None = None) -> dict:
        counts = {"links": 0, "parts": 0, "vehicles": 0, "fitments": 0, "xrefs": 0}
        problem = parse_error or "PARSE_ERROR" if result is None else None
        with self.conn.transaction():
            if result is not None:
                try:
                    # A savepoint: a record the database rejects undoes this page's rows
                    # only. The page index below still commits, so `reparse` can retry
                    # the stored page after a parser fix, and the source keeps running.
                    with self.conn.transaction():
                        counts.update(self.write_result(item.source, result, url))
                        counts["links"] = self.enqueue(item.source, result.links,
                                                       item.depth + 1)
                except (psycopg.DataError, psycopg.IntegrityError, TypeError) as error:
                    counts = dict.fromkeys(counts, 0) | {"store_errors": 1}
                    problem = f"STORE_ERROR:{type(error).__name__}"
            stored = problem is None
            self.conn.execute(
                """INSERT INTO parts_catalog.raw_pages (queue_id, source, url, http_status,
                       content_type, content_sha, storage_path, bytes, parser_version, parsed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END)
                   ON CONFLICT (queue_id) DO UPDATE SET url = EXCLUDED.url,
                       http_status = EXCLUDED.http_status, content_type = EXCLUDED.content_type,
                       content_sha = EXCLUDED.content_sha, storage_path = EXCLUDED.storage_path,
                       bytes = EXCLUDED.bytes, fetched_at = now(),
                       parser_version = EXCLUDED.parser_version, parsed_at = EXCLUDED.parsed_at""",
                (item.id, item.source, url, http_status, content_type[:200], sha, storage_path,
                 size, parser_version if stored else None, stored))
            if stored:
                self.mark(item, "done", http_status=http_status, sha=sha)
            else:
                self.mark(item, "failed", error=problem, http_status=http_status, sha=sha)
        return counts

    def pages_for_reparse(self, source: str, parser_version: str, after_id: int,
                          limit: int = 200) -> list[dict]:
        return self.conn.execute(
            """SELECT r.url, r.storage_path, r.content_type, r.http_status,
                      q.id, q.source, q.kind, q.context, q.depth, q.priority, q.attempts,
                      q.lease_token
               FROM parts_catalog.raw_pages r
               JOIN parts_catalog.crawl_queue q ON q.id = r.queue_id
               WHERE r.source = %s AND r.parser_version IS DISTINCT FROM %s AND r.queue_id > %s
               ORDER BY r.queue_id LIMIT %s""",
            (source, parser_version, after_id, limit)).fetchall()

    def mark_reparsed(self, row: dict, parser_version: str, result: ParseResult) -> dict:
        item = QueueItem.from_row(row)
        with self.conn.transaction():
            counts = self.write_result(item.source, result, row["url"])
            counts["links"] = self.enqueue(item.source, result.links, item.depth + 1)
            self.conn.execute(
                "UPDATE parts_catalog.raw_pages SET parser_version = %s, parsed_at = now() "
                "WHERE queue_id = %s", (parser_version, item.id))
            self.conn.execute(
                "UPDATE parts_catalog.crawl_queue SET status = 'done', last_error = NULL, "
                "updated_at = now() WHERE id = %s AND status = 'failed' "
                "AND (last_error LIKE 'PARSE_ERROR%%' OR last_error LIKE 'STORE_ERROR%%')",
                (item.id,))
        return counts

    def vehicles_for_seed(self, source: str, *, make: str = "", model: str = "",
                          code: str = "", engine: str = "", year: int | None = None,
                          limit: int = 10) -> list[dict]:
        """Stored vehicles of one source matching the given filters, newest first."""
        return self.conn.execute(
            """SELECT source_vehicle_id, make, model, model_code, engine_code, year_from,
                      year_to, label, raw
               FROM parts_catalog.vehicles
               WHERE source = %(source)s
                 AND (%(make)s = '' OR make_norm = %(make)s)
                 AND (%(model)s = '' OR model ILIKE %(model)s)
                 AND (%(code)s = '' OR model_code ILIKE %(code)s)
                 AND (%(engine)s = '' OR engine_code ILIKE %(engine)s)
                 AND (%(year)s::int IS NULL OR (COALESCE(year_from, 0) <= %(year)s
                      AND COALESCE(year_to, 9999) >= %(year)s))
               ORDER BY year_from DESC NULLS LAST, source_vehicle_id LIMIT %(limit)s""",
            {"source": source, "make": normalize_brand(make), "model": model, "code": code,
             "engine": engine, "year": year, "limit": limit}).fetchall()

    def known_makes(self) -> list[str]:
        """Distinct make names already in the catalog, longest first."""
        rows = self.conn.execute(
            "SELECT DISTINCT make FROM parts_catalog.vehicles "
            "WHERE source IN ('ymm_csv', 'fueleconomy', 'vpic') AND length(make) >= 2")
        return sorted({row["make"].strip() for row in rows}, key=len, reverse=True)

    def _part_id(self, ref: PartRef, source: str, name: str = "", kind: str = "unknown",
                 cache: dict | None = None) -> tuple[int, bool] | None:
        if not ref.valid:
            return None
        brand_norm, normalized = ref.key
        if cache is not None and (brand_norm, normalized) in cache:
            return cache[(brand_norm, normalized)], False
        row = self.conn.execute(
            """INSERT INTO parts_catalog.parts
                   (brand, brand_norm, number, normalized, name, kind, first_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (brand_norm, normalized) DO UPDATE SET
                   name = CASE WHEN parts_catalog.parts.name = '' THEN EXCLUDED.name
                               ELSE parts_catalog.parts.name END,
                   kind = CASE WHEN parts_catalog.parts.kind = 'unknown' THEN EXCLUDED.kind
                               ELSE parts_catalog.parts.kind END
               WHERE (parts_catalog.parts.name = '' AND EXCLUDED.name <> '')
                  OR (parts_catalog.parts.kind = 'unknown' AND EXCLUDED.kind <> 'unknown')
               RETURNING id, (xmax = 0) AS inserted""",
            (clean(ref.brand, 120), brand_norm, clean(ref.number, 120), normalized,
             clean(name, 300), kind, source)).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT id, false AS inserted FROM parts_catalog.parts "
                "WHERE brand_norm = %s AND normalized = %s", (brand_norm, normalized)).fetchone()
        if cache is not None:
            cache[(brand_norm, normalized)] = row["id"]
        return row["id"], bool(row["inserted"])

    def write_result(self, source: str, result: ParseResult, url: str) -> dict[str, Any]:
        counts = {"parts": 0, "vehicles": 0, "fitments": 0, "xrefs": 0}
        cache: dict = {}
        for part in result.parts:
            found = self._part_id(part.ref, source, part.name, part.kind, cache)
            counts["parts"] += bool(found and found[1])
        vehicle_ids: dict[str, int] = {}
        for vehicle in result.vehicles:
            key = clean(vehicle.key, 300)
            if not key or not vehicle.make:
                continue
            # Out-of-range numbers become NULL so one odd value cannot fail the whole page.
            row = self.conn.execute(
                """INSERT INTO parts_catalog.vehicles AS v (source, source_vehicle_id, make,
                       make_norm, model, year_from, year_to, engine, body, market, label, raw,
                       model_code, engine_code, displacement_cc, power_kw, fuel, transmission,
                       drive)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source, source_vehicle_id) DO UPDATE SET make = EXCLUDED.make,
                       make_norm = EXCLUDED.make_norm, model = EXCLUDED.model,
                       year_from = COALESCE(EXCLUDED.year_from, v.year_from),
                       year_to = COALESCE(EXCLUDED.year_to, v.year_to),
                       engine = EXCLUDED.engine, body = EXCLUDED.body, market = EXCLUDED.market,
                       label = EXCLUDED.label, raw = EXCLUDED.raw,
                       model_code = COALESCE(NULLIF(EXCLUDED.model_code, ''), v.model_code),
                       engine_code = COALESCE(NULLIF(EXCLUDED.engine_code, ''), v.engine_code),
                       displacement_cc = COALESCE(EXCLUDED.displacement_cc, v.displacement_cc),
                       power_kw = COALESCE(EXCLUDED.power_kw, v.power_kw),
                       fuel = COALESCE(NULLIF(EXCLUDED.fuel, ''), v.fuel),
                       transmission = COALESCE(NULLIF(EXCLUDED.transmission, ''), v.transmission),
                       drive = COALESCE(NULLIF(EXCLUDED.drive, ''), v.drive),
                       observed_at = now()
                   RETURNING id, (xmax = 0) AS inserted""",
                (source, key, clean(vehicle.make, 120), normalize_brand(vehicle.make),
                 clean(vehicle.model, 200), bounded(vehicle.year_from, 1900, 2100),
                 bounded(vehicle.year_to, 1900, 2100),
                 clean(vehicle.engine, 200), clean(vehicle.body, 120),
                 clean(vehicle.market, 60), clean(vehicle.label, 500),
                 Jsonb(vehicle.raw), clean(vehicle.model_code, 120),
                 clean(vehicle.engine_code, 60), bounded(vehicle.displacement_cc, 1, 100000),
                 bounded(vehicle.power_kw, 1, 10000), clean(vehicle.fuel, 60),
                 clean(vehicle.transmission, 120), clean(vehicle.drive, 60))).fetchone()
            vehicle_ids[key] = row["id"]
            counts["vehicles"] += bool(row["inserted"])
        for fitment in result.fitments:
            key = clean(fitment.vehicle_key, 300)
            vehicle_id = vehicle_ids.get(key)
            if vehicle_id is None:
                row = self.conn.execute(
                    "SELECT id FROM parts_catalog.vehicles WHERE source = %s "
                    "AND source_vehicle_id = %s", (source, key)).fetchone()
                if row is None:
                    continue
                vehicle_id = vehicle_ids[key] = row["id"]
            found = self._part_id(fitment.part, source, cache=cache)
            if not found:
                continue
            counts["parts"] += found[1]
            cursor = self.conn.execute(
                """INSERT INTO parts_catalog.part_fitments
                       (part_id, vehicle_id, source, note, position, quantity, source_url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (found[0], vehicle_id, source, clean(fitment.note, 300),
                 clean(fitment.position, 200), clean(fitment.quantity, 40), url[:2048]))
            counts["fitments"] += max(cursor.rowcount, 0)
        for cross in result.cross_refs:
            left = self._part_id(cross.part, source, cache=cache)
            right = self._part_id(cross.other, source,
                                  kind="oe" if cross.relation == "oe" else "unknown", cache=cache)
            if not left or not right or left[0] == right[0]:
                continue
            counts["parts"] += left[1] + right[1]
            cursor = self.conn.execute(
                """INSERT INTO parts_catalog.part_cross_refs
                       (part_id, other_part_id, source, relation, source_url)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (left[0], right[0], source, cross.relation, url[:2048]))
            counts["xrefs"] += max(cursor.rowcount, 0)
        return counts

    # Reporting -----------------------------------------------------------------
    def queue_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(
                "SELECT source, status, count(*) AS n FROM parts_catalog.crawl_queue "
                "GROUP BY source, status"):
            counts.setdefault(row["source"], {})[row["status"]] = row["n"]
        return counts

    def catalog_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for column, sql in (
            ("parts", "SELECT first_source AS source, count(*) AS n FROM parts_catalog.parts "
                      "GROUP BY 1"),
            ("vehicles", "SELECT source, count(*) AS n FROM parts_catalog.vehicles GROUP BY 1"),
            ("fitments", "SELECT source, count(*) AS n FROM parts_catalog.part_fitments "
                         "GROUP BY 1"),
            ("xrefs", "SELECT source, count(*) AS n FROM parts_catalog.part_cross_refs "
                      "GROUP BY 1"),
        ):
            for row in self.conn.execute(sql):
                counts.setdefault(row["source"], {})[column] = row["n"]
        return counts

    def sources(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM parts_catalog.sources ORDER BY source").fetchall()

    def warehouse_codes(self, limit: int) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT normalized, max(manufacturer) AS manufacturer FROM automotive.part_numbers "
            "WHERE length(normalized) >= 4 GROUP BY normalized ORDER BY normalized LIMIT %s",
            (limit,)).fetchall()
        return [(row["normalized"], row["manufacturer"] or "") for row in rows]

    def start_run(self, sources: list[str]) -> int:
        return self.conn.execute(
            "INSERT INTO parts_catalog.crawl_runs (sources) VALUES (%s) RETURNING id",
            (sources,)).fetchone()["id"]

    def finish_run(self, run_id: int, fetched: int, failed: int, stats: dict,
                   stop_reason: str) -> None:
        self.conn.execute(
            "UPDATE parts_catalog.crawl_runs SET finished_at = now(), fetched = %s, "
            "failed = %s, stats = %s, stop_reason = %s WHERE id = %s",
            (fetched, failed, Jsonb(stats), stop_reason[:200], run_id))

    # Importer checkpoints ----------------------------------------------------------
    # An importer commits each batch together with its checkpoint, so stopping it at
    # any moment loses nothing and the next run continues after the last full batch.
    def checkpoint(self, name: str) -> dict:
        row = self.conn.execute(
            "SELECT state FROM parts_catalog.import_checkpoints WHERE name = %s",
            (name,)).fetchone()
        return dict(row["state"]) if row else {}

    def set_checkpoint(self, name: str, state: dict) -> None:
        self.conn.execute(
            "INSERT INTO parts_catalog.import_checkpoints (name, state) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            (name, Jsonb(state)))

    def clear_checkpoint(self, name: str) -> None:
        self.conn.execute(
            "DELETE FROM parts_catalog.import_checkpoints WHERE name = %s", (name,))

    # Bulk writes ---------------------------------------------------------------------
    def upsert_vehicle_names(self, source: str, names: Iterable[VehicleName]) -> int:
        rows = []
        for item in names:
            name = clean(item.name, 300)
            ref = clean(item.ref, 300)
            language = (item.language or "").lower()
            if (not name or not ref or not normalize_name(name)
                    or item.kind not in {"make", "model"}
                    or item.name_kind not in {"label", "alias"}
                    or not LANGUAGE.match(language)):
                continue
            rows.append((source, ref, item.kind, clean(item.make, 120),
                         normalize_brand(item.make), language, name, normalize_name(name),
                         item.name_kind, bounded(item.year_from, 1800, 2100),
                         bounded(item.year_to, 1800, 2100), Jsonb(item.raw)))
        if not rows:
            return 0
        with self.conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO parts_catalog.vehicle_names AS n (source, source_ref, kind, make,
                       make_norm, language, name, name_norm, name_kind, year_from, year_to, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source, source_ref, language, name_kind, name) DO UPDATE SET
                       kind = EXCLUDED.kind, make = EXCLUDED.make,
                       make_norm = EXCLUDED.make_norm,
                       year_from = COALESCE(EXCLUDED.year_from, n.year_from),
                       year_to = COALESCE(EXCLUDED.year_to, n.year_to),
                       raw = EXCLUDED.raw, observed_at = now()""", rows)
        return len(rows)

    def bulk_parts(self, source: str, rows: Iterable[tuple[str, str, str]],
                   kind: str = "unknown") -> int:
        """Insert many (brand, number, name) parts in one statement; existing parts stay."""
        columns: tuple[list[str], ...] = ([], [], [], [], [])
        for brand, number, name in rows:
            ref = PartRef(brand, number)
            if not ref.valid:
                continue
            brand_norm, normalized = ref.key
            for column, value in zip(columns, (clean(brand, 120), brand_norm,
                                               clean(number, 120), normalized,
                                               clean(name, 300)), strict=True):
                column.append(value)
        if not columns[0]:
            return 0
        cursor = self.conn.execute(
            """INSERT INTO parts_catalog.parts
                   (brand, brand_norm, number, normalized, name, kind, first_source)
               SELECT DISTINCT ON (t.brand_norm, t.normalized)
                      t.brand, t.brand_norm, t.number, t.normalized, t.name, %s, %s
               FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[])
                    AS t(brand, brand_norm, number, normalized, name)
               ORDER BY t.brand_norm, t.normalized
               ON CONFLICT (brand_norm, normalized) DO NOTHING""",
            (kind, source, *columns))
        return max(cursor.rowcount, 0)

    def purge_source(self, source: str, *, apply: bool = False) -> dict[str, int]:
        """Count, and with `apply` delete, everything one source contributed.

        Parts that another source also refers to are kept, with the purged source's
        name text removed so a remaining source can fill it in again.
        """
        counts: dict[str, int] = {}
        owned = """p.first_source = %(source)s
            AND NOT EXISTS (SELECT 1 FROM parts_catalog.part_fitments f
                            WHERE f.part_id = p.id AND f.source <> %(source)s)
            AND NOT EXISTS (SELECT 1 FROM parts_catalog.part_cross_refs x
                            WHERE (x.part_id = p.id OR x.other_part_id = p.id)
                              AND x.source <> %(source)s)"""
        values = {"source": source}
        with self.conn.transaction():
            for key, table in (("xrefs", "part_cross_refs"), ("fitments", "part_fitments"),
                               ("vehicles", "vehicles"), ("names", "vehicle_names")):
                counts[key] = self.conn.execute(
                    f"SELECT count(*) AS n FROM parts_catalog.{table} "  # nosec B608
                    "WHERE source = %(source)s", values).fetchone()["n"]
            counts["parts"] = self.conn.execute(
                f"SELECT count(*) AS n FROM parts_catalog.parts p WHERE {owned}",  # nosec B608
                values).fetchone()["n"]
            if apply:
                for table in ("part_cross_refs", "part_fitments", "vehicles", "vehicle_names"):
                    self.conn.execute(
                        f"DELETE FROM parts_catalog.{table} "  # nosec B608
                        "WHERE source = %(source)s", values)
                self.conn.execute(
                    f"DELETE FROM parts_catalog.parts p WHERE {owned}", values)  # nosec B608
                self.conn.execute(
                    """UPDATE parts_catalog.parts p SET name = '', first_source = COALESCE(
                           (SELECT min(f.source) FROM parts_catalog.part_fitments f
                            WHERE f.part_id = p.id),
                           (SELECT min(x.source) FROM parts_catalog.part_cross_refs x
                            WHERE x.part_id = p.id OR x.other_part_id = p.id),
                           p.first_source)
                       WHERE p.first_source = %(source)s""", values)
        return counts
