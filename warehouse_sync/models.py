"""Typed records used by the source parser and PostgreSQL repository."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WarehouseProduct:
    """One product row exactly as observed at the source."""

    source_product_id: str
    product_code: str
    product_name: str
    product_model: str
    original_code: str
    duplicate_code: str
    brand: str
    source_columns: dict[str, str]
    row_hash: str


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """Products and the maximum page number advertised by an API response."""

    products: list[WarehouseProduct]
    total_pages: int


@dataclass(slots=True)
class SyncCounters:
    """Counters accumulated during one synchronisation run."""

    pages_fetched: int = 0
    rows_seen: int = 0
    rows_added: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0
    rows_deactivated: int = 0

    def add(self, other: "SyncCounters") -> None:
        self.pages_fetched += other.pages_fetched
        self.rows_seen += other.rows_seen
        self.rows_added += other.rows_added
        self.rows_changed += other.rows_changed
        self.rows_unchanged += other.rows_unchanged
        self.rows_deactivated += other.rows_deactivated
