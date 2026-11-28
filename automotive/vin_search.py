"""Bounded VIN federation with ephemeral progress, provenance and strict identity merges."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
import unicodedata
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from threading import Event, RLock
from typing import Any

from automotive.catalog_api import categories_for
from automotive.categories import category_for_query, matches_category
from automotive.codes import normalize_code
from automotive.evidence import applies_to, flatten_record
from automotive.motorium import MotoriumError
from automotive.public_http import PublicSourceError
from automotive.recommendations import RecommendationService
from automotive.repository import AutomotiveRepository
from automotive.rules import Rules, oem_code_ok, vehicle_consistent
from automotive.vehicle_inventory import VehicleInventoryService
from automotive.vin import validate_vin

DEFAULT_QUERIES = ('', 'ზეთის ფილტრი', 'ჰაერის ფილტრი', 'სალონის ფილტრი',
                   'სამუხრუჭე ხუნდი', 'სამუხრუჭე დისკი', 'ამორტიზატორი', 'საკიდარი')
SOURCE_LABELS = {
    'vpic': 'vPIC · ავტომობილი', 'warehouse': 'ჩვენი საწყობის კატალოგი',
    'fitment': 'შესაბამისობის ჩანაწერები',
    'maintenance': 'სერვისის მოთხოვნები და პროდუქტის დაშვებები', 'motorium': 'Motorium',
    'fuchs': 'Fuchs · ზეთის შერჩევა', 'shell': 'Shell · ზეთის შერჩევა',
    'pakelo': 'Pakelo · ზეთის შერჩევა', 'jsfilter': 'JS Asakashi · ფილტრები',
    'brembo': 'Brembo · სამუხრუჭე ნაწილები',
    'catalog_api': 'კატალოგის API · OE და ნაწილების კოდები',
    'catalog_codes': 'ნაპოვნი კოდები ჩვენს საწყობში',
    'cross_reference': 'მიმართული გადაკვეთის მტკიცებულება',
}
TERMINAL = {'COMPLETE', 'PARTIAL', 'FAILED', 'CONFLICTED'}
# Motorium is asked for the oil filter; oil itself is chosen by grade, not by code.
OIL_CHANGE_QUERY = 'ზეთის ფილტრი'
# Seconds a fallback source may take before it is abandoned (it is only a backup).
FALLBACK_SECONDS = {'motorium': 7.0, 'jsfilter': 5.0, 'pakelo': 4.0, 'fuchs': 4.0, 'shell': 6.0,
                    'brembo': 12.0}
OIL_FALLBACK_DELAY = 1.0
FALLBACK_SOURCES = {'motorium', 'jsfilter', 'pakelo', 'fuchs', 'shell', 'brembo'}
# Source outcomes that are expected and never make an answer partial.
QUIET_CODES = ('NOT_REQUESTED', 'VIN_REQUIRED', 'NOT_NEEDED')
MAX_CODE_LOOKUPS = 60


def _key(value: Any) -> str:
    # Keep punctuation, slashes and full identifiers. A normalization collision is not identity.
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()


def same_vehicle(left: dict, right: dict) -> bool:
    return all(left.get(k) and right.get(k) and _key(left[k]) == _key(right[k])
               for k in ('make', 'model', 'model_year'))


def merge_parts(local: dict, supplied: list[dict]) -> list[dict]:
    """Merge only one unambiguous main-code/manufacturer identity, never OEM co-occurrence."""
    parts: dict[str, dict] = {}
    identities: dict[tuple[str, str], list[str]] = {}
    denied = {
        (_key(row['product'].get('brand')), _key(row['product'].get('product_code')))
        for row in local.get('excluded_items', [])
    }
    for row in local.get('items', []):
        product = row['product']
        identifier = 'warehouse:' + product['source_product_id']
        identity = (_key(product.get('brand')), _key(product.get('product_code')))
        parts[identifier] = {
            'part_id': identifier, 'name': product.get('product_name', ''),
            'manufacturer': product.get('brand', ''), 'main_code': product.get('product_code', ''),
            'status': row['status'], 'compatible': row.get('compatible'),
            'warehouse_records': [row], 'supplier_records': [],
            'source_names': ['warehouse'], 'warnings': list(row.get('warnings', [])),
        }
        if row.get('evidence') and row.get('match_type') != 'CODE_CANDIDATE':
            parts[identifier]['source_names'].append('fitment')
        if all(identity):
            identities.setdefault(identity, []).append(identifier)
    for item in supplied:
        identity = (_key(item.get('manufacturer')), _key(item.get('mpn')))
        if all(identity) and identity in denied:
            continue
        matches = identities.get(identity, []) if all(identity) else []
        signature = hashlib.sha256(repr(identity).encode()).hexdigest()[:12]
        identifier = (matches[0] if len(matches) == 1 and item['status'] != 'CONFLICTED'
                      else f"motorium:{item['provider_product_id']}:{signature}")
        if identifier not in parts:
            parts[identifier] = {
                'part_id': identifier, 'name': item.get('name') or '',
                'manufacturer': item.get('manufacturer') or '', 'main_code': item['mpn'],
                'status': item['status'], 'compatible': None, 'warehouse_records': [],
                'supplier_records': [], 'source_names': [], 'warnings': [],
            }
        row = parts[identifier]
        if item not in row['supplier_records']:
            row['supplier_records'].append(item)
        if item['status'] == 'CONFLICTED':
            row['status'] = 'CONFLICTED'
        if 'motorium' not in row['source_names']:
            row['source_names'].append('motorium')
        # Adding a retailer assertion never upgrades human fitment verification.
    ranks = {'VERIFIED': 0, 'CONFLICTED': 1, 'PROVISIONAL': 2, 'UNKNOWN': 3}
    return sorted(parts.values(), key=lambda p: (ranks[p['status']], p['part_id']))


class VINSearchCoordinator:
    """Read jobs live in memory for five minutes; no raw VIN is stored in snapshots."""

    def __init__(self, repository: Any, vin_service: Any, motorium_service: Any,
                 hmac_key: str, *, motorium_enabled: bool = True,
                 fuchs_service: Any = None, shell_service: Any = None,
                 pakelo_service: Any = None, jsfilter_service: Any = None,
                 catalog_service: Any = None, brembo_service: Any = None,
                 fuchs_enabled: bool = True, shell_enabled: bool = True,
                 pakelo_enabled: bool = True, jsfilter_enabled: bool = True,
                 brembo_enabled: bool = True,
                 budget_seconds: float = 75, ttl_seconds: float = 300, rules: Any = None,
                 queries: tuple[str, ...] = DEFAULT_QUERIES, cache: Any = None) -> None:
        self.repository = repository
        self.vin_service = vin_service
        self.motorium_service = motorium_service
        self.hmac_key = hmac_key.encode()
        self.motorium_enabled = motorium_enabled
        self._external = {
            'fuchs': (fuchs_service, bool(fuchs_enabled and fuchs_service is not None)),
            'shell': (shell_service, bool(shell_enabled and shell_service is not None)),
            'pakelo': (pakelo_service, bool(pakelo_enabled and pakelo_service is not None)),
            'jsfilter': (jsfilter_service, bool(jsfilter_enabled and jsfilter_service is not None)),
            'brembo': (brembo_service, bool(brembo_enabled and brembo_service is not None)),
        }
        self._catalog = catalog_service
        self.cache = cache
        self.rules = rules or Rules()
        self.budget_seconds, self.ttl_seconds = budget_seconds, ttl_seconds
        self.queries = queries
        self._jobs: dict[str, dict] = {}
        self._lock = RLock()
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vin-search')
        self._warm = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vin-warm')
        self._reports: dict[tuple, tuple[float, dict]] = {}

    def close(self) -> None:
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._warm.shutdown(wait=False, cancel_futures=True)

    def prewarm(self, vin: str) -> None:
        """Start resolving the vehicle as soon as a VIN is seen; failures are silent."""
        if self._catalog is None or not hasattr(self._catalog, 'warm'):
            return
        try:
            vin = validate_vin(vin)
            self._warm.submit(self._prewarm, vin)
        except Exception:  # A warm-up must never affect the customer's request.
            return

    def _prewarm(self, vin: str) -> None:
        try:
            self._catalog.warm(vin, time.monotonic() + 15)
        except Exception:
            return

    def start(self, vin: str | None, part_query: str = '', model_year: int | None = None,
              configuration: dict | None = None, *, deadline: float | None = None) -> dict:
        """Start a read job. Without a VIN, a full make/model/year configuration is required."""
        if vin is None:
            if not configuration or not all(
                configuration.get(k) for k in ('make', 'model', 'model_year')
            ):
                raise ValueError('VEHICLE_REQUIRED')
        else:
            vin = validate_vin(vin)
        material = json.dumps([vin, part_query, model_year, configuration,
                               deadline is not None], sort_keys=True)
        fingerprint = hmac.new(self.hmac_key, material.encode(), hashlib.sha256).hexdigest()
        now = time.monotonic()
        with self._lock:
            for key in list(self._jobs):
                job = self._jobs[key]
                if job['expires'] < now and job['status'] in TERMINAL:
                    del self._jobs[key]
            for key, job in self._jobs.items():
                if job['fingerprint'] == fingerprint:
                    return self.get(key)
            # Finished snapshots make room, oldest first, after a short grace period so a
            # reader paging a just-finished search keeps it; 20 running searches are refused.
            finished = sorted((job['expires'], key) for key, job in self._jobs.items()
                              if job['status'] in TERMINAL
                              and now - (job['expires'] - self.ttl_seconds) > 30)
            while len(self._jobs) >= 20 and finished:
                del self._jobs[finished.pop(0)[1]]
            if len(self._jobs) >= 20:
                raise RuntimeError('SEARCH_CAPACITY')
            identifier = uuid.uuid4().hex
            self._jobs[identifier] = {
                'fingerprint': fingerprint, 'expires': now + self.ttl_seconds,
                'status': 'RUNNING',
                'vehicle': dict(configuration) if vin is None else {}, 'warnings': [
                    'ყველა ჩართული წყარო მოწმდება; სრული ქარხნული ნაწილების სიის დაფარვა '
                    'არ დასტურდება. წყაროს ჩანაწერი მარაგის რაოდენობას არ ნიშნავს.',
                ] + ([] if vin is not None else [
                    'VIN არ არის მითითებული; ძიება მწარმოებლის, მოდელისა და წლის მიხედვითაა.',
                ]), 'created_at': datetime.now(UTC).isoformat(), 'local': {}, 'supplied': [],
                'motorium_vehicle': {}, 'sources': {
                    k: {'source_id': k, 'label': label, 'status': 'PENDING', 'count': 0,
                        'completed_queries': 0, 'total_queries': 0, 'coverage_complete': False,
                        'error_code': None} for k, label in SOURCE_LABELS.items()
                }, 'maintenance': None, 'fluids': [], 'catalog_items': [],
                'code_local': [], 'cross_references': [],
                'part_query': part_query,
            }
            self._workers.submit(self._run, identifier, vin, part_query, model_year, configuration,
                                 deadline)
            return self.get(identifier)

    def get(self, identifier: str, offset: int = 0, limit: int = 50) -> dict | None:
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None or (job['expires'] < time.monotonic() and job['status'] in TERMINAL):
                return None
            supplied = job['supplied']
            if (not job['vehicle'] or (job['motorium_vehicle'] and
                    not same_vehicle(job['vehicle'], job['motorium_vehicle']))):
                supplied = []
            if job.get('part_query'):
                supplied = [item for item in supplied if matches_category(
                    {'product_name': item.get('name')}, job['part_query'],
                )]
            local = copy.deepcopy(job['local'])
            existing = {i['product']['source_product_id'] for i in local.get('items', [])}
            denied = {i['product']['source_product_id']
                      for i in local.get('excluded_items', [])}
            local.setdefault('items', []).extend(
                i for i in job['code_local']
                if i['product']['source_product_id'] not in existing | denied
            )
            items = merge_parts(local, supplied)
            offset, limit = max(0, offset), max(1, min(limit, 100))
            return copy.deepcopy({
                'search_id': identifier, 'status': job['status'], 'vehicle': job['vehicle'],
                'sources': list(job['sources'].values()), 'warnings': job['warnings'],
                'items': items[offset:offset + limit], 'total': len(items), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(items) else None,
                'verified_count': sum(p['status'] == 'VERIFIED' for p in items),
                'candidate_count': sum(p['status'] != 'VERIFIED' for p in items),
                'coverage_complete': False, 'created_at': job['created_at'],
                'maintenance': job['maintenance'], 'fluids': job['fluids'],
                'cross_references': job['cross_references'],
            })

    def _source(self, identifier: str, source: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(identifier)
            if job and job['status'] not in TERMINAL:
                timers = job.setdefault('timers', {})
                status = updates.get('status')
                if status == 'RUNNING':
                    timers.setdefault(source, time.monotonic())
                elif status in ('OK', 'EMPTY', 'ERROR', 'PARTIAL', 'CONFLICTED') \
                        and source in timers and 'elapsed_ms' not in job['sources'][source]:
                    updates['elapsed_ms'] = int((time.monotonic() - timers[source]) * 1000)
                job['sources'][source].update(updates)

    def _store(self, identifier: str, **updates: Any) -> bool:
        with self._lock:
            job = self._jobs.get(identifier)
            if not job or job['status'] in TERMINAL:
                return False
            job.update(updates)
            return True

    def _motorium(self, identifier: str, vin: str, query: str, deadline: float) -> None:
        if not self.motorium_enabled:
            self._source(identifier, 'motorium', status='DISABLED')
            return
        queries = ((OIL_CHANGE_QUERY if category_for_query(query) == 'oil_change' else query),
                   ) if query.strip() else self.queries
        self._source(identifier, 'motorium', status='RUNNING', total_queries=len(queries))
        state: dict[str, Any] = {'partial': False, 'error': None, 'conflict': False,
                                 'succeeded': 0, 'done': 0}
        stop = Event()

        def fail(code: str) -> None:
            with self._lock:
                state['error'] = state['error'] or code

        def one(category: str) -> None:
            if stop.is_set() or state['conflict']:
                return
            if time.monotonic() >= deadline:
                fail('TIME_LIMIT')
                return
            try:
                result = self.motorium_service.lookup(vin, category, limit=6, deadline=deadline)
            except MotoriumError as error:
                if error.code in ('RATE_LIMITED', 'ACCESS_REQUIRED'):
                    stop.set()
                fail(error.code)
                return
            except Exception:
                fail('PROVIDER_UNAVAILABLE')
                return
            with self._lock:
                job = self._jobs.get(identifier)
                if not job or job['status'] in TERMINAL:
                    stop.set()
                    return
                if result.get('vehicle_context_matched'):
                    incoming = result.get('vehicle', {})
                    if (job['motorium_vehicle'] and
                            not same_vehicle(incoming, job['motorium_vehicle'])):
                        state['conflict'] = True
                        return
                    job['motorium_vehicle'] = incoming
                    for item in result.get('items', []):
                        for previous in job['supplied']:
                            same_id = previous['provider_product_id'] == item['provider_product_id']
                            different = any(_key(previous.get(k)) != _key(item.get(k))
                                            for k in ('mpn', 'manufacturer'))
                            different |= bool(previous.get('oem_codes') and
                                              item.get('oem_codes') and
                                              set(previous['oem_codes']) != set(item['oem_codes']))
                            if same_id and different:
                                for row in (previous, item):
                                    row['status'] = 'CONFLICTED'
                                    row['warnings'].append('PRODUCT_IDENTITY_CONFLICT')
                        job['supplied'].append(item)
                # A result list longer than our six-product page is normal; only an
                # unestablished vehicle or an exhausted deadline makes the source partial.
                state['partial'] |= (not result.get('vehicle_context_matched')
                                     or 'TIME_LIMIT' in result.get('warnings', []))
                state['succeeded'] += 1
                state['done'] += 1
                unique = {i['provider_product_id'] for i in job['supplied']}
                self._source(identifier, 'motorium', count=len(unique),
                             completed_queries=state['done'])
                if result.get('status') == 'CONFLICTED' and not result.get('items'):
                    state['conflict'] = True

        # Category lookups are independent reads: a small pool keeps a broad question fast.
        pool = ThreadPoolExecutor(max_workers=min(4, len(queries)),
                                  thread_name_prefix='vin-motorium')
        try:
            futures = [pool.submit(one, category) for category in queries]
            wait(futures, timeout=max(0, deadline - time.monotonic()) + 1)
            if any(not future.done() for future in futures):
                stop.set()
                fail('TIME_LIMIT')
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            job = self._jobs.get(identifier)
            if not job or job['status'] in TERMINAL:
                return
            supplied = job['supplied']
            conflicted = state['conflict'] or any(i['status'] == 'CONFLICTED' for i in supplied)
            if state['conflict']:
                self._source(identifier, 'motorium', status='CONFLICTED',
                             error_code='VEHICLE_CONTEXT_MISMATCH')
            elif state['error']:
                self._source(identifier, 'motorium',
                             status='PARTIAL' if state['succeeded'] else 'ERROR',
                             error_code=state['error'])
            else:
                self._source(identifier, 'motorium', status='CONFLICTED' if conflicted else
                             'PARTIAL' if state['partial'] else ('OK' if supplied else 'EMPTY'))

    def _local(self, identifier: str, vehicle: dict, deadline: float) -> None:
        for source in ('warehouse', 'fitment', 'maintenance'):
            self._source(identifier, source, status='RUNNING')
        repository = self.repository
        if isinstance(repository, AutomotiveRepository):
            repository = AutomotiveRepository(repository.database_url, statement_timeout_ms=10000)
        service = VehicleInventoryService(repository)
        with self._lock:
            part_query = self._jobs[identifier]['part_query']
        entries = {}
        try:
            entries = service.fitment_entries(vehicle)
            self._source(identifier, 'fitment', count=len(entries),
                         status='OK' if entries else 'EMPTY')
        except Exception:
            self._source(identifier, 'fitment', status='ERROR', error_code='DATABASE_UNAVAILABLE')
        try:
            local = service.find_all(vehicle, deadline=deadline, entries=entries,
                                     part_query=part_query)
            if not self._store(identifier, local=local):
                return
            self._source(identifier, 'warehouse', count=len(local['items']),
                         status='PARTIAL' if local['has_more'] else
                         ('OK' if local['items'] else 'EMPTY'),
                         coverage_complete=not local['has_more'],
                         error_code=local.get('error_code'))
        except Exception:
            self._source(identifier, 'warehouse', status='ERROR', error_code='DATABASE_UNAVAILABLE')
        if time.monotonic() >= deadline:
            return
        try:
            maintenance = RecommendationService(repository).recommend_oil(
                vehicle, deadline=deadline,
            )
            if not self._store(identifier, maintenance=maintenance):
                return
            self._source(identifier, 'maintenance', count=len(maintenance['items']),
                         status='PARTIAL' if maintenance.get('has_more') else
                         'OK' if maintenance['status'] == 'VERIFIED' else
                         'CONFLICTED' if maintenance['status'] == 'CONFLICTED'
                         else 'INSUFFICIENT_DATA')
        except Exception:
            self._source(identifier, 'maintenance', status='ERROR',
                         error_code='DATABASE_UNAVAILABLE')

    @staticmethod
    def _filter_codes(job: dict) -> list[tuple[str, str | None]]:
        """Filter part numbers published for this vehicle by the JS Asakashi catalogue."""
        wanted = {'oil_filter': 'OIL', 'air_filter': 'AIR', 'cabin_filter': 'CABIN',
                  'fuel_filter': 'FUEL'}.get(
            category_for_query(job['part_query']) if job['part_query'].strip() else '',
        )
        codes: list[tuple[str, str | None]] = []
        for report in job['fluids']:
            if report.get('provider') != 'JS Asakashi':
                continue
            for item in report.get('items') or []:
                if item.get('product_code') and (wanted is None or item.get('component') == wanted):
                    codes.append((item['product_code'], None))
        return codes

    def _brand_ok(self, warehouse_brand: Any, supplier: Any) -> bool:
        """A part number is only trusted with its brand: `MANN` matches `MANN-FILTER`."""
        return self.rules.brands_match(warehouse_brand, supplier)

    def _catalog_matches(self, repository: Any, items: list[dict]) -> list[tuple[dict, str]]:
        """Warehouse products for catalog-API numbers: OE numbers any brand, part numbers
        only from the same brand (a bare number can collide with another brand's)."""
        wanted: dict[str, list[tuple[Any, str]]] = {}
        order: list[str] = []
        for item in items:  # OE numbers first: they are the most specific.
            for code in item.get('oem_codes', []):
                wanted.setdefault(normalize_code(code), []).append(
                    (None, f'Catalog API OE: {code}'))
                order.append(code)
        for item in items:
            wanted.setdefault(normalize_code(item['code']), []).append(
                (item.get('supplier') or '', f"Catalog API: {item['code']}"))
            order.append(item['code'])
        order = list(dict.fromkeys(c for c in order if normalize_code(c)))
        if callable(getattr(type(repository), 'search_parts_many', None)):
            found = repository.search_parts_many(order)
        else:
            found = {normalize_code(c): repository.search_parts(c, limit=100)['items']
                     for c in order[:MAX_CODE_LOOKUPS]}
        matches: list[tuple[dict, str]] = []
        for normalized, products in found.items():
            for product in products:
                for supplier, via in wanted.get(normalized, []):
                    if supplier is None or self._brand_ok(product.get('brand'), supplier):
                        matches.append((product, via))
        return matches

    def _codes(self, identifier: str, deadline: float) -> None:
        """Bridge supplier identifiers to our catalog, retaining candidate uncertainty."""
        with self._lock:
            job = self._jobs[identifier]
            vehicle = dict(job['vehicle'])
            supplied = (job['supplied'] if same_vehicle(vehicle, job['motorium_vehicle']) else [])
            supplied = [i for i in supplied if i['status'] != 'CONFLICTED']
            if job['part_query'].strip():
                supplied = [i for i in supplied if matches_category(
                    {'product_name': i.get('name')}, job['part_query'],
                )]
            sourced = [(i['mpn'], i.get('manufacturer'), 'Motorium') for i in supplied]
            # A short or all-digit OEM-labelled code with no brand can collide; drop it.
            sourced += [(code, None, 'Motorium') for i in supplied
                        for code in i.get('oem_codes', []) if oem_code_ok(code)]
            sourced += [(code, manufacturer, 'JS Asakashi')
                        for code, manufacturer in self._filter_codes(job)]
            catalog_items = list(job['catalog_items'])
            codes = [(c, m) for c, m, _ in sourced]
            origin: dict[tuple, str] = {}
            for code, manufacturer, name in sourced:
                origin.setdefault((code, manufacturer), name)
            local_codes = [(i['product'].get('product_code'), i['product'].get('brand'))
                           for i in job['local'].get('items', [])]
        codes = list(dict.fromkeys((c, m) for c, m in codes if c))
        self._source(identifier, 'catalog_codes', status='RUNNING',
                     total_queries=len(codes))
        repository = self.repository
        if isinstance(repository, AutomotiveRepository):
            repository = AutomotiveRepository(repository.database_url, statement_timeout_ms=5000)
        rows: dict[str, dict] = {}
        partial = len(codes) > MAX_CODE_LOOKUPS
        reason = 'RESULT_LIMIT' if partial else None
        failed = False
        for index, (code, manufacturer) in enumerate(codes[:MAX_CODE_LOOKUPS]):
            if time.monotonic() >= deadline:
                partial = True
                reason = 'TIME_LIMIT'
                break
            try:
                result = repository.search_parts(code, manufacturer=manufacturer, limit=100)
            except Exception:
                failed = True
                break
            partial |= len(result['items']) >= 100
            if len(result['items']) >= 100:
                reason = 'RESULT_LIMIT'
            for product in result['items']:
                # An exact supplier-code hit identifies the product; its catalog name
                # (often only a brand string) does not need to repeat the category.
                via = f'{origin.get((code, manufacturer), "")}: {code}'.strip(': ')
                known = rows.get(product['source_product_id'], {}).get('provenance', [])
                rows[product['source_product_id']] = {
                    'provenance': known + ([via] if via not in known else []),
                    'product': product, 'status': 'UNKNOWN', 'compatible': None,
                    'match_type': 'CODE_CANDIDATE', 'evidence': [],
                    'warnings': ['წყაროს კოდით ნაპოვნი საწყობის კანდიდატია; '
                                 'VIN-თან შესაბამისობა და ურთიერთჩანაცვლება დაუდასტურებელია.'],
                }
            if not self._store(identifier, code_local=list(rows.values())):
                return
            self._source(identifier, 'catalog_codes', count=len(rows), completed_queries=index + 1)
        if catalog_items and not failed and time.monotonic() < deadline:
            try:
                for product, via in self._catalog_matches(repository, catalog_items):
                    known = rows.get(product['source_product_id'], {}).get('provenance', [])
                    rows[product['source_product_id']] = {
                        'provenance': known + ([via] if via not in known else []),
                        'product': product, 'status': 'UNKNOWN', 'compatible': None,
                        'match_type': 'CODE_CANDIDATE', 'evidence': [],
                        'warnings': ['წყაროს კოდით ნაპოვნი საწყობის კანდიდატია; '
                                     'VIN-თან შესაბამისობა და ურთიერთჩანაცვლება დაუდასტურებელია.'],
                    }
            except Exception:
                failed = True
            if not self._store(identifier, code_local=list(rows.values())):
                return
            self._source(identifier, 'catalog_codes', count=len(rows))
        self._source(identifier, 'catalog_codes',
                     status='PARTIAL' if partial or (failed and rows) else 'ERROR' if failed
                     else 'OK' if rows else 'EMPTY',
                     error_code='DATABASE_UNAVAILABLE' if failed else reason)
        self._source(identifier, 'cross_reference', status='RUNNING')
        if time.monotonic() >= deadline:
            self._source(identifier, 'cross_reference', status='ERROR', error_code='TIME_LIMIT')
            return
        try:
            identities = {(_key(c), _key(m)) for c, m in codes + local_codes if c}
            references = []
            for raw in repository.cross_reference_records():
                row = flatten_record(raw)
                # Preserve forward direction and restrictions; never expand a target automatically.
                if (applies_to(row, vehicle) and
                    (_key(row.get('from_code')), _key(row.get('manufacturer'))) in identities):
                    references.append(row)
            self._store(identifier, cross_references=references)
            self._source(identifier, 'cross_reference', status='OK' if references else 'EMPTY',
                         count=len(references))
        except Exception:
            self._source(identifier, 'cross_reference', status='ERROR',
                         error_code='DATABASE_UNAVAILABLE')

    def _catalog_api(self, identifier: str, vin: str, vehicle: dict, query: str,
                     deadline: float) -> None:
        """VIN to OE and part numbers from the optional catalog API."""
        categories = categories_for(query)
        self._source(identifier, 'catalog_api', status='RUNNING', total_queries=len(categories))
        try:
            report = self._catalog.lookup(vin, vehicle, categories, deadline=deadline)
        except PublicSourceError as exc:
            self._source(identifier, 'catalog_api', status='ERROR', error_code=exc.code)
            return
        except Exception:
            self._source(identifier, 'catalog_api', status='ERROR',
                         error_code='PROVIDER_UNAVAILABLE')
            return
        status = {'PROVISIONAL': 'OK', 'EMPTY': 'EMPTY', 'CONFLICTED': 'CONFLICTED'}.get(
            str(report.get('status')), 'ERROR')
        with self._lock:
            job = self._jobs.get(identifier)
            if not job or job['status'] in TERMINAL:
                return
            job['catalog_items'] = list(report.get('items') or [])
            job['catalog_label'] = report.get('vehicle_label') or ''
        self._source(identifier, 'catalog_api', status=status, count=len(report.get('items') or []),
                     completed_queries=len(categories),
                     error_code='MULTIPLE_VEHICLES' if status == 'CONFLICTED' else None)

    def _fluid(self, identifier: str, source: str, service: Any, vin: str,
               vehicle: dict, deadline: float) -> None:
        self._source(identifier, source, status='RUNNING', total_queries=1)
        # Chooser answers depend only on the vehicle, so they are shared for a day.
        # v2: a report of several engines carries the grades each takes.
        cache_key = (source, 'v2', *(_key(vehicle.get(k)) for k in (
            'make', 'model', 'model_year', 'trim', 'fuel', 'fuel_type', 'body', 'engine_code',
            'displacement_l', 'drive', 'market')))
        with self._lock:
            hit = self._reports.get(cache_key)
        try:
            persistent = self.cache.get('chooser', cache_key) if self.cache is not None else None
            if isinstance(persistent, dict):
                report = persistent
            elif hit and hit[0] > time.monotonic():
                report = copy.deepcopy(hit[1])
            else:
                report = service.lookup(vin, vehicle, deadline=deadline)
                if isinstance(report, dict) and not report.get('partial') and report.get(
                        'status') in {'PROVISIONAL', 'EMPTY', 'CONFLICTED'}:
                    with self._lock:
                        if len(self._reports) > 500:
                            self._reports.clear()
                        ttl = 60 if report.get('status') == 'EMPTY' else 24 * 3600
                        self._reports[cache_key] = (time.monotonic() + ttl,
                                                    copy.deepcopy(report))
                    if self.cache is not None:
                        self.cache.put('chooser', cache_key, report,
                                       empty=report.get('status') == 'EMPTY')
        except PublicSourceError as exc:
            self._source(identifier, source, status='ERROR', error_code=exc.code,
                         completed_queries=1)
            return
        except Exception:
            self._source(identifier, source, status='ERROR', error_code='PROVIDER_UNAVAILABLE',
                         completed_queries=1)
            return
        if not isinstance(report, dict):
            self._source(identifier, source, status='ERROR', error_code='INVALID_RESPONSE',
                         completed_queries=1)
            return
        mapped = {'PROVISIONAL': 'OK', 'EMPTY': 'EMPTY', 'INSUFFICIENT_DATA': 'EMPTY',
                  'CONFLICTED': 'CONFLICTED'}.get(str(report.get('status')), 'ERROR')
        with self._lock:
            job = self._jobs.get(identifier)
            if not job or job['status'] in TERMINAL:
                return
            job['fluids'].append(report)
        self._source(
            identifier, source, status=mapped, count=len(report.get('items') or []),
            completed_queries=1, error_code='MULTIPLE_ENGINES' if mapped == 'CONFLICTED' else None,
        )

    @staticmethod
    def _scope(query: str) -> set[str]:
        """External choosers that can answer the requested category (all for a broad one)."""
        if not query.strip():
            return {'fuchs', 'shell', 'pakelo', 'jsfilter'}
        category = category_for_query(query)
        if category in ('engine_oil', 'oil_change'):
            return {'fuchs', 'shell', 'pakelo'}
        if category in ('oil_filter', 'air_filter', 'cabin_filter', 'fuel_filter'):
            return {'jsfilter'}
        # Brembo answers one car per brake question; a broad search never asks it.
        if category in ('brake_pads', 'brake_discs', 'brakes'):
            return {'brembo'}
        return set()

    def _run(self, identifier: str, vin: str | None, query: str, year: int | None,
             configuration: dict | None, turn_deadline: float | None = None) -> None:
        """Fast tier first (vPIC, catalog API, Shell); slower public sources only as fallbacks."""
        deadline = min(time.monotonic() + self.budget_seconds,
                       turn_deadline or float('inf'))
        scope = self._scope(query)
        oil_request = category_for_query(query) in ('engine_oil', 'oil_change')
        for name, (_service, enabled) in self._external.items():
            if not enabled:
                self._source(identifier, name, status='DISABLED')
            elif name not in scope:
                self._source(identifier, name, status='SKIPPED', error_code='NOT_REQUESTED')
            elif vin is None and name == 'jsfilter':
                self._source(identifier, name, status='SKIPPED', error_code='VIN_REQUIRED')
        use_catalog = False
        if self._catalog is None:
            self._source(identifier, 'catalog_api', status='DISABLED')
        elif not categories_for(query):
            self._source(identifier, 'catalog_api', status='SKIPPED', error_code='NOT_REQUESTED')
        else:
            use_catalog = True
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='vin-source')
        pending: dict = {}
        started: set[str] = set()

        def start(name: str, function: Any, *args: Any, **kwargs: Any) -> None:
            pending[pool.submit(function, *args, **kwargs)] = name
            started.add(name)

        def soft(seconds: float) -> float:
            return min(deadline, time.monotonic() + seconds)

        def start_motorium() -> None:
            if self.motorium_enabled and vin and 'motorium' not in started:
                start('motorium', self._motorium, identifier, vin, query,
                      min(deadline - min(15, self.budget_seconds * 0.2),
                          time.monotonic() + FALLBACK_SECONDS['motorium']))

        def start_fluid(name: str, vehicle: dict, seconds: float) -> None:
            service, enabled = self._external[name]
            if enabled and name in scope and name not in started and not (
                    vin is None and name == 'jsfilter'):
                start(name, self._fluid, identifier, name, service, vin, vehicle, soft(seconds))

        try:
            vpic_done = vin is None
            if vin is None:
                # Make, model and year select the vehicle; VIN-only providers do not run.
                self._source(identifier, 'vpic', status='SKIPPED', error_code='VIN_REQUIRED')
                self._source(identifier, 'motorium', status='SKIPPED', error_code='VIN_REQUIRED')
                if use_catalog:
                    start('catalog_api', self._catalog_api, identifier, None,
                          configuration or {}, query, soft(25))
            else:
                self._source(identifier, 'vpic', status='RUNNING')
                start('vpic', self.vin_service.decode, vin, year, deadline=deadline)
                if use_catalog:
                    start('catalog_api', self._catalog_api, identifier, vin, {}, query,
                          soft(25))
            local_started = codes_started = fluids_started = False
            code_fallback_decided = oil_fallback_decided = False
            oil_fallback_at = float('inf')
            vehicle_checked = not use_catalog
            first_pass = True
            while time.monotonic() < deadline:
                finished, _ = wait(pending, timeout=0.2 if pending else 0,
                                   return_when=FIRST_COMPLETED)
                for future in finished:
                    source = pending.pop(future)
                    try:
                        value = future.result()
                        if source == 'vpic':
                            vehicle = value.get('vehicle', {})
                            if not all(vehicle.get(k) for k in ('make', 'model', 'model_year')):
                                raise ValueError('INCOMPLETE_IDENTIFICATION')
                            with self._lock:
                                self._jobs[identifier]['vehicle'] = vehicle
                                self._jobs[identifier]['warnings'].extend(value.get('warnings', []))
                            self._source(identifier, 'vpic', status='OK', count=1)
                    except Exception:
                        if source == 'vpic':
                            self._source(identifier, 'vpic', status='ERROR',
                                         error_code='DECODE_FAILED')
                        elif source == 'local':
                            self._source(identifier, 'warehouse', status='ERROR',
                                         error_code='DATABASE_UNAVAILABLE')
                        elif source != 'codes':
                            self._source(identifier, source, status='ERROR',
                                         error_code='PROVIDER_UNAVAILABLE')
                    if source == 'vpic':
                        vpic_done = True
                launched = first_pass
                first_pass = False
                with self._lock:
                    job = self._jobs[identifier]
                    if vpic_done and not job['vehicle'] and job['motorium_vehicle']:
                        job['vehicle'] = dict(job['motorium_vehicle'])
                        job['warnings'].append(
                            'vPIC ვერ ჩაიტვირთა; გამოყენებულია Motorium-ის იდენტიფიკაცია.')
                    # Without a vehicle from vPIC, a catalog-only identity is not enough.
                    if vpic_done and not job['vehicle'] and 'motorium' not in started:
                        start_motorium()
                        launched = True
                    if job['vehicle'] and not local_started:
                        vehicle = dict(job['vehicle'])
                        if configuration:
                            if any(configuration.get(k) and
                                   _key(configuration[k]) != _key(vehicle[k])
                                   for k in ('make', 'model', 'model_year')):
                                job['warnings'].append('USER_CONFIGURATION_MISMATCH')
                            else:
                                vehicle.update({k: v for k, v in configuration.items()
                                                if v not in (None, '')})
                                job['warnings'].append(
                                    'დაზუსტებული კონფიგურაცია მომხმარებლისგანაა.')
                                job['vehicle'] = vehicle
                        start('local', self._local, identifier, vehicle, deadline)
                        local_started = launched = True
                    if job['vehicle'] and not fluids_started:
                        fluids_started = launched = True
                        start_fluid('shell', dict(job['vehicle']), FALLBACK_SECONDS['shell'])
                        start_fluid('brembo', dict(job['vehicle']), FALLBACK_SECONDS['brembo'])
                        if oil_request:
                            oil_fallback_at = time.monotonic() + OIL_FALLBACK_DELAY
                    shell_settled = fluids_started and 'shell' not in pending.values()
                    if job['vehicle'] and not oil_fallback_decided and (
                            shell_settled or time.monotonic() >= oil_fallback_at):
                        oil_fallback_decided = launched = True
                        # Shell that read every engine's grade needs no backup either.
                        grouped = any(report.get('provider') == 'Shell'
                                      and report.get('grade_groups') for report in job['fluids'])
                        if job['sources']['shell']['status'] != 'OK' and not grouped:
                            for name in ('pakelo', 'fuchs'):
                                start_fluid(name, dict(job['vehicle']),
                                            FALLBACK_SECONDS[name])
                    catalog_pending = 'catalog_api' in pending.values()
                    if use_catalog and not catalog_pending and vpic_done and not vehicle_checked:
                        vehicle_checked = True
                        self._check_catalog_vehicle(identifier, job)
                    if not code_fallback_decided and (
                            not use_catalog or (not catalog_pending and vpic_done)):
                        code_fallback_decided = launched = True
                        usable = (use_catalog and job['sources']['catalog_api']['status'] == 'OK'
                                  and bool(job['catalog_items']))
                        if not usable:
                            start_motorium()
                            if job['vehicle'] or vin is not None:
                                start_fluid('jsfilter', dict(job['vehicle']),
                                            FALLBACK_SECONDS['jsfilter'])
                    if (job['vehicle'] and local_started and code_fallback_decided
                            and not codes_started and vehicle_checked
                            and not {'local', 'catalog_api', 'motorium', 'jsfilter'}
                            & set(pending.values())
                            and time.monotonic() < deadline):
                        start('codes', self._codes, identifier, deadline)
                        codes_started = launched = True
                if not pending and not launched:
                    break
            with self._lock:
                job = self._jobs[identifier]
                if job['vehicle'] and job['motorium_vehicle'] and not same_vehicle(
                    job['vehicle'], job['motorium_vehicle'],
                ):
                    self._source(identifier, 'motorium', status='CONFLICTED',
                                 error_code='VEHICLE_CONTEXT_MISMATCH')
                for name, source in job['sources'].items():
                    if source['status'] == 'PENDING' and name in FALLBACK_SOURCES \
                            and name not in started and job['vehicle']:
                        source.update(status='SKIPPED', error_code='NOT_NEEDED')
                    elif source['status'] in ('RUNNING', 'PENDING'):
                        source.update(status='SKIPPED' if not local_started else 'ERROR',
                                      error_code='TIME_LIMIT' if pending else 'NO_VEHICLE')
                failed = any(
                    s['status'] in ('ERROR', 'PARTIAL', 'CONFLICTED', 'SKIPPED')
                    and s.get('error_code') not in QUIET_CODES
                    for s in job['sources'].values()
                )
                job['status'] = ('FAILED' if not job['vehicle'] else
                                 'PARTIAL' if failed else 'COMPLETE')
                job['expires'] = time.monotonic() + self.ttl_seconds
        except Exception:
            with self._lock:
                job = self._jobs[identifier]
                for source in job['sources'].values():
                    if source['status'] in ('RUNNING', 'PENDING'):
                        source.update(status='ERROR', error_code='SEARCH_UNAVAILABLE')
                job['status'] = 'PARTIAL' if job['vehicle'] else 'FAILED'
                job['expires'] = time.monotonic() + self.ttl_seconds
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _check_catalog_vehicle(self, identifier: str, job: dict) -> None:
        """Hard rule: catalog codes are ignored when its car contradicts the decoded one."""
        if (not job['catalog_items'] or not job.get('catalog_label')
                or job['sources']['vpic']['status'] != 'OK'):
            return
        if not vehicle_consistent(job['catalog_label'], job['vehicle']):
            job['catalog_items'] = []
            self._source(identifier, 'catalog_api', status='CONFLICTED',
                         error_code='VEHICLE_MISMATCH')
            job['warnings'].append(
                'კატალოგის მანქანა VIN-ის ამოცნობას არ ემთხვევა; მისი კოდები არ გამოიყენება.')
