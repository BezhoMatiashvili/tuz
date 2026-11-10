"""Restore NHTSA's standalone vPIC PostgreSQL database into the `vpic` schema.

The plain dump targets PostgreSQL 17+. Restoring it into the local 16.x server works
after skipping psql meta-commands and the 17-only `transaction_timeout` setting, so the
SQL is streamed statement by statement inside one transaction (all or nothing).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path

import psycopg
import requests
from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn

from parts_crawler.config import Settings
from parts_crawler.importers import download
from parts_crawler.term import console

log = logging.getLogger("parts_crawler.importers.vpic")
DOWNLOADS = "https://vpic.nhtsa.dot.gov/Downloads"
COPY = re.compile(r"^\s*COPY\s+.+\s+FROM\s+stdin\s*;\s*$", re.I | re.S)
DOLLAR = re.compile(r"\$[A-Za-z_0-9]*\$")
SKIP = re.compile(r"^\s*SET\s+transaction_timeout\b", re.I)
GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'parts_crawler') THEN
        GRANT USAGE ON SCHEMA vpic TO parts_crawler;
        GRANT SELECT ON ALL TABLES IN SCHEMA vpic TO parts_crawler;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA vpic TO parts_crawler;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automotive_runtime') THEN
        GRANT USAGE ON SCHEMA vpic TO automotive_runtime;
        GRANT SELECT ON ALL TABLES IN SCHEMA vpic TO automotive_runtime;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA vpic TO automotive_runtime;
    END IF;
END $$;
"""


# Migrations cannot reference schema vpic (this importer drops and rebuilds it), so the
# two lookups the application needs are copied into parts_catalog after every restore.
# make_norm mirrors parts_crawler.records.normalize_brand for ASCII names.
LOOKUPS = (
    ("wmi_cleared", "DELETE FROM parts_catalog.wmi WHERE source = 'vpic'"),
    ("wmi", """
        INSERT INTO parts_catalog.wmi
            (wmi, make, make_norm, manufacturer, country, vehicle_type, source)
        SELECT DISTINCT ON (upper(w.wmi), n.norm)
               upper(w.wmi), COALESCE(mk.name, ''), n.norm, COALESCE(m.name, ''),
               COALESCE(c.name, ''), COALESCE(vt.name, ''), 'vpic'
        FROM vpic.wmi w
        LEFT JOIN vpic.wmi_make wm ON wm.wmiid = w.id
        LEFT JOIN vpic.make mk ON mk.id = COALESCE(wm.makeid, w.makeid)
        LEFT JOIN vpic.manufacturer m ON m.id = w.manufacturerid
        LEFT JOIN vpic.country c ON c.id = w.countryid
        LEFT JOIN vpic.vehicletype vt ON vt.id = w.vehicletypeid
        CROSS JOIN LATERAL (SELECT regexp_replace(upper(COALESCE(mk.name, '')),
                                                  '[^A-Z0-9]+', '', 'g') AS norm) n
        WHERE upper(w.wmi) ~ '^[A-Z0-9]{3,6}$'
        ORDER BY upper(w.wmi), n.norm, w.id"""),
    ("source", "INSERT INTO parts_catalog.sources (source, daily_budget) VALUES ('vpic', 0) "
               "ON CONFLICT (source) DO NOTHING"),
    ("make_models", """
        INSERT INTO parts_catalog.vehicles AS v
            (source, source_vehicle_id, make, make_norm, model, market, label, raw)
        SELECT 'vpic', mm.makeid || '/' || mm.modelid, btrim(mk.name),
               regexp_replace(upper(mk.name), '[^A-Z0-9]+', '', 'g'), btrim(mo.name), 'US',
               btrim(mk.name) || ' ' || btrim(mo.name),
               jsonb_build_object('make_id', mm.makeid, 'model_id', mm.modelid)
        FROM vpic.make_model mm
        JOIN vpic.make mk ON mk.id = mm.makeid
        JOIN vpic.model mo ON mo.id = mm.modelid
        WHERE btrim(mk.name) <> ''
          -- Road vehicles only: vPIC also lists trailer, motorcycle and bus makers.
          AND EXISTS (SELECT 1 FROM vpic.wmi w
                      LEFT JOIN vpic.wmi_make wm ON wm.wmiid = w.id
                      WHERE COALESCE(wm.makeid, w.makeid) = mk.id
                        AND w.vehicletypeid IN (2, 3, 7))
        ON CONFLICT (source, source_vehicle_id) DO UPDATE SET
            make = EXCLUDED.make, make_norm = EXCLUDED.make_norm, model = EXCLUDED.model,
            label = EXCLUDED.label, raw = EXCLUDED.raw, observed_at = now()"""),
)


