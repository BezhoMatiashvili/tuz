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
from threading import RLock
from typing import Any

from automotive.categories import matches_category
from automotive.evidence import applies_to, flatten_record
from automotive.motorium import MotoriumError
from automotive.recommendations import RecommendationService
from automotive.repository import AutomotiveRepository
from automotive.vehicle_inventory import VehicleInventoryService
from automotive.vin import validate_vin

DEFAULT_QUERIES = ('', 'ზეთის ფილტრი', 'ჰაერის ფილტრი', 'სალონის ფილტრი',
                   'სამუხრუჭე ხუნდი', 'სამუხრუჭე დისკი', 'ამორტიზატორი', 'საკიდარი')
SOURCE_LABELS = {
    'vpic': 'vPIC · ავტომობილი', 'warehouse': 'ჩვენი საწყობის კატალოგი',
    'fitment': 'შესაბამისობის ჩანაწერები',
    'maintenance': 'სერვისის მოთხოვნები და პროდუქტის დაშვებები', 'motorium': 'Motorium',
    'catalog_codes': 'ნაპოვნი კოდები ჩვენს საწყობში',
    'cross_reference': 'მიმართული გადაკვეთის მტკიცებულება',
}
TERMINAL = {'COMPLETE', 'PARTIAL', 'FAILED', 'CONFLICTED'}


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
                 budget_seconds: float = 75, ttl_seconds: float = 300,
                 queries: tuple[str, ...] = DEFAULT_QUERIES) -> None:
        self.repository = repository
        self.vin_service = vin_service
        self.motorium_service = motorium_service
        self.hmac_key = hmac_key.encode()
        self.motorium_enabled = motorium_enabled
        self.budget_seconds, self.ttl_seconds = budget_seconds, ttl_seconds
        self.queries = queries
        self._jobs: dict[str, dict] = {}
        self._lock = RLock()
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vin-search')

    def close(self) -> None:
        self._workers.shutdown(wait=False, cancel_futures=True)

    def start(self, vin: str, part_query: str = '', model_year: int | None = None,
              configuration: dict | None = None) -> dict:
        vin = validate_vin(vin)
        material = json.dumps([vin, part_query, model_year, configuration], sort_keys=True)
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
            if len(self._jobs) >= 20:
                raise RuntimeError('SEARCH_CAPACITY')
            identifier = uuid.uuid4().hex
            self._jobs[identifier] = {
                'fingerprint': fingerprint, 'expires': now + self.ttl_seconds,
                'status': 'RUNNING', 'vehicle': {}, 'warnings': [
                    'ყველა ჩართული წყარო მოწმდება; სრული ქარხნული ნაწილების სიის დაფარვა '
                    'არ დასტურდება. წყაროს ჩანაწერი მარაგის რაოდენობას არ ნიშნავს.',
                ], 'created_at': datetime.now(UTC).isoformat(), 'local': {}, 'supplied': [],
                'motorium_vehicle': {}, 'sources': {
                    k: {'source_id': k, 'label': label, 'status': 'PENDING', 'count': 0,
                        'completed_queries': 0, 'total_queries': 0, 'coverage_complete': False,
                        'error_code': None} for k, label in SOURCE_LABELS.items()
                }, 'maintenance': None, 'code_local': [], 'cross_references': [],
                'part_query': part_query,
            }
            self._workers.submit(self._run, identifier, vin, part_query, model_year, configuration)
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
                'maintenance': job['maintenance'],
                'cross_references': job['cross_references'],
            })

    def _source(self, identifier: str, source: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(identifier)
            if job and job['status'] not in TERMINAL:
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
        queries = (query,) if query.strip() else self.queries
        self._source(identifier, 'motorium', status='RUNNING', total_queries=len(queries))
        partial = False
        for index, category in enumerate(queries):
            if time.monotonic() >= deadline:
                self._source(identifier, 'motorium', status='PARTIAL', error_code='TIME_LIMIT')
                return
            try:
                result = self.motorium_service.lookup(vin, category, limit=6, deadline=deadline)
            except MotoriumError as error:
                self._source(identifier, 'motorium', status='PARTIAL' if index else 'ERROR',
                             error_code=error.code)
                return
            except Exception:
                self._source(identifier, 'motorium', status='PARTIAL' if index else 'ERROR',
                             error_code='PROVIDER_UNAVAILABLE')
                return
            with self._lock:
                job = self._jobs.get(identifier)
                if not job or job['status'] in TERMINAL:
                    return
                if result.get('vehicle_context_matched'):
                    incoming = result.get('vehicle', {})
                    if (job['motorium_vehicle'] and
                            not same_vehicle(incoming, job['motorium_vehicle'])):
                        self._source(identifier, 'motorium', status='CONFLICTED',
                                     error_code='VEHICLE_CONTEXT_MISMATCH')
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
                partial |= (bool(result.get('has_more')) or
                            not result.get('vehicle_context_matched'))
                unique = {i['provider_product_id'] for i in job['supplied']}
                self._source(identifier, 'motorium', count=len(unique),
                             completed_queries=index + 1)
            if result.get('status') == 'CONFLICTED' and not result.get('items'):
                self._source(identifier, 'motorium', status='CONFLICTED',
                             error_code='VEHICLE_CONTEXT_MISMATCH')
                return
        with self._lock:
            supplied = self._jobs[identifier]['supplied']
            conflicted = any(i['status'] == 'CONFLICTED' for i in supplied)
            self._source(identifier, 'motorium', status='CONFLICTED' if conflicted else
                         'PARTIAL' if partial else ('OK' if supplied else 'EMPTY'))

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

    def _codes(self, identifier: str, deadline: float) -> None:
        """Bridge supplier identifiers to our catalog, retaining candidate uncertainty."""
        with self._lock:
            job = self._jobs[identifier]
            vehicle = dict(job['vehicle'])
            supplied = (job['supplied'] if same_vehicle(vehicle, job['motorium_vehicle']) else [])
            supplied = [i for i in supplied if i['status'] != 'CONFLICTED']
            codes = [(i['mpn'], i.get('manufacturer')) for i in supplied]
            codes += [(code, None) for i in supplied for code in i.get('oem_codes', [])]
            local_codes = [(i['product'].get('product_code'), i['product'].get('brand'))
                           for i in job['local'].get('items', [])]
            part_query = job['part_query']
        codes = list(dict.fromkeys((c, m) for c, m in codes if c))
        self._source(identifier, 'catalog_codes', status='RUNNING',
                     total_queries=len(codes))
        repository = self.repository
        if isinstance(repository, AutomotiveRepository):
            repository = AutomotiveRepository(repository.database_url, statement_timeout_ms=5000)
        rows: dict[str, dict] = {}
        partial = len(codes) > 24
        reason = 'RESULT_LIMIT' if partial else None
        failed = False
        for index, (code, manufacturer) in enumerate(codes[:24]):
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
                if not matches_category(product, part_query):
                    continue
                rows[product['source_product_id']] = {
                    'product': product, 'status': 'UNKNOWN', 'compatible': None,
                    'match_type': 'CODE_CANDIDATE', 'evidence': [],
                    'warnings': ['წყაროს კოდით ნაპოვნი საწყობის კანდიდატია; '
                                 'VIN-თან შესაბამისობა და ურთიერთჩანაცვლება დაუდასტურებელია.'],
                }
            if not self._store(identifier, code_local=list(rows.values())):
                return
            self._source(identifier, 'catalog_codes', count=len(rows), completed_queries=index + 1)
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

    def _run(self, identifier: str, vin: str, query: str, year: int | None,
             configuration: dict | None) -> None:
        deadline = time.monotonic() + self.budget_seconds
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='vin-source')
        try:
            self._source(identifier, 'vpic', status='RUNNING')
            pending = {
                pool.submit(self.vin_service.decode, vin, year, deadline=deadline): 'vpic',
                pool.submit(self._motorium, identifier, vin, query,
                            deadline - min(15, self.budget_seconds * 0.2)): 'motorium',
            }
            vpic_done, local_started, codes_started = False, False, False
            while pending and time.monotonic() < deadline:
                finished, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
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
                        elif source == 'motorium':
                            self._source(identifier, 'motorium', status='ERROR',
                                         error_code='PROVIDER_UNAVAILABLE')
                        else:
                            self._source(identifier, 'warehouse', status='ERROR',
                                         error_code='DATABASE_UNAVAILABLE')
                    if source == 'vpic':
                        vpic_done = True
                with self._lock:
                    job = self._jobs[identifier]
                    if vpic_done and not job['vehicle'] and job['motorium_vehicle']:
                        job['vehicle'] = dict(job['motorium_vehicle'])
                        job['warnings'].append(
                            'vPIC ვერ ჩაიტვირთა; გამოყენებულია Motorium-ის იდენტიფიკაცია.')
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
                        pending[pool.submit(self._local, identifier, vehicle, deadline)] = 'local'
                        local_started = True
                    if (job['vehicle'] and local_started and not codes_started
                            and 'motorium' not in pending.values()
                            and 'local' not in pending.values()
                            and time.monotonic() < deadline):
                        pending[pool.submit(self._codes, identifier, deadline)] = 'codes'
                        codes_started = True
            with self._lock:
                job = self._jobs[identifier]
                if job['vehicle'] and job['motorium_vehicle'] and not same_vehicle(
                    job['vehicle'], job['motorium_vehicle'],
                ):
                    self._source(identifier, 'motorium', status='CONFLICTED',
                                 error_code='VEHICLE_CONTEXT_MISMATCH')
                for source in job['sources'].values():
                    if source['status'] in ('RUNNING', 'PENDING'):
                        source.update(status='SKIPPED' if not local_started else 'ERROR',
                                      error_code='TIME_LIMIT' if pending else 'NO_VEHICLE')
                failed = any(s['status'] in ('ERROR', 'PARTIAL', 'CONFLICTED', 'SKIPPED')
                             for s in job['sources'].values())
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
