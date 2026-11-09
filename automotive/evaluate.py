"""Batch evaluation of the parts assistant on a list of VINs (read-only).

Runs the same pipeline a customer's message uses and reports, per VIN and category, how
long it took, which sources were slow, how many in-stock offers came out and which rule
rejected what. VINs are never printed (only ``VIN#n``) and never written; the optional
misses file holds make, model, year, trim, category and reason, so staff can add rules.
"""

from __future__ import annotations

import csv
import re
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

VIN = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')
DEFAULT_CATEGORIES = ('I need an oil change', 'front brake pads', 'air filter', 'cabin filter')


def read_vins(path: Path | str) -> tuple[list[str], int]:
    """VINs from the first column of a text/CSV file; returns (valid VINs, skipped lines)."""
    vins: list[str] = []
    skipped = 0
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        first = re.split(r'[,;\s]+', line)[0].strip().upper()
        if VIN.fullmatch(first):
            if first not in vins:
                vins.append(first)
        elif not re.fullmatch(r'(?i)vin', first):  # An optional header line is not an error.
            skipped += 1
    return vins, skipped


def classify(response: dict) -> str:
    """OFFERS, NONE_IN_STOCK, NO_VEHICLE or NO_CODES for one answered request."""
    vehicle = response.get('vehicle') or {}
    if not all(vehicle.get(k) for k in ('make', 'model', 'model_year')):
        return 'NO_VEHICLE'
    if response.get('cards'):
        return 'OFFERS'
    if (response.get('diagnostics') or {}).get('hidden_unavailable'):
        return 'NONE_IN_STOCK'
    return 'NO_CODES'


def row_for(index: int, category: str, seconds: float, response: dict) -> dict:
    cards = response.get('cards') or []
    states = Counter(card.get('availability_status') for card in cards)
    sources = sorted((s for s in response.get('progress') or [] if s.get('elapsed_ms')),
                     key=lambda s: -s['elapsed_ms'])
    vehicle = response.get('vehicle') or {}
    return {
        'vin': f'VIN#{index}', 'category': category, 'seconds': round(seconds, 1),
        'make': vehicle.get('make') or '', 'model': vehicle.get('model') or '',
        'year': vehicle.get('model_year') or '', 'trim': vehicle.get('trim') or '',
        'result': classify(response), 'offers': len(cards),
        'in_store': states.get('IN_STORE', 0), 'tuz11': states.get('WITHIN_1_2_HOURS', 0),
        'rejections': dict((response.get('diagnostics') or {}).get('rejections') or {}),
        'failed': [w.split(':', 1)[1] for w in response.get('warnings', [])
                   if w.startswith('SOURCE_FAILED:')],
        'slowest': [(s['source_id'], s['elapsed_ms']) for s in sources[:2]],
    }


def summarize(rows: list[dict]) -> dict:
    """Totals per category: result shares, time and the rules that rejected items."""
    result: dict[str, dict] = {}
    for category in dict.fromkeys(r['category'] for r in rows):
        group = [r for r in rows if r['category'] == category]
        counts = Counter(r['result'] for r in group)
        times = sorted(r['seconds'] for r in group)
        rejections: Counter = Counter()
        for row in group:
            rejections.update(row['rejections'])
        result[category] = {
            'requests': len(group), 'results': dict(counts),
            'offers_share': counts.get('OFFERS', 0) / len(group),
            'median_seconds': statistics.median(times),
            'slowest_seconds': times[-1], 'rejections': dict(rejections),
            'failed_sources': dict(Counter(s for r in group for s in r['failed'])),
        }
    return result


def anonymous_misses(rows: list[dict]) -> list[dict]:
    """Requests that produced no offer, without any VIN, for staff to review."""
    reasons = {'NO_VEHICLE': 'vehicle_not_identified', 'NO_CODES': 'no_codes_found',
               'NONE_IN_STOCK': 'none_in_stock'}
    return [{'make': r['make'], 'model': r['model'], 'year': r['year'], 'trim': r['trim'],
             'category': r['category'], 'reason': reasons[r['result']]}
            for r in rows if r['result'] != 'OFFERS']


def evaluate(vins: list[str], categories: list[str] | tuple[str, ...], chat: Any, search: Any,
             out: TextIO) -> list[dict]:
    """Run every VIN through every category and print a table; read-only."""
    rows: list[dict] = []
    out.write(f"{'VIN':7} {'car':34} {'category':22} {'sec':>5} {'store':>5} {'tuz11':>5} "
              f"{'result':14} rejected / slow\n")
    for index, vin in enumerate(vins, 1):
        if hasattr(search, 'prewarm'):
            search.prewarm(vin)
        for category in categories:
            started = time.monotonic()
            response = chat.respond_conversation(
                f'{category} {vin}', vin=vin, category=category, language='en',
                search_service=search)
            row = row_for(index, category, time.monotonic() - started, response)
            rows.append(row)
            car = ' '.join(str(row[k]) for k in ('make', 'model', 'year', 'trim') if row[k])
            note = ', '.join(f'{k}:{v}' for k, v in row['rejections'].items())
            slow = ', '.join(f'{name} {ms // 1000}s' for name, ms in row['slowest']
                             if ms >= 3000)
            out.write(f"{row['vin']:7} {car[:34]:34} {category[:22]:22} {row['seconds']:5.1f} "
                      f"{row['in_store']:5d} {row['tuz11']:5d} {row['result']:14} "
                      f"{note}{' | ' if note and slow else ''}{slow}"
                      f"{' | failed: ' + ','.join(row['failed']) if row['failed'] else ''}\n")
            out.flush()
    out.write('\nTotals\n')
    for category, data in summarize(rows).items():
        shares = ', '.join(f'{k} {v}' for k, v in sorted(data['results'].items()))
        out.write(f"- {category}: {data['requests']} requests, offers "
                  f"{data['offers_share']:.0%} ({shares}); median {data['median_seconds']}s, "
                  f"slowest {data['slowest_seconds']}s; rejections {data['rejections'] or '-'}; "
                  f"failed sources {data['failed_sources'] or '-'}\n")
    return rows


def write_misses(rows: list[dict], path: Path | str) -> int:
    misses = anonymous_misses(rows)
    with Path(path).open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'make', 'model', 'year', 'trim', 'category', 'reason'])
        writer.writeheader()
        writer.writerows(misses)
    return len(misses)
