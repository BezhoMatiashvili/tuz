"""Load the FAPI cross-reference text format (manufacturer_list, product_list, analog_list).

Works for the public sample in fapi-dev/auto-parts-cross-reference (evaluation-only data)
and for the purchased full archive, which uses the same three files.
"""

from __future__ import annotations

from pathlib import Path

from rich.progress import Progress

from parts_crawler.config import Settings
from parts_crawler.importers import download
from parts_crawler.records import CrossRef, ParseResult, Part, PartRef
from parts_crawler.store import Store

SAMPLE = "https://raw.githubusercontent.com/fapi-dev/auto-parts-cross-reference/main/"
FILES = ("manufacturer_list.txt", "product_list.txt", "analog_list.txt")
SOURCE = "fapi"
KINDS = {"10": "oe", "50": "aftermarket"}


def fetch_sample(settings: Settings) -> Path:
    directory = settings.data_dir / "downloads" / "fapi-sample"
    for name in FILES:
        download(SAMPLE + name, directory / name, user_agent=settings.user_agent)
    return directory


def import_fapi(settings: Settings, directory: Path | None = None, batch: int = 2000) -> dict:
    directory = directory or fetch_sample(settings)
    brands: dict[str, str] = {}
    with (directory / "manufacturer_list.txt").open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\r\n").split(";")
            if len(fields) >= 2:
                brands[fields[0]] = fields[1]
    products: dict[str, Part] = {}
    with (directory / "product_list.txt").open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < 6:
                continue
            product_id, brand_id, normalized, number, name, kind = fields[:6]
            products[product_id] = Part(brands.get(brand_id, ""), number or normalized,
                                        name=name, kind=KINDS.get(kind, "unknown"))
    with (directory / "analog_list.txt").open(encoding="utf-8") as handle:
        analogs = [line.rstrip("\r\n").split(";") for line in handle if line.strip()]
    store = Store(settings.database_url)
    totals = {"products": len(products), "analogs": len(analogs), "parts": 0, "xrefs": 0}
    try:
        store.ensure_source(SOURCE, 0)
        with Progress() as progress:
            part_task = progress.add_task("FAPI parts", total=len(products))
            items = list(products.values())
            for start in range(0, len(items), batch):
                chunk = items[start:start + batch]
                with store.conn.transaction():
                    counts = store.write_result(SOURCE, ParseResult(parts=chunk), SAMPLE)
                totals["parts"] += counts["parts"]
                progress.advance(part_task, len(chunk))
            cross_task = progress.add_task("FAPI cross references", total=len(analogs))
            for start in range(0, len(analogs), batch):
                result = ParseResult()
                for fields in analogs[start:start + batch]:
                    if len(fields) < 5:
                        continue
                    left, right = products.get(fields[3]), products.get(fields[4])
                    if left and right:
                        result.cross_refs.append(CrossRef(PartRef(left.brand, left.number),
                                                          PartRef(right.brand, right.number),
                                                          "equivalent"))
                with store.conn.transaction():
                    counts = store.write_result(SOURCE, result, SAMPLE)
                totals["xrefs"] += counts["xrefs"]
                progress.advance(cross_task, min(batch, len(analogs) - start))
    finally:
        store.close()
    return totals
