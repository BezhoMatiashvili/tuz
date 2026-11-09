"""Parser for the HTML fragment returned by GetProducts.php."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser

from warehouse_sync.errors import SourceProtocolError
from warehouse_sync.models import ParsedPage, WarehouseProduct

PAGE_NUMBER_PATTERN = re.compile(r"LoadMoreData\((\d+)\)")


class _ProductRowsParser(HTMLParser):
    """Extract product ``tr`` elements without needing a browser-only HTML library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, list[str]]] = []
        self._row_id: str | None = None
        self._cells: list[list[str]] = []
        self._current_cell: list[str] | None = None
        self.invalid_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "tr" and "product_names" in classes:
            if not attributes.get("id"):
                self.invalid_row = True
                return
            if self._row_id is not None:
                self.invalid_row = True
            self._row_id = str(attributes["id"])
            self._cells = []
            self._current_cell = None
            return
        if self._row_id is None:
            return
        if tag == "td" and self._current_cell is None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._row_id is None:
            return
        if tag == "td" and self._current_cell is not None:
            self._cells.append(self._current_cell)
            self._current_cell = None
            return
        if tag == "tr":
            if self._current_cell is not None or not self._cells:
                self.invalid_row = True
            values = [" ".join("".join(cell).split()) for cell in self._cells]
            # Only extracted text crosses the parser boundary; never a source DOM fragment.
            self.rows.append((self._row_id, values))
            self._row_id = None
            self._cells = []
            self._current_cell = None

    def handle_data(self, data: str) -> None:
        if self._row_id is None:
            return
        if self._current_cell is not None:
            self._current_cell.append(data)


def _value_at(values: list[str], index: int) -> str:
    return values[index] if len(values) > index else ""


def _row_hash(source_product_id: str, columns: dict[str, str]) -> str:
    canonical = json.dumps(
        {"source_product_id": source_product_id, "columns": columns},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_products_page(html: str) -> ParsedPage:
    """Parse every product row and preserve every source column by ordinal position.

    The source does not return table headings with this HTML fragment. Known fields are
    exposed individually; all remaining fields stay losslessly available in
    ``source_columns`` rather than being guessed or discarded.
    """
    parser = _ProductRowsParser()
    parser.feed(html)
    parser.close()
    if parser.invalid_row or parser._row_id is not None:
        raise SourceProtocolError("A source product row is malformed or incomplete.")
    products: list[WarehouseProduct] = []
    seen_ids: set[str] = set()

    for raw_id, values in parser.rows:
        source_product_id = raw_id.strip()
        if not source_product_id:
            continue
        if source_product_id in seen_ids:
            raise SourceProtocolError("Duplicate product ID in one source response.")
        seen_ids.add(source_product_id)

        if not values:
            continue
        source_columns = {f"column_{number:02d}": value for number, value in enumerate(values, 1)}
        products.append(
            WarehouseProduct(
                source_product_id=source_product_id,
                product_code=_value_at(values, 0),
                product_name=_value_at(values, 1),
                product_model=_value_at(values, 2),
                original_code=_value_at(values, 3),
                duplicate_code=_value_at(values, 4),
                brand=_value_at(values, 7),
                source_columns=source_columns,
                row_hash=_row_hash(source_product_id, source_columns),
            )
        )

    page_numbers = [int(match) for match in PAGE_NUMBER_PATTERN.findall(html)]
    total_pages = max(page_numbers, default=1)
    return ParsedPage(products=products, total_pages=total_pages)
