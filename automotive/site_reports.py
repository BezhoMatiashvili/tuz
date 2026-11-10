"""Dashboard figures: inventory from the in-memory catalog, sync health and inferred trade
from PostgreSQL, chat volume from the local chat logs. Read-only throughout.

Money figures (stock value, sales amounts) are produced only for admins by the callers.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from automotive.categories import PATTERNS
from automotive.site_catalog import MAKES, Catalog
from automotive.stock import IN_STORE, UNAVAILABLE, WITHIN_HOURS

PRICE_BINS = (25, 50, 100, 200, 400, 800)
CATEGORY_LABELS = {
    "transmission_oil": ("კოლოფის ზეთი", "Transmission oil"),
    "axle_oil": ("ხიდის ზეთი", "Axle oil"),
    "steering_fluid": ("საჭის სითხე", "Steering fluid"), "clutch": ("გადაბმულობა", "Clutch"),
    "brake_pads": ("სამუხრუჭე ხუნდები", "Brake pads"),
    "brake_discs": ("სამუხრუჭე დისკები", "Brake discs"), "brakes": ("მუხრუჭები", "Brakes"),
    "oil_filter": ("ზეთის ფილტრი", "Oil filter"), "air_filter": ("ჰაერის ფილტრი", "Air filter"),
    "cabin_filter": ("სალონის ფილტრი", "Cabin filter"),
    "fuel_filter": ("საწვავის ფილტრი", "Fuel filter"), "spark_plugs": ("სანთლები", "Spark plugs"),
    "battery": ("აკუმულატორი", "Battery"), "wipers": ("მინის საწმენდები", "Wipers"),
    "timing_belt": ("გრმ", "Timing"), "coolant": ("ანტიფრიზი", "Coolant"),
    "bulbs": ("ნათურები", "Bulbs"), "engine_oil": ("ძრავის ზეთი", "Engine oil"),
    "suspension": ("საკიდარი", "Suspension"),
}
# The bundle "oil change" is a customer phrase, not a kind of item.
_CATEGORIES = [(key, re.compile(pattern, re.IGNORECASE))
               for key, pattern in PATTERNS.items() if key != "oil_change"]


def _category(fold: str) -> str | None:
    for key, pattern in _CATEGORIES:
        if pattern.search(fold):
            return key
    return None


def _ranked(counter: Counter, limit: int) -> list[dict]:
    """Top entries plus one "other" row, so a chart never hides the remainder."""
    rows = [{"key": key, "count": count} for key, count in counter.most_common(limit)]
    rest = sum(counter.values()) - sum(row["count"] for row in rows)
    if rest > 0:
        rows.append({"key": "__other__", "count": rest})
    return rows


def inventory_overview(catalog: Catalog) -> dict:
    """Counts for the dashboard, computed once per catalog snapshot."""
    cached = getattr(catalog, "_overview", None)
    if cached is not None:
        return cached
    states = Counter(item.state for item in catalog.items)
    brands: Counter = Counter()
    makes: Counter = Counter()
    categories: Counter = Counter()
    bins = [0] * (len(PRICE_BINS) + 1)
    store_units = warehouse_units = value = 0.0
    unpriced = uncategorised = 0
    for item in catalog.available:
        store, warehouse = max(item.store or 0.0, 0.0), max(item.tuz11 or 0.0, 0.0)
        store_units += store
        warehouse_units += warehouse
        brands[item.brand or "?"] += 1
        for make in item.makes:
            makes[make] += 1
        category = _category(item.fold)
        if category:
            categories[category] += 1
        else:
            uncategorised += 1
        if item.price:
            value += (store + warehouse) * item.price
            bins[sum(1 for edge in PRICE_BINS if item.price >= edge)] += 1
        else:
            unpriced += 1
    edges = (0, *PRICE_BINS)
    overview = {
        "tiles": {"items": len(catalog.items), "available": len(catalog.available),
                  "in_store": states[IN_STORE], "warehouse_only": states[WITHIN_HOURS],
                  "out_of_stock": states[UNAVAILABLE], "store_units": round(store_units, 1),
                  "warehouse_units": round(warehouse_units, 1), "unpriced": unpriced},
        "stock_value": round(value, 2),
        "brands": _ranked(brands, 10),
        "makes": [{**row, "name": MAKES.get(row["key"], "")} for row in _ranked(makes, 12)],
        "categories": [{**row, "label": dict(zip(("ka", "en"), CATEGORY_LABELS.get(
            row["key"], ("", "")), strict=True))} for row in _ranked(categories, 12)],
        "uncategorised": uncategorised,
        "prices": [{"from": edges[index], "to": PRICE_BINS[index] if index < len(PRICE_BINS)
                    else None, "count": count} for index, count in enumerate(bins)],
        "observed_at": catalog.observed_at, "freshness": catalog.freshness(),
    }
    catalog._overview = overview
    return overview


def sync_health(repository: Any) -> dict:
    """Recent sync runs, or ``available: False`` when the site login cannot read them."""
    try:
        runs = repository.sync_runs()
    except Exception:  # Unconfigured or unreachable: the tile says so instead of failing.
        return {"available": False, "runs": []}
    finished = next((run for run in runs if run.get("finished_at")), None)
    return {"available": True, "runs": runs,
            "last_finished_at": finished["finished_at"] if finished else None,
            "last_status": finished["status"] if finished else None}


def trade_report(repository: Any, catalog: Catalog, days: int = 30) -> dict:
    """Units sold and received per day, inferred from stock changes between syncs."""
    try:
        daily, sellers = repository.trade_daily(days), repository.best_sellers(days)
    except Exception:
        daily = sellers = None
    if daily is None or sellers is None:
        return {"available": False}
    best = []
    for row in sellers:
        item = catalog.by_id.get(row["source_product_id"])
        best.append({"name": item.name if item else "", "brand": item.brand if item else "",
                     "fits": item.model if item else "", "units": row["units"],
                     "amount": row["amount"]})
    return {"available": True, "days": days, "since": daily["since"],
            "sales": daily["sales"], "purchases": daily["purchases"], "best_sellers": best}


def _turns(path: str, days: int) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    try:
        with closing(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True,
                                     timeout=2)) as conn:
            window = (f"-{int(days)} days",)
            # Days are Tbilisi days (UTC+4, no daylight saving).
            daily = conn.execute(
                "SELECT date(started_at, 'unixepoch', '+4 hours') AS day, count(*) "
                "FROM turns WHERE started_at >= strftime('%s', 'now', ?) GROUP BY 1 ORDER BY 1",
                window).fetchall()
            categories = conn.execute(
                "SELECT category, count(*) FROM turns WHERE category <> '' "
                "AND started_at >= strftime('%s', 'now', ?) GROUP BY 1 ORDER BY 2 DESC LIMIT 60",
                window).fetchall()
            states = conn.execute(
                "SELECT status, count(*) FROM turns "
                "WHERE started_at >= strftime('%s', 'now', ?) GROUP BY 1", window).fetchall()
    except sqlite3.Error:
        return None
    topics: Counter = Counter()
    for text, count in categories:
        topics[_category(str(text).casefold()) or "__other__"] += count
    return {"daily": [{"day": day, "turns": count} for day, count in daily],
            "topics": [{"key": key, "count": count,
                        "label": dict(zip(("ka", "en"), CATEGORY_LABELS.get(key, ("", "")),
                                          strict=True))}
                       for key, count in topics.most_common(8)],
            "statuses": dict(states)}


def chat_report(settings: Any, days: int = 30) -> dict:
    """Turn counts only. Questions and answers stay in the log files and are never served."""
    return {"days": days,
            "staff": _turns(getattr(settings, "chat_log_path", ""), days),
            "site": _turns(getattr(settings, "site_chat_log_path", ""), days)}
