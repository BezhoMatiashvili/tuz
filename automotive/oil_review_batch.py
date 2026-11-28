"""Prepare the staff oil-research exports for the existing pending evidence importer."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from automotive.importer import parse_import
from automotive.oil_match import normalize_grade

SCOPE = ("make", "model", "model_year", "engine_code", "market", "trim")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _values(value: str, separator: str) -> list[str]:
    return sorted({part.strip() for part in value.split(separator) if part.strip()})


def _record(kind: str, name: str, urls: list[str], scope: dict, payload: dict) -> dict:
    return {
        "kind": kind, "source_name": name, "source_url": urls[0], "authority": "manufacturer",
        "applicability": scope, "restrictions": {},
        "payload": {**payload, "supporting_source_urls": urls},
    }


def prepare(vehicles: Path, products: Path) -> tuple[list[dict], dict[str, int]]:
    """Only prepare unconditioned specs and explicit formal approvals with a package hash."""
    grouped = defaultdict(list)
    for row in _rows(vehicles):
        grouped[tuple(row[key].strip() for key in SCOPE)].append(row)
    records = []
    vehicle_skipped = 0
    for key, rows in sorted(grouped.items()):
        grades = {tuple(_values(row["sae_grades"], ";")) for row in rows}
        urls = sorted({url for row in rows for url in _values(row["source_urls"], "|")})
        options = [[value] for value in sorted({row["approval"].strip() for row in rows})]
        group = dict(zip(SCOPE, key, strict=True))
        if (any(row["conditions"].strip() for row in rows)
                or not all(key[:5]) or not re.fullmatch(r"[0-9]{4}", key[2]) or not urls
                or len(grades) != 1 or not all(option[0] for option in options)):
            vehicle_skipped += 1
            continue
        viscosity = next(iter(grades))
        if not viscosity or any(normalize_grade(grade) != grade for grade in viscosity):
            vehicle_skipped += 1
            continue
        scope = {field: int(value) if field == "model_year" else value
                 for field, value in group.items() if value}
        records.append(_record("service_specification", f"{group['make']} oil documentation",
                               urls, scope, {"category": "engine_oil",
                                             "approval_options": options,
                                             "viscosities": list(viscosity)}))
    vehicle_ready = len(records)
    product_skipped = 0
    for row in _rows(products):
        urls = _values(row["source_urls"], "|")
        approvals = _values(row["approvals"], ";")
        grade = row["sae_grade"]
        if (row["status"] != "APPROVAL_DOCUMENTED" or not row["product_id"] or not urls
                or not approvals or not grade or normalize_grade(grade) != grade
                or not re.fullmatch(r"[0-9a-f]{64}", row["catalog_fingerprint"])):
            product_skipped += 1
            continue
        for approval in approvals:
            records.append(_record("product_approval", row["formulation"], urls, {}, {
                "product_id": row["product_id"], "approval": approval, "claim_type": "approval",
                "viscosity": grade, "approval_content_fingerprint": row["catalog_fingerprint"],
            }))
    return records, {"vehicle_records": vehicle_ready, "vehicle_scopes_skipped": vehicle_skipped,
                     "product_records": len(records) - vehicle_ready,
                     "product_rows_skipped": product_skipped}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vehicles", type=Path)
    parser.add_argument("products", type=Path)
    parser.add_argument("--output", type=Path, help="Write a JSON import array to a new file.")
    parser.add_argument("--authorized-host", action="append", default=[],
                        help="Source host you have confirmed permission to reuse.")
    args = parser.parse_args(argv)
    records, counts = prepare(args.vehicles, args.products)
    if args.output:
        authorized = {host.casefold() for host in args.authorized_host}
        records = [{**row, "reuse_permission": "authorized"} for row in records
                   if all(urlsplit(url).hostname in authorized
                          for url in row["payload"]["supporting_source_urls"])]
        if not records:
            parser.error("No records use only the source hosts authorized for this export.")
        content = json.dumps(records, ensure_ascii=False, indent=2)
        parse_import(content, "json")
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content + "\n")
        counts["exported_for_pending_review"] = len(records)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
