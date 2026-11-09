"""Current warehouse products for a vehicle, without inferring interchangeability."""

from __future__ import annotations

from collections import defaultdict
from time import monotonic
from typing import Any

from automotive.categories import matches_category
from automotive.codes import normalize_code, product_codes
from automotive.evidence import (
    applies_to,
    evaluate_compatibility,
    flatten_record,
)

CATALOG_WARNING = (
    'კატალოგში მწარმოებლისა და მოდელის ხსენება თავსებადობას არ ადასტურებს. '
    'ფასი და რაოდენობა უცნობია, სანამ მათი წყარო არ დადასტურდება.'
)
MISSING_EVIDENCE = (
    'დადასტურებული შესაბამისობის სიისთვის საჭიროა ავტორიზებული fitment ჩანაწერები. '
    'შეგიძლიათ მოძებნოთ ნაწილის ზუსტი კოდიც.'
)


def _vehicle_scoped(row: dict) -> bool:
    return all(any(key in row.get(field, {}) for field in ('applicability', 'restrictions'))
               for key in ('make', 'model'))


def _year_scoped(row: dict) -> bool:
    constraints = {**row.get('applicability', {}), **row.get('restrictions', {})}
    return ('model_year' in constraints or
            all(key in constraints for key in ('model_year_from', 'model_year_to')))


