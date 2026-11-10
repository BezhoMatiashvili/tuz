"""EPA fuel economy vehicles (fueleconomy.gov): one row per model-year configuration.

Public US government data, model years 1984 onward. Each record states the engine size,
cylinders, gearbox, driven axle and fuel of one configuration, which the plain
year/make/model list lacks. US market only.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from pathlib import Path

from parts_crawler.config import Settings
from parts_crawler.importers import download, file_sha, require_schema
from parts_crawler.records import ParseResult, Vehicle
from parts_crawler.store import Store
from parts_crawler.term import Tracker

URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
SOURCE = "fueleconomy"
REQUIRED = ("id", "make", "model", "year", "displ", "trany", "drive", "fuelType1", "VClass")
RAW_KEYS = ("cylinders", "eng_dscr", "fuelType", "atvType", "tCharger", "sCharger",
            "baseModel", "trans_dscr", "evMotor")
log = logging.getLogger("parts_crawler.importers.fueleconomy")


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "NA") else None
    except ValueError:
        return None


def vehicle(row: dict) -> Vehicle | None:
    """One EPA record as a vehicle row, or None when it has no id or make."""
    ident = (row.get("id") or "").strip()
    make = (row.get("make") or "").strip()
    model = (row.get("model") or "").strip()
    if not ident or not make:
        return None
    year = _number(row.get("year"))
    year = int(year) if year else None
    litres = _number(row.get("displ"))
    cylinders = _number(row.get("cylinders"))
    engine = " ".join(part for part in (
        f"{litres:g}L" if litres else "",
        f"{int(cylinders)} cyl" if cylinders else "",
        (row.get("eng_dscr") or "").strip()) if part)
    gearbox = (row.get("trany") or "").strip()
    return Vehicle(
        key=f"fe:{ident}", make=make, model=model, year_from=year, year_to=year,
        engine=engine, body=(row.get("VClass") or "").strip(), market="US",
        label=" ".join(part for part in (str(year or ""), make, model, engine, gearbox)
                       if part),
        displacement_cc=round(litres * 1000) if litres else None,
        fuel=(row.get("fuelType1") or row.get("fuelType") or "").strip(),
        transmission=gearbox, drive=(row.get("drive") or "").strip(),
        raw={key: row[key].strip() for key in RAW_KEYS if (row.get(key) or "").strip()})


def _project(reader: csv.DictReader) -> list[dict]:
    """Keep only the columns used; the file has about 80 and tens of thousands of rows."""
    missing = [name for name in REQUIRED if name not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit("The EPA file no longer has the expected columns: "
                         + ", ".join(missing) + ". Nothing was imported.")
    wanted = set(REQUIRED) | set(RAW_KEYS) | {"fuelType"}
    return [{key: value for key, value in row.items() if key in wanted} for row in reader]


def read_rows(path: Path) -> list[dict]:
    """Rows of vehicles.csv, from the zip as published or from an extracted CSV."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as bundle:
            member = next((info for info in bundle.infolist()
                           if info.filename.lower().endswith(".csv")), None)
            if member is None:
                raise SystemExit(f"{path.name} does not contain a .csv file.")
            with bundle.open(member) as raw:
                return _project(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                                                newline="")))
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return _project(csv.DictReader(handle))


def import_fueleconomy(settings: Settings, path: Path | None = None, *, batch: int = 2000,
                       restart: bool = False, refresh: bool = False) -> dict:
    path = path or download(URL, settings.data_dir / "downloads" / "fueleconomy-vehicles.zip",
                            user_agent=settings.user_agent, refresh=refresh)
    digest = file_sha(path)
    rows = read_rows(path)
    store = Store(settings.database_url)
    totals = {"rows": len(rows), "resumed_at": 0, "vehicles_added": 0, "skipped": 0}
    try:
        require_schema(store)
        store.ensure_source(SOURCE, 0)
        state = {} if restart else store.checkpoint(SOURCE)
        # A checkpoint only counts for the same file; a newer download starts again.
        done = min(int(state.get("rows", 0)), len(rows)) if state.get("sha") == digest else 0
        totals["resumed_at"] = done
        if done >= len(rows) > 0:
            log.info("fueleconomy: this file is already loaded (%s rows)", len(rows))
            return totals | {"status": "already loaded; use --restart to load it again"}
        with Tracker("fueleconomy.gov vehicles", len(rows), done=done, logger=log) as tracker:
            for start in range(done, len(rows), batch):
                chunk = rows[start:start + batch]
                vehicles = [item for item in map(vehicle, chunk) if item is not None]
                totals["skipped"] += len(chunk) - len(vehicles)
                with store.conn.transaction():
                    counts = store.write_result(SOURCE, ParseResult(vehicles=vehicles), URL)
                    store.set_checkpoint(SOURCE, {"sha": digest, "rows": start + len(chunk),
                                                  "total": len(rows)})
                totals["vehicles_added"] += counts["vehicles"]
                tracker.advance(len(chunk))
    finally:
        store.close()
    return totals | {"status": "complete"}