def copy_lookups(cursor: psycopg.Cursor) -> dict:
    """Copy WMI and make/model lookups from schema vpic; rerunnable, one transaction."""
    ready = cursor.execute(
        "SELECT to_regclass('parts_catalog.wmi') IS NOT NULL "
        "AND to_regclass('vpic.wmi') IS NOT NULL").fetchone()[0]
    if not ready:
        log.warning("vpic lookups skipped: apply migration 0011 and restore vPIC first")
        return {"lookups": "skipped (migration 0011 or schema vpic missing)"}
    counts = {}
    for name, statement in LOOKUPS:
        cursor.execute(statement)
        counts[name] = cursor.rowcount
    log.info("vpic lookups copied: %s WMI rows, %s make/model rows", counts["wmi"],
             counts["make_models"])
    return {"wmi": counts["wmi"], "make_models": counts["make_models"]}


def latest_plain_url(user_agent: str) -> str:
    page = requests.get(DOWNLOADS, timeout=30, headers={"User-Agent": user_agent}).text
    match = re.search(r'href="(/downloads/vPICList_lite_\d{4}_\d{2}\.plain\.zip)"', page)
    if not match:
        raise SystemExit("Could not find the vPIC plain dump link on the downloads page.")
    return "https://vpic.nhtsa.dot.gov" + match.group(1)


def restore_lines(cursor: psycopg.Cursor, lines: Iterable[str], advance=lambda n: None) -> int:
    """Execute a pg_dump plain script, including COPY ... FROM stdin data blocks."""
    statements = 0
    buffer: list[str] = []
    dollar: str | None = None
    iterator = iter(lines)
    for line in iterator:
        advance(len(line))
        if not buffer and dollar is None:
            stripped = line.strip()
            if not stripped or stripped.startswith("--") or stripped.startswith("\\") \
                    or SKIP.match(stripped):
                continue
        buffer.append(line)
        for tag in DOLLAR.findall(line):
            if dollar is None:
                dollar = tag
            elif tag == dollar:
                dollar = None
        if dollar is not None or not line.rstrip().endswith(";"):
            continue
        statement = "".join(buffer)
        buffer = []
        if COPY.match(statement):
            with cursor.copy(statement.strip().rstrip(";")) as copy:
                for data in iterator:
                    advance(len(data))
                    if data.rstrip("\r\n") == "\\.":
                        break
                    copy.write(data)
        else:
            cursor.execute(statement)
        statements += 1
    return statements


def import_vpic(settings: Settings, admin_url: str, archive: Path | None = None, *,
                lookups_only: bool = False) -> dict:
    """Restore schema vpic and copy its lookups, all in one transaction.

    A restore that is interrupted rolls back completely and leaves the previous vpic
    schema in place, so running the command again is always safe.
    """
    if lookups_only:
        with psycopg.connect(admin_url) as connection, connection.cursor() as cursor:
            return copy_lookups(cursor)
    if archive is None:
        url = latest_plain_url(settings.user_agent)
        archive = download(url, settings.data_dir / "downloads" / url.rsplit("/", 1)[1],
                           user_agent=settings.user_agent)
    with zipfile.ZipFile(archive) as bundle:
        member = next((info for info in bundle.infolist()
                       if info.filename.lower().endswith(".sql")), None)
        if member is None:
            raise SystemExit("The vPIC archive does not contain a .sql file.")
        log.info("vpic: restoring %s (%s bytes of SQL)", archive.name, member.file_size)
        with psycopg.connect(admin_url) as connection, connection.cursor() as cursor, \
                bundle.open(member) as raw, Progress(
                    "[progress.description]{task.description}", BarColumn(),
                    DownloadColumn(), TimeRemainingColumn(), console=console) as progress:
            task = progress.add_task("restore vPIC", total=member.file_size)
            cursor.execute("DROP SCHEMA IF EXISTS vpic CASCADE")
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            statements = restore_lines(cursor, text,
                                       lambda n: progress.advance(task, n))
            cursor.execute(GRANTS)
            tables = cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'vpic'"
            ).fetchone()[0]
            try:
                # A savepoint: a failed copy must not undo the restore that just finished.
                with connection.transaction():
                    lookups = copy_lookups(cursor)
            except psycopg.Error as error:
                log.error("vpic lookups were not copied: %s", type(error).__name__)
                lookups = {"lookups": f"failed ({type(error).__name__}); rerun with "
                                      "--lookups-only"}
    log.info("vpic: restored %s statements, %s tables", statements, tables)
    return {"statements": statements, "tables": tables, "archive": archive.name, **lookups}