class VehicleInventoryService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def fitment_entries(self, vehicle: dict) -> dict:
        records = [flatten_record(row) for row in self.repository.vehicle_fitment_records()]
        relevant = [row for row in records if _vehicle_scoped(row) and applies_to(row, vehicle)
                    and row.get('review_status') != 'rejected'
                    and not row.get('superseded_by') and not row.get('revoked', False)]
        entries: dict[str, dict] = {}
        if relevant:
            self._evidence_products(vehicle, relevant, entries)
        return entries

    def find(self, vehicle: dict, limit: int = 30, offset: int = 0,
             part_query: str = '') -> dict:
        entries = self.fitment_entries(vehicle) if all(vehicle.get(k) for k in (
            'make', 'model', 'model_year',
        )) else {}
        return self._page(vehicle, entries, limit, offset, part_query)

    def _page(self, vehicle: dict, entries: dict, limit: int, offset: int,
              part_query: str = '') -> dict:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        result = {
            'status': 'INSUFFICIENT_DATA', 'items': [], 'verified_count': 0,
            'candidate_count': 0, 'excluded_count': 0, 'has_more': False,
            'warnings': [CATALOG_WARNING], 'clarification': None,
            'offset': offset, 'next_offset': None,
        }
        if not all(vehicle.get(key) for key in ('make', 'model', 'model_year')):
            result['clarification'] = 'ჯერ მიუთითეთ ავტომობილის მწარმოებელი, მოდელი და წელი.'
            return result
        excluded = {key for key, item in entries.items() if item['compatible'] is False}
        result['excluded_count'] = len(excluded)
        result['excluded_items'] = [entries[key] for key in sorted(excluded)]
        items = [item for key, item in entries.items() if key not in excluded
                 and matches_category(item['product'], part_query)]
        ranks = {'VERIFIED': 0, 'CONFLICTED': 1, 'PROVISIONAL': 2, 'UNKNOWN': 3}
        items.sort(key=lambda item: (ranks[item['status']], item['product']['source_product_id']))
        evidence_total = len(items)
        items = items[offset:offset + limit]
        text_limit = max(1, limit - len(items))
        text_offset = max(0, offset - evidence_total)
        # Text search is a discovery aid only, even if the source uses confident wording.
        options = {'offset': text_offset} if text_offset else {}
        if part_query:
            options['part_query'] = part_query
        candidates = self.repository.vehicle_catalog_candidates(
            vehicle, limit=text_limit, exclude_ids=list(entries), **options,
        )
        result['has_more'] = (evidence_total > offset + limit or candidates['has_more']
                              or (len(items) == limit and bool(candidates['items'])))
        for product in candidates['items'][:limit - len(items)]:
            items.append({
                'product': product, 'status': 'UNKNOWN', 'compatible': None,
                'match_type': 'CATALOG_TEXT', 'evidence': [],
                'warnings': ['სახელით/მოდელით დამთხვევაა; წელთან და ძრავთან შესაბამისობა უცნობია.'],
            })
        result['items'] = items[:limit]
        if result['has_more']:
            result['next_offset'] = offset + len(result['items'])
        result['verified_count'] = sum(item['status'] == 'VERIFIED' for item in result['items'])
        result['candidate_count'] = len(result['items']) - result['verified_count']
        if any(item['status'] == 'CONFLICTED' for item in result['items']):
            result['status'] = 'CONFLICTED'
        elif result['items']:
            result['status'] = 'VERIFIED' if not result['candidate_count'] else 'PROVISIONAL'
        if not result['verified_count']:
            result['clarification'] = MISSING_EVIDENCE
        if not vehicle.get('engine_code'):
            result['warnings'].append(
                'ძრავის კოდი უცნობია; ძრავზე დამოკიდებული ნაწილების შესაბამისობა ვერ დადასტურდება.',
            )
        return result

    def find_all(self, vehicle: dict, max_items: int = 2000, deadline: float | None = None,
                 entries: dict | None = None, part_query: str = '') -> dict:
        """Read all available pages within a declared bound, preparing evidence once."""
        if not all(vehicle.get(k) for k in ('make', 'model', 'model_year')):
            return self.find(vehicle, part_query=part_query)
        if entries is None:
            entries = self.fitment_entries(vehicle)
        max_items = max(1, min(max_items, 2000))
        combined: list[dict] = []
        offset = 0
        while True:
            try:
                page = self._page(vehicle, entries, min(100, max_items - len(combined)),
                                  offset, part_query)
            except Exception:
                if not combined:
                    raise
                page['has_more'] = True
                page['error_code'] = 'DATABASE_UNAVAILABLE'
                page['warnings'].append('შემდეგი გვერდი ვერ ჩაიტვირთა; სია ნაწილობრივია.')
                break
            combined.extend(page['items'])
            if (not page['has_more'] or not page['items'] or len(combined) >= max_items
                    or (deadline is not None and monotonic() >= deadline)):
                break
            offset = page['next_offset']
        # Concurrent catalog changes may repeat rows between SQL pages; keep current IDs unique.
        unique = {item['product']['source_product_id']: item for item in combined}
        page['items'] = list(unique.values())
        page['offset'] = 0
        page['verified_count'] = sum(i['status'] == 'VERIFIED' for i in page['items'])
        page['candidate_count'] = len(page['items']) - page['verified_count']
        if any(i['status'] == 'CONFLICTED' for i in page['items']):
            page['status'] = 'CONFLICTED'
        elif page['items']:
            page['status'] = 'PROVISIONAL' if page['candidate_count'] else 'VERIFIED'
        if page['verified_count']:
            page['clarification'] = None
        return page

    def _evidence_products(self, vehicle: dict, records: list[dict], entries: dict) -> None:
        products = self.repository.inventory_products()
        by_id = {product['source_product_id']: product for product in products}
        codes = defaultdict(list)
        for product in products:
            for code in product_codes(product):
                codes[code.normalized].append((product, code))
        claims = defaultdict(list)
        direct_claims = defaultdict(list)
        for row in records:
            part_code = normalize_code(str(row.get('part_code', row.get('code', ''))))
            product_id = str(row.get('product_id', row.get('source_product_id', '')))
            candidates = ({product_id: by_id[product_id]} if product_id in by_id else {})
            if not product_id:
                candidates.update({p['source_product_id']: p for p, _ in codes[part_code]})
            for key, product in candidates.items():
                fingerprint = row.get('source_fingerprint')
                if fingerprint and fingerprint != product.get('source_fingerprint'):
                    continue
                claims[key].append(row)
                manufacturer = str(row.get('manufacturer', '')).strip().casefold()
                contexts = {
                    (str(p.get('brand', '')).strip().casefold(), code.original.upper())
                    for p, code in codes[part_code]
                    if (not manufacturer or
                        str(p.get('brand', '')).strip().casefold() == manufacturer)
                }
                exact_code = bool(part_code) and (
                    normalize_code(str(product.get('product_code', ''))) == part_code
                )
                exact_brand = bool(manufacturer) and (
                    str(product.get('brand', '')).strip().casefold() == manufacturer
                )
                identity = (bool(product_id and fingerprint) or
                            (exact_code and exact_brand and len(contexts) == 1))
                if identity and _year_scoped(row):
                    direct_claims[key].append(row)
        for key, rows in claims.items():
            evaluated = evaluate_compatibility(direct_claims[key] or rows, vehicle)
            status = evaluated['status']
            warnings = evaluated['warnings']
            if not direct_claims[key]:
                if status == 'VERIFIED':
                    status = 'PROVISIONAL'
                warnings.append(
                    'საწყობის კონკრეტულ პროდუქტთან კავშირი ან წლის გამოყენება დასაზუსტებელია; '
                    'ერთ ჩანაწერში კოდების ხსენება ურთიერთჩანაცვლებას არ ნიშნავს.',
                )
            entries[key] = {
                'product': by_id[key], 'status': status,
                'compatible': evaluated['compatible'] if direct_claims[key] else None,
                'match_type': 'VERIFIED_FITMENT' if status == 'VERIFIED' else 'FITMENT_CANDIDATE',
                'evidence': rows, 'warnings': warnings,
            }
