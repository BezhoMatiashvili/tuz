"""Year/make/model list from rokasmen/car-audio-fitment-api (data/vehicles.csv)."""

from __future__ import annotations

import csv
from pathlib import Path

from rich.progress import Progress

from parts_crawler.config import Settings
from parts_crawler.importers import download
from parts_crawler.records import ParseResult, Vehicle
from parts_crawler.store import Store

URL = "https://raw.githubusercontent.com/rokasmen/car-audio-fitment-api/main/data/vehicles.csv"
SOURCE = "ymm_csv"


def import_ymm(settings: Settings, path: Path | None = None, batch: int = 2000) -> dict:
    path = path or download(URL, settings.data_dir / "downloads" / "vehicles.csv",
                            user_agent=settings.user_agent)
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    store = Store(settings.database_url)
    totals = {"rows": 0, "vehicles": 0}
    try:
        store.ensure_source(SOURCE, 0)
        with Progress() as progress:
            task = progress.add_task("year/make/model", total=len(rows))
            for start in range(0, len(rows), batch):
                result = ParseResult()
                for row in rows[start:start + batch]:
                    year = int(row["year"]) if row.get("year", "").isdigit() else None
                    key = f"{row.get('make_slug')}/{row.get('model_slug')}/{row.get('year')}"
                    result.vehicles.append(Vehicle(
                        key=key, make=row.get("make", ""), model=row.get("model", ""),
                        year_from=year, year_to=year,
                        label=f"{row.get('year')} {row.get('make')} {row.get('model')}",
                        raw={"make_slug": row.get("make_slug"),
                             "model_slug": row.get("model_slug")}))
                with store.conn.transaction():
                    counts = store.write_result(SOURCE, result, URL)
                totals["rows"] += len(result.vehicles)
                totals["vehicles"] += counts["vehicles"]
                progress.advance(task, len(result.vehicles))
    finally:
        store.close()
    return totals
