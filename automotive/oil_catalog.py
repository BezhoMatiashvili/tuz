"""Export oil catalog entries for product and approval review."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from automotive.evidence import verified_record
from automotive.oil_match import pack_litres
from automotive.recommendations import current_product_approval, positive_approval
from automotive.repository import AutomotiveRepository

CATALOG_SPECS = re.compile(
    r"\b(?:MB[- ]?(?:APPROVAL\s*)?\s*)?229[. ]\d{1,2}\b|"
    r"\b(?:BMW\s*)?LL[- ]?\d{2}(?:\s*FE\+?)?\b|"
    r"\b(?:VW\s*)?(?:50[245789]|511)[. ][0-9]{2}\b|\b50[48]\s*/\s*50[79]\b|"
    r"\b(?:API\s*|ACEA\s*|ILSAC\s*)?(?:S[LMNPQ](?:\s*PLUS)?|C[F-K](?:-4)?"
    r"|C[1-7]|A[135]/B[1345]|GF[- ]?[4567][AB]?)\b|"
    r"\bDEXOS\s*[12DR](?:\s*GEN\s*\d+)?\b|\b(?:WSS[- ]?[A-Z0-9-]+|RN\d{2,4})\b",
    re.IGNORECASE,
)

FIELDS = (
    "review_status", "product_id", "product_code", "original_code", "duplicate_code",
    "brand", "product_name", "product_model", "sae_grade_from_catalog", "pack_litres",
    "specs_stated_in_catalog", "verified_approvals", "reviewed_sources", "stock_store",
    "stock_tuz11", "approval_content_fingerprint", "last_observed_at",
)


def review_row(product: dict, approvals: list[dict]) -> dict:
    name = product.get("product_name") or ""
    verified = [row for row in approvals if verified_record(row) and positive_approval(row)
                and current_product_approval(row, product)]
    status = product["catalog_review_status"]
    if verified and status not in ("OTHER_PRODUCT", "SPECIAL_APPLICATION_REVIEW"):
        status = "APPROVAL_EVIDENCE_REVIEWED"
    names = {match.group().upper() for match in CATALOG_SPECS.finditer(name)}
    stock = product.get("stock") or {}
    return {
        **{key: product.get(key) for key in FIELDS if key in product},
        "product_id": product["source_product_id"],
        "review_status": status,
        "sae_grade_from_catalog": "; ".join(product["sae_grades"]),
        "pack_litres": pack_litres(name),
        "specs_stated_in_catalog": "; ".join(sorted(names)),
        "verified_approvals": "; ".join(sorted({row["approval"] for row in verified})),
        "reviewed_sources": "; ".join(sorted({row["source_url"] for row in verified
                                             if row.get("source_url")})),
        "stock_store": stock.get("store"),
        "stock_tuz11": stock.get("tuz11"),
    }


def spreadsheet_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def read_review(repository: AutomotiveRepository) -> list[tuple[dict, dict]]:
    products = repository.oil_review_products()
    approvals = {product["source_product_id"]: rows
                 for product, rows in repository.oil_approval_products()}
    return [(product, review_row(product, approvals.get(product["source_product_id"], [])))
            for product in products]


def export_review(repository: AutomotiveRepository, output: Path) -> int:
    rows = read_review(repository)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for _, row in sorted(rows, key=lambda entry: (
            not entry[1]["stock_store"], not entry[1]["stock_tuz11"],
        )):
            writer.writerow({key: spreadsheet_cell(value)
                             for key, value in row.items()})
    return len(rows)


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New CSV output file")
    args = parser.parse_args()
    config = {**dotenv_values(".env"), **os.environ}
    url = config.get("AUTOMOTIVE_DATABASE_URL") or config.get("DATABASE_URL")
    if not url:
        parser.error("Configure AUTOMOTIVE_DATABASE_URL or DATABASE_URL.")
    try:
        count = export_review(AutomotiveRepository(url, statement_timeout_ms=12000), args.output)
    except FileExistsError:
        parser.error("The output file already exists. Choose a new path.")
    print(f"Exported {count} catalog rows for oil review to {args.output}")


if __name__ == "__main__":
    main()
